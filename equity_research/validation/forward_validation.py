"""Forward-only simulated trials for regime-specific research candidates."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from portfolio.profile_store import FORWARD_VALIDATION_PATH, PROMOTED_PROFILE_PATH, write_json_atomic
from strategies.registry import (
    ALGO_DEFAULT_PARAMETERS, REGIME_PARAMETER_PROFILES,
    refresh_active_parameter_profile, validate_parameter_profile,
)

from ..pipeline.adaptive_search import _load_forward_window_data, _run_replay, _trading_sessions
from . import history_holdout
from ..artifacts.candidate_archive import candidate_id
from ..profile_versions import (
    ProfileStore, cumulative_trial_count, project_live, record_trial_evaluations,
)
from ..costs import EQUITY_COMMISSION_PER_SHARE, EQUITY_SLIPPAGE_BPS, EQUITY_SPREAD_BPS
from ..settings import (
    ADJUSTMENT, FEED, RESEARCH_WARMUP_DAYS, FORWARD_TRIAL_MAX_DAYS,
    SIP_EXTENDED_LOCAL_FEED,
)
from equity_research.backtest.market_sessions import trading_date
from .final_holdout import active_holdout_pairs
from equity_research.validation.acceptance import AcceptancePolicy, evaluate_candidate, evaluate_search


ET = ZoneInfo("America/New_York")
CAPITAL = 100_000.0
MAX_TRIAL_DAYS = FORWARD_TRIAL_MAX_DAYS
MIN_CLOSED_TRADES = 30
MIN_EXIT_SESSIONS = 20
MIN_REGIME_ENTRY_SESSIONS = 15
MAX_ACTIVE_COHORT_PER_STRATEGY_REGIME = 9
# Historical candidates are promoted only after beating the live profile on a rotating holdout.
HISTORY_PROMOTION_ENABLED = True
MAX_PROMOTIONS_PER_CYCLE = 1
MAX_PROMOTIONS_PER_STRATEGY_PER_CYCLE = 1
SIMULATION_POLICY = "algo-forward-simulation-v6-dsr-search-then-holdout"
# Replay equity already carries simulated costs, so turnover costs are not charged twice.
# min_deflated_sharpe is the promotion threshold on the Deflated Sharpe Ratio.
FORWARD_ACCEPTANCE_POLICY = AcceptancePolicy(
    cost_bps_per_turnover=0.0, min_oos_sessions=20, folds=3, purge_sessions=2,
    min_deflated_sharpe=0.90,
)
FORWARD_COST_MODEL = {
    "version": "algo-replay-cost-v1",
    "slippage_bps_each_side": EQUITY_SLIPPAGE_BPS,
    "spread_bps_full": EQUITY_SPREAD_BPS,
    "commission_per_share_each_side": EQUITY_COMMISSION_PER_SHARE,
    "short_borrow_annual_pct": 3.0,
    "unknown_borrow_penalty_bps": 25.0,
}
FORWARD_COST_MODEL_VERSION = (
    f"{FORWARD_COST_MODEL['version']}:{hashlib.sha256(json.dumps(FORWARD_COST_MODEL, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:12]}"
)


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
    return trading_date(parsed, include_extended_hours=True).isoformat() if parsed else None


def _forward_session_data_version(database: str, base_version: str, symbols: list[str], end: date) -> str:
    """Include the auxiliary feeds that can affect active 180-day trials."""
    lower = (end - timedelta(days=MAX_TRIAL_DAYS + 2)).isoformat()
    upper = (end + timedelta(days=2)).isoformat()
    feeds = (SIP_EXTENDED_LOCAL_FEED, "boats")
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """SELECT feed, symbol, COUNT(*), MIN(timestamp), MAX(timestamp), MAX(ingested_at)
               FROM market_bar
               WHERE feed IN (?, ?) AND adjustment=? AND timeframe='5Min'
                 AND symbol IN (%s) AND timestamp>=? AND timestamp<?
               GROUP BY feed, symbol ORDER BY feed, symbol"""
            % ",".join("?" for _ in symbols),
            (
                *feeds, ADJUSTMENT, *(symbol.upper() for symbol in symbols),
                lower, upper,
            ),
        ).fetchall()
    payload = json.dumps(
        {"regular_data_version": base_version, "session_data": rows},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _current_regime(store, end: date) -> str | None:
    from strategies.regime import rank_strategies
    from market.regime import load_fred_regime_series
    from portfolio.config import (
        ALGO_PORTFOLIO_UNIVERSE, get_algo_regime_config, get_algo_runtime_settings,
    )

    settings = get_algo_runtime_settings()
    regime_config = get_algo_regime_config()
    context_symbols = list(dict.fromkeys(
        [ticker for bucket in ALGO_PORTFOLIO_UNIVERSE.values() for ticker in bucket["tickers"]]
        + ["SPY", "HYG", "LQD"]
    ))
    context_bars = store.bars(
        context_symbols, feed=FEED, timeframe="1Day",
        start=(end - timedelta(days=RESEARCH_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy = context_bars.get("SPY", [])
    if not spy:
        return None
    macro_series, _ = load_fred_regime_series(store)
    result = rank_strategies(
        spy, mode="algo", llm_enabled=settings.swing_llm_driven_enabled,
        intraday_trend_enabled=settings.enable_intraday_trend_following,
        intraday_momentum_enabled=settings.enable_intraday_momentum,
        feature_set=regime_config["feature_set"],
        engine=regime_config["engine"],
        strategy_policy=regime_config["strategy_policy"],
        context_bars=context_bars,
        macro_series=macro_series,
        include_models=regime_config["include_models"],
        model_training_window=regime_config["model_training_window"],
        model_min_confidence=regime_config["min_model_confidence"],
        strategy_regime_weights=regime_config["strategy_regime_weights"],
        strategy_enabled=regime_config["strategy_enabled"],
        volatile_policy_risk_multiplier=regime_config["volatile_policy_risk_multiplier"],
        volatility_risk_scales=regime_config["volatility_risk_scales"],
    )
    regime = str(result.get("regime") or "unknown")
    return regime if regime != "unknown" else None


def _candidates(discovery: dict, verification: dict, regime: str) -> list[dict]:
    output = []
    verification_rows = verification.get("strategies") or {}
    candidate_validation_rows = (
        (verification.get("candidate_validation") or {}).get("strategies") or {}
    )
    exact_validation_by_strategy = {}
    for strategy, validation in candidate_validation_rows.items():
        if validation.get("status") != "completed":
            continue
        exact_validation_by_strategy[strategy] = {
            row.get("candidate_id"): row
            for row in validation.get("candidates") or []
            if row.get("candidate_id")
        }
    for strategy, discovery_status in (discovery.get("strategies") or {}).items():
        verification_status = verification_rows.get(strategy) or {}
        verification_summary = verification_status.get("summary") or {}
        if (
            discovery_status.get("status") not in {"completed", "reused"}
            or verification_status.get("status") not in {"completed", "reused"}
            or not verification_summary
        ):
            continue
        checks = verification_summary.get("checks") or {}
        artifact = discovery_status.get("detail_artifact")
        if not artifact:
            continue
        try:
            detail = _read_json(Path(artifact))
        except ValueError:
            continue
        strategy_candidates = []
        for item in (detail.get("adaptive_regime_search") or {}).get("profiles") or []:
            if item.get("regime") != regime:
                continue
            if not item.get("planned_ablation") and not item.get("improved_over_baseline"):
                continue
            params = item.get("parameters")
            if not isinstance(params, dict):
                continue
            try:
                parameters = validate_parameter_profile({strategy: params})[strategy]
            except (ValueError, KeyError):
                continue
            exact_validation = exact_validation_by_strategy.get(strategy, {}).get(
                candidate_id(strategy, parameters),
            )
            if not exact_validation or exact_validation.get("status") != "passed":
                continue
            output.append({
                "candidate_id": candidate_id(strategy, parameters, regime),
                "source_candidate_id": item.get("source_candidate_id"),
                "parent_candidate_id": item.get("parent_candidate_id"),
                "strategy": strategy,
                "regime": regime,
                "parameters": parameters,
                "model_family": item.get("model_family"),
                "logic_version": item.get("logic_version"),
                "candidate_name": item.get("candidate_name"),
                "planned_ablation": bool(item.get("planned_ablation")),
                "cost_model_version": item.get("cost_model_version"),
                "discovery_fitness": item.get("fitness"),
                "discovery_metrics": item.get("metrics"),
                "baseline_discovery_fitness": item.get("baseline_fitness"),
                "historical_verification": {
                    "status": verification_status.get("status"),
                    "overall_pass": checks.get("overall_pass"),
                    "checks": checks,
                    "thresholds": verification_summary.get("thresholds"),
                    "stitched_oos": verification_summary.get("stitched_oos"),
                    "average_fitness_decay": verification_summary.get("average_fitness_decay"),
                    "fold_count": verification_summary.get("fold_count"),
                    "exact_candidate_validation": exact_validation,
                },
                "screening": {
                    "improved_over_training_baseline": bool(item.get("improved_over_baseline")),
                    "historical_sample_met": bool(item.get("historical_sample_met")),
                    "historical_positive_after_costs": bool(item.get("historical_positive_after_costs")),
                    "regime": regime,
                },
                "discovery_artifact": str(Path(artifact).resolve()),
            })
            strategy_candidates.append(output[-1])
        # Stage the complete planned ablation set first. Further candidates can
        # fill unused slots only after a later discovery cycle.
        strategy_candidates.sort(key=lambda row: (
            not row.get("planned_ablation"),
            str(row.get("candidate_name") or ""),
            -float(row.get("discovery_fitness") or 0.0),
            row["candidate_id"],
        ))
        strategy_candidates[:] = strategy_candidates[:MAX_ACTIVE_COHORT_PER_STRATEGY_REGIME]
        output = [row for row in output if row.get("strategy") != strategy]
        output.extend(strategy_candidates)

    existing_ids = {row.get("candidate_id") for row in output}
    for strategy, validation in candidate_validation_rows.items():
        if validation.get("status") != "completed":
            continue
        verification_status = verification_rows.get(strategy) or {}
        verification_summary = verification_status.get("summary") or {}
        if (
            verification_status.get("status") not in {"completed", "reused"}
            or not verification_summary
        ):
            continue
        checks = verification_summary.get("checks") or {}
        for candidate in validation.get("candidates") or []:
            if candidate.get("status") != "passed":
                continue
            roles = set(candidate.get("validation_roles") or [])
            if "challenger" not in roles or roles.intersection({"live_setting", "champion"}):
                continue
            regime_metrics = (
                (candidate.get("current_regime_metrics") or {}).get(regime)
                or (candidate.get("regime_metrics") or {}).get(regime)
                or {}
            )
            if not regime_metrics.get("eligible"):
                continue
            if not regime_metrics.get("planned_ablation") and not regime_metrics.get("improved_over_baseline"):
                continue
            params = candidate.get("parameters")
            if not isinstance(params, dict):
                continue
            try:
                parameters = validate_parameter_profile({strategy: params})[strategy]
            except (ValueError, KeyError):
                continue
            forward_id = candidate_id(strategy, parameters, regime)
            if forward_id in existing_ids:
                continue
            output.append({
                "candidate_id": forward_id,
                "source_candidate_id": candidate.get("candidate_id"),
                "source_invocation_id": candidate.get("source_invocation_id"),
                "source_invocation_ids": candidate.get("source_invocation_ids") or [],
                "parent_candidate_id": regime_metrics.get("parent_candidate_id"),
                "strategy": strategy,
                "regime": regime,
                "parameters": parameters,
                "model_family": candidate.get("model_family"),
                "logic_version": candidate.get("logic_version"),
                "candidate_name": f"archive:{candidate.get('candidate_id')}",
                "planned_ablation": False,
                "cost_model_version": candidate.get("cost_model_version"),
                "discovery_fitness": regime_metrics.get("fitness"),
                "discovery_metrics": regime_metrics.get("metrics"),
                "baseline_discovery_fitness": regime_metrics.get("baseline_fitness"),
                "historical_verification": {
                    "status": verification_status.get("status"),
                    "overall_pass": checks.get("overall_pass"),
                    "checks": checks,
                    "thresholds": verification_summary.get("thresholds"),
                    "stitched_oos": verification_summary.get("stitched_oos"),
                    "average_fitness_decay": verification_summary.get("average_fitness_decay"),
                    "fold_count": verification_summary.get("fold_count"),
                    "exact_candidate_validation": candidate,
                },
                "screening": {
                    "improved_over_training_baseline": bool(regime_metrics.get("improved_over_baseline")),
                    "historical_sample_met": bool(regime_metrics.get("historical_sample_met")),
                    "historical_positive_after_costs": bool(regime_metrics.get("historical_positive_after_costs")),
                    "regime": regime,
                },
                "discovery_artifact": candidate.get("archive_artifact"),
            })
            existing_ids.add(forward_id)
    return output


def _equity_returns(daily_curve: list[dict]) -> dict[str, float]:
    """Per-session returns from a replay's daily equity curve."""
    returns = {}
    for previous, current in zip(daily_curve, daily_curve[1:]):
        if previous["equity"] > 0:
            returns[str(current["date"])] = current["equity"] / previous["equity"] - 1.0
    return returns


