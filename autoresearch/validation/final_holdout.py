"""Prospective holdouts for research cohorts and the legacy frozen-profile scorer.

Cohort holdouts: one active holdout per (strategy, regime) is frozen by
``initialize``. Each row records its window (sessions after the freeze date),
the live profile versions it froze, and a SHA-256 of those frozen parameters.
While a cohort holdout is active, promotions for that pair are paused. Nothing
in the nightly run supersedes a holdout; only the CLI does, and every
supersede writes an event:

    python -m autoresearch.validation.final_holdout initialize [--frozen-on DATE]
    python -m autoresearch.validation.final_holdout status
    python -m autoresearch.validation.final_holdout supersede --reason "why" [--strategy S --regime R]

The JSON functions below (``initialize_final_holdout``, ``advance_final_holdout``)
are the earlier whole-profile scorer. Nothing calls them now; the manifest is
read only for audit.
"""

from __future__ import annotations
from autoresearch.costs import EQUITY_COMMISSION_PER_SHARE

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import argparse
import hashlib
import json
import statistics
import sqlite3
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from autoresearch.backtest import (
    DAILY_WARMUP_DAYS, AlgoPortfolioBacktest, multi_factor_warmup_days,
    read_borrow_availability, strategy_warmup_days,
)
from autoresearch.backtest.config import REGIME_WARMUP_DAYS
from portfolio.config import get_algo_regime_config, get_mode_config
from market.store import SQLiteBarStore
from strategies.portfolio_coordinator import PortfolioCoordinatorConfig
from strategies.regime import (
    REGIME_ENGINE_VERSION, _apply_hysteresis, _raw_rule_states,
)
from strategies.regime_features import compute_regime_feature_history
from strategies.multi_factor.features import aggregate_intraday_bars
from portfolio.config import get_algo_runtime_settings

from portfolio.profile_store import RESEARCH_ROOT
from ..artifacts.files import _atomic_json
from ..artifacts.live_manifest import live_configuration_manifest
from ..profile_versions import ACTIVE, BASE_REGIME, ProfileStore
from .robustness import bootstrap_daily_equity, bootstrap_trade_sequence
from ..settings import (
    ADJUSTMENT, FINAL_HOLDOUT_MIN_REGIMES, FINAL_HOLDOUT_MIN_SESSIONS, FEED,
    LIVE_RESEARCH_STRATEGIES, REGIME_CONTEXT_WARMUP_DAYS,
)


