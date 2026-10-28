"""Per-strategy champion and rotating holdout state, stored in the options history database.

A champion is the parameter set exported for paper trading. Each holdout decision
uses sessions that no earlier decision used. A new candidate replaces the champion
only when it passes the approval gate and beats the incumbent on that holdout.
The champion is pinned into the next search pool so each run starts from it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from options.research.history_store import DEFAULT_DATABASE
from options.research.search import Candidate, _candidate

DEFAULT_MARGIN = 0.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS options_champion (
    strategy TEXT NOT NULL,
    underlying TEXT NOT NULL,
    candidate_id TEXT,
    parameters_json TEXT,
    allowed_regimes_json TEXT,
    holdout_risk_adjusted_score REAL,
    promoted_run_id TEXT,
    promoted_at TEXT,
    holdout_end TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (strategy, underlying)
);
CREATE TABLE IF NOT EXISTS options_champion_decision (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    underlying TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    holdout_start TEXT,
    holdout_end TEXT,
    candidate_id TEXT NOT NULL,
    incumbent_candidate_id TEXT,
    candidate_holdout_score REAL,
    incumbent_holdout_score REAL,
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
    if row is None:
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
    *, approved: bool, candidate_holdout_score: float | None,
    incumbent_holdout_score: float | None, margin: float = DEFAULT_MARGIN,
) -> tuple[bool, str]:
    """Promote only when the approval gate passes and the holdout beats the incumbent."""
    if not approved:
        return False, "candidate did not pass the approval gate"
    if candidate_holdout_score is None:
        return False, "candidate holdout score unavailable"
    if incumbent_holdout_score is None:
        return True, "first approved candidate"
    if candidate_holdout_score > incumbent_holdout_score + margin:
        return True, "holdout score beats incumbent by more than the margin"
    return False, "holdout score does not beat incumbent by the required margin"


def record_decision(
    *, strategy: str, underlying: str, run_id: str, decided_at: str,
    holdout_start: str | None, holdout_end: str, candidate: Candidate,
    candidate_holdout_score: float | None, incumbent_candidate_id: str | None,
    incumbent_holdout_score: float | None, promoted: bool, reason: str,
    path: Path | None = None,
) -> None:
    """Persist one holdout decision and, when promoted, the new champion."""
    with closing(_connect(Path(path or DEFAULT_DATABASE))) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO options_champion_decision (
                    run_id, strategy, underlying, decided_at, holdout_start, holdout_end,
                    candidate_id, incumbent_candidate_id, candidate_holdout_score,
                    incumbent_holdout_score, promoted, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, strategy, underlying, decided_at, holdout_start, holdout_end,
                 candidate.candidate_id, incumbent_candidate_id, candidate_holdout_score,
                 incumbent_holdout_score, int(promoted), reason),
            )
            if promoted:
                connection.execute(
                    """
                    INSERT INTO options_champion (
                        strategy, underlying, candidate_id, parameters_json, allowed_regimes_json,
                        holdout_risk_adjusted_score, promoted_run_id, promoted_at, holdout_end, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (strategy, underlying) DO UPDATE SET
                        candidate_id = excluded.candidate_id,
                        parameters_json = excluded.parameters_json,
                        allowed_regimes_json = excluded.allowed_regimes_json,
                        holdout_risk_adjusted_score = excluded.holdout_risk_adjusted_score,
                        promoted_run_id = excluded.promoted_run_id,
                        promoted_at = excluded.promoted_at,
                        holdout_end = excluded.holdout_end,
                        updated_at = excluded.updated_at
                    """,
                    (strategy, underlying, candidate.candidate_id,
                     json.dumps(candidate.parameters, sort_keys=True),
                     json.dumps(list(candidate.allowed_regimes)),
                     candidate_holdout_score, run_id, decided_at, holdout_end, decided_at),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO options_champion (strategy, underlying, holdout_end, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (strategy, underlying) DO UPDATE SET
                        holdout_end = excluded.holdout_end,
                        updated_at = excluded.updated_at
                    """,
                    (strategy, underlying, holdout_end, decided_at),
                )
