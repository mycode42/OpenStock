"""Shared scan orchestration for Swing, Day, and the dedicated Algo portfolio."""

from __future__ import annotations

from typing import Any, TypedDict


class ScanDependencies(TypedDict):
    """Facade-provided collaborators used by the scan orchestration."""

    ALGO_DEFAULT_PARAMETERS: Any
    ALGO_RULE_MODULES: Any
    ALGO_SIGNAL_DATA_CONFIG: Any
    ACTIVE_PHASES: Any
    CORE_STRATEGIES: Any
    ET: Any
    EventCalendar: Any
    EventSignalGenerator: Any
    EventStrategyConfig: Any
    INTRADAY_STRATEGIES: Any
    LOG: Any
    MAX_SPY_DAILY_AGE_DAYS: Any
    MultiFactorConfig: Any
    Path: Any
    PortfolioCoordinator: Any
    PortfolioCoordinatorConfig: Any
    RULE_MODULES: Any
    RuleStrategyStateMachine: Any
    SATELLITE_STRATEGIES: Any
    STRATEGY_CATALOG: Any
    STRATEGY_CONFIG: Any
    SatelliteOverlayConfig: Any
    __file__: Any
    _completed_daily: Any
    _completed_intraday: Any
    _current_portfolio: Any
    _fetch_bars: Any
    _fetch_latest_quotes: Any
    _latest_bar_age_seconds: Any
    _make_trade: Any
    _multi_factor_rebalance_signals: Any
    _multi_factor_current_targets: Any
    _multi_factor_parameters_for_session: Any
    process_multi_factor_shadow_orders: Any
    _positive_price_level: Any
    _update_trailing_stop: Any
    apply_regime_advisory: Any
    apply_satellite_confirmations: Any
    choose_priority: Any
    configured_universe: Any
    date: Any
    datetime: Any
    evaluate_rule_signal: Any
    evaluate_strategy: Any
    feeds_for_session: Any
    generate_swing_momentum_signals: Any
    get_mode_config: Any
    get_algo_runtime_settings: Any
    hashlib: Any
    historical_delay_seconds: Any
    load_algo_universe: Any
    load_event_context: Any
    os: Any
    rank_strategies: Any
    regular_feed: Any
    throttle_signal: Any
    timedelta: Any
    timezone: Any
    _is_protective_sell_order: Any
    strategy_priority: Any


