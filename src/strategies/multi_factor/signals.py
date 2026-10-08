"""Convert ranked cross-sectional scores into long-only rebalance targets."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date

from src.strategies.multi_factor.config import MultiFactorConfig
from src.strategies.multi_factor.features import FactorCalculator
from src.strategies.multi_factor.ranking import FactorRanker


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

    @property
    def strength(self) -> float:
        return max(0.0, min(1.0, 0.5 + self.score / 6))

    def to_dict(self) -> dict:
        return {**asdict(self), "strength": self.strength}


class SignalGenerator:
    """Build a PIT-safe factor snapshot and target the top-ranked long names."""

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
        held_positions: set[str] | None = None,
        fundamentals_by_symbol: dict[str, list[dict]] | None = None,
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

        selected = [
            row for row in ranked[:self.config.top_n]
            if row["score"] >= self.config.minimum_score
        ]
        targets = {row["symbol"] for row in selected}
        held = {symbol.upper() for symbol in (held_positions or set())}
        target_weight = 1 / self.config.top_n
        result = []
        rows_by_symbol = {row["symbol"]: row for row in ranked}
        for row in ranked:
            symbol = row["symbol"]
            if symbol in targets:
                action, direction = ("hold", "long") if symbol in held else ("buy", "long")
            elif symbol in held:
                action, direction = "sell", "flat"
            else:
                action, direction = "none", "flat"
            result.append(MultiFactorSignal(
                ticker=symbol, strategy="multi_factor", action=action, direction=direction,
                score=row["score"], rank=row["rank"],
                target_weight=target_weight if symbol in targets else 0.0,
                as_of=as_of.isoformat(), rebalance_key=diagnostics["rebalance_key"],
                factor_values=row["factor_values"], factor_zscores=row["factor_zscores"],
                reason=f"{self.config.rebalance_frequency} factor rebalance: rank {row['rank']}/{len(ranked)}, score {row['score']:.3f}",
            ))
        for symbol in sorted(held - set(rows_by_symbol)):
            result.append(MultiFactorSignal(
                ticker=symbol, strategy="multi_factor", action="sell", direction="flat",
                score=0.0, rank=len(ranked) + 1, target_weight=0.0,
                as_of=as_of.isoformat(), rebalance_key=diagnostics["rebalance_key"],
                factor_values={}, factor_zscores={},
                reason="held name is no longer eligible for the factor universe",
            ))
        LOG.info(
            "Multi-factor snapshot %s: ranked=%d targets=%d factors=%s",
            diagnostics["rebalance_key"], len(ranked), len(targets), diagnostics["selected_factors"],
        )
        return result, diagnostics
