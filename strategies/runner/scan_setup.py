"""Build the shared portfolio and request context for an Algo scan."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from market.data import feeds_for_session, regular_feed
from portfolio.config import (
    get_algo_portfolio_coordinator_config,
    get_algo_runtime_settings,
    get_mode_config,
)
from strategies.event_driven import EventStrategyConfig
from strategies.registry import ALGO_DEFAULT_PARAMETERS
from strategies.runner.factors import multi_factor_parameters_for_session
from strategies.portfolio_coordinator import PortfolioCoordinatorConfig
from strategies.state_machine import ACTIVE_PHASES, RuleStrategyStateMachine
from strategies.universe import configured_universe, load_algo_universe
from strategies.runner.data import current_portfolio

from .scan_types import ScanScope


ET = ZoneInfo("America/New_York")


def prepare_scan_scope(
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
    runtime_settings: Any = None,
) -> ScanScope:
    """Validate scan inputs and reconcile broker state before fetching data."""
    runtime_settings = runtime_settings or get_algo_runtime_settings()
    if mode != "algo":
        raise ValueError("Only the algo portfolio scan is supported")
    if position_owners is None or strategy_states is None:
        raise ValueError("Algo scans require the coordinator's position owners and strategy states")

    if session_name:
        default_bar_feed, default_quote_feed = feeds_for_session(session_name)
        data_feed = default_bar_feed if session_name == "overnight" else (data_feed or default_bar_feed)
        quote_feed = quote_feed or default_quote_feed
    else:
        data_feed = data_feed or regular_feed()
        quote_feed = quote_feed or data_feed

    now = datetime.now(ET)
    today = trading_date or now.date().isoformat()
    multi_factor_parameters, multi_factor_parameter_source = multi_factor_parameters_for_session()
    universe = universe_result if universe_result is not None else load_algo_universe()
    selected_symbols = [str(symbol).upper() for symbol in universe.get("tickers", [])]
    event_parameters = dict(ALGO_DEFAULT_PARAMETERS.get("swing_event_driven") or {})
    # Regime eligibility is controlled by the editable per-strategy route table.
    event_parameters["disable_in_volatile"] = False
    event_config = EventStrategyConfig.from_dict(event_parameters)
    event_strategy_enabled = (
        event_config.enabled and runtime_settings.swing_event_driven_enabled
    )
    owner_map = position_owners
    track_owners = True
    mode_config = get_mode_config(mode)
    risk_config = mode_config["risk"]
    coordinator_config = PortfolioCoordinatorConfig.from_dict(
        get_algo_portfolio_coordinator_config(),
    )
    scan_errors: list[str] = []
    entry_pause_reasons: list[str] = []
    if not selected_symbols:
        entry_pause_reasons.append("the Algo universe is empty")
    if session_name == "overnight" and not runtime_settings.overnight_entries_enabled:
        entry_pause_reasons.append("overnight entries are disabled by configuration")

    account, positions, open_orders = current_portfolio(mode)
    pending_by_symbol: dict[str, list[dict]] = {}
    for order in open_orders:
        ticker = str(order.get("symbol", "")).upper()
        if ticker:
            pending_by_symbol.setdefault(ticker, []).append(order)

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
    )
    if unowned_positions:
        entry_pause_reasons.append("broker positions have no verified strategy owner")

    held_symbol_set = {str(symbol).upper() for symbol in owner_map}
    for by_symbol in strategy_states.values():
        for symbol, state in by_symbol.items():
            if state.get("phase") in ACTIVE_PHASES:
                held_symbol_set.add(str(symbol).upper())
    held_symbols = sorted(held_symbol_set)
    excluded = {str(symbol).upper() for symbol in (excluded_symbols or set())}
    strategy_symbols = list(dict.fromkeys(
        [symbol for symbol in selected_symbols if symbol not in excluded] + held_symbols
    ))
    # SPY remains available to macro event signals outside the configured universe.
    if event_strategy_enabled and "SPY" not in strategy_symbols:
        strategy_symbols.append("SPY")
    regime_context_symbols = configured_universe("algo")["tickers"] + ["HYG", "LQD"]
    symbols_for_bars = list(dict.fromkeys(
        strategy_symbols + ["SPY"] + regime_context_symbols
    ))

    return ScanScope(
        mode=mode,
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
        mode_config=mode_config,
        now=now,
        today=today,
        multi_factor_parameters=multi_factor_parameters,
        multi_factor_parameter_source=multi_factor_parameter_source,
        universe=universe,
        selected_symbols=selected_symbols,
        event_config=event_config,
        event_strategy_enabled=event_strategy_enabled,
        track_owners=track_owners,
        owner_map=owner_map,
        risk_config=risk_config,
        coordinator_config=coordinator_config,
        scan_errors=scan_errors,
        entry_pause_reasons=entry_pause_reasons,
        account=account,
        positions=positions,
        open_orders=open_orders,
        pending_by_symbol=pending_by_symbol,
        lifecycle=lifecycle,
        unowned_positions=unowned_positions,
        strategy_symbols=strategy_symbols,
        regime_context_symbols=regime_context_symbols,
        symbols_for_bars=symbols_for_bars,
        now_utc=datetime.now(timezone.utc),
        equity=float(account.get("equity", 0) or 0),
    )
