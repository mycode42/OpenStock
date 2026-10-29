"""Causal market-regime features built from completed daily observations."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from datetime import date
from typing import Iterable


FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "core": ("return_20d", "realized_vol_20d", "realized_vol_percentile_252d", "efficiency_ratio_20d"),
    "technical": (
        "return_20d", "realized_vol_percentile_252d", "efficiency_ratio_20d",
        "adx_14", "atr_pct_14", "atr_percentile_252d",
    ),
    "macro": (
        "return_20d", "realized_vol_percentile_252d", "efficiency_ratio_20d",
        "adx_14", "atr_percentile_252d", "vix", "vix_percentile_252d",
        "vix_term_slope", "high_yield_spread", "high_yield_spread_percentile_252d",
        "hyg_lqd_relative_return_20d",
    ),
    "marketwide": (
        "return_20d", "realized_vol_percentile_252d", "efficiency_ratio_20d",
        "adx_14", "atr_pct_14", "atr_percentile_252d", "vix",
        "vix_percentile_252d", "vix_term_slope", "high_yield_spread",
        "high_yield_spread_percentile_252d", "hyg_lqd_relative_return_20d",
        "breadth_positive_20d", "breadth_above_sma50", "average_pairwise_corr_20d",
        "average_pairwise_corr_percentile_252d",
        "median_dollar_volume_20d", "median_dollar_volume_percentile_252d",
    ),
}

MODEL_CORE_FEATURES = ("return_20d", "realized_vol_percentile_252d", "efficiency_ratio_20d")
MODEL_EXTENDED_FEATURES = (
    "return_20d", "realized_vol_percentile_252d", "efficiency_ratio_20d",
    "adx_14", "atr_percentile_252d", "vix_percentile_252d",
    "vix_term_slope",
    "hyg_lqd_relative_return_20d", "breadth_positive_20d",
    "average_pairwise_corr_percentile_252d",
    "median_dollar_volume_percentile_252d",
)

NON_EQUITY_CONTEXT_SYMBOLS = {"SPY", "QQQ", "GLD", "TLT", "HYG", "LQD"}


def _day(bar: dict) -> str | None:
    value = bar.get("date", bar.get("t"))
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _number(bar: dict, key: str, *, positive: bool = False) -> float | None:
    try:
        value = float(bar[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return value


def _ordered_daily(bars: Iterable[dict]) -> list[tuple[str, float, float, float, float, float]]:
    by_day: dict[str, tuple[str, float, float, float, float, float]] = {}
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        day = _day(bar)
        if day is None:
            continue
        opening = _number(bar, "o", positive=True)
        high = _number(bar, "h", positive=True)
        low = _number(bar, "l", positive=True)
        close = _number(bar, "c", positive=True)
        volume = _number(bar, "v")
        if None in (opening, high, low, close, volume):
            continue
        if high < max(opening, close, low) or low > min(opening, close, high) or volume < 0:
            continue
        by_day[day] = (day, opening, high, low, close, volume)
    return [by_day[key] for key in sorted(by_day)]


def _percentile(value: float | None, history: list[float], window: int = 252) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    sample = [item for item in history[-window:] if math.isfinite(item)]
    if len(sample) < 63:
        return None
    return sum(item <= value for item in sample) / len(sample)


def _rolling_std(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    sample = values[-period:]
    mean = sum(sample) / period
    return math.sqrt(sum((value - mean) ** 2 for value in sample) / period)


def _efficiency_ratio(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period + 1:
        return None
    window = closes[-period - 1:]
    path = sum(abs(window[index] - window[index - 1]) for index in range(1, len(window)))
    if path <= 0:
        return 0.0
    return abs(window[-1] - window[0]) / path


def _atr_adx(bars: list[tuple[str, float, float, float, float, float]], period: int = 14) -> tuple[list[float | None], list[float | None]]:
    """Return Wilder ATR and ADX series, aligned to input bars."""
    size = len(bars)
    atr: list[float | None] = [None] * size
    adx: list[float | None] = [None] * size
    if size < period + 1:
        return atr, adx

    true_ranges: list[float] = []
    plus_moves: list[float] = []
    minus_moves: list[float] = []
    for index, row in enumerate(bars):
        _day_key, _opening, high, low, close, _volume = row
        if index == 0:
            true_ranges.append(high - low)
            plus_moves.append(0.0)
            minus_moves.append(0.0)
            continue
        previous_close = bars[index - 1][4]
        previous_high = bars[index - 1][2]
        previous_low = bars[index - 1][3]
        up = high - previous_high
        down = previous_low - low
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        plus_moves.append(up if up > down and up > 0 else 0.0)
        minus_moves.append(down if down > up and down > 0 else 0.0)

    atr_value = sum(true_ranges[:period]) / period
    # The seed at index ``period - 1`` must stop at that completed bar.
    # Including ``period`` here leaked one day's directional movement backward.
    plus_sum = sum(plus_moves[1:period])
    minus_sum = sum(minus_moves[1:period])
    dx_values: list[float] = []
    for index in range(period - 1, size):
        if index > period - 1:
            atr_value = (atr_value * (period - 1) + true_ranges[index]) / period
            plus_sum = plus_sum - plus_sum / period + plus_moves[index]
            minus_sum = minus_sum - minus_sum / period + minus_moves[index]
        atr[index] = atr_value
        tr_sum = atr_value * period
        if tr_sum <= 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * plus_sum / tr_sum
        minus_di = 100.0 * minus_sum / tr_sum
        denominator = plus_di + minus_di
        dx = 0.0 if denominator <= 0 else 100.0 * abs(plus_di - minus_di) / denominator
        dx_values.append(dx)
        if len(dx_values) == period:
            adx_value = sum(dx_values) / period
            adx[index] = adx_value
        elif len(dx_values) > period:
            previous = next((value for value in reversed(adx[:index]) if value is not None), None)
            if previous is not None:
                adx[index] = (previous * (period - 1) + dx) / period
    return atr, adx


def _macro_map(macro_series: dict | None) -> dict[str, list[tuple[str, float]]]:
    output: dict[str, list[tuple[str, float]]] = {}
    for name, rows in (macro_series or {}).items():
        values: list[tuple[str, float]] = []
        if isinstance(rows, dict):
            iterable = rows.items()
        else:
            iterable = ((row.get("date"), row.get("value")) for row in rows if isinstance(row, dict))
        for raw_day, raw_value in iterable:
            try:
                day = date.fromisoformat(str(raw_day)[:10]).isoformat()
                value = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                values.append((day, value))
        output[str(name)] = sorted(dict(values).items())
    return output


def _macro_at(series: list[tuple[str, float]], day: str, max_age_days: int = 7) -> tuple[float | None, list[float]]:
    index = bisect_right(series, (day, float("inf")))
    if index == 0:
        return None, []
    history = series[max(0, index - 252):index]
    age = (date.fromisoformat(day) - date.fromisoformat(series[index - 1][0])).days
    if age > max_age_days:
        return None, [value for _, value in history]
    return series[index - 1][1], [value for _, value in history]


def _daily_returns(prior: list[tuple[str, float, float, float, float, float]], period: int = 20) -> dict[str, float]:
    if len(prior) < period + 1:
        return {}
    prior = prior[-(period + 1):]
    return {
        prior[index][0]: prior[index][4] / prior[index - 1][4] - 1.0
        for index in range(len(prior) - period, len(prior))
        if prior[index - 1][4] > 0
    }


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 10 or len(left) != len(right):
        return None
    count = len(left)
    # statistics.mean converts floats through exact fractions, which is costly
    # here because pairwise correlations call this repeatedly over rolling
    # market histories. fsum keeps stable precision without that conversion.
    left_mean = math.fsum(left) / count
    right_mean = math.fsum(right) / count
    covariance = math.fsum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = math.fsum((value - left_mean) ** 2 for value in left)
    right_var = math.fsum((value - right_mean) ** 2 for value in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return covariance / math.sqrt(left_var * right_var)


def compute_regime_feature_history(
    spy_daily_bars: list[dict],
    *,
    context_bars: dict[str, list[dict]] | None = None,
    macro_series: dict | None = None,
) -> list[dict]:
    """Calculate indicator and cross-market features without looking past each date."""
    spy = _ordered_daily(spy_daily_bars)
    if not spy:
        return []
    contexts = {str(name).upper(): _ordered_daily(rows) for name, rows in (context_bars or {}).items()}
    contexts.setdefault("SPY", spy)
    context_dates = {name: [row[0] for row in rows] for name, rows in contexts.items()}
    macro = _macro_map(macro_series)
    spy_closes = [row[4] for row in spy]
    returns = [spy_closes[index] / spy_closes[index - 1] - 1.0 for index in range(1, len(spy_closes))]
    atr_values, adx_values = _atr_adx(spy)

    rows: list[dict] = []
    realized_history: list[float] = []
    atr_pct_history: list[float] = []
    dollar_volume_history: list[float] = []
    correlation_history: list[float] = []
    for index, bar in enumerate(spy):
        day = bar[0]
        close = bar[4]
        return_20d = close / spy_closes[index - 20] - 1.0 if index >= 20 else None
        seen_returns = returns[:index]
        recent_returns = seen_returns[-20:] if len(seen_returns) >= 20 else []
        rv20 = _rolling_std(recent_returns, 20) if len(recent_returns) >= 20 else None
        er20 = _efficiency_ratio(spy_closes[:index + 1], 20)
        atr = atr_values[index]
        atr_pct = atr / close if atr is not None and close > 0 else None
        rv_pct = _percentile(rv20, realized_history)
        atr_pct_rank = _percentile(atr_pct, atr_pct_history)

        vix, vix_history = _macro_at(macro.get("vix", []), day)
        vix3m, _vix3m_history = _macro_at(macro.get("vix3m", []), day)
        oas, oas_history = _macro_at(macro.get("high_yield_spread", []), day)
        vix_pct = _percentile(vix, vix_history)
        oas_pct = _percentile(oas, oas_history)
        term_slope = (vix / vix3m - 1.0) if vix is not None and vix3m and vix3m > 0 else None

        context_returns: dict[str, dict[str, float]] = {}
        breadth_returns: list[float] = []
        dollar_volumes: list[float] = []
        breadth_above_sma = []
        for symbol, symbol_bars in contexts.items():
            if symbol in NON_EQUITY_CONTEXT_SYMBOLS:
                continue
            end = bisect_right(context_dates[symbol], day)
            if end == 0:
                continue
            prior = symbol_bars[max(0, end - 50):end]
            daily_return_map = _daily_returns(prior, 20)
            if daily_return_map:
                context_returns[symbol] = daily_return_map
                symbol_return = prior[-1][4] / prior[-21][4] - 1.0 if len(prior) >= 21 else None
                if symbol_return is not None:
                    breadth_returns.append(symbol_return)
                if len(prior) >= 50:
                    sma50 = sum(row[4] for row in prior[-50:]) / 50
                    breadth_above_sma.append(prior[-1][4] > sma50)
            recent = prior[-20:]
            if len(recent) >= 15:
                dollar_volumes.append(statistics.median(row[4] * row[5] for row in recent))

        breadth_positive = (
            sum(value > 0 for value in breadth_returns) / len(breadth_returns)
            if len(breadth_returns) >= 5 else None
        )
        breadth_sma = (
            sum(breadth_above_sma) / len(breadth_above_sma)
            if len(breadth_above_sma) >= 5 else None
        )
        correlations = []
        names = sorted(context_returns)
        for left_index, left_name in enumerate(names):
            for right_name in names[left_index + 1:]:
                left_map = context_returns[left_name]
                right_map = context_returns[right_name]
                # Daily-return mappings are inserted chronologically, so keep
                # that order while avoiding a per-pair sort on every session.
                common = [key for key in left_map if key in right_map]
                if len(common) >= 10:
                    corr = _pearson([left_map[key] for key in common], [right_map[key] for key in common])
                    if corr is not None:
                        correlations.append(corr)
        avg_corr = statistics.mean(correlations) if correlations else None
        avg_corr_pct = _percentile(avg_corr, correlation_history)
        median_dollar_volume = statistics.median(dollar_volumes) if dollar_volumes else None
        dollar_volume_pct = _percentile(median_dollar_volume, dollar_volume_history)

        relative_hyg_lqd = None
        hyg_rows = contexts.get("HYG", [])
        lqd_rows = contexts.get("LQD", [])
        hyg_end = bisect_right(context_dates.get("HYG", []), day)
        lqd_end = bisect_right(context_dates.get("LQD", []), day)
        hyg = hyg_rows[max(0, hyg_end - 21):hyg_end]
        lqd = lqd_rows[max(0, lqd_end - 21):lqd_end]
        if len(hyg) >= 21 and len(lqd) >= 21:
            hyg_return = hyg[-1][4] / hyg[-21][4] - 1.0
            lqd_return = lqd[-1][4] / lqd[-21][4] - 1.0
            relative_hyg_lqd = hyg_return - lqd_return

        row = {
            "date": day,
            "return_1d": spy_closes[index] / spy_closes[index - 1] - 1.0 if index else None,
            "return_20d": return_20d,
            "realized_vol_20d": rv20,
            "realized_vol_percentile_252d": rv_pct,
            "efficiency_ratio_20d": er20,
            "atr_14": atr,
            "atr_pct_14": atr_pct,
            "atr_percentile_252d": atr_pct_rank,
            "adx_14": adx_values[index],
            "vix": vix,
            "vix_percentile_252d": vix_pct,
            "vix3m": vix3m,
            "vix_term_slope": term_slope,
            "high_yield_spread": oas,
            "high_yield_spread_percentile_252d": oas_pct,
            "hyg_lqd_relative_return_20d": relative_hyg_lqd,
            "breadth_positive_20d": breadth_positive,
            "breadth_above_sma50": breadth_sma,
            "average_pairwise_corr_20d": avg_corr,
            "average_pairwise_corr_percentile_252d": avg_corr_pct,
            "median_dollar_volume_20d": median_dollar_volume,
            "median_dollar_volume_percentile_252d": dollar_volume_pct,
            "context_symbol_count": len(breadth_returns),
            "correlation_pair_count": len(correlations),
        }
        rows.append(row)
        if rv20 is not None:
            realized_history.append(rv20)
        if atr_pct is not None:
            atr_pct_history.append(atr_pct)
        if median_dollar_volume is not None:
            dollar_volume_history.append(median_dollar_volume)
        if avg_corr is not None:
            correlation_history.append(avg_corr)
    return rows


__all__ = [
    "FEATURE_SETS", "MODEL_CORE_FEATURES", "MODEL_EXTENDED_FEATURES",
    "compute_regime_feature_history",
]
