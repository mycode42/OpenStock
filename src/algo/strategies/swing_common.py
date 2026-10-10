"""Small shared helpers for strategies that consume completed daily bars."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any
from zoneinfo import ZoneInfo

from src.algo.strategies.signals import Signal


ET = ZoneInfo("America/New_York")

try:
    from src.algo.strategies._swing_common_cy import clean_daily_bars as _clean_daily_bars_cy
except ImportError:
    _clean_daily_bars_cy = None


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


def _clean_daily_bar(bar: dict[str, Any]) -> tuple[date, dict[str, Any], float] | None:
    raw = bar.get("date", bar.get("t"))
    try:
        session = raw if isinstance(raw, date) and not isinstance(raw, datetime) else date.fromisoformat(str(raw)[:10])
        close = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    if math.isfinite(close) and close > 0:
        return session, bar, close
    return None


def _clean_daily_bars_uncached(bars: list[dict[str, Any]]) -> list[tuple[date, dict[str, Any], float]]:
    if _clean_daily_bars_cy is not None:
        return _clean_daily_bars_cy(bars)
    rows = []
    previous_session = None
    already_sorted = True
    for bar in bars:
        row = _clean_daily_bar(bar)
        if row is not None:
            if previous_session is not None and row[0] < previous_session:
                already_sorted = False
            previous_session = row[0]
            rows.append(row)
    if not already_sorted:
        rows.sort(key=lambda item: item[0])
    return rows


class CachedDailyBarHistory(list[dict[str, Any]]):
    """Append-only replay history that reuses its normalized daily rows."""

    def __init__(self) -> None:
        super().__init__()
        self._cleaned_rows: list[tuple[date, dict[str, Any], float]] | None = []
        self._signal_results: dict[tuple[Any, ...], Any] = {}

    def append(self, bar: dict[str, Any]) -> None:
        super().append(bar)
        self._signal_results.clear()
        if self._cleaned_rows is None:
            return
        row = _clean_daily_bar(bar)
        if row is None:
            return
        if self._cleaned_rows and row[0] < self._cleaned_rows[-1][0]:
            # Replay histories are chronological; retain the generic helper's
            # sorting behavior if a caller appends an earlier session.
            self._cleaned_rows = None
            return
        self._cleaned_rows.append(row)

    def extend(self, bars) -> None:
        for bar in bars:
            self.append(bar)

    def clear(self) -> None:
        super().clear()
        self._cleaned_rows = []
        self._signal_results.clear()

    def cleaned_rows(self) -> list[tuple[date, dict[str, Any], float]]:
        if self._cleaned_rows is None:
            self._cleaned_rows = _clean_daily_bars_uncached(self)
        # Preserve clean_daily_bars' independent, mutable result for callers.
        return self._cleaned_rows.copy()

    def cached_signal(self, key: tuple[Any, ...]) -> tuple[bool, Any]:
        try:
            return True, self._signal_results[key]
        except KeyError:
            return False, None

    def remember_signal(self, key: tuple[Any, ...], result: Any) -> None:
        self._signal_results[key] = result


def clean_daily_bars(bars: list[dict[str, Any]]) -> list[tuple[date, dict[str, Any], float]]:
    """Return valid daily rows sorted oldest first."""
    if isinstance(bars, CachedDailyBarHistory):
        return bars.cleaned_rows()
    return _clean_daily_bars_uncached(bars)


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
