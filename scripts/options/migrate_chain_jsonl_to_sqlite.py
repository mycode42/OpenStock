#!/usr/bin/env python3
"""Import legacy chain JSONL files and optionally remove verified copies."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path

from options.research.history_store import DEFAULT_DATABASE, OptionsHistoryStore
from runtime.paths import DATA_DIR


ROOT = DATA_DIR / "options" / "research"
COLLECTOR_LOCK = ROOT / "theta" / "theta_backfill.lock"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files():
    for source_kind, directory, filename in (
        ("theta", ROOT / "theta", "chains.jsonl"),
        ("theta_opening", ROOT / "theta", "opening_chains.jsonl"),
        ("alpaca", ROOT / "alpaca", "chains.jsonl"),
        ("alpaca_opening", ROOT / "alpaca", "opening_chains.jsonl"),
    ):
        for path in sorted(directory.glob(f"*/{filename}")):
            yield source_kind, path.parent.name.upper(), path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete-verified", action="store_true")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    args = parser.parse_args()
    lock_path = COLLECTOR_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("Theta collector is active; retry after it exits") from error
        store = OptionsHistoryStore(args.database)
        results = []
        for kind, symbol, path in _files():
            imported = store.import_jsonl(path, underlying=symbol, source_kind=kind)
            expected_hash = _sha256(path)
            with store._connect() as connection:
                source = connection.execute("""
                    SELECT s.source_id, s.source_sha256, s.session_count, s.status,
                           COUNT(h.snapshot_id)
                    FROM options_history_active_source a
                    JOIN options_history_source s ON s.source_id=a.source_id
                    LEFT JOIN options_history_snapshot h ON h.source_id=s.source_id
                    WHERE a.underlying=? AND a.source_kind=?
                    GROUP BY s.source_id
                """, (symbol, kind)).fetchone()
            verified = bool(
                source and source[1] == expected_hash and source[2] == source[4]
                and source[3] == "complete"
            )
            removed = False
            if verified and args.delete_verified:
                path.unlink()
                removed = True
            results.append({
                "underlying": symbol, "source_kind": kind,
                "status": imported.get("status"), "sessions": source[2] if source else 0,
                "sha256_verified": verified, "removed": removed,
            })
        print(json.dumps({
            "database": str(Path(args.database).resolve()),
            "delete_verified": args.delete_verified,
            "sources": results,
            "verified_count": sum(bool(row["sha256_verified"]) for row in results),
            "removed_count": sum(bool(row["removed"]) for row in results),
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
