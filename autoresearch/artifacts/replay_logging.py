"""Logging helpers for high-volume historical research replays."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Iterator


class _CoordinatorRejectionFilter(logging.Filter):
    """Drop repetitive coordinator decisions while a research replay is active."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "strategy_runner"
            and record.levelno >= logging.WARNING
            and record.getMessage().startswith("Portfolio coordinator rejected ")
        )


@contextmanager
def quiet_coordinator_rejections() -> Iterator[None]:
    """Suppress per-signal coordinator warnings only for this historical replay."""
    logger = logging.getLogger("strategy_runner")
    rejection_filter = _CoordinatorRejectionFilter()
    logger.addFilter(rejection_filter)
    try:
        yield
    finally:
        logger.removeFilter(rejection_filter)
