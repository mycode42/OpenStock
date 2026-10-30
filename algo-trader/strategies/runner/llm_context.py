"""Prepare the scheduled LLM signal context for one strategy scan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from strategies.llm_driven import EventCalendar, LLMSignalGenerator
from strategies.llm_driven.ai import DEFAULT_AI_SIGNAL_DB, LLMSignalBook


@dataclass(frozen=True)
class LLMContext:
    generator: Any | None
    diagnostics: dict
    available: bool
    signal_status: str
    errors: tuple[str, ...]


def load_llm_context(
    *,
    enabled: bool,
    owned_llm_position: bool,
    llm_config,
    checked_at: Callable[[], datetime],
    signal_db: Path = DEFAULT_AI_SIGNAL_DB,
) -> LLMContext:
    """Load the scheduled AI snapshot; owners keep deterministic exits without it."""
    diagnostics: dict = {"signal_source": "disabled_by_config"}
    generator = None
    available = False
    status = "disabled_by_config"

    if enabled:
        book = LLMSignalBook(signal_db, now=checked_at())
        generator = LLMSignalGenerator(EventCalendar(), llm_config, ai_signal_db=str(signal_db))
        available = True
        status = book.status
        diagnostics = {
            "signal_source": "scheduled_ai",
            "ai_signal_book": book.diagnostics(),
        }

    if generator is None and owned_llm_position:
        # A disabled or unavailable snapshot must not disable exits for a
        # position that the portfolio already owns.
        generator = LLMSignalGenerator(EventCalendar(), llm_config)

    return LLMContext(
        generator=generator,
        diagnostics=diagnostics,
        available=available,
        signal_status=status,
        errors=(),
    )
