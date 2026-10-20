"""Event options sleeve using scheduled catalysts, historical moves, and AI bias."""

from __future__ import annotations

from datetime import datetime, time, timezone
import math
from zoneinfo import ZoneInfo

from options.models import OptionSignal
from options.structures import straddle, vertical_spread
from strategies.options.base import (
    OptionsStrategyContext, choose_expiration, nearest_delta,
    regime_risk_multiplier, valid_quote,
)


ET = ZoneInfo("America/New_York")


def historical_event_moves(events, closes, *, ticker: str, event_type: str, as_of: datetime, lookback_days: int) -> list[float]:
    """Absolute event-window close moves from prior matching calendar rows.

    Timing is approximated from daily closes: pre-market/intraday events use
    prior close to event-day close; after-close events use event-day to next
    close. Intraday timestamps and special sessions require richer data later.
    """
    close_map = {day: float(value) for day, value in closes if value and float(value) > 0}
    dates = sorted(close_map)
    samples = []
    now_utc = as_of.astimezone(timezone.utc)
    for event in events:
        if str(getattr(event, "ticker", "")).upper() != ticker.upper():
            continue
        if str(getattr(event, "event_type", "")).lower() != event_type.lower():
            continue
        scheduled = getattr(event, "scheduled_at", None)
        if not isinstance(scheduled, datetime) or scheduled.astimezone(timezone.utc) >= now_utc:
            continue
        if (as_of.date() - scheduled.date()).days > lookback_days:
            continue
        event_day = scheduled.astimezone(ET).date()
        prior = [day for day in dates if day < event_day]
        after = [day for day in dates if day >= event_day]
        if scheduled.astimezone(ET).time() >= time(16, 0):
            prior = [day for day in dates if day <= event_day]
            after = [day for day in dates if day > event_day]
        if not prior or not after:
            continue
        first, second = prior[-1], after[0]
        if first == second:
            continue
        move = abs(math.log(close_map[second] / close_map[first]))
        if math.isfinite(move) and move > 0:
            samples.append(move)
    return samples


class EventDrivenOptionsStrategy:
    strategy_id = "options_event_driven"

    def __init__(self, config: dict) -> None:
        self.config = dict(config)

    def generate(self, context: OptionsStrategyContext) -> OptionSignal | None:
        if not self.config.get("enabled", True):
            return None
        regime_weight = regime_risk_multiplier(self.config, context.regime)
        if regime_weight <= 0:
            return None
        eligible = []
        for event in context.events:
            ticker = str(getattr(event, "ticker", "")).upper()
            scheduled = getattr(event, "scheduled_at", None)
            known = getattr(event, "known_at", None)
            if ticker != context.chain.underlying or not isinstance(scheduled, datetime):
                continue
            if isinstance(known, datetime) and known > context.as_of:
                continue
            days_before = (scheduled.astimezone(timezone.utc) - context.as_of.astimezone(timezone.utc)).total_seconds() / 86400
            if float(self.config.get("min_days_before_event", 0)) <= days_before <= float(self.config.get("max_days_before_event", 5)):
                eligible.append((days_before, event))
        if not eligible:
            return None
        days_before, event = min(eligible, key=lambda item: item[0])
        event_type = str(getattr(event, "event_type", "")).lower()
        moves = historical_event_moves(
            context.events, context.underlying_closes,
            ticker=context.chain.underlying, event_type=event_type,
            as_of=context.as_of,
            lookback_days=int(self.config.get("realized_lookback_days", 730)),
        )
        minimum_samples = int(self.config.get("min_historical_events", 3))
        if len(moves) < minimum_samples:
            return None
        hist_move = sum(moves) / len(moves)
        expiration = choose_expiration(
            context.chain,
            min_dte=max(int(self.config.get("min_dte", 3)), int(math.ceil(days_before)) + 1),
            max_dte=int(self.config.get("max_dte", 35)),
            target_dte=max(int(math.ceil(days_before)) + 7, 14),
        )
        if expiration is None:
            return None
        rows = [
            row for row in context.chain.for_expiration(expiration)
            if valid_quote(
                row,
                max_spread_pct=float(self.config.get("max_spread_pct", 0.20)),
                min_open_interest=0,
            )
        ]
        calls = [row for row in rows if row.contract.right == "call"]
        puts = [row for row in rows if row.contract.right == "put"]
        call = nearest_delta(calls, 0.50)
        put = nearest_delta(puts, -0.50)
        if call is None or put is None:
            return None
        # Scale the same-expiry ATM IV to the event horizon. Raw straddle
        # premium includes all time value through expiry and overstates a move
        # expected over only the next few sessions.
        atm_iv = (call.greeks.implied_volatility + put.greeks.implied_volatility) / 2.0
        implied_move = atm_iv * math.sqrt(max(days_before, 0.5) / 365.0)
        edge_pct = float(self.config.get("min_move_edge_pct", 0.15))

        if hist_move >= implied_move * (1.0 + edge_pct):
            try:
                structure = straddle(call.as_leg("buy"), put.as_leg("buy"), name=self.strategy_id)
            except ValueError:
                return None
            reason = (
                f"{event_type} event in {days_before:.1f} days; historical absolute event move "
                f"{hist_move:.1%} across {len(moves)} prior events exceeds implied move {implied_move:.1%}."
            )
            confidence = min(1.0, (hist_move / max(implied_move, 1e-6) - 1.0) / 0.75)
        else:
            recommendation = context.event_recommendations.get(context.chain.underlying)
            if recommendation is None:
                return None
            action = str(getattr(recommendation, "action", "")).lower()
            confidence_raw = float(getattr(recommendation, "confidence", 0.0))
            confidence = confidence_raw / 100.0 if confidence_raw > 1 else confidence_raw
            if action not in {"buy", "sell"} or confidence < float(self.config.get("min_llm_confidence", 0.60)):
                return None
            right = "call" if action == "buy" else "put"
            directional_rows = calls if right == "call" else puts
            long_row = call if right == "call" else put
            target_delta = 0.25 if right == "call" else -0.25
            short_row = nearest_delta(
                directional_rows, target_delta,
                predicate=lambda row: row.contract.strike > long_row.contract.strike if right == "call"
                else row.contract.strike < long_row.contract.strike,
            )
            if short_row is None:
                return None
            try:
                structure = vertical_spread(
                    long_row.as_leg("buy"), short_row.as_leg("sell"), name=self.strategy_id,
                )
            except ValueError:
                return None
            if structure.structure_type != "vertical_debit":
                return None
            reason = (
                f"{event_type} event in {days_before:.1f} days; directional {action} AI signal "
                f"(confidence {confidence:.0%}) expressed as a debit vertical."
            )
        return OptionSignal(
            strategy=self.strategy_id,
            underlying=context.chain.underlying,
            structure=structure,
            desired_contracts=1,
            confidence=confidence,
            reason=reason,
            regime=context.regime,
            created_at=context.as_of,
            event_id=str(getattr(event, "event_id", "")) or None,
            data_quality={
                "event_type": event_type,
                "days_before_event": days_before,
                "historical_event_move": hist_move,
                "historical_event_samples": len(moves),
                "implied_move": implied_move,
                "feed": context.chain.feed,
                "chain_quality": list(context.chain.data_quality),
                "regime_risk_multiplier": regime_weight,
            },
        )


__all__ = ["EventDrivenOptionsStrategy", "historical_event_moves"]
