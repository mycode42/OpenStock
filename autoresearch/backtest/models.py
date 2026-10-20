"""State records used by the deterministic portfolio replay."""

from __future__ import annotations

from dataclasses import dataclass, field

@dataclass
class Position:
    ticker: str
    strategy: str
    qty: int
    entry_price: float
    entry_at: str
    stop_price: float
    take_profit: float
    signal_at: str
    bars_held: int = 0
    satellite_confirmations: list[str] = field(default_factory=list)
    trailing_stop_pct: float | None = None
    trailing_activation_pct: float | None = None
    favorable_extreme: float | None = None
    entry_commission: float = 0.0
    borrow_cost: float = 0.0
    entry_regime: str = "unknown"
