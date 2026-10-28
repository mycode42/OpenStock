"""Search-space validation and bounded candidate generation."""

from __future__ import annotations

from copy import deepcopy
import itertools
import json
import math
from pathlib import Path
import random
import statistics

from strategies.registry import ALGO_DEFAULT_PARAMETERS, PARAMETER_KEYS, validate_parameter_profile
from ..artifacts.candidate_archive import candidate_id, logic_version, model_family
from .adaptive_config import (
    DEFAULT_SEARCH_SPACES, MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
)
from strategies.multi_factor.config import REGIME_FACTOR_WEIGHT_PROFILES

def _search_space(strategy: str, path: str | None) -> dict[str, list]:
    space = deepcopy(DEFAULT_SEARCH_SPACES[strategy])
    if path:
        supplied = json.loads(Path(path).read_text())
        if not isinstance(supplied, dict):
            raise ValueError("search-space JSON must map parameter names to non-empty value arrays")
        space.update(supplied)
    if not space or any(not isinstance(values, list) or not values for values in space.values()):
        raise ValueError("each search-space parameter must have a non-empty list of candidate values")
    # Searchable strategy parameters are broader than the fields explicitly
    # overridden in ALGO_DEFAULT_PARAMETERS (notably the intraday signal knobs).
    allowed = set(PARAMETER_KEYS[strategy])
    unknown = set(space) - allowed
    if unknown:
        raise ValueError(f"search space includes unsupported {strategy} parameters: {', '.join(sorted(unknown))}")
    return space


def _extend_space_from_archived_parents(
    strategy: str, space: dict[str, list], parents: list[dict] | None,
) -> tuple[dict[str, list], dict[str, list]]:
    """Carry strong archived parameter values into later runs' proposal domains."""
    extended = deepcopy(space)
    added: dict[str, list] = {}
    for parent in parents or []:
        if not isinstance(parent, dict):
            continue
        for name, values in extended.items():
            if strategy == "swing_multi_factor" and name == "min_universe":
                continue
            if name not in parent:
                continue
            value = deepcopy(parent[name])
            try:
                encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
                existing = {
                    json.dumps(item, sort_keys=True, separators=(",", ":"))
                    for item in values
                }
                if encoded in existing:
                    continue
                _make_parameters(strategy, {name: value})
            except (TypeError, ValueError):
                continue
            # The archive supplies at most six diverse parents. Keep the extra
            # options bounded so inherited values do not swamp fresh proposals.
            if len(added.get(name, [])) >= 6:
                continue
            values.append(value)
            added.setdefault(name, []).append(value)
    return extended, added


