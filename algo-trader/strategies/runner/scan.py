"""Public entry point for the staged Algo portfolio scan."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Keep the old module-level dependencies available for scan callers and test
# harnesses that patch the scan boundary. The stage modules receive these
# references immediately before each scan.
from market.data import feeds_for_session, historical_delay_seconds, regular_feed
from market.regime import load_fred_regime_series
from portfolio.config import get_algo_runtime_settings
from strategies.regime import rank_strategies
from strategies.registry import refresh_active_parameter_profile
from strategies.runner.data import (
    _completed_daily,
    _completed_intraday,
    _latest_bar_age_seconds,
    current_portfolio as _current_portfolio,
    fetch_bars as _fetch_bars,
    fetch_latest_quotes as _fetch_latest_quotes,
)
from strategies.momentum.swing import generate_signals as generate_swing_momentum_signals

from . import scan_execution, scan_exits, scan_market, scan_quotes, scan_setup, scan_signals
from .scan_response import build_scan_response


ET = ZoneInfo("America/New_York")
_DAILY_BAR_CACHE_LOCK = scan_market._DAILY_BAR_CACHE_LOCK
_DAILY_BAR_CACHE = scan_market._DAILY_BAR_CACHE


def scan(
    mode: str = "algo",
    *,
    universe_result: dict | None = None,
    excluded_symbols: set[str] | None = None,
    position_owners: dict[str, str] | None = None,
    strategy_states: dict | None = None,
    session_name: str | None = None,
    trading_date: str | None = None,
    extended_hours: bool = False,
    regular_open: datetime | None = None,
    regular_close: datetime | None = None,
    data_feed: str | None = None,
    quote_feed: str | None = None,
    overnight_tradable_tickers: set[str] | None = None,
) -> dict:
    """Run one complete scan through setup, data, signals, and execution stages."""
    _sync_scan_dependencies()
    runtime_settings = get_algo_runtime_settings()
    refresh_active_parameter_profile()
    scope = scan_setup.prepare_scan_scope(
        mode,
        universe_result=universe_result,
        excluded_symbols=excluded_symbols,
        position_owners=position_owners,
        strategy_states=strategy_states,
        session_name=session_name,
        trading_date=trading_date,
        extended_hours=extended_hours,
        regular_open=regular_open,
        regular_close=regular_close,
        data_feed=data_feed,
        quote_feed=quote_feed,
        overnight_tradable_tickers=overnight_tradable_tickers,
        runtime_settings=runtime_settings,
    )
    market = scan_market.load_market_snapshot(scope)
    quotes = scan_quotes.load_quote_snapshot(scope, market)
    signals = scan_signals.generate_scan_signals(scope, market, quotes)
    scan_exits.prepare_strategy_exits(scope, signals.generated)
    execution = scan_execution.execute_scan_candidates(
        scope, market, quotes, signals.generated,
    )
    return build_scan_response(scope, market, quotes, signals, execution)


def _sync_scan_dependencies() -> None:
    """Pass module-level patch points through to their owning scan stages."""
    scan_setup.datetime = datetime
    scan_setup.feeds_for_session = feeds_for_session
    scan_setup.regular_feed = regular_feed
    scan_setup.current_portfolio = _current_portfolio

    scan_market.datetime = datetime
    scan_market.regular_feed = regular_feed
    scan_market.historical_delay_seconds = historical_delay_seconds
    scan_market.load_fred_regime_series = load_fred_regime_series
    scan_market._fetch_bars = _fetch_bars
    scan_market._completed_daily = _completed_daily
    scan_market._completed_intraday = _completed_intraday
    scan_market._latest_bar_age_seconds = _latest_bar_age_seconds
    scan_market.rank_strategies = rank_strategies
    scan_market._DAILY_BAR_CACHE_LOCK = _DAILY_BAR_CACHE_LOCK
    scan_market._DAILY_BAR_CACHE = _DAILY_BAR_CACHE

    scan_quotes.datetime = datetime
    scan_quotes.fetch_latest_quotes = _fetch_latest_quotes

    scan_signals.datetime = datetime
    scan_signals.generate_swing_momentum_signals = generate_swing_momentum_signals
    scan_exits.datetime = datetime
    scan_execution.datetime = datetime
    from . import scan_response

    scan_response.datetime = datetime


__all__ = ["scan"]
