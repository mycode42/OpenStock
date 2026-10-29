"""Basic early-exercise/assignment simulation for American short options."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from options.models import OptionLeg


@dataclass(frozen=True)
class AssignmentEvent:
    symbol: str
    underlying: str
    assigned_contracts: int
    resulting_share_change: int
    intrinsic_value: float
    reason: str


def simulate_early_assignment(
    leg: OptionLeg,
    *,
    contracts: int,
    underlying_price: float,
    as_of: date,
    expected_dividend_per_share: float = 0.0,
    extrinsic_threshold: float = 0.02,
) -> AssignmentEvent | None:
    """Deterministic early exercise heuristic; not a clearing-house simulator.

    Short calls near ex-dividend with extrinsic value below the dividend, and
    short puts deep ITM with near-zero extrinsic, are flagged. Exact assignment
    probabilities and exercise cutoffs need broker-specific event history.
    """
    if leg.side != "sell" or leg.contract.exercise_style != "american" or contracts <= 0:
        return None
    spot, strike = float(underlying_price), leg.contract.strike
    intrinsic = max(spot - strike, 0.0) if leg.contract.right == "call" else max(strike - spot, 0.0)
    if intrinsic <= 0:
        return None
    extrinsic = max(0.0, leg.midpoint - intrinsic)
    dte = (leg.contract.expiration - as_of).days
    dividend_trigger = (
        leg.contract.right == "call"
        and expected_dividend_per_share > 0
        and extrinsic <= expected_dividend_per_share
    )
    expiry_trigger = dte <= 1 and extrinsic <= extrinsic_threshold
    if not (dividend_trigger or expiry_trigger):
        return None
    assigned = contracts * leg.ratio_qty
    share_sign = -1 if leg.contract.right == "call" else 1
    return AssignmentEvent(
        symbol=leg.contract.symbol,
        underlying=leg.contract.underlying,
        assigned_contracts=assigned,
        resulting_share_change=share_sign * assigned * leg.contract.multiplier,
        intrinsic_value=intrinsic,
        reason="short_call_ex_dividend_early_exercise" if dividend_trigger else "near_expiry_itm_extrinsic_threshold",
    )


__all__ = ["AssignmentEvent", "simulate_early_assignment"]
