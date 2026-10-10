"""Convert daily cross-sectional ranks into bounded long/short targets."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, replace
from datetime import date

from src.algo.strategies.multi_factor.config import MultiFactorConfig
from src.algo.strategies.multi_factor.features import FactorCalculator
from src.algo.strategies.multi_factor.ranking import FactorRanker
from src.algo.strategies.swing_common import clean_daily_bars, held_sessions


LOG = logging.getLogger("swarm_trader.multi_factor")


@dataclass(frozen=True)
class MultiFactorSignal:
    ticker: str
    strategy: str
    action: str
    direction: str
    score: float
    rank: int
    target_weight: float
    as_of: str
    rebalance_key: str
    factor_values: dict[str, float]
    factor_zscores: dict[str, float]
    reason: str
    stop_pct: float = 0.06
    risk_multiplier: float = 0.5
    strategy_group: str = "swing_satellite"

    @property
    def strength(self) -> float:
        directional_score = abs(self.score) if self.direction == "short" else self.score
        return max(0.0, min(1.0, 0.5 + directional_score / 6))

    def to_dict(self) -> dict:
        return {**asdict(self), "strength": self.strength}


class SignalGenerator:
    """Build PIT-safe factor evidence and rank candidates for the current regime."""

    def __init__(self, config: MultiFactorConfig | None = None):
        self.config = config or MultiFactorConfig()
        self.calculator = FactorCalculator(self.config)
        self.ranker = FactorRanker(self.config)

    def rebalance_key(self, session: date) -> str:
        if self.config.rebalance_frequency == "daily":
            return session.isoformat()
        year, week, _ = session.isocalendar()
        return f"{year}-W{week:02d}"

    def generate(
        self,
        bars_by_symbol: dict[str, list[dict]],
        *,
        symbols: list[str],
        as_of: date,
        session: date,
        regime: str = "unknown",
        fundamentals_by_symbol: dict[str, list[dict]] | None = None,
        held_positions: set[str] | dict[str, dict] | None = None,
    ) -> tuple[list[MultiFactorSignal], dict]:
        symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        features = self.calculator.calculate(
            bars_by_symbol, symbols=symbols, as_of=as_of,
            fundamentals_by_symbol=fundamentals_by_symbol,
        )
        ranked = self.ranker.rank(features)
        factor_coverage = {
            factor: sum(
                factor in values and isinstance(values[factor], (int, float))
                for values in features.values()
            )
            for factor, weight in self.config.factor_weights.items() if weight > 0
        }
        selected_factors = sorted({factor for row in ranked for factor in row["factor_values"]})
        omitted_factors = [
            f"{factor}: {count} names with usable values; need {self.config.min_universe}"
            for factor, count in factor_coverage.items()
            if factor not in selected_factors and count < self.config.min_universe
        ]
        if factor_coverage.get("value_earnings_yield") == 0 and "value_earnings_yield" in factor_coverage:
            omitted_factors = [
                item for item in omitted_factors if not item.startswith("value_earnings_yield:")
            ]
            omitted_factors.append("value_earnings_yield: no available point-in-time fundamentals")
        diagnostics = {
            "as_of": as_of.isoformat(),
            "rebalance_key": self.rebalance_key(session),
            "rebalance_frequency": self.config.rebalance_frequency,
            "regime": regime,
            "direction_mode": self.config.direction_mode,
            "daily_only": True,
            "universe_count": len(symbols),
            "feature_count": len(features),
            "ranked_count": len(ranked),
            "selected_factors": selected_factors,
            "factor_coverage": factor_coverage,
            "omitted_factors": omitted_factors,
            "minimum_universe_met": len(ranked) >= self.config.min_universe,
            "turnover": None,
        }
        if len(ranked) < self.config.min_universe:
            LOG.info("Multi-factor snapshot skipped: %d ranked names; need %d", len(ranked), self.config.min_universe)
            return [], diagnostics

        volatile_disabled = regime == "volatile" and self.config.disable_in_volatile
        long_candidates = [
            row for row in ranked[:self.config.top_n]
            if row["score"] > self.config.minimum_score
        ]
        bearish = regime in {"trending_down", "bearish", "risk_off"}
        short_candidates = [
            row for row in ranked[-self.config.top_n:]
            if row["score"] < -self.config.minimum_score
        ]
        if volatile_disabled:
            selected_longs, selected_shorts = [], []
        elif self.config.direction_mode == "long_only":
            selected_longs, selected_shorts = long_candidates, []
        elif self.config.direction_mode == "long_short":
            selected_longs, selected_shorts = long_candidates, short_candidates
        elif bearish:
            selected_longs, selected_shorts = [], short_candidates
        else:
            selected_longs, selected_shorts = long_candidates, []

        long_targets = {row["symbol"] for row in selected_longs}
        short_targets = {row["symbol"] for row in selected_shorts} - long_targets
        if isinstance(held_positions, dict):
            holdings = {str(symbol).upper(): dict(value) for symbol, value in held_positions.items()}
        else:
            holdings = {str(symbol).upper(): {"qty": 1} for symbol in (held_positions or set())}
        held = set(holdings)
        target_weight = self.config.max_position_pct
        diagnostics.update({
            "selected_long_count": len(long_targets),
            "selected_short_count": len(short_targets),
            "volatile_regime_disabled": volatile_disabled,
        })
        result = []
        rows_by_symbol = {row["symbol"]: row for row in ranked}
        for row in ranked:
            symbol = row["symbol"]
            desired_direction = "long" if symbol in long_targets else "short" if symbol in short_targets else "flat"
            if symbol in held:
                holding = holdings[symbol]
                quantity = float(holding.get("qty", 1) or 0)
                current_direction = "short" if quantity < 0 else "long"
                held_for = held_sessions(clean_daily_bars(bars_by_symbol.get(symbol, [])), holding.get("entry_at"))
                if held_for is not None and held_for >= self.config.maximum_holding_sessions:
                    action, direction = ("cover" if quantity < 0 else "sell"), "flat"
                elif desired_direction == current_direction or (
                    held_for is not None and held_for < self.config.minimum_hold_sessions
                ):
                    action, direction = "hold", current_direction
                else:
                    action, direction = ("cover" if quantity < 0 else "sell"), "flat"
            elif desired_direction == "long":
                action, direction = "buy", "long"
            elif desired_direction == "short":
                action, direction = "short", "short"
            elif symbol in held:
                action, direction = ("cover" if float(holdings[symbol].get("qty", 1) or 0) < 0 else "sell"), "flat"
            else:
                action, direction = "none", "flat"
            result.append(MultiFactorSignal(
                ticker=symbol, strategy="multi_factor", action=action, direction=direction,
                score=row["score"], rank=row["rank"],
                target_weight=target_weight if symbol in long_targets or symbol in short_targets else 0.0,
                as_of=as_of.isoformat(), rebalance_key=diagnostics["rebalance_key"],
                factor_values=row["factor_values"], factor_zscores=row["factor_zscores"],
                reason=(
                    f"{self.config.rebalance_frequency} factor {direction} candidate: "
                    f"rank {row['rank']}/{len(ranked)}, score {row['score']:.3f}, regime {regime}"
                ),
                stop_pct=self.config.hard_stop_pct,
                risk_multiplier=self.config.risk_multiplier,
            ))
        for symbol in sorted(held - set(rows_by_symbol)):
            holding = holdings[symbol]
            quantity = float(holding.get("qty", 1) or 0)
            held_for = held_sessions(clean_daily_bars(bars_by_symbol.get(symbol, [])), holding.get("entry_at"))
            if held_for is not None and held_for < self.config.minimum_hold_sessions:
                action = "hold"
                direction = "short" if quantity < 0 else "long"
            else:
                action = "cover" if quantity < 0 else "sell"
                direction = "flat"
            result.append(MultiFactorSignal(
                ticker=symbol, strategy="multi_factor", action=action, direction=direction,
                score=0.0, rank=len(ranked) + 1, target_weight=0.0,
                as_of=as_of.isoformat(), rebalance_key=diagnostics["rebalance_key"],
                factor_values={}, factor_zscores={},
                reason="held name is no longer eligible for the factor universe",
                stop_pct=self.config.hard_stop_pct,
                risk_multiplier=self.config.risk_multiplier,
            ))
        LOG.debug(
            "Multi-factor snapshot %s: ranked=%d targets=%d factors=%s",
            diagnostics["rebalance_key"], len(ranked), len(long_targets) + len(short_targets),
            diagnostics["selected_factors"],
        )
        return result, diagnostics

    def apply_intraday_size_tilt(
        self, signals: list[MultiFactorSignal], intraday_bars: dict[str, list[dict]],
    ) -> list[MultiFactorSignal]:
        """Optionally trim daily-ranked positions on a contrary short-term move.

        It never creates or removes a ticker target; an adverse three-bar move
        can only halve the already ranked proposal's target weight.
        """
        if not self.config.enable_intraday_tilt:
            return signals
        adjusted = []
        for signal in signals:
            bars = intraday_bars.get(signal.ticker, [])
            if signal.action not in {"buy", "short"} or len(bars) < 3:
                adjusted.append(signal)
                continue
            try:
                first, last = float(bars[-3]["c"]), float(bars[-1]["c"])
            except (KeyError, TypeError, ValueError):
                adjusted.append(signal)
                continue
            if first <= 0 or not math.isfinite(first) or not math.isfinite(last):
                adjusted.append(signal)
                continue
            move = last / first - 1
            adverse = move < -0.003 if signal.action == "buy" else move > 0.003
            adjusted.append(
                replace(signal, target_weight=signal.target_weight * 0.5)
                if adverse else signal
            )
        return adjusted