def _bar_returns(bars: list[dict], start: date, end: date) -> dict[str, float]:
    """Benchmark per-session returns from daily bars ({"t": ISO timestamp, "c": close})."""
    closes = {}
    for bar in bars:
        stamp = str(bar.get("t") or bar.get("date") or "")[:10]
        close = bar.get("c", bar.get("close"))
        if stamp and close is not None and start.isoformat() <= stamp <= end.isoformat():
            closes[stamp] = float(close)
    days = sorted(closes)
    return {
        current: closes[current] / closes[previous] - 1.0
        for previous, current in zip(days, days[1:]) if closes[previous] > 0
    }


def _discovery_candidate_ids(strategy: str, status: dict) -> set[str]:
    """Every non-baseline parameter set a discovery evaluated, across its window attempts."""
    artifact = status.get("detail_artifact")
    if not artifact:
        return set()
    try:
        detail = _read_json(Path(artifact))
    except ValueError:
        return set()
    paths = [str(artifact)]
    for attempt in (detail.get("adaptive_window_search") or {}).get("attempts") or []:
        if isinstance(attempt, dict) and attempt.get("detail_artifact"):
            paths.append(str(attempt["detail_artifact"]))
    identifiers: set[str] = set()
    for path in dict.fromkeys(paths):
        try:
            item = _read_json(Path(path))
        except ValueError:
            continue
        listed = item.get("evaluated_candidate_ids")
        if isinstance(listed, list):
            identifiers.update(str(value) for value in listed if value)
            continue
        # Artifacts written before evaluated_candidate_ids existed: derive it from the rows.
        for row in item.get("candidates") or []:
            if not isinstance(row, dict) or not isinstance(row.get("parameters"), dict):
                continue
            name = str(row.get("name", ""))
            if name == "baseline" or name.endswith(":baseline"):
                continue
            identifiers.add(candidate_id(strategy, row["parameters"]))
    return identifiers


