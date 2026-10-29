"""Forward-only monitoring of realized strategy returns.

Two inputs feed one detector per strategy:

* ``live_returns``: daily returns realized by the Algo paper account, built
  from broker fills and attributed to the owning strategy (primary signal).
* the stitched out-of-sample equity from the nightly historical verification
  (secondary signal, research only).

A live warning is the only trigger for profile auto-demotion.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics

from ..artifacts.files import _atomic_json
from ..profile_versions import SUPERSEDED, ProfileError


DRIFT_STATE_SCHEMA = "swarm_algo_research_drift_v1"
LIVE_FEED_SCHEMA = "swarm_algo_live_drift_feed_v1"
DEFAULT_DRIFT_CONFIG = {
    "reference_sessions": 21,
    "auto_demote": True,
    "auto_demote_mode": "dry_run",
}
DEMOTE_MODES = ("dry_run", "apply")
DEMOTION_REASON = "drift"
DEMOTION_ACTOR = "drift_monitor"
PH_DELTA = 0.15
PH_THRESHOLD = 8.0
WARNING_STREAK = 3
RECOVERY_SESSIONS = 20


def resolve_drift_config(config: dict | None = None) -> dict:
    """Merge caller overrides onto the defaults and reject unknown or invalid keys."""
    resolved = {**DEFAULT_DRIFT_CONFIG, **(config or {})}
    unknown = set(resolved) - set(DEFAULT_DRIFT_CONFIG)
    if unknown:
        raise ValueError(f"unknown drift monitor config key(s): {', '.join(sorted(unknown))}")
    sessions = resolved["reference_sessions"]
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 2:
        raise ValueError("reference_sessions must be an integer of at least 2")
    if not isinstance(resolved["auto_demote"], bool):
        raise ValueError("auto_demote must be a boolean")
    if resolved["auto_demote_mode"] not in DEMOTE_MODES:
        raise ValueError("auto_demote_mode must be 'dry_run' or 'apply'")
    return resolved


def load_live_drift_feed(path: str | Path) -> dict | None:
    """Return {strategy: [{"date", "return"}, ...]} or None when no feed exists."""
    value = _read_json(Path(path))
    if not value or value.get("schema") != LIVE_FEED_SCHEMA:
        return None
    strategies = value.get("strategies")
    if not isinstance(strategies, dict):
        return None
    return {str(name): rows for name, rows in strategies.items() if isinstance(rows, list)}


def update_research_drift_monitor(
    verification: dict | None, *, output_dir: str | Path,
    live_returns: dict | None = None, config: dict | None = None,
) -> dict:
    """Consume only newly observed returns and update alert state.

    Live returns are consumed from the start of the feed on first use and by
    date cursor afterwards. Backtest equity establishes a cursor at its current
    endpoint, so older research results cannot influence the monitor.
    """
    cfg = resolve_drift_config(config)
    required = cfg["reference_sessions"]
    root = Path(output_dir)
    state_path = root / "drift-monitor.json"
    state = _read_state(state_path)
    now = datetime.now(timezone.utc).isoformat()

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
                **_new_item(now),
                "last_observed_date": rows[-1]["date"],
                "last_equity": rows[-1]["equity"],
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
                _observe(item, realized_return, required)
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
            "reference_session_count": _reference_count(item, required),
        }

    live_updates = {}
    if live_returns is not None:
        for strategy, rows in sorted(live_returns.items()):
            observations = _clean_live_returns(rows)
            if not observations:
                continue
            item = state["live_strategies"].get(strategy)
            if item is None:
                item = {**_new_item(now), "last_observed_date": ""}
                state["live_strategies"][strategy] = item
            consumed = _consume(item, observations, required)
            if item.get("status") != "warning":
                item["demotion_actioned"] = False
            live_updates[strategy] = {
                **_summary(item, required),
                "new_sessions_consumed": consumed,
            }

    state["schema"] = DRIFT_STATE_SCHEMA
    state["updated_at"] = now
    state["detector"] = {
        "method": "one_sided_page_hinkley_on_standardized_daily_returns",
        "reference_sessions": required,
        "delta": PH_DELTA,
        "threshold": PH_THRESHOLD,
        "warning_streak": WARNING_STREAK,
        "recovery_sessions": RECOVERY_SESSIONS,
        "risk_sizing_changed": False,
    }
    _atomic_json(state_path, state)

    live_all = {name: _summary(row, required) for name, row in state["live_strategies"].items()}
    backtest_all = {name: row.get("status", "warming_up") for name, row in state["strategies"].items()}
    live_status = {name: row["status"] for name, row in live_all.items()}
    live_warning = sorted(name for name, value in live_status.items() if value == "warning")
    live_warming = sorted(name for name, value in live_status.items() if value == "warming_up")
    backtest_warning = sorted(name for name, value in backtest_all.items() if value == "warning")
    backtest_warming = sorted(name for name, value in backtest_all.items() if value == "warming_up")
    all_values = list(live_status.values()) + list(backtest_all.values())
    warning = bool(live_warning or backtest_warning)
    return {
        "status": "warning" if warning else (
            "monitoring" if "monitoring" in all_values else "warming_up"
        ),
        "research_only": True,
        "promotion_pause": warning,
        "live_sizing_changed": False,
        "config": cfg,
        "live": {
            "feed_present": live_returns is not None,
            "strategy_statuses": live_updates,
            "all_strategy_statuses": live_all,
            "warning_strategies": live_warning,
            "warming_up_strategies": live_warming,
        },
        "backtest": {
            "strategy_statuses": updates,
            "all_strategy_statuses": backtest_all,
            "warning_strategies": backtest_warning,
            "warming_up_strategies": backtest_warming,
        },
        "strategy_statuses": updates,
        "all_strategy_statuses": backtest_all,
        "state_path": str(state_path.resolve()),
        "action": "pause_profile_promotion_until_drift_clears" if warning else "continue_research_monitoring",
    }


def apply_drift_demotions(
    drift_report: dict, *, output_dir: str | Path, store, config: dict | None = None,
) -> dict:
    """Revert flagged live strategies to their most recent prior profile versions.

    ``store`` is a ``ProfileStore``. In ``dry_run`` mode each revert is planned
    and nothing is written. In ``apply`` mode the revert runs with reason
    ``drift`` and actor ``drift_monitor``; the live state is then marked so the
    same warning episode is not demoted again on the next cycle.
    """
    cfg = resolve_drift_config(config)
    result = {
        "enabled": cfg["auto_demote"], "mode": cfg["auto_demote_mode"],
        "actions": [], "warnings": [],
    }
    if not cfg["auto_demote"]:
        return result
    state_path = Path(output_dir) / "drift-monitor.json"
    state = _read_state(state_path)
    changed = False
    flagged = ((drift_report or {}).get("live") or {}).get("warning_strategies") or []
    for strategy in flagged:
        item = state["live_strategies"].get(strategy) or {}
        if item.get("demotion_actioned"):
            continue
        applied = False
        for profile in store.active_versions(strategy=strategy):
            regime = profile["regime"]
            target = _most_recent_prior(store, profile)
            if target is None:
                result["warnings"].append(
                    f"{strategy}:{regime} has no prior version; not demoted",
                )
                continue
            dry_run = cfg["auto_demote_mode"] == "dry_run"
            try:
                outcome = store.revert(
                    strategy=strategy, regime=regime, version_id=target["version_id"],
                    reason=DEMOTION_REASON, actor=DEMOTION_ACTOR, dry_run=dry_run,
                )
            except ProfileError as exc:
                result["warnings"].append(f"{strategy}:{regime} revert refused: {exc}")
                continue
            action = {
                "strategy": strategy, "regime": regime,
                "from_version_id": profile["version_id"],
                "to_version_id": target["version_id"],
                "changed_keys": outcome.get("changed_keys", []),
                "mode": cfg["auto_demote_mode"],
            }
            if not dry_run:
                action["new_version_id"] = outcome.get("new_version_id")
                action["release_id"] = outcome.get("release_id")
                applied = True
            result["actions"].append(action)
        if applied:
            item["demotion_actioned"] = True
            changed = True
    if changed:
        _atomic_json(state_path, state)
    return result


def _most_recent_prior(store, profile: dict) -> dict | None:
    """Latest superseded version of the same pair that predates the active one."""
    history = store.history(profile["strategy"], profile["regime"])["versions"]
    candidates = [
        row for row in history
        if row["version_id"] != profile["version_id"]
        and row["status"] == SUPERSEDED
        and row["parameters"] is not None
        and str(row["created_at"]) < str(profile["created_at"])
    ]
    return candidates[-1] if candidates else None


def _new_item(now: str) -> dict:
    return {
        "status": "warming_up",
        "reference_returns": [],
        "reference_mean": None,
        "reference_std": None,
        "negative_cusum": 0.0,
        "warning_streak": 0,
        "healthy_streak": 0,
        "new_sessions_consumed": 0,
        "monitor_started_at": now,
    }


def _clean_live_returns(rows) -> list[tuple[str, float]]:
    by_date: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = str(row.get("date") or "")[:10]
        value = _finite(row.get("return"))
        if day and value is not None:
            by_date[day] = value
    return sorted(by_date.items())


def _consume(item: dict, observations: list[tuple[str, float]], required: int) -> int:
    last_date = str(item.get("last_observed_date") or "")
    consumed = 0
    for day, value in observations:
        if day <= last_date:
            continue
        _observe(item, value, required)
        consumed += 1
        last_date = day
    item["last_observed_date"] = last_date
    item["new_sessions_consumed"] = int(item.get("new_sessions_consumed", 0)) + consumed
    return consumed


def _reference_count(item: dict, required: int) -> int:
    if item.get("reference_mean") is not None:
        return required
    return len(item.get("reference_returns") or [])


def _summary(item: dict, required: int) -> dict:
    return {
        "status": item.get("status", "warming_up"),
        "reference_sessions_observed": _reference_count(item, required),
        "reference_sessions_required": required,
        "negative_cusum": round(float(item.get("negative_cusum", 0.0)), 4),
        "warning_streak": int(item.get("warning_streak", 0)),
        "last_observed_date": item.get("last_observed_date"),
    }


def _observe(item: dict, value: float, required: int) -> None:
    if not math.isfinite(value):
        return
    reference = item.setdefault("reference_returns", [])
    if item.get("reference_mean") is None:
        reference.append(value)
        if len(reference) >= required:
            item["reference_mean"] = statistics.mean(reference)
            item["reference_std"] = max(statistics.pstdev(reference), 1e-5)
            item["reference_returns"] = reference[-required:]
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
        return {"schema": DRIFT_STATE_SCHEMA, "strategies": {}, "live_strategies": {}}
    if value.get("schema") != DRIFT_STATE_SCHEMA or not isinstance(value.get("strategies"), dict):
        raise ValueError(f"unsupported or malformed drift monitor state: {path}")
    value.setdefault("live_strategies", {})
    return value


def _finite(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


__all__ = [
    "DEFAULT_DRIFT_CONFIG", "DRIFT_STATE_SCHEMA", "LIVE_FEED_SCHEMA",
    "apply_drift_demotions", "load_live_drift_feed", "resolve_drift_config",
    "update_research_drift_monitor",
]
