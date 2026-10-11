"""Portfolio replay variants used by scheduled research."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile

from .shared import *
from .settings import *
from .reporting import _summarize_backtest
from .files import _atomic_json
from .data import _replay_start_after_spy_warmup


def _combined_portfolio_replay(
    store: SQLiteBarStore,
    *,
    symbols: list[str],
    start: date,
    end: date,
    event_calendar: EventCalendar | None,
    backtest_class=None,
) -> dict:
    """Compare the rule portfolio with its bounded Multi Factor/Event strategies."""
    if end < start:
        raise ValueError("combined replay end must not be before start")
    start = _replay_start_after_spy_warmup(store, start, end)
    warmup_days = multi_factor_warmup_days({})
    bars_start = (start - timedelta(days=warmup_days)).isoformat()
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=bars_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    backtest_class = backtest_class or AlgoPortfolioBacktest
    results = {
        variant: _combined_replay_variant(
            symbols=symbols, start=start, end=end,
            intraday=intraday, spy=spy, event_calendar=event_calendar,
            variant=variant, backtest_class=backtest_class,
        )
        for variant in ("four_core_rules", "six_strategy_satellite_portfolio")
    }
    del intraday, spy
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "event_calendar_available": event_calendar is not None,
        "results": results,
        "comparison_note": (
            "The six-strategy variant keeps the four rule strategies and Multi Factor as entry owners; Event Driven only confirms and amplifies aligned core entries."
        ),
    }

def _profiles_without_event_overlay(source: str | Path) -> tuple[Path, Path | None, list[str]]:
    """Return a temporary profile set retaining core and factor research."""
    path = Path(source)
    profiles = json.loads(path.read_text())
    retained = []
    omitted = []
    for profile in profiles:
        overlay = SatelliteOverlayConfig.from_dict(profile.get("parameters", {}).get("satellite_overlay"))
        if overlay.event_driven_enabled:
            omitted.append(profile["name"])
        else:
            retained.append(profile)
    if not omitted:
        return path, None, []
    if not any(profile.get("name") == "baseline" for profile in retained):
        raise ValueError("Cannot omit event profiles because no baseline profile remains")
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="algo-profiles-no-events-",
        suffix=".json", dir=path.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        json.dump(retained, handle, indent=2)
        handle.write("\n")
    finally:
        handle.close()
    return temporary, temporary, omitted

def _factor_portfolio_replay(
    store: SQLiteBarStore,
    *,
    symbols: list[str],
    start: date,
    end: date,
    weights: dict[str, float],
) -> dict:
    start = _replay_start_after_spy_warmup(store, start, end)
    warmup_days = multi_factor_warmup_days({"multi_factor": {"factor_weights": weights}})
    bars_start = (start - timedelta(days=warmup_days)).isoformat()
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=bars_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=180)).isoformat(), end=end.isoformat(),
        adjustment=ADJUSTMENT,
    ).get("SPY", [])
    result = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        data_adjustment=ADJUSTMENT, slippage_bps=5,
        profile={"multi_factor": {"factor_weights": weights}},
        strategies=["multi_factor"],
    ).run()
    del intraday, spy
    return _summarize_backtest(result)

def _combined_replay_variant(
    *,
    symbols: list[str],
    start: date,
    end: date,
    intraday: dict[str, list[dict]],
    spy: list[dict],
    event_calendar: EventCalendar | None,
    variant: str,
    backtest_class=None,
) -> dict:
    core = ["mean_reversion", "momentum", "opening_range_breakout", "trend_following"]
    if variant == "four_core_rules":
        strategies = core
        profile = {"satellite_overlay": {
            "multi_factor_enabled": False, "event_driven_enabled": False,
        }}
    elif variant == "six_strategy_satellite_portfolio":
        strategies = [*core, "multi_factor", "event_driven"]
        profile = {"satellite_overlay": {
            "multi_factor_enabled": True,
            "event_driven_enabled": event_calendar is not None,
        }, "event_driven": {"enabled": event_calendar is not None}}
    else:
        raise ValueError(f"Unknown combined replay variant: {variant}")

    backtest_class = backtest_class or AlgoPortfolioBacktest
    report = backtest_class(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        data_adjustment=ADJUSTMENT, slippage_bps=5, profile=profile,
        strategies=strategies, event_calendar=event_calendar,
    ).run()
    return _summarize_backtest(report)

def _run_portfolio_replay_job(job: tuple) -> tuple[str, str, str, dict]:
    """Run one independent replay in a worker process."""
    kind, group, variant, payload = job
    store = payload["store"]
    symbols = payload["symbols"]
    start = payload["start"]
    end = payload["end"]
    start = _replay_start_after_spy_warmup(store, start, end)
    if kind == "factor":
        report = _factor_portfolio_replay(
            store, symbols=symbols, start=start, end=end,
            weights=payload["weights"],
        )
        return kind, group, variant, report
    if kind != "combined":
        raise ValueError(f"Unknown replay job kind: {kind}")

    warmup_days = multi_factor_warmup_days({})
    bars_start = (start - timedelta(days=warmup_days)).isoformat()
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=bars_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    report = _combined_replay_variant(
        symbols=symbols, start=start, end=end,
        intraday=intraday, spy=spy,
        event_calendar=payload["event_calendar"], variant=variant,
    )
    return kind, group, variant, report

