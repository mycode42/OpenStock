"""Scheduled-pipeline facade for the modular Algo research package."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.algo.config import get_algo_research_controls
from src.algo.backtest import (
    DAILY_WARMUP_DAYS, AlgoPortfolioBacktest, _regular_session_bars,
    multi_factor_warmup_days,
)
from src.algo.research.factor_research import train_factor_model
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from src.algo.strategies.event_driven import EventCalendar
from src.algo.strategies.satellite_overlay import SatelliteOverlayConfig
from src.market_data import data_tier, regular_feed
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars
from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS
from src.algo.strategies.universe import configured_universe
from src.algo.research.settings import *
from src.algo.research.data import (
    _parse_date, load_algo_universe, _bar_coverage, _load_daily_history, _latest_dates,
    _prepare_event_calendar,
)
from src.algo.research.reporting import _summarize_backtest, _summarize_walk_forward
from src.algo.research.optimization import (
    _isolated_direction_profiles, _run_isolated_direction_job, _run_isolated_core_research,
)
from src.algo.research.replays import (
    _profiles_without_event_overlay, _factor_portfolio_replay, _combined_replay_variant,
    _run_portfolio_replay_job,
)
from src.algo.research.files import _research_lock, _atomic_json
from .nightly import run_daily_research, build_parser, main
from src.algo.research.data import _refresh_incremental as _refresh_incremental_impl
from src.algo.research.replays import _combined_portfolio_replay as _combined_portfolio_replay_impl


def _refresh_incremental(store, *, selected_symbols, end_date):
    return _refresh_incremental_impl(
        store, selected_symbols=selected_symbols, end_date=end_date,
        data_tier_fn=data_tier, regular_feed_fn=regular_feed, fetch_bars_fn=fetch_alpaca_bars,
    )


def _combined_portfolio_replay(store, *, symbols, start, end, event_calendar):
    return _combined_portfolio_replay_impl(
        store, symbols=symbols, start=start, end=end, event_calendar=event_calendar,
        backtest_class=AlgoPortfolioBacktest,
    )


if __name__ == "__main__":
    raise SystemExit(main())