def _refine_saturated_numeric_domains(
    strategy: str, space: dict[str, list], candidates: list[dict], *,
    minimum_coverage: float = 0.75, max_dimensions: int = 2,
) -> tuple[dict[str, list], list[dict]]:
    """Insert validated local midpoints where a plateaued search covered a numeric domain."""
    refined = deepcopy(space)
    ranked = sorted(
        (row for row in candidates if isinstance(row.get("parameters"), dict)),
        key=lambda row: (bool(row.get("eligible")), float(row.get("fitness") or -1e9)),
        reverse=True,
    )
    if not ranked:
        return refined, []

    dimensions = []
    for name, values in space.items():
        # This is a bounded, categorical coverage policy. In particular, the
        # minimum-universe floor must stay tied to observed coverage checks.
        if name == "min_universe" or len(values) < 2:
            continue
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            continue
        numeric_values = sorted({float(value) for value in values if math.isfinite(float(value))})
        if len(numeric_values) < 2:
            continue
        covered = {
            float(row["parameters"][name])
            for row in candidates
            if isinstance(row.get("parameters"), dict)
            and isinstance(row["parameters"].get(name), (int, float))
            and not isinstance(row["parameters"].get(name), bool)
            and math.isfinite(float(row["parameters"][name]))
            and float(row["parameters"][name]) in numeric_values
        }
        coverage = len(covered) / len(numeric_values)
        if coverage >= minimum_coverage:
            gaps = [right - left for left, right in zip(numeric_values, numeric_values[1:])]
            dimensions.append((coverage, max(gaps), name, numeric_values))

    dimensions.sort(key=lambda item: (-item[0], -item[1], item[2]))
    changes = []
    for coverage, _, name, numeric_values in dimensions[:max_dimensions]:
        winner = next(
            (row for row in ranked if isinstance(row["parameters"].get(name), (int, float))
             and not isinstance(row["parameters"].get(name), bool)),
            ranked[0],
        )
        winner_value = float(winner["parameters"].get(name, numeric_values[0]))
        anchor = min(numeric_values, key=lambda value: abs(value - winner_value))
        intervals = list(zip(numeric_values, numeric_values[1:]))
        intervals.sort(key=lambda pair: (0 if anchor in pair else 1, -(pair[1] - pair[0])))
        for left, right in intervals:
            raw_midpoint = (left + right) / 2
            if all(isinstance(value, int) and not isinstance(value, bool) for value in space[name]):
                midpoint = int(round(raw_midpoint))
                if midpoint <= left or midpoint >= right:
                    continue
            else:
                midpoint = round(raw_midpoint, 6)
                if midpoint <= left or midpoint >= right:
                    continue
            if any(float(value) == float(midpoint) for value in refined[name]):
                continue
            try:
                _make_parameters(strategy, {name: midpoint})
            except ValueError:
                continue
            refined[name].append(midpoint)
            refined[name].sort(key=float)
            changes.append({
                "parameter": name,
                "previous_values": list(space[name]),
                "added_value": midpoint,
                "refinement_interval": [left, right],
                "observed_domain_coverage": round(coverage, 4),
                "refined_from_candidate": winner.get("name"),
            })
            break
    return refined, changes

def _make_parameters(strategy: str, values: dict) -> dict:
    parameters = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    parameters.update(deepcopy(values))
    return validate_parameter_profile({strategy: parameters})[strategy]

