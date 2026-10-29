"""Versioned store for promoted Algo profiles.

Every profile that has been live for a (strategy, regime) pair is a row in
``profile_versions``; every promotion, revert, and backfill is a row in
``profile_events``. Both tables live in the research SQLite database
(``data/research/algo-auto/research.sqlite3``). ``promoted-profile.json`` stays
as the live projection that the scanner reads; the database is the record.

Write protocol. Callers hold the forward-validation flock (``_flock``):

1. Stage: insert the new version as ``pending`` in its own transaction.
2. Project: atomically replace ``promoted-profile.json``.
3. Activate: in one transaction, move the previous active version to
   ``superseded`` (promotion) or ``reverted`` (revert), mark the pending version
   ``active``, and insert the event.

``reconcile`` runs before each forward cycle, so it also recovers from a crash
at any point. A pending row whose candidate is already live in the JSON is
activated (roll forward). Any other pending row was never projected, so it is
marked ``abandoned`` and its candidate may be staged again later.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .artifacts.files import _json_safe
from portfolio.profile_store import FORWARD_VALIDATION_PATH, PROMOTED_PROFILE_PATH, RESEARCH_ROOT, write_json_atomic
from strategies.registry import validate_parameter_profile


BASE_REGIME = "*"
PROFILE_SCHEMA = "swarm_algo_promoted_profiles_v1"
PENDING, ACTIVE, SUPERSEDED, REVERTED, ABANDONED = "pending", "active", "superseded", "reverted", "abandoned"
PROMOTION_SOURCE, REVERT_SOURCE, BACKFILL_SOURCE = "promotion", "revert", "backfill"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_versions (
    version_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    parameters_json TEXT,
    fitness_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'superseded', 'reverted', 'abandoned')),
    source TEXT NOT NULL CHECK (source IN ('promotion', 'revert', 'backfill', 'manual')),
    candidate_id TEXT,
    parent_version_id TEXT REFERENCES profile_versions(version_id),
    restores_version_id TEXT REFERENCES profile_versions(version_id),
    release_id TEXT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    deactivated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS profile_versions_one_active
    ON profile_versions(strategy, regime) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS profile_versions_lookup_idx
    ON profile_versions(strategy, regime, status);
CREATE TABLE IF NOT EXISTS profile_events (
    event_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('promote', 'revert', 'backfill')),
    from_version_id TEXT REFERENCES profile_versions(version_id),
    to_version_id TEXT REFERENCES profile_versions(version_id),
    reason TEXT,
    actor TEXT,
    created_at TEXT NOT NULL,
    release_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS profile_events_lookup_idx ON profile_events(strategy, regime, created_at);
CREATE TABLE IF NOT EXISTS trial_evaluation (
    evaluation_key TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('discovery', 'forward')),
    candidate_id TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trial_evaluation_strategy_idx ON trial_evaluation(strategy);
"""


class ProfileError(ValueError):
    """A profile operation was refused; nothing was written."""


def canonical_json(value: object) -> str:
    return json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"),
        default=str, allow_nan=False,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: object) -> str:
    return hashlib.sha256(canonical_json(list(parts)).encode()).hexdigest()[:32]


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read {path.name}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


