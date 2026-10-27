"""CSV calendar loader with explicit point-in-time visibility checks."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from strategies.llm_driven.models import ScheduledEvent, parse_timestamp


CSV_FIELDS = (
    "event_id", "event_type", "ticker", "scheduled_at", "known_at",
    "reaction_start_at", "reaction_end_at", "source",
)


class EventCalendar:
    """Immutable scheduled events; revisions are represented as new rows."""

    def __init__(self, events: list[ScheduledEvent] | tuple[ScheduledEvent, ...] = ()) -> None:
        ids = [event.event_id for event in events]
        if len(ids) != len(set(ids)):
            raise ValueError("event_id values must be unique within a calendar snapshot")
        self.events = tuple(sorted(events, key=lambda event: (event.scheduled_at, event.event_id)))

    @classmethod
    def from_csv(cls, path: str | Path) -> "EventCalendar":
        source_path = Path(path)
        if not source_path.is_file():
            raise FileNotFoundError(f"event calendar does not exist: {source_path}")
        events = []
        with source_path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            missing = set(CSV_FIELDS) - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"event calendar is missing columns: {', '.join(sorted(missing))}")
            for line_number, row in enumerate(reader, start=2):
                try:
                    events.append(ScheduledEvent(
                        event_id=row["event_id"], event_type=row["event_type"],
                        ticker=row["ticker"],
                        scheduled_at=parse_timestamp(row["scheduled_at"], "scheduled_at"),
                        known_at=parse_timestamp(row["known_at"], "known_at"),
                        reaction_start_at=parse_timestamp(row["reaction_start_at"], "reaction_start_at"),
                        reaction_end_at=parse_timestamp(row["reaction_end_at"], "reaction_end_at"),
                        source=row["source"],
                    ))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid event calendar row {line_number}: {exc}") from exc
        return cls(events)

    def known_by(self, as_of: datetime) -> tuple[ScheduledEvent, ...]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return tuple(event for event in self.events if event.known_at <= as_of)

    @property
    def refreshed_at(self) -> datetime | None:
        """Generated snapshots stamp every row with the refresh timestamp."""
        return max((event.known_at for event in self.events), default=None)

    def freshness(self, as_of: datetime, *, max_age: timedelta = timedelta(hours=96)) -> dict:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        stamp = self.refreshed_at
        if stamp is None:
            return {"fresh": False, "age_hours": None, "reason": "calendar is empty"}
        age = as_of - stamp
        if age < timedelta(minutes=-5):
            return {"fresh": False, "age_hours": round(age.total_seconds() / 3600, 2), "reason": "calendar is future-dated"}
        if age > max_age:
            return {"fresh": False, "age_hours": round(age.total_seconds() / 3600, 2), "reason": "calendar refresh is stale"}
        return {"fresh": True, "age_hours": round(max(0.0, age.total_seconds()) / 3600, 2), "reason": None}

    def active_for(self, ticker: str, as_of: datetime) -> tuple[ScheduledEvent, ...]:
        symbol = ticker.upper()
        return tuple(
            event for event in self.known_by(as_of)
            if event.ticker == symbol
            and event.scheduled_at <= as_of
            and event.reaction_start_at <= as_of <= event.reaction_end_at
        )

    def diagnostics(self, as_of: datetime | None = None) -> dict:
        visible = self.events if as_of is None else self.known_by(as_of)
        result = {
            "loaded_events": len(self.events),
            "known_events": len(visible),
            "earnings_events": sum(event.is_earnings for event in visible),
            "macro_events": sum(not event.is_earnings for event in visible),
            "refreshed_at": self.refreshed_at.isoformat() if self.refreshed_at else None,
        }
        if as_of is not None:
            result["freshness"] = self.freshness(as_of)
            macro = [event for event in visible if not event.is_earnings and event.scheduled_at >= as_of]
            result["macro_coverage_by_type"] = {
                event_type: max(
                    (event.scheduled_at.date().isoformat() for event in macro if event.event_type == event_type),
                    default=None,
                )
                for event_type in ("cpi", "nfp", "fomc")
            }
            result["macro_schedule_through"] = max(
                (event.scheduled_at.date().isoformat() for event in macro),
                default=None,
            )
            coverage_days = {"cpi": 45, "nfp": 45, "fomc": 75}
            result["macro_coverage_warnings"] = [
                f"no {event_type.upper()} date is published in the next {days} days"
                for event_type, days in coverage_days.items()
                if not any(
                    event.event_type == event_type
                    and event.scheduled_at <= as_of + timedelta(days=days)
                    for event in macro
                )
            ]
        return result