def _planned_ablation_variants(strategy: str, baseline: dict) -> list[tuple[str, dict]]:
    """Build a small, ordered cohort with one-feature and planned combined variants."""
    if strategy == "intraday_mean_reversion":
        return [
            ("opening_dip_er_gate", {"signal_model": "opening_dip", "regime_filter": "er"}),
            ("opening_dip_adx_gate", {"signal_model": "opening_dip", "regime_filter": "adx"}),
            ("opening_dip_combined_gate", {"signal_model": "opening_dip", "regime_filter": "combined"}),
            ("bollinger_baseline", {"signal_model": "bollinger"}),
            ("er_gate", {"signal_model": "bollinger", "regime_filter": "er"}),
            ("adx_gate", {"signal_model": "bollinger", "regime_filter": "adx"}),
            ("combined_gate", {"signal_model": "bollinger", "regime_filter": "combined"}),
            ("volatility_threshold", {
                "signal_model": "bollinger", "threshold_mode": "volatility_regime_adjusted",
            }),
            ("combined_regime_volatility", {
                "signal_model": "bollinger", "regime_filter": "combined",
                "threshold_mode": "volatility_regime_adjusted",
            }),
        ]
    if strategy == "swing_mean_reversion":
        return [
            ("er_gate", {"regime_filter": "er"}),
            ("adx_gate", {"regime_filter": "adx"}),
            ("combined_gate", {"regime_filter": "combined"}),
            ("standardized_distance", {"entry_distance_mode": "standardized"}),
            ("adaptive_lookback", {"lookback_mode": "volatility_adaptive"}),
            ("combined_volatility", {
                "entry_distance_mode": "standardized", "lookback_mode": "volatility_adaptive",
            }),
            ("combined_regime_volatility", {
                "regime_filter": "combined", "entry_distance_mode": "standardized",
                "lookback_mode": "volatility_adaptive",
            }),
        ]
    if strategy == "swing_trend_following":
        return [
            ("kama_average", {"average_type": "kama"}),
            ("atr_stop", {"stop_mode": "atr"}),
            ("kama_atr", {"average_type": "kama", "stop_mode": "atr"}),
        ]
    if strategy == "swing_momentum":
        return [
            ("cross_sectional_21_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [21],
            }),
            ("cross_sectional_63_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [63],
            }),
            ("cross_sectional_126_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [126],
            }),
            ("cross_sectional_252_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [252],
            }),
            ("cross_sectional_long_horizons", {
                "momentum_mode": "cross_sectional", "lookbacks": [21, 63, 126, 252],
            }),
            ("time_series_long_horizons", {
                "momentum_mode": "time_series", "lookbacks": [21, 63, 126, 252],
            }),
        ]
    if strategy == "swing_multi_factor":
        return [
            ("all_factors_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "none",
                "regime_factor_weights": {},
                "min_universe": 15,
                "factor_weight_mode": "static",
            }),
            ("ridge_factor_set_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector",
                "regime_factor_weights": {},
                "min_universe": 15,
                "factor_weight_mode": "static",
            }),
            ("ridge_walk_forward_weights", {
                "factor_weight_mode": "ridge_walk_forward",
                "regime_factor_weights": {},
                "factor_neutralization": "sector",
                "min_universe": 15,
            }),
            ("lightgbm_walk_forward_scores", {
                "factor_weight_mode": "lightgbm_walk_forward",
                "regime_factor_weights": {},
                "factor_neutralization": "sector",
                "min_universe": 15,
            }),
            ("xgboost_walk_forward_scores", {
                "factor_weight_mode": "xgboost_walk_forward",
                "regime_factor_weights": {},
                "factor_neutralization": "sector",
                "min_universe": 15,
            }),
            ("momentum_only", {"factor_weights": {
                "momentum_1m": 0.33, "momentum_3m": 0.34, "momentum_6m": 0.33,
            }, "regime_factor_weights": {}, "factor_weight_mode": "static"}),
            ("low_volatility_only", {"factor_weights": {"low_volatility": 1.0}, "regime_factor_weights": {}, "factor_weight_mode": "static"}),
            ("price_volume_only", {"factor_weights": {"price_volume_momentum": 1.0}, "regime_factor_weights": {}, "factor_weight_mode": "static"}),
            ("value_only", {"factor_weights": {
                "value_earnings_yield": 0.5, "value_book_to_price": 0.25,
                "value_fcf_yield": 0.25,
            }, "regime_factor_weights": {}, "min_universe": 15, "factor_weight_mode": "static"}),
            ("quality_only", {"factor_weights": {
                "quality_profitability": 0.5, "quality_balance_sheet": 0.5,
            }, "regime_factor_weights": {}, "min_universe": 15, "factor_weight_mode": "static"}),
            ("earnings_yield_only", {"factor_weights": {"value_earnings_yield": 1.0}, "regime_factor_weights": {}, "min_universe": 15, "factor_weight_mode": "static"}),
            ("book_to_price_only", {"factor_weights": {"value_book_to_price": 1.0}, "regime_factor_weights": {}, "min_universe": 15, "factor_weight_mode": "static"}),
            ("free_cash_flow_yield_only", {"factor_weights": {"value_fcf_yield": 1.0}, "regime_factor_weights": {}, "min_universe": 15, "factor_weight_mode": "static"}),
            ("profitability_only", {"factor_weights": {"quality_profitability": 1.0}, "regime_factor_weights": {}, "min_universe": 15, "factor_weight_mode": "static"}),
            ("balance_sheet_quality_only", {"factor_weights": {"quality_balance_sheet": 1.0}, "regime_factor_weights": {}, "min_universe": 15, "factor_weight_mode": "static"}),
            ("sector_neutralized_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector", "regime_factor_weights": {},
                "min_universe": 15, "factor_weight_mode": "static",
            }),
            ("regime_timing", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector",
                "regime_factor_weights": REGIME_FACTOR_WEIGHT_PROFILES,
                "min_universe": 15, "factor_weight_mode": "static",
            }),
            ("shrinkage_min_variance", {"portfolio_weight_method": "shrinkage_min_variance", "factor_weight_mode": "static"}),
            ("combined_shrinkage_min_variance", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector", "regime_factor_weights": {},
                "min_universe": 15,
                "factor_weight_mode": "static",
                "portfolio_weight_method": "shrinkage_min_variance",
            }),
        ]
    return []

