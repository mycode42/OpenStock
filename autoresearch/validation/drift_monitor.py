"""Forward-only monitoring of realized out-of-sample strategy returns."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics

from ..artifacts.files import _atomic_json


DRIFT_STATE_SCHEMA = "swarm_algo_research_drift_v1"
REFERENCE_SESSIONS = 63
PH_DELTA = 0.15
PH_THRESHOLD = 8.0
WARNING_STREAK = 3
RECOVERY_SESSIONS = 20


def update_research_drift_monitor(
    verification: dict | None, *, output_dir: str | Path,
    ignore_through_date: str | None = None,
) -> dict:
    """Consume only newly appended stitched OOS returns and update alert state.

    The first call establishes a cursor at the current OOS endpoint, so older
    research results cannot influence this prospective monitor.
    """
    root = Path(output_dir)
    state_path = root / "drift-monitor.json"
    state = _read_state(state_path)
    strategies = (verification or {}).get("strategies") or {}
    updates = {}
    for strategy, status in strategies.items():
        detail_path = status.get("detail_artifact") or status.get("verification_artifact")
        detail = _read_json(Path(detail_path)) if detail_path else None
        curve = (detail or {}).get("stitched_oos_daily_equity") or []
        if not curve:
            continue
        rows = sorted(
            (
                {"date": str(row.get("date", ""))[:10], "equity": _finite(row.get("equity"))}
                for row in curve
            ),
            key=lambda row: row["date"],
        )
        rows = [row for row in rows if row["date"] and row["equity"] is not None and row["equity"] > 0]
        if not rows:
            continue
        item = state["strategies"].get(strategy)
        if item is None:
            state["strategies"][strategy] = {
                "status": "warming_up",
                "last_observed_date": rows[-1]["date"],
                "last_equity": rows[-1]["equity"],
                "reference_returns": [],
                "reference_mean": None,
                "reference_std": None,
                "negative_cusum": 0.0,
                "warning_streak": 0,
                "healthy_streak": 0,
                "new_sessions_consumed": 0,
                "monitor_started_at": datetime.now(timezone.utc).isoformat(),
            }
            updates[strategy] = {
                "status": "warming_up", "new_sessions_consumed": 0,
                "cursor_initialized_at": rows[-1]["date"],
            }
            continue

        previous_equity = item.get("last_equity")
        last_date = str(item.get("last_observed_date") or "")
        consumed = 0
        for row in rows:
            if row["date"] <= last_date:
                continue
            if previous_equity and previous_equity > 0:
                realized_return = row["equity"] / previous_equity - 1.0
                if ignore_through_date and row["date"] <= ignore_through_date:
                    item["ignored_holdout_sessions"] = int(item.get("ignored_holdout_sessions", 0)) + 1
                else:
                    _observe(item, realized_return)
                    consumed += 1
            previous_equity = row["equity"]
            last_date = row["date"]
        item["last_observed_date"] = last_date
        item["last_equity"] = previous_equity
        item["new_sessions_consumed"] = int(item.get("new_sessions_consumed", 0)) + consumed
        updates[strategy] = {
            "status": item.get("status", "warming_up"),
            "new_sessions_consumed": consumed,
            "last_observed_date": last_date,
            "negative_cusum": round(float(item.get("negative_cusum", 0.0)), 4),
            "reference_session_count": len(item.get("reference_returns") or []),
        }

    state["schema"] = DRIFT_STATE_SCHEMA
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["detector"] = {
        "method": "one_sided_page_hinkley_on_standardized_daily_oos_returns",
        "reference_sessions": REFERENCE_SESSIONS,
        "delta": PH_DELTA,
        "threshold": PH_THRESHOLD,
        "warning_streak": WARNING_STREAK,
        "recovery_sessions": RECOVERY_SESSIONS,
        "risk_sizing_changed": False,
    }
    _atomic_json(state_path, state)
    states = {
        strategy: row.get("status", "warming_up")
        for strategy, row in state["strategies"].items()
    }
    warning = any(value == "warning" for value in states.values())
    return {
        "status": "warning" if warning else (
            "monitoring" if any(value == "monitoring" for value in states.values()) else "warming_up"
        ),
        "research_only": True,
        "promotion_pause": warning,
        "live_sizing_changed": False,
        "strategy_statuses": updates,
        "all_strategy_statuses": states,
        "state_path": str(state_path.resolve()),
        "ignored_through_date": ignore_through_date,
        "action": "pause_profile_promotion_until_drift_clears" if warning else "continue_research_monitoring",
    }


def _observe(item: dict, value: float) -> None:
    if not math.isfinite(value):
        return
    reference = item.setdefault("reference_returns", [])
    if item.get("reference_mean") is None:
        reference.append(value)
        if len(reference) >= REFERENCE_SESSIONS:
            item["reference_mean"] = statistics.mean(reference)
            item["reference_std"] = max(statistics.pstdev(reference), 1e-5)
            item["reference_returns"] = reference[-REFERENCE_SESSIONS:]
            item["status"] = "monitoring"
        return

    mean = float(item["reference_mean"])
    scale = max(float(item["reference_std"] or 0.0), 1e-5)
    standardized = (value - mean) / scale
    cusum = max(0.0, float(item.get("negative_cusum", 0.0)) - standardized - PH_DELTA)
    item["negative_cusum"] = cusum
    if standardized < -1.0:
        item["warning_streak"] = int(item.get("warning_streak", 0)) + 1
        item["healthy_streak"] = 0
    else:
        item["warning_streak"] = 0
        item["healthy_streak"] = int(item.get("healthy_streak", 0)) + 1
    if cusum >= PH_THRESHOLD and int(item.get("warning_streak", 0)) >= WARNING_STREAK:
        item["status"] = "warning"
    elif item.get("status") == "warning" and int(item.get("healthy_streak", 0)) >= RECOVERY_SESSIONS:
        item["status"] = "monitoring"
        item["negative_cusum"] = 0.0


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_state(path: Path) -> dict:
    value = _read_json(path)
    if not value:
        return {"schema": DRIFT_STATE_SCHEMA, "strategies": {}}
    if value.get("schema") != DRIFT_STATE_SCHEMA or not isinstance(value.get("strategies"), dict):
        raise ValueError(f"unsupported or malformed drift monitor state: {path}")
    return value


def _finite(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


__all__ = ["DRIFT_STATE_SCHEMA", "update_research_drift_monitor"]
