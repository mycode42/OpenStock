"""Defined-risk long-volatility sleeve for volatile market regimes.

Starting assumptions: prefer a 25-delta long strangle, 7–45 DTE, and enter
only below the 75th point-in-time IV percentile after at least 20 prior daily
ATM-IV observations. Keep the options account's existing per-trade, vega, and
gamma limits enabled. Long premium has a known maximum loss (debit plus costs),
but remains exposed to IV crush, theta decay, and overnight gaps.
"""

from __future__ import annotations

from datetime import date
import math
from typing import Iterable

from options.data import OptionChain, realized_volatility
from options.models import OptionSignal, OptionStructure
from options.structures import straddle, strangle
from strategies.options.base import (
    OptionsStrategyContext,
    choose_expiration,
    nearest_delta,
    regime_risk_multiplier,
    valid_quote,
)


def historical_iv_percentile(current: float | None, history: Iterable[float], *, min_samples: int = 20) -> float | None:
    """Empirical point-in-time percentile (0–100); history must precede current."""
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    samples = []
    for value in history:
        try:
            sample = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(sample) and sample > 0:
            samples.append(sample)
    try:
        current_value = float(current)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(current_value) or current_value <= 0 or len(samples) < min_samples:
        return None
    return 100.0 * sum(value <= current_value for value in samples) / len(samples)


def _liquid_chain_rows(chain: OptionChain, expiration: date, config: dict):
    max_spread = float(config.get("max_spread_pct", 0.15))
    min_oi = int(config.get("min_open_interest", 100))
    key = ("long-volatility-liquid-rows", expiration, max_spread, min_oi)
    return chain.cached_derived(
        key,
        lambda: tuple(
            row for row in chain.for_expiration(expiration)
            if row.contract.open_interest is not None
            and row.contract.open_interest >= min_oi
            and valid_quote(row, max_spread_pct=max_spread, min_open_interest=min_oi)
        ),
    )


def atm_implied_volatility(chain: OptionChain, config: dict) -> float | None:
    """Return liquid, near-ATM call/put IV for the configured target expiry."""
    min_dte = int(config.get("min_dte", 7))
    max_dte = int(config.get("max_dte", 45))
    target_dte = int(config.get("target_dte", 30))
    max_spread = float(config.get("max_spread_pct", 0.15))
    min_oi = int(config.get("min_open_interest", 100))
    delta_tolerance = float(config.get("atm_delta_tolerance", 0.20))
    key = (
        "long-volatility-atm-iv", min_dte, max_dte, target_dte,
        max_spread, min_oi, delta_tolerance,
    )

    def calculate() -> float | None:
        expiration = choose_expiration(
            chain, min_dte=min_dte, max_dte=max_dte, target_dte=target_dte,
        )
        if expiration is None:
            return None
        rows = _liquid_chain_rows(chain, expiration, config)
        calls = [row for row in rows if row.contract.right == "call"]
        puts = [row for row in rows if row.contract.right == "put"]
        call = nearest_delta(calls, 0.50)
        put = nearest_delta(puts, -0.50)
        if call is None or put is None:
            return None
        if (
            abs(call.greeks.delta - 0.50) > delta_tolerance
            or abs(put.greeks.delta + 0.50) > delta_tolerance
        ):
            return None
        values = (call.greeks.implied_volatility, put.greeks.implied_volatility)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            return None
        return sum(values) / 2.0

    return chain.cached_derived(key, calculate)


