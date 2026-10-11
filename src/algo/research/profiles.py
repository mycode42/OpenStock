"""Versioned, bounded parameter search spaces for Algo strategy research.

The search bank changes signal-selection parameters only. Execution costs,
position sizing, stops, portfolio limits, and coordinator policy continue to
come from the live Algo configuration during replay.
"""

from __future__ import annotations

from copy import deepcopy

from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS, validate_parameter_profile

from .settings import CORE_RESEARCH_CONTROLS, RESEARCHABLE_STRATEGIES, STRATEGY_FLAVORS


SEARCH_SPACE_VERSION = "2026-10-11.1"

# Each row is a coherent strategy profile, rather than a Cartesian product of
# every parameter value. That keeps the search reviewable and compute bounded
# while giving walk-forward selection materially different signal behaviors.
STRATEGY_SEARCH_SPEC = {
    "mean_reversion": {
        "flavor": "intraday",
        "signal_parameters": [
            "signal_model", "minimum_dip_pct", "minimum_rip_pct",
            "minimum_bounce_pct", "minimum_fade_pct", "minimum_volume_ratio",
            "entry_window_minutes", "window", "band_width", "entry_rsi",
            "exit_rsi", "short_entry_rsi", "short_exit_rsi",
        ],
        "search_design": "opening-dip profiles plus a separate Bollinger-reversal family",
    },
    "opening_range_breakout": {
        "flavor": "intraday",
        "signal_parameters": [
            "opening_range_bars", "post_range_delay_bars", "minimum_volume_ratio",
            "breakout_buffer_pct",
        ],
        "search_design": "short, standard, and long opening ranges with confirmation filters",
    },
    "intraday_trend": {
        "flavor": "intraday",
        "signal_parameters": ["fast_period", "slow_period", "confirmation_bars", "minimum_slope_pct"],
        "search_design": "responsive through patient moving-average crossovers",
    },
    "intraday_momentum": {
        "flavor": "intraday",
        "signal_parameters": [
            "lookback", "minimum_return", "minimum_short_return",
            "minimum_volume_ratio", "minimum_short_volume_ratio", "max_extension_pct",
        ],
        "search_design": "short through long return windows with volume and extension filters",
    },
    "trend_following": {
        "flavor": "swing",
        "signal_parameters": ["average_type", "fast_period", "slow_period", "minimum_slope_pct"],
        "search_design": "daily SMA and EMA crossovers across faster and slower horizons",
    },
    "momentum": {
        "flavor": "swing",
        "signal_parameters": ["lookbacks", "top_n", "minimum_score_spread"],
        "search_design": "cross-sectional daily return blends and ranking selectivity",
    },
    "swing_mean_reversion": {
        "flavor": "swing",
        "signal_parameters": [
            "lookback_days", "stretch_pct", "minimum_stretch_days",
            "mean_target_fraction", "use_rsi_confirmation", "long_rsi_max", "short_rsi_min",
        ],
        "search_design": "multi-session stretch and reversal confirmation on completed daily bars",
    },
    "multi_factor": {
        "flavor": "swing",
        "signal_parameters": [
            "rebalance_frequency", "top_n", "minimum_score", "direction_mode", "factor_weights",
        ],
        "search_design": "daily and weekly factor rebalance variants with bounded factor-weight alternatives",
    },
}