def _record_trial_evidence(profile_db: Path, discovery: dict, active: dict, history: list) -> int:
    """Persist every evaluation seen so far. Insert-only, so the trial count never shrinks."""
    rows = []
    for strategy, status in (discovery.get("strategies") or {}).items():
        if not isinstance(status, dict) or status.get("status") not in {"completed", "reused"}:
            continue
        for identifier in sorted(_discovery_candidate_ids(strategy, status)):
            rows.append((f"discovery:{strategy}:{identifier}", strategy, "discovery", identifier))
    for trial in [*active.values(), *history]:
        if isinstance(trial, dict) and trial.get("trial_id") and trial.get("strategy"):
            rows.append((
                f"forward:{trial['trial_id']}", str(trial["strategy"]), "forward",
                trial.get("candidate_id"),
            ))
    return record_trial_evaluations(profile_db, rows)


def _window_split(series: dict) -> tuple[list[str], str | None]:
    """Shared sessions and the holdout start: the first two thirds search, the last third holds out."""
    days = sorted(set(series["candidate"]) & set(series["baseline"]) & set(series["benchmark"]))
    if len(days) < 3:
        return days, None
    return days, days[(len(days) * 2) // 3]


def _acceptance_decision(series: dict | None, *, trials: int, final: bool = True) -> dict:
    """Apply the acceptance rules to a forward candidate against its baseline.

    ``final=False`` applies the search-window rules only and never reads the
    holdout; it returns ``search_passed`` or ``reject``. ``final=True`` adds the
    holdout gate and is applied only to the candidate the cycle has chosen.
    """
    if not series:
        return {"decision": "reject", "reasons": ["no return series recorded"], "metrics": {}}
    days, holdout_start = _window_split(series)
    if holdout_start is None:
        return {"decision": "reject", "reasons": ["insufficient shared sessions"], "metrics": {}}
    evaluate = evaluate_candidate if final else evaluate_search
    result = evaluate(
        series["candidate"], series["baseline"], series["benchmark"],
        holdout_start=holdout_start, trials=trials,
        policy=FORWARD_ACCEPTANCE_POLICY,
    )
    return {
        "stage": "final" if final else "search",
        "decision": result.decision,
        "reasons": result.reasons,
        "metrics": result.metrics,
        "holdout_start": holdout_start,
    }


def _search_return(series: dict | None) -> float:
    """Compounded candidate return over the search sessions only. Never reads the holdout."""
    if not series:
        return 0.0
    days, holdout_start = _window_split(series)
    if holdout_start is None:
        return 0.0
    growth = 1.0
    for day in days:
        if day >= holdout_start:
            break
        growth *= 1.0 + float(series["candidate"][day])
    return growth - 1.0


def _cohort_winner(qualified: list[dict]) -> dict | None:
    """Pick the cohort winner from search-window evidence only. Holdout results never rank."""
    if not qualified:
        return None
    return max(qualified, key=lambda trial: (
        _search_return((trial.get("latest_simulation") or {}).get("return_series")),
        float(trial.get("discovery_fitness") or 0.0),
        str(trial.get("candidate_id") or ""),
    ))


def _simulate(
    *, strategy: str, regime: str, parameters: dict, start: date, end: date,
    database: str, symbols: list[str], candidate: bool,
    factor_weight_timeline: list[dict] | None = None,
) -> dict:
    from strategies.multi_factor.features import aggregate_intraday_bars

    bars, spy, regular_bars = _load_forward_window_data(
        db=database, symbols=symbols, start=start, end=end,
        adjustment=ADJUSTMENT, strategy=strategy,
    )
    # Daily swing signals retain the regular-session close history. The extra
    # sessions participate in execution, stop checks, and intraday signals.
    daily = aggregate_intraday_bars(regular_bars)
    current_regimes = deepcopy(REGIME_PARAMETER_PROFILES.get(strategy) or {})
    if candidate:
        current_regimes[regime] = parameters
    args = SimpleNamespace(
        adjustment=ADJUSTMENT,
        capital=CAPITAL,
        slippage_bps=FORWARD_COST_MODEL["slippage_bps_each_side"],
        spread_bps=FORWARD_COST_MODEL["spread_bps_full"],
        commission_per_share=FORWARD_COST_MODEL["commission_per_share_each_side"],
        short_borrow_annual_pct=FORWARD_COST_MODEL["short_borrow_annual_pct"],
        unknown_borrow_penalty_bps=FORWARD_COST_MODEL["unknown_borrow_penalty_bps"],
        borrow_availability_data={},
        factor_weight_timeline=factor_weight_timeline or [],
    )
    report = _run_replay(
        strategy=strategy,
        parameters=ALGO_DEFAULT_PARAMETERS[strategy],
        symbols=symbols, bars=bars, spy_daily=spy, start=start, end=end,
        args=args, regime_profiles={strategy: current_regimes},
        force_close_at_end=False,
        preaggregated_daily_bars=daily,
        include_extended_hours=True,
    )
    report["benchmark_returns"] = _bar_returns(spy, start, end)
    return report


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
    traded_notional = sum(
        abs(float(row.get("qty") or 0.0) * float(row.get("entry_price") or 0.0))
        for row in trades
    )
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
        "turnover_pct_of_initial_capital": round(100.0 * traded_notional / CAPITAL, 4),
        "marked_pnl_after_estimated_exit_costs": marked_pnl,
        "entry_sessions": sorted(entry_sessions),
        "exit_sessions": sorted(exit_sessions),
        "overall_net_return_pct": report.get("net_return_pct"),
        "overall_max_drawdown_pct": report.get("max_drawdown_pct"),
        "overall_open_position_count": report.get("open_position_count"),
        "overall_closed_trade_count": report.get("closed_trade_count"),
        "closed_trades_by_entry_regime": report.get("closed_trades_by_entry_regime", {}),
    }


