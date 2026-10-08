"""Event calendar to standard rule-signal adapter."""

from __future__ import annotations

from datetime import datetime, timedelta

from src.strategies.event_driven.calendar import EventCalendar
from src.strategies.event_driven.config import EventStrategyConfig
from src.strategies.event_driven.filters import EventFilter
from src.strategies.event_driven.models import ScheduledEvent
from src.strategies.event_driven.reactions import post_event_reaction
from src.strategies.signals import Signal


class EventSignalGenerator:
    def __init__(self, calendar: EventCalendar, config: EventStrategyConfig) -> None:
        self.calendar = calendar
        self.config = config
        self.event_filter = EventFilter(config)
        self.counts = {"active_event_windows": 0, "signals": 0}

    def generate(
        self,
        ticker: str,
        bars: list[dict],
        *,
        as_of: datetime,
        universe: set[str],
        regime: str,
        previous_close: float | None,
        strategy_state: dict,
    ) -> Signal | None:
        if not self.config.enabled:
            return None
        for event in self.calendar.active_for(ticker, as_of):
            self.counts["active_event_windows"] += 1
            if as_of > event.reaction_start_at + timedelta(
                minutes=self.config.reaction_window_minutes,
            ):
                self.event_filter._reject("configured_reaction_cutoff")
                continue
            if not self.event_filter.eligible(
                event, ticker=ticker, as_of=as_of, universe=universe,
                regime=regime, state=strategy_state,
            ):
                continue
            signal = post_event_reaction(
                event, ticker, bars, as_of=as_of, previous_close=previous_close,
                config=self.config,
            )
            if signal is not None:
                self.counts["signals"] += 1
                return signal
        return None

    def record_proposal(
        self, signal: Signal | dict, *, as_of: datetime, strategy_state: dict,
    ) -> None:
        event_id = signal.event_id if isinstance(signal, Signal) else signal.get("event_id")
        ticker = signal.ticker if isinstance(signal, Signal) else signal.get("ticker")
        event = next((item for item in self.calendar.events if item.event_id == event_id), None)
        if event is None or not isinstance(ticker, str):
            return
        self.event_filter.record(event, ticker, as_of, strategy_state)

    def diagnostics(self, as_of: datetime | None = None) -> dict:
        return {
            **self.calendar.diagnostics(as_of),
            "enabled": self.config.enabled,
            **self.counts,
            "filter_passed": self.event_filter.counts["passed"],
            "filter_rejected": dict(sorted(self.event_filter.counts["rejected"].items())),
        }
