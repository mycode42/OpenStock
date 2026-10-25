"""Serialize the outputs of the Algo scan pipeline."""

from __future__ import annotations

from datetime import datetime, timezone

from .scan_types import ExecutionResult, MarketSnapshot, QuoteSnapshot, ScanScope, SignalSnapshot


def build_scan_response(
    scope: ScanScope,
    market: MarketSnapshot,
    quotes: QuoteSnapshot,
    signals: SignalSnapshot,
    execution: ExecutionResult,
) -> dict:
    """Build the stable scan response consumed by the service and dashboard."""
    if market.event_generator is not None:
        event_diagnostics = {
            **market.event_generator.diagnostics(datetime.now(timezone.utc)),
            "signal_status": market.event_calendar_status,
        }
    else:
        event_diagnostics = {
            **market.event_calendar_diagnostics,
            "enabled": False,
            "configured_enabled": scope.event_config.enabled,
            "status": market.event_calendar_status,
        }
    return {
        "timestamp": scope.now.isoformat(),
        "trading_mode": scope.mode,
        "universe": scope.universe,
        "market_regime": market.regime,
        "multi_factor_diagnostics": signals.multi_factor_diagnostics,
        "multi_factor_parameter_profile": scope.multi_factor_parameter_source,
        "multi_factor_execution_mode": "live",
        "event_strategy_diagnostics": event_diagnostics,
        "signals": signals.generated,
        "portfolio_coordinator": {
            "enabled": True,
            "options_delta_overlay": {
                "enabled": bool(execution.options_delta_overlay),
                "underlyings": execution.options_delta_overlay,
                "status": execution.options_overlay_status,
            },
            "max_name_pct": scope.coordinator_config.max_name_pct,
            "sector_caps": dict(scope.coordinator_config.sector_caps),
            "max_gross_pct": scope.coordinator_config.max_gross_pct,
            "max_net_long_pct": scope.coordinator_config.max_net_long_pct,
            "max_net_short_pct": scope.coordinator_config.max_net_short_pct,
            "max_daily_entries": scope.coordinator_config.max_daily_entries,
            "max_intraday_entries": scope.coordinator_config.max_intraday_entries,
            "allow_opposite_netting": scope.coordinator_config.allow_opposite_netting,
            "allow_intraday_overnight": scope.coordinator_config.allow_intraday_overnight,
            "rejections": [
                {
                    "ticker": item.get("ticker"),
                    "strategy": item.get("strategy"),
                    "action": item.get("action"),
                    "reason": item.get("suppressed_reason"),
                }
                for item in signals.generated
                if str(item.get("suppressed_reason", "")).startswith("portfolio coordinator:")
            ],
        },
        "trades": execution.trades,
        "portfolio_positions": sorted(scope.positions),
        "position_details": scope.positions,
        "pending_symbols": sorted(scope.pending_by_symbol),
        "session": scope.session_name,
        "extended_hours": scope.extended_hours,
        "open_orders": scope.open_orders,
        "entries_paused": execution.entries_paused,
        "entry_pause_reasons": list(dict.fromkeys(scope.entry_pause_reasons)),
        "scan_errors": scope.scan_errors,
        "unowned_positions": scope.unowned_positions,
        "data_status": {
            "historical_bars": market.historical_status,
            "bar_feed": scope.data_feed,
            "bar_end": market.historical_bar_end,
            "quote_feed": scope.quote_feed,
            "quotes": quotes.quote_status,
            "quote_request_status": "failed" if quotes.quote_errors else "succeeded",
            "session_limited_quotes": quotes.session_limited_quotes,
            "invalid_quote_symbols": quotes.invalid_quote_symbols,
            "fresh_quote_count": len(quotes.fresh_quote_symbols),
            "requested_quote_count": len(scope.strategy_symbols),
            "owned_positions_without_fresh_quotes": quotes.owned_positions_without_fresh_quotes,
            "owned_position_quote_diagnostics": quotes.owned_position_quote_diagnostics,
            "owned_positions_without_software_stops": quotes.owned_positions_without_software_stops,
            "historical_delay_seconds": market.delay_seconds,
            "fresh_signal_bar_count": len(market.fresh_signal_bar_symbols),
            "signal_bar_max_age_seconds": market.signal_bar_max_age_seconds,
            "overnight_swing_extended_age_signal_bar_count": len(
                market.overnight_swing_signal_bar_symbols
            ),
            "overnight_swing_signal_bar_max_age_seconds": (
                market.overnight_swing_signal_bar_max_age_seconds or None
            ),
            "spy_last_completed_date": market.spy_last_completed_date,
            "spy_daily_age_days": market.spy_daily_age_days,
        },
    }
