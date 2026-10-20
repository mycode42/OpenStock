"""Shared protocol and immutable context for rule-based options sleeves."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence

from options.data import OptionChain
from options.models import OptionSignal


@dataclass(frozen=True)
class OptionsStrategyContext:
    chain: OptionChain
    underlying_closes: Sequence[tuple]
    regime: str
    volatility_state: str = "unknown"
    events: Sequence[object] = ()
    event_recommendations: Mapping[str, object] = field(default_factory=dict)
    equity_signals: Mapping[str, object] = field(default_factory=dict)
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OptionsStrategy(Protocol):
    strategy_id: str

    def generate(self, context: OptionsStrategyContext) -> OptionSignal | None: ...


def valid_quote(row, *, max_spread_pct: float, min_open_interest: int = 0) -> bool:
    return bool(
        (row.contract.tradable or row.feed in {"model", "historical"})
        and row.bid > 0
        and row.ask >= row.bid
        and row.midpoint > 0
        and row.spread_pct <= max_spread_pct
        and (row.contract.open_interest is None or row.contract.open_interest >= min_open_interest)
    )


def choose_expiration(chain: OptionChain, *, min_dte: int, max_dte: int, target_dte: int | None = None):
    dates = chain.expirations(min_dte=min_dte, max_dte=max_dte)
    if not dates:
        return None
    target = target_dte if target_dte is not None else (min_dte + max_dte) // 2
    return min(dates, key=lambda expiration: abs((expiration - chain.as_of.date()).days - target))


def nearest_delta(rows, target: float, *, predicate=None):
    candidates = [row for row in rows if predicate is None or predicate(row)]
    return min(candidates, key=lambda row: abs(row.greeks.delta - target), default=None)


def regime_risk_multiplier(config: Mapping, regime: str) -> float:
    """Return a configured risk weight; missing regime entries fail closed to zero."""
    weights = config.get("regime_risk_multipliers")
    if not isinstance(weights, Mapping):
        return 1.0
    try:
        value = float(weights.get(str(regime or "unknown"), 0.0))
    except (TypeError, ValueError):
        return 0.0
    return value if 0.0 <= value <= 2.0 else 0.0


__all__ = [
    "OptionsStrategy", "OptionsStrategyContext", "choose_expiration", "nearest_delta",
    "regime_risk_multiplier", "valid_quote",
]
