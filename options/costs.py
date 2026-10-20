"""Configurable round-trip cost assumptions for option simulations."""

from __future__ import annotations

from dataclasses import dataclass

from options.models import OptionStructure


@dataclass(frozen=True)
class OptionsCostModel:
    """Dollar assumptions per contract/leg, adjustable in the JSON config.

    Commission defaults are intentionally conservative placeholders. Broker fees
    and exchange/regulatory pass-throughs change; paper fills themselves may not
    include them. Slippage is charged in addition to the quote midpoint.
    """

    commission_per_contract_usd: float = 0.65
    slippage_per_contract_usd: float = 5.0

    def estimate(self, structure: OptionStructure, contracts: int = 1) -> float:
        if contracts <= 0:
            raise ValueError("contracts must be positive")
        units = sum(leg.ratio_qty for leg in structure.legs) * contracts
        return units * (self.commission_per_contract_usd + self.slippage_per_contract_usd)

    def estimated_leg_fill(self, midpoint: float, *, side: str, multiplier: int = 100) -> float:
        """Conservative single-leg fill estimate beyond midpoint."""
        if midpoint < 0 or multiplier <= 0 or side not in {"buy", "sell"}:
            raise ValueError("invalid estimated fill inputs")
        adjustment = self.slippage_per_contract_usd / multiplier
        return max(0.0, midpoint + adjustment if side == "buy" else midpoint - adjustment)


__all__ = ["OptionsCostModel"]
