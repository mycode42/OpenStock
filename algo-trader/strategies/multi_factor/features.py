"""Point-in-time factor features from completed daily bars."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from strategies.multi_factor.config import MultiFactorConfig

try:
    from strategies.multi_factor._features_cy import aggregate_intraday_bars as _aggregate_intraday_bars_cy
except ImportError:  # The extension is optional until the package build compiles it.
    _aggregate_intraday_bars_cy = None


ET = ZoneInfo("America/New_York")
BAR_MINUTES = 5
FUNDAMENTAL_MAX_PERIOD_AGE_DAYS = 550


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


def _available_value(observations: list[dict], as_of: date, key: str | tuple[str, ...]) -> float | None:
    eligible = []
    cutoff = datetime.combine(as_of, time.max, tzinfo=timezone.utc)
    keys = (key,) if isinstance(key, str) else key
    for row in observations:
        try:
            available = datetime.fromisoformat(str(row["available_at"]).replace("Z", "+00:00"))
            if available.tzinfo is None:
                available = available.replace(tzinfo=timezone.utc)
            period_end = row.get("period_end")
            if period_end:
                reported_through = date.fromisoformat(str(period_end)[:10])
                if (as_of - reported_through).days > FUNDAMENTAL_MAX_PERIOD_AGE_DAYS:
                    continue
            else:
                reported_through = date.min
            raw_value = next(row[name] for name in keys if row.get(name) is not None)
            value = float(raw_value)
            if available <= cutoff and math.isfinite(value):
                eligible.append((available, reported_through, value))
        except (KeyError, StopIteration, TypeError, ValueError):
            continue
    return max(eligible, default=(None, None, None), key=lambda item: (item[0], item[1]))[2]


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

        Optional value and quality inputs must include the corresponding ratio
        fields and an ``available_at`` timestamp. Missing PIT facts are omitted
        rather than approximated with price-only proxies.
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
                    volume = float(bar.get("v", bar.get("volume", 0)) or 0)
                    if not math.isfinite(volume) or volume < 0:
                        volume = 0.0
                except (KeyError, TypeError, ValueError):
                    continue
                valid[session] = {"c": close, "v": volume}
            closes = [valid[session] for session in sorted(valid)]
            factors: dict[str, float] = {}
            for name, lookback in self.config.momentum_lookbacks.items():
                if weights.get(name, 0) <= 0 or len(closes) <= lookback:
                    continue
                factors[name] = closes[-1]["c"] / closes[-lookback - 1]["c"] - 1
            volatility_lookback = self.config.volatility_lookback
            if weights.get("low_volatility", 0) > 0 and len(closes) > volatility_lookback:
                returns = [closes[index]["c"] / closes[index - 1]["c"] - 1 for index in range(1, len(closes))]
                recent = returns[-volatility_lookback:]
                volatility = statistics.pstdev(recent)
                if math.isfinite(volatility):
                    factors["low_volatility"] = -volatility
            reversal_lookback = self.config.reversal_lookback
            if weights.get("short_reversal_5d", 0) > 0 and len(closes) > reversal_lookback:
                factors["short_reversal_5d"] = -(closes[-1]["c"] / closes[-reversal_lookback - 1]["c"] - 1)
            if weights.get("price_volume_momentum", 0) > 0 and len(closes) > self.config.liquidity_lookback:
                recent = closes[-self.config.liquidity_lookback - 1:]
                volumes = [row["v"] for row in recent]
                if all(volume > 0 for volume in volumes):
                    typical_volume = statistics.median(volumes[:-1])
                    if typical_volume > 0:
                        volume_weighted_returns = []
                        for index in range(1, len(recent)):
                            relative_volume = min(4.0, max(0.25, recent[index]["v"] / typical_volume))
                            daily_return = recent[index]["c"] / recent[index - 1]["c"] - 1
                            volume_weighted_returns.append(daily_return * relative_volume)
                        factors["price_volume_momentum"] = sum(volume_weighted_returns)
            if weights.get("value_earnings_yield", 0) > 0:
                fundamental_rows = (fundamentals_by_symbol or {}).get(symbol, [])
                value = _available_value(fundamental_rows, as_of, "earnings_yield")
                if value is None and closes:
                    eps = _available_value(
                        fundamental_rows, as_of,
                        ("eps_per_share", "earnings_per_share_diluted", "earnings_per_share_basic"),
                    )
                    value = eps / closes[-1]["c"] if eps is not None else None
                if value is not None:
                    factors["value_earnings_yield"] = value
            for factor, keys in (
                ("value_book_to_price", ("book_to_price", "book_equity_to_market_cap")),
                ("value_fcf_yield", ("free_cash_flow_yield", "fcf_yield")),
                ("quality_profitability", ("gross_profitability", "return_on_invested_capital", "return_on_equity")),
            ):
                if weights.get(factor, 0) <= 0:
                    continue
                fundamental_rows = (fundamentals_by_symbol or {}).get(symbol, [])
                value = _available_value(fundamental_rows, as_of, keys)
                if value is None and closes and factor == "value_book_to_price":
                    book_per_share = _available_value(
                        fundamental_rows, as_of, "book_value_per_share",
                    )
                    value = book_per_share / closes[-1]["c"] if book_per_share is not None else None
                if value is None and closes and factor == "value_fcf_yield":
                    fcf_per_share = _available_value(
                        fundamental_rows, as_of, "free_cash_flow_per_share",
                    )
                    value = fcf_per_share / closes[-1]["c"] if fcf_per_share is not None else None
                if value is not None:
                    factors[factor] = value
            if weights.get("quality_balance_sheet", 0) > 0:
                leverage = _available_value(
                    (fundamentals_by_symbol or {}).get(symbol, []), as_of,
                    ("debt_to_equity", "net_debt_to_ebitda"),
                )
                if leverage is not None:
                    factors["quality_balance_sheet"] = -leverage
            if factors:
                output[symbol] = factors
        return output

    def calculate_with_diagnostics(
        self,
        bars_by_symbol: dict[str, list[dict]],
        *,
        symbols: list[str],
        as_of: date,
        fundamentals_by_symbol: dict[str, list[dict]] | None = None,
        tradable_symbols: set[str] | None = None,
    ) -> tuple[dict[str, dict[str, float]], dict]:
        """Return PIT factors and explicit price, history, liquidity, and external masks."""
        factors = self.calculate(
            bars_by_symbol, symbols=symbols, as_of=as_of,
            fundamentals_by_symbol=fundamentals_by_symbol,
        )
        symbol_metrics: dict[str, dict] = {}
        volume_coverage = 0
        for raw_symbol in symbols:
            symbol = raw_symbol.upper()
            rows = []
            for bar in bars_by_symbol.get(symbol, []):
                session = _bar_date(bar)
                try:
                    close = float(bar["c"])
                except (KeyError, TypeError, ValueError):
                    continue
                if session is None or session > as_of or not math.isfinite(close) or close <= 0:
                    continue
                try:
                    raw_volume = bar.get("v", bar.get("volume"))
                    volume = float(raw_volume) if raw_volume is not None else None
                except (TypeError, ValueError):
                    volume = None
                if volume is not None and (not math.isfinite(volume) or volume < 0):
                    volume = None
                rows.append((session, close, volume))
            rows.sort(key=lambda row: row[0])
            recent_rows = rows[-self.config.liquidity_lookback:]
            volume_rows = [row for row in recent_rows if row[2] is not None]
            volume_complete = (
                len(recent_rows) >= self.config.liquidity_lookback
                and len(volume_rows) == self.config.liquidity_lookback
                and all(row[2] > 0 for row in volume_rows)
            )
            volume_coverage += int(volume_complete)
            dollar_volume = (
                statistics.mean(close * volume for _, close, volume in volume_rows)
                if volume_complete else None
            )
            latest = rows[-1] if rows else None
            symbol_metrics[symbol] = {
                "history_sessions": len(rows),
                "latest_close": latest[1] if latest else None,
                "latest_bar_date": latest[0].isoformat() if latest else None,
                "bar_age_days": (as_of - latest[0]).days if latest else None,
                "average_dollar_volume": dollar_volume,
                "volume_available": volume_complete,
            }

        # Older test fixtures and imported series sometimes contain closes only.
        # In production OHLCV snapshots, enough covered names are required before
        # the dollar-volume rule is enabled; diagnostics make missing coverage visible.
        liquidity_mask_enabled = volume_coverage >= self.config.min_universe
        eligible = set()
        rejected: dict[str, list[str]] = {}
        for symbol in (raw.upper() for raw in symbols):
            metrics = symbol_metrics[symbol]
            reasons = []
            if tradable_symbols is not None and symbol not in {item.upper() for item in tradable_symbols}:
                reasons.append("external_tradability")
            if metrics["latest_close"] is None or metrics["latest_close"] < self.config.minimum_price:
                reasons.append("minimum_price")
            if metrics["history_sessions"] < self.config.minimum_history_sessions:
                reasons.append("minimum_history")
            if metrics["bar_age_days"] is None or metrics["bar_age_days"] > self.config.maximum_bar_age_days:
                reasons.append("stale_daily_bar")
            if liquidity_mask_enabled:
                if not metrics["volume_available"] or metrics["average_dollar_volume"] is None:
                    reasons.append("missing_volume")
                elif metrics["average_dollar_volume"] < self.config.minimum_average_dollar_volume:
                    reasons.append("minimum_dollar_volume")
            if reasons:
                rejected[symbol] = reasons
            else:
                eligible.add(symbol)
        return {symbol: values for symbol, values in factors.items() if symbol in eligible}, {
            "tradability_mask_status": "enforced" if liquidity_mask_enabled else "partial_data",
            "liquidity_mask_enabled": liquidity_mask_enabled,
            "volume_coverage_count": volume_coverage,
            "volume_coverage_required": self.config.min_universe,
            "candidate_count": len(symbols),
            "eligible_count": len(eligible),
            "rejected_count": len(rejected),
            "rejected_reasons": rejected,
            "metrics_by_symbol": symbol_metrics,
        }
