"""Eligibility and sparse activity gates for scheduled event reactions."""

from __future__ import annotations

from datetime import datetime, timedelta

from strategies.event_driven.config import EventStrategyConfig
from strategies.event_driven.models import ScheduledEvent


class EventFilter:
    def __init__(self, config: EventStrategyConfig) -> None:
        self.config = config
        self.counts = {"considered": 0, "passed": 0, "rejected": {}}

    def _reject(self, reason: str) -> bool:
        rejected = self.counts["rejected"]
        rejected[reason] = rejected.get(reason, 0) + 1
        return False

    def eligible(
        self,
        event: ScheduledEvent,
        *,
        ticker: str,
        as_of: datetime,
        universe: set[str],
        regime: str,
        state: dict,
    ) -> bool:
        self.counts["considered"] += 1
        if not self.config.enabled:
            return self._reject("disabled")
        if ticker.upper() not in universe:
            return self._reject("outside_universe")
        if event.ticker != ticker.upper():
            return self._reject("symbol_mismatch")
        if event.known_at > as_of or event.scheduled_at > as_of:
            return self._reject("not_yet_known_or_occurred")
        configured_end = event.reaction_start_at + timedelta(days=self.config.reaction_window_days)
        if not event.reaction_start_at <= as_of <= configured_end:
            return self._reject("outside_reaction_window")
        if regime == "volatile" and self.config.disable_in_volatile:
            return self._reject("volatile_regime")

        portfolio = state.get("_PORTFOLIO", {})
        if event.event_id in portfolio.get("fired_event_ids", []):
            return self._reject("event_already_used")
        ticker_state = state.get(ticker.upper(), {})
        last_ticker = _timestamp(ticker_state.get("last_event_signal_at"))
        if last_ticker and as_of - last_ticker < timedelta(days=self.config.cooldown_days_per_ticker):
            return self._reject("ticker_cooldown")
        self.counts["passed"] += 1
        return True

    @staticmethod
    def record(event: ScheduledEvent, ticker: str, as_of: datetime, state: dict) -> None:
        # Keep event-id deduplication in the existing strategy state map.
        portfolio = state.setdefault("_PORTFOLIO", {})
        fired = list(portfolio.get("fired_event_ids", []))
        if event.event_id not in fired:
            fired.append(event.event_id)
        portfolio["fired_event_ids"] = fired[-256:]
        state.setdefault(ticker.upper(), {})["last_event_signal_at"] = as_of.isoformat()
        state[ticker.upper()]["last_event_id"] = event.event_id


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
