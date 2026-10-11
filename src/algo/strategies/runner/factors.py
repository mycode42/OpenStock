"""Factor rebalance state and daily core-target helpers."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS, validate_parameter_profile
from src.algo.strategies.multi_factor import MultiFactorConfig, MultiFactorSignal, SignalGenerator


def _multi_factor_rebalance_signals(
    strategy_states: dict,
    daily_bars: dict[str, list[dict]],
    selected_symbols: list[str],
    position_owners: dict[str, str],
    session: date,
    regime: str = "unknown",
    intraday_bars: dict[str, list[dict]] | None = None,
    positions: dict[str, dict] | None = None,
    shadow_positions: dict[str, dict] | None = None,
    parameters: dict | None = None,
) -> tuple[dict[str, MultiFactorSignal], dict | None]:
    """Persist one PIT-safe factor rebalance per period, including shadow holdings."""
    config_values = dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor") or {})
    if parameters:
        config_values.update(parameters)
    generator = SignalGenerator(MultiFactorConfig.from_dict(config_values))
    period = generator.rebalance_key(session)
    by_symbol = strategy_states.setdefault("multi_factor", {})
    owners_for_signals = dict(position_owners)
    positions_for_signals = dict(positions or {})
    for ticker, shadow in (shadow_positions or {}).items():
        symbol = str(ticker).upper()
        if symbol not in owners_for_signals and not float((positions_for_signals.get(symbol) or {}).get("qty", 0) or 0):
            owners_for_signals[symbol] = "multi_factor"
            positions_for_signals[symbol] = {
                "qty": shadow.get("qty", 0),
                "avg_entry_price": shadow.get("entry_price"),
                "entry_at": shadow.get("entry_at"),
            }
    held = {ticker for ticker, owner in owners_for_signals.items() if owner == "multi_factor"}
    holdings = {
        ticker: {
            "qty": positions_for_signals.get(ticker, {}).get("qty", by_symbol.get(ticker, {}).get("last_known_qty", 1)),
            "entry_at": by_symbol.get(ticker, {}).get("opened_at") or positions_for_signals.get(ticker, {}).get("entry_at"),
        }
        for ticker in held
    }
    tracked = set(selected_symbols) | held
    if not any(by_symbol.get(ticker, {}).get("last_rebalance_period") != period for ticker in tracked):
        return {}, None
    signals, diagnostics = generator.generate(
        daily_bars,
        symbols=selected_symbols,
        as_of=session - timedelta(days=1),
        session=session,
        regime=regime,
        held_positions=holdings,
    )
    if not diagnostics["minimum_universe_met"]:
        return {}, diagnostics
    signals = generator.apply_intraday_size_tilt(signals, intraday_bars or {})
    records = {item.ticker: item for item in signals}
    for ticker in tracked:
        item = records.get(ticker)
        state = by_symbol.setdefault(ticker, {})
        state.update({
            "last_rebalance_period": period,
            "factor_score": item.score if item else None,
            "factor_rank": item.rank if item else None,
            "desired_direction": item.direction if item else "flat",
            "target_weight": item.target_weight if item else 0.0,
        })
    return {
        ticker: item for ticker, item in records.items()
        if item.action in {"buy", "sell", "cover", "short"}
    }, diagnostics


def _multi_factor_current_targets(
    strategy_states: dict,
    selected_symbols: list[str],
    session: date,
    parameters: dict | None = None,
) -> dict[str, dict]:
    """Read this period's cached targets for the standalone core strategy."""
    config_values = dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor") or {})
    if parameters:
        config_values.update(parameters)
    config = MultiFactorConfig.from_dict(config_values)
    generator = SignalGenerator(config)
    period = generator.rebalance_key(session)
    by_symbol = strategy_states.get("multi_factor", {})
    output = {}
    for ticker in selected_symbols:
        state = by_symbol.get(ticker, {})
        direction = state.get("desired_direction")
        if state.get("last_rebalance_period") != period or direction not in {"long", "short"}:
            continue
        output[ticker] = {
            "ticker": ticker,
            "strategy": "multi_factor",
            "action": "buy" if direction == "long" else "short",
            "direction": direction,
            "rank": state.get("factor_rank"),
            "score": state.get("factor_score"),
            "target_weight": config.max_position_pct,
            "stop_pct": config.hard_stop_pct,
            "risk_multiplier": config.risk_multiplier,
            "strategy_group": "swing",
            "reason": "current-period cross-sectional factor target",
        }
    return output


def multi_factor_parameters_for_session(
    session: date, *, execution_mode: str, artifact_path: str | Path | None = None,
) -> tuple[dict, dict]:
    """Use a fresh positive discovery only in the explicitly shadowed route."""
    baseline = validate_parameter_profile({
        "multi_factor": ALGO_DEFAULT_PARAMETERS["multi_factor"],
    })["multi_factor"]
    metadata = {"source": "live_defaults", "execution_mode": execution_mode}
    if execution_mode != "shadow":
        return baseline, metadata
    path = Path(artifact_path) if artifact_path else (
        Path(__file__).resolve().parents[4]
        / "data" / "research" / "algo-auto" / "adaptive"
        / "multi_factor" / "latest-discovery.json"
    )
    try:
        artifact = json.loads(path.read_text())
        window_end = date.fromisoformat(str(artifact.get("window", {}).get("end", "")))
        selected = artifact.get("selected_candidate") or {}
        metrics = selected.get("metrics") or {}
        age_days = (session - window_end).days
        if (
            artifact.get("schema") != "swarm_algo_adaptive_discovery_v1"
            or artifact.get("strategy") != "multi_factor"
            or artifact.get("selection_status") != "eligible_positive_candidate"
            or artifact.get("data_tier") != "algo_trader_plus"
            or artifact.get("feed") != "sip"
            or artifact.get("adjustment") != "split"
            or not selected.get("eligible")
            or float(metrics.get("net_return_pct") or 0) <= 0
            or not 0 < age_days <= 4
        ):
            return baseline, {**metadata, "candidate_status": "no_fresh_positive_candidate"}
        from src.algo.strategies.universe import configured_universe

        expected_symbols = set(configured_universe("algo")["tickers"])
        if set(artifact.get("symbols") or []) != expected_symbols:
            return baseline, {**metadata, "candidate_status": "universe_mismatch"}
        parameters = validate_parameter_profile({
            "multi_factor": selected["parameters"],
        })["multi_factor"]
        return parameters, {
            "source": "adaptive_shadow_discovery", "execution_mode": "shadow",
            "artifact": str(path.resolve()), "window_end": window_end.isoformat(),
            "candidate": selected.get("name"), "net_return_pct": metrics.get("net_return_pct"),
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return baseline, {**metadata, "candidate_status": "discovery_artifact_unavailable"}

