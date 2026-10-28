"""Rotating holdout for historically validated candidates.

A candidate that passes exact rolling out-of-sample validation is queued. Its
holdout is the first ``HOLDOUT_SESSIONS`` trading sessions after both the
candidate's staging date and the previous holdout for the same strategy and
regime. The holdout is never reused, so each decision sees data no earlier
decision saw. The candidate is promoted only if, on that holdout, it beats the
live profile on net realized P&L and on marked P&L. Until enough new sessions
exist, the candidate waits and nothing changes.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HOLDOUT_SESSIONS = 42

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_holdout_queue (
    candidate_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    staged_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'promoted', 'rejected')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    decided_at TEXT,
    PRIMARY KEY (candidate_id, strategy, regime)
);
CREATE TABLE IF NOT EXISTS history_holdout_state (
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    holdout_end TEXT NOT NULL,
    PRIMARY KEY (strategy, regime)
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def enqueue(profile_db: Path, *, candidate: dict[str, Any], staged_end: date) -> bool:
    """Queue a candidate once. Returns True when it was newly queued."""
    import json

    with closing(_connect(profile_db)) as connection, connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO history_holdout_queue
                (candidate_id, strategy, regime, parameters_json, staged_end, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (candidate["candidate_id"], candidate["strategy"], candidate["regime"],
             json.dumps(candidate["parameters"], sort_keys=True), staged_end.isoformat()),
        )
        return cursor.rowcount == 1


def pending(profile_db: Path) -> list[dict[str, Any]]:
    import json

    with closing(_connect(profile_db)) as connection:
        rows = connection.execute(
            "SELECT * FROM history_holdout_queue WHERE status = 'pending' ORDER BY staged_end, candidate_id"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["parameters"] = json.loads(item.pop("parameters_json"))
        out.append(item)
    return out


def holdout_end(profile_db: Path, strategy: str, regime: str) -> date | None:
    with closing(_connect(profile_db)) as connection:
        row = connection.execute(
            "SELECT holdout_end FROM history_holdout_state WHERE strategy = ? AND regime = ?",
            (strategy, regime),
        ).fetchone()
    return date.fromisoformat(row["holdout_end"]) if row else None


def _decide(profile_db: Path, item: dict[str, Any], *, status: str, block_end: date, evidence: dict) -> None:
    import json

    stamp = datetime.now(timezone.utc).isoformat()
    with closing(_connect(profile_db)) as connection, connection:
        connection.execute(
            "UPDATE history_holdout_queue SET status = ?, evidence_json = ?, decided_at = ? "
            "WHERE candidate_id = ? AND strategy = ? AND regime = ?",
            (status, json.dumps(evidence, sort_keys=True, default=str), stamp,
             item["candidate_id"], item["strategy"], item["regime"]),
        )
        connection.execute(
            """
            INSERT INTO history_holdout_state (strategy, regime, holdout_end) VALUES (?, ?, ?)
            ON CONFLICT (strategy, regime) DO UPDATE SET holdout_end = excluded.holdout_end
            """,
            (item["strategy"], item["regime"], block_end.isoformat()),
        )


def evaluate(
    profile_db: Path, *, sessions_after: Callable[[date], list[date]], end: date,
    simulate: Callable[[dict[str, Any], date, date], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Decide each pending candidate whose holdout block is complete.

    ``sessions_after(start)`` lists trading sessions from ``start`` through ``end``.
    ``simulate(item, start, end)`` returns ``{"candidate": metrics, "baseline": metrics}``
    with ``net_realized_pnl_after_simulated_costs`` and ``marked_pnl_after_estimated_exit_costs``.
    Returns the decided items, each with its status and evidence.
    """
    decided = []
    for item in pending(profile_db):
        staged_end = date.fromisoformat(item["staged_end"])
        last = holdout_end(profile_db, item["strategy"], item["regime"])
        floor = max(staged_end, last) if last else staged_end
        block = sessions_after(floor + timedelta(days=1))[:HOLDOUT_SESSIONS]
        if len(block) < HOLDOUT_SESSIONS:
            continue
        start, block_end = block[0], block[-1]
        result = simulate(item, start, block_end)
        cand, base = result["candidate"], result["baseline"]
        cand_net = float(cand["net_realized_pnl_after_simulated_costs"])
        base_net = float(base["net_realized_pnl_after_simulated_costs"])
        cand_marked = cand.get("marked_pnl_after_estimated_exit_costs")
        base_marked = base.get("marked_pnl_after_estimated_exit_costs")
        beats = bool(
            cand_net > base_net
            and isinstance(cand_marked, (int, float)) and isinstance(base_marked, (int, float))
            and cand_marked > base_marked
        )
        evidence = {
            "holdout_start": start.isoformat(), "holdout_end": block_end.isoformat(),
            "holdout_sessions": HOLDOUT_SESSIONS,
            "candidate_net_pnl": round(cand_net, 2), "live_net_pnl": round(base_net, 2),
            "candidate_marked_pnl": cand_marked, "live_marked_pnl": base_marked,
            "candidate_metrics": cand, "baseline_metrics": base,
            "beats_live": beats,
        }
        status = "promoted" if beats else "rejected"
        _decide(profile_db, item, status=status, block_end=block_end, evidence=evidence)
        decided.append({**item, "status": status, "evidence": evidence})
    return decided
