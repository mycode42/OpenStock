"""Path-aware options portfolio simulator with per-leg conservative execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from bisect import bisect_left
import hashlib
import json
import math
import os
import random
import time
from statistics import mean, median, pstdev

from options.assignment import simulate_early_assignment
from options.data import ChainQuote, OptionChain
from options.research.execution import is_two_sided
from options.models import GreeksSnapshot, OptionLeg, OptionSignal
from options.research.dataset import OptionsResearchDataset, ResearchObservation
from options.research.execution import (
    ExecutionAssumptions, assess_structure_liquidity, structure_entry_cost,
)
from options.research.strategy_configuration import StrategyConfiguration
from options.structures import OptionStructure
from strategies.options.base import OptionsStrategyContext
from strategies.options.long_volatility import atm_implied_volatility, historical_iv_percentile
from strategies.options.vrp import vrp_exit_reason
from options.research.strategy_factory import make_strategy




@dataclass
class _LegPosition:
    contract: object
    signed_contracts: int
    last_quote: ChainQuote | None = None
    last_greeks: GreeksSnapshot | None = None
    last_iv: float = 0.0
    last_spot: float | None = None


@dataclass
class _OpenTrade:
    signal: OptionSignal
    strategy_name: str
    contracts: int
    entry_at: datetime
    entry_regime: str
    entry_spot: float
    entry_debit: float = 0.0
    entry_credit: float = 0.0
    cashflow: float = 0.0
    shares: int = 0
    legs: dict[str, _LegPosition] = field(default_factory=dict)
    entry_costs: float = 0.0
    exit_costs: float = 0.0
    assignment_events: list[dict] = field(default_factory=list)
    maximum_abs_gamma_1pct: float = 0.0
    maximum_abs_vega_per_point: float = 0.0
    maximum_abs_delta_notional: float = 0.0
    stale_marks: int = 0
    greek_path: list[dict] = field(default_factory=list)
    last_spot: float | None = None
    current_exposures: dict[str, float] = field(default_factory=dict)
    exit_at: datetime | None = None
    exit_reason: str = ""
    pnl: float = 0.0
    closed: bool = False


def _hash_uniform(seed: int, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _regime_allowed(regime: str, volatility_state: str, allowed: tuple[str, ...]) -> bool:
    return regime in allowed or volatility_state in allowed


def _intrinsic(spot: float, strike: float, right: str) -> float:
    return max(0.0, spot - strike) if right == "call" else max(0.0, strike - spot)


def _package_greeks(structure, spot: float) -> dict[str, float]:
    exposure = {"delta_notional": 0.0, "gamma_pnl_1pct": 0.0,
                "vega_per_vol_point": 0.0, "theta_per_day": 0.0}
    for leg in structure.legs:
        signed, multiplier = leg.signed_ratio, leg.contract.multiplier
        exposure["delta_notional"] += leg.greeks.delta * signed * multiplier * spot
        exposure["gamma_pnl_1pct"] += 0.5 * leg.greeks.gamma * signed * multiplier * (spot * 0.01) ** 2
        exposure["vega_per_vol_point"] += leg.greeks.vega * signed * multiplier * 0.01
        exposure["theta_per_day"] += leg.greeks.theta * signed * multiplier
    return exposure


def _widen_chain(chain: OptionChain, multiplier: float) -> OptionChain:
    if multiplier == 1.0:
        return chain
    if not math.isfinite(multiplier) or multiplier < 1.0:
        raise ValueError("spread_width_multiplier must be finite and at least 1")
    quotes = []
    for quote in chain.contracts:
        midpoint = quote.midpoint
        half_spread = (quote.ask - quote.bid) * multiplier / 2.0
        bid = max(0.0, midpoint - half_spread)
        ask = max(bid, midpoint + half_spread)
        quotes.append(replace(quote, bid=bid, ask=ask))
    return replace(chain, contracts=tuple(quotes), data_quality=tuple(sorted(set(chain.data_quality) | {"spread_width_stress"})))


def _metrics(curve: list[dict], trades: list[dict], greek_summary: dict, quality: dict, threshold: float) -> dict:
    equity_values = [float(row["equity"]) for row in curve]
    returns = [float(row["return"]) for row in curve]
    total_return = equity_values[-1] / equity_values[0] - 1.0 if len(equity_values) > 1 and equity_values[0] else 0.0
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = mean(returns) / volatility * math.sqrt(252) if volatility > 0 else 0.0
    downside = [min(0.0, value) for value in returns]
    downside_dev = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    sortino = mean(returns) / downside_dev * math.sqrt(252) if downside_dev > 0 else 0.0
    peak = equity_values[0] if equity_values else 0.0
    max_drawdown = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    pnls = [float(row["pnl"]) for row in trades]
    gains = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    positive_daily = sum(max(0.0, float(row["daily_pnl"])) for row in curve)
    gamma_vega_positive = sum(
        max(0.0, float(row.get("attribution", {}).get("gamma", 0.0)))
        + max(0.0, float(row.get("attribution", {}).get("vega", 0.0)))
        for row in curve
    )
    gamma_vega_share = gamma_vega_positive / positive_daily if positive_daily > 0 else 0.0
    largest_positive_trade_share = max((value for value in pnls if value > 0), default=0.0) / gains if gains > 0 else 0.0
    episode_dominated = gamma_vega_share >= threshold or largest_positive_trade_share >= 0.50
    missing_fraction = quality.get("missing_chain_observations", 0) / max(
        1, quality.get("observations_total", len(curve)),
    )
    risk_adjusted = sharpe - 2.0 * max_drawdown - 2.0 * missing_fraction
    # A no-trade replay has supplied no evidence of an exploitable edge.
    if not trades:
        risk_adjusted = -5.0
    if episode_dominated:
        risk_adjusted -= 0.75
    return {
        "sessions": len(curve),
        "trade_count": len(trades),
        "total_return": total_return,
        "annualized_sharpe": sharpe,
        "annualized_sortino": sortino,
        "max_drawdown": max_drawdown,
        # Keep reports strict-JSON serializable. ``None`` means no losing trades.
        "profit_factor": gains / losses if losses > 0 else (None if gains > 0 else 0.0),
        "win_rate": sum(value > 0 for value in pnls) / len(pnls) if pnls else 0.0,
        "average_trade_pnl": mean(pnls) if pnls else 0.0,
        "median_trade_pnl": median(pnls) if pnls else 0.0,
        "risk_adjusted_score": risk_adjusted,
        "gamma_vega_positive_pnl_share": gamma_vega_share,
        "largest_winning_trade_share": largest_positive_trade_share,
        "returns_dominated_by_gamma_or_vega_episodes": episode_dominated,
        "greeks": greek_summary,
        "quality": quality,
    }


# Lab runs set ALGO_LAB_PROGRESS=1: the replay then prints a LAB_EVENT replay_progress line at most every half second,
# so the lab's progress bar moves while one underlying replays. Same event shape as the equity replay.
PROGRESS_EVENT_PREFIX = "LAB_EVENT "
PROGRESS_MIN_INTERVAL_SECONDS = 0.5


class _ReplayProgress:
    def __init__(self, total: int, callback=None) -> None:
        self.enabled = os.environ.get("ALGO_LAB_PROGRESS") == "1"
        self.callback = callback
        self.total = total
        self.started = time.monotonic()
        self.last = 0.0
        self.last_callback = 0.0

    def tick(self, done: int, through) -> None:
        now = time.monotonic()
        elapsed = now - self.started
        rate = elapsed / done if done else 0.0
        event = {
            "event": "replay_progress", "label": os.environ.get("ALGO_REPLAY_LABEL", "replay"),
            "sessions_done": done, "sessions_total": self.total,
            "fraction": round(done / self.total, 4) if self.total else 1.0,
            "through": through.isoformat() if through else None,
            "elapsed_seconds": round(elapsed, 1),
            "remaining_seconds": round(rate * (self.total - done), 1),
        }
        if self.enabled and (done >= self.total or now - self.last >= PROGRESS_MIN_INTERVAL_SECONDS):
            self.last = now
            print(PROGRESS_EVENT_PREFIX + json.dumps(event, sort_keys=True), flush=True)
        if self.callback and (done >= self.total or now - self.last_callback >= 5.0):
            self.last_callback = now
            self.callback(event)


def simulate_options_strategy(
    dataset: OptionsResearchDataset,
    observations: tuple[ResearchObservation, ...],
    *,
    strategy_name: str,
    strategy_configuration: StrategyConfiguration,
    config: dict,
    base_config: dict,
    slippage_multiplier: float = 1.0,
    spread_width_multiplier: float = 1.0,
    assignment_enabled: bool = True,
    force_close: bool = True,
    strategy_configurations: dict[str, StrategyConfiguration] | None = None,
    strategy_underlyings: dict[str, set[str] | tuple[str, ...] | list[str]] | None = None,
    progress_callback=None,
) -> dict:
    """Replay one shared options account over a chronological multi-symbol slice."""
    if not observations:
        raise ValueError("simulation slice is empty")
    execution_values = dict(config.get("execution", {}))
    execution_values["spread_fraction_per_leg"] = float(execution_values.get("spread_fraction_per_leg", 0.5)) * slippage_multiplier
    execution = ExecutionAssumptions.from_dict(execution_values)
    market = config.get("market_data", {})
    configurations = dict(strategy_configurations or {strategy_name: strategy_configuration})
    strategy_universes = {
        name: {str(symbol).upper() for symbol in symbols}
        for name, symbols in (strategy_underlyings or {}).items()
    }
    observed_symbols = sorted({item.underlying.upper() for item in observations})
    strategy_definitions = {}
    for name, selected_configuration in configurations.items():
        strategy_parameters = dict(selected_configuration.parameters)
        if name in {"long_volatility", "directional_overlay"} and "underlyings" not in strategy_parameters:
            strategy_parameters["underlyings"] = sorted(strategy_universes.get(name, set(observed_symbols)))
        strategy_instance, strategy_section = make_strategy(
            name,
            parameters=strategy_parameters,
            allowed_regimes=selected_configuration.allowed_regimes,
            base_config=base_config,
        )
        strategy_definitions[name] = {
            "configuration": selected_configuration,
            "instance": strategy_instance,
            "section": strategy_section,
            "needs_iv_history": name == "long_volatility" or (
                name == "vrp" and strategy_section.get("min_iv_percentile") is not None
            ),
        }
    if not strategy_definitions:
        raise ValueError("portfolio replay needs at least one enabled strategy")
    strategy_name = next(iter(strategy_definitions))
    date_to_index = {
        (row.underlying.upper(), row.as_of): index
        for index, row in enumerate(dataset.observations)
    }
    iv_history_by_underlying: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    # Build IV history during the chronological replay so lazy chain batches
    # stay disk-backed and prior observations cannot leak into earlier dates.
    initial_equity = float(config.get("starting_equity", 100000))
    if initial_equity <= 0 or not math.isfinite(initial_equity):
        raise ValueError("starting_equity must be positive and finite")
    max_contracts = int(execution_values.get("maximum_contracts_per_trade", 1))
    risk_per_trade = float(execution_values["max_risk_per_trade_pct"])
    max_total_risk = float(execution_values.get("max_total_defined_risk_pct", 0.10))
    minimum_chain_contracts = int(market.get("minimum_contracts_per_chain", 20))
    maximum_spread = float(market.get("maximum_leg_spread_pct", 0.25))
    minimum_oi = int(market.get("minimum_open_interest", 100))
    require_oi = bool(market.get("require_open_interest", True))
    maximum_quote_age = int(market.get("maximum_quote_age_seconds", 172800))
    max_snapshot_age = int(market.get("maximum_snapshot_age_sessions", 1))
    greek_limits = config.get("greek_limits", {})
    assignment_threshold = float(config.get("assignment", {}).get("extrinsic_threshold", 0.02))
    dividends = dict(config.get("dividends", {}))
    seed = int(config.get("random_seed", 1))
    active: list[_OpenTrade] = []
    closed_trades: list[dict] = []
    realized_cash = 0.0
    curve: list[dict] = []
    quality = {
        "observations_total": len(observations),
        "missing_chain_observations": 0,
        "stale_chain_observations": 0,
        "low_liquidity_signals": 0,
        "signals_seen": 0,
        "signals_filled": 0,
        "signals_unfilled": 0,
        "multileg_order_no_fills": 0,
        "missing_open_interest_legs": 0,
        "low_open_interest_legs": 0,
        "stale_entry_quote_legs": 0,
        "stale_position_marks": 0,
        "greek_limit_rejections": 0,
        "risk_limit_rejections": 0,
        "iv_rank_warmup_skips": 0,
    }
    greek_peaks = {"delta_notional": 0.0, "gamma_pnl_1pct": 0.0, "vega_per_vol_point": 0.0, "theta_per_day": 0.0}
    last_equity = initial_equity
    long_vol_entries_by_session: dict[tuple[str, date], int] = {}
    total_attribution = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "residual": 0.0}

    def current_chain(observation: ResearchObservation):
        chain = observation.chain
        if chain is None and observation.chain_error is None:
            load_chain = getattr(dataset.provider, "get_chain_for_observation", None)
            if load_chain is not None:
                chain = load_chain(observation)
        if chain is None:
            quality["missing_chain_observations"] += 1
            return None
        symbol = observation.underlying.upper()
        snapshot_index = date_to_index.get((symbol, chain.as_of))
        current_index = date_to_index.get((symbol, observation.as_of))
        age = current_index - snapshot_index if snapshot_index is not None and current_index is not None else 10**6
        if age > max_snapshot_age:
            quality["stale_chain_observations"] += 1
            if market.get("missing_chain_policy", "skip") == "skip":
                return None
        return chain

    def close_share_inventory(trade: _OpenTrade, spot: float) -> None:
        nonlocal realized_cash
        if trade.shares:
            trade.cashflow += trade.shares * spot
            fee = abs(trade.shares) * execution.stock_commission_per_share_usd
            trade.cashflow -= fee
            trade.exit_costs += fee
            trade.shares = 0
        trade.closed = True
        trade.pnl = trade.cashflow
        realized_cash += trade.cashflow
        closed_trades.append({
            "strategy": trade.strategy_name,
            "structure_type": trade.signal.structure.structure_type,
            "structure_id": trade.signal.structure.structure_id,
            "entry_at": trade.entry_at.isoformat(),
            "exit_at": trade.exit_at.isoformat(),
            "entry_regime": trade.entry_regime,
            "exit_reason": trade.exit_reason,
            "underlying": trade.signal.underlying,
            "entry_spot": trade.entry_spot,
            "exit_spot": spot,
            "contracts": trade.contracts,
            "pnl": trade.pnl,
            "return_on_defined_risk": trade.pnl / max(trade.signal.structure.risk.max_loss * trade.contracts, 1e-9),
            "entry_costs": trade.entry_costs,
            "exit_costs": trade.exit_costs,
            "assignment_events": list(trade.assignment_events),
            "maximum_abs_gamma_pnl_1pct": trade.maximum_abs_gamma_1pct,
            "maximum_abs_vega_per_vol_point": trade.maximum_abs_vega_per_point,
            "maximum_abs_delta_notional": trade.maximum_abs_delta_notional,
            "stale_marks": trade.stale_marks,
            "entry_reason": trade.signal.reason,
            "entry_iv_percentile": trade.signal.data_quality.get("iv_percentile"),
            "structure_details": {
                "type": trade.signal.structure.structure_type,
                "expiration": trade.signal.structure.expiration.isoformat(),
                "legs": [
                    {"symbol": leg.contract.symbol, "right": leg.contract.right,
                     "strike": leg.contract.strike, "side": leg.side,
                     "ratio_qty": leg.ratio_qty}
                    for leg in trade.signal.structure.legs
                ],
            },
            "greek_path": list(trade.greek_path),
        })

    def settle_leg(trade: _OpenTrade, symbol: str, state: _LegPosition, spot: float, as_of: datetime, *, reason: str) -> None:
        quantity = state.signed_contracts
        if quantity == 0:
            return
        contract = state.contract
        intrinsic = _intrinsic(spot, contract.strike, contract.right)
        # European index options such as SPX are cash-settled. SPY ETF options
        # are generally American and settle into shares. Contract metadata
        # must identify exercise style correctly in historical data.
        if str(getattr(contract, "exercise_style", "american")).lower() == "european":
            trade.cashflow += quantity * intrinsic * contract.multiplier
            fee = abs(quantity) * execution.exercise_fee_per_contract_usd
            trade.cashflow -= fee
            trade.exit_costs += fee
            state.signed_contracts = 0
            return
        moneyness = spot - contract.strike if contract.right == "call" else contract.strike - spot
        exercise = quantity > 0 and intrinsic > 0
        assigned = quantity < 0 and intrinsic > 0
        if quantity < 0 and not assigned and abs(moneyness) <= float(execution_values.get("pin_risk_threshold_dollars", 0.02)):
            probability = float(execution_values.get("pin_risk_assignment_probability", 0.10))
            assigned = _hash_uniform(seed, f"pin:{as_of.date()}:{symbol}") < probability
        if exercise or assigned:
            count = abs(quantity) * contract.multiplier
            if contract.right == "call":
                share_change = count if quantity > 0 else -count
            else:
                share_change = -count if quantity > 0 else count
            trade.cashflow -= share_change * contract.strike
            trade.shares += share_change
            fee = abs(quantity) * execution.exercise_fee_per_contract_usd
            trade.cashflow -= fee
            trade.exit_costs += fee
            if assigned:
                event = {
                    "symbol": symbol,
                    "at": as_of.isoformat(),
                    "reason": reason,
                    "contracts": abs(quantity),
                    "resulting_share_change": share_change,
                    "spot": spot,
                    "strike": contract.strike,
                }
                trade.assignment_events.append(event)
        state.signed_contracts = 0

    def mark_and_manage(trade: _OpenTrade, observation: ResearchObservation, chain, chain_by_symbol: dict[str, ChainQuote]) -> tuple[float, dict, dict]:
        spot = observation.spot
        daily_attribution = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        greeks = {"delta_shares": float(trade.shares), "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        previous_spot = getattr(trade, "last_spot", None)
        if previous_spot is not None and trade.shares:
            daily_attribution["delta"] += trade.shares * (spot - previous_spot)
        for symbol, state in list(trade.legs.items()):
            if state.signed_contracts == 0:
                continue
            quote = chain_by_symbol.get(symbol) if chain is not None else None
            quote_is_fresh = quote is not None
            if quote is not None:
                quote_age = (observation.as_of - quote.observed_at).total_seconds()
                if quote_age < 0 or quote_age > maximum_quote_age:
                    quote = None
                    quote_is_fresh = False
            if quote is None:
                trade.stale_marks += 1
                quality["stale_position_marks"] += 1
                if state.last_quote is None and state.contract.expiration > observation.as_of.date():
                    continue
                quote = state.last_quote
            if quote is not None:
                if state.last_greeks is not None and quote_is_fresh:
                    elapsed = max(0.0, (observation.as_of - state.last_greeks.observed_at).total_seconds() / 86400.0)
                    move = spot - state.last_spot if state.last_spot is not None else 0.0
                    scale = state.signed_contracts * state.contract.multiplier
                    daily_attribution["delta"] += state.last_greeks.delta * move * scale
                    daily_attribution["gamma"] += 0.5 * state.last_greeks.gamma * move * move * scale
                    daily_attribution["vega"] += state.last_greeks.vega * (quote.greeks.implied_volatility - state.last_iv) * scale
                    daily_attribution["theta"] += state.last_greeks.theta * elapsed * scale
                if quote_is_fresh:
                    state.last_quote = quote
                    state.last_greeks = quote.greeks
                    state.last_iv = quote.greeks.implied_volatility
                    state.last_spot = spot
            if assignment_enabled and state.signed_contracts < 0 and quote is not None and quote_is_fresh:
                leg = OptionLeg(
                    state.contract, "sell", max(1, abs(state.signed_contracts) // trade.contracts),
                    bid=quote.bid, ask=quote.ask, greeks=quote.greeks,
                )
                assignment = simulate_early_assignment(
                    leg,
                    contracts=trade.contracts,
                    underlying_price=spot,
                    as_of=observation.as_of.date(),
                    expected_dividend_per_share=float(observation.dividends.get(
                        trade.signal.underlying,
                        dividends.get(trade.signal.underlying, 0.0),
                    )),
                    extrinsic_threshold=assignment_threshold,
                )
                if assignment is not None:
                    settle_leg(trade, symbol, state, spot, observation.as_of, reason=assignment.reason)
        # Rebuild exposures after exercise/assignment so assigned legs are
        # removed and resulting stock inventory contributes its delta.
        greeks = {"delta_shares": float(trade.shares), "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        for state in trade.legs.values():
            if state.signed_contracts == 0 or state.last_greeks is None:
                continue
            scale = state.signed_contracts * state.contract.multiplier
            greeks["delta_shares"] += state.last_greeks.delta * scale
            greeks["gamma"] += state.last_greeks.gamma * scale
            greeks["vega"] += state.last_greeks.vega * scale
            greeks["theta"] += state.last_greeks.theta * scale
        trade.last_spot = spot
        delta_notional = greeks["delta_shares"] * spot
        gamma_1pct = 0.5 * greeks["gamma"] * (spot * 0.01) ** 2
        vega_point = greeks["vega"] * 0.01
        trade.maximum_abs_delta_notional = max(trade.maximum_abs_delta_notional, abs(delta_notional))
        trade.maximum_abs_gamma_1pct = max(trade.maximum_abs_gamma_1pct, abs(gamma_1pct))
        trade.maximum_abs_vega_per_point = max(trade.maximum_abs_vega_per_point, abs(vega_point))
        current_value = 0.0
        for state in trade.legs.values():
            if state.signed_contracts == 0:
                continue
            quote = state.last_quote
            if quote is None or not is_two_sided(quote):
                continue
            exit_price = execution.exit_fill(quote, state.signed_contracts)
            current_value += state.signed_contracts * exit_price * state.contract.multiplier
        equity_value = trade.cashflow + current_value + trade.shares * spot
        daily_attribution["residual"] = 0.0
        exposures = {
            "delta_notional": delta_notional,
            "gamma_pnl_1pct": gamma_1pct,
            "vega_per_vol_point": vega_point,
            "theta_per_day": greeks["theta"],
        }
        greek_row = {
            "date": observation.as_of.isoformat(),
            **exposures,
            "leg_iv": {
                symbol: state.last_iv
                for symbol, state in trade.legs.items() if state.last_iv > 0
            },
        }
        if trade.greek_path and trade.greek_path[-1]["date"][:10] == greek_row["date"][:10]:
            trade.greek_path[-1] = greek_row
        else:
            trade.greek_path.append(greek_row)
        trade.current_exposures = {
            "delta_notional": delta_notional,
            "gamma_pnl_1pct": gamma_1pct,
            "vega_per_vol_point": vega_point,
            "theta_per_day": exposures["theta_per_day"],
        }
        return equity_value, daily_attribution, exposures

    def _pnl_if_closed(trade: _OpenTrade, chain_by_symbol: dict[str, ChainQuote]) -> float | None:
        """Cash the trade would return if every open leg were closed now at its exit fill."""
        cash = trade.cashflow
        for symbol, state in trade.legs.items():
            if state.signed_contracts == 0:
                continue
            quote = chain_by_symbol.get(symbol) or state.last_quote
            if quote is None:
                return None
            try:
                price = execution.exit_fill(quote, state.signed_contracts)
            except ValueError:
                # No two-sided quote to close against: the value is unknown, so no stop is triggered on it.
                return None
            cash += state.signed_contracts * price * state.contract.multiplier
        return cash

    def close_at_window_end(
        trade: _OpenTrade,
        observation: ResearchObservation,
        chain_by_symbol: dict[str, ChainQuote],
        *,
        exit_reason: str = "validation_window_end",
    ) -> None:
        for symbol, state in trade.legs.items():
            if state.signed_contracts == 0:
                continue
            quote = chain_by_symbol.get(symbol)
            if quote is not None:
                quote_age = (observation.as_of - quote.observed_at).total_seconds()
                if quote_age < 0 or quote_age > maximum_quote_age:
                    quote = None
            if quote is not None and not is_two_sided(quote):
                quote = None
            if quote is None and state.last_quote is not None and is_two_sided(state.last_quote):
                quote = state.last_quote
            if quote is not None:
                quote_age = (observation.as_of - quote.observed_at).total_seconds()
                if quote_age < 0 or quote_age > maximum_quote_age:
                    quote = None
            if quote is None:
                # Data loss at forced liquidation is charged at intrinsic plus
                # one full spread proxy; this is reported as a stale mark.
                intrinsic = _intrinsic(observation.spot, state.contract.strike, state.contract.right)
                penalty = max(0.01, observation.spot * 0.0025)
                # A missing-quote liquidation hurts longs through a lower sale
                # value and hurts shorts through a higher cover value.
                price = max(0.0, intrinsic - penalty) if state.signed_contracts > 0 else intrinsic + penalty
                trade.stale_marks += 1
                quality["stale_position_marks"] += 1
            else:
                price = execution.exit_fill(quote, state.signed_contracts)
            trade.cashflow += state.signed_contracts * price * state.contract.multiplier
            fee = execution.commission(abs(state.signed_contracts), 1)
            trade.cashflow -= fee
            trade.exit_costs += fee
            state.signed_contracts = 0
        trade.exit_at = observation.as_of
        trade.exit_reason = exit_reason
        close_share_inventory(trade, observation.spot)

    progress = _ReplayProgress(len(observations), callback=progress_callback)
    session_date: date | None = None
    session_attribution = {key: 0.0 for key in total_attribution}
    session_regimes: set[str] = set()
    session_volatility_states: set[str] = set()
    last_observation_by_symbol = {
        item.underlying.upper(): item for item in observations
    }
    observation_count_by_symbol: dict[str, int] = {}
    for item in observations:
        key = item.underlying.upper()
        observation_count_by_symbol[key] = observation_count_by_symbol.get(key, 0) + 1
    seen_observation_count_by_symbol: dict[str, int] = {}

    def finish_session() -> None:
        nonlocal last_equity
        if session_date is None:
            return
        current_equity = initial_equity + realized_cash
        for trade in active:
            option_value = sum(
                state.signed_contracts * execution.exit_fill(state.last_quote, state.signed_contracts)
                * state.contract.multiplier
                for state in trade.legs.values()
                if state.signed_contracts and state.last_quote is not None and is_two_sided(state.last_quote)
            )
            current_equity += trade.cashflow + trade.shares * float(trade.last_spot or trade.entry_spot) + option_value
        daily_pnl = current_equity - last_equity
        daily_greeks = {
            "delta_notional": sum(item.current_exposures.get("delta_notional", 0.0) for item in active),
            "gamma_pnl_1pct": sum(item.current_exposures.get("gamma_pnl_1pct", 0.0) for item in active),
            "vega_per_vol_point": sum(item.current_exposures.get("vega_per_vol_point", 0.0) for item in active),
            "theta_per_day": sum(item.current_exposures.get("theta_per_day", 0.0) for item in active),
        }
        for key, value in daily_greeks.items():
            greek_peaks[key] = max(greek_peaks[key], abs(value))
        attribution = dict(session_attribution)
        attribution["residual"] = daily_pnl - sum(
            attribution[key] for key in ("delta", "gamma", "vega", "theta")
        )
        for key, value in attribution.items():
            total_attribution[key] += value
        curve.append({
            "date": session_date.isoformat(),
            "equity": current_equity,
            "daily_pnl": daily_pnl,
            "return": daily_pnl / last_equity if last_equity else 0.0,
            "regime": ",".join(sorted(session_regimes)) or "unknown",
            "volatility_state": ",".join(sorted(session_volatility_states)) or "unknown",
            "greeks": daily_greeks,
            "attribution": attribution,
            "open_structures": len(active),
        })
        last_equity = current_equity

    for obs_index, observation in enumerate(observations):
        progress.tick(obs_index + 1, observation.as_of)
        observation_session = observation.as_of.date()
        symbol = observation.underlying.upper()
        if session_date is None:
            session_date = observation_session
        elif observation_session != session_date:
            finish_session()
            session_date = observation_session
            session_attribution = {key: 0.0 for key in total_attribution}
            session_regimes.clear()
            session_volatility_states.clear()
        session_regimes.add(observation.regime or "unknown")
        session_volatility_states.add(observation.volatility_state or "unknown")
        chain = None
        chain = current_chain(observation)
        if chain is not None:
            chain = _widen_chain(chain, spread_width_multiplier)
        chain_by_symbol = chain.symbol_index() if chain is not None else {}
        day_attribution = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "residual": 0.0}
        newly_opened: list[_OpenTrade] = []
        for trade in list(active):
            if trade.signal.underlying.upper() != symbol:
                continue
            trade_definition = strategy_definitions[trade.strategy_name]
            trade_strategy = trade_definition["instance"]
            trade_section = trade_definition["section"]
            marked_value, attribution, exposures = mark_and_manage(trade, observation, chain, chain_by_symbol)
            for key in ("delta", "gamma", "vega", "theta"):
                day_attribution[key] += attribution[key]
            if all(state.signed_contracts == 0 for state in trade.legs.values()):
                trade.exit_at = observation.as_of
                trade.exit_reason = "assignment_or_expiration"
                close_share_inventory(trade, observation.spot)
                active.remove(trade)
                realized_cash = sum(float(item["pnl"]) for item in closed_trades)
            else:
                exit_reason = None
                if trade.strategy_name == "vrp":
                    held_days = max(0, (observation.as_of.date() - trade.entry_at.date()).days)
                    exit_reason = vrp_exit_reason(
                        entry_credit=trade.entry_credit,
                        pnl_if_closed=_pnl_if_closed(trade, chain_by_symbol),
                        held_days=held_days,
                        config=trade_section,
                    )
                if trade.strategy_name == "long_volatility":
                    held_days = max(0, (observation.as_of.date() - trade.entry_at.date()).days)
                    open_dtes = [
                        (state.contract.expiration - observation.as_of.date()).days
                        for state in trade.legs.values() if state.signed_contracts
                    ]
                    package_value = marked_value - trade.cashflow - trade.shares * observation.spot
                    exit_reason = trade_strategy.exit_reason(
                        entry_debit=trade.entry_debit,
                        current_value=package_value,
                        held_days=held_days,
                        days_to_expiry=min(open_dtes, default=0),
                        regime=observation.volatility_state,
                        entry_regime=str(
                            trade.signal.data_quality.get("activation_state")
                            or trade.entry_regime
                        ),
                        config=trade_section,
                    )
                expired = all(state.contract.expiration <= observation.as_of.date() for state in trade.legs.values() if state.signed_contracts)
                if exit_reason:
                    close_at_window_end(
                        trade, observation, chain_by_symbol, exit_reason=exit_reason,
                    )
                    active.remove(trade)
                    realized_cash = sum(float(item["pnl"]) for item in closed_trades)
                elif expired:
                    for leg_symbol, state in trade.legs.items():
                        if state.signed_contracts:
                            settle_leg(trade, leg_symbol, state, observation.spot, observation.as_of, reason="expiration_assignment")
                    trade.exit_at = observation.as_of
                    trade.exit_reason = "expiration"
                    close_share_inventory(trade, observation.spot)
                    active.remove(trade)
                    realized_cash = sum(float(item["pnl"]) for item in closed_trades)

        entry_signals = []
        iv_samples = {}
        if chain is not None and len(chain.contracts) >= minimum_chain_contracts:
            max_lookback = max((
                int(definition["section"].get("realized_lookback_sessions", 0)) + 1
                for definition in strategy_definitions.values()
            ), default=2)
            closes = dataset.provider.underlying_closes(
                observation.underlying, as_of=observation.as_of,
                sessions=max(3, max_lookback),
            )
            iv_contexts = {}
            for history_name, definition in strategy_definitions.items():
                if not definition["needs_iv_history"]:
                    continue
                if history_name in strategy_universes and symbol not in strategy_universes[history_name]:
                    continue
                history_section = definition["section"]
                try:
                    atm_iv = atm_implied_volatility(chain, history_section)
                except (ValueError, ArithmeticError, OverflowError):
                    atm_iv = None
                iv_samples[history_name] = atm_iv
                prior = iv_history_by_underlying.get((history_name, symbol), [])
                lookback = int(history_section.get("iv_percentile_lookback_sessions", 252))
                prior_values = [value for stamp, value in prior if stamp < observation.as_of][-lookback:]
                percentile = historical_iv_percentile(
                    atm_iv,
                    prior_values,
                    min_samples=int(history_section.get("iv_rank_min_samples", 20)),
                ) if atm_iv is not None else None
                iv_contexts[history_name] = (percentile, len(prior_values))
                if percentile is None:
                    quality["iv_rank_warmup_skips"] += 1
            for current_name, definition in strategy_definitions.items():
                if current_name in strategy_universes and symbol not in strategy_universes[current_name]:
                    continue
                if any(
                    trade.signal.underlying.upper() == symbol and trade.strategy_name == current_name
                    for trade in active
                ):
                    continue
                current_configuration = definition["configuration"]
                current_strategy = definition["instance"]
                current_section = definition["section"]
                session_key = (symbol, observation_session)
                if (
                    current_name == "long_volatility"
                    and long_vol_entries_by_session.get(session_key, 0)
                    >= int(current_section.get("max_entries_per_day", 1))
                ):
                    continue
                threshold_activation = (
                    current_name == "long_volatility"
                    and current_section.get("activation_mode") == "regime_or_realized_vol"
                )
                if not threshold_activation and not _regime_allowed(
                    observation.regime, observation.volatility_state,
                    current_configuration.allowed_regimes,
                ):
                    continue
                context = OptionsStrategyContext(
                    chain=chain,
                    underlying_closes=closes[-max(3, int(current_section.get("realized_lookback_sessions", 0)) + 1):],
                    regime=observation.regime,
                    volatility_state=observation.volatility_state,
                    events=observation.events,
                    event_recommendations=observation.event_recommendations,
                    equity_signals=observation.equity_signals,
                    as_of=observation.as_of,
                )
                if current_name in iv_contexts:
                    percentile, sample_count = iv_contexts[current_name]
                    context = replace(
                        context,
                        iv_percentile=percentile,
                        iv_percentile_samples=sample_count,
                        iv_percentile_source="historical_chain_atm_iv",
                    )
                try:
                    signal = current_strategy.generate(context)
                except (ValueError, ArithmeticError, OverflowError):
                    signal = None
                if signal is not None:
                    entry_signals.append((signal, current_name, current_configuration, current_section))
            for current_name, sample in iv_samples.items():
                if sample is not None:
                    iv_history_by_underlying.setdefault((current_name, symbol), []).append(
                        (observation.as_of, sample),
                    )
        quality["signals_seen"] += len(entry_signals)
        if entry_signals:
            signal, strategy_name, strategy_configuration, strategy_section = max(
                entry_signals, key=lambda row: float(row[0].confidence),
            )
            liquidity = assess_structure_liquidity(
                signal.structure,
                chain_by_symbol,
                maximum_spread_pct=min(maximum_spread, float(strategy_section.get("max_spread_pct", maximum_spread))),
                minimum_open_interest=minimum_oi,
                require_open_interest=require_oi,
            )
            stale_symbols = []
            for leg in signal.structure.legs:
                quote = chain_by_symbol.get(leg.contract.symbol)
                if quote is None:
                    continue
                age = (observation.as_of - quote.observed_at).total_seconds()
                if age < 0 or age > maximum_quote_age:
                    stale_symbols.append(leg.contract.symbol)
            if stale_symbols:
                quality["stale_entry_quote_legs"] += len(stale_symbols)
                liquidity = replace(
                    liquidity, eligible=False,
                    reasons=liquidity.reasons + tuple(f"stale_quote:{symbol}" for symbol in stale_symbols),
                )
            quality["missing_open_interest_legs"] += liquidity.missing_open_interest_legs
            quality["low_open_interest_legs"] += liquidity.low_open_interest_legs
            if not liquidity.eligible:
                quality["low_liquidity_signals"] += 1
            else:
                contracts = min(max_contracts, int(signal.desired_contracts))
                premium_cap = signal.data_quality.get("max_premium_per_trade_usd")
                if premium_cap is not None and signal.structure.risk.premium_at_risk > 0:
                    contracts = min(
                        contracts,
                        int(float(premium_cap) // signal.structure.risk.premium_at_risk),
                    )
                package_greeks = _package_greeks(signal.structure, chain.spot)
                limits = {
                    "delta_notional": initial_equity * float(greek_limits.get("max_abs_delta_notional_pct_equity", 0.75)),
                    "gamma_pnl_1pct": initial_equity * float(greek_limits.get("max_abs_gamma_pnl_1pct_pct_equity", 0.005)),
                    "vega_per_vol_point": initial_equity * float(greek_limits.get("max_abs_vega_per_vol_point_pct_equity", 0.01)),
                    "theta_per_day": initial_equity * float(greek_limits.get("max_abs_theta_per_day_pct_equity", 0.001)),
                }
                greek_rejected = False
                for greek_name, exposure in package_greeks.items():
                    exposure_abs = abs(exposure)
                    limit = limits[greek_name]
                    if exposure_abs > 0:
                        current_exposure = sum(
                            abs(trade.current_exposures.get(greek_name, 0.0))
                            for trade in active
                        )
                        available = limit - current_exposure
                        if available <= 0:
                            contracts = 0
                            greek_rejected = True
                            break
                        contracts = min(contracts, int(available // exposure_abs))
                current_assignment_shares = sum(
                    abs(trade.signal.structure.risk.assignment_share_exposure * trade.contracts)
                    for trade in active
                )
                if current_assignment_shares + signal.structure.risk.assignment_share_exposure * contracts > int(greek_limits.get("max_assignment_shares", 200)):
                    contracts = 0
                    greek_rejected = True
                if contracts < 1:
                    quality["greek_limit_rejections"] += int(greek_rejected)
                    quality["signals_unfilled"] += 1
                if not all(is_two_sided(chain_by_symbol[leg.contract.symbol]) for leg in signal.structure.legs):
                    contracts = 0
                    quality["signals_unquoted"] = quality.get("signals_unquoted", 0) + 1
                entry_costs = structure_entry_cost(signal.structure, chain_by_symbol, execution, contracts) if contracts > 0 else 0.0
                regime_weight = float(signal.data_quality.get("regime_risk_multiplier", 1.0))
                risk_per_trade = float(execution_values["max_risk_per_trade_pct"])
                if strategy_name == "long_volatility":
                    risk_per_trade = min(
                        risk_per_trade,
                        float(strategy_section.get("max_risk_per_trade_pct", risk_per_trade)),
                    )
                budget = initial_equity * risk_per_trade * regime_weight
                new_trade_risk = signal.structure.risk.max_loss * contracts + entry_costs
                current_open_risk = sum(
                    trade.signal.structure.risk.max_loss * trade.contracts + trade.entry_costs
                    for trade in active
                )
                if (
                    contracts > 0 and signal.structure.risk.defined_risk
                    and new_trade_risk <= budget
                    and current_open_risk + new_trade_risk <= initial_equity * max_total_risk
                ):
                    fill_key = f"entry:{observation.as_of.date()}:{observation.underlying}:{strategy_name}"
                    gross_entry_cashflow = sum(
                        -leg.signed_ratio * contracts
                        * execution.entry_fill(chain_by_symbol[leg.contract.symbol], leg.side)
                        * leg.contract.multiplier
                        for leg in signal.structure.legs
                    )
                    credit_remains_credit = (
                        signal.structure.net_debit_per_share >= 0 or gross_entry_cashflow > 0
                    )
                    package_fill_probability = execution.package_fill_probability(len(signal.structure.legs))
                    fill_draw = _hash_uniform(seed, fill_key)
                    if credit_remains_credit and fill_draw <= package_fill_probability:
                        entry_cashflow = 0.0
                        states = {}
                        realized_entry_slippage = 0.0
                        for leg in signal.structure.legs:
                            quote = chain_by_symbol[leg.contract.symbol]
                            fill = execution.entry_fill(quote, leg.side)
                            signed_qty = leg.signed_ratio * contracts
                            entry_cashflow -= signed_qty * fill * leg.contract.multiplier
                            midpoint = (quote.bid + quote.ask) / 2.0
                            realized_entry_slippage += abs(fill - midpoint) * leg.ratio_qty * contracts * leg.contract.multiplier
                            states[leg.contract.symbol] = _LegPosition(
                                contract=leg.contract,
                                signed_contracts=signed_qty,
                                last_quote=quote,
                                last_greeks=quote.greeks,
                                last_iv=quote.greeks.implied_volatility,
                                last_spot=observation.spot,
                            )
                        commission = execution.commission(contracts, sum(leg.ratio_qty for leg in signal.structure.legs))
                        entry_cashflow -= commission
                        entry_cost_total = realized_entry_slippage + commission
                        signal_structure = replace(
                            signal.structure,
                            metadata={**signal.structure.metadata, "underlying_spot": chain.spot},
                        )
                        signal = replace(signal, structure=signal_structure)
                        active.append(_OpenTrade(
                            signal=signal,
                            strategy_name=strategy_name,
                            contracts=contracts,
                            entry_at=observation.as_of,
                            entry_regime=observation.regime,
                            entry_spot=observation.spot,
                            entry_debit=max(0.0, -gross_entry_cashflow),
                            entry_credit=max(0.0, gross_entry_cashflow),
                            cashflow=entry_cashflow,
                            legs=states,
                            entry_costs=entry_cost_total,
                            last_spot=observation.spot,
                        ))
                        newly_opened.append(active[-1])
                        quality["signals_filled"] += 1
                        if strategy_name == "long_volatility":
                            session_key = (symbol, observation_session)
                            long_vol_entries_by_session[session_key] = long_vol_entries_by_session.get(session_key, 0) + 1
                    else:
                        quality["signals_unfilled"] += 1
                        if credit_remains_credit and len(signal.structure.legs) > 1 and fill_draw > package_fill_probability:
                            quality["multileg_order_no_fills"] += 1
                elif contracts > 0:
                    quality["risk_limit_rejections"] += 1

        for trade in newly_opened:
            _, attribution, exposures = mark_and_manage(trade, observation, chain, chain_by_symbol)
            for key in ("delta", "gamma", "vega", "theta"):
                day_attribution[key] += attribution[key]
            if all(state.signed_contracts == 0 for state in trade.legs.values()):
                trade.exit_at = observation.as_of
                trade.exit_reason = "assignment_at_entry"
                close_share_inventory(trade, observation.spot)
                active.remove(trade)
                realized_cash = sum(float(item["pnl"]) for item in closed_trades)
        for key in ("delta", "gamma", "vega", "theta"):
            session_attribution[key] += day_attribution[key]
        seen_observation_count_by_symbol[symbol] = seen_observation_count_by_symbol.get(symbol, 0) + 1
        if force_close and seen_observation_count_by_symbol[symbol] == observation_count_by_symbol[symbol]:
            final_observation = last_observation_by_symbol[symbol]
            for trade in list(active):
                if trade.signal.underlying.upper() != symbol:
                    continue
                close_at_window_end(trade, final_observation, chain_by_symbol)
                active.remove(trade)
                realized_cash = sum(float(item["pnl"]) for item in closed_trades)

    finish_session()

    regime_breakdown = {}
    volatility_breakdown = {}
    for row in curve:
        label = row["regime"] or "unknown"
        cell = regime_breakdown.setdefault(label, {"sessions": 0, "pnl": 0.0, "positive_sessions": 0, "negative_sessions": 0})
        cell["sessions"] += 1
        cell["pnl"] += row["daily_pnl"]
        cell["positive_sessions"] += row["daily_pnl"] > 0
        cell["negative_sessions"] += row["daily_pnl"] < 0
        vol_label = row["volatility_state"] or "unknown"
        vol_cell = volatility_breakdown.setdefault(vol_label, {"sessions": 0, "pnl": 0.0, "positive_sessions": 0, "negative_sessions": 0})
        vol_cell["sessions"] += 1
        vol_cell["pnl"] += row["daily_pnl"]
        vol_cell["positive_sessions"] += row["daily_pnl"] > 0
        vol_cell["negative_sessions"] += row["daily_pnl"] < 0
    attribution_summary = dict(total_attribution)
    if curve and force_close:
        attribution_summary = {
            key: sum(float(row.get("attribution", {}).get(key, 0.0)) for row in curve)
            for key in total_attribution
        }
    active_greeks = [row["greeks"] for row in curve]
    greek_summary = {
        "peak_abs_delta_notional": greek_peaks["delta_notional"],
        "peak_abs_gamma_pnl_1pct": greek_peaks["gamma_pnl_1pct"],
        "peak_abs_vega_per_vol_point": greek_peaks["vega_per_vol_point"],
        "peak_abs_theta_per_day": greek_peaks["theta_per_day"],
        "mean_abs_delta_notional": mean(abs(float(row["delta_notional"])) for row in active_greeks) if active_greeks else 0.0,
        "mean_abs_gamma_pnl_1pct": mean(abs(float(row["gamma_pnl_1pct"])) for row in active_greeks) if active_greeks else 0.0,
        "mean_abs_vega_per_vol_point": mean(abs(float(row["vega_per_vol_point"])) for row in active_greeks) if active_greeks else 0.0,
        "mean_abs_theta_per_day": mean(abs(float(row["theta_per_day"])) for row in active_greeks) if active_greeks else 0.0,
        "pnl_attribution": attribution_summary,
    }
    metrics = _metrics(
        curve, closed_trades, greek_summary, quality,
        float(config.get("robustness", {}).get("gamma_vega_episode_share_threshold", 0.60)),
    )
    return {
        "metrics": metrics,
        "risk_adjusted_score": metrics["risk_adjusted_score"],
        "trades": closed_trades,
        "equity_curve": curve,
        "regime_breakdown": regime_breakdown,
        "volatility_breakdown": volatility_breakdown,
        "greek_summary": greek_summary,
        "assignment_events": [event for trade in closed_trades for event in trade["assignment_events"]],
        "execution": {
            **asdict(execution),
            "slippage_multiplier": slippage_multiplier,
            "spread_width_multiplier": spread_width_multiplier,
            "assignment_enabled": assignment_enabled,
        },
    }


__all__ = ["simulate_options_strategy"]
