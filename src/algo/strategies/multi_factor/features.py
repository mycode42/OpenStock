"""Point-in-time factor features from completed daily bars."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.algo.strategies.multi_factor.config import MultiFactorConfig

try:
    from src.algo.strategies.multi_factor._features_cy import aggregate_intraday_bars as _aggregate_intraday_bars_cy
except ImportError:  # The extension is optional until the package build compiles it.
    _aggregate_intraday_bars_cy = None


ET = ZoneInfo("America/New_York")
BAR_MINUTES = 5


def _aggregate_unordered_symbol(raw_symbol: str, bars: list[dict]) -> list[dict]:
    """Preserve timestamp-sorted semantics for callers with unordered bars."""
    sessions: dict[date, list[tuple[datetime, dict]]] = defaultdict(list)
    for bar in bars:
        try:
            stamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            local = stamp.astimezone(ET)
            local_time = local.time().replace(tzinfo=None)
            if not time(9, 30) <= local_time < time(16, 0):
                continue
            ingested_at = bar.get("_ingested_at")
            if ingested_at:
                ingested = datetime.fromisoformat(str(ingested_at).replace("Z", "+00:00"))
                if ingested.tzinfo is None:
                    ingested = ingested.replace(tzinfo=timezone.utc)
                if stamp.astimezone(timezone.utc) + timedelta(minutes=BAR_MINUTES) > ingested.astimezone(timezone.utc):
                    continue
            values = {key: float(bar[key]) for key in ("o", "h", "l", "c")}
            volume = float(bar.get("v", 0) or 0)
            if (
                any(not math.isfinite(value) or value <= 0 for value in values.values())
                or not math.isfinite(volume) or volume < 0
                or values["h"] < max(values["o"], values["c"], values["l"])
                or values["l"] > min(values["o"], values["c"], values["h"])
            ):
                continue
            sessions[local.date()].append((stamp, {**values, "v": volume}))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    daily = []
    for session, rows in sorted(sessions.items()):
        rows.sort(key=lambda item: item[0])
        candles = [row for _, row in rows]
        daily.append({
            "date": session.isoformat(),
            "o": candles[0]["o"], "h": max(row["h"] for row in candles),
            "l": min(row["l"] for row in candles), "c": candles[-1]["c"],
            "v": sum(row["v"] for row in candles),
        })
    return daily


def _aggregate_intraday_bars_python(bars_by_symbol: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Python fallback for ordered and unordered regular-session bars."""
    output = {}
    for raw_symbol, bars in bars_by_symbol.items():
        sessions: dict[date, dict] = {}
        previous_stamp: datetime | None = None
        unordered = False
        for bar in bars:
            try:
                stamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if previous_stamp is not None and stamp < previous_stamp:
                    unordered = True
                    break
                previous_stamp = stamp
                local = stamp.astimezone(ET)
                local_time = local.time().replace(tzinfo=None)
                if not time(9, 30) <= local_time < time(16, 0):
                    continue
                ingested_at = bar.get("_ingested_at")
                if ingested_at:
                    ingested = datetime.fromisoformat(str(ingested_at).replace("Z", "+00:00"))
                    if ingested.tzinfo is None:
                        ingested = ingested.replace(tzinfo=timezone.utc)
                    if stamp.astimezone(timezone.utc) + timedelta(minutes=BAR_MINUTES) > ingested.astimezone(timezone.utc):
                        continue
                opening = float(bar["o"])
                high = float(bar["h"])
                low = float(bar["l"])
                close = float(bar["c"])
                volume = float(bar.get("v", 0) or 0)
                if (
                    not math.isfinite(opening) or opening <= 0
                    or not math.isfinite(high) or high <= 0
                    or not math.isfinite(low) or low <= 0
                    or not math.isfinite(close) or close <= 0
                    or not math.isfinite(volume) or volume < 0
                    or high < max(opening, close, low)
                    or low > min(opening, close, high)
                ):
                    continue
                session = local.date()
                state = sessions.get(session)
                if state is None:
                    sessions[session] = {
                        "first_stamp": stamp, "open": opening,
                        "last_stamp": stamp, "close": close,
                        "high": high, "low": low, "volume": volume,
                    }
                else:
                    state["high"] = max(state["high"], high)
                    state["low"] = min(state["low"], low)
                    state["volume"] += volume
                    if stamp < state["first_stamp"]:
                        state["first_stamp"] = stamp
                        state["open"] = opening
                    if stamp >= state["last_stamp"]:
                        state["last_stamp"] = stamp
                        state["close"] = close
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if unordered:
            output[raw_symbol.upper()] = _aggregate_unordered_symbol(raw_symbol, bars)
            continue
        output[raw_symbol.upper()] = [
            {
                "date": session.isoformat(), "o": state["open"],
                "h": state["high"], "l": state["low"],
                "c": state["close"], "v": state["volume"],
            }
            for session, state in sorted(sessions.items())
        ]
    return output