_VARIANTS: dict[str, tuple[tuple[str, dict], ...]] = {
    "mean_reversion": (
        ("opening_dip_responsive", {
            "signal_model": "opening_dip", "minimum_dip_pct": 0.010,
            "minimum_rip_pct": 0.010, "minimum_bounce_pct": 0.0015,
            "minimum_fade_pct": 0.0015, "minimum_volume_ratio": 1.1,
            "entry_window_minutes": 120,
        }),
        ("opening_dip_selective", {
            "signal_model": "opening_dip", "minimum_dip_pct": 0.020,
            "minimum_rip_pct": 0.020, "minimum_bounce_pct": 0.003,
            "minimum_fade_pct": 0.003, "minimum_volume_ratio": 1.6,
            "entry_window_minutes": 45,
        }),
        ("opening_dip_patient", {
            "signal_model": "opening_dip", "minimum_dip_pct": 0.016,
            "minimum_rip_pct": 0.016, "minimum_bounce_pct": 0.0025,
            "minimum_fade_pct": 0.0025, "minimum_volume_ratio": 1.35,
            "entry_window_minutes": 60, "higher_low_bars": 2, "lower_high_bars": 2,
        }),
        ("opening_dip_fast_confirmation", {
            "signal_model": "opening_dip", "minimum_dip_pct": 0.012,
            "minimum_rip_pct": 0.012, "minimum_bounce_pct": 0.001,
            "minimum_fade_pct": 0.001, "minimum_volume_ratio": 1.5,
            "entry_window_minutes": 30,
        }),
        ("bollinger_baseline_thresholds", {
            "signal_model": "bollinger", "window": 20, "band_width": 2.0,
            "entry_rsi": 30, "exit_rsi": 58,
            "short_entry_rsi": 68, "short_exit_rsi": 42,
        }),
        ("bollinger_wider_band", {
            "signal_model": "bollinger", "window": 30, "band_width": 2.25,
            "entry_rsi": 28, "exit_rsi": 60,
            "short_entry_rsi": 72, "short_exit_rsi": 40,
        }),
        ("bollinger_selective", {
            "signal_model": "bollinger", "window": 25, "band_width": 2.5,
            "entry_rsi": 25, "exit_rsi": 55,
            "short_entry_rsi": 75, "short_exit_rsi": 45,
        }),
    ),
    "opening_range_breakout": (
        ("short_range_15m", {
            "opening_range_bars": 3, "post_range_delay_bars": 1,
            "minimum_volume_ratio": 1.25, "breakout_buffer_pct": 0.001,
        }),
        ("standard_range_confirmed", {
            "opening_range_bars": 6, "post_range_delay_bars": 1,
            "minimum_volume_ratio": 1.5, "breakout_buffer_pct": 0.001,
        }),
        ("standard_range_selective", {
            "opening_range_bars": 6, "post_range_delay_bars": 2,
            "minimum_volume_ratio": 2.0, "breakout_buffer_pct": 0.003,
        }),
        ("long_range_45m", {
            "opening_range_bars": 9, "post_range_delay_bars": 1,
            "minimum_volume_ratio": 1.25, "breakout_buffer_pct": 0.001,
        }),
        ("long_range_60m", {
            "opening_range_bars": 12, "post_range_delay_bars": 2,
            "minimum_volume_ratio": 1.5, "breakout_buffer_pct": 0.0015,
        }),
        ("low_volume_30m", {
            "opening_range_bars": 6, "post_range_delay_bars": 1,
            "minimum_volume_ratio": 1.1, "breakout_buffer_pct": 0.0005,
        }),
        ("high_conviction_30m", {
            "opening_range_bars": 6, "post_range_delay_bars": 3,
            "minimum_volume_ratio": 2.5, "breakout_buffer_pct": 0.004,
        }),
    ),
    "intraday_trend": (
        ("responsive_6_18", {
            "fast_period": 6, "slow_period": 18,
            "confirmation_bars": 1, "minimum_slope_pct": 0.0,
        }),
        ("confirmed_8_20", {
            "fast_period": 8, "slow_period": 20,
            "confirmation_bars": 2, "minimum_slope_pct": 0.001,
        }),
        ("patient_12_30", {
            "fast_period": 12, "slow_period": 30,
            "confirmation_bars": 2, "minimum_slope_pct": 0.002,
        }),
        ("smooth_7_24", {
            "fast_period": 7, "slow_period": 24,
            "confirmation_bars": 2, "minimum_slope_pct": 0.0005,
        }),
        ("patient_10_35", {
            "fast_period": 10, "slow_period": 35,
            "confirmation_bars": 3, "minimum_slope_pct": 0.0015,
        }),
        ("very_patient_15_40", {
            "fast_period": 15, "slow_period": 40,
            "confirmation_bars": 3, "minimum_slope_pct": 0.0025,
        }),
        ("early_5_15", {
            "fast_period": 5, "slow_period": 15,
            "confirmation_bars": 1, "minimum_slope_pct": 0.0005,
        }),
    ),
    "intraday_momentum": (
        ("responsive_5bar", {
            "lookback": 5, "minimum_return": 0.01, "minimum_short_return": 0.01,
            "minimum_volume_ratio": 1.3, "minimum_short_volume_ratio": 1.3,
        }),
        ("balanced_12bar", {
            "lookback": 12, "minimum_return": 0.02, "minimum_short_return": 0.02,
            "minimum_volume_ratio": 1.4, "minimum_short_volume_ratio": 1.4,
        }),
        ("selective_20bar", {
            "lookback": 20, "minimum_return": 0.04, "minimum_short_return": 0.04,
            "minimum_volume_ratio": 1.6, "minimum_short_volume_ratio": 1.6,
        }),
        ("fast_8bar_low_volume", {
            "lookback": 8, "minimum_return": 0.012, "minimum_short_return": 0.012,
            "minimum_volume_ratio": 1.1, "minimum_short_volume_ratio": 1.1,
        }),
        ("slow_16bar_confirmed", {
            "lookback": 16, "minimum_return": 0.03, "minimum_short_return": 0.03,
            "minimum_volume_ratio": 1.5, "minimum_short_volume_ratio": 1.5,
            "max_extension_pct": 0.015,
        }),
        ("low_threshold_10bar", {
            "lookback": 10, "minimum_return": 0.008, "minimum_short_return": 0.008,
            "minimum_volume_ratio": 1.2, "minimum_short_volume_ratio": 1.2,
        }),
        ("high_conviction_15bar", {
            "lookback": 15, "minimum_return": 0.035, "minimum_short_return": 0.035,
            "minimum_volume_ratio": 1.8, "minimum_short_volume_ratio": 1.8,
            "max_extension_pct": 0.02,
        }),
    ),
    "trend_following": (
        ("sma_15_40_low_slope", {
            "average_type": "sma", "fast_period": 15,
            "slow_period": 40, "minimum_slope_pct": 0.0,
        }),
        ("ema_10_40_confirmed", {
            "average_type": "ema", "fast_period": 10,
            "slow_period": 40, "minimum_slope_pct": 0.002,
        }),
        ("sma_30_80_strict", {
            "average_type": "sma", "fast_period": 30,
            "slow_period": 80, "minimum_slope_pct": 0.005,
        }),
        ("ema_20_50_low_slope", {
            "average_type": "ema", "fast_period": 20,
            "slow_period": 50, "minimum_slope_pct": 0.0,
        }),
        ("sma_10_50_confirmed", {
            "average_type": "sma", "fast_period": 10,
            "slow_period": 50, "minimum_slope_pct": 0.003,
        }),
        ("ema_25_70_patient", {
            "average_type": "ema", "fast_period": 25,
            "slow_period": 70, "minimum_slope_pct": 0.004,
        }),
        ("sma_20_60_balanced", {
            "average_type": "sma", "fast_period": 20,
            "slow_period": 60, "minimum_slope_pct": 0.002,
        }),
    ),
    "momentum": (
        ("daily_10_20_40_rank3", {"lookbacks": [10, 20, 40], "top_n": 3, "minimum_score_spread": 0.04}),
        ("fast_daily_3_5_10_rank2", {"lookbacks": [3, 5, 10], "top_n": 2, "minimum_score_spread": 0.03}),
        ("selective_daily_5_10_20", {"lookbacks": [5, 10, 20], "top_n": 2, "minimum_score_spread": 0.06}),
        ("patient_daily_10_20_rank2", {"lookbacks": [10, 20], "top_n": 2, "minimum_score_spread": 0.05}),
        ("broad_daily_5_10_20_40_rank3", {
            "lookbacks": [5, 10, 20, 40], "top_n": 3,
            "minimum_score_spread": 0.04,
        }),
        ("fast_daily_5_10_rank3", {"lookbacks": [5, 10], "top_n": 3, "minimum_score_spread": 0.025}),
        ("selective_daily_10_20_40_rank2", {
            "lookbacks": [10, 20, 40], "top_n": 2,
            "minimum_score_spread": 0.08,
        }),
    ),
    "swing_mean_reversion": (
        ("stretch_5pct_2days", {"lookback_days": 15, "stretch_pct": 0.05, "minimum_stretch_days": 2}),
        ("stretch_10pct_2days", {"lookback_days": 20, "stretch_pct": 0.10, "minimum_stretch_days": 2}),
        ("patient_12pct_3days", {"lookback_days": 20, "stretch_pct": 0.12, "minimum_stretch_days": 3}),
        ("short_reference_8pct", {"lookback_days": 10, "stretch_pct": 0.08, "minimum_stretch_days": 2}),
        ("long_reference_6pct", {"lookback_days": 25, "stretch_pct": 0.06, "minimum_stretch_days": 2}),
        ("persistent_7pct_3days", {"lookback_days": 15, "stretch_pct": 0.07, "minimum_stretch_days": 3}),
        ("rsi_free_stretch_8pct", {
            "lookback_days": 20, "stretch_pct": 0.08,
            "minimum_stretch_days": 2, "use_rsi_confirmation": False,
        }),
    ),
    "multi_factor": (
        ("daily_equal_weight", {
            "rebalance_frequency": "daily", "factor_weights": {
                "momentum_1m": 0.25, "momentum_3m": 0.25,
                "momentum_6m": 0.25, "low_volatility": 0.25,
            },
        }),
        ("daily_long_only_momentum_blend", {
            "rebalance_frequency": "daily", "direction_mode": "long_only",
            "factor_weights": {"momentum_1m": 0.2, "momentum_3m": 0.3, "momentum_6m": 0.3, "low_volatility": 0.2},
        }),
        ("weekly_6m_momentum", {
            "rebalance_frequency": "weekly", "direction_mode": "regime_dependent",
            "factor_weights": {"momentum_6m": 1.0},
        }),
        ("daily_top_two", {"rebalance_frequency": "daily", "top_n": 2}),
        ("daily_top_four", {"rebalance_frequency": "daily", "top_n": 4}),
        ("daily_selective_score", {"rebalance_frequency": "daily", "minimum_score": 0.1}),
        ("weekly_equal_weight", {
            "rebalance_frequency": "weekly", "factor_weights": {
                "momentum_1m": 0.25, "momentum_3m": 0.25,
                "momentum_6m": 0.25, "low_volatility": 0.25,
            },
        }),
    ),
}


