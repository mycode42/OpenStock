"""Rename the LLM-signal strategy ID in persisted Algo state.

Rewrites the retired equity strategy ID (and its retired short alias) to
``swing_llm_driven`` in:

* ``algo_state.sqlite3``: ``strategy_state`` (key column and ``state_json``),
  ``position_owner``, ``entry_provenance``, and ``coordinator.state_json``;
* ``trade_journal.jsonl``: any string value equal to the old ID.

Dry-run is the default. ``--apply`` first copies both files into a timestamped
backup directory, then writes the database in one transaction and replaces the
journal atomically. It refuses to run while the Algo daemon holds its lock.
A second ``--apply`` finds nothing to rewrite and writes nothing (idempotent).

Usage::

    poetry run python -m strategies.migrations.rename_llm_driven            # dry run
    poetry run python -m strategies.migrations.rename_llm_driven --apply    # daemon stopped
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any

from runtime.paths import DATA_DIR


NEW_ID = "swing_llm_driven"
OLD_IDS = frozenset({"swing_event_driven", "event_driven"})
DEFAULT_STATE_DB = DATA_DIR / "algo_state.sqlite3"
DEFAULT_JOURNAL = DATA_DIR / "trade_journal.jsonl"
DEFAULT_BACKUP_ROOT = DATA_DIR / "backups"
DEFAULT_LOCK_FILE = Path("/run/lock/algo-trader.lock")


class MigrationConflict(RuntimeError):
    """Raised before any write when a rename would collide with existing rows."""


def rewrite_value(value: Any, counts: dict[str, int]) -> Any:
    """Return ``value`` with old IDs replaced, recursing into dicts and lists.

    Matches exact strings and ``<old>:<suffix>`` keys (profile IDs). Substrings
    inside free text are left alone.
    """
    if isinstance(value, str):
        if value in OLD_IDS:
            counts["values"] = counts.get("values", 0) + 1
            return NEW_ID
        prefix, sep, rest = value.partition(":")
        if sep and prefix in OLD_IDS:
            counts["values"] = counts.get("values", 0) + 1
            return f"{NEW_ID}:{rest}"
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            new_key = rewrite_value(key, counts) if isinstance(key, str) else key
            result[new_key] = rewrite_value(item, counts)
        return result
    if isinstance(value, list):
        return [rewrite_value(item, counts) for item in value]
    return value


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone()
    return row is not None


def _json_contains_old_id(text: str) -> bool:
    return any(old in text for old in OLD_IDS)


def plan_state_db(path: Path) -> dict:
    """Read the database (read-only) and describe every row that must change."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        plan: dict[str, Any] = {
            "strategy_state": [], "position_owner": [], "entry_provenance": [],
            "coordinator": [], "conflicts": [],
        }
        if _table_exists(connection, "strategy_state"):
            rows = connection.execute(
                "SELECT strategy, ticker, state_json FROM strategy_state"
            ).fetchall()
            existing = {(strategy, ticker) for strategy, ticker, _ in rows}
            for strategy, ticker, state_json in rows:
                new_strategy = rewrite_value(strategy, {})
                new_json = state_json
                if _json_contains_old_id(state_json):
                    json_counts: dict[str, int] = {}
                    rewritten = rewrite_value(json.loads(state_json), json_counts)
                    if json_counts:
                        new_json = json.dumps(rewritten, sort_keys=True, separators=(",", ":"))
                if new_strategy == strategy and new_json == state_json:
                    continue
                if new_strategy != strategy and (new_strategy, ticker) in existing:
                    plan["conflicts"].append(f"strategy_state already has {new_strategy}/{ticker}")
                    continue
                plan["strategy_state"].append({
                    "old": (strategy, ticker), "new": (new_strategy, ticker),
                    "state_json": new_json,
                })
        for table, key_column in (("position_owner", "ticker"), ("entry_provenance", "order_id")):
            if not _table_exists(connection, table):
                continue
            rows = connection.execute(
                f"SELECT {key_column}, strategy FROM {table} WHERE strategy IN (?, ?)",
                tuple(sorted(OLD_IDS)),
            ).fetchall()
            plan[table] = [(key, strategy, NEW_ID) for key, strategy in rows]
        if _table_exists(connection, "coordinator"):
            for (state_json,) in connection.execute("SELECT state_json FROM coordinator"):
                # Free-text diagnostics (for example a KeyError message) are not IDs.
                if _json_contains_old_id(state_json):
                    counts: dict[str, int] = {}
                    rewrite_value(json.loads(state_json), counts)
                    if counts:
                        plan["coordinator"].append(state_json)
        return plan
    finally:
        connection.close()


