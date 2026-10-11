"""Scheduled adaptive Algo research entry point."""

from __future__ import annotations

from src.algo.market_store import fetch_alpaca_bars
from src.market_data import data_tier, regular_feed

from .data import _refresh_incremental as _refresh_incremental_impl, load_algo_universe
from .nightly import build_parser, main, run_daily_research


def _refresh_incremental(store, *, selected_symbols, end_date):
    return _refresh_incremental_impl(
        store, selected_symbols=selected_symbols, end_date=end_date,
        data_tier_fn=data_tier, regular_feed_fn=regular_feed,
        fetch_bars_fn=fetch_alpaca_bars,
    )


if __name__ == "__main__":
    raise SystemExit(main())