def _baseline_profile_fingerprint(strategy: str) -> str:
    payload = {
        "parameters": ALGO_DEFAULT_PARAMETERS[strategy],
        "regime_profiles": REGIME_PARAMETER_PROFILES.get(strategy) or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _fitness_summary(simulation: dict) -> dict:
    """Forward evidence behind a promotion, without the replay return series."""
    candidate = simulation.get("candidate_metrics") or {}
    baseline = simulation.get("baseline_metrics") or {}
    acceptance = simulation.get("acceptance") or {}
    acceptance_metrics = acceptance.get("metrics") or {}
    return {
        "net_realized_pnl_after_simulated_costs": candidate.get("net_realized_pnl_after_simulated_costs"),
        "marked_pnl_after_estimated_exit_costs": candidate.get("marked_pnl_after_estimated_exit_costs"),
        "baseline_net_realized_pnl_after_simulated_costs": baseline.get("net_realized_pnl_after_simulated_costs"),
        "baseline_marked_pnl_after_estimated_exit_costs": baseline.get("marked_pnl_after_estimated_exit_costs"),
        "forward_pnl_improvement": simulation.get("forward_pnl_improvement"),
        "marked_equity_improvement": simulation.get("marked_equity_improvement"),
        "closed_trades": candidate.get("closed_trades"),
        "distinct_exit_sessions": candidate.get("distinct_exit_sessions"),
        "target_regime_entry_sessions": candidate.get("target_regime_entry_sessions"),
        "t_stat": acceptance_metrics.get("t_stat"),
        "deflated_sharpe": acceptance_metrics.get("deflated_sharpe"),
        "min_deflated_sharpe": acceptance_metrics.get("min_deflated_sharpe"),
        "search_decision": (simulation.get("search_gate") or {}).get("decision"),
        "acceptance_decision": acceptance.get("decision"),
        "acceptance_reasons": acceptance.get("reasons") or [],
        "deflation_trial_count": simulation.get("deflation_trial_count"),
        "simulation_start": simulation.get("simulation_start"),
        "data_end": simulation.get("data_end"),
        "cost_model_version": simulation.get("cost_model_version"),
    }


def _promote(trial: dict, metrics: dict, promoted: dict, store: ProfileStore) -> dict | None:
    """Stage a promotion in the profile store and project it into ``promoted``.

    Returns None when the candidate was already promoted, so nothing changes.
    The caller activates the staged version only after the JSON is written.
    """
    strategy, regime = trial["strategy"], trial["regime"]
    release_id = f"forward-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    record = {
        "trial_id": trial.get("trial_id"),
        "candidate_id": trial["candidate_id"],
        "strategy": strategy,
        "regime": regime,
        "model_family": trial.get("model_family"),
        "logic_version": trial.get("logic_version"),
        "parent_candidate_id": trial.get("parent_candidate_id") or trial.get("source_candidate_id"),
        "cohort_id": trial.get("cohort_id"),
        "discovery_cost_model_version": trial.get("cost_model_version"),
        "forward_cost_model_version": trial.get("forward_cost_model_version") or (trial.get("latest_simulation") or {}).get("cost_model_version"),
        "parameters": trial["parameters"],
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "forward_metrics": metrics,
        "historical_verification": trial.get("historical_verification"),
        "discovery_artifact": trial.get("discovery_artifact"),
        "release_id": release_id,
    }
    version_id = store.stage_promotion(
        strategy=strategy, regime=regime, candidate_id=trial["candidate_id"],
        parameters=trial["parameters"], release_id=release_id,
        fitness=_fitness_summary(metrics),
        metadata={
            "trial_id": trial.get("trial_id"),
            "parent_candidate_id": record["parent_candidate_id"],
            "cohort_id": trial.get("cohort_id"),
            "model_family": trial.get("model_family"),
            "logic_version": trial.get("logic_version"),
            "discovery_artifact": trial.get("discovery_artifact"),
        },
        replaced_parameters=((promoted.get("regime_profiles") or {}).get(strategy) or {}).get(regime),
    )
    if version_id is None:
        return None
    project_live(promoted, strategy, regime, trial["parameters"], trial["candidate_id"], release_id)
    record["version_id"] = version_id
    return record


