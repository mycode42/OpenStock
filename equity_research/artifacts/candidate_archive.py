"""Persistent, diverse strategy-candidate memory and regime scorecards."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import fcntl
import hashlib
import json
import math
from pathlib import Path
import statistics
from urllib.parse import quote

from strategies.catalog import STRATEGY_ID_ALIASES, canonical_strategy_id

ARCHIVE_SCHEMA = "swarm_algo_candidate_archive_v1"
MAX_CANDIDATES_PER_STRATEGY = 64
MAX_DATA_VERSIONS_PER_STRATEGY = 32
MAX_SEARCH_RUNS_PER_STRATEGY = 256

STRATEGY_LOGIC_VERSIONS = {
    "intraday_mean_reversion": "intraday-mr-2",
    "swing_mean_reversion": "swing-mr-2",
    "swing_trend_following": "swing-trend-2",
    "swing_momentum": "swing-momentum-2",
    "swing_multi_factor": "multi-factor-rank-5-boosted-walk-forward",
}


def _archive_path(root: str | Path) -> Path:
    """Return the stable identity path used for the SQLite archive document."""
    path = Path(root)
    if path.name != "candidate-archive.json":
        path = path / "candidate-archive.json"
    return path


def _database_path(path: Path) -> Path:
    if path.parent.name == "archive" and path.parent.parent.name == "adaptive":
        output_root = path.parent.parent.parent
    else:
        output_root = path.parent
    return output_root / "research.sqlite3"


@lru_cache(maxsize=8)
def _ledger_for_database(path: str):
    from .research_ledger import ResearchLedger

    return ResearchLedger(path)


def archive_reference(root: str | Path) -> str:
    """Return a stable reference to the candidate archive document in SQLite."""
    path = _archive_path(root).resolve()
    database = _database_path(path).resolve()
    identity = quote(str(path), safe="")
    return f"sqlite://{database}#research_document/candidate_archive/{identity}"


def _write_archive(path: Path, archive: dict) -> None:
    """Persist candidate state in SQLite; no JSON working copy is written."""
    _ledger_for_database(str(_database_path(path).resolve())).record_document(
        kind="candidate_archive", identity=str(path.resolve()), payload=archive,
    )


def _canonicalize_strategy_maps(value: dict) -> None:
    """Move old strategy namespaces into their renamed strategy entries."""
    for section in (
        "run_counters", "candidates", "search_runs", "champions",
        "validation_queue", "evaluated_candidates_by_data_version",
    ):
        mapping = value.get(section)
        if not isinstance(mapping, dict):
            continue
        for legacy, canonical in STRATEGY_ID_ALIASES.items():
            if legacy not in mapping:
                continue
            old = mapping.pop(legacy)
            current = mapping.get(canonical)
            if section == "run_counters":
                mapping[canonical] = max(int(old or 0), int(current or 0))
            elif isinstance(old, list) and isinstance(current, list):
                mapping[canonical] = old + current
            elif isinstance(old, dict) and isinstance(current, dict):
                mapping[canonical] = {**old, **current}
            elif current is None:
                mapping[canonical] = old
        if section in {"candidates", "champions", "validation_queue", "search_runs"}:
            for rows in mapping.values():
                records = rows.values() if isinstance(rows, dict) else rows if isinstance(rows, list) else ()
                for row in records:
                    if isinstance(row, dict) and isinstance(row.get("strategy"), str):
                        row["strategy"] = canonical_strategy_id(row["strategy"])


@contextmanager
def _locked(path: Path):
    database = _database_path(path)
    lock_path = database.with_suffix(database.suffix + ".candidate-archive.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _read(path: Path) -> dict:
    ledger = _ledger_for_database(str(_database_path(path).resolve()))
    value = ledger.read_document(
        kind="candidate_archive", identity=str(path.resolve()),
    )
    if value is None and path.is_file():
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot safely read legacy candidate archive: {type(exc).__name__}") from exc
        if not isinstance(value, dict):
            raise ValueError("legacy candidate archive has an unsupported schema")
    if value is None:
        value = {
            "schema": ARCHIVE_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_counters": {},
            "candidates": {},
            "search_runs": {},
            "champions": {},
            "validation_queue": {},
        }
    if not isinstance(value, dict) or value.get("schema") != ARCHIVE_SCHEMA:
        raise ValueError("SQLite candidate archive has an unsupported schema")
    value.setdefault("run_counters", {})
    value.setdefault("candidates", {})
    value.setdefault("search_runs", {})
    value.setdefault("champions", {})
    value.setdefault("validation_queue", {})
    _canonicalize_strategy_maps(value)
    if (
        not isinstance(value["run_counters"], dict)
        or not isinstance(value["candidates"], dict)
        or not isinstance(value["search_runs"], dict)
        or not isinstance(value["champions"], dict)
        or not isinstance(value["validation_queue"], dict)
        or any(
            not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs)
            for runs in value["search_runs"].values()
        )
        or any(
            not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            for rows in value["validation_queue"].values()
        )
    ):
        raise ValueError("candidate archive counters or candidates are malformed")
    return value


def model_family(strategy: str, parameters: dict) -> str:
    """Stable family label used to group controlled research variants."""
    strategy = canonical_strategy_id(strategy)
    values = parameters if isinstance(parameters, dict) else {}
    if strategy == "intraday_mean_reversion":
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
    if strategy == "swing_trend_following":
        return f"swing_trend_following:{values.get('average_type', 'sma')}:{values.get('stop_mode', 'fixed_pct')}"
    if strategy == "swing_momentum":
        lookbacks = values.get("lookbacks") or (5, 10, 20)
        horizons = "-".join(str(value) for value in lookbacks)
        return f"swing_momentum:{values.get('momentum_mode', 'cross_sectional')}:{horizons}"
    if strategy == "swing_multi_factor":
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
            "swing_multi_factor", weight_mode, combination, active_factors,
            str(values.get("factor_neutralization", "none")), timing,
            str(values.get("portfolio_weight_method", "equal")),
        ))
    return strategy


def logic_version(strategy: str) -> str:
    return STRATEGY_LOGIC_VERSIONS.get(canonical_strategy_id(strategy), "1")


def candidate_id(
    strategy: str, parameters: dict, regime: str | None = None, *,
    family: str | None = None, version: str | None = None,
    regime_profiles: dict | None = None,
) -> str:
    strategy = canonical_strategy_id(strategy)
    payload = json.dumps(
        {
            "strategy": strategy, "regime": regime,
            "model_family": family or model_family(strategy, parameters),
            "logic_version": version or logic_version(strategy),
            "parameters": parameters,
            **({"regime_profiles": regime_profiles} if regime_profiles else {}),
        },
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def reserve_search_seed(root: str | Path, strategy: str, pipeline_version: str) -> tuple[int, int]:
    """Advance a persistent sequence so discovery varies independently of quote dates."""
    strategy = canonical_strategy_id(strategy)
    path = _archive_path(root)
    with _locked(path):
        archive = _read(path)
        counters = archive.setdefault("run_counters", {})
        sequence = int(counters.get(strategy, 0)) + 1
        counters[strategy] = sequence
        digest = hashlib.sha256(f"{strategy}:{sequence}:{pipeline_version}".encode()).hexdigest()
        seed = int(digest[:8], 16)
        archive["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_archive(path, archive)
    return seed, sequence


def evaluated_candidate_ids(
    root: str | Path, strategy: str, data_version: str, *, window_end: str | None = None,
) -> set[str]:
    """Return parameter sets already evaluated against this exact quote snapshot."""
    strategy = canonical_strategy_id(strategy)
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
    strategy = canonical_strategy_id(strategy)
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
            baseline_regimes = baseline.get("regime_metrics") or {}
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
                candidate_name = str(item.get("name") or "candidate")
                planned_ablation = candidate_name.startswith("ablation:") or ":ablation:" in candidate_name
                regime_metrics = {}
                for regime, result in (item.get("regime_metrics") or {}).items():
                    if not isinstance(result, dict):
                        continue
                    baseline_result = baseline_regimes.get(regime) or {}
                    baseline_sample_met = bool(baseline_result.get("minimum_sample_met"))
                    regime_fitness = float(result.get("fitness") or 0.0)
                    baseline_regime_fitness = float(baseline_result.get("fitness") or 0.0)
                    regime_metrics[regime] = {
                        **result,
                        "baseline_fitness": baseline_regime_fitness,
                        "improved_over_baseline": bool(
                            baseline_sample_met and regime_fitness > baseline_regime_fitness + 1e-9
                        ),
                        "planned_ablation": planned_ablation,
                    }
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
                    "regime_metrics": regime_metrics,
                    "candidate_name": candidate_name,
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
                baseline_value = float(baseline_fitness) if isinstance(baseline_fitness, (int, float)) else None
                candidate_value = float(fitness) if isinstance(fitness, (int, float)) else None
                if (
                    item.get("name") != "baseline"
                    and item.get("eligible")
                    and item.get("minimum_trade_count_met")
                    and item.get("positive_after_costs")
                    and baseline_value is not None
                    and candidate_value is not None
                    and math.isfinite(baseline_value)
                    and math.isfinite(candidate_value)
                    and candidate_value > baseline_value + 1e-4
                ):
                    queue = archive.setdefault("validation_queue", {}).setdefault(strategy, [])
                    queued = next((entry for entry in queue if entry.get("candidate_id") == key), None)
                    if queued is None:
                        queue.append({
                            "candidate_id": key,
                            "strategy": strategy,
                            "parameters": parameters,
                            "regime_profiles": {},
                            "model_family": existing.get("model_family"),
                            "logic_version": existing.get("logic_version"),
                            "cost_model_version": existing.get("cost_model_version"),
                            "parent_candidate_id": existing.get("parent_candidate_id"),
                            "source_invocation_id": invocation_id,
                            "source_invocation_ids": list(existing.get("source_invocation_ids") or []),
                            "discovery_fitness": candidate_value,
                            "baseline_discovery_fitness": baseline_value,
                            "discovery_delta": candidate_value - baseline_value,
                            "discovery_metrics": item.get("metrics") or {},
                            "regime_metrics": regime_metrics,
                            "data_version": data_version,
                            "window": window,
                            "status": "queued",
                            "queued_at": timestamp,
                            "last_attempt_data_version": None,
                            "discovery_history": [{
                                "data_version": data_version,
                                "window": window,
                                "fitness": candidate_value,
                                "baseline_fitness": baseline_value,
                                "fitness_delta": candidate_value - baseline_value,
                                "invocation_id": invocation_id,
                                "observed_at": timestamp,
                            }],
                        })
                    else:
                        discovery_history = queued.setdefault("discovery_history", [])
                        observation = {
                            "data_version": data_version,
                            "window": window,
                            "fitness": candidate_value,
                            "baseline_fitness": baseline_value,
                            "fitness_delta": candidate_value - baseline_value,
                            "invocation_id": invocation_id,
                            "observed_at": timestamp,
                        }
                        history_key = (data_version, window.get("start"), window.get("end"))
                        prior_observation = next((entry for entry in discovery_history if (
                            entry.get("data_version"),
                            (entry.get("window") or {}).get("start"),
                            (entry.get("window") or {}).get("end"),
                        ) == history_key), None)
                        if prior_observation is None:
                            discovery_history.append(observation)
                        else:
                            prior_observation.update(observation)
                        del discovery_history[:-32]
                        if candidate_value - baseline_value >= float(queued.get("discovery_delta") or -math.inf):
                            queued.update({
                                "discovery_fitness": candidate_value,
                                "baseline_discovery_fitness": baseline_value,
                                "discovery_delta": candidate_value - baseline_value,
                                "discovery_metrics": item.get("metrics") or {},
                                "regime_metrics": regime_metrics,
                                "data_version": data_version,
                                "window": window,
                            })
                        queued["last_seen_eligible_at"] = timestamp
                        if invocation_id:
                            queued["source_invocation_id"] = invocation_id
                            source_ids = queued.setdefault("source_invocation_ids", [])
                            if invocation_id not in source_ids:
                                source_ids.append(invocation_id)
                                del source_ids[:-32]
                        if queued.get("status") in {"failed", "insufficient_sample"} and (
                            queued.get("validation_data_version") != data_version
                        ):
                            queued["status"] = "queued"
                            queued["requeued_at"] = timestamp
                    queue.sort(key=lambda entry: (
                        -float(entry.get("discovery_delta") or 0.0),
                        str(entry.get("queued_at") or ""),
                        str(entry.get("candidate_id") or ""),
                    ))
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
        _write_archive(path, archive)
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
    strategy = canonical_strategy_id(strategy)
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
                "archive_artifact": archive_reference(root),
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
            "archive_artifact": archive_reference(root),
        })
        if len(selected) >= limit:
            break
    return selected


def archived_candidate(root: str | Path, strategy: str, identifier: str) -> dict | None:
    """Return the full persistent record for one exact candidate identity."""
    strategy = canonical_strategy_id(strategy)
    archive = _read(_archive_path(root))
    row = ((archive.get("candidates") or {}).get(strategy) or {}).get(identifier)
    return dict(row) if isinstance(row, dict) else None


def pending_validation_candidates(
    root: str | Path, strategy: str, *, limit: int = 2, data_version: str | None = None,
) -> list[dict]:
    """Return due queued challengers, strongest discovery improvement first."""
    strategy = canonical_strategy_id(strategy)
    if limit < 1:
        return []
    archive = _read(_archive_path(root))
    queue = (archive.get("validation_queue") or {}).get(strategy) or []
    eligible = [
        row for row in queue
        if row.get("status") == "queued"
        and (data_version is None or row.get("last_attempt_data_version") != data_version)
        and row.get("logic_version") in (None, logic_version(strategy))
        and isinstance(row.get("parameters"), dict)
    ]
    eligible.sort(key=lambda row: (
        -float(row.get("discovery_delta") or 0.0),
        str(row.get("queued_at") or ""),
        str(row.get("candidate_id") or ""),
    ))
    return [dict(row) for row in eligible[:limit]]


def record_validation_queue_result(
    root: str | Path, strategy: str, identifier: str, *, status: str,
    summary: dict, data_version: str, validation_end: str | None = None,
) -> None:
    """Persist challenger gate outcomes while leaving insufficient samples queued."""
    strategy = canonical_strategy_id(strategy)
    if status not in {"passed", "failed", "insufficient_sample", "validation_error"}:
        raise ValueError(f"unsupported validation queue status: {status}")
    path = _archive_path(root)
    timestamp = datetime.now(timezone.utc).isoformat()
    with _locked(path):
        archive = _read(path)
        queue = archive.setdefault("validation_queue", {}).setdefault(strategy, [])
        row = next((entry for entry in queue if entry.get("candidate_id") == identifier), None)
        if row is None:
            return
        history = row.setdefault("validation_history", [])
        history.append({
            "status": status,
            "data_version": data_version,
            "validation_end": validation_end,
            "validated_at": timestamp,
            "summary": summary,
        })
        del history[:-16]
        row.update({
            "status": "queued" if status in {"insufficient_sample", "validation_error"} else status,
            "last_validation_status": status,
            "last_attempt_data_version": data_version,
            "validation_data_version": data_version,
            "validation_end": validation_end,
            "last_validation_at": timestamp,
            "validation_summary": summary,
        })
        archive["updated_at"] = timestamp
        _write_archive(path, archive)


def champion_candidate(root: str | Path, strategy: str) -> dict | None:
    """Return the strategy's durable research champion, if one has passed gates."""
    strategy = canonical_strategy_id(strategy)
    archive = _read(_archive_path(root))
    row = (archive.get("champions") or {}).get(strategy)
    return dict(row) if isinstance(row, dict) else None


