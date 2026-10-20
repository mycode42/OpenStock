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

def _make_parameters(strategy: str, values: dict) -> dict:
    parameters = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    parameters.update(deepcopy(values))
    return validate_parameter_profile({strategy: parameters})[strategy]

def _planned_ablation_variants(strategy: str, baseline: dict) -> list[tuple[str, dict]]:
    """Build a small, ordered cohort with one-feature and planned combined variants."""
    if strategy == "mean_reversion":
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
    if strategy in {"trend_following", "swing_trend_following"}:
        return [
            ("kama_average", {"average_type": "kama"}),
            ("atr_stop", {"stop_mode": "atr"}),
            ("kama_atr", {"average_type": "kama", "stop_mode": "atr"}),
        ]
    if strategy in {"momentum", "swing_momentum"}:
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
    if strategy == "multi_factor":
        return [
            ("all_factors_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "none",
                "regime_factor_weights": {},
                "min_universe": 15,
            }),
            ("ridge_factor_set_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector",
                "regime_factor_weights": {},
                "min_universe": 15,
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
            }, "regime_factor_weights": {}}),
            ("low_volatility_only", {"factor_weights": {"low_volatility": 1.0}, "regime_factor_weights": {}}),
            ("price_volume_only", {"factor_weights": {"price_volume_momentum": 1.0}, "regime_factor_weights": {}}),
            ("value_only", {"factor_weights": {
                "value_earnings_yield": 0.5, "value_book_to_price": 0.25,
                "value_fcf_yield": 0.25,
            }, "regime_factor_weights": {}, "min_universe": 15}),
            ("quality_only", {"factor_weights": {
                "quality_profitability": 0.5, "quality_balance_sheet": 0.5,
            }, "regime_factor_weights": {}, "min_universe": 15}),
            ("earnings_yield_only", {"factor_weights": {"value_earnings_yield": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("book_to_price_only", {"factor_weights": {"value_book_to_price": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("free_cash_flow_yield_only", {"factor_weights": {"value_fcf_yield": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("profitability_only", {"factor_weights": {"quality_profitability": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("balance_sheet_quality_only", {"factor_weights": {"quality_balance_sheet": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("sector_neutralized_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector", "regime_factor_weights": {},
                "min_universe": 15,
            }),
            ("regime_timing", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector",
                "regime_factor_weights": REGIME_FACTOR_WEIGHT_PROFILES,
                "min_universe": 15,
            }),
            ("shrinkage_min_variance", {"portfolio_weight_method": "shrinkage_min_variance"}),
            ("combined_shrinkage_min_variance", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector", "regime_factor_weights": {},
                "min_universe": 15,
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
    for name, changes in _planned_ablation_variants(strategy, baseline):
        if len(output) >= count:
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
    if strategy == "multi_factor" and len(output) < count:
        for min_universe in space.get("min_universe", []):
            variation = deepcopy(baseline)
            variation["min_universe"] = min_universe
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
            if len(output) >= count:
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
    if parents and len(output) < count:
        for index, parent in enumerate(parents[:max(1, count // 4)]):
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
        derivative_limit = min(count, max(len(output), count * 2 // 3))
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
    "_bayes_proposal", "_candidate_parameters", "_distance", "_flatten",
    "_make_parameters", "_planned_ablation_variants", "_random_values",
    "_search_space",
]
