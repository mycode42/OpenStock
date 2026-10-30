"""Scheduled AI recommendations and deterministic daily exit management."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from strategies.llm_driven.calendar import EventCalendar
from strategies.llm_driven.config import LLMStrategyConfig
from strategies.llm_driven.models import ScheduledEvent
from strategies.llm_driven.ai import LLMSignalBook, DEFAULT_AI_SIGNAL_DB
from strategies.swing_common import SwingSignal, clean_daily_bars, held_sessions, position_entry_price
from strategies.signals import Signal


ET = ZoneInfo("America/New_York")


class EventSignal(SwingSignal):
    """A risk-sized swing signal carrying its chosen exit horizon."""

    def __init__(self, *args, exit_mode: str, maximum_holding_sessions: int, **kwargs) -> None:
        if exit_mode not in {"follow_through"}:
            raise ValueError("exit_mode must be follow_through")
        super().__init__(*args, strategy_group="swing", **kwargs)
        object.__setattr__(self, "exit_mode", exit_mode)
        object.__setattr__(self, "maximum_holding_sessions", maximum_holding_sessions)

    def to_dict(self) -> dict:
        return {
            **super().to_dict(),
            "exit_mode": self.exit_mode,
            "maximum_holding_sessions": self.maximum_holding_sessions,
        }



class LLMSignalGenerator:
    def __init__(
        self,
        calendar: EventCalendar,
        config: LLMStrategyConfig,
        *,
        ai_signal_db: str | None = None,
    ) -> None:
        self.calendar = calendar
        self.config = config
        self.ai_signal_book = (
            LLMSignalBook(ai_signal_db or DEFAULT_AI_SIGNAL_DB)
            if ai_signal_db is not None else None
        )
        self.counts = {
            "ai_signals": 0,
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
        if self.ai_signal_book is not None:
            return self._generate_ai_signal(
                ticker, bars, as_of=as_of, universe=universe,
                strategy_state=strategy_state, position_qty=position_qty,
                entry_at=entry_at, entry_price=entry_price,
            )
        if position_qty:
            return self._manage_position(
                ticker, bars, as_of=as_of, position_qty=position_qty,
                entry_at=entry_at, entry_price=entry_price,
                exit_mode=str(strategy_state.get(ticker.upper(), {}).get("exit_mode", "follow_through")),
            )
        if not self.config.enabled:
            return None
        symbol = ticker.upper()
        return None

    def _generate_ai_signal(
        self,
        ticker: str,
        bars: list[dict],
        *,
        as_of: datetime,
        universe: set[str],
        strategy_state: dict,
        position_qty: float,
        entry_at: str | None,
        entry_price: float | None,
    ) -> Signal | None:
        book = self.ai_signal_book
        if book is None:
            return None
        recommendation = book.recommendation(ticker, universe=universe)
        if position_qty:
            if (
                recommendation is not None
                and recommendation.action == "sell"
                and recommendation.confidence >= self.config.minimum_confidence
                and position_qty > 0
            ):
                self.counts["ai_signals"] += 1
                return Signal(
                    ticker=ticker.upper(), action="sell", strategy="swing_llm_driven",
                    reason=recommendation.reason,
                    strength=recommendation.confidence / 100,
                    stop_pct=self.config.stop_pct,
                    target_weight=self.config.risk_limited_weight,
                    event_id=recommendation.run_id,
                )
            # Keep owner protection deterministic when a run has no sell call,
            # the AI snapshot is unavailable, or the holding predates this mode.
            return self._manage_position(
                ticker, bars, as_of=as_of, position_qty=position_qty,
                entry_at=entry_at, entry_price=entry_price,
                exit_mode=str(strategy_state.get(ticker.upper(), {}).get("exit_mode", "follow_through")),
            )

        if (
            not self.config.enabled
            or not self.config.allow_long
            or recommendation is None
            or recommendation.action != "buy"
            or recommendation.confidence < self.config.minimum_confidence
        ):
            return None

        self.counts["ai_signals"] += 1
        return Signal(
            ticker=ticker.upper(), action="buy", strategy="swing_llm_driven",
            reason=recommendation.reason,
            strength=recommendation.confidence / 100,
            stop_pct=self.config.stop_pct,
            strategy_group="swing",
            target_weight=self.config.risk_limited_weight,
            event_id=recommendation.run_id,
        )

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
        maximum = self.config.follow_through_sessions
        adverse = current > entry if is_short else current < entry
        if adverse and held >= 1:
            reason = "event follow-through reversed after the catalyst"
        elif held >= maximum:
            reason = f"event follow-through reached its {maximum}-session time stop"
        else:
            return None
        return EventSignal(
            ticker=ticker.upper(), action="cover" if is_short else "sell",
            strategy="swing_llm_driven", reason=reason, strength=0.8,
            stop_pct=self.config.stop_pct, target_weight=self.config.risk_limited_weight,
            risk_multiplier=self.config.risk_multiplier,
            exit_mode=exit_mode, maximum_holding_sessions=maximum,
        )

    def record_proposal(
        self, signal: Signal | dict, *, as_of: datetime, strategy_state: dict,
    ) -> None:
        event_id = signal.event_id if isinstance(signal, Signal) else signal.get("event_id")
        ticker = signal.ticker if isinstance(signal, Signal) else signal.get("ticker")
        if not isinstance(ticker, str):
            return
        metadata = signal.to_dict() if isinstance(signal, Signal) else signal
        state = strategy_state.setdefault(ticker.upper(), {})
        state["exit_mode"] = str(metadata.get("exit_mode", "follow_through"))
        state["event_maximum_holding_sessions"] = int(
            metadata.get("maximum_holding_sessions", self.config.follow_through_sessions)
        )

    def diagnostics(self, as_of: datetime | None = None) -> dict:
        ai_diagnostics = self.ai_signal_book.diagnostics() if self.ai_signal_book else None
        return {
            **(self.calendar.diagnostics(as_of) if self.ai_signal_book is None else {}),
            "enabled": self.config.enabled,
            "bar_horizon": "daily",
            "signal_source": "scheduled_ai" if self.ai_signal_book else "event_calendar",
            "ai_signal_book": ai_diagnostics,
            **self.counts,
        }


__all__ = ["LLMSignalGenerator"]
