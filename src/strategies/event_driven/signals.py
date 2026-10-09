"""Calendar-driven event entries and daily exit management."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.strategies.event_driven.calendar import EventCalendar
from src.strategies.event_driven.config import EventStrategyConfig
from src.strategies.event_driven.filters import EventFilter
from src.strategies.event_driven.models import ScheduledEvent
from src.strategies.event_driven.reactions import EventSignal, daily_event_reaction
from src.strategies.swing_common import clean_daily_bars, held_sessions, position_entry_price
from src.strategies.signals import Signal


ET = ZoneInfo("America/New_York")


class EventSignalGenerator:
    def __init__(self, calendar: EventCalendar, config: EventStrategyConfig) -> None:
        self.calendar = calendar
        self.config = config
        self.event_filter = EventFilter(config)
        self.counts = {
            "active_event_windows": 0, "signals": 0,
            "signals_by_direction": {"long": 0, "short": 0},
            "signals_by_exit_mode": {"reaction": 0, "follow_through": 0},
        }

    def generate(
        self,
        ticker: str,
        bars: list[dict],
        *,
        as_of: datetime,
        universe: set[str],
        regime: str,
        previous_close: float | None = None,
        strategy_state: dict,
        position_qty: float = 0,
        entry_at: str | None = None,
        entry_price: float | None = None,
    ) -> Signal | None:
        """Bars are completed daily aggregates; no five-minute entry path exists."""
        if position_qty:
            return self._manage_position(
                ticker, bars, as_of=as_of, position_qty=position_qty,
                entry_at=entry_at, entry_price=entry_price,
                exit_mode=str(strategy_state.get(ticker.upper(), {}).get("exit_mode", "follow_through")),
            )
        if not self.config.enabled:
            return None
        symbol = ticker.upper()
        for event in self.calendar.known_by(as_of):
            reaction_end = event.reaction_start_at + timedelta(days=self.config.reaction_window_days)
            if (
                event.ticker != symbol
                or event.scheduled_at > as_of
                or not event.reaction_start_at <= as_of <= reaction_end
            ):
                continue
            self.counts["active_event_windows"] += 1
            if not self.event_filter.eligible(
                event, ticker=ticker, as_of=as_of, universe=universe,
                regime=regime, state=strategy_state,
            ):
                continue
            signal = daily_event_reaction(
                event, ticker, bars, as_of=as_of, config=self.config,
            )
            if signal is not None:
                self.counts["signals"] += 1
                direction = "short" if signal.action == "short" else "long"
                self.counts["signals_by_direction"][direction] += 1
                self.counts["signals_by_exit_mode"][signal.exit_mode] += 1
                return signal
        return None

    def _manage_position(
        self, ticker: str, bars: list[dict], *, as_of: datetime,
        position_qty: float, entry_at: str | None, entry_price: float | None,
        exit_mode: str,
    ) -> EventSignal | None:
        session = as_of.astimezone(ET).date()
        rows = [row for row in clean_daily_bars(bars) if row[0] < session]
        held = held_sessions(rows, entry_at)
        entry = position_entry_price(entry_price)
        if held is None or held <= 0 or entry is None:
            return None
        is_short = position_qty < 0
        current = rows[-1][2]
        maximum = 1 if exit_mode == "reaction" else self.config.follow_through_sessions
        adverse = current > entry if is_short else current < entry
        if exit_mode == "reaction" and held >= 1:
            reason = "one-session event reaction completed" if not adverse else "event reaction failed; exit after first completed session"
        elif adverse and held >= 1:
            reason = "event follow-through reversed after the catalyst"
        elif held >= maximum:
            reason = f"event follow-through reached its {maximum}-session time stop"
        else:
            return None
        return EventSignal(
            ticker=ticker.upper(), action="cover" if is_short else "sell",
            strategy="event_driven", reason=reason, strength=0.8,
            stop_pct=self.config.stop_pct, target_weight=self.config.risk_limited_weight,
            risk_multiplier=self.config.risk_multiplier,
            exit_mode=exit_mode, maximum_holding_sessions=maximum,
        )

    def record_proposal(
        self, signal: Signal | dict, *, as_of: datetime, strategy_state: dict,
    ) -> None:
        event_id = signal.event_id if isinstance(signal, Signal) else signal.get("event_id")
        ticker = signal.ticker if isinstance(signal, Signal) else signal.get("ticker")
        event = next((item for item in self.calendar.events if item.event_id == event_id), None)
        if event is None or not isinstance(ticker, str):
            return
        self.event_filter.record(event, ticker, as_of, strategy_state)
        metadata = signal.to_dict() if isinstance(signal, Signal) else signal
        state = strategy_state.setdefault(ticker.upper(), {})
        state["exit_mode"] = str(metadata.get("exit_mode", "follow_through"))
        state["event_maximum_holding_sessions"] = int(
            metadata.get("maximum_holding_sessions", self.config.follow_through_sessions)
        )

    def diagnostics(self, as_of: datetime | None = None) -> dict:
        return {
            **self.calendar.diagnostics(as_of),
            "enabled": self.config.enabled,
            "bar_horizon": "daily",
            **self.counts,
            "filter_passed": self.event_filter.counts["passed"],
            "filter_rejected": dict(sorted(self.event_filter.counts["rejected"].items())),
        }


__all__ = ["EventSignalGenerator"]
