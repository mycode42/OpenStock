"""Defined-risk option structures and coordinator exposure projections."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
from typing import Iterable

from options.models import GreeksSnapshot, OptionContract, OptionLeg, OptionStructure, RiskMetrics


def aggregate_greeks(legs: Iterable[OptionLeg], contracts: int = 1) -> GreeksSnapshot:
    """Aggregate per-share Greeks into the full structure per spread unit."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    totals = {key: 0.0 for key in ("delta", "gamma", "theta", "vega", "rho")}
    iv_weight, weight = 0.0, 0.0
    observed_at = None
    sources = set()
    for leg in legs:
        scale = leg.signed_ratio * leg.contract.multiplier * contracts
        for key in totals:
            totals[key] += getattr(leg.greeks, key) * scale
        iv_weight += leg.greeks.implied_volatility * abs(scale)
        weight += abs(scale)
        observed_at = max(observed_at, leg.greeks.observed_at) if observed_at else leg.greeks.observed_at
        sources.add(leg.greeks.source)
    return GreeksSnapshot(
        **totals,
        implied_volatility=iv_weight / weight if weight else 0.0,
        observed_at=observed_at or datetime.now(timezone.utc),
        source="+".join(sorted(sources)) or "unknown",
    )


def net_debit_per_share(legs: Iterable[OptionLeg]) -> float:
    """Positive values are debits; negative values are credits."""
    return sum(leg.midpoint * leg.signed_ratio for leg in legs)


def _assignment(legs: Iterable[OptionLeg]) -> tuple[int, str]:
    shares = sum(
        leg.ratio_qty * leg.contract.multiplier
        for leg in legs
        if leg.side == "sell" and leg.contract.exercise_style == "american"
    )
    return shares, "american_short_option" if shares else "none"


def _structure(
    name: str,
    kind: str,
    underlying: str,
    legs: tuple[OptionLeg, ...],
    *,
    max_loss: float,
    max_profit: float | None,
    premium_at_risk: float,
    defined_risk: bool = True,
    underlying_shares: int = 0,
    underlying_cost_basis: float | None = None,
    metadata: dict | None = None,
) -> OptionStructure:
    if not legs:
        raise ValueError("at least one option leg is required")
    multiplier = legs[0].contract.multiplier
    if any(leg.contract.multiplier != multiplier for leg in legs):
        raise ValueError("mixed contract multipliers require an explicit deliverables model")
    net = net_debit_per_share(legs)
    assignment_shares, assignment_risk = _assignment(legs)
    risk = RiskMetrics(
        max_loss=max(0.0, float(max_loss)),
        max_profit=None if max_profit is None else max(0.0, float(max_profit)),
        estimated_margin=max(0.0, float(max_loss)),
        premium_at_risk=max(0.0, float(premium_at_risk)),
        assignment_share_exposure=assignment_shares,
        assignment_risk=assignment_risk,
        defined_risk=defined_risk,
    )
    signature = ":".join(f"{leg.contract.symbol}:{leg.side}:{leg.ratio_qty}" for leg in legs)
    structure_id = f"{name}-{hashlib.sha256(signature.encode()).hexdigest()[:12]}"
    return OptionStructure(
        structure_id=structure_id,
        strategy=name,
        structure_type=kind,
        underlying=underlying.upper(),
        legs=legs,
        net_debit_per_share=net,
        greeks=aggregate_greeks(legs),
        risk=risk,
        created_at=datetime.now(timezone.utc),
        underlying_shares=underlying_shares,
        underlying_cost_basis=underlying_cost_basis,
        metadata=dict(metadata or {}),
    )