def scan(
    mode: str = "swing",
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
    multi_factor_shadow: dict | None = None,
    _dependencies: ScanDependencies,
) -> dict:
    """Run one complete scan using the compatibility facade dependencies."""
    # Binding the facade namespace here keeps legacy monkeypatches and adapters effective.
    ALGO_DEFAULT_PARAMETERS = _dependencies["ALGO_DEFAULT_PARAMETERS"]
    ALGO_RUNTIME_SETTINGS = _dependencies["get_algo_runtime_settings"]()
    ALGO_RULE_MODULES = _dependencies["ALGO_RULE_MODULES"]
    ALGO_SIGNAL_DATA_CONFIG = _dependencies["ALGO_SIGNAL_DATA_CONFIG"]
    ACTIVE_PHASES = _dependencies["ACTIVE_PHASES"]
    CORE_STRATEGIES = _dependencies["CORE_STRATEGIES"]
    ET = _dependencies["ET"]
    EventCalendar = _dependencies["EventCalendar"]
    EventSignalGenerator = _dependencies["EventSignalGenerator"]
    EventStrategyConfig = _dependencies["EventStrategyConfig"]
    INTRADAY_STRATEGIES = _dependencies["INTRADAY_STRATEGIES"]
    LOG = _dependencies["LOG"]
    MAX_SPY_DAILY_AGE_DAYS = _dependencies["MAX_SPY_DAILY_AGE_DAYS"]
    MultiFactorConfig = _dependencies["MultiFactorConfig"]
    Path = _dependencies["Path"]
    PortfolioCoordinator = _dependencies["PortfolioCoordinator"]
    PortfolioCoordinatorConfig = _dependencies["PortfolioCoordinatorConfig"]
    RULE_MODULES = _dependencies["RULE_MODULES"]
    RuleStrategyStateMachine = _dependencies["RuleStrategyStateMachine"]
    SATELLITE_STRATEGIES = _dependencies["SATELLITE_STRATEGIES"]
    STRATEGY_CATALOG = _dependencies["STRATEGY_CATALOG"]
    STRATEGY_CONFIG = _dependencies["STRATEGY_CONFIG"]
    SatelliteOverlayConfig = _dependencies["SatelliteOverlayConfig"]
    __file__ = _dependencies["__file__"]
    _completed_daily = _dependencies["_completed_daily"]
    _completed_intraday = _dependencies["_completed_intraday"]
    _current_portfolio = _dependencies["_current_portfolio"]
    _fetch_bars = _dependencies["_fetch_bars"]
    _fetch_latest_quotes = _dependencies["_fetch_latest_quotes"]
    _latest_bar_age_seconds = _dependencies["_latest_bar_age_seconds"]
    _make_trade = _dependencies["_make_trade"]
    _multi_factor_rebalance_signals = _dependencies["_multi_factor_rebalance_signals"]
    _multi_factor_current_targets = _dependencies["_multi_factor_current_targets"]
    _multi_factor_parameters_for_session = _dependencies["_multi_factor_parameters_for_session"]
    process_multi_factor_shadow_orders = _dependencies["process_multi_factor_shadow_orders"]
    _positive_price_level = _dependencies["_positive_price_level"]
    _update_trailing_stop = _dependencies["_update_trailing_stop"]
    apply_regime_advisory = _dependencies["apply_regime_advisory"]
    apply_satellite_confirmations = _dependencies["apply_satellite_confirmations"]
    choose_priority = _dependencies["choose_priority"]
    configured_universe = _dependencies["configured_universe"]
    date = _dependencies["date"]
    datetime = _dependencies["datetime"]
    evaluate_rule_signal = _dependencies["evaluate_rule_signal"]
    evaluate_strategy = _dependencies["evaluate_strategy"]
    feeds_for_session = _dependencies["feeds_for_session"]
    generate_swing_momentum_signals = _dependencies["generate_swing_momentum_signals"]
    get_mode_config = _dependencies["get_mode_config"]
    hashlib = _dependencies["hashlib"]
    historical_delay_seconds = _dependencies["historical_delay_seconds"]
    load_algo_universe = _dependencies["load_algo_universe"]
    load_event_context = _dependencies["load_event_context"]
    os = _dependencies["os"]
    rank_strategies = _dependencies["rank_strategies"]
    regular_feed = _dependencies["regular_feed"]
    throttle_signal = _dependencies["throttle_signal"]
    timedelta = _dependencies["timedelta"]
    timezone = _dependencies["timezone"]
    _is_protective_sell_order = _dependencies["_is_protective_sell_order"]
    strategy_priority = _dependencies["strategy_priority"]

    if mode not in {"swing", "day", "algo"}:
        raise ValueError("Strategy scan mode must be swing, day, or algo")
    if mode == "algo" and (position_owners is None or strategy_states is None):
        raise ValueError("Algo scans require the coordinator's position owners and strategy states")
    if mode == "algo" and session_name:
        default_bar_feed, default_quote_feed = feeds_for_session(session_name)
        # Plus uses BOATS for both overnight bars and quotes.
        data_feed = default_bar_feed if session_name == "overnight" else (data_feed or default_bar_feed)
        quote_feed = quote_feed or default_quote_feed
    else:
        data_feed = data_feed or regular_feed()
        quote_feed = quote_feed or data_feed
    mode_config = get_mode_config(mode)
    multi_factor_execution_mode = str(
        ALGO_RUNTIME_SETTINGS.algo_multi_factor_execution_mode
    ).strip().lower() if mode == "algo" else "live"
    if multi_factor_execution_mode not in {"live", "shadow"}:
        multi_factor_execution_mode = "shadow"
    now = datetime.now(ET)
    today = trading_date or now.date().isoformat()
    multi_factor_parameters, multi_factor_parameter_source = _multi_factor_parameters_for_session(
        date.fromisoformat(today), execution_mode=multi_factor_execution_mode,
    )
    if universe_result is not None:
        universe = universe_result
    elif mode == "algo":
        universe = load_algo_universe()
    else:
        universe = configured_universe(mode)
    selected_symbols = [str(symbol).upper() for symbol in universe.get("tickers", [])]
    event_config = EventStrategyConfig.from_dict(ALGO_DEFAULT_PARAMETERS.get("event_driven"))
    satellite_config = SatelliteOverlayConfig.from_dict({
        **(ALGO_DEFAULT_PARAMETERS.get("satellite_overlay") or {}),
        "multi_factor_enabled": ALGO_RUNTIME_SETTINGS.algo_multi_factor_enabled,
        "event_driven_enabled": ALGO_RUNTIME_SETTINGS.algo_event_driven_enabled,
    })
    event_satellite_enabled = event_config.enabled and satellite_config.event_driven_enabled
    excluded = {str(symbol).upper() for symbol in (excluded_symbols or set())}
    track_owners = position_owners is not None
    owner_map = position_owners if position_owners is not None else {}
    risk_config = mode_config["risk"]
    coordinator_config = PortfolioCoordinatorConfig.from_dict() if mode == "algo" else None
    scan_errors: list[str] = []
    entry_pause_reasons: list[str] = []
    if mode == "algo" and not selected_symbols:
        entry_pause_reasons.append("the Algo universe is empty")
    if mode == "algo" and session_name == "overnight" and not bool(
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
    if mode == "algo" and unowned_positions:
        entry_pause_reasons.append("broker positions have no verified strategy owner")

    held_symbol_set = {str(symbol).upper() for symbol in owner_map} if track_owners else set()
    if strategy_states is not None:
        for by_symbol in strategy_states.values():
            for symbol, state in by_symbol.items():
                if state.get("phase") in ACTIVE_PHASES:
                    held_symbol_set.add(str(symbol).upper())
    held_symbols = sorted(held_symbol_set if mode == "algo" else held_symbol_set - excluded)
    strategy_symbols = list(dict.fromkeys(
        [symbol for symbol in selected_symbols if symbol not in excluded] + held_symbols
    ))
    # SPY is part of the fixed Algo universe and remains available to macro
    # event signals; ordinary strategies still use their configured symbols.
    if mode == "algo" and event_satellite_enabled and "SPY" not in strategy_symbols:
        strategy_symbols.append("SPY")
    symbols_for_bars = list(dict.fromkeys(strategy_symbols + ["SPY"]))

    now_utc = datetime.now(timezone.utc)
    factor_config_values = dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor") or {})
    factor_config_values.update(multi_factor_parameters)
    factor_config = MultiFactorConfig.from_dict(factor_config_values)
    daily_history_days = factor_config.warmup_calendar_days() if mode == "algo" else 220
    daily_start = (now_utc - timedelta(days=daily_history_days)).date().isoformat()
    daily_feed = regular_feed() if mode == "algo" else data_feed
    historical_status = "ok"
    historical_bar_end: str | None = None
    try:
        daily_bars = _fetch_bars(
            symbols_for_bars,
            mode=mode,
            timeframe="1Day",
            start=daily_start,
            end=now_utc.isoformat(),
            limit=10000,
            feed=daily_feed,
            adjustment="split" if mode == "algo" else None,
        )
        daily_bars = {ticker: _completed_daily(bars, today) for ticker, bars in daily_bars.items()}
    except Exception as exc:
        if mode != "algo":
            raise
        daily_bars = {}
        historical_status = "degraded"
        scan_errors.append(f"daily historical bars: {type(exc).__name__}: {exc}")
        entry_pause_reasons.append("daily historical bars are unavailable")

    if mode in {"day", "algo"}:
        intraday_bars = {}
        if mode == "algo" and session_name:
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
            if mode != "algo":
                raise
            bars_by_symbol = {}
            historical_status = "degraded"
            scan_errors.append(f"intraday bar validation: {type(exc).__name__}: {exc}")
            entry_pause_reasons.append("intraday historical bars are invalid")
        if mode == "algo" and strategy_symbols and not any(bars_by_symbol.values()) and historical_status == "ok":
            historical_status = "warming_up"
            entry_pause_reasons.append("no completed intraday historical bars are available yet")
    else:
        bars_by_symbol = daily_bars

    delay_seconds = historical_delay_seconds(data_feed, session_name or "") if mode == "algo" else 0
    signal_bar_max_age_seconds = (
        ALGO_SIGNAL_DATA_CONFIG["delayed_signal_bar_max_age_minutes"] if delay_seconds
        else ALGO_SIGNAL_DATA_CONFIG["signal_bar_max_age_minutes"]
    ) * 60
    fresh_signal_bar_symbols = set()
    if mode == "algo":
        fresh_signal_bar_symbols = {
            ticker for ticker, bars in bars_by_symbol.items()
            if (age := _latest_bar_age_seconds(bars, now)) is not None
            and 0 <= age <= signal_bar_max_age_seconds
        }
        if selected_symbols and not fresh_signal_bar_symbols.intersection(selected_symbols):
            entry_pause_reasons.append("the Algo universe has no recent completed signal bars")
            if historical_status == "ok":
                historical_status = "stale"

    spy_last_completed_date: str | None = None
    spy_daily_age_days: int | None = None
    if mode == "algo":
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

    default_calendar = Path(__file__).resolve().parents[4] / "data" / "event_calendar.csv"
    calendar_path = Path(os.environ.get("ALGO_EVENT_CALENDAR", str(default_calendar)))
    event_context = load_event_context(
        enabled=mode == "algo" and event_satellite_enabled,
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
    try:
        if mode == "algo":
            regime = rank_strategies(
                daily_bars.get("SPY", []), mode=mode, event_enabled=event_strategy_available,
                event_disable_in_volatile=event_config.disable_in_volatile,
                intraday_trend_enabled=ALGO_RUNTIME_SETTINGS.enable_intraday_trend,
                intraday_momentum_enabled=ALGO_RUNTIME_SETTINGS.enable_intraday_momentum,
            )
        else:
            regime = rank_strategies(daily_bars.get("SPY", []), mode=mode)
    except Exception as exc:
        if mode != "algo":
            raise
        regime = {"regime": "unknown", "reason": "SPY regime calculation failed", "ranked": [], "active": []}
        scan_errors.append(f"SPY regime calculation: {type(exc).__name__}: {exc}")
        entry_pause_reasons.append("SPY regime calculation failed")
    if mode == "algo":
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
    allowed_satellites = set(regime.get("satellites", []))
    equity = float(account.get("equity", 0) or 0)
    quote_errors: list[str] = []
    latest_quotes = _fetch_latest_quotes(
        strategy_symbols, mode=mode, feed=quote_feed,
        errors=quote_errors if mode == "algo" else None,
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
    quote_status = (
        "idle" if not strategy_symbols else
        "degraded" if quote_errors or owned_positions_without_fresh_quotes else
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
                shadow_positions=(multi_factor_shadow or {}).get("positions", {})
                if multi_factor_execution_mode == "shadow" else None,
                parameters=multi_factor_parameters,
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
            momentum_parameters = dict(ALGO_DEFAULT_PARAMETERS["momentum"])
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
    generated = []
    shadow_generated = []
    shadow_candidate_keys = set()
    satellite_confirmation_counts = {"multi_factor": 0, "event_driven": 0}

    for ticker in strategy_symbols:
        position = positions.get(ticker, {})
        position_qty = float(position.get("qty", 0) or 0)
        symbol_bars = bars_by_symbol.get(ticker, [])
        options = []
        owner = owner_map.get(ticker)
        force_intraday_flatten = (
            mode == "algo"
            and owner in INTRADAY_STRATEGIES
            and position_qty != 0
            and not coordinator_config.allow_intraday_overnight
            and (
                session_name not in {None, "regular"}
                or now >= (
                    regular_close or now.replace(hour=16, minute=0, second=0, microsecond=0)
                ) - timedelta(minutes=coordinator_config.intraday_close_cutoff_minutes)
            )
        )
        if force_intraday_flatten:
            generated.append({
                "ticker": ticker,
                "action": "cover" if position_qty < 0 else "sell",
                "strategy": owner,
                "reason": "portfolio coordinator mandatory intraday session flatten",
                "strength": 1.0,
                "rank_score": 1_000_000.0,
                "signal_bar_time": symbol_bars[-1].get("t") if symbol_bars else None,
                "timeframe": "5Min",
                "software_protection": True,
                "coordinator_force_flatten": True,
            })
            continue
        if track_owners and position_qty != 0 and owner is None:
            # On the continuous shared account, don't let a newly active
            # strategy take over a holding whose entry strategy is unknown.
            continue
        candidates = [owner] if owner and owner not in SATELLITE_STRATEGIES else [
            name for name in regime.get("active", [])
            if name in CORE_STRATEGIES
            or name in {"intraday_trend", "intraday_momentum"}
        ]
        factor_target = factor_confirmation_signals.get(ticker)
        if (
            mode == "algo" and owner is None and position_qty == 0
            and satellite_config.multi_factor_enabled
            and "multi_factor" in allowed_satellites
            and factor_target is not None
        ):
            candidates.append("multi_factor")
        # Existing satellite-owned holdings remain exit-manageable. A flat
        # ticker never receives a satellite entry candidate.
        if owner in SATELLITE_STRATEGIES and position_qty != 0:
            candidates = [owner]
        elif owner in SATELLITE_STRATEGIES:
            candidates = []
        event_signal = None
        if (
            mode == "algo" and event_generator is not None
            and (event_satellite_enabled or owner == "event_driven")
            and session_name in {None, "regular"}
            and ("event_driven" in candidates or "event_driven" in allowed_satellites)
        ):
            try:
                event_state = strategy_states.setdefault("event_driven", {})
                event_signal = event_generator.generate(
                    ticker, daily_bars.get(ticker, []), as_of=now_utc,
                    universe=set(selected_symbols) | ({"SPY"} if ticker == "SPY" else set()),
                    regime=str(regime.get("regime", "unknown")),
                    strategy_state=event_state,
                    position_qty=position_qty if owner == "event_driven" else 0,
                    entry_at=event_state.get(ticker, {}).get("opened_at") if owner == "event_driven" else None,
                    entry_price=(position.get("avg_entry_price") if owner == "event_driven" else None),
                )
            except (KeyError, TypeError, ValueError) as exc:
                scan_errors.append(f"daily event strategy signal {ticker}: {type(exc).__name__}: {exc}")
        protective_state = (strategy_states or {}).get(owner, {}).get(ticker, {}) if owner else {}
        quote = latest_quotes.get(ticker)
        if owner and position_qty != 0 and quote:
            quote_age = (datetime.now(timezone.utc) - quote["timestamp"]).total_seconds()
            # Evaluate protection on the closing side: bid for longs, ask for shorts.
            current_price = float(
                quote.get("ask" if position_qty < 0 else "bid", quote["price"])
            )
            stop_price = _positive_price_level(protective_state.get("stop_price"))
            if 0 <= quote_age <= int(STRATEGY_CONFIG["quote_stale_seconds"]):
                stop_price = _update_trailing_stop(
                    protective_state,
                    strategy=owner,
                    qty=position_qty,
                    entry_price=position.get("avg_entry_price"),
                    current_price=current_price,
                    mode_risk=risk_config,
                )
            target_price = _positive_price_level(protective_state.get("take_profit"))
            protective_reason = None
            if 0 <= quote_age <= int(STRATEGY_CONFIG["quote_stale_seconds"]):
                if position_qty > 0:
                    if stop_price is not None and current_price <= stop_price:
                        protective_reason = "software stop-loss reached"
                    elif target_price is not None and current_price >= target_price:
                        protective_reason = "software take-profit reached"
                else:
                    if stop_price is not None and current_price >= stop_price:
                        protective_reason = "software short stop-loss reached"
                    elif target_price is not None and current_price <= target_price:
                        protective_reason = "software short take-profit reached"
            if protective_reason:
                signal_bar_time = symbol_bars[-1].get("t") if symbol_bars else None
                if lifecycle is None or lifecycle.allows_protective_exit(owner, ticker):
                    options.append({
                        "ticker": ticker,
                        "action": "cover" if position_qty < 0 else "sell",
                        "strategy": owner,
                        "reason": protective_reason,
                        "strength": 1.0,
                        "rank_score": rank_score.get(owner, 0),
                        "signal_bar_time": signal_bar_time,
                        "software_protection": True,
                    })
        for strategy_name in candidates:
            if strategy_name == "event_driven" and (mode != "algo" or session_name not in {None, "regular"}):
                continue
            signal_bar_time = symbol_bars[-1].get("t") if symbol_bars else None
            strategy_spec = STRATEGY_CATALOG.get(strategy_name)
            if strategy_spec is not None and strategy_spec.bar_interval == "1Day":
                daily_signal_bars = daily_bars.get(ticker, [])
                signal_bar_time = (
                    daily_signal_bars[-1].get("t", daily_signal_bars[-1].get("date"))
                    if daily_signal_bars else None
                )
            if strategy_name == "multi_factor":
                signal = multi_factor_signals.get(ticker)
                if signal is None and owner is None and position_qty == 0:
                    signal = factor_confirmation_signals.get(ticker)
                signal_action = signal.get("action") if isinstance(signal, dict) else getattr(signal, "action", None)
                if signal is None or signal_action not in {"buy", "short", "sell", "cover"}:
                    continue
            elif strategy_name == "event_driven":
                if event_signal is None:
                    continue
                signal = event_signal
            elif strategy_name == "momentum":
                signal = momentum_signals.get(ticker)
            else:
                signal = None
            if lifecycle:
                lifecycle.observe(strategy_name, ticker, signal_bar_time)
            if signal is None and (strategy_name in RULE_MODULES or strategy_name in ALGO_RULE_MODULES):
                try:
                    if mode == "algo":
                        daily_strategy = bool(
                            strategy_spec and strategy_spec.bar_interval == "1Day"
                        )
                        if strategy_name == "momentum":
                            continue
                        if (
                            strategy_spec is not None
                            and strategy_spec.runtime_setting
                            and not getattr(ALGO_RUNTIME_SETTINGS, strategy_spec.runtime_setting)
                        ):
                            continue
                        signal_bars = daily_bars.get(ticker, []) if daily_strategy else symbol_bars
                        protective_state = (strategy_states or {}).get(strategy_name, {}).get(ticker, {})
                        signal = evaluate_rule_signal(
                            strategy_name, ticker, signal_bars,
                            parameters=ALGO_DEFAULT_PARAMETERS.get(strategy_name),
                            position_qty=position_qty,
                            entry_at=protective_state.get("opened_at"),
                            entry_price=(position.get("avg_entry_price") if position else None),
                            position_target=protective_state.get("take_profit"),
                        )
                    else:
                        signal = evaluate_strategy(
                            strategy_name, mode, ticker, symbol_bars, position_qty,
                        )
                except Exception as exc:
                    if mode != "algo":
                        raise
                    scan_errors.append(f"{strategy_name} signal {ticker}: {type(exc).__name__}: {exc}")
                    entry_pause_reasons.append("one or more Algo signal rules failed")
                    continue
            signal_action = signal.get("action") if isinstance(signal, dict) else getattr(signal, "action", None)
            expected_exit = "cover" if position_qty < 0 else "sell"
            if signal is None or (owner and signal_action != expected_exit):
                continue
            item = dict(signal) if isinstance(signal, dict) else signal.to_dict()
            item["rank_score"] = (
                -float(item.get("rank") or 1) if strategy_name == "multi_factor"
                else -1.0 if strategy_name == "event_driven"
                else rank_score.get(strategy_name, 0)
            )
            item["signal_bar_time"] = signal_bar_time
            item["timeframe"] = strategy_spec.bar_interval if strategy_spec else "5Min"
            if lifecycle and not lifecycle.allows(
                strategy_name, ticker, signal_action, item["signal_bar_time"],
            ):
                continue
            if (
                mode == "algo" and multi_factor_execution_mode == "shadow"
                and strategy_name == "multi_factor" and owner is None
            ):
                item["execution_mode"] = "shadow"
                item["shadow_only"] = True
                ticker_orders = pending_by_symbol.get(ticker, [])
                if item["action"] in {"buy", "short"} and ticker not in fresh_signal_bar_symbols:
                    item["suppressed_reason"] = "latest completed signal bar is stale or unavailable"
                elif item["action"] in {"buy", "short"} and ticker_orders:
                    item["suppressed_reason"] = "a conflicting Alpaca order is already open for this symbol"
                elif item["action"] == "buy" and regime.get("regime") == "trending_down":
                    item["suppressed_reason"] = "long entries are disabled in the downtrend regime"
                elif item["action"] == "short" and str(regime.get("regime")) not in {"range_bound", "trending_down"}:
                    item["suppressed_reason"] = "short entries require a neutral or bearish deterministic regime"
                elif item["action"] == "short" and ticker not in set(universe.get("easy_to_borrow_tickers") or []):
                    item["suppressed_reason"] = "Alpaca has not confirmed this ticker as easy to borrow"
                shadow_generated.append(item)
                shadow_candidate_keys.add((ticker, str(item.get("action"))))
                generated.append(item)
                continue
            options.append(item)

        factor_confirmation = None
        if "multi_factor" in allowed_satellites:
            factor_confirmation = factor_confirmation_signals.get(ticker)

        event_confirmation = event_signal.to_dict() if event_signal is not None else None

        if mode == "algo" and options:
            options = [
                apply_satellite_confirmations(
                    item,
                    factor_signal=factor_confirmation,
                    event_signal=(event_confirmation if "event_driven" in allowed_satellites else None),
                    config=satellite_config,
                )
                for item in options
            ]
        if not options:
            continue
        # One action per ticker per scan prevents conflicting strategy orders.
        if mode == "algo" and len(options) > 1:
            chosen, lower_priority = choose_priority(options)
            generated.extend(lower_priority)
        else:
            chosen = max(
                options,
                key=lambda item: (bool(item.get("software_protection")), item["rank_score"], item["strength"]),
            )
        ticker_orders = pending_by_symbol.get(ticker, [])
        pending_entry = position_qty == 0 and any(
            str(order.get("side", "")).lower() in {"buy", "sell"}
            for order in ticker_orders
        )
        protective_exit_order = any(
            _is_protective_sell_order(order, position_qty) for order in ticker_orders
        )
        pending_exit = any(
            str(order.get("side", "")).lower() == ("buy" if position_qty < 0 else "sell")
            and not _is_protective_sell_order(order, position_qty)
            for order in ticker_orders
        )
        if (
            mode == "algo" and chosen["action"] in {"sell", "cover"}
            and protective_exit_order and not chosen.get("coordinator_force_flatten")
            and not (chosen.get("software_protection") and session_name == "regular")
        ):
            # A broker bracket/OCO already owns the exit quantity. Alpaca may
            # cancel its stop and profit legs at different times; replacing a
            # rule exit by cancelling both can leave the holding without a
            # stop even during regular hours. Let the broker exit stand.
            chosen["suppressed_reason"] = (
                "broker protective order remains active; wait for its stop or target"
            )
            generated.append(chosen)
            continue
        if (
            (chosen["action"] in {"buy", "short"} and ticker_orders)
            or pending_entry
            or (pending_exit and not chosen.get("coordinator_force_flatten"))
        ):
            chosen["suppressed_reason"] = "a conflicting Alpaca order is already open for this symbol"
            generated.append(chosen)
            continue
        if regime.get("regime") == "trending_down" and chosen["action"] == "buy":
            chosen["suppressed_reason"] = "long entries are disabled in the downtrend regime"
            generated.append(chosen)
            continue
        if chosen["action"] == "short":
            regime_name = str(regime.get("regime", "unknown"))
            if risk_config.get("short_disabled_in_bull_regime", True) and regime_name == "trending_up":
                chosen["suppressed_reason"] = "short entries are disabled in the strong bull regime"
                generated.append(chosen)
                continue
            if regime_name not in {"range_bound", "trending_down"}:
                chosen["suppressed_reason"] = "short entries require a neutral or bearish deterministic regime"
                generated.append(chosen)
                continue
            if session_name not in {None, "regular"}:
                chosen["suppressed_reason"] = "short entries are restricted to regular market hours"
                generated.append(chosen)
                continue
            if ticker not in set(universe.get("easy_to_borrow_tickers") or []):
                chosen["suppressed_reason"] = "Alpaca has not confirmed this ticker as easy to borrow"
                generated.append(chosen)
                continue
            if regime_name == "range_bound":
                chosen["risk_multiplier"] = float(risk_config.get("range_regime_short_risk_multiplier", 0.75))
        if mode == "algo" and not chosen.get("software_protection") and ticker not in fresh_signal_bar_symbols:
            chosen["suppressed_reason"] = "latest completed signal bar is stale or unavailable"
            generated.append(chosen)
            continue
        generated.append(chosen)

    if mode == "algo" and multi_factor_execution_mode == "shadow":
        virtual_positions = (multi_factor_shadow or {}).get("positions", {})
        for ticker, signal in multi_factor_signals.items():
            signal_action = signal.get("action") if isinstance(signal, dict) else getattr(signal, "action", None)
            key = (str(ticker).upper(), str(signal_action))
            if ticker not in virtual_positions or signal_action not in {"sell", "cover"} or key in shadow_candidate_keys:
                continue
            item = dict(signal) if isinstance(signal, dict) else signal.to_dict()
            item.update({
                "ticker": str(ticker).upper(), "strategy": "multi_factor",
                "rank_score": -float(item.get("rank") or 1),
                "signal_bar_time": (daily_bars.get(ticker) or [{}])[-1].get("t"),
                "timeframe": "1Day", "execution_mode": "shadow", "shadow_only": True,
            })
            shadow_generated.append(item)
            shadow_candidate_keys.add(key)
            generated.append(item)

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
    group_open_counts = {
        name: sum(owner in set(group.get("strategies", [])) for owner in owner_map.values())
        for name, group in group_limits.items()
    }
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
    shadow_trades = []
    shadow_summary = None
    shadow_coordinator_book = None
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
        coordinator_book = coordinator.new_book(
            equity=equity,
            positions=positions,
            position_owners=owner_map,
            pending_buy_values=pending_buys,
            pending_short_values=pending_shorts,
            entries_today=entries_today,
            intraday_entries_today=intraday_entries_today,
            swing_entries_today=group_entry_counts.get("swing", 0),
        )
        if multi_factor_execution_mode == "shadow":
            shadow_positions_for_risk = dict(positions)
            shadow_owners_for_risk = dict(owner_map)
            for ticker, shadow_position in (multi_factor_shadow or {}).get("positions", {}).items():
                symbol = str(ticker).upper()
                if symbol not in shadow_owners_for_risk and not float((shadow_positions_for_risk.get(symbol) or {}).get("qty", 0) or 0):
                    shadow_positions_for_risk[symbol] = {
                        "qty": shadow_position.get("qty", 0),
                        "avg_entry_price": shadow_position.get("entry_price"),
                    }
                    shadow_owners_for_risk[symbol] = "multi_factor"
            shadow_coordinator_book = coordinator.new_book(
                equity=equity,
                positions=shadow_positions_for_risk,
                position_owners=shadow_owners_for_risk,
                pending_buy_values=pending_buys,
                pending_short_values=pending_shorts,
                entries_today=0,
                intraday_entries_today=0,
                swing_entries_today=0,
            )

    # Resolve shared limits in strategy-priority order so lower-priority
    # candidates cannot consume the daily budget before the core swing book.
    ordered_generated = [item for item in generated if not item.get("shadow_only")]
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
                if group_open_counts.get(group_name, 0) >= int(group.get("max_open_positions", 10**9)):
                    item["suppressed_reason"] = f"{group_name} open-position budget reached"
                    group_blocked = True
                elif group_entry_counts.get(group_name, 0) >= int(group.get("max_entries_per_day", 10**9)):
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
            identity = f"{mode}:{today}:{item['strategy']}:{item['ticker']}:{item['action']}:{signal_kind}:{item.get('signal_bar_time') or ''}"
            trade["client_order_id"] = f"rule-{today.replace('-', '')}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
            trade["signal_bar_time"] = item.get("signal_bar_time")
            if mode == "algo":
                trade["market_regime"] = str(regime.get("regime", "unknown"))
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
                        group_open_counts[group_name] = group_open_counts.get(group_name, 0) + 1
            for satellite in item.get("satellite_confirmations", []):
                if satellite in satellite_confirmation_counts:
                    satellite_confirmation_counts[satellite] += 1
            item["entry_price"] = price
            item["qty"] = trade["qty"]
        else:
            item["suppressed_reason"] = "position or strategy sizing produced zero shares"

    if mode == "algo" and multi_factor_execution_mode == "shadow" and shadow_coordinator_book is not None:
        initial_capital = ALGO_RUNTIME_SETTINGS.algo_multi_factor_shadow_capital
        shadow_trades = []
        multi_factor_shadow, shadow_trades, shadow_summary = process_multi_factor_shadow_orders(
            multi_factor_shadow, shadow_generated,
            quotes=latest_quotes, real_positions=positions,
            today=today, timestamp=now, initial_capital=initial_capital,
            max_entries_per_day=int(risk_config.get("max_entries_per_strategy_per_day", {}).get("multi_factor", 2)),
            max_positions=int(risk_config.get("max_positions_per_strategy_by_strategy", {}).get("multi_factor", 4)),
            slippage_bps=ALGO_RUNTIME_SETTINGS.algo_multi_factor_shadow_slippage_bps,
            commission_per_share=ALGO_RUNTIME_SETTINGS.algo_multi_factor_shadow_commission_per_share,
            quote_stale_seconds=freshness_limit,
            cooldown_minutes=int(risk_config.get("cooldown_minutes_by_strategy", {}).get("multi_factor", 0)),
            make_trade=_make_trade, risk=risk_config, universe=mode_config.get("universe", {}),
            coordinator_book=shadow_coordinator_book,
            session=session_name or "regular", regime=str(regime.get("regime", "unknown")),
            session_open=regular_open, session_close=regular_close,
        )

    return {
        "timestamp": now.isoformat(),
        "trading_mode": mode,
        "universe": universe,
        "market_regime": regime,
        "multi_factor_diagnostics": multi_factor_diagnostics,
        "multi_factor_parameter_profile": multi_factor_parameter_source,
        "multi_factor_execution_mode": multi_factor_execution_mode,
        "multi_factor_shadow_summary": shadow_summary,
        "multi_factor_shadow_trades": shadow_trades,
        "satellite_confirmation_counts": satellite_confirmation_counts,
        "event_strategy_diagnostics": (
            {
                **event_generator.diagnostics(datetime.now(timezone.utc)),
                "calendar_status": event_calendar_status,
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
            "fresh_quote_count": len(fresh_quote_symbols),
            "requested_quote_count": len(strategy_symbols),
            "owned_positions_without_fresh_quotes": owned_positions_without_fresh_quotes,
            "owned_positions_without_software_stops": owned_positions_without_software_stops,
            "historical_delay_seconds": delay_seconds,
            "fresh_signal_bar_count": len(fresh_signal_bar_symbols),
            "signal_bar_max_age_seconds": signal_bar_max_age_seconds if mode == "algo" else None,
            "spy_last_completed_date": spy_last_completed_date,
            "spy_daily_age_days": spy_daily_age_days,
        },
    }
