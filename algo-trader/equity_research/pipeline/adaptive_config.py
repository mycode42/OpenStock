"""Stable parameter domains and scoring defaults for adaptive research."""

from __future__ import annotations

from strategies.multi_factor.config import REGIME_FACTOR_WEIGHT_PROFILES
from equity_research.settings import MULTI_FACTOR_MIN_UNIVERSE_VALUES

MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS = {
    name: 0.1 for name in (
        "momentum_1m", "momentum_3m", "momentum_6m", "low_volatility",
        "price_volume_momentum", "value_earnings_yield", "value_book_to_price",
        "value_fcf_yield", "quality_profitability", "quality_balance_sheet",
    )
}

MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT = 0.005

DEFAULT_FITNESS_WEIGHTS = {
    "sharpe": 0.25,
    "sortino": 0.15,
    "return": 0.25,
    "win_rate": 0.10,
    "profit_factor": 0.15,
    "drawdown": 0.10,
}

ADAPTIVE_REGIMES = ("trending_up", "trending_down", "range_bound", "volatile")

DEFAULT_SEARCH_SPACES: dict[str, dict[str, list]] = {
    "intraday_mean_reversion": {
        "signal_model": ["opening_dip", "bollinger"],
        "minimum_dip_pct": [0.010, 0.014, 0.020],
        "minimum_bounce_pct": [0.001, 0.002, 0.003],
        "minimum_volume_ratio": [1.0, 1.3, 1.7],
        "entry_window_minutes": [30, 60, 90],
        "window": [15, 20, 30], "band_width": [1.8, 2.0, 2.4],
        "entry_rsi": [25, 30, 35], "exit_rsi": [55, 58, 62],
        "regime_filter": ["none", "er", "adx", "combined"],
        "threshold_mode": ["fixed", "volatility_regime_adjusted"],
        "efficiency_ratio_max": [0.25, 0.35, 0.45], "adx_max": [16.0, 20.0, 24.0],
    },
    "intraday_trend_following": {
        "fast_period": [5, 7, 9, 12], "slow_period": [15, 21, 30, 40],
        "confirmation_bars": [1, 2, 3],
        "minimum_slope_pct": [0.0, 0.0005, 0.001, 0.002],
        "allow_long": [True], "allow_short": [False, True],
    },
    "intraday_momentum": {
        "lookback": [5, 10, 12, 16, 20],
        "minimum_return": [0.005, 0.01, 0.015, 0.02],
        "minimum_volume_ratio": [1.0, 1.2, 1.5, 2.0],
        "minimum_short_return": [0.005, 0.01, 0.015, 0.02],
        "minimum_short_volume_ratio": [1.0, 1.2, 1.5, 2.0],
        "max_extension_pct": [0.0, 0.015, 0.025, 0.04],
        "allow_long": [True], "allow_short": [False, True],
    },
    "swing_trend_following": {
        "average_type": ["sma", "ema", "kama"], "stop_mode": ["fixed_pct", "atr"],
        "atr_stop_multiple": [2.0, 2.5, 3.0], "fast_period": [10, 15, 20, 25, 30],
        "slow_period": [40, 50, 60, 70, 80], "minimum_slope_pct": [0.0, 0.001, 0.0025, 0.005],
    },
    "swing_momentum": {
        "momentum_mode": ["cross_sectional", "time_series"],
        "lookbacks": [
            [5, 10, 20], [10, 20, 40], [10, 20, 60],
            [21, 63, 126, 252], [5, 21, 63, 126, 252],
        ],
        "top_n": [2, 3, 4], "minimum_score_spread": [0.02, 0.04, 0.06, 0.08],
        "minimum_time_series_return": [0.0, 0.02, 0.04],
    },
    "swing_mean_reversion": {
        "lookback_days": [10, 15, 20, 25], "stretch_pct": [0.05, 0.07, 0.08, 0.10, 0.12],
        "minimum_stretch_days": [2, 3, 4], "use_rsi_confirmation": [True, False],
        "long_rsi_max": [25, 30, 35], "short_rsi_min": [65, 70, 75],
        "regime_filter": ["none", "er", "adx", "combined"],
        "entry_distance_mode": ["fixed_pct", "standardized"],
        "lookback_mode": ["fixed", "volatility_adaptive"],
        "stretch_zscore": [1.25, 1.75, 2.25],
    },
    "swing_multi_factor": {
        "rebalance_frequency": ["daily", "weekly"], "top_n": [1, 2, 3, 4],
        "min_universe": list(MULTI_FACTOR_MIN_UNIVERSE_VALUES),
        "factor_weight_mode": [
            "static", "ridge_walk_forward", "lightgbm_walk_forward", "xgboost_walk_forward",
        ],
        "minimum_score": [0.0, 0.05, 0.1, 0.2],
        "direction_mode": ["long_only", "regime_dependent", "long_short"],
        "factor_neutralization": ["none", "sector"],
        "portfolio_weight_method": ["equal", "shrinkage_min_variance"],
        "regime_factor_weights": [{}, REGIME_FACTOR_WEIGHT_PROFILES],
        "factor_weights": [
            {"momentum_1m": 0.30, "momentum_3m": 0.30, "momentum_6m": 0.20, "low_volatility": 0.20},
            {"momentum_1m": 0.20, "momentum_3m": 0.30, "momentum_6m": 0.30, "low_volatility": 0.20},
            {"momentum_1m": 0.10, "momentum_3m": 0.25, "momentum_6m": 0.45, "low_volatility": 0.20},
            {"momentum_1m": 0.0, "momentum_3m": 0.0, "momentum_6m": 1.0},
            {"momentum_1m": 0.0, "momentum_3m": 0.50, "momentum_6m": 0.30, "low_volatility": 0.20},
            {"momentum_1m": 0.22, "momentum_3m": 0.27, "momentum_6m": 0.21, "low_volatility": 0.12, "price_volume_momentum": 0.18},
            {"price_volume_momentum": 1.0},
            {"low_volatility": 1.0},
            {"value_earnings_yield": 0.5, "value_book_to_price": 0.25, "value_fcf_yield": 0.25},
            {"quality_profitability": 0.5, "quality_balance_sheet": 0.5},
        ],
    },
}