@contextmanager
def _flock(path: Path):
    """Same lock file as forward_validation._locked, so both writers exclude each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def live_candidate(promoted: dict, strategy: str, regime: str) -> str | None:
    """Candidate id that promoted-profile.json currently projects for a pair."""
    if regime == BASE_REGIME:
        return _dict(promoted.get("profile_ids")).get(strategy)
    return _dict(_dict(promoted.get("regime_profile_ids")).get(strategy)).get(regime)


def project_live(
    promoted: dict, strategy: str, regime: str, parameters: dict,
    candidate: str, release_id: str,
) -> None:
    """Write one pair into the live JSON projection, in memory."""
    if regime == BASE_REGIME:
        promoted.setdefault("parameters", {})[strategy] = parameters
        promoted.setdefault("profile_ids", {})[strategy] = candidate
    else:
        promoted.setdefault("regime_profiles", {}).setdefault(strategy, {})[regime] = parameters
        promoted.setdefault("regime_profile_ids", {}).setdefault(strategy, {})[regime] = candidate
    promoted["schema"] = PROFILE_SCHEMA
    promoted["updated_at"] = _now()
    promoted["release_id"] = release_id


def _research_connection(database: str | Path) -> sqlite3.Connection:
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.executescript(_SCHEMA)
    return connection


def record_trial_evaluations(database: str | Path, rows: list[tuple[str, str, str, str | None]]) -> int:
    """Record evaluated candidates as ``(evaluation_key, strategy, kind, candidate_id)``.

    Rows are insert-only and keyed, so recording the same evaluation again is a
    no-op. Returns the number of newly recorded evaluations.
    """
    if not rows:
        return 0
    stamp = _now()
    with closing(_research_connection(database)) as connection:
        with connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO trial_evaluation "
                "(evaluation_key, strategy, kind, candidate_id, recorded_at) VALUES (?, ?, ?, ?, ?)",
                [(key, strategy, kind, candidate, stamp) for key, strategy, kind, candidate in rows],
            )
            return connection.total_changes - before


ROLLING_TRIAL_WINDOW_DAYS = 28  # about 20 trading sessions


def cumulative_trial_count(
    database: str | Path, strategy: str, *, window_days: int | None = ROLLING_TRIAL_WINDOW_DAYS,
) -> int:
    """Evaluated candidates recorded for a strategy in the trailing window, across all regimes.

    ``window_days=None`` counts everything ever recorded. A candidate re-discovered
    inside the window is one evaluation (the key is unique).

    Counts discovery evaluations (``discovery:...`` keys) and forward trials
    (``forward:...`` keys) from ``trial_evaluation``, plus forward trials that
    predate that table and exist only in the ``forward_trial`` ledger table. The
    rows are never deleted, so trimming the forward state file cannot shrink it.
    """
    path = Path(database)
    keys: set[str] = set()
    if path.exists():
        with closing(sqlite3.connect(path, timeout=30)) as connection:
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )
            }
            if "trial_evaluation" in tables:
                if window_days is None:
                    query, params = "SELECT evaluation_key FROM trial_evaluation WHERE strategy=?", (strategy,)
                else:
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
                    query = ("SELECT evaluation_key FROM trial_evaluation "
                             "WHERE strategy=? AND recorded_at>=?")
                    params = (strategy, cutoff)
                keys.update(str(row[0]) for row in connection.execute(query, params))
            if "forward_trial" in tables and window_days is None:
                keys.update(
                    f"forward:{row[0]}" for row in connection.execute(
                        "SELECT trial_id FROM forward_trial WHERE strategy=?", (strategy,),
                    )
                )
    return len(keys)


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    data["parameters"] = json.loads(data.pop("parameters_json")) if data.get("parameters_json") else None
    data["fitness"] = json.loads(data.pop("fitness_json") or "{}")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


class ProfileStore:
    """Reads and writes profile versions in the research database."""

    def __init__(
        self, database: str | Path, *, promoted_path: Path = PROMOTED_PROFILE_PATH,
        lock_path: Path = FORWARD_VALIDATION_PATH,
    ):
        self.database = Path(database)
        self.promoted_path = Path(promoted_path)
        self.lock_path = Path(lock_path)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self, *, dry_run: bool = False):
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("ROLLBACK" if dry_run else "COMMIT")

    # Queries -----------------------------------------------------------------

    def _resolve(self, connection, version_id: str) -> dict:
        rows = connection.execute(
            "SELECT * FROM profile_versions WHERE version_id=? OR "
            "substr(version_id, 1, length(?)) = ? ORDER BY version_id",
            (version_id, version_id, version_id),
        ).fetchall()
        exact = [row for row in rows if row["version_id"] == version_id]
        if len(exact) == 1:
            return _row(exact[0])
        if not rows:
            raise ProfileError(f"unknown profile version {version_id}")
        if len(rows) > 1:
            raise ProfileError(f"profile version prefix {version_id} is ambiguous")
        return _row(rows[0])

    def get_version(self, version_id: str) -> dict:
        with closing(self._connect()) as connection:
            return self._resolve(connection, version_id)

    def active_versions(self, strategy: str | None = None, regime: str | None = None) -> list[dict]:
        clauses, values = ["status IN (?, ?)"], [ACTIVE, PENDING]
        if strategy:
            clauses.append("strategy=?")
            values.append(strategy)
        if regime:
            clauses.append("regime=?")
            values.append(regime)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM profile_versions WHERE {' AND '.join(clauses)} "
                "ORDER BY strategy, regime, created_at, rowid",
                values,
            ).fetchall()
        return [_row(row) for row in rows]

    def history(self, strategy: str, regime: str) -> dict:
        with closing(self._connect()) as connection:
            versions = connection.execute(
                "SELECT * FROM profile_versions WHERE strategy=? AND regime=? ORDER BY created_at, rowid",
                (strategy, regime),
            ).fetchall()
            events = connection.execute(
                "SELECT * FROM profile_events WHERE strategy=? AND regime=? ORDER BY created_at, rowid",
                (strategy, regime),
            ).fetchall()
        return {
            "versions": [_row(row) for row in versions],
            "events": [{**dict(row), "details": json.loads(row["details_json"] or "{}")} for row in events],
        }

    # Writes ------------------------------------------------------------------

    def stage_promotion(
        self, *, strategy: str, regime: str, candidate_id: str, parameters: dict,
        release_id: str, fitness: dict, metadata: dict, replaced_parameters: dict | None,
    ) -> str | None:
        """Insert a pending promotion. Returns None if the candidate was already promoted.

        A candidate is promoted at most once per pair. Re-activating an older
        version is an explicit revert, never an automatic promotion.
        """
        stamp = _now()
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT version_id FROM profile_versions WHERE strategy=? AND regime=? "
                "AND candidate_id=? AND status!=?",
                (strategy, regime, candidate_id, ABANDONED),
            ).fetchone()
            if prior is not None:
                return None
            active = _active(connection, strategy, regime)
            version_id = uuid4().hex
            _insert_version(
                connection, version_id=version_id, strategy=strategy, regime=regime,
                parameters=parameters, fitness=fitness,
                metadata={
                    **metadata, "reason": "forward promotion", "actor": "forward_validation",
                    "replaced_parameters": (active or {}).get("parameters", replaced_parameters),
                    "replaced_candidate_id": (active or {}).get("candidate_id"),
                },
                status=PENDING, source=PROMOTION_SOURCE, candidate_id=candidate_id,
                parent=(active or {}).get("version_id"), release_id=release_id, created_at=stamp,
            )
        return version_id

    def activate(self, version_ids: list[str]) -> None:
        """Make staged versions live in the database, after their JSON projection is written."""
        stamp = _now()
        with self._transaction() as connection:
            for version_id in version_ids:
                pending = _row(connection.execute(
                    "SELECT * FROM profile_versions WHERE version_id=?", (version_id,),
                ).fetchone())
                if pending is None or pending["status"] != PENDING:
                    raise RuntimeError(f"profile version {version_id} is not pending")
                _activate(connection, pending, stamp=stamp)

    def reconcile(self, promoted: dict) -> dict:
        """Resolve pending rows against the live JSON. Call while holding the flock."""
        stamp = _now()
        completed, abandoned = [], []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM profile_versions WHERE status=? ORDER BY created_at, rowid",
                (PENDING,),
            ).fetchall()
            for raw in rows:
                pending = _row(raw)
                if live_candidate(promoted, pending["strategy"], pending["regime"]) == pending["candidate_id"]:
                    _activate(connection, pending, stamp=stamp)
                    completed.append(pending["version_id"])
                else:
                    connection.execute(
                        "UPDATE profile_versions SET status=?, deactivated_at=? WHERE version_id=?",
                        (ABANDONED, stamp, pending["version_id"]),
                    )
                    abandoned.append(pending["version_id"])
        return {"completed": completed, "abandoned": abandoned}

    def plan_revert(self, *, strategy: str, regime: str, version_id: str) -> dict:
        """Validate a revert target and describe the change. Writes nothing."""
        with closing(self._connect()) as connection:
            target = self._resolve(connection, version_id)
            current = _active(connection, strategy, regime)
        if target["strategy"] != strategy or target["regime"] != regime:
            raise ProfileError(f"version {target['version_id']} belongs to {target['strategy']}:{target['regime']}")
        if target["status"] in (PENDING, ABANDONED):
            raise ProfileError(f"version {target['version_id']} is {target['status']} and cannot be restored")
        if target["status"] == ACTIVE:
            raise ProfileError(f"version {target['version_id']} is already active")
        if target["parameters"] is None:
            raise ProfileError(f"version {target['version_id']} has no recorded parameters")
        try:
            validated = validate_parameter_profile({strategy: target["parameters"]})
            normalized = next(iter(validated.values()))
        except (ValueError, KeyError) as exc:
            raise ProfileError(
                f"version {target['version_id']} fails parameter validation: {exc}",
            ) from exc
        current_parameters = (current or {}).get("parameters") or {}
        keys = sorted(set(current_parameters) | set(normalized))
        return {
            "strategy": strategy,
            "regime": regime,
            "target_version_id": target["version_id"],
            "target_status": target["status"],
            "target_candidate_id": target["candidate_id"] or target["version_id"],
            "target_parameters": normalized,
            "current_version_id": (current or {}).get("version_id"),
            "current_parameters": current_parameters,
            "changed_keys": [
                key for key in keys if current_parameters.get(key) != normalized.get(key)
            ],
        }

    def revert(
        self, *, strategy: str, regime: str, version_id: str,
        reason: str = "manual revert", actor: str = "cli", dry_run: bool = False,
    ) -> dict:
        """Restore an earlier version as the live profile.

        Takes the same flock as the forward cycle. The target row is kept as
        history; the revert inserts a new ``source=revert`` version that copies
        its parameters and records the target in ``restores_version_id``.
        """
        with _flock(self.lock_path):
            promoted = _read_json(self.promoted_path)
            if not dry_run:
                self.reconcile(promoted)
            plan = self.plan_revert(strategy=strategy, regime=regime, version_id=version_id)
            if dry_run:
                return {**plan, "dry_run": True}
            release_id = f"revert-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            stamp = _now()
            with self._transaction() as connection:
                active = _active(connection, strategy, regime)
                new_id = uuid4().hex
                _insert_version(
                    connection, version_id=new_id, strategy=strategy, regime=regime,
                    parameters=plan["target_parameters"], fitness={},
                    metadata={
                        "reason": reason, "actor": actor,
                        "replaced_parameters": (active or {}).get("parameters"),
                        "replaced_candidate_id": (active or {}).get("candidate_id"),
                    },
                    status=PENDING, source=REVERT_SOURCE,
                    candidate_id=plan["target_candidate_id"],
                    parent=(active or {}).get("version_id"),
                    restores=plan["target_version_id"],
                    release_id=release_id, created_at=stamp,
                )
            project_live(
                promoted, strategy, regime, plan["target_parameters"],
                plan["target_candidate_id"], release_id,
            )
            write_json_atomic(self.promoted_path, promoted)
            self.activate([new_id])
        return {**plan, "dry_run": False, "new_version_id": new_id, "release_id": release_id}

    def backfill(self, *, dry_run: bool = False) -> dict:
        """Import the live JSON and its history records. Idempotent; never writes the JSON."""
        promoted = _read_json(self.promoted_path)
        stamp = _now()
        counts = {
            "dry_run": dry_run, "live_entries": 0, "history_records": 0,
            "history_attached_to_live": 0, "versions_inserted": 0,
            "events_inserted": 0, "already_present": 0, "conflicts": [],
        }
        release = promoted.get("release_id")
        live_entries: list[dict] = []
        for strategy, parameters in sorted(_dict(promoted.get("parameters")).items()):
            if isinstance(parameters, dict):
                live_entries.append({
                    "strategy": strategy, "regime": BASE_REGIME, "parameters": parameters,
                    "candidate": _dict(promoted.get("profile_ids")).get(strategy),
                })
        for strategy, by_regime in sorted(_dict(promoted.get("regime_profiles")).items()):
            for regime, parameters in sorted(_dict(by_regime).items()):
                if isinstance(parameters, dict):
                    live_entries.append({
                        "strategy": strategy, "regime": regime, "parameters": parameters,
                        "candidate": _dict(_dict(promoted.get("regime_profile_ids")).get(strategy)).get(regime),
                    })
        counts["live_entries"] = len(live_entries)

        history = promoted.get("history") if isinstance(promoted.get("history"), list) else []
        attached: dict[int, list[dict]] = {}
        history_rows: list[tuple[str, str, dict]] = []
        for record in history:
            if not isinstance(record, dict) or not isinstance(record.get("strategy"), str):
                continue
            counts["history_records"] += 1
            strategy = record["strategy"]
            regime = record.get("regime") if isinstance(record.get("regime"), str) and record.get("regime") else BASE_REGIME
            candidate = record.get("candidate_id")
            match = next((
                index for index, entry in enumerate(live_entries)
                if candidate and entry["strategy"] == strategy and entry["regime"] == regime
                and entry["candidate"] == candidate
            ), None)
            if match is not None:
                # Same state as a live entry: keep it on that version instead of duplicating it.
                attached.setdefault(match, []).append(record)
                counts["history_attached_to_live"] += 1
                continue
            history_rows.append((strategy, regime, record))

        with self._transaction(dry_run=dry_run) as connection:
            chain: dict[tuple[str, str], str] = {}
            for strategy, regime, record in history_rows:
                version_id = "bf-" + _digest("history", strategy, regime, record)
                key = (strategy, regime)
                if _exists(connection, version_id):
                    counts["already_present"] += 1
                    chain[key] = version_id
                    continue
                parent = chain.get(key)
                created = str(record.get("recorded_at") or record.get("promoted_at") or stamp)
                _insert_version(
                    connection, version_id=version_id, strategy=strategy, regime=regime,
                    parameters=record.get("parameters") if isinstance(record.get("parameters"), dict) else None,
                    fitness=_dict(record.get("forward_metrics")),
                    metadata={"history_record": record},
                    status=SUPERSEDED, source=BACKFILL_SOURCE,
                    candidate_id=record.get("candidate_id"), parent=parent,
                    release_id=record.get("release_id"), created_at=created,
                )
                _insert_event(
                    connection, strategy=strategy, regime=regime, action="backfill",
                    from_id=parent, to_id=version_id, reason="backfill from promoted-profile.json history",
                    actor="backfill", created_at=created, release_id=record.get("release_id"),
                    details={"source": "history"},
                )
                counts["versions_inserted"] += 1
                counts["events_inserted"] += 1
                chain[key] = version_id

            for index, entry in enumerate(live_entries):
                strategy, regime, parameters = entry["strategy"], entry["regime"], entry["parameters"]
                version_id = "bf-" + _digest("live", strategy, regime, entry["candidate"], release, parameters)
                if _exists(connection, version_id) or (
                    entry["candidate"] and _candidate_present(connection, strategy, regime, entry["candidate"])
                ):
                    counts["already_present"] += 1
                    continue
                if _active(connection, strategy, regime) is not None:
                    counts["conflicts"].append(f"{strategy}:{regime} has an active version not in the live JSON")
                    continue
                key = (strategy, regime)
                created = str(promoted.get("updated_at") or stamp)
                _insert_version(
                    connection, version_id=version_id, strategy=strategy, regime=regime,
                    parameters=parameters, fitness={},
                    metadata={"history_records": attached.get(index, [])},
                    status=ACTIVE, source=BACKFILL_SOURCE, candidate_id=entry["candidate"],
                    parent=chain.get(key), release_id=release, created_at=created,
                    activated_at=created,
                )
                _insert_event(
                    connection, strategy=strategy, regime=regime, action="backfill",
                    from_id=chain.get(key), to_id=version_id,
                    reason="backfill from promoted-profile.json", actor="backfill",
                    created_at=created, release_id=release, details={"source": "live"},
                )
                counts["versions_inserted"] += 1
                counts["events_inserted"] += 1
        return counts


def _exists(connection, version_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM profile_versions WHERE version_id=?", (version_id,),
    ).fetchone() is not None


def _candidate_present(connection, strategy: str, regime: str, candidate: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM profile_versions WHERE strategy=? AND regime=? AND candidate_id=? AND status!=?",
        (strategy, regime, candidate, ABANDONED),
    ).fetchone() is not None


def _active(connection, strategy: str, regime: str) -> dict | None:
    return _row(connection.execute(
        "SELECT * FROM profile_versions WHERE strategy=? AND regime=? AND status=?",
        (strategy, regime, ACTIVE),
    ).fetchone())


def _insert_version(
    connection, *, version_id: str, strategy: str, regime: str, parameters: dict | None,
    fitness: dict, metadata: dict, status: str, source: str, candidate_id: str | None,
    parent: str | None, release_id: str | None, created_at: str,
    restores: str | None = None, activated_at: str | None = None,
    deactivated_at: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO profile_versions
           (version_id, strategy, regime, parameters_json, fitness_json, metadata_json,
            status, source, candidate_id, parent_version_id, restores_version_id,
            release_id, created_at, activated_at, deactivated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            version_id, strategy, regime,
            canonical_json(parameters) if parameters is not None else None,
            canonical_json(fitness), canonical_json(metadata), status, source, candidate_id,
            parent, restores, release_id, created_at, activated_at, deactivated_at,
        ),
    )


def _insert_event(
    connection, *, strategy: str, regime: str, action: str, from_id: str | None,
    to_id: str | None, reason: str | None, actor: str | None, created_at: str,
    release_id: str | None, details: dict,
) -> None:
    connection.execute(
        """INSERT INTO profile_events
           (event_id, strategy, regime, action, from_version_id, to_version_id, reason,
            actor, created_at, release_id, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid4().hex, strategy, regime, action, from_id, to_id, reason, actor,
            created_at, release_id, canonical_json(details),
        ),
    )


