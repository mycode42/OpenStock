"""Strategy-family registry and default rule profiles."""

from __future__ import annotations

from src.strategies import mean_reversion, momentum, opening_range_breakout, trend_following


RULE_MODULES = {
    "mean_reversion": mean_reversion,
    "trend_following": trend_following,
    "momentum": momentum,
    "opening_range_breakout": opening_range_breakout,
}


def evaluate(name: str, mode: str, ticker: str, bars: list[dict], position_qty: float = 0):
    """Call a strategy module with defaults matched to its bar interval."""
    module = RULE_MODULES.get(name)
    if module is None:
        return None
    if name == "opening_range_breakout":
        if mode not in {"day", "algo"}:
            return None
        return module.signal(ticker, bars, position_qty=position_qty)
    if mode in {"day", "algo"}:
        if name == "trend_following":
            return module.signal(ticker, bars, position_qty=position_qty, fast_period=9, slow_period=21)
        if name == "momentum":
            return module.signal(
                ticker, bars, position_qty=position_qty, lookback=12,
                minimum_return=0.015, minimum_volume_ratio=1.2,
            )
        if name == "mean_reversion":
            return module.signal(
                ticker, bars, position_qty=position_qty, window=20,
                band_width=2.0, entry_rsi=30, exit_rsi=58,
            )
    return module.signal(ticker, bars, position_qty=position_qty)


__all__ = ["RULE_MODULES", "evaluate"]