def vertical_spread(
    first_leg: OptionLeg,
    second_leg: OptionLeg,
    *,
    name: str = "vertical",
) -> OptionStructure:
    """Build a 1x1 same-right spread; leg order defines debit or credit."""
    if first_leg.contract.right != second_leg.contract.right or first_leg.contract.expiration != second_leg.contract.expiration:
        raise ValueError("vertical legs must have the same right and expiration")
    if first_leg.contract.multiplier != second_leg.contract.multiplier or first_leg.side == second_leg.side:
        raise ValueError("vertical legs must have the same multiplier")
    if first_leg.ratio_qty != second_leg.ratio_qty:
        raise ValueError("vertical legs must have matching ratios")
    low, high = sorted((first_leg, second_leg), key=lambda leg: leg.contract.strike)
    is_debit_orientation = (
        low.side == "buy" and high.side == "sell"
        if first_leg.contract.right == "call"
        else high.side == "buy" and low.side == "sell"
    )
    is_credit_orientation = (
        low.side == "sell" and high.side == "buy"
        if first_leg.contract.right == "call"
        else high.side == "sell" and low.side == "buy"
    )
    if not (is_debit_orientation or is_credit_orientation):
        raise ValueError("vertical sides and strikes do not define a bounded debit or credit spread")
    legs = (first_leg, second_leg)
    net = net_debit_per_share(legs)
    width = abs(first_leg.contract.strike - second_leg.contract.strike)
    multiplier = first_leg.contract.multiplier
    if is_debit_orientation:
        if net < 0:
            raise ValueError("debit spread quotes imply a net credit")
        if net > width:
            raise ValueError("debit spread midpoint exceeds its strike width")
        max_loss, max_profit = net * multiplier, max(0.0, width - net) * multiplier
    else:
        if net > 0:
            raise ValueError("credit spread quotes imply a net debit")
        if -net >= width:
            raise ValueError("credit spread midpoint is inconsistent with its strike width")
        max_loss, max_profit = max(0.0, width + net) * multiplier, -net * multiplier
    kind = "vertical_debit" if is_debit_orientation else "vertical_credit"
    return _structure(name, kind, first_leg.contract.underlying, legs,
                      max_loss=max_loss, max_profit=max_profit,
                      premium_at_risk=max(0.0, net) * multiplier)


def iron_condor(
    long_put: OptionLeg,
    short_put: OptionLeg,
    short_call: OptionLeg,
    long_call: OptionLeg,
    *,
    name: str = "vrp",
) -> OptionStructure:
    legs = (long_put, short_put, short_call, long_call)
    if any(leg.side != expected for leg, expected in zip(legs, ("buy", "sell", "sell", "buy"))):
        raise ValueError("iron condor leg sides must be long put, short put, short call, long call")
    contracts = [leg.contract for leg in legs]
    if any(contract.right != right for contract, right in zip(contracts, ("put", "put", "call", "call"))):
        raise ValueError("iron condor must have two puts and two calls")
    if any(leg.ratio_qty != 1 for leg in legs):
        raise ValueError("initial iron condor implementation supports 1:1 leg ratios only")
    if len({contract.expiration for contract in contracts}) != 1 or len({contract.multiplier for contract in contracts}) != 1:
        raise ValueError("iron condor legs must share expiration and multiplier")
    if not long_put.contract.strike < short_put.contract.strike < short_call.contract.strike < long_call.contract.strike:
        raise ValueError("iron condor strikes must be ordered long put < short put < short call < long call")
    net = net_debit_per_share(legs)
    if net >= 0:
        raise ValueError("iron condor must be entered for a net credit")
    put_width = short_put.contract.strike - long_put.contract.strike
    call_width = long_call.contract.strike - short_call.contract.strike
    multiplier = contracts[0].multiplier
    credit = -net
    if credit >= min(put_width, call_width):
        raise ValueError("iron condor credit is inconsistent with its wing widths")
    max_loss = max(put_width, call_width) * multiplier - credit * multiplier
    return _structure(name, "iron_condor", contracts[0].underlying, legs,
                      max_loss=max_loss, max_profit=credit * multiplier,
                      premium_at_risk=0.0, metadata={"net_credit_per_share": credit})


def straddle(call: OptionLeg, put: OptionLeg, *, direction: str = "long", name: str = "event_driven") -> OptionStructure:
    if call.contract.right != "call" or put.contract.right != "put":
        raise ValueError("straddle requires one call and one put")
    if call.contract.strike != put.contract.strike or call.contract.expiration != put.contract.expiration:
        raise ValueError("straddle legs must share strike and expiration")
    if call.ratio_qty != put.ratio_qty:
        raise ValueError("straddle leg ratios must match")
    if direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    legs = (call, put) if direction == "long" else (
        OptionLeg(call.contract, "sell", call.ratio_qty, bid=call.bid, ask=call.ask, greeks=call.greeks),
        OptionLeg(put.contract, "sell", put.ratio_qty, bid=put.bid, ask=put.ask, greeks=put.greeks),
    )
    net = net_debit_per_share(legs)
    mult = call.contract.multiplier
    if direction == "long":
        return _structure(name, "long_straddle", call.contract.underlying, legs,
                          max_loss=max(0.0, net) * mult, max_profit=None,
                          premium_at_risk=max(0.0, net) * mult)
    # Naked short straddles are modeled for research but rejected by live risk.
    return _structure(name, "short_straddle_undefined", call.contract.underlying, legs,
                      max_loss=0.0, max_profit=max(0.0, -net) * mult,
                      premium_at_risk=0.0, defined_risk=False)


