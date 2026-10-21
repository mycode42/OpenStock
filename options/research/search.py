"""Small constrained grids and an interpretable distance-weighted surrogate."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import product
import math
from typing import Callable


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    parameters: dict
    allowed_regimes: tuple[str, ...]
    grid_indices: tuple[int, ...]
    is_baseline: bool = False

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "parameters": self.parameters,
            "allowed_regimes": list(self.allowed_regimes),
            "is_baseline": self.is_baseline,
        }


def _candidate(parameters: dict, regimes, indices, *, baseline=False) -> Candidate:
    canonical = {"parameters": parameters, "allowed_regimes": sorted(regimes)}
    identity = sha256(repr(sorted(canonical.items())).encode("utf-8")).hexdigest()[:14]
    return Candidate(identity, dict(parameters), tuple(sorted(set(regimes))), tuple(indices), baseline)


def candidate_pool(strategy: str, config: dict, base_config: dict) -> list[Candidate]:
    grids = config.get("search", {}).get("grids", {}).get(strategy, {})
    names = tuple(sorted(grids))
    values = []
    for name in names:
        choices = grids[name]
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"search grid {strategy}.{name} must be a non-empty list")
        if len(choices) > 7:
            raise ValueError(f"search grid {strategy}.{name} exceeds the seven-value limit")
        values.append(choices)
    search_config = config.get("search", {})
    regime_sets = search_config.get("regime_sets_by_strategy", {}).get(strategy)
    if regime_sets is None:
        regime_sets = search_config.get("regime_sets")
    regime_sets = regime_sets or [
        base_config.get(strategy, {}).get("allowed_regimes", ["range_bound"]),
    ]
    if not isinstance(regime_sets, list) or not regime_sets:
        raise ValueError("search.regime_sets must be a non-empty list")
    baseline_regimes = base_config.get(strategy, {}).get("allowed_regimes")
    if baseline_regimes is None:
        baseline_regimes = [
            key for key, value in base_config.get("regime_risk_multipliers", {}).items()
            if float(value) > 0
        ]
    candidates = [_candidate({}, baseline_regimes, (), baseline=True)]
    for regime_index, regimes in enumerate(regime_sets):
        if not isinstance(regimes, list) or not regimes:
            raise ValueError("each regime set must be a non-empty list of regime labels")
        for indices in product(*(range(len(choices)) for choices in values)) if values else [()]:
            parameters = {name: values[pos][choice] for pos, (name, choice) in enumerate(zip(names, indices))}
            candidates.append(_candidate(parameters, regimes, (regime_index, *indices)))
    # Keep baseline first and remove repeated values/regime combinations.
    unique = {}
    for candidate in candidates:
        unique.setdefault(candidate.candidate_id, candidate)
    return list(unique.values())


def _distance(left: Candidate, right: Candidate, grid: dict, regime_sets: list) -> float:
    dimensions = []
    names = tuple(sorted(grid))
    if left.grid_indices and right.grid_indices:
        if left.grid_indices[0] != right.grid_indices[0]:
            dimensions.append(1.0)
        for offset, name in enumerate(names, start=1):
            choices = grid[name]
            a = left.grid_indices[offset] if len(left.grid_indices) > offset else -1
            b = right.grid_indices[offset] if len(right.grid_indices) > offset else -1
            dimensions.append(abs(a - b) / max(1, len(choices) - 1))
    else:
        dimensions.extend(
            0.0 if left.parameters.get(name) == right.parameters.get(name) else 1.0
            for name in names
        )
        dimensions.append(0.0 if left.allowed_regimes == right.allowed_regimes else 1.0)
    return math.sqrt(sum(value * value for value in dimensions))


def _select_budget(pool: list[Candidate], budget: int) -> list[Candidate]:
    if len(pool) <= budget:
        return pool
    baseline = [item for item in pool if item.is_baseline]
    others = [item for item in pool if not item.is_baseline]
    remaining = max(0, budget - len(baseline))
    if remaining == 0:
        return baseline[:budget]
    selected = [others[round(index * (len(others) - 1) / max(1, remaining - 1))] for index in range(remaining)]
    unique = {item.candidate_id: item for item in baseline + selected}
    return list(unique.values())[:budget]


def evaluate_candidates(
    strategy: str,
    config: dict,
    base_config: dict,
    evaluator: Callable[[Candidate], dict],
) -> list[dict]:
    search_config = config.get("search", {})
    budget = int(search_config.get("maximum_candidates", 20))
    if budget < 2 or budget > 100:
        raise ValueError("search.maximum_candidates must be between 2 and 100")
    full_pool = candidate_pool(strategy, config, base_config)
    mode = str(search_config.get("mode", "surrogate")).lower()
    if mode not in {"grid", "surrogate"}:
        raise ValueError("search.mode must be grid or surrogate")
    if mode == "grid":
        selected = _select_budget(full_pool, budget)
        return [{"candidate": item.to_dict(), **evaluator(item)} for item in selected]

    # A tiny inverse-distance surrogate avoids a new ML dependency and keeps
    # the adaptive search inspectable. It predicts train-window score only;
    # untouched OOS and holdout data never enter this loop.
    results = []
    remaining = list(full_pool)
    seed = _select_budget(full_pool, min(4, budget))
    for item in seed:
        results.append({"candidate": item.to_dict(), **evaluator(item), "_candidate_obj": item})
        remaining.remove(item)
    exploration = float(search_config.get("surrogate_exploration", 0.15))
    while remaining and len(results) < budget:
        scored = []
        observed = [(row["_candidate_obj"], float(row.get("risk_adjusted_score", -5.0))) for row in results]
        # Keep private objects out of reports while retaining them for distance calculations.
        for item in remaining:
            distances = [(max(1e-6, _distance(item, known, search_config.get("grids", {}).get(strategy, {}), search_config.get("regime_sets", []))), score) for known, score in observed]
            weights = [1.0 / (distance * distance) for distance, _ in distances]
            prediction = sum(weight * score for weight, (_, score) in zip(weights, distances)) / sum(weights)
            novelty = min(distance for distance, _ in distances)
            scored.append((prediction + exploration * novelty, item))
        _, chosen = max(scored, key=lambda pair: (pair[0], pair[1].candidate_id))
        result = evaluator(chosen)
        results.append({"candidate": chosen.to_dict(), **result, "_candidate_obj": chosen})
        remaining.remove(chosen)
    for row in results:
        row.pop("_candidate_obj", None)
    return results


def add_plateau_scores(
    rows: list[dict],
    *,
    min_profitable_neighbour_fraction: float,
    neighbour_count: int,
) -> list[dict]:
    for row in rows:
        candidate = row["candidate"]
        neighbours = []
        for other in rows:
            if other is row:
                continue
            p_left, p_right = candidate.get("parameters", {}), other["candidate"].get("parameters", {})
            changed = sum(p_left.get(key) != p_right.get(key) for key in set(p_left) | set(p_right))
            regime_changed = candidate.get("allowed_regimes") != other["candidate"].get("allowed_regimes")
            if changed + int(regime_changed) == 1:
                neighbours.append(other)
        neighbours.sort(key=lambda item: -float(item.get("risk_adjusted_score", -5.0)))
        neighbours = neighbours[:neighbour_count]
        scores = [float(item.get("risk_adjusted_score", -5.0)) for item in neighbours]
        fraction = sum(score > 0 for score in scores) / len(scores) if scores else 0.0
        row["plateau"] = {
            "neighbour_count": len(scores),
            "profitable_neighbour_fraction": fraction,
            "neighbour_median_score": sorted(scores)[len(scores) // 2] if scores else -5.0,
            "stable": bool(scores) and fraction >= min_profitable_neighbour_fraction,
        }
        row["selection_score"] = (
            0.65 * float(row.get("risk_adjusted_score", -5.0))
            + 0.35 * float(row["plateau"]["neighbour_median_score"])
        )
    rows.sort(key=lambda item: (item.get("selection_score", -5.0), item.get("candidate", {}).get("candidate_id", "")), reverse=True)
    return rows


__all__ = ["Candidate", "add_plateau_scores", "candidate_pool", "evaluate_candidates"]