def build_long_volatility_structure(chain: OptionChain, config: dict) -> OptionStructure | None:
    """Build one same-expiration long strangle or straddle from liquid quotes."""
    expiration = choose_expiration(
        chain,
        min_dte=int(config.get("min_dte", 7)),
        max_dte=int(config.get("max_dte", 45)),
        target_dte=int(config.get("target_dte", 30)),
    )
    if expiration is None:
        return None
    rows = _liquid_chain_rows(chain, expiration, config)
    calls = [row for row in rows if row.contract.right == "call"]
    puts = [row for row in rows if row.contract.right == "put"]
    kind = str(config.get("structure", "strangle")).strip().lower()
    try:
        if kind == "straddle":
            call_by_strike = {row.contract.strike: row for row in calls}
            put_by_strike = {row.contract.strike: row for row in puts}
            common = set(call_by_strike) & set(put_by_strike)
            strike = min(common, key=lambda value: (abs(value - chain.spot), value), default=None)
            if strike is None:
                return None
            structure = straddle(
                call_by_strike[strike].as_leg("buy"),
                put_by_strike[strike].as_leg("buy"),
                name=LongVolatilitySleeve.strategy_id,
            )
        elif kind == "strangle":
            target_delta = float(config.get("otm_delta", 0.25))
            put = nearest_delta(
                puts, -target_delta,
                predicate=lambda row: row.contract.strike < chain.spot,
            )
            call = nearest_delta(
                calls, target_delta,
                predicate=lambda row: row.contract.strike > chain.spot,
            )
            if put is None or call is None:
                return None
            structure = strangle(
                put.as_leg("buy"), call.as_leg("buy"),
                name=LongVolatilitySleeve.strategy_id,
            )
        else:
            raise ValueError("structure must be strangle or straddle")
    except ValueError:
        return None
    if (
        not structure.risk.defined_risk
        or structure.net_debit_per_share <= 0
        or structure.risk.premium_at_risk > float(config.get("max_premium_per_trade_usd", 1000.0))
    ):
        return None
    return structure


def long_volatility_exit_reason(
    *,
    entry_debit: float,
    current_value: float,
    held_days: int,
    days_to_expiry: int,
    regime: str,
    entry_regime: str = "volatile",
    config: dict,
) -> str | None:
    """Apply deterministic close rules to long-option package liquidation value."""
    if entry_debit <= 0 or not math.isfinite(entry_debit) or not math.isfinite(current_value):
        return None
    if (
        bool(config.get("close_on_regime_exit", True))
        and str(entry_regime).lower() == "volatile"
        and str(regime).lower() != "volatile"
    ):
        return "regime_left_volatile"
    if days_to_expiry <= int(config.get("close_dte", 3)):
        return "time_to_expiry_stop"
    if held_days >= int(config.get("max_holding_days", 14)):
        return "maximum_holding_period"
    pnl = current_value - entry_debit
    if pnl <= -entry_debit * float(config.get("stop_loss_pct", 0.50)):
        return "structure_stop_loss"
    if pnl >= entry_debit * float(config.get("profit_target_pct", 0.40)):
        return "profit_target"
    return None


