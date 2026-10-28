"""Standalone options research runner with purged walk-forward validation."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import multiprocessing as mp
from pathlib import Path
import random
import statistics
from typing import Any

from equity_research.cpu import available_cpu_count
from options.market_inputs import research_market_inputs
from options.research.artifact_store import OptionsResearchArtifactStore
from options.research.champion import (
    champion_candidate, decide_promotion, load_state, pool_with_champion, record_decision,
)
from options.research.dataset import load_options_research_dataset
from options.research.reporting import write_research_report
from options.research.search import Candidate, add_plateau_scores, candidate_pool, evaluate_candidates
from options.research.simulator import simulate_options_strategy
from options.research.strategy_factory import export_section, live_defaults
from options.research.validation import FreshHoldoutUnavailable, make_purged_walk_forward
from runtime.atomic import atomic_write_json
from runtime.paths import ROOT


RESEARCH_DEFAULTS_PATH = Path(__file__).with_name("config.default.json")
_FOLD_EVALUATION_CONTEXT: dict[str, Any] = {}


def _merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"configuration must be a JSON object: {path}")
    return payload


def _project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _digest_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: str(value)):
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dataset_digest(path: Path, underlying: str) -> str:
    if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        from options.research.history_store import OptionsHistoryStore

        return OptionsHistoryStore(path, initialize=False).dataset_fingerprint(underlying)
    return _digest_path(path)


def _mean_metrics(rows: list[dict]) -> dict:
    keys = (
        "trade_count", "total_return", "annualized_sharpe", "annualized_sortino",
        "max_drawdown", "risk_adjusted_score", "win_rate", "average_trade_pnl",
        "gamma_vega_positive_pnl_share", "largest_winning_trade_share",
    )
    result = {}
    for key in keys:
        values = [float(row[key]) for row in rows if row.get(key) is not None and math.isfinite(float(row[key]))]
        result[key] = statistics.mean(values) if values else None
    result["windows"] = len(rows)
    return result


def _metric_deltas(tuned: dict, baseline: dict) -> dict:
    """Return paired tuned-minus-baseline deltas for the headline metrics."""
    keys = ("risk_adjusted_score", "total_return", "annualized_sharpe", "max_drawdown", "trade_count")
    deltas = {}
    for key in keys:
        tuned_value, baseline_value = tuned.get(key), baseline.get(key)
        if tuned_value is None or baseline_value is None:
            deltas[key] = None
            continue
        tuned_number, baseline_number = float(tuned_value), float(baseline_value)
        deltas[key] = tuned_number - baseline_number if math.isfinite(tuned_number) and math.isfinite(baseline_number) else None
    return deltas


def _equity_summary(run: dict) -> dict:
    curve = run.get("equity_curve", [])
    if not curve:
        return {"sessions": 0, "start": None, "end": None, "starting_equity": None,
                "ending_equity": None, "total_return": 0.0, "max_drawdown": 0.0}
    start_equity = float(curve[0]["equity"]) - float(curve[0]["daily_pnl"])
    end_equity = float(curve[-1]["equity"])
    peak = start_equity
    max_drawdown = 0.0
    for row in curve:
        peak = max(peak, float(row["equity"]))
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - float(row["equity"])) / peak)
    return {"sessions": len(curve), "start": curve[0]["date"], "end": curve[-1]["date"],
            "starting_equity": start_equity, "ending_equity": end_equity,
            "total_return": end_equity / start_equity - 1.0 if start_equity else 0.0,
            "max_drawdown": max_drawdown}


def _aggregate_runs(runs: list[dict], *, starting_equity: float, gamma_vega_threshold: float) -> dict:
    """Stitch disjoint OOS periods using fixed-size trade P&L and linked equity."""
    raw_rows = [row for run in runs for row in run["equity_curve"]]
    raw_rows.sort(key=lambda row: row["date"])
    curve = []
    equity = starting_equity
    previous = equity
    for row in raw_rows:
        daily_pnl = float(row["daily_pnl"])
        equity += daily_pnl
        curve.append({**row, "equity": equity, "daily_pnl": daily_pnl,
                      "return": daily_pnl / previous if previous else 0.0})
        previous = equity
    trades = [trade for run in runs for trade in run["trades"]]
    pnls = [float(trade["pnl"]) for trade in trades]
    daily_returns = [float(row["return"]) for row in curve]
    vol = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = statistics.mean(daily_returns) / vol * math.sqrt(252) if vol > 0 else 0.0
    downside = [min(0.0, value) for value in daily_returns]
    downside_dev = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    sortino = statistics.mean(daily_returns) / downside_dev * math.sqrt(252) if downside_dev > 0 else 0.0
    peak = starting_equity
    max_drawdown = 0.0
    for row in curve:
        peak = max(peak, row["equity"])
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - row["equity"]) / peak)
    gain = sum(value for value in pnls if value > 0)
    loss = -sum(value for value in pnls if value < 0)
    attribution = {key: sum(float(row.get("attribution", {}).get(key, 0.0)) for row in curve)
                   for key in ("delta", "gamma", "vega", "theta", "residual")}
    greek_keys = ("delta_notional", "gamma_pnl_1pct", "vega_per_vol_point", "theta_per_day")
    exposure_rows = [row.get("greeks", {}) for row in curve]
    greek_summary = {f"peak_abs_{key}": max((abs(float(item.get(key, 0.0))) for item in exposure_rows), default=0.0)
                     for key in greek_keys}
    greek_summary.update({f"mean_abs_{key}": statistics.mean(abs(float(item.get(key, 0.0))) for item in exposure_rows) if exposure_rows else 0.0
                          for key in greek_keys})
    greek_summary["pnl_attribution"] = attribution
    quality = defaultdict(int)
    for run in runs:
        for key, value in run["metrics"].get("quality", {}).items():
            quality[key] += int(value)
    missing_fraction = quality["missing_chain_observations"] / max(1, len(curve))
    gamma_vega_positive = sum(
        max(0.0, float(row.get("attribution", {}).get("gamma", 0.0)))
        + max(0.0, float(row.get("attribution", {}).get("vega", 0.0)))
        for row in curve
    )
    positive_daily = sum(max(0.0, float(row["daily_pnl"])) for row in curve)
    gamma_vega_share = gamma_vega_positive / positive_daily if positive_daily > 0 else 0.0
    largest_winner_share = max((value for value in pnls if value > 0), default=0.0) / gain if gain > 0 else 0.0
    dominated = gamma_vega_share >= gamma_vega_threshold or largest_winner_share >= 0.5
    score = sharpe - 2.0 * max_drawdown - 2.0 * missing_fraction
    if not trades:
        score = -5.0
    if dominated:
        score -= 0.75
    metrics = {
        "sessions": len(curve), "trade_count": len(trades),
        "total_return": equity / starting_equity - 1.0 if starting_equity else 0.0,
        "annualized_sharpe": sharpe, "annualized_sortino": sortino,
        "max_drawdown": max_drawdown,
        "profit_factor": gain / loss if loss > 0 else (None if gain > 0 else 0.0),
        "win_rate": sum(value > 0 for value in pnls) / len(pnls) if pnls else 0.0,
        "average_trade_pnl": statistics.mean(pnls) if pnls else 0.0,
        "median_trade_pnl": statistics.median(pnls) if pnls else 0.0,
        "risk_adjusted_score": score,
        "gamma_vega_positive_pnl_share": gamma_vega_share,
        "largest_winning_trade_share": largest_winner_share,
        "returns_dominated_by_gamma_or_vega_episodes": dominated,
        "greeks": greek_summary, "quality": dict(quality),
    }
    regime, volatility = {}, {}
    for row in curve:
        for target, label in ((regime, row.get("regime") or "unknown"),
                              (volatility, row.get("volatility_state") or "unknown")):
            item = target.setdefault(label, {"sessions": 0, "pnl": 0.0, "positive_sessions": 0, "negative_sessions": 0})
            item["sessions"] += 1
            item["pnl"] += float(row["daily_pnl"])
            item["positive_sessions"] += float(row["daily_pnl"]) > 0
            item["negative_sessions"] += float(row["daily_pnl"]) < 0
    return {"metrics": metrics, "trades": trades, "equity_curve": curve,
            "regime_breakdown": regime, "volatility_breakdown": volatility,
            "greek_summary": greek_summary}


def _parameter_stability(selected: list[dict]) -> dict:
    if not selected:
        return {"stability_score": 0.0, "parameters": {}, "regime_set_frequency": {}}
    names = sorted({name for row in selected for name in row["candidate"].get("parameters", {})})
    parameters = {}
    scores = []
    for name in names:
        values = [row["candidate"].get("parameters", {}).get(name) for row in selected]
        numeric = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
        if len(numeric) == len(values):
            spread = max(numeric) - min(numeric)
            normalized_std = statistics.pstdev(numeric) / spread if spread > 0 else 0.0
            stability = max(0.0, 1.0 - min(1.0, normalized_std))
            parameters[name] = {"mean": statistics.mean(numeric), "standard_deviation": statistics.pstdev(numeric),
                                "normalized_standard_deviation": normalized_std, "stability_score": stability,
                                "values": values}
            scores.append(stability)
        else:
            counts = defaultdict(int)
            for value in values:
                counts[json.dumps(value, sort_keys=True)] += 1
            stability = max(counts.values()) / len(values)
            parameters[name] = {"values": values, "value_frequency": dict(counts), "stability_score": stability}
            scores.append(stability)
    regime_counts = defaultdict(int)
    for row in selected:
        regime_counts[tuple(row["candidate"].get("allowed_regimes", []))] += 1
    regime_frequency = {", ".join(key): count / len(selected) for key, count in regime_counts.items()}
    regime_stability = max(regime_counts.values()) / len(selected)
    scores.append(regime_stability)
    return {"stability_score": statistics.mean(scores) if scores else regime_stability,
            "parameters": parameters, "regime_set_frequency": regime_frequency,
            "regime_set_stability": regime_stability, "selected_windows": len(selected)}


def _sequence_reshuffle(trades: list[dict], *, paths: int, seed: int, starting_equity: float) -> dict:
    pnls = [float(item["pnl"]) for item in trades]
    if len(pnls) < 2 or paths <= 0:
        return {"paths": 0, "trades": len(pnls), "status": "insufficient_closed_trades"}
    rng = random.Random(seed)
    terminal = []
    drawdowns = []
    for _ in range(paths):
        sequence = list(pnls)
        rng.shuffle(sequence)
        equity = peak = float(starting_equity)
        drawdown = 0.0
        for value in sequence:
            equity += value
            peak = max(peak, equity)
            if peak > 0:
                drawdown = max(drawdown, (peak - equity) / peak)
        terminal.append(equity - starting_equity)
        drawdowns.append(drawdown)
    def quantiles(values):
        ordered = sorted(values)
        return {"p05": ordered[int(0.05 * (len(ordered) - 1))],
                "p50": statistics.median(ordered), "p95": ordered[int(0.95 * (len(ordered) - 1))]}
    return {"paths": paths, "trades": len(pnls), "terminal_pnl": quantiles(terminal),
            "maximum_drawdown_fraction": quantiles(drawdowns),
            "probability_negative_terminal_pnl": sum(value < 0 for value in terminal) / len(terminal),
            "method": "random permutation of realized net trade P&L; does not model new market paths"}


def _append_run_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(encoded)
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_equity_csv(path: Path, rows: list[dict]) -> None:
    columns = ("date", "equity", "daily_pnl", "return", "regime", "volatility_state", "open_structures",
               "delta_notional", "gamma_pnl_1pct", "vega_per_vol_point", "theta_per_day",
               "delta_pnl", "gamma_pnl", "vega_pnl", "theta_pnl", "residual_pnl")
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(",".join(columns) + "\n")
        for row in rows:
            greeks, attribution = row.get("greeks", {}), row.get("attribution", {})
            values = [row.get("date"), row.get("equity"), row.get("daily_pnl"), row.get("return"),
                      row.get("regime"), row.get("volatility_state"), row.get("open_structures", 0),
                      greeks.get("delta_notional", 0), greeks.get("gamma_pnl_1pct", 0),
                      greeks.get("vega_per_vol_point", 0), greeks.get("theta_per_day", 0),
                      attribution.get("delta", 0), attribution.get("gamma", 0),
                      attribution.get("vega", 0), attribution.get("theta", 0), attribution.get("residual", 0)]
            stream.write(",".join('"' + str(value).replace('"', '""') + '"' for value in values) + "\n")


def _write_trades_csv(path: Path, rows: list[dict]) -> None:
    import csv

    columns = ("strategy", "structure_type", "structure_id", "entry_at", "exit_at",
               "entry_regime", "exit_reason", "underlying", "entry_spot", "exit_spot",
               "contracts", "pnl", "return_on_defined_risk", "entry_costs", "exit_costs",
               "maximum_abs_gamma_pnl_1pct", "maximum_abs_vega_per_vol_point",
               "maximum_abs_delta_notional", "stale_marks", "assignment_events")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "assignment_events": json.dumps(row.get("assignment_events", []), sort_keys=True)})


def _evaluate_fold_index(fold_index: int) -> dict:
    """Evaluate one independent fold using its process's inherited dataset."""
    context = _FOLD_EVALUATION_CONTEXT
    dataset = context["dataset"]
    folds = context["folds"]
    strategy = context["strategy"]
    research_config = context["research_config"]
    base_config = context["base_config"]
    fold = folds[fold_index]
    train = dataset.observations[fold.train_start:fold.train_end]
    oos = dataset.observations[fold.oos_start:fold.oos_end]

    def evaluate(candidate):
        run = simulate_options_strategy(
            dataset, train, strategy_name=strategy, candidate=candidate,
            config=research_config, base_config=base_config, force_close=True,
        )
        return {
            "risk_adjusted_score": run["risk_adjusted_score"],
            "metrics": run["metrics"],
            "regime_breakdown": run["regime_breakdown"],
            "volatility_breakdown": run["volatility_breakdown"],
            "equity_curve_summary": _equity_summary(run),
        }

    candidate_rows = evaluate_candidates(
        strategy, research_config, base_config, evaluate,
        include_candidate_objects=True,
        candidate_pool_override=context["candidate_pool"],
    )
    candidate_rows = add_plateau_scores(
        candidate_rows,
        min_profitable_neighbour_fraction=float(
            research_config.get("search", {}).get("minimum_profitable_neighbour_fraction", 0.5)
        ),
        neighbour_count=int(research_config.get("search", {}).get("plateau_neighbours", 4)),
    )
    chosen = candidate_rows[0]
    chosen_obj = chosen.pop("_candidate_obj")
    for row in candidate_rows[1:]:
        row.pop("_candidate_obj", None)
    oos_run = simulate_options_strategy(
        dataset, oos, strategy_name=strategy, candidate=chosen_obj,
        config=research_config, base_config=base_config, force_close=True,
    )
    baseline_candidate = next(
        (item for item in context["candidate_pool"] if item.is_baseline), None,
    )
    if baseline_candidate is None:
        raise ValueError("candidate pool lost its baseline candidate")
    baseline_oos_run = simulate_options_strategy(
        dataset, oos, strategy_name=strategy, candidate=baseline_candidate,
        config=research_config, base_config=base_config, force_close=True,
    )
    return {
        "candidate_rows": candidate_rows,
        "chosen": chosen,
        "oos_run": oos_run,
        "baseline_oos_run": baseline_oos_run,
    }


