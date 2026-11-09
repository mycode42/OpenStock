"""Load point-in-time bars, LLM signal inputs, and market regime for Algo scans."""

from __future__ import annotations

from strategies.hourly_bars import HourlyAggregator, parse_bar_time
from market.store import DEFAULT_DB, SQLiteBarStore
from strategies.catalog import WEEKLY_STRATEGIES
from portfolio.config import get_algo_configuration
from datetime import date, datetime, timedelta, timezone
from threading import RLock

from market.data import historical_delay_seconds, regular_feed
from market.regime import load_fred_regime_series
from portfolio.config import (
    ALGO_REGIME_WARMUP_CALENDAR_DAYS,
    get_signal_data_settings,
    get_algo_regime_config,
)
from strategies.regime import rank_strategies
from strategies.registry import ALGO_DEFAULT_PARAMETERS, parameters_for_regime
from strategies.runner.data import (
    _completed_daily,
    _completed_intraday,
    fetch_bars as _fetch_bars,
    _latest_bar_age_seconds,
)
from strategies.multi_factor import MultiFactorConfig

from .scan_types import MarketSnapshot, ScanScope


_DAILY_BAR_CACHE_LOCK = RLock()
_DAILY_BAR_CACHE: dict[tuple, dict[str, list[dict]]] = {}


# Hourly bars for the weekly tier. A cold start fetches 45 calendar days of
# five-minute bars once per symbol; every later scan fetches only the bars
# since the last one consumed, so steady-state cost is a few bars per symbol.
_HOURLY_STATE: dict = {}
_HOURLY_LAST: dict = {}
_HOURLY_LOCK = RLock()
WEEKLY_LOOKBACK_DAYS = 45


def _ingest_five_minute(ticker: str, bars: list[dict], last):
    """Feed unseen five-minute bars into the ticker's hourly aggregator; return the newest stamp."""
    aggregator = _HOURLY_STATE.setdefault(ticker, HourlyAggregator())
    for bar in sorted(bars, key=lambda item: parse_bar_time(item["t"])):
        stamp = parse_bar_time(bar["t"])
        if last is not None and stamp <= last:
            continue
        aggregator.add(bar)
        last = stamp
    return last


def _weekly_hourly_bars(scope: ScanScope) -> dict[str, list[dict]]:
    """Hourly bars per symbol, seeded from the local store and extended from Alpaca.

    The store holds split-adjusted regular-session five-minute bars, so the live
    fetch uses split adjustment too: the history and the new bars share one basis.
    A symbol with no stored history is fetched from Alpaca over the full lookback.
    """
    symbols = list(dict.fromkeys(scope.strategy_symbols))
    if not symbols:
        return {}
    lookback_start = (scope.now - timedelta(days=WEEKLY_LOOKBACK_DAYS)).isoformat()
    with _HOURLY_LOCK:
        unseeded = [symbol for symbol in symbols if symbol not in _HOURLY_LAST]
        if unseeded:
            stored = SQLiteBarStore(DEFAULT_DB).bars(
                unseeded, feed=scope.data_feed, timeframe="5Min",
                start=lookback_start, end=scope.now.isoformat(), adjustment="split",
            )
            for ticker in unseeded:
                rows = stored.get(ticker, [])
                if rows:
                    last = _ingest_five_minute(ticker, rows, None)
                    if last is not None:
                        _HOURLY_LAST[ticker] = last
        cold = [symbol for symbol in symbols if symbol not in _HOURLY_LAST]
        warm = [symbol for symbol in symbols if symbol in _HOURLY_LAST]
        requests_ = []
        if cold:
            requests_.append((cold, lookback_start))
        if warm:
            requests_.append((warm, min(_HOURLY_LAST[symbol] for symbol in warm).isoformat()))
        for group, start in requests_:
            fetched = _fetch_bars(
                group, mode=scope.mode, timeframe="5Min", start=start,
                end=scope.now.isoformat(), limit=10000, feed=scope.data_feed, adjustment="split",
            )
            for ticker in group:
                last = _ingest_five_minute(ticker, fetched.get(ticker, []), _HOURLY_LAST.get(ticker))
                if last is not None:
                    _HOURLY_LAST[ticker] = last
        return {
            ticker: _HOURLY_STATE[ticker].recent(200)
            for ticker in symbols if ticker in _HOURLY_STATE
        }


def _weekly_enabled() -> bool:
    strategies = get_algo_configuration()["strategies"]
    return any(strategies[name]["enabled"] for name in WEEKLY_STRATEGIES)


