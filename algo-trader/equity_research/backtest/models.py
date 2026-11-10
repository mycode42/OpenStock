"""State records used by the deterministic portfolio replay."""

from __future__ import annotations

from dataclasses import dataclass

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
    trailing_stop_pct: float | None = None
    trailing_activation_pct: float | None = None
    favorable_extreme: float | None = None
    entry_commission: float = 0.0
    borrow_cost: float = 0.0
    entry_regime: str = "unknown"
    # Price before spread and slippage, so the cost split can be reported.
    entry_raw_price: float | None = None
    # Best and worst prices reached while held, for excursion analysis.
    max_high_price: float | None = None
    min_low_price: float | None = None
    # Signal strength and the measurements taken at the signal bar, for the trade records.
    entry_strength: float | None = None
    entry_features: tuple = ()
