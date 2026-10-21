"""Shared scan orchestration for Swing, Day, and the dedicated Algo portfolio."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import logging
import os
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from options.risk import load_options_delta_overlay

from market.data import feeds_for_session, historical_delay_seconds, regular_feed
from market.regime import load_fred_regime_series
from portfolio.config import (
    ALGO_REGIME_CONFIG,
    ALGO_REGIME_WARMUP_CALENDAR_DAYS,
    ALGO_SIGNAL_DATA_CONFIG,
    STRATEGY_CONFIG,
    get_algo_runtime_settings,
    get_mode_config,
)
from strategies.event_driven import EventCalendar, EventSignalGenerator, EventStrategyConfig
from .event_context import load_event_context
from strategies.portfolio_coordinator import (
    INTRADAY_STRATEGIES,
    PortfolioCoordinator,
    PortfolioCoordinatorConfig,
    strategy_priority,
    throttle_signal,
)
from strategies.regime import rank_strategies
from strategies.regime_advisor import apply_regime_advisory
from strategies.registry import (
    ALGO_DEFAULT_PARAMETERS,
    parameters_for_regime,
    profile_id_for_regime,
    refresh_active_parameter_profile,
)
from strategies.runner.data import (
    _completed_daily,
    _completed_intraday,
    _latest_bar_age_seconds,
    current_portfolio as _current_portfolio,
    fetch_bars as _fetch_bars,
    fetch_latest_quotes as _fetch_latest_quotes,
)
from strategies.runner.factors import (
    _multi_factor_current_targets,
    _multi_factor_rebalance_signals,
    multi_factor_parameters_for_session as _multi_factor_parameters_for_session,
)
from strategies.runner.signal_generation import (
    SignalGenerationContext,
    generate_strategy_signals,
)
from strategies.runner.sizing import (
    _is_protective_sell_order,
    _make_trade,
    _positive_price_level,
)
from strategies.satellite_overlay import (
    CORE_STRATEGIES,
    SATELLITE_STRATEGIES,
    SatelliteOverlayConfig,
)
from strategies.state_machine import ACTIVE_PHASES, RuleStrategyStateMachine
from strategies.universe import configured_universe, load_algo_universe
from strategies.momentum.swing import generate_signals as generate_swing_momentum_signals
from strategies.multi_factor import MultiFactorConfig, MultiFactorSignal

LOG = logging.getLogger("strategy_runner")
ET = ZoneInfo("America/New_York")
MAX_SPY_DAILY_AGE_DAYS = ALGO_SIGNAL_DATA_CONFIG["max_spy_daily_age_days"]


_DAILY_BAR_CACHE_LOCK = RLock()
_DAILY_BAR_CACHE: dict[tuple, dict[str, list[dict]]] = {}


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
    """Run one complete scan using the coordinator's portfolio state."""
    ALGO_RUNTIME_SETTINGS = get_algo_runtime_settings()
    refresh_active_parameter_profile()

    if mode != "algo":
        raise ValueError("Only the algo portfolio scan is supported")
    if position_owners is None or strategy_states is None:
        raise ValueError("Algo scans require the coordinator's position owners and strategy states")
    if session_name:
        default_bar_feed, default_quote_feed = feeds_for_session(session_name)
        # Plus uses BOATS for both overnight bars and quotes.
        data_feed = default_bar_feed if session_name == "overnight" else (data_feed or default_bar_feed)
        quote_feed = quote_feed or default_quote_feed
    else:
        data_feed = data_feed or regular_feed()
        quote_feed = quote_feed or data_feed
    mode_config = get_mode_config(mode)
    now = datetime.now(ET)
    today = trading_date or now.date().isoformat()
    multi_factor_parameters, multi_factor_parameter_source = _multi_factor_parameters_for_session()
    if universe_result is not None:
        universe = universe_result
    else:
        universe = load_algo_universe()
    selected_symbols = [str(symbol).upper() for symbol in universe.get("tickers", [])]
    event_config = EventStrategyConfig.from_dict(ALGO_DEFAULT_PARAMETERS.get("event_driven"))
    satellite_config = SatelliteOverlayConfig.from_dict({
        **(ALGO_DEFAULT_PARAMETERS.get("satellite_overlay") or {}),
        "multi_factor_enabled": ALGO_RUNTIME_SETTINGS.algo_multi_factor_enabled,
        "event_driven_enabled": ALGO_RUNTIME_SETTINGS.algo_event_driven_enabled,
    })
    event_strategy_enabled = event_config.enabled and ALGO_RUNTIME_SETTINGS.algo_event_driven_enabled
    excluded = {str(symbol).upper() for symbol in (excluded_symbols or set())}
    track_owners = position_owners is not None
    owner_map = position_owners if position_owners is not None else {}
    risk_config = mode_config["risk"]
    coordinator_config = PortfolioCoordinatorConfig.from_dict()
    scan_errors: list[str] = []
    entry_pause_reasons: list[str] = []
    if not selected_symbols:
        entry_pause_reasons.append("the Algo universe is empty")
    if session_name == "overnight" and not bool(
        ALGO_RUNTIME_SETTINGS.overnight_entries_enabled
    ):
        entry_pause_reasons.append("overnight entries are disabled by configuration")

    # Broker holdings are fetched and reconciled before choosing scan symbols.
    # This lets a recovered owner receive quote-based protection in this scan.
    account, positions, open_orders = _current_portfolio(mode)
    pending_by_symbol: dict[str, list[dict]] = {}
    for order in open_orders:
        ticker = str(order.get("symbol", "")).upper()
        if ticker:
            pending_by_symbol.setdefault(ticker, []).append(order)
    lifecycle = None
    if strategy_states is not None:
        lifecycle = RuleStrategyStateMachine(
            strategy_states, owner_map,
            cooldown_minutes=risk_config.get("cooldown_minutes_by_strategy"),
            minimum_hold_minutes=risk_config.get("minimum_hold_minutes_by_strategy"),
            max_entries_per_day=risk_config.get("max_entries_per_strategy_per_day"),
            max_positions_per_strategy=risk_config.get("max_positions_per_strategy"),
            max_positions_per_strategy_by_strategy=risk_config.get("max_positions_per_strategy_by_strategy"),
        )
        lifecycle.reconcile(positions, open_orders)
    unowned_positions = sorted(
        ticker for ticker, position in positions.items()
        if float(position.get("qty", 0) or 0) != 0 and ticker not in owner_map
    ) if track_owners else []
    if unowned_positions:
        entry_pause_reasons.append("broker positions have no verified strategy owner")

    held_symbol_set = {str(symbol).upper() for symbol in owner_map} if track_owners else set()
    if strategy_states is not None:
        for by_symbol in strategy_states.values():
            for symbol, state in by_symbol.items():
                if state.get("phase") in ACTIVE_PHASES:
                    held_symbol_set.add(str(symbol).upper())
    held_symbols = sorted(held_symbol_set)
    strategy_symbols = list(dict.fromkeys(
        [symbol for symbol in selected_symbols if symbol not in excluded] + held_symbols
    ))
    # SPY is part of the fixed Algo universe and remains available to macro
    # event signals; ordinary strategies still use their configured symbols.
    if event_strategy_enabled and "SPY" not in strategy_symbols:
        strategy_symbols.append("SPY")
    regime_context_symbols = (
        configured_universe("algo")["tickers"] + ["HYG", "LQD"]
    )
    symbols_for_bars = list(dict.fromkeys(strategy_symbols + ["SPY"] + regime_context_symbols))

    now_utc = datetime.now(timezone.utc)
    factor_config_values = dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor") or {})
    factor_config_values.update(multi_factor_parameters)
    factor_config = MultiFactorConfig.from_dict(factor_config_values)
    daily_history_days = max(
        factor_config.warmup_calendar_days(), ALGO_REGIME_WARMUP_CALENDAR_DAYS,
    )
    daily_start = (now_utc - timedelta(days=daily_history_days)).date().isoformat()
    daily_feed = regular_feed()
    historical_status = "ok"
    historical_bar_end: str | None = None
    try:
        local_snapshot_time = now_utc.astimezone(ET)
        snapshot_phase = "postclose" if (
            local_snapshot_time.hour > 16
            or (local_snapshot_time.hour == 16 and local_snapshot_time.minute >= 15)
        ) else "intraday"
        cache_key = (
            today, daily_start, daily_feed, tuple(symbols_for_bars), snapshot_phase,
            _fetch_bars,
        )
        with _DAILY_BAR_CACHE_LOCK:
            cached = _DAILY_BAR_CACHE.get(cache_key)
            if cached is None:
                cached = _fetch_bars(
                    symbols_for_bars, mode=mode, timeframe="1Day",
                    start=daily_start, end=now_utc.isoformat(), limit=10000,
                    feed=daily_feed, adjustment="split",
                )
                _DAILY_BAR_CACHE.clear()
                _DAILY_BAR_CACHE[cache_key] = {
                    ticker: list(rows) for ticker, rows in cached.items()
                }
            daily_bars = {ticker: list(rows) for ticker, rows in cached.items()}
        daily_bars = {ticker: _completed_daily(bars, today) for ticker, bars in daily_bars.items()}
    except Exception as exc:
        daily_bars = {}
        historical_status = "degraded"
        scan_errors.append(f"daily historical bars: {type(exc).__name__}: {exc}")
        entry_pause_reasons.append("daily historical bars are unavailable")

    intraday_bars = {}
    if session_name:
        if session_name == "overnight":
            bar_day = now.date() if now.hour >= 20 else now.date() - timedelta(days=1)
            start_at = datetime.combine(bar_day, datetime.min.time(), tzinfo=ET).replace(hour=20)
        elif session_name == "premarket":
            start_at = datetime.fromisoformat(f"{today}T04:00:00").replace(tzinfo=ET)
        else:
            start_at = datetime.fromisoformat(f"{today}T09:30:00").replace(tzinfo=ET)
        delay_seconds = historical_delay_seconds(data_feed, session_name)
        bar_end = now - timedelta(seconds=delay_seconds) if delay_seconds else now
        historical_bar_end = bar_end.isoformat()
        if bar_end > start_at:
            try:
                intraday_bars = _fetch_bars(
                    strategy_symbols,
                    mode=mode,
                    timeframe="5Min",
                    start=start_at.isoformat(),
                    end=historical_bar_end,
                    limit=10000,
                    feed=data_feed,
                )
            except Exception as exc:
                historical_status = "degraded"
                scan_errors.append(f"intraday historical bars ({data_feed}): {type(exc).__name__}: {exc}")
                entry_pause_reasons.append("intraday historical bars are unavailable")
        elif historical_status == "ok":
            historical_status = "warming_up"
            entry_pause_reasons.append("waiting for completed intraday historical bars")
    elif now >= now.replace(hour=9, minute=30, second=0, microsecond=0):
        session_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        intraday_bars = _fetch_bars(
            strategy_symbols,
            mode=mode,
            timeframe="5Min",
            start=session_open.isoformat(),
            end=now.isoformat(),
            limit=10000,
            feed=data_feed,
        )
    try:
        bars_by_symbol = {
            ticker: _completed_intraday(bars, now=now)
            for ticker, bars in intraday_bars.items()
        }
    except Exception as exc:
        bars_by_symbol = {}
        historical_status = "degraded"
        scan_errors.append(f"intraday bar validation: {type(exc).__name__}: {exc}")
        entry_pause_reasons.append("intraday historical bars are invalid")
    if strategy_symbols and not any(bars_by_symbol.values()) and historical_status == "ok":
        historical_status = "warming_up"
        entry_pause_reasons.append("no completed intraday historical bars are available yet")

    delay_seconds = historical_delay_seconds(data_feed, session_name or "")
    signal_bar_max_age_seconds = (
        ALGO_SIGNAL_DATA_CONFIG["delayed_signal_bar_max_age_minutes"] if delay_seconds
        else ALGO_SIGNAL_DATA_CONFIG["signal_bar_max_age_minutes"]
    ) * 60
    overnight_swing_signal_bar_max_age_seconds = (
        ALGO_SIGNAL_DATA_CONFIG["overnight_swing_signal_bar_max_age_minutes"] * 60
        if session_name == "overnight" else 0
    )
    fresh_signal_bar_symbols = {
        ticker for ticker, bars in bars_by_symbol.items()
        if (age := _latest_bar_age_seconds(bars, now)) is not None
        and 0 <= age <= signal_bar_max_age_seconds
    }
    overnight_swing_signal_bar_symbols = {
        ticker for ticker, bars in bars_by_symbol.items()
        if overnight_swing_signal_bar_max_age_seconds
        and (age := _latest_bar_age_seconds(bars, now)) is not None
        and signal_bar_max_age_seconds < age <= overnight_swing_signal_bar_max_age_seconds
    }
    eligible_signal_bar_symbols = fresh_signal_bar_symbols | overnight_swing_signal_bar_symbols
    if selected_symbols and not eligible_signal_bar_symbols.intersection(selected_symbols):
        entry_pause_reasons.append("the Algo universe has no recent completed signal bars")
        if historical_status == "ok":
            historical_status = "stale"

    spy_last_completed_date: str | None = None
    spy_daily_age_days: int | None = None
    if daily_bars.get("SPY"):
        try:
            spy_last_completed_date = str(daily_bars["SPY"][-1]["t"])[:10]
            spy_daily_age_days = (date.fromisoformat(today) - date.fromisoformat(spy_last_completed_date)).days
        except (KeyError, TypeError, ValueError):
            spy_daily_age_days = None
    if spy_daily_age_days is None or not 1 <= spy_daily_age_days <= MAX_SPY_DAILY_AGE_DAYS:
        entry_pause_reasons.append("the latest completed SPY daily bar is stale or invalid")
        if historical_status == "ok":
            historical_status = "stale"

    from runtime.paths import DATA_DIR
    default_calendar = DATA_DIR / "event_calendar.csv"
    calendar_path = Path(os.environ.get("ALGO_EVENT_CALENDAR", str(default_calendar)))
    event_context = load_event_context(
        enabled=event_strategy_enabled,
        owned_event_position="event_driven" in set(owner_map.values()),
        event_config=event_config,
        calendar_path=calendar_path,
        checked_at=lambda: datetime.now(timezone.utc),
        calendar_type=EventCalendar,
        generator_type=EventSignalGenerator,
    )
    event_generator = event_context.generator
    event_strategy_available = event_context.available
    event_calendar_diagnostics = event_context.diagnostics
    event_calendar_status = event_context.calendar_status
    scan_errors.extend(event_context.errors)
    regime_macro_series = {}
    regime_macro_diagnostics = {"status": "not_loaded"}
    try:
        regime_macro_series, regime_macro_diagnostics = load_fred_regime_series(refresh=True)
    except Exception as exc:
        regime_macro_diagnostics = {"status": "unavailable", "error": type(exc).__name__}
        scan_errors.append(f"regime macro data: {type(exc).__name__}: {exc}")
    try:
        regime = rank_strategies(
            daily_bars.get("SPY", []), mode=mode, event_enabled=event_strategy_available,
            event_disable_in_volatile=event_config.disable_in_volatile,
            intraday_trend_enabled=ALGO_RUNTIME_SETTINGS.enable_intraday_trend,
            intraday_momentum_enabled=ALGO_RUNTIME_SETTINGS.enable_intraday_momentum,
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
        regime = {"regime": "unknown", "reason": "SPY regime calculation failed", "ranked": [], "active": []}
        scan_errors.append(f"SPY regime calculation: {type(exc).__name__}: {exc}")
        entry_pause_reasons.append("SPY regime calculation failed")
    try:
        regime = apply_regime_advisory(regime)
    except Exception as exc:
        # The optional ranking advisor must never interrupt deterministic
        # strategy exits or quote-driven protection.
        scan_errors.append(f"optional regime advisor: {type(exc).__name__}: {exc}")
        regime["regime_advisory"] = {"status": "fallback", "error": str(exc)}
    if regime.get("regime") == "unknown":
        entry_pause_reasons.append("deterministic SPY regime is unavailable")
        if historical_status == "ok":
            historical_status = "degraded"
    regime_name = str(regime.get("regime", "unknown"))
    multi_factor_parameters = parameters_for_regime("multi_factor", regime_name)
    allowed_satellites = set(regime.get("satellites", []))
    equity = float(account.get("equity", 0) or 0)
    quote_errors: list[str] = []
    quote_fetch_diagnostics: dict[str, dict] = {}
    latest_quotes = _fetch_latest_quotes(
        strategy_symbols, mode=mode, feed=quote_feed,
        errors=quote_errors,
        diagnostics=quote_fetch_diagnostics,
    )
    freshness_limit = int(STRATEGY_CONFIG["quote_stale_seconds"])
    quote_checked_at = datetime.now(timezone.utc)
    fresh_quote_symbols = {
        ticker for ticker, quote in latest_quotes.items()
        if 0 <= (quote_checked_at - quote["timestamp"]).total_seconds() <= freshness_limit
    }
    owned_positions_without_fresh_quotes = sorted(
        ticker for ticker, position in positions.items()
        if ticker in owner_map and float(position.get("qty", 0) or 0) != 0
        and ticker not in fresh_quote_symbols
    ) if track_owners else []
    owned_position_quote_diagnostics = []
    invalid_quote_symbols = sorted(
        ticker for ticker, diagnostic in quote_fetch_diagnostics.items()
        if str(diagnostic.get("status", "")).startswith("invalid_")
    )
    if mode == "algo":
        for ticker, position in sorted(positions.items()):
            if ticker not in owner_map or float(position.get("qty", 0) or 0) == 0:
                continue
            quote = latest_quotes.get(ticker)
            fetch_diagnostic = quote_fetch_diagnostics.get(ticker) or {"status": "missing"}
            timestamp = quote.get("timestamp") if quote else None
            if timestamp is None and fetch_diagnostic.get("quote_timestamp"):
                try:
                    timestamp = datetime.fromisoformat(fetch_diagnostic["quote_timestamp"].replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    timestamp = None
            age_seconds = (quote_checked_at - timestamp).total_seconds() if timestamp is not None else None
            if quote is not None:
                if age_seconds is not None and age_seconds < 0:
                    status = "future_timestamp"
                elif age_seconds is not None and age_seconds <= freshness_limit:
                    status = "fresh"
                else:
                    status = "stale"
            else:
                status = fetch_diagnostic.get("status", "missing")
            owned_position_quote_diagnostics.append({
                "symbol": ticker,
                "feed": quote_feed,
                "status": status,
                "quote_availability": (
                    "session_limited"
                    if session_name in {"premarket", "after_hours", "overnight"}
                    and not quote_errors and status in {"missing", "stale"}
                    else status
                ),
                "overnight_tradable": (
                    ticker in overnight_tradable_tickers
                    if session_name == "overnight" and overnight_tradable_tickers is not None
                    else None
                ),
                "quote_timestamp": timestamp.isoformat() if timestamp is not None else None,
                "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                "freshness_limit_seconds": freshness_limit,
            })
    owned_positions_without_software_stops = sorted(
        ticker for ticker, position in positions.items()
        if ticker in owner_map and float(position.get("qty", 0) or 0) != 0
        and _positive_price_level((strategy_states or {}).get(owner_map[ticker], {}).get(ticker, {}).get("stop_price")) is None
    ) if track_owners else []
    if mode == "algo":
        if quote_errors:
            scan_errors.extend(quote_errors)
            entry_pause_reasons.append("latest quote requests failed")
        if owned_positions_without_fresh_quotes:
            entry_pause_reasons.append("owned positions lack fresh protective quotes")
        if owned_positions_without_software_stops:
            entry_pause_reasons.append("owned positions lack valid software stop levels")
        if selected_symbols and not fresh_quote_symbols.intersection(selected_symbols):
            entry_pause_reasons.append("the Algo universe has no fresh quotes")
    selected_symbols_with_fresh_quotes = fresh_quote_symbols.intersection(selected_symbols)
    session_limited_quotes = bool(
        mode == "algo"
        and session_name in {"premarket", "after_hours", "overnight"}
        and not quote_errors
        and not invalid_quote_symbols
        and (
            owned_positions_without_fresh_quotes
            or (selected_symbols and not selected_symbols_with_fresh_quotes)
        )
    )
    quote_status = (
        "idle" if not strategy_symbols else
        "degraded" if quote_errors or invalid_quote_symbols or (
            session_name == "regular" and owned_positions_without_fresh_quotes
        ) else
        "session_limited" if session_limited_quotes else
        "unavailable" if not fresh_quote_symbols else "ok"
    )
    multi_factor_signals: dict[str, MultiFactorSignal] = {}
    multi_factor_diagnostics = None
    if (
        mode == "algo"
        and (satellite_config.multi_factor_enabled or "multi_factor" in set(owner_map.values()))
        and fresh_quote_symbols.intersection(strategy_symbols)
    ):
        try:
            multi_factor_signals, multi_factor_diagnostics = _multi_factor_rebalance_signals(
                strategy_states, daily_bars, selected_symbols, owner_map, date.fromisoformat(today),
                regime=str(regime.get("regime", "unknown")),
                intraday_bars=bars_by_symbol,
                positions=positions,
                parameters=multi_factor_parameters,
                groups_by_symbol=universe.get("sector_by_symbol"),
                tradable_symbols=(
                    set(overnight_tradable_tickers if overnight_tradable_tickers is not None else universe.get("overnight_tradable_tickers", []))
                    if session_name == "overnight"
                    and (universe.get("overnight_metadata_status") == "ok" or overnight_tradable_tickers is not None)
                    else None
                ),
            )
        except Exception as exc:
            scan_errors.append(f"multi-factor rebalance: {type(exc).__name__}: {exc}")
    rank_score = {
        entry["strategy"]: entry.get("selection_score", entry["score"])
        for entry in regime.get("ranked", [])
    }
    momentum_signals: dict[str, object] = {}
    momentum_diagnostics = None
    if mode == "algo":
        try:
            momentum_parameters = parameters_for_regime("momentum", regime_name)
            momentum_parameters.update({
                "hard_stop_pct": risk_config["stop_loss_pct_by_strategy"]["momentum"],
                "trailing_stop_pct": risk_config["trailing_stop_pct_by_strategy"]["momentum"],
                "trailing_activation_pct": risk_config["trailing_activation_pct_by_strategy"]["momentum"],
            })
            held_momentum = {}
            for ticker, owner in owner_map.items():
                if owner != "momentum":
                    continue
                state = (strategy_states or {}).get("momentum", {}).get(ticker, {})
                held_momentum[ticker] = {
                    "qty": float((positions.get(ticker) or {}).get("qty", 0) or 0),
                    "entry_at": state.get("opened_at"),
                    "entry_price": (positions.get(ticker) or {}).get("avg_entry_price"),
                }
            momentum_signals, momentum_diagnostics = generate_swing_momentum_signals(
                daily_bars, symbols=selected_symbols, as_of=date.fromisoformat(today) - timedelta(days=1),
                regime=str(regime.get("regime", "unknown")), held_positions=held_momentum,
                parameters=momentum_parameters,
            )
        except Exception as exc:
            scan_errors.append(f"daily momentum ranking: {type(exc).__name__}: {exc}")
            entry_pause_reasons.append("daily momentum ranking failed")
    factor_confirmation_signals = (
        _multi_factor_current_targets(
            strategy_states, selected_symbols, date.fromisoformat(today),
            parameters=multi_factor_parameters,
        )
        if mode == "algo" and satellite_config.multi_factor_enabled
        else {}
    )
    quote_check_time = datetime.now(timezone.utc)
    generated, satellite_confirmation_counts = generate_strategy_signals(
        SignalGenerationContext(
            mode=mode,
            strategy_symbols=strategy_symbols,
            positions=positions,
            bars_by_symbol=bars_by_symbol,
            owner_map=owner_map,
            track_owners=track_owners,
            coordinator_config=coordinator_config,
            now=now,
            regular_close=regular_close,
            session_name=session_name,
            regime=regime,
            factor_confirmation_signals=factor_confirmation_signals,
            satellite_config=satellite_config,
            allowed_satellites=allowed_satellites,
            event_generator=event_generator,
            event_strategy_enabled=event_strategy_enabled,
            strategy_states=strategy_states,
            daily_bars=daily_bars,
            now_utc=now_utc,
            quote_check_time=quote_check_time,
            selected_symbols=selected_symbols,
            latest_quotes=latest_quotes,
            risk_config=risk_config,
            rank_score=rank_score,
            lifecycle=lifecycle,
            multi_factor_signals=multi_factor_signals,
            momentum_signals=momentum_signals,
            runtime_settings=ALGO_RUNTIME_SETTINGS,
            pending_by_symbol=pending_by_symbol,
            universe=universe,
            fresh_signal_bar_symbols=fresh_signal_bar_symbols,
            overnight_swing_signal_bar_symbols=overnight_swing_signal_bar_symbols,
            scan_errors=scan_errors,
            entry_pause_reasons=entry_pause_reasons,
        )
    )

    entries_paused = bool(entry_pause_reasons)
    if entries_paused:
        reason = "; ".join(dict.fromkeys(entry_pause_reasons))
        for item in generated:
            if item["action"] in {"buy", "short"} and not item.get("suppressed_reason"):
                item["suppressed_reason"] = reason

    trades = []
    now_utc = datetime.now(timezone.utc)
    strategy_entry_counts = {
        strategy: sum(
            int(state.get("entries_today", 0) or 0)
            for state in by_symbol.values()
            if state.get("entry_day") == today and state.get("strategy") == strategy
        )
        for strategy, by_symbol in (strategy_states or {}).items()
    }
    strategy_open_counts = {
        strategy: sum(owner == strategy for owner in owner_map.values())
        for strategy in set(CORE_STRATEGIES) | set(SATELLITE_STRATEGIES) | {
            "intraday_trend", "intraday_momentum", "multi_factor", "event_driven",
        }
    }
    group_limits = risk_config.get("strategy_group_limits", {})
    group_entry_counts = {
        name: sum(
            int(state.get("entries_today", 0) or 0)
            for strategy, by_symbol in (strategy_states or {}).items()
            if strategy in set(group.get("strategies", []))
            for state in by_symbol.values()
            if state.get("entry_day") == today and state.get("strategy") == strategy
        )
        for name, group in group_limits.items()
    }
    strategy_caps = risk_config.get("max_positions_per_strategy_by_strategy", {})
    batch_entry_counts: dict[str, int] = {}
    batch_open_counts: dict[str, int] = {}
    if mode == "algo":
        coordinator = PortfolioCoordinator(coordinator_config)
        entries_today = sum(
            int(state.get("entries_today", 0) or 0)
            for by_symbol in (strategy_states or {}).values()
            for state in by_symbol.values()
            if state.get("entry_day") == today
        )
        intraday_entries_today = sum(
            int(state.get("entries_today", 0) or 0)
            for strategy, by_symbol in (strategy_states or {}).items()
            if strategy in INTRADAY_STRATEGIES
            for state in by_symbol.values()
            if state.get("entry_day") == today
        )
        pending_buys: dict[str, float] = {}
        pending_shorts: dict[str, float] = {}
        for order in open_orders:
            ticker = str(order.get("symbol", "")).upper()
            if not ticker or float((positions.get(ticker) or {}).get("qty", 0) or 0) != 0:
                continue
            side = str(order.get("side", "")).lower()
            if side not in {"buy", "sell"}:
                continue
            qty = max(0.0, float(order.get("qty") or 0) - float(order.get("filled_qty") or 0))
            reference = max((
                float(order.get(key) or 0)
                for key in ("limit_price", "stop_price", "filled_avg_price")
            ), default=0.0)
            if reference <= 0:
                quote = latest_quotes.get(ticker)
                reference = float(quote.get("price") or 0) if quote else 0.0
            if qty <= 0 or reference <= 0:
                continue
            target = pending_buys if side == "buy" else pending_shorts
            target[ticker] = target.get(ticker, 0.0) + qty * reference
        options_delta_overlay, options_overlay_status = load_options_delta_overlay()
        coordinator_book = coordinator.new_book(
            equity=equity,
            positions=positions,
            position_owners=owner_map,
            pending_buy_values=pending_buys,
            pending_short_values=pending_shorts,
            options_delta_notional_by_underlying=options_delta_overlay,
            entries_today=entries_today,
            intraday_entries_today=intraday_entries_today,
            swing_entries_today=group_entry_counts.get("swing", 0),
        )
    else:
        options_delta_overlay, options_overlay_status = {}, {"available": False, "reason": "equity options overlay is Algo-only"}

    # Resolve shared limits in strategy-priority order so lower-priority
    # candidates cannot consume the daily budget before the core swing book.
    ordered_generated = list(generated)
    if mode == "algo":
        ordered_generated = sorted(ordered_generated, key=lambda item: (
            0 if item.get("software_protection") else
            1 if item.get("action") in {"sell", "cover"} else
            2 + strategy_priority(str(item.get("strategy", ""))),
            -float(item.get("rank_score", 0) or 0), str(item.get("ticker", "")),
        ))
    for item in ordered_generated:
        if item.get("suppressed_reason"):
            continue
        action = str(item.get("action", "")).lower()
        strategy = str(item.get("strategy", ""))
        if action in {"buy", "short"}:
            strategy_limit = risk_config.get("max_entries_per_strategy_per_day", {}).get(strategy)
            already_entered = strategy_entry_counts.get(strategy, 0) + batch_entry_counts.get(strategy, 0)
            if strategy_limit is not None and already_entered >= int(strategy_limit):
                item["suppressed_reason"] = "per-strategy daily entry limit reached"
                continue
            position_limit = strategy_caps.get(strategy, risk_config.get("max_positions_per_strategy"))
            already_open = strategy_open_counts.get(strategy, 0) + batch_open_counts.get(strategy, 0)
            if position_limit is not None and already_open >= int(position_limit):
                item["suppressed_reason"] = "per-strategy open-position limit reached"
                continue
            group_blocked = False
            for group_name, group in group_limits.items():
                if strategy not in group.get("strategies", []):
                    continue
                if group_entry_counts.get(group_name, 0) >= int(group.get("max_entries_per_day", 10**9)):
                    item["suppressed_reason"] = f"{group_name} daily-entry budget reached"
                    group_blocked = True
                if group_blocked:
                    break
            if group_blocked:
                continue
        quote = latest_quotes.get(item["ticker"])
        if quote is None:
            item["suppressed_reason"] = "latest Alpaca quote unavailable"
            continue
        price = float(quote["price"])
        quote_time = quote["timestamp"]
        age = (now_utc - quote_time).total_seconds()
        if age < 0 or age > freshness_limit:
            item["suppressed_reason"] = f"latest Alpaca quote is stale ({age:.0f}s)"
            continue
        if extended_hours:
            spread_pct = (float(quote["ask"]) - float(quote["bid"])) / price if price else 1.0
            if item["action"] == "buy" and spread_pct > float(STRATEGY_CONFIG["extended_hours_max_spread_pct"]):
                item["suppressed_reason"] = f"extended-hours spread is too wide ({spread_pct:.1%})"
                continue
            if session_name == "overnight" and item["ticker"] not in (overnight_tradable_tickers or set()):
                item["suppressed_reason"] = "Alpaca does not mark this asset overnight_tradable"
                continue
        execution_price = (
            float(quote["ask"] if item["action"] in {"buy", "cover"} else quote["bid"])
            if extended_hours else price
        )
        sizing_signal = item
        throttle_note = None
        if mode == "algo" and action in {"buy", "short"}:
            sizing_signal, throttle_note = throttle_signal(
                item, str(regime.get("regime", "unknown")), coordinator_config,
                risk_multiplier=float(regime.get("strategy_risk_multipliers", {}).get(item["strategy"], 1.0)),
            )
            if throttle_note:
                LOG.info("%s for %s", throttle_note, item["ticker"])
        trade = _make_trade(
            sizing_signal, execution_price, equity,
            positions.get(item["ticker"], {}), mode_config["risk"],
            mode_universe=mode_config.get("universe"),
        )
        if trade:
            if mode == "algo" and action in {"buy", "short"}:
                decision = coordinator_book.review(
                    trade, session=session_name or "regular", timestamp=now,
                    regime=str(regime.get("regime", "unknown")),
                    volatility_state=str(regime.get("volatility_state", "unknown")),
                    session_open=regular_open, session_close=regular_close,
                )
                if not decision.approved or decision.proposal is None:
                    item["suppressed_reason"] = f"portfolio coordinator: {decision.reason}"
                    continue
                trade = decision.proposal
            if extended_hours:
                trade.update({
                    "order_type": "limit",
                    "limit_price": execution_price,
                    "extended_hours": True,
                })
            signal_kind = "protective" if item.get("software_protection") else "rule"
            identity = (
                f"{mode}:{today}:{item['strategy']}:{item['ticker']}:{item['action']}:{signal_kind}:"
                f"{item.get('event_id') or ''}:{item.get('signal_bar_time') or ''}"
            )
            trade["client_order_id"] = f"rule-{today.replace('-', '')}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
            trade["signal_bar_time"] = item.get("signal_bar_time")
            if mode == "algo":
                trade["market_regime"] = regime_name
                owner_state = (strategy_states or {}).get(item["strategy"], {}).get(item["ticker"], {})
                trade["research_profile_id"] = (
                    owner_state.get("research_profile_id")
                    if item["action"] in {"sell", "cover"} and owner_state.get("research_profile_id")
                    else profile_id_for_regime(item["strategy"], regime_name)
                )
                trade["research_profile_regime"] = (
                    owner_state.get("research_profile_regime", regime_name)
                    if item["action"] in {"sell", "cover"}
                    else regime_name
                )
            if item.get("event_id"):
                trade["event_id"] = item["event_id"]
            if item.get("strategy") == "event_driven" and event_generator is not None:
                try:
                    event_generator.record_proposal(
                        item, as_of=now_utc,
                        strategy_state=strategy_states.setdefault("event_driven", {}),
                    )
                except (TypeError, ValueError):
                    scan_errors.append("event strategy cooldown state could not be recorded")
            elif item.get("event_confirmation") and event_generator is not None:
                try:
                    signal_time = datetime.fromisoformat(
                        str(item.get("signal_bar_time") or quote_time.isoformat()).replace("Z", "+00:00"),
                    ) + timedelta(minutes=5)
                    event_generator.record_proposal(
                        item["event_confirmation"], as_of=signal_time,
                        strategy_state=strategy_states.setdefault("event_driven", {}),
                    )
                except (TypeError, ValueError):
                    scan_errors.append("event strategy cooldown state could not be recorded")
            item_orders = pending_by_symbol.get(item["ticker"], [])
            held_qty = float((positions.get(item["ticker"]) or {}).get("qty", 0) or 0)
            protective_exit = any(
                _is_protective_sell_order(order, held_qty) for order in item_orders
            )
            if trade["action"] in {"sell", "cover"} and (protective_exit or item.get("coordinator_force_flatten")):
                trade["cancel_open_orders"] = True
            if item.get("software_protection"):
                trade["software_protection"] = True
            if item.get("coordinator_force_flatten"):
                trade["coordinator_force_flatten"] = True
            trades.append(trade)
            if action in {"buy", "short"}:
                batch_entry_counts[strategy] = batch_entry_counts.get(strategy, 0) + 1
                batch_open_counts[strategy] = batch_open_counts.get(strategy, 0) + 1
                for group_name, group in group_limits.items():
                    if strategy in group.get("strategies", []):
                        group_entry_counts[group_name] = group_entry_counts.get(group_name, 0) + 1
            for satellite in item.get("satellite_confirmations", []):
                if satellite in satellite_confirmation_counts:
                    satellite_confirmation_counts[satellite] += 1
            item["entry_price"] = price
            item["qty"] = trade["qty"]
        else:
            item["suppressed_reason"] = "position or strategy sizing produced zero shares"

    return {
        "timestamp": now.isoformat(),
        "trading_mode": mode,
        "universe": universe,
        "market_regime": regime,
        "multi_factor_diagnostics": multi_factor_diagnostics,
        "multi_factor_parameter_profile": multi_factor_parameter_source,
        "multi_factor_execution_mode": "live",
        "satellite_confirmation_counts": satellite_confirmation_counts,
        "event_strategy_diagnostics": (
            {
                **event_generator.diagnostics(datetime.now(timezone.utc)),
                "signal_status": event_calendar_status,
            }
            if event_generator is not None else {
                **event_calendar_diagnostics,
                "enabled": False,
                "configured_enabled": event_config.enabled,
                "status": event_calendar_status,
            }
        ),
        "signals": generated,
        "portfolio_coordinator": (
            {
                "enabled": mode == "algo",
                "options_delta_overlay": {
                    "enabled": bool(options_delta_overlay),
                    "underlyings": options_delta_overlay,
                    "status": options_overlay_status,
                },
                "max_name_pct": coordinator_config.max_name_pct if mode == "algo" else None,
                "sector_caps": dict(coordinator_config.sector_caps) if mode == "algo" else {},
                "max_gross_pct": coordinator_config.max_gross_pct if mode == "algo" else None,
                "max_net_long_pct": coordinator_config.max_net_long_pct if mode == "algo" else None,
                "max_net_short_pct": coordinator_config.max_net_short_pct if mode == "algo" else None,
                "max_daily_entries": coordinator_config.max_daily_entries if mode == "algo" else None,
                "max_intraday_entries": coordinator_config.max_intraday_entries if mode == "algo" else None,
                "allow_opposite_netting": coordinator_config.allow_opposite_netting if mode == "algo" else False,
                "allow_intraday_overnight": coordinator_config.allow_intraday_overnight if mode == "algo" else False,
                "rejections": [
                    {"ticker": item.get("ticker"), "strategy": item.get("strategy"),
                     "action": item.get("action"), "reason": item.get("suppressed_reason")}
                    for item in generated
                    if str(item.get("suppressed_reason", "")).startswith("portfolio coordinator:")
                ],
            }
        ),
        "trades": trades,
        "portfolio_positions": sorted(positions),
        "position_details": positions,
        "pending_symbols": sorted(pending_by_symbol),
        "session": session_name,
        "extended_hours": extended_hours,
        "open_orders": open_orders,
        "entries_paused": entries_paused,
        "entry_pause_reasons": list(dict.fromkeys(entry_pause_reasons)),
        "scan_errors": scan_errors,
        "unowned_positions": unowned_positions,
        "data_status": {
            "historical_bars": historical_status,
            "bar_feed": data_feed,
            "bar_end": historical_bar_end,
            "quote_feed": quote_feed,
            "quotes": quote_status,
            "quote_request_status": "failed" if quote_errors else "succeeded",
            "session_limited_quotes": session_limited_quotes,
            "invalid_quote_symbols": invalid_quote_symbols,
            "fresh_quote_count": len(fresh_quote_symbols),
            "requested_quote_count": len(strategy_symbols),
            "owned_positions_without_fresh_quotes": owned_positions_without_fresh_quotes,
            "owned_position_quote_diagnostics": owned_position_quote_diagnostics,
            "owned_positions_without_software_stops": owned_positions_without_software_stops,
            "historical_delay_seconds": delay_seconds,
            "fresh_signal_bar_count": len(fresh_signal_bar_symbols),
            "signal_bar_max_age_seconds": signal_bar_max_age_seconds if mode == "algo" else None,
            "overnight_swing_extended_age_signal_bar_count": len(overnight_swing_signal_bar_symbols),
            "overnight_swing_signal_bar_max_age_seconds": (
                overnight_swing_signal_bar_max_age_seconds or None
            ),
            "spy_last_completed_date": spy_last_completed_date,
            "spy_daily_age_days": spy_daily_age_days,
        },
    }