def apply_state_db(path: Path, plan: dict) -> dict:
    """Write the planned changes in one IMMEDIATE transaction."""
    if plan["conflicts"]:
        raise MigrationConflict("; ".join(plan["conflicts"]))
    written = {"strategy_state": 0, "position_owner": 0, "entry_provenance": 0, "coordinator": 0}
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in plan["strategy_state"]:
                (old_strategy, ticker), (new_strategy, _) = item["old"], item["new"]
                connection.execute(
                    "UPDATE strategy_state SET strategy = ?, state_json = ? "
                    "WHERE strategy = ? AND ticker = ?",
                    (new_strategy, item["state_json"], old_strategy, ticker),
                )
                written["strategy_state"] += 1
            for ticker, _, new_strategy in plan["position_owner"]:
                connection.execute(
                    "UPDATE position_owner SET strategy = ? WHERE ticker = ?",
                    (new_strategy, ticker),
                )
                written["position_owner"] += 1
            for order_id, _, new_strategy in plan["entry_provenance"]:
                connection.execute(
                    "UPDATE entry_provenance SET strategy = ? WHERE order_id = ?",
                    (new_strategy, order_id),
                )
                written["entry_provenance"] += 1
            for state_json in plan["coordinator"]:
                rewritten = json.dumps(
                    rewrite_value(json.loads(state_json), {}),
                    sort_keys=True, separators=(",", ":"),
                )
                connection.execute(
                    "UPDATE coordinator SET state_json = ? WHERE state_json = ?",
                    (rewritten, state_json),
                )
                written["coordinator"] += 1
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return written


def plan_journal(path: Path) -> dict:
    """Describe journal lines whose JSON contains an old ID (original text kept)."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changes: dict[int, str] = {}
    unparsed = 0
    for index, line in enumerate(lines):
        if not _json_contains_old_id(line):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            unparsed += 1
            continue
        counts: dict[str, int] = {}
        rewritten = rewrite_value(record, counts)
        if counts:
            newline = "\n" if line.endswith("\n") else ""
            changes[index] = json.dumps(rewritten, ensure_ascii=False) + newline
    return {"lines": len(lines), "changes": changes, "unparsed_with_old_id": unparsed}


def apply_journal(path: Path, plan: dict) -> int:
    """Replace the journal atomically; unchanged lines keep their exact bytes."""
    if not plan["changes"]:
        return 0
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, replacement in plan["changes"].items():
        lines[index] = replacement
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return len(plan["changes"])


def backup_files(state_db: Path, journal: Path, backup_root: Path) -> Path:
    """Copy both files into ``<backup_root>/llm_driven_rename-<UTC stamp>/``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = backup_root / f"llm_driven_rename-{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    if state_db.exists():
        # The SQLite backup API copies a consistent snapshot even if a writer is active.
        source = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        target = sqlite3.connect(str(directory / state_db.name))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    if journal.exists():
        shutil.copy2(journal, directory / journal.name)
    return directory


def daemon_lock_held(lock_file: Path) -> bool:
    """True when the Algo daemon currently holds its exclusive lock."""
    if not lock_file.exists():
        return False
    descriptor = os.open(lock_file, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def run(
    *, state_db: Path, journal: Path, apply: bool, backup_root: Path,
    lock_file: Path | None = DEFAULT_LOCK_FILE,
) -> dict:
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "state_db": str(state_db), "journal": str(journal),
        "new_id": NEW_ID, "old_ids": sorted(OLD_IDS),
    }
    state_plan = plan_state_db(state_db) if state_db.exists() else None
    journal_plan = plan_journal(journal) if journal.exists() else None
    report["counts"] = {
        "strategy_state_rows": len(state_plan["strategy_state"]) if state_plan else 0,
        "position_owner_rows": len(state_plan["position_owner"]) if state_plan else 0,
        "entry_provenance_rows": len(state_plan["entry_provenance"]) if state_plan else 0,
        "coordinator_rows": len(state_plan["coordinator"]) if state_plan else 0,
        "journal_lines": len(journal_plan["changes"]) if journal_plan else 0,
    }
    report["journal_unparsed_with_old_id"] = journal_plan["unparsed_with_old_id"] if journal_plan else 0
    report["conflicts"] = state_plan["conflicts"] if state_plan else []
    if not apply:
        return report
    if report["conflicts"]:
        raise MigrationConflict("; ".join(report["conflicts"]))
    if lock_file is not None and daemon_lock_held(lock_file):
        raise RuntimeError(f"the Algo daemon holds {lock_file}; stop it before --apply")
    if not any(report["counts"].values()):
        report["mode"] = "apply_nothing_to_do"
        return report
    report["backup_dir"] = str(backup_files(state_db, journal, backup_root))
    if state_plan is not None:
        report["state_written"] = apply_state_db(state_db, state_plan)
    if journal_plan is not None:
        report["journal_written"] = apply_journal(journal, journal_plan)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--no-lock-check", action="store_true",
                        help="skip the daemon lock check (tests and copies only)")
    parser.add_argument("--apply", action="store_true", help="write changes (default is dry run)")
    args = parser.parse_args(argv)
    try:
        report = run(
            state_db=args.state_db, journal=args.journal, apply=args.apply,
            backup_root=args.backup_root,
            lock_file=None if args.no_lock_check else args.lock_file,
        )
    except (MigrationConflict, RuntimeError, sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"status": "refused", "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["NEW_ID", "OLD_IDS", "rewrite_value", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
