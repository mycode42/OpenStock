"""Load point-in-time bars, event inputs, and market regime for Algo scans."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
from threading import RLock

from market.data import historical_delay_seconds, regular_feed
from market.regime import load_fred_regime_series
from portfolio.config import (
    ALGO_REGIME_CONFIG,
    ALGO_REGIME_WARMUP_CALENDAR_DAYS,
    ALGO_SIGNAL_DATA_CONFIG,
)
from runtime.paths import DATA_DIR
from strategies.event_driven import EventCalendar, EventSignalGenerator
from strategies.regime import rank_strategies
from strategies.regime_advisor import apply_regime_advisory
from strategies.registry import ALGO_DEFAULT_PARAMETERS, parameters_for_regime
from strategies.runner.data import (
    _completed_daily,
    _completed_intraday,
    fetch_bars as _fetch_bars,
    _latest_bar_age_seconds,
)
from strategies.multi_factor import MultiFactorConfig

from .scan_types import MarketSnapshot, ScanScope


MAX_SPY_DAILY_AGE_DAYS = ALGO_SIGNAL_DATA_CONFIG["max_spy_daily_age_days"]
_DAILY_BAR_CACHE_LOCK = RLock()
_DAILY_BAR_CACHE: dict[tuple, dict[str, list[dict]]] = {}


def load_market_snapshot(scope: ScanScope) -> MarketSnapshot:
    """Fetch completed bars and derive the regime and enabled event context."""
    now_utc = scope.now_utc or datetime.now(timezone.utc)
    factor_config_values = dict(ALGO_DEFAULT_PARAMETERS.get("swing_multi_factor") or {})
    factor_config_values.update(scope.multi_factor_parameters)
    factor_config = MultiFactorConfig.from_dict(factor_config_values)
    daily_history_days = max(
        factor_config.warmup_calendar_days(), ALGO_REGIME_WARMUP_CALENDAR_DAYS,
    )
    daily_start = (now_utc - timedelta(days=daily_history_days)).date().isoformat()
    daily_feed = regular_feed()
    historical_status = "ok"
    historical_bar_end: str | None = None
    try:
        local_snapshot_time = now_utc.astimezone(scope.now.tzinfo)
        snapshot_phase = "postclose" if (
            local_snapshot_time.hour > 16
            or (local_snapshot_time.hour == 16 and local_snapshot_time.minute >= 15)
        ) else "intraday"
        cache_key = (
            scope.today, daily_start, daily_feed, tuple(scope.symbols_for_bars), snapshot_phase,
            _fetch_bars,
        )
        with _DAILY_BAR_CACHE_LOCK:
            cached = _DAILY_BAR_CACHE.get(cache_key)
            if cached is None:
                cached = _fetch_bars(
                    scope.symbols_for_bars, mode=scope.mode, timeframe="1Day",
                    start=daily_start, end=now_utc.isoformat(), limit=10000,
                    feed=daily_feed, adjustment="split",
                )
                _DAILY_BAR_CACHE.clear()
                _DAILY_BAR_CACHE[cache_key] = {
                    ticker: list(rows) for ticker, rows in cached.items()
                }
            daily_bars = {ticker: list(rows) for ticker, rows in cached.items()}
        daily_bars = {
            ticker: _completed_daily(bars, scope.today)
            for ticker, bars in daily_bars.items()
        }
    except Exception as exc:
        daily_bars = {}
        historical_status = "degraded"
        scope.scan_errors.append(f"daily historical bars: {type(exc).__name__}: {exc}")
        scope.entry_pause_reasons.append("daily historical bars are unavailable")

    intraday_bars = {}
    if scope.session_name:
        if scope.session_name == "overnight":
            bar_day = scope.now.date() if scope.now.hour >= 20 else scope.now.date() - timedelta(days=1)
            start_at = datetime.combine(bar_day, datetime.min.time(), tzinfo=scope.now.tzinfo).replace(hour=20)
        elif scope.session_name == "premarket":
            start_at = datetime.fromisoformat(f"{scope.today}T04:00:00").replace(tzinfo=scope.now.tzinfo)
        else:
            start_at = datetime.fromisoformat(f"{scope.today}T09:30:00").replace(tzinfo=scope.now.tzinfo)
        delay_seconds = historical_delay_seconds(scope.data_feed, scope.session_name)
        bar_end = scope.now - timedelta(seconds=delay_seconds) if delay_seconds else scope.now
        historical_bar_end = bar_end.isoformat()
        if bar_end > start_at:
            try:
                intraday_bars = _fetch_bars(
                    scope.strategy_symbols,
                    mode=scope.mode,
                    timeframe="5Min",
                    start=start_at.isoformat(),
                    end=historical_bar_end,
                    limit=10000,
                    feed=scope.data_feed,
                )
            except Exception as exc:
                historical_status = "degraded"
                scope.scan_errors.append(
                    f"intraday historical bars ({scope.data_feed}): {type(exc).__name__}: {exc}"
                )
                scope.entry_pause_reasons.append("intraday historical bars are unavailable")
        elif historical_status == "ok":
            historical_status = "warming_up"
            scope.entry_pause_reasons.append("waiting for completed intraday historical bars")
    elif scope.now >= scope.now.replace(hour=9, minute=30, second=0, microsecond=0):
        session_open = scope.now.replace(hour=9, minute=30, second=0, microsecond=0)
        intraday_bars = _fetch_bars(
            scope.strategy_symbols,
            mode=scope.mode,
            timeframe="5Min",
            start=session_open.isoformat(),
            end=scope.now.isoformat(),
            limit=10000,
            feed=scope.data_feed,
        )
    try:
        bars_by_symbol = {
            ticker: _completed_intraday(bars, now=scope.now)
            for ticker, bars in intraday_bars.items()
        }
    except Exception as exc:
        bars_by_symbol = {}
        historical_status = "degraded"
        scope.scan_errors.append(f"intraday bar validation: {type(exc).__name__}: {exc}")
        scope.entry_pause_reasons.append("intraday historical bars are invalid")
    if scope.strategy_symbols and not any(bars_by_symbol.values()) and historical_status == "ok":
        historical_status = "warming_up"
        scope.entry_pause_reasons.append("no completed intraday historical bars are available yet")

    delay_seconds = historical_delay_seconds(scope.data_feed, scope.session_name or "")
    signal_bar_max_age_seconds = (
        ALGO_SIGNAL_DATA_CONFIG["delayed_signal_bar_max_age_minutes"] if delay_seconds
        else ALGO_SIGNAL_DATA_CONFIG["signal_bar_max_age_minutes"]
    ) * 60
    overnight_swing_signal_bar_max_age_seconds = (
        ALGO_SIGNAL_DATA_CONFIG["overnight_swing_signal_bar_max_age_minutes"] * 60
        if scope.session_name == "overnight" else 0
    )
    fresh_signal_bar_symbols = {
        ticker for ticker, bars in bars_by_symbol.items()
        if (age := _latest_bar_age_seconds(bars, scope.now)) is not None
        and 0 <= age <= signal_bar_max_age_seconds
    }
    overnight_swing_signal_bar_symbols = {
        ticker for ticker, bars in bars_by_symbol.items()
        if overnight_swing_signal_bar_max_age_seconds
        and (age := _latest_bar_age_seconds(bars, scope.now)) is not None
        and signal_bar_max_age_seconds < age <= overnight_swing_signal_bar_max_age_seconds
    }
    eligible_signal_bar_symbols = fresh_signal_bar_symbols | overnight_swing_signal_bar_symbols
    if scope.selected_symbols and not eligible_signal_bar_symbols.intersection(scope.selected_symbols):
        scope.entry_pause_reasons.append("the Algo universe has no recent completed signal bars")
        if historical_status == "ok":
            historical_status = "stale"

    spy_last_completed_date: str | None = None
    spy_daily_age_days: int | None = None
    if daily_bars.get("SPY"):
        try:
            spy_last_completed_date = str(daily_bars["SPY"][-1]["t"])[:10]
            spy_daily_age_days = (
                date.fromisoformat(scope.today) - date.fromisoformat(spy_last_completed_date)
            ).days
        except (KeyError, TypeError, ValueError):
            spy_daily_age_days = None
    if spy_daily_age_days is None or not 1 <= spy_daily_age_days <= MAX_SPY_DAILY_AGE_DAYS:
        scope.entry_pause_reasons.append("the latest completed SPY daily bar is stale or invalid")
        if historical_status == "ok":
            historical_status = "stale"

    calendar_path = DATA_DIR / "event_calendar.csv"
    calendar_path = Path(os.environ.get("ALGO_EVENT_CALENDAR", str(calendar_path)))
    from .event_context import load_event_context

    event_context = load_event_context(
        enabled=scope.event_strategy_enabled,
        owned_event_position="swing_event_driven" in set(scope.owner_map.values()),
        event_config=scope.event_config,
        calendar_path=calendar_path,
        checked_at=lambda: datetime.now(timezone.utc),
        calendar_type=EventCalendar,
        generator_type=EventSignalGenerator,
    )
    scope.scan_errors.extend(event_context.errors)

    regime_macro_series = {}
    regime_macro_diagnostics = {"status": "not_loaded"}
    try:
        regime_macro_series, regime_macro_diagnostics = load_fred_regime_series(refresh=True)
    except Exception as exc:
        regime_macro_diagnostics = {"status": "unavailable", "error": type(exc).__name__}
        scope.scan_errors.append(f"regime macro data: {type(exc).__name__}: {exc}")
    try:
        regime = rank_strategies(
            daily_bars.get("SPY", []), mode=scope.mode,
            event_enabled=event_context.available,
            event_disable_in_volatile=scope.event_config.disable_in_volatile,
            intraday_trend_enabled=scope.runtime_settings.enable_intraday_trend_following,
            intraday_momentum_enabled=scope.runtime_settings.enable_intraday_momentum,
            feature_set=ALGO_REGIME_CONFIG["feature_set"],
            engine=ALGO_REGIME_CONFIG["engine"],
            strategy_policy=ALGO_REGIME_CONFIG["strategy_policy"],
            context_bars=daily_bars,
            macro_series=regime_macro_series,
            include_models=ALGO_REGIME_CONFIG["include_models"],
            model_training_window=ALGO_REGIME_CONFIG["model_training_window"],
            model_min_confidence=ALGO_REGIME_CONFIG["min_model_confidence"],
        )
        regime["macro_data"] = regime_macro_diagnostics
    except Exception as exc:
        regime = {
            "regime": "unknown", "reason": "SPY regime calculation failed",
            "ranked": [], "active": [],
        }
        scope.scan_errors.append(f"SPY regime calculation: {type(exc).__name__}: {exc}")
        scope.entry_pause_reasons.append("SPY regime calculation failed")
    try:
        regime = apply_regime_advisory(regime)
    except Exception as exc:
        scope.scan_errors.append(f"optional regime advisor: {type(exc).__name__}: {exc}")
        regime["regime_advisory"] = {"status": "fallback", "error": str(exc)}
    if regime.get("regime") == "unknown":
        scope.entry_pause_reasons.append("deterministic SPY regime is unavailable")
        if historical_status == "ok":
            historical_status = "degraded"
    regime_name = str(regime.get("regime", "unknown"))

    return MarketSnapshot(
        daily_bars=daily_bars,
        bars_by_symbol=bars_by_symbol,
        historical_status=historical_status,
        historical_bar_end=historical_bar_end,
        delay_seconds=delay_seconds,
        signal_bar_max_age_seconds=signal_bar_max_age_seconds,
        overnight_swing_signal_bar_max_age_seconds=overnight_swing_signal_bar_max_age_seconds,
        fresh_signal_bar_symbols=fresh_signal_bar_symbols,
        overnight_swing_signal_bar_symbols=overnight_swing_signal_bar_symbols,
        spy_last_completed_date=spy_last_completed_date,
        spy_daily_age_days=spy_daily_age_days,
        event_generator=event_context.generator,
        event_strategy_available=event_context.available,
        event_calendar_diagnostics=event_context.diagnostics,
        event_calendar_status=event_context.calendar_status,
        regime=regime,
        regime_name=regime_name,
        multi_factor_parameters=parameters_for_regime("swing_multi_factor", regime_name),
    )
