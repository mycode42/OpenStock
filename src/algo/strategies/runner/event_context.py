"""Prepare the point-in-time event context for one strategy scan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.algo.strategies.event_driven import EventCalendar, EventSignalGenerator
from src.algo.strategies.event_driven.ai import AIEventSignalBook, DEFAULT_AI_SIGNAL_FILE


@dataclass(frozen=True)
class EventContext:
    generator: Any | None
    diagnostics: dict
    available: bool
    calendar_status: str
    errors: tuple[str, ...]


def load_event_context(
    *,
    enabled: bool,
    owned_event_position: bool,
    event_config,
    calendar_path: Path,
    checked_at: Callable[[], datetime],
    calendar_type=EventCalendar,
    generator_type=EventSignalGenerator,
    ai_signal_path: Path | None = DEFAULT_AI_SIGNAL_FILE,
) -> EventContext:
    """Load scheduled AI signals, preserving legacy exits for existing owners."""
    diagnostics = calendar_type().diagnostics()
    generator = None
    available = False
    status = "disabled_by_config"
    errors: list[str] = []

    if enabled and ai_signal_path is not None:
        book = AIEventSignalBook(ai_signal_path, now=checked_at())
        generator = generator_type(calendar_type(), event_config, ai_signal_file=str(ai_signal_path))
        available = True
        status = book.status
        diagnostics = {
            "signal_source": "scheduled_ai",
            "ai_signal_book": book.diagnostics(),
        }
    elif enabled:
        try:
            calendar = calendar_type.from_csv(calendar_path)
            checked = checked_at()
            freshness = calendar.freshness(checked)
            diagnostics = calendar.diagnostics(checked)
            if freshness["fresh"] and calendar.events:
                generator = generator_type(calendar, event_config)
                available = True
                status = "ready"
            else:
                status = "stale" if calendar.events else "empty"
                reason = freshness["reason"] or "calendar is empty"
                diagnostics["unavailable_reason"] = reason
                errors.append(f"event strategy calendar {status}: {reason}: {calendar_path}")
        except (OSError, ValueError) as exc:
            status = "unavailable"
            errors.append(f"event strategy calendar unavailable: {type(exc).__name__}: {exc}")

    if generator is None and owned_event_position:
        # Calendar failure or an entry-disable flag must not disable exits for
        # a position that the portfolio already owns.
        generator = generator_type(calendar_type(), event_config)

    return EventContext(
        generator=generator,
        diagnostics=diagnostics,
        available=available,
        calendar_status=status,
        errors=tuple(errors),
    )