ET = ZoneInfo("America/New_York")
FINAL_HOLDOUT_SCHEMA = "swarm_algo_final_holdout_v1"
FINAL_HOLDOUT_FILENAME = "final-holdout.json"
HOLDOUT_REGIMES = ("trending_up", "trending_down", "range_bound", "volatile")
POLICY_KEYS = (
    "entry_limits", "runtime", "coordinator", "risk", "research_controls",
    "strategy_defaults", "regime_profiles", "regime_profile_ids",
)
_COHORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS final_holdout (
    holdout_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    frozen_on TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT,
    parameters_sha256 TEXT NOT NULL,
    frozen_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    superseded_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS final_holdout_one_active
    ON final_holdout(strategy, regime) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS final_holdout_event (
    event_id TEXT PRIMARY KEY,
    holdout_id TEXT NOT NULL REFERENCES final_holdout(holdout_id),
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('initialize', 'supersede')),
    reason TEXT,
    actor TEXT,
    created_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
"""


def read_final_holdout_state(output_dir: str | Path) -> dict | None:
    path = Path(output_dir) / FINAL_HOLDOUT_FILENAME
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read final holdout manifest {path}: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or value.get("schema") != FINAL_HOLDOUT_SCHEMA:
        raise ValueError(f"unsupported or malformed final holdout manifest: {path}")
    return value


def advance_final_holdout(
    *, database: str, latest_end: date, output_dir: str | Path,
) -> dict:
    """Score the immutable profile once its prospective data and regime gates mature."""
    state = read_final_holdout_state(output_dir)
    if not state:
        return {"status": "not_initialized", "research_only": True, "holdout_quarantining": False}
    if state.get("status") != "collecting":
        return _public_status(state)

    frozen_end = date.fromisoformat(state["frozen_data_end"])
    frozen_database = state["frozen_profile"]["database"]
    if Path(database).resolve() != Path(frozen_database).resolve():
        state.update({
            "status": "invalidated",
            "invalidated_at": datetime.now(timezone.utc).isoformat(),
            "invalidation_reason": "market database path changed after freeze",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_state(output_dir, state)
        return _public_status(state)
    if latest_end <= frozen_end:
        return _public_status(state)

    symbols = list(state["frozen_profile"]["symbols"])
    store = SQLiteBarStore(database)
    regime_history_start = frozen_end - timedelta(days=REGIME_WARMUP_DAYS)
    regime_context_start = frozen_end - timedelta(days=REGIME_CONTEXT_WARMUP_DAYS)
    spy = store.bars(
        ["SPY"], feed=state["frozen_profile"]["feed"], timeframe="1Day",
        start=regime_history_start.isoformat(),
        end=latest_end.isoformat(), adjustment=state["frozen_profile"]["adjustment"],
    ).get("SPY", [])
    context_symbols = list(dict.fromkeys(
        [*symbols, "SPY", "QQQ", "GLD", "TLT", "HYG", "LQD"]
    ))
    regime_intraday = store.bars(
        context_symbols, feed=state["frozen_profile"]["feed"], timeframe="5Min",
        start=regime_context_start.isoformat(), end=latest_end.isoformat(),
        adjustment=state["frozen_profile"]["adjustment"],
    )
    context_daily = aggregate_intraday_bars(regime_intraday)
    context_daily.setdefault("SPY", spy)
    from market.regime import load_fred_regime_series

    macro_series, _macro_diagnostics = load_fred_regime_series(store, refresh=False)
    regime_feature_set = str(get_algo_regime_config().get("feature_set", "core"))
    feature_rows = compute_regime_feature_history(
        spy, context_bars=context_daily, macro_series=macro_series,
    )
    raw_states, _ = _raw_rule_states(feature_rows, regime_feature_set)
    regimes = _apply_hysteresis(raw_states)
    holdout_dates = {
        str(row.get("date", ""))[:10]
        for row in feature_rows
        if str(row.get("date", ""))[:10] > frozen_end.isoformat()
    }
    date_to_regime = {
        str(row.get("date")): regime
        for row, regime in zip(feature_rows, regimes)
    }
    regime_counts: dict[str, int] = {}
    for day in sorted(holdout_dates):
        regime = date_to_regime.get(day, "unknown")
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
    recognized = {
        name: count for name, count in regime_counts.items()
        if name in {"trending_up", "trending_down", "range_bound", "volatile"}
    }
    qualification = {
        "holdout_session_count": len(holdout_dates),
        "minimum_sessions": FINAL_HOLDOUT_MIN_SESSIONS,
        "rule_regime_counts": recognized,
        "distinct_rule_regimes": len(recognized),
        "minimum_distinct_regimes": FINAL_HOLDOUT_MIN_REGIMES,
        "latest_available_session": latest_end.isoformat(),
    }
    state["qualification"] = qualification
    if (
        qualification["holdout_session_count"] < FINAL_HOLDOUT_MIN_SESSIONS
        or qualification["distinct_rule_regimes"] < FINAL_HOLDOUT_MIN_REGIMES
    ):
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(output_dir, state)
        return _public_status(state)

    current_manifest = live_configuration_manifest(state["frozen_profile"]["strategies"])
    current_policy_hash = _policy_fingerprint(current_manifest["snapshot"])
    current_regime_hash = _regime_policy_hash()
    if (
        current_policy_hash != state["frozen_profile"]["policy_fingerprint"]
        or current_regime_hash != state["frozen_profile"]["regime_config_sha256"]
    ):
        state.update({
            "status": "invalidated",
            "invalidated_at": datetime.now(timezone.utc).isoformat(),
            "invalidation_reason": "live risk, coordinator, runtime, research-control, or regime policy changed after freeze",
            "current_policy_fingerprint": current_policy_hash,
            "current_regime_config_sha256": current_regime_hash,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_state(output_dir, state)
        return _public_status(state)

    try:
        replay = _run_frozen_replay(
            store=store, state=state, start=min(holdout_dates), end=latest_end,
        )
    except Exception as exc:
        state["last_scoring_error"] = f"{type(exc).__name__}: {exc}"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(output_dir, state)
        return {**_public_status(state), "scoring_error": state["last_scoring_error"]}

    replay_regimes = {}
    for row in replay.get("daily_equity") or []:
        name = str(row.get("regime") or "unknown")
        replay_regimes[name] = replay_regimes.get(name, 0) + 1
    result = {
        "status": "completed_once",
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "frozen_profile_sha256": state["frozen_profile"]["profile_sha256"],
        "frozen_replay_sha256": state["frozen_profile"]["frozen_replay_sha256"],
        "llm_driven_overlay_included": state["frozen_profile"].get("llm_driven_overlay_included", False),
        "holdout_range": {"start": min(holdout_dates), "end": latest_end.isoformat()},
        "qualification": qualification,
        "replay_regime_session_counts": replay_regimes,
        "regime_performance": _regime_performance(replay.get("daily_equity") or []),
        "metrics": {
            key: replay.get(key) for key in (
                "net_return_pct", "max_drawdown_pct", "closed_trade_count",
                "open_position_count", "entry_count", "average_entries_per_session",
                "benchmark_net_return_pct", "excess_return_vs_buy_hold_pct_points",
                "profit_factor", "sharpe_ratio", "sortino_ratio",
            ) if key in replay
        },
        "bootstrap": bootstrap_daily_equity(
            replay.get("daily_equity") or [], iterations=1000,
            block_size=5, seed=941,
        ),
        "closed_trade_sequence_bootstrap": bootstrap_trade_sequence(
            [float(row.get("pnl") or 0.0) for row in replay.get("closed_trades", [])],
            initial_capital=float(replay.get("initial_capital") or 100_000.0),
            iterations=1000, block_size=5, seed=942,
        ),
        "strategy_metrics": replay.get("strategy_metrics"),
        "transaction_cost_model": state["frozen_profile"]["cost_model"],
        "execution_delay_bars": state["frozen_profile"]["execution_delay_bars"],
        "trade_or_order_actions": 0,
        "live_configuration_changed": False,
        "research_only": True,
    }
    state.update({
        "status": "completed_once", "result": result,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _write_state(output_dir, state)
    return _public_status(state)


def initialize_final_holdout(
    *, verification: dict, database: str, symbols: list[str], frozen_data_end: date,
    output_dir: str | Path, factor_weight_timeline: list[dict] | None = None,
) -> dict:
    """Legacy whole-profile freeze; never replace an existing manifest."""
    existing = read_final_holdout_state(output_dir)
    if existing:
        return _public_status(existing)
    strategies = list(LIVE_RESEARCH_STRATEGIES)
    statuses = verification.get("strategies") or {}
    if not strategies or any(not (statuses.get(name) or {}).get("coordinator_artifact") for name in strategies):
        return {"status": "awaiting_complete_coordinator_artifacts", "research_only": True}

    profile: dict = {}
    controls = None
    cost_model = None
    artifact_hashes = {}
    for strategy in strategies:
        path = Path(statuses[strategy]["coordinator_artifact"])
        try:
            raw = path.read_bytes()
            artifact = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "invalid_coordinator_artifact", "strategy": strategy,
                "error": f"{type(exc).__name__}: {exc}", "research_only": True,
            }
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("source"), dict)
            or not isinstance(artifact.get("verification"), dict)
            or artifact.get("schema") != "swarm_algo_adaptive_coordinator_v1"
            or (artifact.get("source") or {}).get("database") != str(Path(database).resolve())
            or (artifact.get("verification") or {}).get("end") != frozen_data_end.isoformat()
        ):
            return {
                "status": "incompatible_coordinator_artifacts", "strategy": strategy,
                "reason": "schema, database, or data cutoff differs from the frozen cohort",
                "research_only": True,
            }
        artifact_hashes[strategy] = hashlib.sha256(raw).hexdigest()
        selected = artifact.get("latest_training_selection") or {}
        if not isinstance(selected, dict):
            return {"status": "awaiting_complete_coordinator_artifacts", "research_only": True}
        parameters = (selected.get("parameters") or {}).get(strategy)
        if not isinstance(parameters, dict):
            return {"status": "awaiting_complete_coordinator_artifacts", "research_only": True}
        profile[strategy] = deepcopy(parameters)
        candidate_controls = selected.get("controls") or {}
        if controls is None:
            controls = candidate_controls
        elif controls != candidate_controls:
            return {"status": "incompatible_coordinator_artifacts", "research_only": True}
        candidate_cost = artifact["verification"].get("transaction_cost_model") or {}
        if cost_model is None:
            cost_model = candidate_cost
        elif cost_model != candidate_cost:
            return {"status": "incompatible_cost_models", "research_only": True}

    runtime = get_algo_runtime_settings()
    live_manifest = live_configuration_manifest(strategies)
    manifest_snapshot = live_manifest["snapshot"]
    profile_digest = hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":"), default=list).encode()
    ).hexdigest()
    frozen_factor_weight_timeline = [
        deepcopy(row) for row in (factor_weight_timeline or [])
        if isinstance(row, dict)
        and row.get("effective_date")
        and str(row.get("effective_date", ""))[:10] <= frozen_data_end.isoformat()
    ]
    frozen_profile = {
        "strategies": strategies,
        "symbols": list(dict.fromkeys(str(symbol).upper() for symbol in symbols)),
        "database": str(Path(database).resolve()),
        "feed": FEED, "adjustment": ADJUSTMENT,
        "profile": profile,
        "profile_sha256": profile_digest,
        "factor_weight_timeline": frozen_factor_weight_timeline,
        "factor_weight_timeline_sha256": _hash(frozen_factor_weight_timeline),
        "research_controls": deepcopy(controls or {}),
        "coordinator": deepcopy(manifest_snapshot["coordinator"]),
        "runtime": deepcopy(manifest_snapshot["runtime"]),
        "risk": deepcopy(get_mode_config("algo")["risk"]),
        "strategy_defaults": deepcopy(manifest_snapshot["strategy_defaults"]),
        "regime_profiles": deepcopy(manifest_snapshot["regime_profiles"]),
        "regime_config": deepcopy(get_algo_regime_config()),
        "regime_engine_version": REGIME_ENGINE_VERSION,
        "regime_config_sha256": _regime_policy_hash(),
        "live_manifest_sha256": live_manifest["sha256"],
        "policy_fingerprint": _policy_fingerprint(manifest_snapshot),
        "cost_model": deepcopy(cost_model or {}),
        "execution_delay_bars": 0,
        "source_artifact_sha256": artifact_hashes,
        "selection_source": "latest_training_selection_per_strategy",
        "selection_cutoff": frozen_data_end.isoformat(),
        "llm_driven_overlay_included": False,
    }
    frozen_profile["frozen_replay_sha256"] = _hash(frozen_profile)
    state = {
        "schema": FINAL_HOLDOUT_SCHEMA,
        "status": "collecting",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "frozen_data_end": frozen_data_end.isoformat(),
        "manifest_path": str((Path(output_dir) / FINAL_HOLDOUT_FILENAME).resolve()),
        "holdout_start_policy": "sessions strictly after frozen_data_end",
        "minimum_sessions": FINAL_HOLDOUT_MIN_SESSIONS,
        "minimum_distinct_regimes": FINAL_HOLDOUT_MIN_REGIMES,
        "frozen_profile": frozen_profile,
        "result": None,
        "research_quarantine": "discovery, rolling verification, forward trial staging, and promotion are capped at frozen_data_end until one-time scoring",
    }
    _write_state(output_dir, state)
    return _public_status(state)


def _run_frozen_replay(*, store: SQLiteBarStore, state: dict, start: str, end: date) -> dict:
    frozen = state["frozen_profile"]
    start_day = date.fromisoformat(start)
    warmup_days = max(
        REGIME_CONTEXT_WARMUP_DAYS, DAILY_WARMUP_DAYS,
        *(strategy_warmup_days(strategy, frozen["profile"]) for strategy in frozen["strategies"]),
        multi_factor_warmup_days(frozen["profile"]) if "swing_multi_factor" in frozen["strategies"] else 0,
    )
    bars_start = (start_day - timedelta(days=warmup_days)).isoformat()
    replay_symbols = list(dict.fromkeys([
        *frozen["symbols"], "QQQ", "GLD", "TLT", "HYG", "LQD",
    ]))
    intraday = store.bars(
        replay_symbols, feed=frozen["feed"], timeframe="5Min",
        start=bars_start, end=end.isoformat(), adjustment=frozen["adjustment"],
    )
    available = [symbol for symbol in frozen["symbols"] if intraday.get(symbol)]
    if not available:
        raise ValueError(f"no frozen-universe intraday bars found for holdout {start}..{end}")
    spy_daily = store.bars(
        ["SPY"], feed=frozen["feed"], timeframe="1Day",
        start=(start_day - timedelta(days=REGIME_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=frozen["adjustment"],
    ).get("SPY", [])
    from market.regime import load_fred_regime_series

    macro_series, macro_diagnostics = load_fred_regime_series(store, refresh=False)
    cost = frozen["cost_model"]
    replay = AlgoPortfolioBacktest(
        symbols=frozen["symbols"], intraday_bars=intraday, spy_daily_bars=spy_daily,
        start=start, end=end.isoformat(), feed=frozen["feed"],
        data_adjustment=frozen["adjustment"], profile=frozen["profile"],
        strategies=frozen["strategies"], research_controls=frozen["research_controls"],
        coordinator_config=PortfolioCoordinatorConfig.from_dict(frozen["coordinator"]),
        regime_profiles=frozen["regime_profiles"],
        slippage_bps=cost.get("slippage_bps_each_side", 5),
        spread_bps=cost.get("spread_bps", 3),
        commission_per_share=cost.get("commission_per_share_each_side", EQUITY_COMMISSION_PER_SHARE),
        short_borrow_annual_pct=cost.get("short_borrow_annual_pct", 3) / 100,
        unknown_borrow_penalty_bps=cost.get("unknown_borrow_adverse_fill_penalty_bps", 25),
        borrow_availability=read_borrow_availability(None),
        regime_macro_series=macro_series,
        regime_config={
            **frozen["regime_config"],
            "macro_data_status": macro_diagnostics.get("status"),
        },
        execution_delay_bars=frozen["execution_delay_bars"],
        factor_weight_timeline=frozen.get("factor_weight_timeline") or [],
    )
    return replay.run()


def _policy_fingerprint(snapshot: dict) -> str:
    return _hash({key: snapshot.get(key) for key in POLICY_KEYS})


def _regime_policy_hash() -> str:
    return _hash({"config": get_algo_regime_config(), "engine_version": REGIME_ENGINE_VERSION})


def _regime_performance(daily_equity: list[dict]) -> dict:
    """Summarize account daily returns by the regime recorded during replay."""
    grouped: dict[str, list[float]] = {}
    previous = 100_000.0
    for row in daily_equity:
        try:
            equity = float(row["equity"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if previous > 0 and equity > 0:
            grouped.setdefault(str(row.get("regime") or "unknown"), []).append(equity / previous - 1.0)
        previous = equity
    output = {}
    for regime, returns in sorted(grouped.items()):
        compounded = 1.0
        for value in returns:
            compounded *= 1.0 + value
        output[regime] = {
            "session_count": len(returns),
            "compounded_return_pct": round((compounded - 1.0) * 100, 4),
            "mean_daily_return_pct": round(statistics.mean(returns) * 100, 6),
            "positive_session_fraction": round(sum(value > 0 for value in returns) / len(returns), 6),
        }
    return output


def _hash(value: dict) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(payload.encode()).hexdigest()


def _write_state(output_dir: str | Path, state: dict) -> None:
    _atomic_json(Path(output_dir) / FINAL_HOLDOUT_FILENAME, state)


def _public_status(state: dict) -> dict:
    return {
        "status": state.get("status"),
        "research_only": True,
        "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "holdout_quarantining": state.get("status") == "collecting",
        "frozen_data_end": state.get("frozen_data_end"),
        "holdout_start_policy": state.get("holdout_start_policy"),
        "qualification": state.get("qualification"),
        "result": state.get("result"),
        "prior_status": state.get("prior_status"),
        "superseded_at": state.get("superseded_at"),
        "superseded_reason": state.get("superseded_reason"),
        "frozen_profile": {
            key: state.get("frozen_profile", {}).get(key)
            for key in (
                "strategies", "symbols", "profile_sha256", "frozen_replay_sha256",
                "factor_weight_timeline_sha256", "source_artifact_sha256",
            )
        } if state.get("frozen_profile") else None,
        "llm_driven_overlay_included": (state.get("frozen_profile") or {}).get(
            "llm_driven_overlay_included", False,
        ),
        "manifest_path": state.get("manifest_path"),
        "invalidation_reason": state.get("invalidation_reason"),
    }


# Cohort holdouts -------------------------------------------------------------

def default_research_database() -> Path:
    return RESEARCH_ROOT / "research.sqlite3"


def _cohort_connection(database: str | Path) -> sqlite3.Connection:
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(_COHORT_SCHEMA)
    return connection


def _frozen_cohort_payload(store: ProfileStore, strategy: str, regime: str) -> dict:
    """The live profile versions a cohort holdout freezes (regime version and base)."""
    active = {
        row["regime"]: row for row in store.active_versions(strategy)
        if row["status"] == ACTIVE
    }
    regime_row = active.get(regime)
    base_row = active.get(BASE_REGIME)
    return {
        "strategy": strategy, "regime": regime,
        "regime_version_id": (regime_row or {}).get("version_id"),
        "regime_candidate_id": (regime_row or {}).get("candidate_id"),
        "regime_parameters": (regime_row or {}).get("parameters"),
        "base_version_id": (base_row or {}).get("version_id"),
        "base_parameters": (base_row or {}).get("parameters"),
    }


def _pair_label(row: dict) -> str:
    return f"{row['strategy']}:{row['regime']}"


def initialize_cohort_holdouts(*, database: str | Path, frozen_on: date, actor: str = "cli") -> dict:
    """Freeze one holdout per strategy and regime cohort. Idempotent.

    A pair that already has an active holdout is left unchanged, even if its
    live profile has moved since; the frozen record is the one that counts.
    """
    payloads = {}
    store = ProfileStore(database)
    for strategy in LIVE_RESEARCH_STRATEGIES:
        for regime in HOLDOUT_REGIMES:
            payloads[(strategy, regime)] = _frozen_cohort_payload(store, strategy, regime)
    stamp = datetime.now(timezone.utc).isoformat()
    window_start = (frozen_on + timedelta(days=1)).isoformat()
    created, existing = [], []
    with closing(_cohort_connection(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for (strategy, regime), payload in payloads.items():
                row = connection.execute(
                    "SELECT holdout_id FROM final_holdout WHERE strategy=? AND regime=? AND status='active'",
                    (strategy, regime),
                ).fetchone()
                if row is not None:
                    existing.append(f"{strategy}:{regime}")
                    continue
                parameters_sha256 = _hash(payload)
                holdout_id = uuid4().hex
                connection.execute(
                    """INSERT INTO final_holdout
                       (holdout_id, strategy, regime, status, frozen_on, window_start, window_end,
                        parameters_sha256, frozen_payload_json, created_at)
                       VALUES (?, ?, ?, 'active', ?, ?, NULL, ?, ?, ?)""",
                    (holdout_id, strategy, regime, frozen_on.isoformat(), window_start,
                     parameters_sha256, json.dumps(payload, sort_keys=True, default=str), stamp),
                )
                connection.execute(
                    """INSERT INTO final_holdout_event
                       (event_id, holdout_id, strategy, regime, action, reason, actor, created_at, details_json)
                       VALUES (?, ?, ?, ?, 'initialize', 'cohort holdout frozen', ?, ?, ?)""",
                    (uuid4().hex, holdout_id, strategy, regime, actor, stamp,
                     json.dumps({"parameters_sha256": parameters_sha256, "window_start": window_start},
                                sort_keys=True)),
                )
                created.append(f"{strategy}:{regime}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return {
        "status": "initialized", "frozen_on": frozen_on.isoformat(), "window_start": window_start,
        "created": created, "already_active": existing,
        "research_only": True, "live_configuration_changed": False, "trade_or_order_actions": 0,
    }


def cohort_holdout_status(database: str | Path) -> dict:
    """Active and superseded cohort holdouts. Creates the tables if absent; never changes a row."""
    if not Path(database).exists():
        return {"active": [], "superseded_count": 0}
    with closing(_cohort_connection(database)) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM final_holdout ORDER BY strategy, regime, created_at",
        ).fetchall()]
    active = [row for row in rows if row["status"] == "active"]
    return {
        "active": active,
        "superseded_count": sum(row["status"] == "superseded" for row in rows),
    }


def active_holdout_pairs(database: str | Path) -> set[tuple[str, str]]:
    """(strategy, regime) pairs whose promotions are paused by an active holdout."""
    if not Path(database).exists():
        return set()
    with closing(sqlite3.connect(database, timeout=30)) as connection:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='final_holdout'",
        ).fetchone()
        if not has_table:
            return set()
        return {
            (str(row[0]), str(row[1])) for row in connection.execute(
                "SELECT strategy, regime FROM final_holdout WHERE status='active'",
            )
        }


def supersede_cohort_holdouts(
    *, database: str | Path, reason: str, strategy: str | None = None,
    regime: str | None = None, actor: str = "cli",
) -> dict:
    """Retire active cohort holdouts. Explicit operator action only; writes one event each."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("supersede requires a non-empty --reason")
    stamp = datetime.now(timezone.utc).isoformat()
    clauses, values = ["status='active'"], []
    if strategy:
        clauses.append("strategy=?")
        values.append(strategy)
    if regime:
        clauses.append("regime=?")
        values.append(regime)
    superseded = []
    with closing(_cohort_connection(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = [dict(row) for row in connection.execute(
                f"SELECT * FROM final_holdout WHERE {' AND '.join(clauses)} ORDER BY strategy, regime",
                values,
            ).fetchall()]
            for row in rows:
                connection.execute(
                    "UPDATE final_holdout SET status='superseded', superseded_at=?, superseded_reason=? "
                    "WHERE holdout_id=?",
                    (stamp, reason, row["holdout_id"]),
                )
                connection.execute(
                    """INSERT INTO final_holdout_event
                       (event_id, holdout_id, strategy, regime, action, reason, actor, created_at, details_json)
                       VALUES (?, ?, ?, ?, 'supersede', ?, ?, ?, ?)""",
                    (uuid4().hex, row["holdout_id"], row["strategy"], row["regime"], reason, actor, stamp,
                     json.dumps({"frozen_on": row["frozen_on"], "window_start": row["window_start"],
                                 "parameters_sha256": row["parameters_sha256"]}, sort_keys=True)),
                )
                superseded.append(_pair_label(row))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return {
        "status": "superseded", "superseded": superseded, "reason": reason,
        "research_only": True, "live_configuration_changed": False, "trade_or_order_actions": 0,
    }


def holdout_report(*, database: str | Path, output_dir: str | Path | None = None) -> dict:
    """Holdout state for the nightly report. Never changes a holdout."""
    cohorts = cohort_holdout_status(database)
    legacy = read_final_holdout_state(output_dir) if output_dir else None
    active = cohorts["active"]
    return {
        "status": "active" if active else "none",
        "research_only": True, "live_configuration_changed": False, "trade_or_order_actions": 0,
        "active_holdout_count": len(active),
        "promotion_paused_pairs": sorted(_pair_label(row) for row in active),
        "superseded_holdout_count": cohorts["superseded_count"],
        "legacy_manifest_status": (legacy or {}).get("status"),
    }


# Command line -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autoresearch.validation.final_holdout",
        description="Initialize, inspect, and supersede prospective cohort holdouts.",
    )
    parser.add_argument("--database", default=str(default_research_database()))
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("initialize", help="freeze a holdout for every strategy and regime cohort")
    init.add_argument("--frozen-on", type=date.fromisoformat, default=None,
                      help="freeze date (YYYY-MM-DD); the window starts the next day; default today in ET")
    commands.add_parser("status", help="active and superseded cohort holdouts")
    supersede = commands.add_parser("supersede", help="retire active holdouts (explicit action only)")
    supersede.add_argument("--reason", required=True)
    supersede.add_argument("--strategy")
    supersede.add_argument("--regime")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "initialize":
            frozen_on = args.frozen_on or datetime.now(ET).date()
            result = initialize_cohort_holdouts(database=args.database, frozen_on=frozen_on)
        elif args.command == "status":
            result = cohort_holdout_status(args.database)
        else:
            result = supersede_cohort_holdouts(
                database=args.database, reason=args.reason,
                strategy=args.strategy, regime=args.regime,
            )
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


__all__ = [
    "FINAL_HOLDOUT_SCHEMA", "active_holdout_pairs", "advance_final_holdout",
    "cohort_holdout_status", "holdout_report", "initialize_cohort_holdouts",
    "initialize_final_holdout", "read_final_holdout_state", "supersede_cohort_holdouts",
]


if __name__ == "__main__":
    raise SystemExit(main())