def load_market_snapshot(scope: ScanScope) -> MarketSnapshot:
    """Fetch completed bars and derive the regime and enabled LLM signal context."""
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
        get_signal_data_settings()["delayed_signal_bar_max_age_minutes"] if delay_seconds
        else get_signal_data_settings()["signal_bar_max_age_minutes"]
    ) * 60
    overnight_swing_signal_bar_max_age_seconds = (
        get_signal_data_settings()["overnight_swing_signal_bar_max_age_minutes"] * 60
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
    if spy_daily_age_days is None or not 1 <= spy_daily_age_days <= get_signal_data_settings()["max_spy_daily_age_days"]:
        scope.entry_pause_reasons.append("the latest completed SPY daily bar is stale or invalid")
        if historical_status == "ok":
            historical_status = "stale"

    from .llm_context import load_llm_context

    llm_context = load_llm_context(
        enabled=scope.llm_strategy_enabled,
        owned_llm_position="swing_llm_driven" in set(scope.owner_map.values()),
        llm_config=scope.llm_config,
        checked_at=lambda: datetime.now(timezone.utc),
    )
    scope.scan_errors.extend(llm_context.errors)

    regime_macro_series = {}
    regime_macro_diagnostics = {"status": "not_loaded"}
    try:
        regime_macro_series, regime_macro_diagnostics = load_fred_regime_series(refresh=True)
    except Exception as exc:
        regime_macro_diagnostics = {"status": "unavailable", "error": type(exc).__name__}
        scope.scan_errors.append(f"regime macro data: {type(exc).__name__}: {exc}")
    regime_config = get_algo_regime_config()
    try:
        regime = rank_strategies(
            daily_bars.get("SPY", []), mode=scope.mode,
            llm_enabled=llm_context.available,
            llm_disable_in_volatile=scope.llm_config.disable_in_volatile,
            intraday_trend_enabled=scope.runtime_settings.enable_intraday_trend_following,
            intraday_momentum_enabled=scope.runtime_settings.enable_intraday_momentum,
            feature_set=regime_config["feature_set"],
            engine=regime_config["engine"],
            strategy_policy=regime_config["strategy_policy"],
            context_bars=daily_bars,
            macro_series=regime_macro_series,
            include_models=regime_config["include_models"],
            model_training_window=regime_config["model_training_window"],
            model_min_confidence=regime_config["min_model_confidence"],
            strategy_regime_weights=regime_config["strategy_regime_weights"],
            strategy_enabled=regime_config["strategy_enabled"],
            strategy_risk_scales=regime_config["strategy_risk_scales"],
            volatile_policy_risk_multiplier=regime_config["volatile_policy_risk_multiplier"],
            volatility_risk_scales=regime_config["volatility_risk_scales"],
        )
        regime["macro_data"] = regime_macro_diagnostics
    except Exception as exc:
        regime = {
            "regime": "unknown", "reason": "SPY regime calculation failed",
            "ranked": [], "active": [],
        }
        scope.scan_errors.append(f"SPY regime calculation: {type(exc).__name__}: {exc}")
        scope.entry_pause_reasons.append("SPY regime calculation failed")
    if regime.get("regime") == "unknown":
        scope.entry_pause_reasons.append("deterministic SPY regime is unavailable")
        if historical_status == "ok":
            historical_status = "degraded"
    regime_name = str(regime.get("regime", "unknown"))

    intraday_recent: dict = {}
    if scope.session_name and scope.mode == "algo":
        # Four days of five-minute bars, so the reversion reference exists from the first bar of the session.
        try:
            recent = _fetch_bars(
                scope.strategy_symbols, mode=scope.mode, timeframe="5Min",
                start=(scope.now - timedelta(days=4)).isoformat(), end=scope.now.isoformat(),
                limit=10000, feed=scope.data_feed,
            )
            intraday_recent = {ticker: list(rows)[-40:] for ticker, rows in recent.items()}
        except Exception as exc:
            scope.scan_errors.append(f"recent intraday bars: {type(exc).__name__}: {exc}")

    hourly_by_symbol: dict = {}
    if scope.session_name and scope.mode == "algo" and _weekly_enabled():
        try:
            hourly_by_symbol = _weekly_hourly_bars(scope)
        except Exception as exc:
            scope.scan_errors.append(f"weekly hourly bars: {type(exc).__name__}: {exc}")

    multi_factor_parameters = parameters_for_regime("swing_multi_factor", regime_name)
    if regime_config["strategy_regime_weights"]["swing_multi_factor"].get("volatile", 0) > 0:
        multi_factor_parameters["disable_in_volatile"] = False

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
        llm_generator=llm_context.generator,
        llm_signal_diagnostics=llm_context.diagnostics,
        llm_signal_status=llm_context.signal_status,
        regime=regime,
        regime_name=regime_name,
        multi_factor_parameters=multi_factor_parameters,
        intraday_recent_by_symbol=intraday_recent,
        hourly_by_symbol=hourly_by_symbol,
    )