def _select_promotions(
    queue: list[dict], *, max_per_cycle: int, max_per_strategy: int,
) -> list[dict]:
    """Choose which queued promotions may write this cycle. Marks the rest ``skipped``.

    Ranking uses search-window return only, so the cap cannot select on holdout
    results. Ties break on candidate ID. Skipped candidates carry a reason and
    are not written anywhere.
    """
    ranked = sorted(queue, key=lambda trial: (
        -_search_return((trial.get("latest_simulation") or {}).get("return_series")),
        str(trial.get("candidate_id") or ""),
    ))
    per_strategy: dict[str, int] = {}
    selected: list[dict] = []
    for trial in ranked:
        strategy = trial["strategy"]
        if len(selected) >= max_per_cycle:
            reason = f"Promotion skipped: cycle cap of {max_per_cycle} promotion(s) reached"
        elif per_strategy.get(strategy, 0) >= max_per_strategy:
            reason = f"Promotion skipped: per-strategy cap of {max_per_strategy} reached for {strategy}"
        else:
            per_strategy[strategy] = per_strategy.get(strategy, 0) + 1
            selected.append(trial)
            continue
        trial["status"] = "skipped"
        trial["reason"] = reason
    return selected


def run_forward_validation_cycle(
    *, discovery: dict, verification: dict, store, database: str,
    symbols: list[str], end: date, data_version: str, allow_stage: bool = True,
    allow_promotion: bool = True, promotion_pause_reasons: list[str] | None = None,
    invocation_id: str | None = None, ledger_path: str | Path | None = None,
    factor_weight_timeline: list[dict] | None = None,
    max_promotions_per_cycle: int = MAX_PROMOTIONS_PER_CYCLE,
    max_promotions_per_strategy_per_cycle: int = MAX_PROMOTIONS_PER_STRATEGY_PER_CYCLE,
) -> dict:
    """Replay each immutable candidate version only after its staging date.

    Search-window results select candidates. The holdout gate runs once, for the
    chosen candidate only, and promotions are capped before any write.
    """
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    pause_reasons = list(promotion_pause_reasons or [])
    if not allow_promotion and not pause_reasons:
        pause_reasons.append("drift")
    # Profile versions and trial counts live in the research database that the ledger uses.
    profile_db = Path(ledger_path) if ledger_path else PROMOTED_PROFILE_PATH.with_name("research.sqlite3")
    with _locked(FORWARD_VALIDATION_PATH):
        try:
            state = _read_json(FORWARD_VALIDATION_PATH, {
                "schema": "swarm_algo_forward_validation_v1",
                "active_trials": {}, "history": [],
            })
            refresh_active_parameter_profile()
            promoted = _read_json(PROMOTED_PROFILE_PATH)
            store = ProfileStore(profile_db, promoted_path=PROMOTED_PROFILE_PATH, lock_path=FORWARD_VALIDATION_PATH)
            # Finish or abandon promotions a previous crash left pending before anything new is staged.
            store.reconcile(promoted)
            active = state.setdefault("active_trials", {})
            history = state.setdefault("history", [])
            if not isinstance(active, dict) or not isinstance(history, list):
                raise ValueError("Forward validation record has invalid active_trials or history")
            for trial in [*active.values(), *history]:
                if isinstance(trial, dict):
                    trial.setdefault("trial_id", uuid4().hex)
            _record_trial_evidence(profile_db, discovery, active, history)
            held_pairs = active_holdout_pairs(profile_db)
            data_version = _forward_session_data_version(
                database, data_version, symbols, end,
            )
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
        observed_trial_ids = set()
        promotion_queue: list[dict] = []
        skipped_promotions: list[str] = []
        for key, trial in list(active.items()):
            if not isinstance(trial, dict) or trial.get("status") not in {"validating", "cohort_ready"}:
                continue
            trial["forward_cost_model_version"] = FORWARD_COST_MODEL_VERSION
            try:
                staged = date.fromisoformat(trial["staged_on"])
                age_days = (end - staged).days
            except (KeyError, TypeError, ValueError):
                continue
            if age_days > MAX_TRIAL_DAYS and trial.get("status") == "validating":
                trial.update({
                    "status": "expired", "finished_at": now.isoformat(),
                    "reason": "Forward simulation exceeded its maximum duration",
                })
                history.append(trial)
                active.pop(key, None)
                expired.append(trial["candidate_id"])
                continue
            if age_days > MAX_TRIAL_DAYS and trial.get("status") == "cohort_ready":
                observed_trial_ids.add(trial["trial_id"])
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
                observed_trial_ids.add(trial["trial_id"])
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
                previous["reused"] = True
            else:
                try:
                    baseline_report = _simulate(
                        strategy=trial["strategy"], regime=trial["regime"],
                        parameters=trial["parameters"], start=simulation_start, end=end,
                        database=database, symbols=symbols, candidate=False,
                        factor_weight_timeline=factor_weight_timeline,
                    )
                    candidate_report = _simulate(
                        strategy=trial["strategy"], regime=trial["regime"],
                        parameters=trial["parameters"], start=simulation_start, end=end,
                        database=database, symbols=symbols, candidate=True,
                        factor_weight_timeline=factor_weight_timeline,
                    )
                    baseline_metrics = _target_metrics(
                        baseline_report, trial["strategy"], trial["regime"],
                    )
                    candidate_metrics = _target_metrics(
                        candidate_report, trial["strategy"], trial["regime"],
                    )
                    trial["latest_simulation"] = {
                        "status": "completed", "simulation_policy": SIMULATION_POLICY,
                        "return_series": {
                            "candidate": _equity_returns(candidate_report.get("daily_equity", [])),
                            "baseline": _equity_returns(baseline_report.get("daily_equity", [])),
                            "benchmark": candidate_report.get("benchmark_returns", {}),
                        },
                        "data_end": end.isoformat(),
                        "data_version": data_version,
                        "cost_model_version": FORWARD_COST_MODEL_VERSION,
                        "baseline_profile_sha256": baseline_profile_sha256,
                        "simulation_start": simulation_start.isoformat(),
                        "candidate_metrics": candidate_metrics,
                        "baseline_metrics": baseline_metrics,
                        "market_session_coverage": {
                            "candidate": candidate_report.get("session_source_bar_counts", {}),
                            "baseline": baseline_report.get("session_source_bar_counts", {}),
                        },
                        "reused": False,
                    }
                except Exception as exc:
                    trial["latest_simulation"] = {
                        "status": "error", "simulation_policy": SIMULATION_POLICY,
                        "data_end": end.isoformat(),
                        "data_version": data_version,
                        "cost_model_version": FORWARD_COST_MODEL_VERSION,
                        "baseline_profile_sha256": baseline_profile_sha256,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    simulation_errors.append({
                        "candidate_id": trial.get("candidate_id"),
                        "error": trial["latest_simulation"]["error"],
                    })
                    observed_trial_ids.add(trial["trial_id"])
                    continue

            observed_trial_ids.add(trial["trial_id"])

            sample_met = bool(
                candidate_metrics["closed_trades"] >= MIN_CLOSED_TRADES
                and candidate_metrics["distinct_exit_sessions"] >= MIN_EXIT_SESSIONS
                and candidate_metrics["target_regime_entry_sessions"] >= MIN_REGIME_ENTRY_SESSIONS
            )
            trial["latest_simulation"]["minimum_sample_met"] = sample_met
            if not sample_met:
                if trial.get("cohort_id"):
                    trial["status"] = "validating"
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
            legacy_pass = bool(
                candidate_pnl > 0 and forward_improvement and marked_equity_improvement
            )
            trial["latest_simulation"]["legacy_gate_pass"] = legacy_pass
            # Every evaluation for this strategy, across regimes, from the database.
            trial_count = max(1, cumulative_trial_count(profile_db, trial["strategy"]))
            trial["latest_simulation"]["deflation_trial_count"] = trial_count
            # Search-window gate first. The holdout is not read here.
            search_gate = _acceptance_decision(
                trial["latest_simulation"].get("return_series"), trials=trial_count, final=False,
            )
            trial["latest_simulation"]["search_gate"] = search_gate
            trial["latest_simulation"]["acceptance"] = search_gate
            trial["search_passed"] = search_gate["decision"] == "search_passed"
            trial["forward_validation_pass"] = False
            if trial.get("cohort_id"):
                trial["status"] = "cohort_ready"
                trial["finished_at"] = now.isoformat()
                continue
            if not trial["search_passed"]:
                trial["status"] = "rejected"
                trial["reason"] = "Forward search gate failed: " + "; ".join(search_gate["reasons"])
                history.append(trial)
                rejected.append(trial["candidate_id"])
                active.pop(key, None)
                continue
            if not allow_promotion or (trial["strategy"], trial["regime"]) in held_pairs:
                trial["status"] = "cohort_ready"
                trial["promotion_paused"] = True
                trial["reason"] = (
                    "Promotion paused by " + ", ".join(pause_reasons) if not allow_promotion
                    else f"Promotion paused by final holdout for {trial['strategy']}:{trial['regime']}"
                )
                continue
            # Only the candidate the cycle has chosen reaches the holdout gate.
            final = _acceptance_decision(
                trial["latest_simulation"].get("return_series"), trials=trial_count, final=True,
            )
            trial["latest_simulation"]["acceptance"] = final
            trial["forward_validation_pass"] = final["decision"] == "accept"
            if not trial["forward_validation_pass"]:
                trial["status"] = "rejected"
                trial["reason"] = "Forward holdout gate failed: " + "; ".join(final["reasons"])
                history.append(trial)
                rejected.append(trial["candidate_id"])
                active.pop(key, None)
                continue
            trial["status"] = "promotion_pending"
            promotion_queue.append(trial)
            active.pop(key, None)

        # Candidates staged together share the same live baseline and future
        # window. Select at most one winner after every member has either
        # completed its sample or expired, so iteration order cannot decide the
        # regime profile. The winner is ranked on search-window results only.
        cohorts: dict[str, list[tuple[str, dict]]] = {}
        for key, trial in active.items():
            if isinstance(trial, dict) and trial.get("cohort_id"):
                cohorts.setdefault(str(trial["cohort_id"]), []).append((key, trial))
        for cohort_id, members in (cohorts.items() if allow_promotion else []):
            expected = max(int(trial.get("cohort_size") or len(members)) for _, trial in members)
            terminal_members = [
                trial for trial in history
                if isinstance(trial, dict) and trial.get("cohort_id") == cohort_id
            ]
            if len(members) + len(terminal_members) < expected:
                continue
            if any(trial.get("status") == "validating" for _, trial in members):
                continue
            if (members[0][1]["strategy"], members[0][1]["regime"]) in held_pairs:
                continue
            ready = [trial for _, trial in members if trial.get("status") == "cohort_ready"]
            qualified = [trial for trial in ready if trial.get("search_passed")]
            winner = _cohort_winner(qualified)
            winner_note = "Another candidate in the completed ablation cohort had stronger search-window results"
            if winner is not None:
                winner_count = max(1, cumulative_trial_count(profile_db, winner["strategy"]))
                final = _acceptance_decision(
                    winner["latest_simulation"].get("return_series"), trials=winner_count, final=True,
                )
                winner["latest_simulation"]["acceptance"] = final
                winner["forward_validation_pass"] = final["decision"] == "accept"
                if not winner["forward_validation_pass"]:
                    winner_note = "The cohort's search-window winner did not pass the holdout gate: " + "; ".join(final["reasons"])
                    winner = None
            for key, trial in sorted(members, key=lambda pair: str(pair[1].get("candidate_id") or "")):
                if trial.get("status") != "cohort_ready":
                    continue
                if trial is winner:
                    trial["status"] = "promotion_pending"
                    promotion_queue.append(trial)
                elif trial.get("search_passed"):
                    trial["status"] = "qualified_not_selected"
                    trial["reason"] = winner_note
                    history.append(trial)
                else:
                    trial["status"] = "rejected"
                    trial["reason"] = "Forward search gate failed: " + "; ".join(
                        (trial.get("latest_simulation") or {}).get("search_gate", {}).get("reasons") or ["not evaluated"],
                    )
                    rejected.append(trial["candidate_id"])
                    history.append(trial)
                active.pop(key, None)

        # History promotion. A candidate that passes exact rolling out-of-sample
        # validation is queued. It is promoted only if, on the next untouched
        # 42-session holdout, it beats the live profile on net and marked P&L.
        # Its forward trial keeps running as monitoring; the weekly review decides keep or demote.
        if HISTORY_PROMOTION_ENABLED and allow_promotion and regime and not pause_reasons:
            try:
                history_candidates = _candidates(discovery, verification, regime)
            except Exception as exc:
                history_candidates = []
                staging_errors.append(f"history promotion: {type(exc).__name__}: {exc}")
            for candidate in sorted(history_candidates, key=lambda row: str(row["candidate_id"])):
                screening = candidate.get("screening") or {}
                exact = (candidate.get("historical_verification") or {}).get("exact_candidate_validation") or {}
                if not (
                    exact.get("status") == "passed"
                    and screening.get("improved_over_training_baseline")
                    and screening.get("historical_sample_met")
                    and screening.get("historical_positive_after_costs")
                ):
                    continue
                if (candidate["strategy"], regime) in held_pairs:
                    continue
                history_holdout.enqueue(profile_db, candidate=candidate, staged_end=end)

            def _holdout_simulate(item: dict, start: date, block_end: date) -> dict:
                live = _simulate(
                    strategy=item["strategy"], regime=item["regime"], parameters=item["parameters"],
                    start=start, end=block_end, database=database, symbols=symbols, candidate=False,
                    factor_weight_timeline=factor_weight_timeline,
                )
                challenger = _simulate(
                    strategy=item["strategy"], regime=item["regime"], parameters=item["parameters"],
                    start=start, end=block_end, database=database, symbols=symbols, candidate=True,
                    factor_weight_timeline=factor_weight_timeline,
                )
                return {
                    "candidate": _target_metrics(challenger, item["strategy"], item["regime"]),
                    "baseline": _target_metrics(live, item["strategy"], item["regime"]),
                }

            try:
                decided = history_holdout.evaluate(
                    profile_db,
                    sessions_after=lambda start: _trading_sessions(database, end, start=start, adjustment=ADJUSTMENT),
                    end=end,
                    simulate=_holdout_simulate,
                )
            except Exception as exc:
                decided = []
                staging_errors.append(f"history holdout: {type(exc).__name__}: {exc}")
            for item in decided:
                if item["status"] != "promoted":
                    continue
                evidence = item["evidence"]
                promotion_queue.append({
                    "trial_id": f"history-{item['candidate_id']}",
                    "candidate_id": item["candidate_id"],
                    "strategy": item["strategy"],
                    "regime": item["regime"],
                    "parameters": item["parameters"],
                    "promotion_basis": "historical_oos_then_holdout",
                    "historical_verification": {"holdout": {k: evidence[k] for k in (
                        "holdout_start", "holdout_end", "holdout_sessions",
                        "candidate_net_pnl", "live_net_pnl", "candidate_marked_pnl", "live_marked_pnl",
                    )}},
                    "status": "promotion_pending",
                    "latest_simulation": {
                        "status": "completed",
                        "candidate_metrics": evidence["candidate_metrics"],
                        "baseline_metrics": evidence["baseline_metrics"],
                        "forward_pnl_improvement": round(evidence["candidate_net_pnl"] - evidence["live_net_pnl"], 2),
                        "marked_equity_improvement": True,
                        "legacy_gate_pass": True,
                        "return_series": None,
                        "acceptance": {"decision": "holdout_passed", "reasons": []},
                        "data_end": evidence["holdout_end"],
                        "simulation_start": evidence["holdout_start"],
                    },
                })

        # Cap promotions before any write.
        selected_for_promotion = _select_promotions(
            promotion_queue, max_per_cycle=max_promotions_per_cycle,
            max_per_strategy=max_promotions_per_strategy_per_cycle,
        )
        for trial in promotion_queue:
            if trial["status"] == "skipped":
                history.append(trial)
                skipped_promotions.append(trial["candidate_id"])

        for trial in selected_for_promotion:
            record = _promote(trial, trial["latest_simulation"], promoted, store)
            if record is not None:
                promoted_rows.append(record)
            else:
                trial["reason"] = "Candidate was already promoted; no new profile version written"
            trial["status"] = "promoted"
            history.append(trial)

        staged_ids = []
        if allow_stage and regime:
            try:
                candidates = _candidates(discovery, verification, regime)
            except Exception as exc:
                candidates = []
                staging_errors.append(f"{type(exc).__name__}: {exc}")
            active_pairs = {
                (trial.get("strategy"), trial.get("regime"))
                for trial in active.values()
                if isinstance(trial, dict) and trial.get("status") in {"validating", "cohort_ready"}
            }
            active_candidate_ids = {
                trial.get("candidate_id") for trial in active.values()
                if isinstance(trial, dict)
            }
            live_ids = promoted.get("regime_profile_ids") or {}
            by_strategy: dict[str, list[dict]] = {}
            for candidate in candidates:
                by_strategy.setdefault(candidate["strategy"], []).append(candidate)
            for strategy, rows in by_strategy.items():
                if (strategy, regime) in active_pairs:
                    continue
                already_terminal = {
                    row.get("candidate_id") for row in history
                    if isinstance(row, dict)
                    and row.get("status") in {"rejected", "expired", "promoted", "qualified_not_selected"}
                }
                cohort = [
                    candidate for candidate in rows
                    if candidate.get("candidate_id") not in active_candidate_ids
                    and candidate.get("candidate_id") not in already_terminal
                    and (live_ids.get(strategy) or {}).get(regime) != candidate.get("candidate_id")
                ][:MAX_ACTIVE_COHORT_PER_STRATEGY_REGIME]
                if not cohort:
                    continue
                cohort_candidate_ids = sorted(candidate["candidate_id"] for candidate in cohort)
                cohort_payload = json.dumps(
                    {"strategy": strategy, "regime": regime, "staged_on": end.isoformat(),
                     "candidate_ids": cohort_candidate_ids},
                    sort_keys=True, separators=(",", ":"),
                )
                cohort_id = hashlib.sha256(cohort_payload.encode()).hexdigest()[:20]
                for candidate in cohort:
                    candidate.update({
                        "trial_id": uuid4().hex,
                        "origin_invocation_id": invocation_id,
                        "source_candidate_id": candidate.get("source_candidate_id"),
                        "baseline_profile_sha256": _baseline_profile_fingerprint(strategy),
                        "cohort_id": cohort_id,
                        "cohort_size": len(cohort),
                        "cohort_candidate_ids": cohort_candidate_ids,
                        "forward_cost_model_version": FORWARD_COST_MODEL_VERSION,
                        "status": "validating", "staged_on": end.isoformat(),
                        "staged_at": now.isoformat(), "latest_simulation": None,
                    })
                    key = f"{strategy}:{regime}:{candidate['candidate_id']}"
                    active[key] = candidate
                    active_candidate_ids.add(candidate["candidate_id"])
                    staged_ids.append(candidate["candidate_id"])

        # Record staged candidates too, so a later count includes them.
        _record_trial_evidence(profile_db, discovery, active, history)

        if promoted_rows:
            # Protocol: pending rows are already staged; project the JSON, then activate in the database.
            write_json_atomic(PROMOTED_PROFILE_PATH, promoted)
            store.activate([row["version_id"] for row in promoted_rows])
        held_labels = sorted(f"{strategy}:{regime}" for strategy, regime in held_pairs)
        cap = {
            "max_promotions_per_cycle": max_promotions_per_cycle,
            "max_promotions_per_strategy_per_cycle": max_promotions_per_strategy_per_cycle,
        }
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
            "promotion_paused_by_drift": "drift" in pause_reasons,
            "promotion_paused_by_final_holdout": bool(held_labels),
            "final_holdout_paused_pairs": held_labels,
            "promotion_pause_reasons": pause_reasons,
            "promotion_cap": cap,
            "simulation_policy": SIMULATION_POLICY,
            "cost_model": {
                **FORWARD_COST_MODEL,
                "cost_model_version": FORWARD_COST_MODEL_VERSION,
            },
            "acceptance_policy": {
                "min_deflated_sharpe": FORWARD_ACCEPTANCE_POLICY.min_deflated_sharpe,
                "min_oos_sessions": FORWARD_ACCEPTANCE_POLICY.min_oos_sessions,
                "holdout": "last third of the forward window; final gate for the chosen candidate only",
            },
            "last_cycle": {
                "data_end": end.isoformat(), "data_version": data_version,
                "promoted_candidate_ids": [row["candidate_id"] for row in promoted_rows],
                "promotion_skipped_candidate_ids": skipped_promotions,
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

    if invocation_id and ledger_path:
        from ..artifacts.research_ledger import ResearchLedger

        ResearchLedger(ledger_path).record_forward_cycle(
            invocation_id, active_trials=active, terminal_trials=history,
            promoted_rows=promoted_rows, data_version=data_version,
            data_end=end.isoformat(), observed_trial_ids=observed_trial_ids,
        )

    return {
        "status": "partial_error" if simulation_errors or staging_errors else "completed",
        "current_regime": regime,
        "active_trial_count": len(active),
        "staged_candidate_ids": staged_ids,
        "promoted": promoted_rows,
        "rejected_candidate_ids": rejected,
        "expired_candidate_ids": expired,
        "promotion_skipped_candidate_ids": skipped_promotions,
        "simulation_errors": simulation_errors,
        "staging_errors": staging_errors,
        "minimum_sample": {
            "closed_trades": MIN_CLOSED_TRADES,
            "distinct_exit_sessions": MIN_EXIT_SESSIONS,
            "target_regime_entry_sessions": MIN_REGIME_ENTRY_SESSIONS,
        },
        "live_configuration_changed": bool(promoted_rows),
        "promotion_paused_by_drift": "drift" in pause_reasons,
        "promotion_paused_by_final_holdout": bool(held_labels),
        "final_holdout_paused_pairs": held_labels,
        "promotion_pause_reasons": pause_reasons,
        "promotion_cap": cap,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


__all__ = ["run_forward_validation_cycle"]