def _random_values(space: dict[str, list], rng: random.Random) -> dict:
    return {key: deepcopy(rng.choice(values)) for key, values in space.items()}

def _flatten(value, prefix="") -> dict[str, object]:
    if isinstance(value, dict):
        output = {}
        for key, child in value.items():
            output.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return output
    if isinstance(value, (list, tuple)):
        return {prefix: list(value)}
    return {prefix: value}

def _distance(left: dict, right: dict, tuned_parameters: set[str] | None = None) -> float:
    first, second = _flatten(left), _flatten(right)
    keys = sorted(set(first) | set(second))
    if tuned_parameters is not None:
        keys = [key for key in keys if key.split(".", 1)[0] in tuned_parameters]
    if not keys:
        return 0.0
    distances = []
    for key in keys:
        a, b = first.get(key), second.get(key)
        if a == b:
            distances.append(0.0)
        elif isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            scale = max(abs(float(a)), abs(float(b)), 1e-12)
            distances.append(min(1.0, abs(float(a) - float(b)) / scale))
        else:
            distances.append(1.0)
    return statistics.mean(distances)

def _bayes_proposal(space: dict[str, list], history: list[dict], seen: set[str], rng: random.Random) -> dict:
    pool = []
    pool_keys = set()
    for _ in range(160):
        proposal = _random_values(space, rng)
        key = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
        if key not in seen and key not in pool_keys:
            pool.append(proposal)
            pool_keys.add(key)
    if not pool:
        return {}
    if len(history) < 4:
        return pool[0]

    def acquisition(candidate: dict) -> float:
        weights = [math.exp(-6.0 * _distance(candidate, row["parameters"], set(space))) for row in history]
        total = sum(weights)
        predicted = sum(weight * row["fitness"] for weight, row in zip(weights, history)) / total if total else 0.0
        uncertainty = 1.0 / math.sqrt(max(total, 1e-9))
        return predicted + 0.15 * uncertainty

    return max(pool, key=acquisition)