def aggregate_intraday_bars(bars_by_symbol: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Aggregate complete regular-session 5-minute bars into daily OHLCV."""
    if _aggregate_intraday_bars_cy is not None:
        return _aggregate_intraday_bars_cy(bars_by_symbol, ET, _aggregate_unordered_symbol)
    return _aggregate_intraday_bars_python(bars_by_symbol)


def _bar_date(bar: dict[str, Any]) -> date | None:
    raw = bar.get("date", bar.get("t"))
    if raw is None:
        return None
    try:
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return stamp.date()
    except (TypeError, ValueError):
        return None


def _available_value(observations: list[dict], as_of: date, key: str) -> float | None:
    eligible = []
    cutoff = datetime.combine(as_of, time.max, tzinfo=timezone.utc)
    for row in observations:
        try:
            available = datetime.fromisoformat(str(row["available_at"]).replace("Z", "+00:00"))
            if available.tzinfo is None:
                available = available.replace(tzinfo=timezone.utc)
            value = float(row[key])
            if available <= cutoff and math.isfinite(value):
                eligible.append((available, value))
        except (KeyError, TypeError, ValueError):
            continue
    return max(eligible, default=(None, None), key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc))[1]


class FactorCalculator:
    """Calculate a few explicit return and risk factors for each symbol."""

    def __init__(self, config: MultiFactorConfig | None = None):
        self.config = config or MultiFactorConfig()

    def calculate(
        self,
        bars_by_symbol: dict[str, list[dict]],
        *,
        symbols: list[str],
        as_of: date,
        fundamentals_by_symbol: dict[str, list[dict]] | None = None,
    ) -> dict[str, dict[str, float]]:
        """Use only daily bars dated on or before ``as_of``.

        Optional value inputs must provide earnings_yield and an available_at
        timestamp. Without point-in-time fundamental rows the value factor is
        omitted rather than approximated with a price-only proxy.
        """
        output: dict[str, dict[str, float]] = {}
        weights = self.config.factor_weights
        for raw_symbol in symbols:
            symbol = raw_symbol.upper()
            valid = {}
            for bar in bars_by_symbol.get(symbol, []):
                session = _bar_date(bar)
                if session is None or session > as_of:
                    continue
                try:
                    close = float(bar["c"])
                    if not math.isfinite(close) or close <= 0:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                valid[session] = close
            closes = [valid[session] for session in sorted(valid)]
            factors: dict[str, float] = {}
            for name, lookback in self.config.momentum_lookbacks.items():
                if weights.get(name, 0) <= 0 or len(closes) <= lookback:
                    continue
                factors[name] = closes[-1] / closes[-lookback - 1] - 1
            volatility_lookback = self.config.volatility_lookback
            if weights.get("low_volatility", 0) > 0 and len(closes) > volatility_lookback:
                returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
                recent = returns[-volatility_lookback:]
                volatility = statistics.pstdev(recent)
                if math.isfinite(volatility):
                    factors["low_volatility"] = -volatility
            reversal_lookback = self.config.reversal_lookback
            if weights.get("short_reversal_5d", 0) > 0 and len(closes) > reversal_lookback:
                factors["short_reversal_5d"] = -(closes[-1] / closes[-reversal_lookback - 1] - 1)
            if weights.get("value_earnings_yield", 0) > 0:
                value = _available_value(
                    (fundamentals_by_symbol or {}).get(symbol, []), as_of, "earnings_yield",
                )
                if value is not None:
                    factors["value_earnings_yield"] = value
            if factors:
                output[symbol] = factors
        return output
