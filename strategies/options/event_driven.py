"""Express fresh event-driven LLM briefing signals with defined-risk options."""

from __future__ import annotations

from options.models import OptionSignal
from options.structures import vertical_spread
from strategies.options.base import (
    OptionsStrategyContext,
    choose_expiration,
    nearest_delta,
    regime_risk_multiplier,
    valid_quote,
)


class EventDrivenOptionsStrategy:
    """Translate the existing event-driven strategy's AI briefing into a spread.

    The briefing snapshot is the source of the direction and confidence. This
    expression layer deliberately does not require the legacy event calendar.
    """

    strategy_id = "options_event_driven"

    def __init__(self, config: dict) -> None:
        self.config = dict(config)

    def generate(self, context: OptionsStrategyContext) -> OptionSignal | None:
        if not self.config.get("enabled", True):
            return None
        regime_weight = regime_risk_multiplier(self.config, context.regime)
        if regime_weight <= 0:
            return None

        recommendation = context.event_recommendations.get(context.chain.underlying)
        if recommendation is None:
            return None
        action = str(getattr(recommendation, "action", "")).lower()
        confidence_raw = float(getattr(recommendation, "confidence", 0.0))
        confidence = confidence_raw / 100.0 if confidence_raw > 1 else confidence_raw
        if action not in {"buy", "sell"} or confidence < float(self.config.get("min_llm_confidence", 0.60)):
            return None

        expiration = choose_expiration(
            context.chain,
            min_dte=int(self.config.get("min_dte", 3)),
            max_dte=int(self.config.get("max_dte", 35)),
            target_dte=int(self.config.get("target_dte", 21)),
        )
        if expiration is None:
            return None
        rows = [
            row for row in context.chain.for_expiration(expiration)
            if valid_quote(
                row,
                max_spread_pct=float(self.config.get("max_spread_pct", 0.20)),
                min_open_interest=int(self.config.get("min_open_interest", 100)),
            )
        ]
        right = "call" if action == "buy" else "put"
        directional_rows = [row for row in rows if row.contract.right == right]
        long_row = nearest_delta(directional_rows, 0.50 if right == "call" else -0.50)
        if long_row is None:
            return None
        short_row = nearest_delta(
            directional_rows,
            0.25 if right == "call" else -0.25,
            predicate=lambda row: row.contract.strike > long_row.contract.strike if right == "call"
            else row.contract.strike < long_row.contract.strike,
        )
        if short_row is None:
            return None
        try:
            structure = vertical_spread(long_row.as_leg("buy"), short_row.as_leg("sell"), name=self.strategy_id)
        except ValueError:
            return None
        if structure.structure_type != "vertical_debit":
            return None

        briefing_reason = str(getattr(recommendation, "reason", ""))
        reason = (
            f"Fresh event-driven LLM briefing recommends {action} {context.chain.underlying} "
            f"(confidence {confidence:.0%}): {briefing_reason}"
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
            event_id=str(getattr(recommendation, "run_id", "")) or None,
            data_quality={
                "signal_source": "event_driven_llm_briefing",
                "briefing_run_id": getattr(recommendation, "run_id", None),
                "briefing_generated_at": getattr(recommendation, "generated_at", None),
                "briefing_action": action,
                "briefing_confidence": confidence,
                "briefing_reason": briefing_reason,
                "feed": context.chain.feed,
                "chain_quality": list(context.chain.data_quality),
                "regime_risk_multiplier": regime_weight,
            },
        )


__all__ = ["EventDrivenOptionsStrategy"]