def record_champion_validation_result(
    root: str | Path, strategy: str, candidate: dict, summary: dict, *,
    data_version: str, validation_end: str | None = None,
) -> None:
    """Track the champion's latest gate result without discarding its history."""
    strategy = canonical_strategy_id(strategy)
    identifier = candidate.get("candidate_id")
    if not identifier:
        return
    path = _archive_path(root)
    timestamp = datetime.now(timezone.utc).isoformat()
    with _locked(path):
        archive = _read(path)
        champion = (archive.get("champions") or {}).get(strategy)
        if not isinstance(champion, dict) or champion.get("candidate_id") != identifier:
            return
        champion["latest_validation"] = {
            "status": summary.get("status"),
            "data_version": data_version,
            "validation_end": validation_end,
            "validated_at": timestamp,
            "summary": summary,
        }
        champion["currently_passes_gates"] = summary.get("status") == "passed"
        archive["updated_at"] = timestamp
        _write_archive(path, archive)


def update_champion_if_better(
    root: str | Path, strategy: str, candidate: dict, summary: dict,
) -> dict:
    """Keep the strongest exact-validated candidate as the persistent search seed."""
    strategy = canonical_strategy_id(strategy)
    if summary.get("status") != "passed":
        return {"updated": False, "reason": "candidate_did_not_pass_exact_validation"}
    identifier = candidate.get("candidate_id")
    parameters = candidate.get("parameters")
    if not identifier or not isinstance(parameters, dict):
        return {"updated": False, "reason": "candidate_identity_missing"}

    def rank(value: dict) -> tuple[float, float, float, float]:
        evidence = value.get("validation_summary") or value
        def metric(name: str, default: float) -> float:
            raw = evidence.get(name)
            if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                return default
            return float(raw)
        return (
            metric("lower_quartile_fitness", -math.inf),
            metric("median_fitness", -math.inf),
            metric("positive_fold_fraction", 0.0),
            metric("median_net_return_pct", -math.inf),
        )

    path = _archive_path(root)
    timestamp = datetime.now(timezone.utc).isoformat()
    with _locked(path):
        archive = _read(path)
        champions = archive.setdefault("champions", {})
        prior = champions.get(strategy)
        proposed = {
            "candidate_id": identifier,
            "strategy": strategy,
            "parameters": parameters,
            "regime_profiles": candidate.get("regime_profiles") or {},
            "model_family": candidate.get("model_family") or model_family(strategy, parameters),
            "logic_version": candidate.get("logic_version") or logic_version(strategy),
            "source": candidate.get("validation_role") or candidate.get("source") or "validated_candidate",
            "validation_summary": summary,
            "latest_validation": {
                "status": "passed",
                "data_version": summary.get("validation_data_version"),
                "validation_end": summary.get("validation_end"),
                "validated_at": timestamp,
                "summary": summary,
            },
            "currently_passes_gates": True,
            "selected_at": timestamp,
        }
        if prior and prior.get("candidate_id") != identifier and rank(prior) >= rank(proposed):
            return {"updated": False, "reason": "existing_champion_has_stronger_validation", "candidate_id": prior.get("candidate_id")}
        if prior and prior.get("candidate_id") == identifier:
            proposed["first_selected_at"] = prior.get("first_selected_at") or prior.get("selected_at")
        else:
            proposed["first_selected_at"] = timestamp
        champions[strategy] = proposed
        archive["updated_at"] = timestamp
        _write_archive(path, archive)
        return {"updated": True, "candidate_id": identifier, "previous_candidate_id": (prior or {}).get("candidate_id")}