def candidate_profiles(strategy: str) -> list[dict]:
    """Build and validate a deterministic candidate bank for one strategy."""
    if strategy not in RESEARCHABLE_STRATEGIES:
        raise ValueError(f"Unsupported Algo strategy: {strategy}")

    baseline = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    if strategy in {"trend_following", "momentum", "swing_mean_reversion"}:
        baseline["direction_mode"] = "both"
    else:
        baseline["allow_long"] = True
        baseline["allow_short"] = True
    candidates = [("baseline", {})]
    candidates.extend(_VARIANTS[strategy])

    profiles = []
    for name, overrides in candidates:
        parameters = deepcopy(baseline)
        parameters.update(deepcopy(overrides))
        if strategy in {"intraday_trend", "intraday_momentum"}:
            parameters["enabled"] = True
        validate_parameter_profile({strategy: parameters})
        profiles.append({
            "name": name if name == "baseline" else f"{strategy}_{name}",
            "strategies": [strategy],
            "parameters": {strategy: parameters},
            "controls": deepcopy(CORE_RESEARCH_CONTROLS),
        })

    names = [profile["name"] for profile in profiles]
    if len(names) != len(set(names)):
        raise ValueError(f"Candidate profile names are not unique for {strategy}")
    return profiles


def profile_search_manifest(strategy: str) -> dict:
    """Describe the search bank placed in research and optimization artifacts."""
    if strategy not in STRATEGY_SEARCH_SPEC:
        raise ValueError(f"Unsupported Algo strategy: {strategy}")
    return {
        "version": SEARCH_SPACE_VERSION,
        "strategy": strategy,
        "flavor": STRATEGY_FLAVORS[strategy],
        "method": "bounded deterministic discrete profile search",
        "selection_stage": "walk-forward training folds only",
        "tuned_fields": list(STRATEGY_SEARCH_SPEC[strategy]["signal_parameters"]),
        "search_design": STRATEGY_SEARCH_SPEC[strategy]["search_design"],
        "risk_and_portfolio_limits_tuned": False,
        "candidate_count": len(candidate_profiles(strategy)),
    }
