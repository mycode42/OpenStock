"""Conservative per-leg research fills and liquidity checks."""

from __future__ import annotations

from dataclasses import dataclass
import math

from options.data import ChainQuote
from options.models import OptionStructure


@dataclass(frozen=True)
class LiquidityDecision:
    eligible: bool
    reasons: tuple[str, ...]
    average_spread_pct: float
    missing_open_interest_legs: int
    low_open_interest_legs: int


@dataclass(frozen=True)
class ExecutionAssumptions:
    spread_fraction_per_leg: float = 0.50
    commission_per_contract_usd: float = 0.65
    exercise_fee_per_contract_usd: float = 0.15
    stock_commission_per_share_usd: float = 0.005
    base_fill_probability: float = 0.80
    multi_leg_fill_probability: float = 0.80

    def __post_init__(self) -> None:
        numeric = (
            self.spread_fraction_per_leg, self.commission_per_contract_usd,
            self.exercise_fee_per_contract_usd, self.stock_commission_per_share_usd,
            self.base_fill_probability, self.multi_leg_fill_probability,
        )
        if any(not math.isfinite(float(value)) or value < 0 for value in numeric):
            raise ValueError("execution assumptions must be finite and non-negative")
        if not 0 <= self.base_fill_probability <= 1:
            raise ValueError("base_fill_probability must be in [0, 1]")
        if not 0 <= self.multi_leg_fill_probability <= 1:
            raise ValueError("multi_leg_fill_probability must be in [0, 1]")

    @classmethod
    def from_dict(cls, values: dict) -> "ExecutionAssumptions":
        return cls(
            spread_fraction_per_leg=float(values.get("spread_fraction_per_leg", 0.5)),
            commission_per_contract_usd=float(values.get("commission_per_contract_usd", 0.65)),
            exercise_fee_per_contract_usd=float(values.get("exercise_fee_per_contract_usd", 0.15)),
            stock_commission_per_share_usd=float(values.get("stock_commission_per_share_usd", 0.005)),
            base_fill_probability=float(values.get("base_fill_probability", 0.8)),
            multi_leg_fill_probability=float(values.get("multi_leg_fill_probability", 0.8)),
        )

    def package_fill_probability(self, leg_count: int) -> float:
        """Completion probability for one atomic multi-leg order.

        The simulator assumes broker-style package fills, so it never creates
        unhedged leg exposure. The lower configurable probability represents
        the harder fill of a complex order and results in a no-fill outcome.
        """
        return self.base_fill_probability if leg_count <= 1 else self.base_fill_probability * self.multi_leg_fill_probability

    def commission(self, contracts: int, leg_units: int) -> float:
        return self.commission_per_contract_usd * contracts * leg_units

    def entry_fill(self, quote: ChainQuote, side: str) -> float:
        return self._fill(quote, side)

    def exit_fill(self, quote: ChainQuote, signed_contracts: int) -> float:
        # A long position sells to close; a short position buys to close.
        return self._fill(quote, "sell" if signed_contracts > 0 else "buy")

    def _fill(self, quote: ChainQuote, side: str) -> float:
        if side not in {"buy", "sell"} or quote.bid <= 0 or quote.ask < quote.bid:
            raise ValueError("two-sided positive quotes are required for research fills")
        midpoint = (quote.bid + quote.ask) / 2.0
        adverse = (quote.ask - quote.bid) * self.spread_fraction_per_leg
        # Fractions above 0.5 deliberately model fills worse than the displayed
        # ask/bid in stress runs; this is not an execution guarantee.
        price = midpoint + adverse if side == "buy" else midpoint - adverse
        return max(0.0, price)


def assess_structure_liquidity(
    structure: OptionStructure,
    chain_by_symbol: dict[str, ChainQuote],
    *,
    maximum_spread_pct: float,
    minimum_open_interest: int,
    require_open_interest: bool = True,
) -> LiquidityDecision:
    reasons = []
    spreads = []
    missing_oi = low_oi = 0
    for leg in structure.legs:
        quote = chain_by_symbol.get(leg.contract.symbol)
        if quote is None:
            reasons.append(f"missing_quote:{leg.contract.symbol}")
            continue
        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            reasons.append(f"invalid_quote:{leg.contract.symbol}")
            continue
        spread = quote.spread_pct
        spreads.append(spread)
        if not math.isfinite(spread) or spread > maximum_spread_pct:
            reasons.append(f"wide_spread:{leg.contract.symbol}:{spread:.4f}")
        open_interest = leg.contract.open_interest
        if open_interest is None:
            missing_oi += 1
            if require_open_interest:
                reasons.append(f"missing_open_interest:{leg.contract.symbol}")
        elif open_interest < minimum_open_interest:
            low_oi += 1
            reasons.append(f"low_open_interest:{leg.contract.symbol}:{open_interest}")
    return LiquidityDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        average_spread_pct=sum(spreads) / len(spreads) if spreads else math.inf,
        missing_open_interest_legs=missing_oi,
        low_open_interest_legs=low_oi,
    )


def structure_entry_cost(
    structure: OptionStructure,
    chain_by_symbol: dict[str, ChainQuote],
    execution: ExecutionAssumptions,
    contracts: int,
) -> float:
    total = 0.0
    for leg in structure.legs:
        quote = chain_by_symbol[leg.contract.symbol]
        fill = execution.entry_fill(quote, leg.side)
        midpoint = (quote.bid + quote.ask) / 2.0
        total += (fill - midpoint) * leg.contract.multiplier * leg.ratio_qty * contracts
    return max(0.0, total) + execution.commission(
        contracts, sum(leg.ratio_qty for leg in structure.legs),
    )


__all__ = [
    "ExecutionAssumptions", "LiquidityDecision", "assess_structure_liquidity",
    "structure_entry_cost",
]