class LongVolatilitySleeve:
    """Buy a capped-loss volatility package only when activation and gates pass."""

    strategy_id = "options_long_volatility_sleeve"

    def __init__(self, config: dict) -> None:
        self.config = dict(config)

    @staticmethod
    def atm_implied_volatility(chain: OptionChain, config: dict) -> float | None:
        return atm_implied_volatility(chain, config)

    @staticmethod
    def exit_reason(**kwargs) -> str | None:
        return long_volatility_exit_reason(**kwargs)

    def _activation(self, context: OptionsStrategyContext) -> tuple[bool, float, str]:
        regime = str(context.regime or "unknown").lower()
        volatility_state = str(context.volatility_state or "unknown").lower()
        allowed = {str(value).lower() for value in self.config.get("allowed_regimes", ["volatile"])}
        if "volatile" in allowed and "volatile" in {regime, volatility_state}:
            weight = regime_risk_multiplier(self.config, "volatile")
            return weight > 0, weight, (
                f"volatile market state (regime {regime}, volatility {volatility_state})"
            )
        if str(self.config.get("activation_mode", "regime_only")) == "regime_or_realized_vol":
            try:
                lookback = int(self.config.get("realized_lookback_sessions", 20))
                realized = realized_volatility(list(context.underlying_closes)[-(lookback + 1):])
            except (ValueError, TypeError):
                return False, 0.0, "realized-volatility activation data unavailable"
            threshold = float(self.config.get("min_realized_volatility", 0.30))
            if realized >= threshold:
                weight = float(self.config.get("threshold_risk_multiplier", 0.50))
                return 0 < weight <= 2.0, weight, f"realized volatility {realized:.1%} exceeded {threshold:.1%}"
        return False, 0.0, f"regime {regime} is outside the long-volatility activation set"

    def generate(self, context: OptionsStrategyContext) -> OptionSignal | None:
        if not self.config.get("enabled", True):
            return None
        chain = context.chain
        underlyings = {str(value).upper() for value in self.config.get("underlyings", ["SPY"])}
        if chain.underlying.upper() not in underlyings:
            return None
        activated, weight, activation_reason = self._activation(context)
        if not activated:
            return None

        current_iv = atm_implied_volatility(chain, self.config)
        minimum_samples = int(self.config.get("iv_rank_min_samples", 20))
        try:
            iv_percentile = float(context.iv_percentile)
            iv_sample_count = int(context.iv_percentile_samples)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(iv_percentile) or not 0 <= iv_percentile <= 100 or iv_sample_count < minimum_samples:
            return None
        ceiling = float(self.config.get("iv_percentile_ceiling", 75.0))
        if iv_percentile > ceiling:
            return None
        maximum_iv_rv_ratio = self.config.get("max_iv_to_realized_ratio")
        realized = None
        if maximum_iv_rv_ratio is not None:
            try:
                lookback = int(self.config.get("realized_lookback_sessions", 20))
                realized = realized_volatility(list(context.underlying_closes)[-(lookback + 1):])
            except (ValueError, TypeError):
                return None
            if current_iv is None or realized <= 0 or current_iv / realized > float(maximum_iv_rv_ratio):
                return None

        structure = build_long_volatility_structure(chain, self.config)
        if structure is None:
            return None
        # A threshold-triggered entry uses an explicit reduced risk weight.
        ivs = [leg.greeks.implied_volatility for leg in structure.legs]
        reason = (
            f"{activation_reason}; liquid {structure.structure_type.replace('_', ' ')} "
            f"at {structure.expiration.isoformat()}, {iv_percentile:.0f}th IV percentile "
            f"({iv_sample_count} prior samples); defined maximum loss is the debit."
        )
        return OptionSignal(
            strategy=self.strategy_id,
            underlying=chain.underlying,
            structure=structure,
            desired_contracts=1,
            confidence=max(0.0, min(1.0, 1.0 - iv_percentile / 100.0)),
            reason=reason,
            regime=str(context.regime or "unknown"),
            created_at=context.as_of,
            data_quality={
                "entry_reason": reason,
                "regime_at_entry": str(context.regime or "unknown"),
                "volatility_state_at_entry": str(context.volatility_state or "unknown"),
                "activation_state": (
                    "volatile"
                    if str(context.regime or "").lower() == "volatile"
                    or str(context.volatility_state or "").lower() == "volatile"
                    else "realized_volatility"
                ),
                "iv_percentile": iv_percentile,
                "iv_percentile_samples": iv_sample_count,
                "iv_percentile_source": context.iv_percentile_source,
                "atm_implied_volatility": current_iv,
                "structure_leg_iv": ivs,
                "structure": structure.structure_type,
                "expiration": structure.expiration.isoformat(),
                "strikes": [leg.contract.strike for leg in structure.legs],
                "premium_at_risk": structure.risk.premium_at_risk,
                "max_premium_per_trade_usd": float(self.config.get("max_premium_per_trade_usd", 1000.0)),
                "max_risk_per_trade_pct": float(self.config.get("max_risk_per_trade_pct", 0.005)),
                "regime_risk_multiplier": weight,
                "activation_reason": activation_reason,
                "realized_volatility": realized,
                "feed": chain.feed,
                "chain_quality": list(chain.data_quality),
            },
        )


__all__ = [
    "LongVolatilitySleeve", "atm_implied_volatility", "build_long_volatility_structure",
    "historical_iv_percentile", "long_volatility_exit_reason",
]
