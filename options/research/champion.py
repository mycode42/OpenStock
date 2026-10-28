"""Per-strategy champion state, stored in the options history database.

A champion is the parameter set exported for paper trading. A candidate replaces it
only when it passes the approval gate and ranks above the incumbent on the same
out-of-sample folds (runtime/fold_ranking.py, the same rule equity uses). The
champion is pinned into the next search pool so each run starts from it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from options.research.history_store import DEFAULT_DATABASE
from options.research.search import Candidate, _candidate
from runtime.fold_ranking import beats

_SCHEMA = """
CREATE TABLE IF NOT EXISTS options_champion (
    strategy TEXT NOT NULL,
    underlying TEXT NOT NULL,
    candidate_id TEXT,
    parameters_json TEXT,
    allowed_regimes_json TEXT,
    promoted_run_id TEXT,
    promoted_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (strategy, underlying)
);
CREATE TABLE IF NOT EXISTS options_champion_decision (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    underlying TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    incumbent_candidate_id TEXT,
    candidate_fold_summary_json TEXT NOT NULL,
    incumbent_fold_summary_json TEXT,
    promoted INTEGER NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS options_champion_decision_key_idx
    ON options_champion_decision(strategy, underlying, decided_at);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def load_state(strategy: str, underlying: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return the champion row for a strategy and underlying, or None before the first decision."""
    with closing(_connect(Path(path or DEFAULT_DATABASE))) as connection:
        row = connection.execute(
            "SELECT * FROM options_champion WHERE strategy = ? AND underlying = ?",
            (strategy, underlying),
        ).fetchone()
    if row is None or not row["candidate_id"]:
        return None
    state = dict(row)
    state["parameters"] = json.loads(state.pop("parameters_json") or "{}")
    state["allowed_regimes"] = json.loads(state.pop("allowed_regimes_json") or "[]")
    return state


def champion_candidate(record: dict[str, Any]) -> Candidate:
    """Rebuild the champion as a pinned candidate with the same identity."""
    base = _candidate(record["parameters"], record["allowed_regimes"], ())
    return Candidate(base.candidate_id, dict(base.parameters), base.allowed_regimes, (), pinned=True)


def pool_with_champion(pool: list[Candidate], record: dict[str, Any] | None) -> list[Candidate]:
    """Return the search pool with the champion present once, pinned after the baseline."""
    if not record or not record.get("candidate_id"):
        return pool
    pinned = champion_candidate(record)
    kept = [item for item in pool if item.candidate_id != pinned.candidate_id]
    baseline = [item for item in kept if item.is_baseline]
    others = [item for item in kept if not item.is_baseline]
    return [*baseline, pinned, *others]


def decide_promotion(
    *, approved: bool, candidate_summary: dict | None, incumbent_summary: dict | None,
    has_incumbent: bool,
) -> tuple[bool, str]:
    """Promote when the approval gate passes and the candidate ranks above the incumbent on the same folds."""
    if not approved:
        return False, "candidate did not pass the approval gate"
    if not has_incumbent:
        return True, "first approved candidate"
    return beats(candidate_summary, incumbent_summary)


def record_decision(
    *, strategy: str, underlying: str, run_id: str, decided_at: str, candidate: Candidate,
    candidate_summary: dict | None, incumbent_candidate_id: str | None,
    incumbent_summary: dict | None, promoted: bool, reason: str, path: Path | None = None,
) -> None:
    """Persist one decision and, when promoted, the new champion."""
    with closing(_connect(Path(path or DEFAULT_DATABASE))) as connection, connection:
        connection.execute(
            """
            INSERT INTO options_champion_decision (
                run_id, strategy, underlying, decided_at, candidate_id, incumbent_candidate_id,
                candidate_fold_summary_json, incumbent_fold_summary_json, promoted, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, strategy, underlying, decided_at, candidate.candidate_id, incumbent_candidate_id,
             json.dumps(candidate_summary, sort_keys=True), json.dumps(incumbent_summary, sort_keys=True),
             int(promoted), reason),
        )
        if promoted:
            connection.execute(
                """
                INSERT INTO options_champion (
                    strategy, underlying, candidate_id, parameters_json, allowed_regimes_json,
                    promoted_run_id, promoted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (strategy, underlying) DO UPDATE SET
                    candidate_id = excluded.candidate_id,
                    parameters_json = excluded.parameters_json,
                    allowed_regimes_json = excluded.allowed_regimes_json,
                    promoted_run_id = excluded.promoted_run_id,
                    promoted_at = excluded.promoted_at,
                    updated_at = excluded.updated_at
                """,
                (strategy, underlying, candidate.candidate_id,
                 json.dumps(candidate.parameters, sort_keys=True),
                 json.dumps(list(candidate.allowed_regimes)),
                 run_id, decided_at, decided_at),
            )
