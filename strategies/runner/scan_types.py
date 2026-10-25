"""State passed between the Algo scan pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ScanScope:
    """Request, portfolio state, and shared diagnostics for one scan."""

    mode: str
    universe_result: dict | None
    excluded_symbols: set[str] | None
    position_owners: dict[str, str] | None
    strategy_states: dict | None
    session_name: str | None
    trading_date: str | None
    extended_hours: bool
    regular_open: datetime | None
    regular_close: datetime | None
    data_feed: str | None
    quote_feed: str | None
    overnight_tradable_tickers: set[str] | None
    runtime_settings: Any
    mode_config: dict
    now: datetime
    today: str
    multi_factor_parameters: dict
    multi_factor_parameter_source: Any
    universe: dict
    selected_symbols: list[str]
    event_config: Any
    event_strategy_enabled: bool
    track_owners: bool
    owner_map: dict[str, str]
    risk_config: dict
    coordinator_config: Any
    scan_errors: list[str] = field(default_factory=list)
    entry_pause_reasons: list[str] = field(default_factory=list)
    account: dict = field(default_factory=dict)
    positions: dict[str, dict] = field(default_factory=dict)
    open_orders: list[dict] = field(default_factory=list)
    pending_by_symbol: dict[str, list[dict]] = field(default_factory=dict)
    lifecycle: Any = None
    unowned_positions: list[str] = field(default_factory=list)
    strategy_symbols: list[str] = field(default_factory=list)
    regime_context_symbols: list[str] = field(default_factory=list)
    symbols_for_bars: list[str] = field(default_factory=list)
    now_utc: datetime | None = None
    equity: float = 0.0


@dataclass
class MarketSnapshot:
    """Bars, regime, and event-calendar inputs prepared for a scan."""

    daily_bars: dict[str, list[dict]]
    bars_by_symbol: dict[str, list[dict]]
    historical_status: str
    historical_bar_end: str | None
    delay_seconds: int
    signal_bar_max_age_seconds: int
    overnight_swing_signal_bar_max_age_seconds: int
    fresh_signal_bar_symbols: set[str]
    overnight_swing_signal_bar_symbols: set[str]
    spy_last_completed_date: str | None
    spy_daily_age_days: int | None
    event_generator: Any
    event_strategy_available: bool
    event_calendar_diagnostics: dict
    event_calendar_status: str
    regime: dict
    regime_name: str
    multi_factor_parameters: dict


@dataclass
class QuoteSnapshot:
    """Latest quote data and per-scan freshness/protection diagnostics."""

    latest_quotes: dict[str, dict]
    quote_errors: list[str]
    quote_fetch_diagnostics: dict[str, dict]
    freshness_limit: int
    quote_checked_at: datetime
    fresh_quote_symbols: set[str]
    owned_positions_without_fresh_quotes: list[str]
    owned_position_quote_diagnostics: list[dict]
    invalid_quote_symbols: list[str]
    owned_positions_without_software_stops: list[str]
    selected_symbols_with_fresh_quotes: set[str]
    session_limited_quotes: bool
    quote_status: str


@dataclass
class SignalSnapshot:
    """Strategy candidates and their diagnostics for one scan."""

    multi_factor_diagnostics: dict | None
    rank_score: dict[str, float]
    momentum_signals: dict[str, Any]
    generated: list[dict]


@dataclass
class ExecutionResult:
    """Orders approved by sizing and portfolio coordination."""

    trades: list[dict]
    entries_paused: bool
    options_delta_overlay: dict
    options_overlay_status: dict
