"""Deterministic Algo portfolio backtest and compatibility API."""

from __future__ import annotations

from .config import (
    BAR_MINUTES, DAILY_WARMUP_DAYS, ET, FACTOR_WARMUP_DAYS, MIN_REGIME_DAILY_BARS,
    MOMENTUM_WARMUP_DAYS, RESEARCH_CONTROL_KEYS, STRATEGY_NAMES,
    multi_factor_warmup_days, strategy_warmup_days,
    validate_research_controls, validate_strategy_filter,
)
from .shared import (
    DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars, STRATEGY_CONFIG,
    evaluate_rule_signal, validate_trade, EventCalendar, get_mode_config, data_tier, regular_feed,
    rank_strategies,
)
from .models import Position
from .portfolio import PortfolioBacktest
from .data import (
    _split_symbols, _regular_session_bars, _read_profile, read_borrow_availability,
    _read_strategy_filter, _read_research_controls, fetch_dataset, run_backtest,
)
from .cli import build_parser, main


class AlgoPortfolioBacktest(PortfolioBacktest):
    """Public replay API; dependency hooks preserve established patch points."""

    def __init__(
        self, *, symbols, intraday_bars, spy_daily_bars, start, end, feed, data_adjustment,
        initial_capital=100_000, slippage_bps=5, profile=None, strategies=None,
        research_controls=None, regime_profiles=None, event_calendar=None, spread_bps=3.0,
        commission_per_share=0.005, short_borrow_annual_pct=0.03,
        unknown_borrow_penalty_bps=25.0, borrow_availability=None,
        coordinator_config=None, force_close_at_end=False,
        research_runtime_overrides=None,
        preaggregated_daily_bars=None,
        precomputed_timeline=None,
        include_extended_hours=False,
        regime_context_bars=None, regime_macro_series=None, regime_config=None,
        execution_delay_bars=0, factor_weight_timeline=None,
    ):
        super().__init__(
            symbols=symbols, intraday_bars=intraday_bars, spy_daily_bars=spy_daily_bars,
            start=start, end=end, feed=feed, data_adjustment=data_adjustment,
            initial_capital=initial_capital, slippage_bps=slippage_bps, profile=profile,
            strategies=strategies, research_controls=research_controls,
            regime_profiles=regime_profiles,
            event_calendar=event_calendar, spread_bps=spread_bps,
            commission_per_share=commission_per_share,
            short_borrow_annual_pct=short_borrow_annual_pct,
            unknown_borrow_penalty_bps=unknown_borrow_penalty_bps,
            borrow_availability=borrow_availability, coordinator_config=coordinator_config,
            force_close_at_end=force_close_at_end,
            research_runtime_overrides=research_runtime_overrides,
            preaggregated_daily_bars=preaggregated_daily_bars,
            precomputed_timeline=precomputed_timeline,
            include_extended_hours=include_extended_hours,
            regime_context_bars=regime_context_bars,
            regime_macro_series=regime_macro_series,
            regime_config=regime_config,
            execution_delay_bars=execution_delay_bars,
            factor_weight_timeline=factor_weight_timeline,
            _signal_evaluator=lambda *args, **kwargs: evaluate_rule_signal(*args, **kwargs),
            _trade_validator=lambda **kwargs: validate_trade(**kwargs),
            _regime_ranker=lambda *args, **kwargs: rank_strategies(*args, **kwargs),
        )

__all__ = [
    "AlgoPortfolioBacktest", "Position", "multi_factor_warmup_days",
    "validate_strategy_filter", "validate_research_controls",
    "_regular_session_bars", "read_borrow_availability", "fetch_dataset",
    "run_backtest", "build_parser", "main", "DEFAULT_DB",
]
