"""Fixed-window candidate comparisons for one Algo strategy."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from src.algo.backtest import DAILY_WARMUP_DAYS, AlgoPortfolioBacktest
from src.algo.config import get_algo_research_controls
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.market_data import data_tier, regular_feed
from src.algo.strategies.universe import configured_universe

from .live_manifest import live_configuration_manifest
from .profiles import candidate_profiles, profile_search_manifest
from .data import _replay_start_after_spy_warmup
from .settings import ADJUSTMENT, RESEARCHABLE_STRATEGIES, STRATEGY_FLAVORS, FEED


def run_strategy_research(args) -> dict:
    """Compare predeclared parameter variants on one historical interval.

    This is a descriptive research run, not a parameter selector. Optimization
    and out-of-sample selection are handled by ``strategy_optimization``.
    """
    if args.strategy not in RESEARCHABLE_STRATEGIES:
        raise ValueError(f"Unsupported Algo strategy: {args.strategy}")
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("Algo strategy research is restricted to Plus SIP data")

    requested_start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < requested_start:
        raise ValueError("End date must not be before start date")
    symbols = _symbols(args.symbols)
    db = args.db or str(DEFAULT_DB)
    store = SQLiteBarStore(db)
    start = _replay_start_after_spy_warmup(store, requested_start, end)
    bar_start = (start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat()
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=bar_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy_daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day", start=bar_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    available = [symbol for symbol in symbols if intraday.get(symbol)]
    if not available:
        raise ValueError(f"No stored 5-minute {FEED} bars for the supplied symbols")

    variants = candidate_profiles(args.strategy)
    runtime_overrides = (
        {args.strategy: True} if args.strategy in {"intraday_trend", "intraday_momentum"} else {}
    )
    comparisons = []
    for candidate in variants:
        replay = AlgoPortfolioBacktest(
            symbols=available,
            intraday_bars={symbol: intraday[symbol] for symbol in available},
            spy_daily_bars=spy_daily,
            start=start.isoformat(), end=end.isoformat(), feed=FEED,
            data_adjustment=ADJUSTMENT, slippage_bps=args.slippage_bps,
            profile=candidate["parameters"], strategies=[args.strategy],
            research_controls=get_algo_research_controls(),
            spread_bps=args.spread_bps,
            commission_per_share=args.commission_per_share,
            short_borrow_annual_pct=args.short_borrow_annual_pct / 100,
            unknown_borrow_penalty_bps=args.unknown_borrow_penalty_bps,
            force_close_at_end=args.force_close_at_end,
            research_runtime_overrides=runtime_overrides or None,
        ).run()
        comparisons.append({
            "profile": candidate["name"],
            "parameters": candidate["parameters"],
            "metrics": _research_metrics(replay),
        })

    return {
        "schema": "swarm_algo_strategy_research_v1",
        "stage": "strategy_research",
        "research_only": True,
        "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "strategy": args.strategy,
        "strategy_flavor": STRATEGY_FLAVORS[args.strategy],
        "research_runtime_overrides": runtime_overrides,
        "created_at": args.created_at,
        "data": {
            "tier": data_tier(), "feed": FEED, "adjustment": ADJUSTMENT,
            "database": str(Path(db).resolve()),
            "start": start.isoformat(), "end": end.isoformat(),
            "requested_start": requested_start.isoformat(),
            "requested_symbols": symbols, "symbols_with_bars": available,
            "symbols_without_bars": [symbol for symbol in symbols if symbol not in available],
            "warmup_start": bar_start,
        },
        "selection_performed": False,
        "profile_search": profile_search_manifest(args.strategy),
        "live_configuration": live_configuration_manifest([args.strategy]),
        "candidate_profiles": variants,
        "candidate_comparisons": comparisons,
        "transaction_cost_model": {
            "slippage_bps_each_side": args.slippage_bps,
            "spread_bps": args.spread_bps,
            "commission_per_share_each_side": args.commission_per_share,
            "short_borrow_annual_pct": args.short_borrow_annual_pct,
            "unknown_borrow_adverse_fill_penalty_bps": args.unknown_borrow_penalty_bps,
            "force_close_at_end": args.force_close_at_end,
        },
        "note": (
            "These fixed-window metrics are descriptive. Do not select a live profile from this report; "
            "use the separate chronological optimization and portfolio simulation stages."
        ),
    }


def _symbols(value: str | None) -> list[str]:
    if value:
        symbols = list(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))
    else:
        symbols = configured_universe("algo")["tickers"]
    if not symbols:
        raise ValueError("The Algo research universe is empty")
    return symbols


def _research_metrics(report: dict) -> dict:
    keys = (
        "start", "end", "net_return_pct", "max_drawdown_pct", "closed_trade_count",
        "entry_count", "average_entries_per_session", "time_in_market_pct",
        "entry_count_by_strategy", "satellite_confirmed_entries",
        "strategy_metrics", "benchmark_net_return_pct",
        "excess_return_vs_buy_hold_pct_points",
    )
    return {key: report.get(key) for key in keys}
