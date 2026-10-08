"""Robust cross-sectional normalization and equal-weight score combination."""

from __future__ import annotations

import math
from statistics import mean, pstdev

from src.strategies.multi_factor.config import MultiFactorConfig


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    location = (len(ordered) - 1) * fraction
    lower = math.floor(location)
    upper = math.ceil(location)
    portion = location - lower
    return ordered[lower] * (1 - portion) + ordered[upper] * portion


class Combiner:
    """Combine normalized cross-sectional factors with configured linear weights."""

    def __init__(self, config: MultiFactorConfig | None = None):
        self.config = config or MultiFactorConfig()

    def combine(
        self,
        zscores: dict[str, dict[str, float]],
        factor_values: dict[str, dict[str, float]],
        factors: list[str],
    ) -> list[dict]:
        usable = [
            name for name in factors
            if self.config.factor_weights.get(name, 0) > 0
        ]
        if not usable:
            return []
        max_weight = max(self.config.factor_weights[name] for name in usable)
        normalized_weights = {
            name: self.config.factor_weights[name] / max_weight for name in usable
        }
        total_weight = sum(normalized_weights.values())
        rows = []
        for symbol, values in zscores.items():
            if any(name not in values for name in usable):
                continue
            factor_scores = {name: values[name] for name in usable}
            score = sum(
                factor_scores[name] * normalized_weights[name] / total_weight
                for name in usable
            )
            rows.append({
                "symbol": symbol,
                "score": score,
                "factor_values": {name: factor_values[symbol][name] for name in usable},
                "factor_zscores": factor_scores,
            })
        rows.sort(key=lambda row: (-row["score"], row["symbol"]))
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows


class FactorRanker:
    """Winsorize and z-score each available factor cross-sectionally."""

    def __init__(self, config: MultiFactorConfig | None = None):
        self.config = config or MultiFactorConfig()
        self.combiner = Combiner(self.config)

    def rank(self, factor_values: dict[str, dict[str, float]]) -> list[dict]:
        active = [name for name, weight in self.config.factor_weights.items() if weight > 0]
        usable = [
            name for name in active
            if sum(name in values and math.isfinite(values[name]) for values in factor_values.values())
            >= self.config.min_universe
        ]
        if not usable:
            return []
        zscores: dict[str, dict[str, float]] = {symbol: {} for symbol in factor_values}
        for factor in usable:
            sample = [
                values[factor] for values in factor_values.values()
                if factor in values and math.isfinite(values[factor])
            ]
            low = _quantile(sample, self.config.winsor_quantile)
            high = _quantile(sample, 1 - self.config.winsor_quantile)
            clipped = {symbol: min(high, max(low, values[factor])) for symbol, values in factor_values.items() if factor in values}
            center = mean(clipped.values())
            deviation = pstdev(clipped.values())
            for symbol, value in clipped.items():
                zscores[symbol][factor] = (value - center) / deviation if deviation > 0 else 0.0
        return self.combiner.combine(zscores, factor_values, usable)
