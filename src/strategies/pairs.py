"""Signal-only two-leg mean reversion for highly correlated liquid pairs.

The current executor sends independent Alpaca orders, so the runner reports
pair setups but does not submit either leg automatically.
"""

from __future__ import annotations

import math

from src.strategies.indicators import closes
from src.strategies.signals import PairSignal

DEFAULT_PAIRS = (("KO", "PEP"), ("XOM", "CVX"), ("JPM", "BAC"), ("HD", "LOW"))


def _log_prices(bars: list[dict], window: int) -> list[float]:
    values = closes(bars)[-window:]
    return [math.log(value) for value in values]


def candidate(
    left_ticker: str,
    left_bars: list[dict],
    right_ticker: str,
    right_bars: list[dict],
    *,
    window: int = 60,
    minimum_correlation: float = 0.80,
    entry_z: float = 2.0,
) -> PairSignal | None:
    """OLS log-price spread entry when correlation and spread z-score qualify."""
    left = _log_prices(left_bars, window)
    right = _log_prices(right_bars, window)
    size = min(len(left), len(right))
    if size < max(30, window // 2):
        return None
    left, right = left[-size:], right[-size:]
    mean_left = sum(left) / size
    mean_right = sum(right) / size
    var_left = sum((value - mean_left) ** 2 for value in left)
    var_right = sum((value - mean_right) ** 2 for value in right)
    if var_left <= 0 or var_right <= 0:
        return None
    covariance = sum((x - mean_left) * (y - mean_right) for x, y in zip(left, right))
    correlation = covariance / math.sqrt(var_left * var_right)
    if correlation < minimum_correlation:
        return None
    hedge_ratio = covariance / var_left
    intercept = mean_right - hedge_ratio * mean_left
    residuals = [y - (intercept + hedge_ratio * x) for x, y in zip(left, right)]
    spread_mean = sum(residuals) / size
    spread_std = math.sqrt(sum((value - spread_mean) ** 2 for value in residuals) / size)
    if spread_std <= 0:
        return None
    z_score = (residuals[-1] - spread_mean) / spread_std
    if abs(z_score) < entry_z:
        return None
    if z_score < 0:
        long_ticker, short_ticker = right_ticker, left_ticker
    else:
        long_ticker, short_ticker = left_ticker, right_ticker
    return PairSignal(
        long_ticker=long_ticker,
        short_ticker=short_ticker,
        strategy="pairs_stat_arb",
        reason=f"{left_ticker}/{right_ticker} spread z-score {z_score:.2f}",
        z_score=z_score,
        hedge_ratio=hedge_ratio,
    )


def candidates(
    bars_by_symbol: dict[str, list[dict]],
    pairs=DEFAULT_PAIRS,
    *,
    minimum_correlation: float = 0.80,
    entry_z: float = 2.0,
) -> list[PairSignal]:
    output = []
    for left, right in pairs:
        if left not in bars_by_symbol or right not in bars_by_symbol:
            continue
        setup = candidate(
            left,
            bars_by_symbol[left],
            right,
            bars_by_symbol[right],
            minimum_correlation=minimum_correlation,
            entry_z=entry_z,
        )
        if setup:
            output.append(setup)
    return output