def _activate(connection, pending: dict, *, stamp: str) -> None:
    """Swap the active version for a pending one inside an open transaction."""
    strategy, regime = pending["strategy"], pending["regime"]
    previous = _active(connection, strategy, regime)
    if previous is not None:
        # Retire the old row first: the partial unique index allows one active row at a time.
        connection.execute(
            "UPDATE profile_versions SET status=?, deactivated_at=? WHERE version_id=?",
            (REVERTED if pending["source"] == REVERT_SOURCE else SUPERSEDED, stamp, previous["version_id"]),
        )
    connection.execute(
        "UPDATE profile_versions SET status=?, activated_at=? WHERE version_id=?",
        (ACTIVE, stamp, pending["version_id"]),
    )
    metadata = pending["metadata"]
    replaced = previous["parameters"] if previous else metadata.get("replaced_parameters")
    replaced_candidate = previous["candidate_id"] if previous else metadata.get("replaced_candidate_id")
    _insert_event(
        connection, strategy=strategy, regime=regime,
        action="revert" if pending["source"] == REVERT_SOURCE else "promote",
        from_id=previous["version_id"] if previous else None, to_id=pending["version_id"],
        reason=metadata.get("reason"), actor=metadata.get("actor"), created_at=stamp,
        release_id=pending["release_id"],
        details={
            "replaced_parameters": replaced,
            "replaced_candidate_id": replaced_candidate,
            "restores_version_id": pending["restores_version_id"],
        },
    )


