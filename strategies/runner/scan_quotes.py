"""Quote loading and stale-data diagnostics for the Algo scan."""

from __future__ import annotations

from datetime import datetime, timezone

from portfolio.config import STRATEGY_CONFIG
from strategies.runner.data import fetch_latest_quotes
from strategies.runner.sizing import _positive_price_level

from .scan_types import MarketSnapshot, QuoteSnapshot, ScanScope


def load_quote_snapshot(scope: ScanScope, market: MarketSnapshot) -> QuoteSnapshot:
    """Fetch quotes and determine whether entries/protective exits have fresh data."""
    quote_errors: list[str] = []
    quote_fetch_diagnostics: dict[str, dict] = {}
    latest_quotes = fetch_latest_quotes(
        scope.strategy_symbols,
        mode=scope.mode,
        feed=scope.quote_feed,
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
        ticker for ticker, position in scope.positions.items()
        if ticker in scope.owner_map and float(position.get("qty", 0) or 0) != 0
        and ticker not in fresh_quote_symbols
    ) if scope.track_owners else []
    invalid_quote_symbols = sorted(
        ticker for ticker, diagnostic in quote_fetch_diagnostics.items()
        if str(diagnostic.get("status", "")).startswith("invalid_")
    )
    owned_position_quote_diagnostics = []
    for ticker, position in sorted(scope.positions.items()):
        if ticker not in scope.owner_map or float(position.get("qty", 0) or 0) == 0:
            continue
        quote = latest_quotes.get(ticker)
        fetch_diagnostic = quote_fetch_diagnostics.get(ticker) or {"status": "missing"}
        timestamp = quote.get("timestamp") if quote else None
        if timestamp is None and fetch_diagnostic.get("quote_timestamp"):
            try:
                timestamp = datetime.fromisoformat(
                    fetch_diagnostic["quote_timestamp"].replace("Z", "+00:00"),
                )
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
            "feed": scope.quote_feed,
            "status": status,
            "quote_availability": (
                "session_limited"
                if scope.session_name in {"premarket", "after_hours", "overnight"}
                and not quote_errors and status in {"missing", "stale"}
                else status
            ),
            "overnight_tradable": (
                ticker in scope.overnight_tradable_tickers
                if scope.session_name == "overnight" and scope.overnight_tradable_tickers is not None
                else None
            ),
            "quote_timestamp": timestamp.isoformat() if timestamp is not None else None,
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "freshness_limit_seconds": freshness_limit,
        })

    owned_positions_without_software_stops = sorted(
        ticker for ticker, position in scope.positions.items()
        if ticker in scope.owner_map and float(position.get("qty", 0) or 0) != 0
        and _positive_price_level(
            (scope.strategy_states or {}).get(scope.owner_map[ticker], {})
            .get(ticker, {}).get("stop_price")
        ) is None
    ) if scope.track_owners else []
    if quote_errors:
        scope.scan_errors.extend(quote_errors)
        scope.entry_pause_reasons.append("latest quote requests failed")
    if owned_positions_without_fresh_quotes:
        scope.entry_pause_reasons.append("owned positions lack fresh protective quotes")
    if owned_positions_without_software_stops:
        scope.entry_pause_reasons.append("owned positions lack valid software stop levels")
    if scope.selected_symbols and not fresh_quote_symbols.intersection(scope.selected_symbols):
        scope.entry_pause_reasons.append("the Algo universe has no fresh quotes")

    selected_symbols_with_fresh_quotes = fresh_quote_symbols.intersection(scope.selected_symbols)
    session_limited_quotes = bool(
        scope.session_name in {"premarket", "after_hours", "overnight"}
        and not quote_errors
        and not invalid_quote_symbols
        and (
            owned_positions_without_fresh_quotes
            or (scope.selected_symbols and not selected_symbols_with_fresh_quotes)
        )
    )
    quote_status = (
        "idle" if not scope.strategy_symbols else
        "degraded" if quote_errors or invalid_quote_symbols or (
            scope.session_name == "regular" and owned_positions_without_fresh_quotes
        ) else
        "session_limited" if session_limited_quotes else
        "unavailable" if not fresh_quote_symbols else "ok"
    )
    return QuoteSnapshot(
        latest_quotes=latest_quotes,
        quote_errors=quote_errors,
        quote_fetch_diagnostics=quote_fetch_diagnostics,
        freshness_limit=freshness_limit,
        quote_checked_at=quote_checked_at,
        fresh_quote_symbols=fresh_quote_symbols,
        owned_positions_without_fresh_quotes=owned_positions_without_fresh_quotes,
        owned_position_quote_diagnostics=owned_position_quote_diagnostics,
        invalid_quote_symbols=invalid_quote_symbols,
        owned_positions_without_software_stops=owned_positions_without_software_stops,
        selected_symbols_with_fresh_quotes=selected_symbols_with_fresh_quotes,
        session_limited_quotes=session_limited_quotes,
        quote_status=quote_status,
    )