def run_research(
    strategy: str,
    *,
    data_path: str | Path | None = None,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    underlying: str | None = None,
    parallel_workers: int | None = None,
) -> dict:
    """Research one options expression module; it never reads credentials or submits orders."""
    strategy = str(strategy).strip().lower()
    supported = {"vrp", "llm_driven", "directional_overlay", "long_volatility"}
    if strategy not in supported:
        raise ValueError(f"strategy must be one of {', '.join(sorted(supported))}")
    config_path_resolved = _project_path(config_path) if config_path else RESEARCH_DEFAULTS_PATH
    research_config = _merge(_read_json(RESEARCH_DEFAULTS_PATH), _read_json(config_path_resolved)) if config_path_resolved != RESEARCH_DEFAULTS_PATH else _read_json(RESEARCH_DEFAULTS_PATH)
    configured_parallel_workers = research_config.get("search", {}).get("parallel_workers", 1)
    if isinstance(configured_parallel_workers, bool) or not isinstance(configured_parallel_workers, int) or configured_parallel_workers < 1:
        raise ValueError("search.parallel_workers must be a positive integer")
    effective_parallel_workers = configured_parallel_workers if parallel_workers is None else parallel_workers
    if isinstance(effective_parallel_workers, bool) or not isinstance(effective_parallel_workers, int) or effective_parallel_workers < 1:
        raise ValueError("parallel_workers must be a positive integer")
    loader_worker_count = min(effective_parallel_workers, available_cpu_count())
    selected_data_path = _project_path(data_path or research_config.get("data_path", "data/options/research/chains.jsonl"))
    output_root = _project_path(output_dir or research_config.get("output_dir", "data/options/research/runs"))
    underlying = str(underlying or research_config.get("underlying", "SPY")).strip().upper()
    if not underlying or not underlying.replace(".", "").isalnum():
        raise ValueError("underlying must be a non-empty ticker symbol")
    live_config_path = Path(__file__).resolve().parents[1] / "config.default.json"
    base_config = live_defaults(live_config_path)
    base_config_digest = hashlib.sha256(live_config_path.read_bytes()).hexdigest()
    code_digest = _digest_files([
        Path(__file__), Path(__file__).with_name("simulator.py"),
        Path(__file__).with_name("dataset.py"), Path(__file__).with_name("history_store.py"),
        Path(__file__).with_name("search.py"),
        Path(__file__).with_name("validation.py"), Path(__file__).with_name("execution.py"),
        Path(__file__).with_name("strategy_factory.py"), Path(__file__).with_name("reporting.py"),
        Path(__file__).parents[1] / "data.py", Path(__file__).parents[1] / "pricing.py",
        Path(__file__).parents[1] / "models.py", Path(__file__).parents[1] / "structures.py",
        Path(__file__).parents[1] / "assignment.py", live_config_path,
        ROOT / "strategies/options/vrp.py", ROOT / "strategies/options/llm_driven.py",
        ROOT / "strategies/options/directional_overlay.py", ROOT / "strategies/options/long_volatility.py",
        ROOT / "strategies/options/base.py",
        ROOT / "strategies/llm_driven/models.py",
    ])
    section = deepcopy(base_config[strategy])
    section.update(research_config.get("strategy_defaults", {}).get(strategy, {}))
    section["underlying"] = underlying
    if strategy in {"long_volatility", "directional_overlay"}:
        section["underlyings"] = [underlying]
    if strategy == "long_volatility":
        research_config.setdefault("search", {}).setdefault("grids", {}).setdefault(
            "long_volatility", {},
        )["underlyings"] = [[underlying]]
    config_digest = hashlib.sha256(json.dumps({
        "research_config": research_config,
        "selected_underlying": underlying,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    timestamp = datetime.now(timezone.utc)
    source_digest = _dataset_digest(selected_data_path, underlying) if selected_data_path.is_file() else "missing"
    run_id = f"{strategy}-{underlying.lower()}-{timestamp:%Y%m%dT%H%M%S%fZ}-{source_digest[:10]}-{config_digest[:8]}"
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    run_start = {
        "run_id": run_id, "created_at": timestamp.isoformat(), "strategy": strategy,
        "underlying": underlying, "research_config_sha256": config_digest,
        "live_base_config_sha256": base_config_digest, "code_sha256": code_digest,
        "dataset_path": str(selected_data_path), "dataset_sha256": source_digest,
        "status": "started",
    }
    atomic_write_json(run_dir / "run.json", {**run_start, "research_config": research_config},
                      indent=2, sort_keys=True, allow_nan=False)
    _append_run_log(output_root / "research_runs.jsonl", run_start)
    try:
        print(
            f"Options research {strategy}/{underlying}: loading {selected_data_path.name} "
            f"with {loader_worker_count} worker(s)",
            flush=True,
        )
        research_inputs = research_market_inputs(research_config.get("execution", {}))
        input_day = datetime.now(timezone.utc).date()
        dataset = load_options_research_dataset(
            selected_data_path, underlying=underlying,
            rate=research_inputs.rate(input_day).value,
            dividend_yield=research_inputs.dividend_yield(underlying, input_day).value,
            data_mode=str(research_config.get("data_mode", "auto")),
            model_surface_config=research_config.get("model_surface", {}),
            workers=loader_worker_count,
        )
        database_source = selected_data_path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        read_stage = "SQLite/DuckDB read" if database_source else "JSONL parse/normalize"
        read_seconds = dataset.loading_parallelism.get(
            "database_read_elapsed_seconds" if database_source
            else "jsonl_parse_and_normalize_elapsed_seconds",
            0,
        )
        print(
            f"Options research {strategy}/{underlying}: loading complete in "
            f"{dataset.loading_parallelism.get('elapsed_seconds', 0)}s total; "
            f"{read_stage} {read_seconds}s, "
            f"chain preparation {dataset.loading_parallelism.get('chain_preparation_elapsed_seconds', 0)}s "
            f"({dataset.loading_parallelism.get('chain_workers_used', 1)} workers)",
            flush=True,
        )
        try:
            folds, holdout, split = make_purged_walk_forward(
                dataset.observations, strategy=strategy, strategy_section=section, config=research_config,
                holdout_after=(champion_state or {}).get("holdout_end") if champion_enabled else None,
                minimum_fresh_holdout_sessions=int(
                    champion_config.get("minimum_fresh_holdout_sessions", 42)
                ) if champion_enabled else 0,
            )
        except FreshHoldoutUnavailable:
            raise
        except ValueError as exc:
            short_history = research_config.get("short_history_exploration", {})
            minimum_sessions = int(short_history.get("minimum_sessions", 60))
            if (
                short_history.get("enabled", False)
                and "insufficient history for" in str(exc)
                and len(dataset.observations) >= minimum_sessions
            ):
                baseline = candidate_pool(strategy, research_config, base_config)[0]
                replay = simulate_options_strategy(
                    dataset, dataset.observations, strategy_name=strategy,
                    candidate=baseline, config=research_config,
                    base_config=base_config, force_close=True,
                )
                replay_summary = _aggregate_runs(
                    [replay], starting_equity=float(research_config.get("starting_equity", 100000)),
                    gamma_vega_threshold=float(
                        research_config.get("robustness", {}).get("gamma_vega_episode_share_threshold", 0.60)
                    ),
                )
                report = {
                    "run_id": run_id,
                    "created_at": timestamp.isoformat(),
                    "strategy": strategy,
                    "underlying": underlying,
                    "status": "exploratory_only",
                    "research_tier": "short_history_baseline_replay",
                    "dataset_path": str(dataset.source_path),
                    "run_dir": str(run_dir),
                    "dataset_sha256": dataset.source_sha256,
                    "dataset_observations": len(dataset.observations),
                    "dataset_chain_errors": sum(item.chain is None for item in dataset.observations),
                    "data_mode": dataset.data_mode,
                    "parallelism": {
                        "loader_workers_requested": loader_worker_count,
                        "loader_workers_used": dataset.loading_parallelism.get("workers_used", 1),
                        "chain_workers_used": dataset.loading_parallelism.get("chain_workers_used", 1),
                        "loader_elapsed_seconds": dataset.loading_parallelism.get("elapsed_seconds"),
                        "jsonl_parse_and_normalize_elapsed_seconds": dataset.loading_parallelism.get("jsonl_parse_and_normalize_elapsed_seconds"),
                        "chain_preparation_elapsed_seconds": dataset.loading_parallelism.get("chain_preparation_elapsed_seconds"),
                        "fold_workers_requested": effective_parallel_workers,
                        "fold_workers_used": 0,
                        "dataset_sharing": "not_applicable_exploratory_replay",
                    },
                    "research_config_path": str(config_path_resolved),
                    "research_config_sha256": config_digest,
                    "live_base_config_sha256": base_config_digest,
                    "code_sha256": code_digest,
                    "random_seed": int(research_config.get("random_seed", 1)),
                    "split": {
                        "standard_validation_available": False,
                        "available_sessions": len(dataset.observations),
                        "standard_validation_requirement": str(exc),
                    },
                    "folds": [],
                    "candidate_leaderboard": [],
                    "exploratory_replay": {
                        "period_start": dataset.observations[0].as_of.isoformat(),
                        "period_end": dataset.observations[-1].as_of.isoformat(),
                        "profile": "current live defaults; no parameter search",
                        "candidate": baseline.to_dict(),
                        "metrics": replay_summary["metrics"],
                        "regime_breakdown": replay_summary["regime_breakdown"],
                        "volatility_breakdown": replay_summary["volatility_breakdown"],
                        "greek_summary": replay_summary["greek_summary"],
                        "equity_curve": replay_summary["equity_curve"],
                        "trades": replay_summary["trades"],
                    },
                    "walk_forward": {},
                    "holdout": {},
                    "final_parameters": {},
                    "final_allowed_regimes": list(baseline.allowed_regimes),
                    "parameter_stability": {},
                    "robustness": {},
                    "validation": {
                        "approved": False,
                        "promotion_eligible": False,
                        "status": "not_validated",
                        "reason": "Short-history replay is descriptive only; standard rolling OOS and holdout gates were not met.",
                    },
                    "data_assumptions": {
                        "historical_chain_snapshots_required_for_empirical_claims": True,
                        "model_or_synthetic_rows_present": dataset.data_mode.startswith("model_surface") or dataset.data_mode == "model_or_synthetic_chain_rows",
                        "point_in_time_events_and_signals_only": True,
                    },
                }
                write_research_report(run_dir, report)
                atomic_write_json(run_dir / "candidate_reports.json", [], indent=2, sort_keys=True)
                _write_equity_csv(run_dir / "exploratory_equity_curve.csv", replay_summary["equity_curve"])
                _write_trades_csv(run_dir / "exploratory_trades.csv", replay_summary["trades"])
                atomic_write_json(run_dir / "run.json", {
                    "run_id": run_id, "strategy": strategy,
                    "research_config": research_config,
                    "research_config_sha256": config_digest,
                    "live_base_config_sha256": base_config_digest,
                    "code_sha256": code_digest,
                    "dataset_path": str(dataset.source_path), "dataset_sha256": dataset.source_sha256,
                    "created_at": timestamp.isoformat(), "status": "exploratory_only",
                    "validation": report["validation"],
                    "parallelism": report["parallelism"],
                }, indent=2, sort_keys=True, allow_nan=False)
                _append_run_log(output_root / "research_runs.jsonl", {
                    "run_id": run_id, "created_at": timestamp.isoformat(), "strategy": strategy,
                    "underlying": underlying, "dataset_sha256": dataset.source_sha256,
                    "research_config_sha256": config_digest,
                    "live_base_config_sha256": base_config_digest, "code_sha256": code_digest,
                    "run_dir": str(run_dir), "status": "exploratory_only",
                    "validation": report["validation"],
                    "parallelism": report["parallelism"],
                })
                OptionsResearchArtifactStore().store_run(run_dir, report)
                return report
            raise
    except Exception as exc:
        failure = {**run_start, "status": "preflight_failed", "error": f"{type(exc).__name__}: {exc}"}
        atomic_write_json(run_dir / "run.json", {**failure, "research_config": research_config},
                          indent=2, sort_keys=True, allow_nan=False)
        _append_run_log(output_root / "research_runs.jsonl", failure)
        OptionsResearchArtifactStore().store_run(run_dir, failure)
        raise
    if not all(item.chain is not None for item in dataset.observations):
        chain_errors = sum(item.chain is None for item in dataset.observations)
    else:
        chain_errors = 0
    starting_equity = float(research_config.get("starting_equity", 100000))
    gamma_vega_threshold = float(research_config.get("robustness", {}).get("gamma_vega_episode_share_threshold", 0.60))

    requested_fold_workers = configured_parallel_workers
    fold_worker_count = min(effective_parallel_workers, available_cpu_count(), len(folds))
    fork_available = "fork" in mp.get_all_start_methods()
    if fold_worker_count > 1 and not fork_available:
        fold_worker_count = 1

    global _FOLD_EVALUATION_CONTEXT
    champion_config = research_config.get("champion", {})
    champion_enabled = bool(champion_config.get("enabled", True))
    champion_margin = float(champion_config.get("minimum_holdout_score_margin", 0.0))
    champion_state = load_state(strategy, underlying) if champion_enabled else None
    incumbent = champion_state if champion_state and champion_state.get("candidate_id") else None
    shared_candidate_pool = pool_with_champion(candidate_pool(strategy, research_config, base_config), incumbent)
    _FOLD_EVALUATION_CONTEXT = {
        "dataset": dataset,
        "folds": folds,
        "strategy": strategy,
        "research_config": research_config,
        "base_config": base_config,
        "candidate_pool": shared_candidate_pool,
    }
    try:
        if fold_worker_count > 1:
            # Fork only the independent fold jobs. Their large point-in-time
            # dataset stays read-only and shared copy-on-write; task payloads
            # are fold indexes instead of serialized multi-gigabyte datasets.
            with ProcessPoolExecutor(
                max_workers=fold_worker_count,
                mp_context=mp.get_context("fork"),
            ) as executor:
                fold_results = list(executor.map(_evaluate_fold_index, range(len(folds)), chunksize=1))
        else:
            fold_results = [_evaluate_fold_index(index) for index in range(len(folds))]
    finally:
        _FOLD_EVALUATION_CONTEXT = {}

    fold_records = []
    candidate_ledger: dict[str, dict] = {}
    selected_rows = []
    selected_oos_runs = []
    baseline_oos_runs = []
    for fold, fold_result in zip(folds, fold_results):
        candidate_rows = fold_result["candidate_rows"]
        chosen = fold_result["chosen"]
        oos_run = fold_result["oos_run"]
        baseline_oos_run = fold_result["baseline_oos_run"]
        selected_oos_runs.append(oos_run)
        baseline_oos_runs.append(baseline_oos_run)
        selected_entry = {"candidate": chosen["candidate"], "plateau": chosen["plateau"],
                          "is_metrics": chosen.get("metrics", {}), "oos_metrics": oos_run["metrics"],
                          "fold": fold.number}
        selected_rows.append(selected_entry)
        fold_records.append({
            "fold": fold.__dict__, "selected_candidate": chosen["candidate"],
            "selection_score": chosen.get("selection_score"), "plateau": chosen["plateau"],
            "is_metrics": chosen.get("metrics", {}), "oos_metrics": oos_run["metrics"],
            "baseline_oos_metrics": baseline_oos_run["metrics"],
            "oos_delta_vs_baseline": _metric_deltas(oos_run["metrics"], baseline_oos_run["metrics"]),
        })
        for item in candidate_rows:
            identity = item["candidate"]["candidate_id"]
            entry = candidate_ledger.setdefault(identity, {
                "candidate": item["candidate"], "is_metrics": [], "selected_oos_metrics": [],
                "selected_oos_folds": 0, "plateau_stability": [],
                "in_sample_reports": [], "selected_oos_reports": [],
            })
            entry["is_metrics"].append(item.get("metrics", {}))
            entry["in_sample_reports"].append({
                "fold": fold.number,
                "metrics": item.get("metrics", {}),
                "regime_breakdown": item.get("regime_breakdown", {}),
                "volatility_breakdown": item.get("volatility_breakdown", {}),
                "equity_curve_summary": item.get("equity_curve_summary", {}),
                "plateau": item.get("plateau", {}),
            })
            if identity == chosen["candidate"]["candidate_id"]:
                entry["selected_oos_metrics"].append(oos_run["metrics"])
                entry["selected_oos_folds"] += 1
                entry["plateau_stability"].append(bool(item.get("plateau", {}).get("stable")))
                entry["selected_oos_reports"].append({
                    "fold": fold.number, "metrics": oos_run["metrics"],
                    "regime_breakdown": oos_run["regime_breakdown"],
                    "volatility_breakdown": oos_run["volatility_breakdown"],
                    "equity_curve_summary": _equity_summary(oos_run),
                })

    walk_forward = _aggregate_runs(
        selected_oos_runs, starting_equity=starting_equity,
        gamma_vega_threshold=gamma_vega_threshold,
    )
    baseline_candidate = next(
        (item for item in shared_candidate_pool if item.is_baseline), None,
    )
    if baseline_candidate is None:
        raise ValueError("candidate pool lost its baseline candidate")
    baseline_walk_forward = _aggregate_runs(
        baseline_oos_runs, starting_equity=starting_equity,
        gamma_vega_threshold=gamma_vega_threshold,
    )
    mean_is_score = statistics.mean(
        float(row["is_metrics"].get("risk_adjusted_score", -5.0)) for row in selected_rows
    ) if selected_rows else 0.0
    mean_oos_score = float(walk_forward["metrics"]["risk_adjusted_score"])
    efficiency = mean_oos_score / mean_is_score if mean_is_score > 0 else None
    walk_forward["walk_forward_efficiency"] = efficiency
    walk_forward["mean_selected_is_risk_adjusted_score"] = mean_is_score
    walk_forward["mean_oos_risk_adjusted_score"] = mean_oos_score

    # The final candidate is selected by risk-adjusted OOS score among the
    # configurations already selected on training windows. The final holdout
    # remains untouched until after this step.
    vote_scores = defaultdict(list)
    selected_by_id = {}
    for entry in selected_rows:
        cid = entry["candidate"]["candidate_id"]
        selected_by_id[cid] = entry["candidate"]
        vote_scores[cid].append(float(entry["oos_metrics"].get("risk_adjusted_score", -5.0)))
    final_id = max(vote_scores, key=lambda key: (statistics.mean(vote_scores[key]), len(vote_scores[key]), key))
    final_candidate_payload = selected_by_id[final_id]
    final_candidate = Candidate(
        final_candidate_payload["candidate_id"], final_candidate_payload.get("parameters", {}),
        tuple(final_candidate_payload.get("allowed_regimes", [])), (),
    )
    holdout_run = simulate_options_strategy(
        dataset, holdout, strategy_name=strategy, candidate=final_candidate,
        config=research_config, base_config=base_config, force_close=True,
    )
    baseline_holdout_run = simulate_options_strategy(
        dataset, holdout, strategy_name=strategy, candidate=baseline_candidate,
        config=research_config, base_config=base_config, force_close=True,
    )
    incumbent_holdout_run = None
    if incumbent and incumbent["candidate_id"] != final_candidate.candidate_id:
        incumbent_holdout_run = simulate_options_strategy(
            dataset, holdout, strategy_name=strategy, candidate=champion_candidate(incumbent),
            config=research_config, base_config=base_config, force_close=True,
        )
    elif incumbent:
        incumbent_holdout_run = holdout_run
    baseline_comparison = {
        "baseline_candidate": baseline_candidate.to_dict(),
        "same_oos_folds_and_holdout": True,
        "same_execution_cost_model": True,
        "walk_forward": {
            "baseline_metrics": baseline_walk_forward["metrics"],
            "tuned_metrics": walk_forward["metrics"],
            "tuned_minus_baseline": _metric_deltas(
                walk_forward["metrics"], baseline_walk_forward["metrics"],
            ),
        },
        "holdout": {
            "baseline_metrics": baseline_holdout_run["metrics"],
            "tuned_metrics": holdout_run["metrics"],
            "tuned_minus_baseline": _metric_deltas(
                holdout_run["metrics"], baseline_holdout_run["metrics"],
            ),
        },
    }

    sensitivity = []
    slippage_scenarios = research_config.get("robustness", {}).get("slippage_multipliers", [1.0, 1.5, 2.0])
    spread_scenarios = research_config.get("robustness", {}).get("spread_width_multipliers", [1.0, 1.5, 2.0])
    for slippage in slippage_scenarios:
        for spread_width in spread_scenarios:
            run = simulate_options_strategy(
                dataset, holdout, strategy_name=strategy, candidate=final_candidate,
                config=research_config, base_config=base_config,
                slippage_multiplier=float(slippage), spread_width_multiplier=float(spread_width),
                force_close=True,
            )
            sensitivity.append({"slippage_multiplier": float(slippage),
                                "spread_width_multiplier": float(spread_width),
                                "metrics": run["metrics"]})
    assignment_sensitivity = {"enabled": bool(research_config.get("robustness", {}).get("run_assignment_sensitivity", True))}
    if assignment_sensitivity["enabled"]:
        without_assignment = simulate_options_strategy(
            dataset, holdout, strategy_name=strategy, candidate=final_candidate,
            config=research_config, base_config=base_config, assignment_enabled=False,
            force_close=True,
        )
        assignment_sensitivity.update({
            "with_assignment": holdout_run["metrics"],
            "without_early_assignment": without_assignment["metrics"],
            "pnl_difference_usd": starting_equity * (
                holdout_run["metrics"]["total_return"] - without_assignment["metrics"]["total_return"]
            ),
            "assignment_events": len(holdout_run.get("assignment_events", [])),
        })
    reshuffle = _sequence_reshuffle(
        walk_forward["trades"],
        paths=int(research_config.get("robustness", {}).get("monte_carlo_paths", 500)),
        seed=int(research_config.get("random_seed", 1)),
        starting_equity=starting_equity,
    )

    selected_stability = _parameter_stability(selected_rows)
    leaderboard = []
    for identity, entry in candidate_ledger.items():
        is_rows = entry["is_metrics"]
        oos_rows = entry["selected_oos_metrics"]
        leaderboard.append({
            "candidate_id": identity,
            "parameters": entry["candidate"].get("parameters", {}),
            "allowed_regimes": entry["candidate"].get("allowed_regimes", []),
            "is_baseline": bool(entry["candidate"].get("is_baseline", False)),
            "selected_oos_folds": entry["selected_oos_folds"],
            "mean_is_metrics": _mean_metrics(is_rows),
            "selected_oos_metrics": _mean_metrics(oos_rows) if oos_rows else None,
            "plateau_stable_selection_fraction": (sum(entry["plateau_stability"]) / len(entry["plateau_stability"])
                                                    if entry["plateau_stability"] else None),
            "in_sample_reports": entry["in_sample_reports"],
            "selected_oos_reports": entry["selected_oos_reports"],
        })
    leaderboard.sort(key=lambda row: (
        row["selected_oos_metrics"] is not None,
        (row["selected_oos_metrics"] or {}).get("risk_adjusted_score", -5.0),
        row["mean_is_metrics"].get("risk_adjusted_score", -5.0),
    ), reverse=True)
    baseline_row = next((row for row in leaderboard if row["is_baseline"]), None)
    baseline_fitness = (
        baseline_row["mean_is_metrics"].get("risk_adjusted_score")
        if baseline_row else None
    )
    for row in leaderboard:
        fitness = row["mean_is_metrics"].get("risk_adjusted_score")
        row["baseline_fitness"] = baseline_fitness
        row["fitness_delta_vs_baseline"] = (
            float(fitness) - float(baseline_fitness)
            if fitness is not None and baseline_fitness is not None else None
        )

    wf_min_trades = int(research_config.get("walk_forward", {}).get("minimum_oos_trades", 8))
    holdout_min_trades = int(research_config.get("walk_forward", {}).get("minimum_holdout_trades", 4))
    holdout_coverage = split["holdout_regime_coverage"]
    plateau_fraction = statistics.mean([bool(row["plateau"].get("stable")) for row in selected_rows]) if selected_rows else 0.0
    min_plateau_fraction = float(research_config.get("search", {}).get("minimum_stable_selection_fraction", 0.50))
    min_parameter_stability = float(research_config.get("search", {}).get("minimum_parameter_stability_score", 0.50))
    minimum_stress_slippage = float(research_config.get("robustness", {}).get("minimum_stress_slippage_multiplier", 1.5))
    minimum_stress_spread = float(research_config.get("robustness", {}).get("minimum_stress_spread_width_multiplier", 1.5))
    stress_case = next((item for item in sensitivity
                        if item["slippage_multiplier"] >= minimum_stress_slippage
                        and item["spread_width_multiplier"] >= minimum_stress_spread), None)
    stress_resilient = bool(
        stress_case and stress_case["metrics"]["risk_adjusted_score"] > 0
        and stress_case["metrics"]["total_return"] > 0
    )
    validation = {
        "minimum_oos_trades": wf_min_trades,
        "observed_oos_trades": walk_forward["metrics"]["trade_count"],
        "minimum_oos_trades_met": walk_forward["metrics"]["trade_count"] >= wf_min_trades,
        "minimum_holdout_trades": holdout_min_trades,
        "observed_holdout_trades": holdout_run["metrics"]["trade_count"],
        "minimum_holdout_trades_met": holdout_run["metrics"]["trade_count"] >= holdout_min_trades,
        "holdout_coverage_met": bool(holdout_coverage["meets_minimum_coverage"]),
        "walk_forward_oos_positive": walk_forward["metrics"]["risk_adjusted_score"] > 0,
        "holdout_positive_after_costs": holdout_run["metrics"]["risk_adjusted_score"] > 0 and holdout_run["metrics"]["total_return"] > 0,
        "plateau_stable_selection_fraction": plateau_fraction,
        "minimum_stable_selection_fraction": min_plateau_fraction,
        "plateau_gate_met": plateau_fraction >= min_plateau_fraction,
        "parameter_stability_score": selected_stability["stability_score"],
        "minimum_parameter_stability_score": min_parameter_stability,
        "parameter_stability_gate_met": selected_stability["stability_score"] >= min_parameter_stability,
        "stress_slippage_multiplier": minimum_stress_slippage,
        "stress_spread_width_multiplier": minimum_stress_spread,
        "stress_case_positive_after_costs": stress_resilient,
        "returns_not_dominated_by_gamma_or_vega_episodes": not (
            walk_forward["metrics"]["returns_dominated_by_gamma_or_vega_episodes"]
            or holdout_run["metrics"]["returns_dominated_by_gamma_or_vega_episodes"]
        ),
        "approved": False,
    }
    validation["approved"] = all((
        validation["minimum_oos_trades_met"], validation["minimum_holdout_trades_met"],
        validation["holdout_coverage_met"], validation["walk_forward_oos_positive"],
        validation["holdout_positive_after_costs"], validation["plateau_gate_met"],
        validation["parameter_stability_gate_met"], validation["stress_case_positive_after_costs"],
        validation["returns_not_dominated_by_gamma_or_vega_episodes"],
    ))
    candidate_score = holdout_run["metrics"].get("risk_adjusted_score")
    incumbent_score = (incumbent_holdout_run["metrics"].get("risk_adjusted_score") if incumbent_holdout_run else None)
    promoted, promotion_reason = (False, "champion carry disabled")
    if champion_enabled:
        promoted, promotion_reason = decide_promotion(
            approved=validation["approved"],
            candidate_holdout_score=float(candidate_score) if candidate_score is not None else None,
            incumbent_holdout_score=float(incumbent_score) if incumbent_score is not None else None,
            margin=champion_margin,
        )
    live_candidate = final_candidate if promoted else (champion_candidate(incumbent) if incumbent else None)
    validation["champion"] = {
        "enabled": champion_enabled,
        "incumbent_candidate_id": (incumbent or {}).get("candidate_id"),
        "incumbent_holdout_risk_adjusted_score": incumbent_score,
        "candidate_holdout_risk_adjusted_score": candidate_score,
        "minimum_holdout_score_margin": champion_margin,
        "promoted": promoted,
        "reason": promotion_reason,
        "live_candidate_id": live_candidate.candidate_id if live_candidate else None,
    }
    exported = live_defaults(live_config_path)
    exported[strategy] = export_section(
        strategy, parameters=(live_candidate or final_candidate).parameters,
        allowed_regimes=(live_candidate or final_candidate).allowed_regimes, base_config=exported,
    )
    if strategy in {"long_volatility", "directional_overlay"}:
        exported[strategy]["underlyings"] = [underlying]
    else:
        exported[strategy]["underlying"] = underlying
    exported[strategy]["enabled"] = live_candidate is not None

    report = {
        "run_id": run_id,
        "created_at": timestamp.isoformat(),
        "strategy": strategy,
        "underlying": underlying,
        "dataset_path": str(dataset.source_path),
        "run_dir": str(run_dir),
        "dataset_sha256": dataset.source_sha256,
        "dataset_observations": len(dataset.observations),
        "dataset_chain_errors": chain_errors,
        "data_mode": dataset.data_mode,
        "research_config_path": str(config_path_resolved),
        "research_config_sha256": config_digest,
        "live_base_config_sha256": base_config_digest,
        "code_sha256": code_digest,
        "random_seed": int(research_config.get("random_seed", 1)),
        "parallelism": {
            "loader_workers_requested": loader_worker_count,
            "loader_workers_used": dataset.loading_parallelism.get("workers_used", 1),
            "chain_workers_used": dataset.loading_parallelism.get("chain_workers_used", 1),
            "loader_elapsed_seconds": dataset.loading_parallelism.get("elapsed_seconds"),
            "jsonl_parse_and_normalize_elapsed_seconds": dataset.loading_parallelism.get("jsonl_parse_and_normalize_elapsed_seconds"),
            "chain_preparation_elapsed_seconds": dataset.loading_parallelism.get("chain_preparation_elapsed_seconds"),
            "fold_workers_requested": effective_parallel_workers,
            "fold_workers_used": fold_worker_count,
            "dataset_sharing": "fork_copy_on_write" if fold_worker_count > 1 else "single_process",
        },
        "split": split,
        "folds": fold_records,
        "candidate_leaderboard": leaderboard,
        "final_candidate_id": final_id,
        "final_parameters": final_candidate.parameters,
        "final_allowed_regimes": list(final_candidate.allowed_regimes),
        "baseline_comparison": baseline_comparison,
        "parameter_stability": selected_stability,
        "walk_forward": {
            "metrics": walk_forward["metrics"],
            "oos_metrics": walk_forward["metrics"],
            "walk_forward_efficiency": efficiency,
            "mean_selected_is_risk_adjusted_score": mean_is_score,
            "mean_oos_risk_adjusted_score": mean_oos_score,
            "regime_breakdown": walk_forward["regime_breakdown"],
            "volatility_breakdown": walk_forward["volatility_breakdown"],
            "greek_summary": walk_forward["greek_summary"],
            "equity_curve": walk_forward["equity_curve"],
            "trades": walk_forward["trades"],
        },
        "holdout": {
            "metrics": holdout_run["metrics"],
            "regime_breakdown": holdout_run["regime_breakdown"],
            "volatility_breakdown": holdout_run["volatility_breakdown"],
            "greek_summary": holdout_run["greek_summary"],
            "equity_curve": holdout_run["equity_curve"],
            "trades": holdout_run["trades"],
            "assignment_events": holdout_run["assignment_events"],
            "start": holdout[0].as_of.isoformat(), "end": holdout[-1].as_of.isoformat(),
        },
        "robustness": {
            "slippage_sensitivity": sensitivity,
            "assignment_sensitivity": assignment_sensitivity,
            "trade_sequence_reshuffle": reshuffle,
        },
        "validation": validation,
        "execution_assumptions": holdout_run["execution"],
        "data_assumptions": {
            "event_llm_core_searched": False,
            "historical_chain_snapshots_required_for_empirical_claims": True,
            "model_or_synthetic_rows_present": dataset.data_mode.startswith("model_surface") or dataset.data_mode == "model_or_synthetic_chain_rows",
            "model_iv_surface_used": dataset.data_mode.startswith("model_surface"),
            "spot_close_fallback_used": dataset.data_mode.endswith("spot_close_proxy"),
            "point_in_time_events_and_signals_only": True,
        },
    }
    write_research_report(run_dir, report)
    atomic_write_json(run_dir / "candidate_reports.json", leaderboard,
                      indent=2, sort_keys=True, allow_nan=False)
    _write_equity_csv(run_dir / "equity_curve.csv", walk_forward["equity_curve"])
    _write_equity_csv(run_dir / "holdout_equity_curve.csv", holdout_run["equity_curve"])
    _write_equity_csv(run_dir / "baseline_equity_curve.csv", baseline_walk_forward["equity_curve"])
    _write_equity_csv(run_dir / "baseline_holdout_equity_curve.csv", baseline_holdout_run["equity_curve"])
    _write_trades_csv(run_dir / "oos_trades.csv", walk_forward["trades"])
    _write_trades_csv(run_dir / "holdout_trades.csv", holdout_run["trades"])
    _write_trades_csv(run_dir / "baseline_oos_trades.csv", baseline_walk_forward["trades"])
    _write_trades_csv(run_dir / "baseline_holdout_trades.csv", baseline_holdout_run["trades"])
    atomic_write_json(run_dir / "run.json", {
        "run_id": run_id, "strategy": strategy,
        "research_config": research_config,
        "research_config_sha256": config_digest,
        "live_base_config_sha256": base_config_digest,
        "code_sha256": code_digest,
        "dataset_path": str(dataset.source_path), "dataset_sha256": dataset.source_sha256,
        "created_at": timestamp.isoformat(), "validation": validation, "status": "completed",
        "final_candidate_id": final_id,
        "parallelism": report["parallelism"],
    }, indent=2, sort_keys=True, allow_nan=False)
    if champion_enabled:
        record_decision(
            strategy=strategy, underlying=underlying, run_id=run_id,
            decided_at=timestamp.isoformat(),
            holdout_start=split["holdout_start"], holdout_end=split["holdout_end"],
            candidate=final_candidate,
            candidate_holdout_score=float(candidate_score) if candidate_score is not None else None,
            incumbent_candidate_id=(incumbent or {}).get("candidate_id"),
            incumbent_holdout_score=float(incumbent_score) if incumbent_score is not None else None,
            promoted=promoted, reason=promotion_reason,
        )
    if exported[strategy]["enabled"]:
        atomic_write_json(run_dir / "approved-options-config.json", exported, indent=2, sort_keys=True, allow_nan=False)
    else:
        atomic_write_json(run_dir / "candidate-options-config.json", exported, indent=2, sort_keys=True, allow_nan=False)
    _append_run_log(output_root / "research_runs.jsonl", {
        "run_id": run_id, "created_at": timestamp.isoformat(), "strategy": strategy,
        "underlying": underlying, "dataset_sha256": dataset.source_sha256,
        "research_config_sha256": config_digest, "validation": validation,
        "live_base_config_sha256": base_config_digest, "code_sha256": code_digest,
        "run_dir": str(run_dir), "status": "completed",
        "parallelism": report["parallelism"],
    })
    OptionsResearchArtifactStore().store_run(run_dir, report)
    return report


__all__ = ["run_research"]
