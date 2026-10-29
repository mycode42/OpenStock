"""Spread-aware option cost assumptions shared by live pricing and research fills."""

from __future__ import annotations

from dataclasses import dataclass
import math

from options.models import OptionStructure


DEFAULT_SLIPPAGE_FRACTION_OF_SPREAD = 0.5
DEFAULT_MAX_SLIPPAGE_PER_CONTRACT_USD = 25.0


def adverse_slippage_per_share(
    bid: float, ask: float, *, fraction: float, floor_per_contract_usd: float,
    cap_per_contract_usd: float, multiplier: int = 100,
) -> float:
    """Adverse move beyond the quote midpoint for one option leg, per share.

    Per contract the move is ``fraction x (ask - bid) x multiplier``, floored at the
    flat per-contract slippage and capped at the configured maximum. Live limit
    prices, risk cost estimates, and research fills all call this function.
    """
    if not all(math.isfinite(value) for value in (bid, ask, fraction, floor_per_contract_usd, cap_per_contract_usd)):
        raise ValueError("slippage inputs must be finite")
    if bid < 0 or ask < bid or fraction < 0 or floor_per_contract_usd < 0:
        raise ValueError("slippage needs a non-crossed, non-negative quote and non-negative assumptions")
    if cap_per_contract_usd < floor_per_contract_usd or multiplier <= 0:
        raise ValueError("maximum slippage must be at least the floor and multiplier must be positive")
    spread = ask - bid
    per_contract = min(cap_per_contract_usd, max(floor_per_contract_usd, fraction * spread * multiplier))
    return per_contract / multiplier


@dataclass(frozen=True)
class OptionsCostModel:
    """Dollar assumptions per contract/leg, adjustable in the JSON config.

    Commission defaults are intentionally conservative placeholders. Broker fees
    and exchange/regulatory pass-throughs change; paper fills themselves may not
    include them. Slippage is charged beyond the quote midpoint: a fraction of each
    leg's quoted spread, floored at ``slippage_per_contract_usd`` and capped at
    ``max_slippage_per_contract_usd``.
    """

    commission_per_contract_usd: float = 0.65
    slippage_per_contract_usd: float = 5.0
    slippage_fraction_of_spread: float = DEFAULT_SLIPPAGE_FRACTION_OF_SPREAD
    max_slippage_per_contract_usd: float = DEFAULT_MAX_SLIPPAGE_PER_CONTRACT_USD

    def leg_slippage_per_share(self, bid: float, ask: float, *, multiplier: int = 100) -> float:
        return adverse_slippage_per_share(
            bid, ask,
            fraction=self.slippage_fraction_of_spread,
            floor_per_contract_usd=self.slippage_per_contract_usd,
            cap_per_contract_usd=self.max_slippage_per_contract_usd,
            multiplier=multiplier,
        )

    def estimate(self, structure: OptionStructure, contracts: int = 1) -> float:
        if contracts <= 0:
            raise ValueError("contracts must be positive")
        total = 0.0
        for leg in structure.legs:
            units = leg.ratio_qty * contracts
            per_share = self.leg_slippage_per_share(leg.bid, leg.ask, multiplier=leg.contract.multiplier)
            total += units * (self.commission_per_contract_usd + per_share * leg.contract.multiplier)
        return total


__all__ = [
    "DEFAULT_MAX_SLIPPAGE_PER_CONTRACT_USD", "DEFAULT_SLIPPAGE_FRACTION_OF_SPREAD",
    "OptionsCostModel", "adverse_slippage_per_share",
]
