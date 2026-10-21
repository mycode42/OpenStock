"""Path-aware options portfolio simulator with per-leg conservative execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from bisect import bisect_left
import hashlib
import math
import random
from statistics import mean, median, pstdev

from options.assignment import simulate_early_assignment
from options.data import ChainQuote, OptionChain
from options.models import GreeksSnapshot, OptionLeg, OptionSignal
from options.research.dataset import OptionsResearchDataset, ResearchObservation
from options.research.execution import (
    ExecutionAssumptions, assess_structure_liquidity, structure_entry_cost,
)
from options.research.search import Candidate
from options.structures import OptionStructure
from strategies.options.base import OptionsStrategyContext
from strategies.options.long_volatility import historical_iv_percentile
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
    contracts: int
    entry_at: datetime
    entry_regime: str
    entry_spot: float
    entry_debit: float = 0.0
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
    missing_fraction = quality.get("missing_chain_observations", 0) / max(1, len(curve))
    risk_adjusted = sharpe - 2.0 * max_drawdown - 2.0 * missing_fraction
    # A no-trade candidate has supplied no evidence of an exploitable edge.
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


def simulate_options_strategy(
    dataset: OptionsResearchDataset,
    observations: tuple[ResearchObservation, ...],
    *,
    strategy_name: str,
    candidate: Candidate,
    config: dict,
    base_config: dict,
    slippage_multiplier: float = 1.0,
    spread_width_multiplier: float = 1.0,
    assignment_enabled: bool = True,
    force_close: bool = True,
) -> dict:
    """Run one options sleeve over a chronological slice; no equity pipeline calls."""
    if not observations:
        raise ValueError("simulation slice is empty")
    execution_values = dict(config.get("execution", {}))
    execution_values["spread_fraction_per_leg"] = float(execution_values.get("spread_fraction_per_leg", 0.5)) * slippage_multiplier
    execution = ExecutionAssumptions.from_dict(execution_values)
    market = config.get("market_data", {})
    strategy_parameters = dict(candidate.parameters)
    if strategy_name == "long_volatility" and "underlyings" not in strategy_parameters:
        strategy_parameters["underlyings"] = sorted({item.underlying.upper() for item in observations})
    strategy, strategy_section = make_strategy(
        strategy_name,
        parameters=strategy_parameters,
        allowed_regimes=candidate.allowed_regimes,
        base_config=base_config,
    )
    date_to_index = {row.as_of: index for index, row in enumerate(dataset.observations)}
    iv_history_by_underlying: dict[str, tuple[list[datetime], list[float]]] = {}
    if strategy_name == "long_volatility":
        from strategies.options.long_volatility import atm_implied_volatility
        histories: dict[str, list[tuple[datetime, float]]] = {}
        for historical in dataset.observations:
            if historical.chain is None:
                continue
            snapshot_index = date_to_index.get(historical.chain.as_of)
            observation_index = date_to_index.get(historical.as_of)
            if (
                snapshot_index is None or observation_index is None
                or snapshot_index > observation_index
                or observation_index - snapshot_index > int(market.get("maximum_snapshot_age_sessions", 1))
            ):
                continue
            sample = atm_implied_volatility(historical.chain, strategy_section)
            if sample is not None:
                histories.setdefault(historical.underlying.upper(), []).append((historical.as_of, sample))
        iv_history_by_underlying = {
            symbol: (
                [stamp for stamp, _value in ordered],
                [value for _stamp, value in ordered],
            )
            for symbol, values in histories.items()
            for ordered in [sorted(values)]
        }
    initial_equity = float(config.get("starting_equity", 100000))
    if initial_equity <= 0 or not math.isfinite(initial_equity):
        raise ValueError("starting_equity must be positive and finite")
    max_contracts = int(execution_values.get("maximum_contracts_per_trade", 1))
    risk_per_trade = float(execution_values.get("max_risk_per_trade_pct", 0.005))
    if strategy_name == "long_volatility":
        risk_per_trade = min(
            risk_per_trade,
            float(strategy_section.get("max_risk_per_trade_pct", risk_per_trade)),
        )
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
    long_vol_entry_session = None
    long_vol_entries_this_session = 0
    total_attribution = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "residual": 0.0}

    def current_chain(observation: ResearchObservation):
        chain = observation.chain
        if chain is None:
            quality["missing_chain_observations"] += 1
            return None
        snapshot_index = date_to_index.get(chain.as_of)
        current_index = date_to_index.get(observation.as_of)
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
            "strategy": strategy_name,
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
            if quote is None:
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
        return equity_value, daily_attribution, exposures

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
            quote = quote or state.last_quote
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

    for obs_index, observation in enumerate(observations):
        observation_session = observation.as_of.date()
        if observation_session != long_vol_entry_session:
            long_vol_entry_session = observation_session
            long_vol_entries_this_session = 0
        chain = current_chain(observation)
        if chain is not None:
            chain = _widen_chain(chain, spread_width_multiplier)
        chain_by_symbol = chain.by_symbol() if chain is not None else {}
        day_attribution = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "residual": 0.0}
        daily_greeks = {key: 0.0 for key in greek_peaks}
        newly_opened: list[_OpenTrade] = []
        for trade in list(active):
            marked_value, attribution, exposures = mark_and_manage(trade, observation, chain, chain_by_symbol)
            for key in ("delta", "gamma", "vega", "theta"):
                day_attribution[key] += attribution[key]
            for key in daily_greeks:
                daily_greeks[key] += exposures[key]
            if all(state.signed_contracts == 0 for state in trade.legs.values()):
                trade.exit_at = observation.as_of
                trade.exit_reason = "assignment_or_expiration"
                close_share_inventory(trade, observation.spot)
                active.remove(trade)
                realized_cash = sum(float(item["pnl"]) for item in closed_trades)
            else:
                exit_reason = None
                if strategy_name == "long_volatility":
                    held_days = max(0, (observation.as_of.date() - trade.entry_at.date()).days)
                    open_dtes = [
                        (state.contract.expiration - observation.as_of.date()).days
                        for state in trade.legs.values() if state.signed_contracts
                    ]
                    package_value = marked_value - trade.cashflow - trade.shares * observation.spot
                    exit_reason = strategy.exit_reason(
                        entry_debit=trade.entry_debit,
                        current_value=package_value,
                        held_days=held_days,
                        days_to_expiry=min(open_dtes, default=0),
                        regime=observation.regime,
                        entry_regime=trade.entry_regime,
                        config=strategy_section,
                    )
                expired = all(state.contract.expiration <= observation.as_of.date() for state in trade.legs.values() if state.signed_contracts)
                if exit_reason:
                    close_at_window_end(
                        trade, observation, chain_by_symbol, exit_reason=exit_reason,
                    )
                    active.remove(trade)
                    realized_cash = sum(float(item["pnl"]) for item in closed_trades)
                elif expired:
                    for symbol, state in trade.legs.items():
                        if state.signed_contracts:
                            settle_leg(trade, symbol, state, observation.spot, observation.as_of, reason="expiration_assignment")
                    trade.exit_at = observation.as_of
                    trade.exit_reason = "expiration"
                    close_share_inventory(trade, observation.spot)
                    active.remove(trade)
                    realized_cash = sum(float(item["pnl"]) for item in closed_trades)

        daily_entry_room = (
            strategy_name != "long_volatility"
            or long_vol_entries_this_session < int(strategy_section.get("max_entries_per_day", 1))
        )
        if chain is not None and len(chain.contracts) >= minimum_chain_contracts and not active and daily_entry_room:
            threshold_activation = (
                strategy_name == "long_volatility"
                and strategy_section.get("activation_mode") == "regime_or_realized_vol"
            )
            if threshold_activation or _regime_allowed(observation.regime, observation.volatility_state, candidate.allowed_regimes):
                dte_min = max(0, int(strategy_section.get("min_dte", 0)))
                dte_max = int(strategy_section.get("max_dte", 730))
                closes = dataset.provider.underlying_closes(
                    observation.underlying, as_of=observation.as_of,
                    sessions=max(3, int(strategy_section.get("realized_lookback_sessions", 0)) + 1),
                )
                context = OptionsStrategyContext(
                    chain=chain,
                    underlying_closes=closes,
                    regime=observation.regime,
                    volatility_state=observation.volatility_state,
                    events=observation.events,
                    event_recommendations=observation.event_recommendations,
                    equity_signals=observation.equity_signals,
                    as_of=observation.as_of,
                )
                if strategy_name == "long_volatility":
                    atm_iv = strategy.atm_implied_volatility(chain, strategy_section)
                    prior_stamps, prior_values = iv_history_by_underlying.get(
                        observation.underlying.upper(), ([], []),
                    )
                    history_end = bisect_left(prior_stamps, observation.as_of)
                    lookback = int(strategy_section.get("iv_percentile_lookback_sessions", 252))
                    prior_values_window = prior_values[max(0, history_end - lookback):history_end]
                    percentile = historical_iv_percentile(
                        atm_iv,
                        prior_values_window,
                        min_samples=int(strategy_section.get("iv_rank_min_samples", 20)),
                    )
                    context = replace(
                        context,
                        iv_percentile=percentile,
                        iv_percentile_samples=len(prior_values_window),
                        iv_percentile_source="historical_chain_atm_iv",
                    )
                    if percentile is None:
                        quality["iv_rank_warmup_skips"] += 1
                try:
                    signal = strategy.generate(context)
                except (ValueError, ArithmeticError, OverflowError):
                    signal = None
                if signal is not None:
                    quality["signals_seen"] += 1
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
                                if limit <= 0:
                                    contracts = 0
                                    greek_rejected = True
                                    break
                                contracts = min(contracts, int(limit // exposure_abs))
                        if signal.structure.risk.assignment_share_exposure * contracts > int(greek_limits.get("max_assignment_shares", 200)):
                            contracts = 0
                            greek_rejected = True
                        if contracts < 1:
                            quality["greek_limit_rejections"] += int(greek_rejected)
                            quality["signals_unfilled"] += 1
                        entry_costs = structure_entry_cost(signal.structure, chain_by_symbol, execution, contracts) if contracts > 0 else 0.0
                        regime_weight = float(signal.data_quality.get("regime_risk_multiplier", 1.0))
                        budget = initial_equity * risk_per_trade * regime_weight
                        total_open_risk = signal.structure.risk.max_loss * contracts + entry_costs
                        if contracts > 0 and signal.structure.risk.defined_risk and total_open_risk <= budget and total_open_risk <= initial_equity * max_total_risk:
                            fill_key = f"entry:{observation.as_of.date()}:{observation.underlying}"
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
                                    contracts=contracts,
                                    entry_at=observation.as_of,
                                    entry_regime=observation.regime,
                                    entry_spot=observation.spot,
                                    entry_debit=max(0.0, -gross_entry_cashflow),
                                    cashflow=entry_cashflow,
                                    legs=states,
                                    entry_costs=entry_cost_total,
                                    last_spot=observation.spot,
                                ))
                                newly_opened.append(active[-1])
                                quality["signals_filled"] += 1
                                if strategy_name == "long_volatility":
                                    long_vol_entries_this_session += 1
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
            for key in daily_greeks:
                daily_greeks[key] += exposures[key]
            if all(state.signed_contracts == 0 for state in trade.legs.values()):
                trade.exit_at = observation.as_of
                trade.exit_reason = "assignment_at_entry"
                close_share_inventory(trade, observation.spot)
                active.remove(trade)
                realized_cash = sum(float(item["pnl"]) for item in closed_trades)
        for key, value in daily_greeks.items():
            greek_peaks[key] = max(greek_peaks[key], abs(value))
        current_equity = initial_equity + realized_cash + sum(
            trade.cashflow
            + trade.shares * observation.spot
            + sum(
                state.signed_contracts * execution.exit_fill(state.last_quote, state.signed_contracts) * state.contract.multiplier
                for state in trade.legs.values()
                if state.signed_contracts and state.last_quote is not None
            )
            for trade in active
        )
        daily_pnl = current_equity - last_equity
        # Daily attribution also includes commissions, spread crossing, and
        # exercise settlement in the residual term.
        attribution_total = sum(day_attribution.values())
        day_attribution["residual"] = daily_pnl - attribution_total
        for key in total_attribution:
            total_attribution[key] += day_attribution[key]
        curve.append({
            "date": observation.as_of.isoformat(),
            "equity": current_equity,
            "daily_pnl": daily_pnl,
            "return": daily_pnl / last_equity if last_equity else 0.0,
            "regime": observation.regime,
            "volatility_state": observation.volatility_state,
            "greeks": dict(daily_greeks),
            "attribution": dict(day_attribution),
            "open_structures": len(active),
        })
        last_equity = current_equity

    if force_close and active:
        observation = observations[-1]
        chain = current_chain(observation)
        if chain is not None:
            chain = _widen_chain(chain, spread_width_multiplier)
        chain_by_symbol = chain.by_symbol() if chain else {}
        for trade in list(active):
            close_at_window_end(trade, observation, chain_by_symbol)
            active.remove(trade)
        realized_cash = sum(float(item["pnl"]) for item in closed_trades)
        current_equity = initial_equity + realized_cash
        if curve:
            prior = curve[-1]["equity"]
            curve[-1]["daily_pnl"] = current_equity - (curve[-2]["equity"] if len(curve) > 1 else initial_equity)
            curve[-1]["equity"] = current_equity
            curve[-1]["return"] = curve[-1]["daily_pnl"] / (curve[-2]["equity"] if len(curve) > 1 else initial_equity)
            curve[-1]["attribution"]["residual"] += current_equity - prior

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
