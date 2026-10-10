"""Point-in-time event records shared by live scans and research replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


EARNINGS_TYPES = {"earnings"}
MACRO_TYPES = {"cpi", "fomc", "nfp", "macro"}


def parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed


@dataclass(frozen=True)
class ScheduledEvent:
    event_id: str
    event_type: str
    ticker: str
    scheduled_at: datetime
    known_at: datetime
    reaction_start_at: datetime
    reaction_end_at: datetime
    source: str

    def __post_init__(self) -> None:
        event_id = self.event_id.strip()
        event_type = self.event_type.strip().lower()
        ticker = self.ticker.strip().upper()
        source = self.source.strip()
        if not event_id or not source:
            raise ValueError("event_id and source are required")
        if event_type not in EARNINGS_TYPES | MACRO_TYPES:
            raise ValueError(f"Unsupported event_type: {event_type}")
        if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
            raise ValueError("ticker must be a valid symbol; use SPY for macro events")
        if event_type in MACRO_TYPES and ticker != "SPY":
            raise ValueError("macro event rows must target SPY")
        for field_name in ("scheduled_at", "known_at", "reaction_start_at", "reaction_end_at"):
            value = getattr(self, field_name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.known_at > self.scheduled_at:
            raise ValueError("known_at cannot be later than scheduled_at")
        if self.reaction_start_at < self.scheduled_at:
            raise ValueError("reaction window cannot start before the scheduled event")
        if self.reaction_end_at <= self.reaction_start_at:
            raise ValueError("reaction_end_at must be later than reaction_start_at")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "source", source)

    @property
    def is_earnings(self) -> bool:
        return self.event_type in EARNINGS_TYPES

    def to_dict(self) -> dict:
        row = asdict(self)
        for key in ("scheduled_at", "known_at", "reaction_start_at", "reaction_end_at"):
            row[key] = row[key].isoformat()
        return row
