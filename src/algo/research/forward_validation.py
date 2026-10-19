"""Forward-only simulated trials for regime-specific research candidates."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.algo.profile_store import FORWARD_VALIDATION_PATH, PROMOTED_PROFILE_PATH, write_json_atomic
from src.algo.strategies.registry import (
    ALGO_DEFAULT_PARAMETERS, REGIME_PARAMETER_PROFILES,
    refresh_active_parameter_profile, validate_parameter_profile,
)

from .adaptive_search import _load_window_data, _run_replay, _trading_sessions
from .candidate_archive import candidate_id
from .settings import ADJUSTMENT, FEED, RESEARCH_WARMUP_DAYS


ET = ZoneInfo("America/New_York")
CAPITAL = 100_000.0
MAX_TRIAL_DAYS = 180
MIN_CLOSED_TRADES = 30
MIN_EXIT_SESSIONS = 20
MIN_REGIME_ENTRY_SESSIONS = 15
SIMULATION_POLICY = "algo-forward-simulation-v2-marked-equity"


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read {path.name}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ET)


def _session(value: object) -> str | None:
    parsed = _time(value)
    return parsed.date().isoformat() if parsed else None


def _current_regime(store, end: date) -> str | None:
    from src.algo.strategies.regime import rank_strategies
    from src.config import get_algo_runtime_settings

    settings = get_algo_runtime_settings()
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(end - timedelta(days=RESEARCH_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    if not spy:
        return None
    result = rank_strategies(
        spy, mode="algo", event_enabled=settings.algo_event_driven_enabled,
        intraday_trend_enabled=settings.enable_intraday_trend,
        intraday_momentum_enabled=settings.enable_intraday_momentum,
    )
    regime = str(result.get("regime") or "unknown")
    return regime if regime != "unknown" else None


def _candidates(discovery: dict, verification: dict, regime: str) -> list[dict]:
    output = []
    verification_rows = verification.get("strategies") or {}
    for strategy, discovery_status in (discovery.get("strategies") or {}).items():
        verification_status = verification_rows.get(strategy) or {}
        checks = (verification_status.get("summary") or {}).get("checks") or {}
        if verification_status.get("status") == "error" or checks.get("overall_pass") is not True:
            continue
        artifact = discovery_status.get("detail_artifact")
        if not artifact:
            continue
        try:
            detail = _read_json(Path(artifact))
        except ValueError:
            continue
        for item in (detail.get("adaptive_regime_search") or {}).get("profiles") or []:
            if item.get("regime") != regime or not item.get("improved_over_baseline"):
                continue
            params = item.get("parameters")
            if not isinstance(params, dict):
                continue
            try:
                parameters = validate_parameter_profile({strategy: params})[strategy]
            except (ValueError, KeyError):
                continue
            output.append({
                "candidate_id": candidate_id(strategy, parameters, regime),
                "strategy": strategy,
                "regime": regime,
                "parameters": parameters,
                "discovery_fitness": item.get("fitness"),
                "discovery_metrics": item.get("metrics"),
                "baseline_discovery_fitness": item.get("baseline_fitness"),
                "historical_verification": {"overall_pass": True, "checks": checks},
                "discovery_artifact": str(Path(artifact).resolve()),
            })
    return output


def _simulate(
    *, strategy: str, regime: str, parameters: dict, start: date, end: date,
    database: str, symbols: list[str], candidate: bool,
) -> dict:
    from src.algo.strategies.multi_factor.features import aggregate_intraday_bars

    bars, spy = _load_window_data(
        db=database, symbols=symbols, start=start, end=end,
        adjustment=ADJUSTMENT, strategy=strategy,
    )
    daily = aggregate_intraday_bars(bars)
    current_regimes = deepcopy(REGIME_PARAMETER_PROFILES.get(strategy) or {})
    if candidate:
        current_regimes[regime] = parameters
    args = SimpleNamespace(
        adjustment=ADJUSTMENT,
        capital=CAPITAL,
        slippage_bps=5.0,
        spread_bps=3.0,
        commission_per_share=0.005,
        short_borrow_annual_pct=3.0,
        unknown_borrow_penalty_bps=25.0,
        borrow_availability_data={},
    )
    return _run_replay(
        strategy=strategy,
        parameters=ALGO_DEFAULT_PARAMETERS[strategy],
        symbols=symbols, bars=bars, spy_daily=spy, start=start, end=end,
        args=args, regime_profiles={strategy: current_regimes},
        force_close_at_end=False,
        preaggregated_daily_bars=daily,
    )


def _target_metrics(report: dict, strategy: str, regime: str) -> dict:
    trades = [
        row for row in report.get("closed_trades", [])
        if row.get("strategy") == strategy and row.get("entry_regime") == regime
    ]
    exit_sessions = {_session(row.get("exit_at")) for row in trades}
    entry_sessions = {_session(row.get("entry_at")) for row in trades}
    exit_sessions.discard(None)
    entry_sessions.discard(None)
    net_pnl = round(sum(float(row.get("pnl") or 0.0) for row in trades), 2)
    final_equity = report.get("final_equity")
    marked_pnl = (
        round(float(final_equity) - CAPITAL, 2)
        if isinstance(final_equity, (int, float)) else None
    )
    return {
        "closed_trades": len(trades),
        "distinct_exit_sessions": len(exit_sessions),
        "target_regime_entry_sessions": len(entry_sessions),
        "net_realized_pnl_after_simulated_costs": net_pnl,
        "marked_pnl_after_estimated_exit_costs": marked_pnl,
        "entry_sessions": sorted(entry_sessions),
        "exit_sessions": sorted(exit_sessions),
        "overall_net_return_pct": report.get("net_return_pct"),
        "overall_max_drawdown_pct": report.get("max_drawdown_pct"),
        "overall_open_position_count": report.get("open_position_count"),
        "overall_closed_trade_count": report.get("closed_trade_count"),
    }


def _baseline_profile_fingerprint(strategy: str) -> str:
    payload = {
        "parameters": ALGO_DEFAULT_PARAMETERS[strategy],
        "regime_profiles": REGIME_PARAMETER_PROFILES.get(strategy) or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _promote(trial: dict, metrics: dict, promoted: dict) -> dict:
    strategy, regime = trial["strategy"], trial["regime"]
    promoted.setdefault("regime_profiles", {}).setdefault(strategy, {})[regime] = trial["parameters"]
    promoted.setdefault("regime_profile_ids", {}).setdefault(strategy, {})[regime] = trial["candidate_id"]
    record = {
        "candidate_id": trial["candidate_id"],
        "strategy": strategy,
        "regime": regime,
        "parameters": trial["parameters"],
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "forward_metrics": metrics,
        "historical_verification": trial.get("historical_verification"),
        "discovery_artifact": trial.get("discovery_artifact"),
    }
    promoted["schema"] = "swarm_algo_promoted_profiles_v1"
    promoted["updated_at"] = datetime.now(timezone.utc).isoformat()
    promoted["release_id"] = f"forward-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    prior = promoted.setdefault("history", [])
    prior.append(record)
    promoted["history"] = prior[-200:]
    return record


def run_forward_validation_cycle(
    *, discovery: dict, verification: dict, store, database: str,
    symbols: list[str], end: date, data_version: str, allow_stage: bool = True,
) -> dict:
    """Replay staged profiles only over sessions after their discovery date."""
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    with _locked(FORWARD_VALIDATION_PATH):
        try:
            state = _read_json(FORWARD_VALIDATION_PATH, {
                "schema": "swarm_algo_forward_validation_v1",
                "active_trials": {}, "history": [],
            })
            refresh_active_parameter_profile()
            promoted = _read_json(PROMOTED_PROFILE_PATH)
            active = state.setdefault("active_trials", {})
            history = state.setdefault("history", [])
            if not isinstance(active, dict) or not isinstance(history, list):
                raise ValueError("Forward validation record has invalid active_trials or history")
            regime = _current_regime(store, end)
        except Exception as exc:
            return {
                "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "promoted": [], "staged_candidate_ids": [],
                "live_configuration_changed": False,
            }

        promoted_rows = []
        rejected = []
        expired = []
        simulation_errors = []
        staging_errors = []
        for key, trial in list(active.items()):
            if not isinstance(trial, dict) or trial.get("status") != "validating":
                continue
            try:
                staged = date.fromisoformat(trial["staged_on"])
                age_days = (end - staged).days
            except (KeyError, TypeError, ValueError):
                continue
            if age_days > MAX_TRIAL_DAYS:
                trial.update({
                    "status": "expired", "finished_at": now.isoformat(),
                    "reason": "Forward simulation exceeded its maximum duration",
                })
                history.append(trial)
                active.pop(key, None)
                expired.append(trial["candidate_id"])
                continue

            start = staged + timedelta(days=1)
            sessions = _trading_sessions(
                database, end, start=start, adjustment=ADJUSTMENT,
            )
            if not sessions:
                trial["latest_simulation"] = {
                    "status": "waiting_for_future_sessions",
                    "data_end": end.isoformat(), "data_version": data_version,
                }
                continue
            simulation_start = sessions[0]
            previous = trial.get("latest_simulation") or {}
            baseline_profile_sha256 = _baseline_profile_fingerprint(trial["strategy"])
            if (
                previous.get("status") == "completed"
                and previous.get("simulation_policy") == SIMULATION_POLICY
                and isinstance(previous.get("candidate_metrics"), dict)
                and isinstance(previous.get("baseline_metrics"), dict)
                and previous.get("data_version") == data_version
                and previous.get("data_end") == end.isoformat()
                and previous.get("baseline_profile_sha256") == baseline_profile_sha256
            ):
                candidate_metrics = previous["candidate_metrics"]
                baseline_metrics = previous["baseline_metrics"]
            else:
                try:
                    baseline_report = _simulate(
                        strategy=trial["strategy"], regime=trial["regime"],
                        parameters=trial["parameters"], start=simulation_start, end=end,
                        database=database, symbols=symbols, candidate=False,
                    )
                    candidate_report = _simulate(
                        strategy=trial["strategy"], regime=trial["regime"],
                        parameters=trial["parameters"], start=simulation_start, end=end,
                        database=database, symbols=symbols, candidate=True,
                    )
                    baseline_metrics = _target_metrics(
                        baseline_report, trial["strategy"], trial["regime"],
                    )
                    candidate_metrics = _target_metrics(
                        candidate_report, trial["strategy"], trial["regime"],
                    )
                    trial["latest_simulation"] = {
                        "status": "completed", "simulation_policy": SIMULATION_POLICY,
                        "data_end": end.isoformat(),
                        "data_version": data_version,
                        "baseline_profile_sha256": baseline_profile_sha256,
                        "simulation_start": simulation_start.isoformat(),
                        "candidate_metrics": candidate_metrics,
                        "baseline_metrics": baseline_metrics,
                    }
                except Exception as exc:
                    trial["latest_simulation"] = {
                        "status": "error", "simulation_policy": SIMULATION_POLICY,
                        "data_end": end.isoformat(),
                        "data_version": data_version,
                        "baseline_profile_sha256": baseline_profile_sha256,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    simulation_errors.append({
                        "candidate_id": trial.get("candidate_id"),
                        "error": trial["latest_simulation"]["error"],
                    })
                    continue

            sample_met = bool(
                candidate_metrics["closed_trades"] >= MIN_CLOSED_TRADES
                and candidate_metrics["distinct_exit_sessions"] >= MIN_EXIT_SESSIONS
                and candidate_metrics["target_regime_entry_sessions"] >= MIN_REGIME_ENTRY_SESSIONS
            )
            trial["latest_simulation"]["minimum_sample_met"] = sample_met
            if not sample_met:
                continue
            candidate_pnl = float(candidate_metrics["net_realized_pnl_after_simulated_costs"])
            baseline_pnl = float(baseline_metrics["net_realized_pnl_after_simulated_costs"])
            forward_improvement = candidate_pnl > baseline_pnl
            trial["latest_simulation"]["forward_pnl_improvement"] = round(
                candidate_pnl - baseline_pnl, 2,
            )
            trial["finished_at"] = now.isoformat()
            candidate_marked_pnl = candidate_metrics.get("marked_pnl_after_estimated_exit_costs")
            baseline_marked_pnl = baseline_metrics.get("marked_pnl_after_estimated_exit_costs")
            marked_equity_improvement = bool(
                isinstance(candidate_marked_pnl, (int, float))
                and isinstance(baseline_marked_pnl, (int, float))
                and candidate_marked_pnl > 0
                and candidate_marked_pnl > baseline_marked_pnl
            )
            trial["latest_simulation"]["marked_equity_improvement"] = marked_equity_improvement
            trial["forward_validation_pass"] = bool(
                candidate_pnl > 0 and forward_improvement and marked_equity_improvement
            )
            if trial["forward_validation_pass"]:
                promoted_rows.append(_promote(
                    trial, trial["latest_simulation"], promoted,
                ))
                trial["status"] = "promoted"
                history.append(trial)
            else:
                trial["status"] = "rejected"
                trial["reason"] = (
                    "Forward candidate did not produce positive realized P&L, outperform the "
                    "current profile, and finish with higher positive marked equity after "
                    "estimated exit costs"
                )
                history.append(trial)
                rejected.append(trial["candidate_id"])
            active.pop(key, None)

        staged_ids = []
        if allow_stage and regime:
            try:
                candidates = _candidates(discovery, verification, regime)
            except Exception as exc:
                candidates = []
                staging_errors.append(f"{type(exc).__name__}: {exc}")
            active_strategies = {
                trial.get("strategy") for trial in active.values()
                if isinstance(trial, dict) and trial.get("status") == "validating"
            }
            live_ids = promoted.get("regime_profile_ids") or {}
            for candidate in candidates:
                strategy = candidate["strategy"]
                key = f"{strategy}:{regime}"
                if strategy in active_strategies:
                    continue
                if (live_ids.get(strategy) or {}).get(regime) == candidate["candidate_id"]:
                    continue
                if any(
                    row.get("candidate_id") == candidate["candidate_id"]
                    and row.get("status") in {"rejected", "expired"}
                    for row in history if isinstance(row, dict)
                ):
                    continue
                candidate.update({
                    "status": "validating", "staged_on": end.isoformat(),
                    "staged_at": now.isoformat(), "latest_simulation": None,
                })
                active[key] = candidate
                active_strategies.add(strategy)
                staged_ids.append(candidate["candidate_id"])

        if promoted_rows:
            write_json_atomic(PROMOTED_PROFILE_PATH, promoted)
        state.update({
            "schema": "swarm_algo_forward_validation_v1",
            "updated_at": now.isoformat(),
            "current_regime": regime,
            "minimum_sample": {
                "closed_trades": MIN_CLOSED_TRADES,
                "distinct_exit_sessions": MIN_EXIT_SESSIONS,
                "target_regime_entry_sessions": MIN_REGIME_ENTRY_SESSIONS,
            },
            "maximum_trial_days": MAX_TRIAL_DAYS,
            "simulation_policy": SIMULATION_POLICY,
            "cost_model": {
                "slippage_bps_each_side": 5.0,
                "spread_bps_full": 3.0,
                "commission_per_share_each_side": 0.005,
                "short_borrow_annual_pct": 3.0,
                "unknown_borrow_penalty_bps": 25.0,
            },
            "last_cycle": {
                "data_end": end.isoformat(), "data_version": data_version,
                "promoted_candidate_ids": [row["candidate_id"] for row in promoted_rows],
                "staged_candidate_ids": staged_ids,
                "rejected_candidate_ids": rejected,
                "expired_candidate_ids": expired,
                "simulation_errors": simulation_errors,
                "staging_errors": staging_errors,
            },
            "active_trials": active,
            "history": history[-500:],
        })
        write_json_atomic(FORWARD_VALIDATION_PATH, state)

    return {
        "status": "partial_error" if simulation_errors or staging_errors else "completed",
        "current_regime": regime,
        "active_trial_count": len(active),
        "staged_candidate_ids": staged_ids,
        "promoted": promoted_rows,
        "rejected_candidate_ids": rejected,
        "expired_candidate_ids": expired,
        "simulation_errors": simulation_errors,
        "staging_errors": staging_errors,
        "minimum_sample": {
            "closed_trades": MIN_CLOSED_TRADES,
            "distinct_exit_sessions": MIN_EXIT_SESSIONS,
            "target_regime_entry_sessions": MIN_REGIME_ENTRY_SESSIONS,
        },
        "live_configuration_changed": bool(promoted_rows),
        "duration_seconds": round(time.monotonic() - started, 3),
    }


__all__ = ["run_forward_validation_cycle"]