def strangle(put: OptionLeg, call: OptionLeg, *, direction: str = "long", name: str = "event_driven") -> OptionStructure:
    if put.contract.right != "put" or call.contract.right != "call":
        raise ValueError("strangle requires one put and one call")
    if put.contract.strike >= call.contract.strike or put.contract.expiration != call.contract.expiration:
        raise ValueError("strangle requires ordered strikes and a shared expiration")
    if put.ratio_qty != call.ratio_qty:
        raise ValueError("strangle leg ratios must match")
    if direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    legs = (put, call) if direction == "long" else (
        OptionLeg(put.contract, "sell", put.ratio_qty, bid=put.bid, ask=put.ask, greeks=put.greeks),
        OptionLeg(call.contract, "sell", call.ratio_qty, bid=call.bid, ask=call.ask, greeks=call.greeks),
    )
    net = net_debit_per_share(legs)
    mult = put.contract.multiplier
    if direction == "long":
        return _structure(name, "long_strangle", put.contract.underlying, legs,
                          max_loss=max(0.0, net) * mult, max_profit=None,
                          premium_at_risk=max(0.0, net) * mult)
    return _structure(name, "short_strangle_undefined", put.contract.underlying, legs,
                      max_loss=0.0, max_profit=max(0.0, -net) * mult,
                      premium_at_risk=0.0, defined_risk=False)


def protective_put(
    put: OptionLeg,
    *,
    shares: int,
    share_cost_basis: float,
    name: str = "protective_put",
) -> OptionStructure:
    if put.side != "buy" or put.contract.right != "put" or shares <= 0 or share_cost_basis <= 0:
        raise ValueError("protective put requires a long put and positive underlying shares/cost basis")
    if shares % put.contract.multiplier:
        raise ValueError("protective put requires whole contract coverage")
    covered_contracts = shares // put.contract.multiplier
    if covered_contracts != put.ratio_qty:
        raise ValueError("protective put quantity must exactly match the covered shares")
    option_premium = put.midpoint * put.contract.multiplier * covered_contracts
    floor_loss = max(0.0, share_cost_basis - put.contract.strike) * shares + option_premium
    return _structure(name, "protective_put", put.contract.underlying, (put,),
                      max_loss=floor_loss, max_profit=None,
                      premium_at_risk=option_premium, underlying_shares=shares,
                      underlying_cost_basis=share_cost_basis)


def collar(
    put: OptionLeg,
    call: OptionLeg,
    *,
    shares: int,
    share_cost_basis: float,
    name: str = "collar",
) -> OptionStructure:
    if put.side != "buy" or put.contract.right != "put" or call.side != "sell" or call.contract.right != "call":
        raise ValueError("collar requires a bought put and a sold call")
    if put.contract.expiration != call.contract.expiration or put.contract.multiplier != call.contract.multiplier:
        raise ValueError("collar legs must share expiration and multiplier")
    if shares <= 0 or shares % put.contract.multiplier or shares % call.contract.multiplier or share_cost_basis <= 0:
        raise ValueError("collar requires whole covered contract units and positive cost basis")
    units = shares // put.contract.multiplier
    if put.ratio_qty != units or call.ratio_qty != units or put.contract.strike >= call.contract.strike:
        raise ValueError("collar option quantities must match shares and put strike must be below call strike")
    net_option = net_debit_per_share((put, call)) * put.contract.multiplier * units
    max_loss = max(0.0, (share_cost_basis - put.contract.strike) * shares + net_option)
    max_profit = max(0.0, (call.contract.strike - share_cost_basis) * shares - net_option)
    return _structure(name, "collar", put.contract.underlying, (put, call),
                      max_loss=max_loss, max_profit=max_profit,
                      premium_at_risk=max(0.0, net_option), underlying_shares=shares,
                      underlying_cost_basis=share_cost_basis,
                      metadata={"requires_preexisting_shares": shares})


def structure_limit_price(structure: OptionStructure, *, slippage_per_contract: float, contracts: int = 1) -> float:
    """Net debit limit per spread; positive debit, negative credit.

    The added cost moves every trade against the strategy, including credits.
    Alpaca MLeg uses positive net debit and negative net credit notation.
    """
    if slippage_per_contract < 0 or contracts <= 0:
        raise ValueError("slippage and contracts must be non-negative/positive")
    multiplier = structure.legs[0].contract.multiplier
    leg_units = sum(leg.ratio_qty for leg in structure.legs)
    per_share_slippage = slippage_per_contract * leg_units / multiplier
    return round(structure.net_debit_per_share + per_share_slippage, 2)


__all__ = [
    "aggregate_greeks", "net_debit_per_share", "vertical_spread", "iron_condor",
    "straddle", "strangle", "protective_put", "collar", "structure_limit_price",
]
