"""Rule-based volatility-risk-premium sleeve using a defined-risk iron condor."""

from __future__ import annotations

import math

from options.data import realized_volatility
from options.models import OptionSignal
from options.structures import iron_condor
from strategies.options.base import (
    OptionsStrategyContext, choose_expiration, nearest_delta,
    regime_risk_multiplier, valid_quote,
)


def iv_rank_allows(iv_percentile: float | None, minimum: float | None) -> bool:
    """Entry gate on the point-in-time IV percentile. No minimum configured means no gate; a missing percentile fails closed."""
    if minimum is None:
        return True
    return iv_percentile is not None and float(iv_percentile) >= float(minimum)


def vrp_exit_reason(
    *, entry_credit: float, pnl_if_closed: float | None, held_days: int, config: dict,
) -> str | None:
    """Close a short-premium structure once its loss reaches the configured multiple of the credit received.

    Unset settings keep the strategy's behaviour: no stop and no holding limit.
    """
    if not math.isfinite(entry_credit) or entry_credit <= 0 or pnl_if_closed is None or not math.isfinite(pnl_if_closed):
        return None
    maximum_days = config.get("max_holding_days")
    if maximum_days is not None and held_days >= int(maximum_days):
        return "maximum_holding_period"
    multiple = config.get("stop_loss_credit_multiple")
    if multiple is not None and pnl_if_closed <= -float(multiple) * entry_credit:
        return "structure_stop_loss"
    return None


class VolatilityRiskPremiumStrategy:
    strategy_id = "options_vrp"

    def __init__(self, config: dict) -> None:
        self.config = dict(config)

    def generate(self, context: OptionsStrategyContext) -> OptionSignal | None:
        if not self.config.get("enabled", True):
            return None
        regime = str(context.regime or "unknown")
        if regime not in set(self.config.get("allowed_regimes", [])):
            return None
        regime_weight = regime_risk_multiplier(self.config, regime)
        if regime_weight <= 0:
            return None
        if not iv_rank_allows(context.iv_percentile, self.config.get("min_iv_percentile")):
            return None
        closes = list(context.underlying_closes)
        lookback = int(self.config.get("realized_lookback_sessions", 20))
        try:
            realized = realized_volatility(closes[-(lookback + 1):])
        except ValueError:
            return None
        expiration = choose_expiration(
            context.chain,
            min_dte=int(self.config.get("min_dte", 25)),
            max_dte=int(self.config.get("max_dte", 45)),
        )
        if expiration is None:
            return None
        rows = [
            row for row in context.chain.for_expiration(expiration)
            if valid_quote(
                row,
                max_spread_pct=float(self.config.get("max_quote_spread_pct", 0.25)),
                min_open_interest=int(self.config.get("min_open_interest", 0)),
            )
        ]
        calls = [row for row in rows if row.contract.right == "call"]
        puts = [row for row in rows if row.contract.right == "put"]
        atm_call = nearest_delta(calls, 0.50)
        atm_put = nearest_delta(puts, -0.50)
        if atm_call is None or atm_put is None:
            return None
        atm_iv = (atm_call.greeks.implied_volatility + atm_put.greeks.implied_volatility) / 2.0
        edge = atm_iv - realized
        minimum_edge = float(self.config.get("min_iv_minus_rv", 0.04))
        if edge < minimum_edge:
            return None

        delta_min = float(self.config.get("short_delta_min", 0.10))
        delta_max = float(self.config.get("short_delta_max", 0.20))
        target_delta = (delta_min + delta_max) / 2.0
        short_call = nearest_delta(
            calls, target_delta,
            predicate=lambda row: delta_min <= row.greeks.delta <= delta_max and row.contract.strike > context.chain.spot,
        )
        short_put = nearest_delta(
            puts, -target_delta,
            predicate=lambda row: -delta_max <= row.greeks.delta <= -delta_min and row.contract.strike < context.chain.spot,
        )
        wing_delta = float(self.config.get("wing_delta_max", 0.07))
        long_call = nearest_delta(
            calls, wing_delta,
            predicate=lambda row: row.contract.strike > (short_call.contract.strike if short_call else float("inf"))
            and abs(row.greeks.delta) <= wing_delta,
        )
        long_put = nearest_delta(
            puts, -wing_delta,
            predicate=lambda row: row.contract.strike < (short_put.contract.strike if short_put else 0)
            and abs(row.greeks.delta) <= wing_delta,
        )
        if not all((short_call, short_put, long_call, long_put)):
            return None
        try:
            structure = iron_condor(
                long_put.as_leg("buy"), short_put.as_leg("sell"),
                short_call.as_leg("sell"), long_call.as_leg("buy"),
                name=self.strategy_id,
            )
        except ValueError:
            return None
        if not structure.risk.defined_risk or structure.net_debit_per_share >= 0:
            return None
        confidence = max(0.0, min(1.0, edge / max(atm_iv, 0.01)))
        return OptionSignal(
            strategy=self.strategy_id,
            underlying=context.chain.underlying,
            structure=structure,
            desired_contracts=1,
            confidence=confidence,
            reason=(
                f"ATM IV {atm_iv:.1%} exceeds {lookback}-session realized volatility "
                f"{realized:.1%} by {edge:.1%}; defined-risk {expiration.isoformat()} iron condor."
            ),
            regime=regime,
            created_at=context.as_of,
            data_quality={
                "iv": atm_iv,
                "realized_volatility": realized,
                "iv_minus_realized": edge,
                "feed": context.chain.feed,
                "chain_quality": list(context.chain.data_quality),
                "regime_risk_multiplier": regime_weight,
            },
        )


__all__ = ["VolatilityRiskPremiumStrategy"]
