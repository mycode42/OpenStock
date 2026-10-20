"""Defined-risk vertical expression of an existing directional equity signal."""

from __future__ import annotations

from options.models import OptionSignal
from options.structures import vertical_spread
from strategies.options.base import (
    OptionsStrategyContext, choose_expiration, nearest_delta,
    regime_risk_multiplier, valid_quote,
)


def _value(record, name: str, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


class DirectionalOverlayStrategy:
    """Translate a frozen, point-in-time equity signal into a debit vertical.

    The upstream equity signal is data, not a searched model. Research only
    varies the options expression (DTE, deltas, spread filter, and confidence).
    """

    strategy_id = "options_directional_overlay"

    def __init__(self, config: dict) -> None:
        self.config = dict(config)

    def generate(self, context: OptionsStrategyContext) -> OptionSignal | None:
        if not self.config.get("enabled", True):
            return None
        regime = str(context.regime or "unknown")
        regime_weight = regime_risk_multiplier(self.config, regime)
        if regime_weight <= 0:
            return None
        signal = context.equity_signals.get(context.chain.underlying)
        if signal is None:
            return None
        action = str(_value(signal, "action", "")).lower()
        confidence = float(_value(signal, "confidence", 0.0) or 0.0)
        if confidence > 1.0:
            confidence /= 100.0
        if action not in {"buy", "sell", "long", "short"}:
            return None
        if confidence < float(self.config.get("min_signal_confidence", 0.60)):
            return None
        expiration = choose_expiration(
            context.chain,
            min_dte=int(self.config.get("min_dte", 25)),
            max_dte=int(self.config.get("max_dte", 60)),
            target_dte=int(self.config.get("target_dte", 40)),
        )
        if expiration is None:
            return None
        right = "call" if action in {"buy", "long"} else "put"
        rows = [
            row for row in context.chain.for_expiration(expiration)
            if row.contract.right == right and valid_quote(
                row,
                max_spread_pct=float(self.config.get("max_spread_pct", 0.20)),
                min_open_interest=int(self.config.get("min_open_interest", 0)),
            )
        ]
        long_delta = float(self.config.get("long_delta", 0.50))
        short_delta = float(self.config.get("short_delta", 0.25))
        long_target = long_delta if right == "call" else -long_delta
        short_target = short_delta if right == "call" else -short_delta
        long_option = nearest_delta(rows, long_target)
        if long_option is None:
            return None
        short_option = nearest_delta(
            rows, short_target,
            predicate=lambda row: row.contract.strike > long_option.contract.strike if right == "call"
            else row.contract.strike < long_option.contract.strike,
        )
        if short_option is None:
            return None
        try:
            structure = vertical_spread(
                long_option.as_leg("buy"), short_option.as_leg("sell"),
                name=self.strategy_id,
            )
        except ValueError:
            return None
        if structure.structure_type != "vertical_debit":
            return None
        return OptionSignal(
            strategy=self.strategy_id,
            underlying=context.chain.underlying,
            structure=structure,
            desired_contracts=1,
            confidence=max(0.0, min(1.0, confidence)),
            reason=f"Existing {action} equity signal expressed as a defined-risk {right} debit vertical.",
            regime=regime,
            created_at=context.as_of,
            event_id=str(_value(signal, "signal_id", "") or "") or None,
            data_quality={
                "source_signal_strategy": _value(signal, "strategy"),
                "source_signal_confidence": confidence,
                "regime_risk_multiplier": regime_weight,
                "feed": context.chain.feed,
                "chain_quality": list(context.chain.data_quality),
            },
        )


__all__ = ["DirectionalOverlayStrategy"]