def record_candidate_validation_folds(
    root: str | Path, strategy: str, records: list[dict],
) -> int:
    """Persist exact-parameter OOS folds, deduplicated by data and fold version."""
    strategy = canonical_strategy_id(strategy)
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
            key = record.get("candidate_id") or candidate_id(
                strategy, parameters, regime_profiles=record.get("regime_profiles"),
            )
            row = rows.get(key) or {
                "candidate_id": key, "strategy": strategy,
                "parameters": parameters, "first_seen": timestamp,
                "discovery_observations": 0, "regime_metrics": {},
                "verification_folds": [],
            }
            for name in (
                "model_family", "logic_version", "cost_model_version",
                "parent_candidate_id", "last_invocation_id", "regime_profiles",
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
        _write_archive(path, archive)
    return added


def record_verification(root: str | Path, strategy: str, detail: dict) -> int:
    """Attach strictly chronological OOS evidence to the exact parameter sets."""
    strategy = canonical_strategy_id(strategy)
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
        _write_archive(path, archive)
    return added


def parent_candidates(
    root: str | Path, strategy: str, *, limit: int = 6,
    before: str | None = None,
) -> tuple[list[dict], dict]:
    """Choose several high-quality, meaningfully different archive members."""
    strategy = canonical_strategy_id(strategy)
    if limit < 1:
        return [], {"used": False, "count": 0, "candidate_ids": []}
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
    champion = (archive.get("champions") or {}).get(strategy)
    champion_selected = False
    queued_seed_ids = []
    if (
        isinstance(champion, dict)
        and isinstance(champion.get("parameters"), dict)
        and champion.get("logic_version") in (None, logic_version(strategy))
    ):
        champion_row = next(
            (row for row in rows if row.get("candidate_id") == champion.get("candidate_id")),
            champion,
        )
        selected.append(champion_row)
        champion_selected = True
    pending = [
        row for row in ((archive.get("validation_queue") or {}).get(strategy) or [])
        if row.get("status") == "queued"
        and isinstance(row.get("parameters"), dict)
        and row.get("logic_version") in (None, logic_version(strategy))
    ]
    pending.sort(key=lambda row: (
        -float(row.get("discovery_delta") or 0.0),
        str(row.get("queued_at") or ""),
        str(row.get("candidate_id") or ""),
    ))
    max_queued_seeds = min(2, max(0, limit - len(selected)))
    for row in pending:
        if len(queued_seed_ids) >= max_queued_seeds:
            break
        if any(row.get("candidate_id") == parent.get("candidate_id") for parent in selected):
            continue
        selected.append(row)
        queued_seed_ids.append(row.get("candidate_id"))
    for row in rows:
        if len(selected) >= limit:
            break
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
        "champion_candidate_id": (champion or {}).get("candidate_id"),
        "champion_seeded": champion_selected,
        "queued_candidate_ids_seeded": queued_seed_ids,
        "queued_candidates_seeded": len(queued_seed_ids),
    }


def next_regime_candidates(root: str | Path, strategy: str) -> list[dict]:
    """Return the strongest sample-qualified candidate for each known regime."""
    strategy = canonical_strategy_id(strategy)
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
