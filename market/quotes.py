"""Validation and normalization for Alpaca trade and quote timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Literal


class QuoteParseError(ValueError):
    """A broker quote could not be used safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def parse_timestamp(
    raw_timestamp: object,
    *,
    context: str = "Alpaca price",
    assume_utc: bool = True,
) -> datetime:
    """Return an aware UTC timestamp, optionally treating naive input as UTC."""
    try:
        timestamp = (
            raw_timestamp
            if isinstance(raw_timestamp, datetime)
            else datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError) as exc:
        raise QuoteParseError("invalid_timestamp", f"{context} timestamp is unavailable") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        if not assume_utc:
            raise QuoteParseError("invalid_timestamp", f"{context} timestamp has no timezone")
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def parse_price_timestamp(
    raw_price: object,
    raw_timestamp: object,
    *,
    context: str = "Alpaca price",
    assume_utc: bool = True,
) -> tuple[float, datetime]:
    """Return a finite positive price and aware UTC timestamp."""
    try:
        price = float(raw_price)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QuoteParseError("invalid_price", f"{context} price is unavailable") from exc
    if not math.isfinite(price) or price <= 0:
        raise QuoteParseError("invalid_price", f"{context} price is not finite and positive")
    return price, parse_timestamp(raw_timestamp, context=context, assume_utc=assume_utc)


def parse_quote(
    quote: object,
    *,
    side: Literal["ask", "bid"] | None = None,
    assume_utc: bool = True,
) -> dict:
    """Normalize an Alpaca quote, rejecting non-finite prices and crossed books.

    With ``side`` set, the requested side must be valid but the opposite side
    may be absent. When both sides are present, their ordering is still checked.
    Without ``side``, a complete two-sided quote is required.
    """
    if side not in {None, "ask", "bid"}:
        raise ValueError("quote side must be ask or bid")
    if not isinstance(quote, dict):
        raise QuoteParseError("invalid_quote", "Alpaca quote is not an object")

    timestamp = parse_timestamp(
        quote.get("t"), context="Alpaca quote", assume_utc=assume_utc,
    )

    parsed: dict[str, float | None] = {}
    for field, source in (("bid", "bp"), ("ask", "ap")):
        raw = quote.get(source)
        if raw is None or raw == "":
            parsed[field] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise QuoteParseError("invalid_bid_ask", f"Alpaca quote {field} is invalid") from exc
        if not math.isfinite(value):
            raise QuoteParseError("invalid_bid_ask", f"Alpaca quote {field} is not finite")
        parsed[field] = value if value > 0 else None

    bid, ask = parsed["bid"], parsed["ask"]
    if bid is not None and ask is not None and ask < bid:
        raise QuoteParseError("invalid_bid_ask", "Alpaca quote is crossed")
    if side is None:
        if bid is None or ask is None:
            raise QuoteParseError("invalid_bid_ask", "Alpaca quote requires positive bid and ask")
        price = (bid + ask) / 2
    else:
        price = parsed[side]
        if price is None:
            raise QuoteParseError("invalid_bid_ask", f"Alpaca quote {side} is unavailable")
    return {"price": price, "bid": bid, "ask": ask, "timestamp": timestamp}


__all__ = ["QuoteParseError", "parse_price_timestamp", "parse_quote", "parse_timestamp"]
