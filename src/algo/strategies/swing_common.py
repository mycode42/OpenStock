"""Small shared helpers for strategies that consume completed daily bars."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any
from zoneinfo import ZoneInfo

from src.algo.strategies.signals import Signal


ET = ZoneInfo("America/New_York")


class SwingSignal(Signal):
    """Signal with sizing and bracket metadata understood by the Algo runner."""

    def __init__(
        self, ticker: str, action: str, strategy: str, reason: str,
        strength: float = 0.8, stop_pct: float | None = None,
        strategy_group: str | None = "swing", target_weight: float | None = None,
        event_id: str | None = None, *, risk_multiplier: float = 0.75,
        take_profit_price: float | None = None,
    ) -> None:
        super().__init__(ticker, action, strategy, reason, strength, stop_pct,
                         strategy_group, target_weight, event_id)
        object.__setattr__(self, "risk_multiplier", risk_multiplier)
        object.__setattr__(self, "take_profit_price", take_profit_price)

    def to_dict(self) -> dict:
        result = super().to_dict()
        result["risk_multiplier"] = self.risk_multiplier
        result["take_profit_price"] = self.take_profit_price
        return result


def clean_daily_bars(bars: list[dict[str, Any]]) -> list[tuple[date, dict[str, Any], float]]:
    """Return valid daily rows sorted oldest first."""
    rows = []
    for bar in bars:
        raw = bar.get("date", bar.get("t"))
        try:
            session = raw if isinstance(raw, date) and not isinstance(raw, datetime) else date.fromisoformat(str(raw)[:10])
            close = float(bar["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            rows.append((session, bar, close))
    rows.sort(key=lambda item: item[0])
    return rows


def entry_session(value: str | None) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def held_sessions(rows: list[tuple[date, dict, float]], entry_at: str | None) -> int | None:
    opened = entry_session(entry_at)
    if opened is None:
        return None
    return sum(session > opened for session, _, _ in rows)


def position_entry_price(value: object) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


__all__ = ["SwingSignal", "clean_daily_bars", "entry_session", "held_sessions", "position_entry_price"]
