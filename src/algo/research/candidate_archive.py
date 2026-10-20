"""Persistent, diverse strategy-candidate memory and regime scorecards."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path

from .files import _atomic_json


ARCHIVE_SCHEMA = "swarm_algo_candidate_archive_v1"
MAX_CANDIDATES_PER_STRATEGY = 64
MAX_DATA_VERSIONS_PER_STRATEGY = 32

STRATEGY_LOGIC_VERSIONS = {
    "mean_reversion": "intraday-mr-2",
    "swing_mean_reversion": "swing-mr-2",
    "trend_following": "swing-trend-2",
    "swing_trend_following": "swing-trend-2",
    "momentum": "swing-momentum-2",
    "swing_momentum": "swing-momentum-2",
    "multi_factor": "multi-factor-rank-4-ridge-walk-forward",
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
        }
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read candidate archive: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or value.get("schema") != ARCHIVE_SCHEMA:
        raise ValueError("candidate archive has an unsupported schema")
    value.setdefault("run_counters", {})
    value.setdefault("candidates", {})
    if not isinstance(value["run_counters"], dict) or not isinstance(value["candidates"], dict):
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
        if weight_mode == "ridge_walk_forward":
            active_factors = "research_model_factors"
        timing = "regime_timed" if values.get("regime_factor_weights") else "static"
        return ":".join((
            "multi_factor", weight_mode, "linear_composite", active_factors,
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
        evaluated = archive.setdefault("evaluated_candidates_by_data_version", {}).setdefault(strategy, {})
        for detail in details:
            window = detail.get("window") or {}
            data_version = (
                (detail.get("adaptive_window_search") or {}).get("data_version")
                or detail.get("data_version")
            )
            prior_record = (evaluated.get(data_version) or {}) if data_version else {}
            prior_ids = (
                prior_record.get("candidate_ids") or []
                if isinstance(prior_record, dict) else prior_record
            )
            seen_for_version = set(prior_ids)
            for item in detail.get("candidates") or []:
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
                    "parent_candidate_id": item.get("parent_candidate_id"),
                    "data_version": data_version,
                    "cost_model_version": item.get("cost_model_version"),
                })
                existing["discovery_observations"] = int(existing.get("discovery_observations", 0)) + 1
                rows[key] = existing
                count += 1
            if data_version:
                evaluated[data_version] = {
                    "updated_at": timestamp,
                    "candidate_ids": sorted(seen_for_version),
                }

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


def _candidate_rank(row: dict) -> tuple[float, float, int]:
    folds = row.get("verification_folds") or []
    oos = [float(item.get("fitness") or 0.0) for item in folds if item.get("fitness") is not None]
    regime = row.get("regime_metrics") or {}
    regime_values = [
        float(value.get("fitness") or 0.0)
        for value in regime.values()
        if isinstance(value, dict) and value.get("minimum_sample_met")
    ]
    robust = min(oos) if oos else min(regime_values) if regime_values else float(row.get("fitness") or 0.0)
    positive_folds = sum(value > 0 for value in oos)
    return robust, float(row.get("fitness") or 0.0), positive_folds


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
        and (before is None or str((row.get("last_window") or {}).get("end") or "") < before)
        and (
            (
                bool(row.get("eligible"))
                and float((row.get("metrics") or {}).get("net_return_pct") or 0.0) > 0
            )
            or any(
                isinstance(value, dict)
                and value.get("eligible")
                and float((value.get("metrics") or {}).get("net_return_pct") or 0.0) > 0
                for value in (row.get("regime_metrics") or {}).values()
            )
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