# Command line -----------------------------------------------------------------

def _table(rows: list[list[object]], headers: list[str]) -> str:
    cells = [headers] + [["" if value is None else str(value) for value in row] for row in rows]
    widths = [max(len(row[index]) for row in cells) for index in range(len(headers))]
    return "\n".join("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in cells)


def _short(value: object, size: int = 12) -> str:
    return str(value)[:size] if value else "-"


def _compare_text(store: ProfileStore, left_id: str, right_id: str) -> str:
    left, right = store.get_version(left_id), store.get_version(right_id)
    lines = []
    for label, row in (("A", left), ("B", right)):
        lines.append(
            f"{label} {row['version_id']}  {row['strategy']}:{row['regime']}  "
            f"status={row['status']} source={row['source']} release={row['release_id']}",
        )
    left_params, right_params = left["parameters"] or {}, right["parameters"] or {}
    lines.append("parameters (= same, ! differs):")
    for key in sorted(set(left_params) | set(right_params)):
        mark = "=" if left_params.get(key) == right_params.get(key) else "!"
        lines.append(f"  {mark} {key}: {json.dumps(left_params.get(key), default=str)} | "
                     f"{json.dumps(right_params.get(key), default=str)}")
    lines.append("fitness (A | B):")
    for key in sorted(set(left["fitness"]) | set(right["fitness"])):
        lines.append(f"    {key}: {json.dumps(left['fitness'].get(key), default=str)} | "
                     f"{json.dumps(right['fitness'].get(key), default=str)}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m equity_research.profile_versions",
        description="Inspect, compare, backfill, and revert versioned Algo profiles.",
    )
    parser.add_argument("--database", default=str(RESEARCH_ROOT / "research.sqlite3"))
    parser.add_argument("--promoted", default=str(PROMOTED_PROFILE_PATH))
    parser.add_argument("--lock", default=str(FORWARD_VALIDATION_PATH))
    commands = parser.add_subparsers(dest="command", required=True)

    backfill = commands.add_parser("backfill", help="import promoted-profile.json and its history")
    backfill.add_argument("--dry-run", action="store_true")

    listing = commands.add_parser("list", help="active and pending versions")
    listing.add_argument("--strategy")
    listing.add_argument("--regime")

    history = commands.add_parser("history", help="versions and events in order")
    history.add_argument("--strategy", required=True)
    history.add_argument("--regime", required=True)

    compare = commands.add_parser("compare", help="parameter and fitness diff of two versions")
    compare.add_argument("--a", required=True)
    compare.add_argument("--b", required=True)

    revert = commands.add_parser("revert", help="restore an earlier version as the live profile")
    revert.add_argument("--strategy", required=True)
    revert.add_argument("--regime", required=True)
    revert.add_argument("--to", required=True, help="version id or unique prefix")
    revert.add_argument("--dry-run", action="store_true")
    revert.add_argument("--reason", default="manual revert")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ProfileStore(args.database, promoted_path=Path(args.promoted), lock_path=Path(args.lock))
    try:
        if args.command == "backfill":
            counts = store.backfill(dry_run=args.dry_run)
            for key, value in counts.items():
                print(f"{key}: {value}")
        elif args.command == "list":
            rows = store.active_versions(args.strategy, args.regime)
            print(_table(
                [[row["strategy"], row["regime"], row["status"], row["source"], _short(row["version_id"]),
                  _short(row["candidate_id"]), row["release_id"], row["activated_at"]] for row in rows],
                ["strategy", "regime", "status", "source", "version", "candidate", "release", "activated_at"],
            ))
        elif args.command == "history":
            data = store.history(args.strategy, args.regime)
            print("versions:")
            print(_table(
                [[row["created_at"], row["status"], row["source"], _short(row["version_id"]),
                  _short(row["parent_version_id"]), _short(row["restores_version_id"]),
                  row["release_id"]] for row in data["versions"]],
                ["created_at", "status", "source", "version", "parent", "restores", "release"],
            ))
            print("events:")
            print(_table(
                [[row["created_at"], row["action"], _short(row["from_version_id"]),
                  _short(row["to_version_id"]), row["reason"], row["release_id"]] for row in data["events"]],
                ["created_at", "action", "from", "to", "reason", "release"],
            ))
        elif args.command == "compare":
            print(_compare_text(store, args.a, args.b))
        elif args.command == "revert":
            result = store.revert(
                strategy=args.strategy, regime=args.regime, version_id=args.to,
                reason=args.reason, actor=os.environ.get("USER") or "cli", dry_run=args.dry_run,
            )
            print(f"revert {args.strategy}:{args.regime} to {result['target_version_id']} "
                  f"(target status {result['target_status']})")
            print(f"current version: {result['current_version_id']}")
            print(f"changed parameter keys: {', '.join(result['changed_keys']) or 'none'}")
            if args.dry_run:
                print("dry run: nothing written")
            else:
                print(f"new active version: {result['new_version_id']} release={result['release_id']}")
    except ProfileError as exc:
        print(f"refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
