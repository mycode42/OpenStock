"""Eligibility and sparse activity gates for scheduled event reactions."""

from __future__ import annotations

from datetime import datetime, timedelta

from src.strategies.event_driven.config import EventStrategyConfig
from src.strategies.event_driven.models import ScheduledEvent


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
        if not event.reaction_start_at <= as_of <= event.reaction_end_at:
            return self._reject("outside_reaction_window")
        if regime == "volatile" and self.config.disable_in_volatile:
            return self._reject("volatile_regime")

        portfolio = state.get("_PORTFOLIO", {})
        if event.event_id in portfolio.get("fired_event_ids", []):
            return self._reject("event_already_used")
        current_week = as_of.isocalendar()[:2]
        weekly_entries = [
            item for item in portfolio.get("signals", [])
            if _week_key(item.get("at")) == current_week
        ]
        if len(weekly_entries) >= self.config.max_entries_per_week:
            return self._reject("weekly_entry_cap")
        last_global = _timestamp(portfolio.get("last_signal_at"))
        if last_global and as_of - last_global < timedelta(hours=self.config.cooldown_hours_portfolio):
            return self._reject("portfolio_cooldown")
        ticker_state = state.get(ticker.upper(), {})
        last_ticker = _timestamp(ticker_state.get("last_event_signal_at"))
        if last_ticker and as_of - last_ticker < timedelta(days=self.config.cooldown_days_per_ticker):
            return self._reject("ticker_cooldown")
        self.counts["passed"] += 1
        return True

    @staticmethod
    def record(event: ScheduledEvent, ticker: str, as_of: datetime, state: dict) -> None:
        # A reserved uppercase pseudo-symbol keeps portfolio-wide throttles in
        # the existing per-strategy SQLite state map without a second store.
        portfolio = state.setdefault("_PORTFOLIO", {})
        signals = [item for item in portfolio.get("signals", []) if _timestamp(item.get("at"))]
        signals.append({"event_id": event.event_id, "ticker": ticker.upper(), "at": as_of.isoformat()})
        portfolio["signals"] = signals[-64:]
        portfolio["last_signal_at"] = as_of.isoformat()
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


def _week_key(value: object) -> tuple[int, int] | None:
    timestamp = _timestamp(value)
    return timestamp.isocalendar()[:2] if timestamp else None
