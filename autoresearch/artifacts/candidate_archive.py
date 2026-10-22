"""Persistent, diverse strategy-candidate memory and regime scorecards."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import statistics

from .files import _atomic_json


ARCHIVE_SCHEMA = "swarm_algo_candidate_archive_v1"
MAX_CANDIDATES_PER_STRATEGY = 64
MAX_DATA_VERSIONS_PER_STRATEGY = 32
MAX_SEARCH_RUNS_PER_STRATEGY = 256

STRATEGY_LOGIC_VERSIONS = {
    "mean_reversion": "intraday-mr-2",
    "swing_mean_reversion": "swing-mr-2",
    "trend_following": "swing-trend-2",
    "swing_trend_following": "swing-trend-2",
    "momentum": "swing-momentum-2",
    "swing_momentum": "swing-momentum-2",
    "multi_factor": "multi-factor-rank-5-boosted-walk-forward",
}


def _archive_path(root: str | Path) -> Path:
    path = Path(root)
    if path.name != "candidate-archive.json":
        path = path / "candidate-archive.json"
    return path


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _read(path: Path) -> dict:
    if not path.exists():
        return {
            "schema": ARCHIVE_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_counters": {},
            "candidates": {},
            "search_runs": {},
        }
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read candidate archive: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or value.get("schema") != ARCHIVE_SCHEMA:
        raise ValueError("candidate archive has an unsupported schema")
    value.setdefault("run_counters", {})
    value.setdefault("candidates", {})
    value.setdefault("search_runs", {})
    if (
        not isinstance(value["run_counters"], dict)
        or not isinstance(value["candidates"], dict)
        or not isinstance(value["search_runs"], dict)
        or any(
            not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs)
            for runs in value["search_runs"].values()
        )
    ):
        raise ValueError("candidate archive counters or candidates are malformed")
    return value


def model_family(strategy: str, parameters: dict) -> str:
    """Stable family label used to group controlled research variants."""
    values = parameters if isinstance(parameters, dict) else {}
    if strategy == "mean_reversion":
        return ":".join((
            "intraday_mean_reversion", str(values.get("signal_model", "bollinger")),
            str(values.get("regime_filter", "none")), str(values.get("threshold_mode", "fixed")),
        ))
    if strategy == "swing_mean_reversion":
        return ":".join((
            "swing_mean_reversion", str(values.get("regime_filter", "none")),
            str(values.get("entry_distance_mode", "fixed_pct")),
            str(values.get("lookback_mode", "fixed")),
        ))
    if strategy in {"trend_following", "swing_trend_following"}:
        return f"swing_trend_following:{values.get('average_type', 'sma')}:{values.get('stop_mode', 'fixed_pct')}"
    if strategy in {"momentum", "swing_momentum"}:
        lookbacks = values.get("lookbacks") or (5, 10, 20)
        horizons = "-".join(str(value) for value in lookbacks)
        return f"swing_momentum:{values.get('momentum_mode', 'cross_sectional')}:{horizons}"
    if strategy == "multi_factor":
        weight_mode = str(values.get("factor_weight_mode", "static"))
        active_factors = "+".join(sorted(
            name for name, weight in (values.get("factor_weights") or {}).items()
            if isinstance(weight, (int, float)) and weight > 0
        )) or "none"
        if weight_mode in {
            "ridge_walk_forward", "lightgbm_walk_forward", "xgboost_walk_forward",
        }:
            active_factors = "research_model_factors"
        timing = "regime_timed" if values.get("regime_factor_weights") else "static"
        combination = "tree_regression_scores" if weight_mode in {
            "lightgbm_walk_forward", "xgboost_walk_forward",
        } else "linear_composite"
        return ":".join((
            "multi_factor", weight_mode, combination, active_factors,
            str(values.get("factor_neutralization", "none")), timing,
            str(values.get("portfolio_weight_method", "equal")),
        ))
    return strategy


def logic_version(strategy: str) -> str:
    return STRATEGY_LOGIC_VERSIONS.get(strategy, "1")


def candidate_id(
    strategy: str, parameters: dict, regime: str | None = None, *,
    family: str | None = None, version: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "strategy": strategy, "regime": regime,
            "model_family": family or model_family(strategy, parameters),
            "logic_version": version or logic_version(strategy),
            "parameters": parameters,
        },
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def reserve_search_seed(root: str | Path, strategy: str, pipeline_version: str) -> tuple[int, int]:
    """Advance a persistent sequence so discovery varies independently of quote dates."""
    path = _archive_path(root)
    with _locked(path):
        archive = _read(path)
        counters = archive.setdefault("run_counters", {})
        sequence = int(counters.get(strategy, 0)) + 1
        counters[strategy] = sequence
        digest = hashlib.sha256(f"{strategy}:{sequence}:{pipeline_version}".encode()).hexdigest()
        seed = int(digest[:8], 16)
        archive["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(path, archive)
    return seed, sequence


def evaluated_candidate_ids(
    root: str | Path, strategy: str, data_version: str, *, window_end: str | None = None,
) -> set[str]:
    """Return parameter sets already evaluated against this exact quote snapshot."""
    archive = _read(_archive_path(root))
    by_strategy = archive.get("evaluated_candidates_by_data_version") or {}
    by_version = by_strategy.get(strategy) or {}
    record = by_version.get(data_version)
    if record is None and window_end:
        # Migrate prior archives that recorded candidates but not their data fingerprint.
        rows = (archive.get("candidates") or {}).get(strategy) or {}
        return {
            row["candidate_id"]
            for row in rows.values()
            if isinstance(row, dict)
            and row.get("candidate_id")
            and (row.get("last_window") or {}).get("end") == window_end
        }
    record = record or {}
    if isinstance(record, dict):
        return set(record.get("candidate_ids") or [])
    return set(record or [])


def _parameter_distance(left: dict, right: dict) -> float:
    def flatten(value, prefix=""):
        if isinstance(value, dict):
            out = {}
            for key, child in value.items():
                out.update(flatten(child, f"{prefix}.{key}" if prefix else str(key)))
            return out
        return {prefix: value}

    a, b = flatten(left), flatten(right)
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    distances = []
    for key in keys:
        x, y = a.get(key), b.get(key)
        if x == y:
            distances.append(0.0)
        elif isinstance(x, (int, float)) and not isinstance(x, bool) and isinstance(y, (int, float)) and not isinstance(y, bool):
            distances.append(min(1.0, abs(float(x) - float(y)) / max(abs(float(x)), abs(float(y)), 1e-12)))
        else:
            distances.append(1.0)
    return sum(distances) / len(distances)


def archive_discoveries(root: str | Path, strategy: str, details: list[dict]) -> int:
    """Merge every evaluated candidate and its regime outcomes into the archive."""
    path = _archive_path(root)
    timestamp = datetime.now(timezone.utc).isoformat()
    count = 0
    with _locked(path):
        archive = _read(path)
        rows = archive.setdefault("candidates", {}).setdefault(strategy, {})
        search_runs = archive.setdefault("search_runs", {}).setdefault(strategy, [])
        evaluated = archive.setdefault("evaluated_candidates_by_data_version", {}).setdefault(strategy, {})
        for detail in details:
            window = detail.get("window") or {}
            data_version = (
                (detail.get("adaptive_window_search") or {}).get("data_version")
                or detail.get("data_version")
            )
            candidates = detail.get("candidates") or []
            baseline = next(
                (item for item in candidates if item.get("name") == "baseline"),
                next((item for item in candidates if str(item.get("name", "")).endswith(":baseline")), {}),
            )
            baseline_fitness = baseline.get("fitness")
            selected = detail.get("selected_candidate") or {}
            run_identity = "|".join((
                strategy, str(data_version or "unknown"),
                str(window.get("start") or ""), str(window.get("end") or ""),
                str(detail.get("archive_sequence") or detail.get("search_seed") or ""),
                str(detail.get("invocation_id") or detail.get("created_at") or ""),
            ))
            run_summary = {
                "run_key": hashlib.sha256(run_identity.encode("utf-8")).hexdigest(),
                "created_at": detail.get("created_at") or timestamp,
                "strategy": strategy,
                "invocation_id": detail.get("invocation_id"),
                "archive_sequence": detail.get("archive_sequence"),
                "search_seed": detail.get("search_seed"),
                "data_version": data_version,
                "window": window,
                "selection_status": detail.get("selection_status"),
                "selected_candidate_id": candidate_id(strategy, selected["parameters"])
                if isinstance(selected.get("parameters"), dict) else None,
                "selected_fitness": selected.get("fitness"),
                "baseline_fitness": baseline_fitness,
                "candidate_count_evaluated": detail.get("candidate_count_evaluated"),
                "candidate_count_requested": detail.get("candidate_count_requested"),
                "search_iterations": detail.get("search_iterations"),
                "adaptive_novelty": detail.get("adaptive_novelty"),
                "inherited_archive_search_values": detail.get("inherited_archive_search_values", {}),
                "saturation_aware_search": detail.get("saturation_aware_search", {}),
            }
            prior_run = next(
                (item for item in search_runs if item.get("run_key") == run_summary["run_key"]),
                None,
            )
            if prior_run is None:
                search_runs.append(run_summary)
            else:
                prior_run.update(run_summary)
            prior_record = (evaluated.get(data_version) or {}) if data_version else {}
            prior_ids = (
                prior_record.get("candidate_ids") or []
                if isinstance(prior_record, dict) else prior_record
            )
            seen_for_version = set(prior_ids)
            for item in candidates:
                parameters = item.get("parameters")
                if not isinstance(parameters, dict):
                    continue
                key = candidate_id(strategy, parameters)
                if data_version:
                    seen_for_version.add(key)
                existing = rows.get(key) or {
                    "candidate_id": key,
                    "strategy": strategy,
                    "parameters": parameters,
                    "first_seen": timestamp,
                    "discovery_observations": 0,
                    "verification_folds": [],
                }
                existing.update({
                    "last_seen": timestamp,
                    "last_window": window,
                    "fitness": item.get("fitness"),
                    "metrics": item.get("metrics"),
                    "eligible": bool(item.get("eligible")),
                    "regime_metrics": item.get("regime_metrics") or {},
                    "search_iteration": item.get("search_iteration"),
                    "model_family": item.get("model_family") or model_family(strategy, parameters),
                    "logic_version": item.get("logic_version") or logic_version(strategy),
                    "parent_candidate_id": item.get("parent_candidate_id") or existing.get("parent_candidate_id"),
                    "data_version": data_version,
                    "cost_model_version": item.get("cost_model_version"),
                    "minimum_trade_count_met": bool(item.get("minimum_trade_count_met")),
                    "positive_after_costs": bool(item.get("positive_after_costs")),
                })
                invocation_id = detail.get("invocation_id")
                if invocation_id:
                    existing["last_invocation_id"] = invocation_id
                    invocation_ids = existing.get("source_invocation_ids")
                    if not isinstance(invocation_ids, list):
                        invocation_ids = []
                        existing["source_invocation_ids"] = invocation_ids
                    if invocation_id not in invocation_ids:
                        invocation_ids.append(invocation_id)
                        del invocation_ids[:-32]
                existing["discovery_observations"] = int(existing.get("discovery_observations", 0)) + 1
                history = existing.setdefault("evaluation_history", [])
                if not isinstance(history, list):
                    history = []
                    existing["evaluation_history"] = history
                evaluation_key = "|".join((
                    str(data_version or "unknown"),
                    str(window.get("start") or ""),
                    str(window.get("end") or ""),
                    str(item.get("cost_model_version") or ""),
                    str(detail.get("archive_sequence") or detail.get("search_seed") or ""),
                ))
                fitness = item.get("fitness")
                fitness_delta = None
                if (
                    isinstance(fitness, (int, float))
                    and isinstance(baseline_fitness, (int, float))
                ):
                    fitness_delta = float(fitness) - float(baseline_fitness)
                observation = {
                    "evaluation_key": evaluation_key,
                    "archive_sequence": detail.get("archive_sequence"),
                    "search_seed": detail.get("search_seed"),
                    "invocation_id": invocation_id,
                    "data_version": data_version,
                    "window": window,
                    "fitness": fitness,
                    "baseline_fitness": baseline_fitness,
                    "fitness_delta": fitness_delta,
                    "metrics": item.get("metrics"),
                    "minimum_trade_count_met": bool(item.get("minimum_trade_count_met")),
                    "eligible": bool(item.get("eligible")),
                    "positive_after_costs": bool(item.get("positive_after_costs")),
                    "logic_version": item.get("logic_version") or logic_version(strategy),
                    "cost_model_version": item.get("cost_model_version"),
                    "last_seen": timestamp,
                }
                prior_observation = next(
                    (row for row in history if row.get("evaluation_key") == evaluation_key),
                    None,
                )
                if prior_observation is None:
                    history.append(observation)
                else:
                    prior_observation.update(observation)
                history.sort(key=lambda row: str(row.get("last_seen") or ""), reverse=True)
                del history[32:]
                rows[key] = existing
                count += 1
            if data_version:
                evaluated[data_version] = {
                    "updated_at": timestamp,
                    "candidate_ids": sorted(seen_for_version),
                }

        search_runs.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        del search_runs[MAX_SEARCH_RUNS_PER_STRATEGY:]

        if len(evaluated) > MAX_DATA_VERSIONS_PER_STRATEGY:
            retained_versions = {
                version for version, record in sorted(
                    evaluated.items(),
                    key=lambda item: str((item[1] or {}).get("updated_at") or "")
                    if isinstance(item[1], dict) else "",
                    reverse=True,
                )[:MAX_DATA_VERSIONS_PER_STRATEGY]
            }
            for old_version in list(evaluated):
                if old_version not in retained_versions:
                    del evaluated[old_version]

        if len(rows) > MAX_CANDIDATES_PER_STRATEGY:
            ranked = sorted(
                rows.values(), key=_candidate_rank, reverse=True,
            )
            retained = []
            for row in ranked:
                parameters = row.get("parameters") or {}
                if any(_parameter_distance(parameters, old.get("parameters") or {}) < 0.08 for old in retained):
                    continue
                retained.append(row)
                if len(retained) >= MAX_CANDIDATES_PER_STRATEGY:
                    break
            rows.clear()
            rows.update({row["candidate_id"]: row for row in retained})
        archive["updated_at"] = timestamp
        _atomic_json(path, archive)
    return count


def _candidate_rank(row: dict) -> tuple[float, float, float, int]:
    exact_folds = row.get("candidate_validation_folds") or []
    if exact_folds:
        latest_by_window = {}
        for item in exact_folds:
            if not isinstance(item, dict) or not isinstance(item.get("fitness"), (int, float)):
                continue
            key = (item.get("test_start"), item.get("test_end"))
            prior = latest_by_window.get(key)
            if prior is None or str(item.get("evaluated_at") or "") > str(prior.get("evaluated_at") or ""):
                latest_by_window[key] = item
        folds = list(latest_by_window.values())
    else:
        folds = row.get("verification_folds") or []
    oos = [
        float(item["fitness"])
        for item in folds
        if isinstance(item, dict)
        and isinstance(item.get("fitness"), (int, float))
        and math.isfinite(float(item["fitness"]))
    ]
    history = row.get("evaluation_history") or []
    sampled = [
        item for item in history
        if isinstance(item, dict) and item.get("minimum_trade_count_met")
    ]
    deltas = [
        float(item["fitness_delta"])
        for item in sampled
        if isinstance(item.get("fitness_delta"), (int, float))
        and math.isfinite(float(item["fitness_delta"]))
    ]
    if not sampled and isinstance(row.get("fitness"), (int, float)):
        deltas = [float(row["fitness"])]
    discovery_score = statistics.median(deltas) if deltas else -math.inf
    oos_score = statistics.median(oos) if oos else -math.inf
    positive_oos = sum(value > 0 for value in oos)
    positive_discovery = sum(value > 0 for value in deltas)
    # Rank by lower-quartile OOS fitness when available; otherwise use repeatable
    # improvement over each run's baseline, even if absolute return is negative.
    ranked_oos = sorted(oos)
    lower_quartile = ranked_oos[int((len(ranked_oos) - 1) * 0.25)] if ranked_oos else -math.inf
    return (
        lower_quartile if oos else discovery_score, float(bool(oos)),
        oos_score if oos else discovery_score,
        positive_oos if oos else positive_discovery,
    )


def candidate_validation_candidates(
    root: str | Path, strategy: str, *, limit: int = 3,
    include_parameters: list[dict] | None = None,
    invocation_id: str | None = None,
) -> list[dict]:
    """Return a small, diverse archive cohort for exact rolling OOS checks."""
    if limit < 1:
        return []
    archive = _read(_archive_path(root))
    rows = list((archive.get("candidates") or {}).get(strategy, {}).values())
    rows = [
        row for row in rows
        if isinstance(row.get("parameters"), dict)
        and row.get("logic_version") in (None, logic_version(strategy))
        and (
            row.get("eligible")
            or any(
                isinstance(item, dict)
                and item.get("eligible")
                and item.get("minimum_trade_count_met")
                for item in row.get("evaluation_history") or []
            )
            or any(
                isinstance(item, dict) and item.get("eligible")
                for item in (row.get("regime_metrics") or {}).values()
            )
        )
    ]
    by_id = {row.get("candidate_id"): row for row in rows if row.get("candidate_id")}
    ordered = []
    for parameters in include_parameters or []:
        if not isinstance(parameters, dict):
            continue
        row = by_id.get(candidate_id(strategy, parameters))
        if row is None:
            row = {
                "candidate_id": candidate_id(strategy, parameters),
                "strategy": strategy,
                "parameters": parameters,
                "eligible": True,
                "model_family": model_family(strategy, parameters),
                "logic_version": logic_version(strategy),
                "last_invocation_id": invocation_id,
                "source_invocation_ids": [invocation_id] if invocation_id else [],
                "candidate_validation_folds": [],
                "archive_artifact": str(_archive_path(root).resolve()),
            }
        if row not in ordered:
            ordered.append(row)
    ranked = sorted(
        rows,
        key=lambda row: (
            *_candidate_rank(row), str(row.get("last_seen") or ""),
            str(row.get("candidate_id") or ""),
        ),
        reverse=True,
    )
    selected = []
    for row in [*ordered, *ranked]:
        parameters = row.get("parameters") or {}
        if any(
            item.get("candidate_id") == row.get("candidate_id")
            or _parameter_distance(parameters, item.get("parameters") or {}) < 0.08
            for item in selected
        ):
            continue
        selected.append({
            "candidate_id": row["candidate_id"],
            "parameters": parameters,
            "eligible": bool(row.get("eligible")),
            "model_family": row.get("model_family") or model_family(strategy, parameters),
            "logic_version": row.get("logic_version") or logic_version(strategy),
            "cost_model_version": row.get("cost_model_version"),
            "parent_candidate_id": row.get("parent_candidate_id"),
            "last_invocation_id": row.get("last_invocation_id"),
            "source_invocation_ids": row.get("source_invocation_ids") or [],
            "last_window": row.get("last_window") or {},
            "regime_metrics": row.get("regime_metrics") or {},
            "candidate_validation_folds": row.get("candidate_validation_folds") or [],
            "last_seen": row.get("last_seen"),
            "archive_rank": _candidate_rank(row),
            "archive_artifact": str(_archive_path(root).resolve()),
        })
        if len(selected) >= limit:
            break
    return selected


def record_candidate_validation_folds(
    root: str | Path, strategy: str, records: list[dict],
) -> int:
    """Persist exact-parameter OOS folds, deduplicated by data and fold version."""
    path = _archive_path(root)
    added = 0
    with _locked(path):
        archive = _read(path)
        rows = archive.setdefault("candidates", {}).setdefault(strategy, {})
        timestamp = datetime.now(timezone.utc).isoformat()
        for record in records:
            parameters = record.get("parameters")
            if not isinstance(parameters, dict):
                continue
            key = candidate_id(strategy, parameters)
            row = rows.get(key) or {
                "candidate_id": key, "strategy": strategy,
                "parameters": parameters, "first_seen": timestamp,
                "discovery_observations": 0, "regime_metrics": {},
                "verification_folds": [],
            }
            for name in (
                "model_family", "logic_version", "cost_model_version",
                "parent_candidate_id", "last_invocation_id",
            ):
                if record.get(name) is not None:
                    row[name] = record[name]
            invocation_id = record.get("last_invocation_id")
            if invocation_id:
                invocation_ids = row.get("source_invocation_ids")
                if not isinstance(invocation_ids, list):
                    invocation_ids = []
                    row["source_invocation_ids"] = invocation_ids
                if invocation_id not in invocation_ids:
                    invocation_ids.append(invocation_id)
                    del invocation_ids[:-32]
            folds = row.get("candidate_validation_folds")
            if not isinstance(folds, list):
                folds = []
                row["candidate_validation_folds"] = folds
            fold = {
                **{name: record.get(name) for name in (
                    "validation_version", "data_version", "train_start", "train_end",
                    "test_start", "test_end", "fitness", "metrics", "status",
                    "validation_cost_model_version", "validation_context_sha256",
                    "logic_version",
                )},
                "evaluated_at": timestamp,
            }
            dedupe_key = (
                fold.get("validation_version"), fold.get("data_version"),
                fold.get("validation_cost_model_version"), fold.get("logic_version"),
                fold.get("validation_context_sha256"),
                fold.get("test_start"), fold.get("test_end"),
            )
            prior_index = next((index for index, item in enumerate(folds) if (
                item.get("validation_version"), item.get("data_version"),
                item.get("validation_cost_model_version"), item.get("logic_version"),
                item.get("validation_context_sha256"),
                item.get("test_start"), item.get("test_end"),
            ) == dedupe_key), None)
            if prior_index is None:
                folds.append(fold)
                added += 1
            else:
                folds[prior_index] = fold
            folds.sort(key=lambda item: (str(item.get("test_start") or ""), str(item.get("evaluated_at") or "")))
            del folds[:-32]
            row["last_candidate_validation_at"] = timestamp
            rows[key] = row
        if len(rows) > MAX_CANDIDATES_PER_STRATEGY:
            ranked = sorted(rows.values(), key=_candidate_rank, reverse=True)
            retained = []
            for row in ranked:
                parameters = row.get("parameters") or {}
                if any(
                    _parameter_distance(parameters, old.get("parameters") or {}) < 0.08
                    for old in retained
                ):
                    continue
                retained.append(row)
                if len(retained) >= MAX_CANDIDATES_PER_STRATEGY:
                    break
            rows.clear()
            rows.update({row["candidate_id"]: row for row in retained})
        archive["updated_at"] = timestamp
        _atomic_json(path, archive)
    return added


def record_verification(root: str | Path, strategy: str, detail: dict) -> int:
    """Attach strictly chronological OOS evidence to the exact parameter sets."""
    path = _archive_path(root)
    added = 0
    with _locked(path):
        archive = _read(path)
        rows = archive.setdefault("candidates", {}).setdefault(strategy, {})
        for fold in detail.get("folds") or []:
            parameters = fold.get("train_parameters")
            metrics = fold.get("oos_metrics") or {}
            if not isinstance(parameters, dict) or not metrics:
                continue
            key = candidate_id(strategy, parameters)
            row = rows.get(key)
            if row is None:
                row = {
                    "candidate_id": key, "strategy": strategy,
                    "parameters": parameters, "first_seen": datetime.now(timezone.utc).isoformat(),
                    "discovery_observations": 0, "regime_metrics": {},
                    "verification_folds": [],
                }
            folds = row.setdefault("verification_folds", [])
            fold_record = {
                "train_end": fold.get("train_end"),
                "test_start": fold.get("test_start"),
                "test_end": fold.get("test_end"),
                "fitness": fold.get("oos_fitness"),
                "metrics": metrics,
            }
            if not any(
                item.get("test_start") == fold_record["test_start"]
                and item.get("test_end") == fold_record["test_end"]
                for item in folds
            ):
                folds.append(fold_record)
                folds.sort(key=lambda item: str(item.get("test_start") or ""))
                added += 1
            row["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            rows[key] = row
        archive["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(path, archive)
    return added


def parent_candidates(
    root: str | Path, strategy: str, *, limit: int = 6,
    before: str | None = None,
) -> tuple[list[dict], dict]:
    """Choose several high-quality, meaningfully different archive members."""
    path = _archive_path(root)
    archive = _read(path)
    rows = list((archive.get("candidates") or {}).get(strategy, {}).values())
    rows = [
        row for row in rows
        if isinstance(row.get("parameters"), dict)
        and row.get("logic_version") in (None, logic_version(strategy))
        and (before is None or str((row.get("last_window") or {}).get("end") or "") < before)
        and (
            bool(row.get("evaluation_history"))
            or bool(row.get("verification_folds"))
            or isinstance(row.get("fitness"), (int, float))
        )
    ]
    rows.sort(key=_candidate_rank, reverse=True)
    selected = []
    for row in rows:
        if any(_parameter_distance(row["parameters"], parent["parameters"]) < 0.08 for parent in selected):
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return [row["parameters"] for row in selected], {
        "used": bool(selected),
        "archive": str(path.resolve()),
        "candidate_ids": [row["candidate_id"] for row in selected],
        "count": len(selected),
    }


def next_regime_candidates(root: str | Path, strategy: str) -> list[dict]:
    """Return the strongest sample-qualified candidate for each known regime."""
    path = _archive_path(root)
    archive = _read(path)
    rows = list((archive.get("candidates") or {}).get(strategy, {}).values())
    regime_best: dict[str, dict] = {}
    for row in rows:
        for regime, result in (row.get("regime_metrics") or {}).items():
            if not isinstance(result, dict) or not result.get("eligible"):
                continue
            score = float(result.get("fitness") or -math.inf)
            current = regime_best.get(regime)
            if current is None or score > float(current.get("fitness") or -math.inf):
                regime_best[regime] = {
                    "regime": regime,
                    "candidate_id": candidate_id(strategy, row["parameters"], regime),
                    "parameters": row["parameters"],
                    "fitness": score,
                    "metrics": result.get("metrics"),
                    "source_candidate_id": row.get("candidate_id"),
                }
    return [regime_best[key] for key in sorted(regime_best)]