def _candidate_parameters(
    strategy: str, method: str, count: int, seed: int, space: dict[str, list],
    parent_parameters: dict | list[dict] | None = None,
    excluded_candidate_ids: set[str] | None = None,
) -> list[dict]:
    """Produce a bounded parameter bank, prioritizing variants unseen on these bars."""
    if count < 1 or count > 512:
        raise ValueError("candidate count must be between 1 and 512")
    baseline = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    excluded = set(excluded_candidate_ids or ())

    def previously_evaluated(parameters: dict) -> bool:
        return candidate_id(strategy, parameters) in excluded

    baseline = validate_parameter_profile({strategy: baseline})[strategy]
    output = [{
        "name": "baseline", "parameters": baseline,
        "model_family": model_family(strategy, baseline),
        "logic_version": logic_version(strategy),
    }]
    seen = {json.dumps(baseline, sort_keys=True, separators=(",", ":"))}
    rng = random.Random(seed)
    baseline_id = candidate_id(strategy, baseline)
    bayes_seed_limit = (
        max(1, count - max(2, count // 3))
        if method == "bayes" and count >= 4 else count
    )
    ablation_limit = (
        max(1, min(len(_planned_ablation_variants(strategy, baseline)), count // 3))
        if method == "bayes" else count
    )
    ablations_added = 0
    for name, changes in _planned_ablation_variants(strategy, baseline):
        if len(output) >= bayes_seed_limit or ablations_added >= ablation_limit:
            break
        variation = deepcopy(baseline)
        variation.update(deepcopy(changes))
        try:
            parameters = _make_parameters(strategy, variation)
        except ValueError:
            continue
        key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        if key in seen or previously_evaluated(parameters):
            continue
        seen.add(key)
        output.append({
            "name": f"ablation:{name}", "parameters": parameters,
            "model_family": model_family(strategy, parameters),
            "logic_version": logic_version(strategy),
            "parent_candidate_id": baseline_id,
        })
        ablations_added += 1
    if strategy == "swing_multi_factor" and len(output) < bayes_seed_limit:
        minimum_universe_limit = (
            max(1, count // 8) if method == "bayes" else count
        )
        minimum_universe_added = 0
        for min_universe in space.get("min_universe", []):
            variation = deepcopy(baseline)
            variation["min_universe"] = min_universe
            variation["factor_weight_mode"] = "static"
            try:
                parameters = _make_parameters(strategy, variation)
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen or previously_evaluated(parameters):
                continue
            seen.add(key)
            output.append({
                "name": f"min_universe_{min_universe}", "parameters": parameters,
            })
            minimum_universe_added += 1
            if len(output) >= bayes_seed_limit or minimum_universe_added >= minimum_universe_limit:
                break
    raw_parents = parent_parameters if isinstance(parent_parameters, list) else [parent_parameters]
    parents = []
    for raw in raw_parents:
        if not isinstance(raw, dict):
            continue
        try:
            parent = validate_parameter_profile({strategy: raw})[strategy]
        except ValueError:
            continue
        if parent not in parents:
            parents.append(parent)
    if parents and len(output) < bayes_seed_limit:
        parent_limit = max(1, count // 8) if method == "bayes" else max(1, count // 4)
        for index, parent in enumerate(parents[:parent_limit]):
            key = json.dumps(parent, sort_keys=True, separators=(",", ":"))
            if key not in seen and not previously_evaluated(parent):
                seen.add(key)
                output.append({
                    "name": f"archive_{index + 1:02d}", "parameters": parent,
                    "model_family": model_family(strategy, parent),
                    "logic_version": logic_version(strategy),
                })
        # Spend a stable fraction of the budget on neighboring variants from
        # several archived elites; reserve the rest for independent exploration.
        derivative_limit = (
            min(bayes_seed_limit, max(len(output), count // 3))
            if method == "bayes"
            else min(count, max(len(output), count * 2 // 3))
        )
        for parameter, values in space.items():
            for parent_index, parent in enumerate(parents):
                for value in values:
                    if len(output) >= derivative_limit:
                        break
                    if parent.get(parameter) == value:
                        continue
                    variation = deepcopy(parent)
                    variation[parameter] = deepcopy(value)
                    try:
                        parameters = _make_parameters(strategy, variation)
                    except ValueError:
                        continue
                    key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
                    if key in seen or previously_evaluated(parameters):
                        continue
                    seen.add(key)
                    output.append({
                        "name": f"archive_{parent_index + 1:02d}_variant_{len(output):03d}",
                        "parameters": parameters,
                        "model_family": model_family(strategy, parameters),
                        "logic_version": logic_version(strategy),
                        "parent_candidate_id": candidate_id(strategy, parent),
                    })
                if len(output) >= derivative_limit:
                    break
            if len(output) >= derivative_limit:
                break
    if method == "grid":
        combinations = itertools.product(*(space[key] for key in space))
        for row in combinations:
            values = dict(zip(space, row))
            try:
                parameters = _make_parameters(strategy, values)
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen or previously_evaluated(parameters):
                continue
            seen.add(key)
            output.append({"name": f"grid_{len(output):03d}", "parameters": parameters})
            if len(output) >= count:
                break
    elif method == "random":
        attempts = 0
        while len(output) < count and attempts < count * 40:
            attempts += 1
            try:
                parameters = _make_parameters(strategy, _random_values(space, rng))
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen or previously_evaluated(parameters):
                continue
            seen.add(key)
            output.append({"name": f"random_{len(output):03d}", "parameters": parameters})
    elif method != "bayes":
        raise ValueError("search method must be grid, random, or bayes")
    return output[:count]

__all__ = [
    "_bayes_proposal", "_candidate_parameters", "_distance", "_extend_space_from_archived_parents",
    "_flatten", "_make_parameters", "_planned_ablation_variants", "_random_values",
    "_refine_saturated_numeric_domains", "_search_space",
]
