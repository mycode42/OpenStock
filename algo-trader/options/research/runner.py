"""Standalone options research runner with purged walk-forward validation."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
import dataclasses
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
from options.research.dataset import load_options_research_dataset
from options.research.reporting import write_research_report
from options.research.strategy_configuration import fixed_strategy_configuration
from options.research.simulator import simulate_options_strategy
from options.research.strategy_factory import live_defaults
from options.research.validation import make_purged_walk_forward
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


def _dataset_chain_errors(dataset) -> int:
    if getattr(dataset.provider, "lazy_chains", False):
        return sum(item.chain_error is not None for item in dataset.observations)
    return sum(item.chain is None for item in dataset.observations)


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
    """Replay one fixed configuration over the train and OOS slices."""
    context = _FOLD_EVALUATION_CONTEXT
    dataset = context["dataset"]
    folds = context["folds"]
    strategy = context["strategy"]
    research_config = context["research_config"]
    base_config = context["base_config"]
    strategy_configuration = context["strategy_configuration"]
    fold = folds[fold_index]
    train = dataset.observations[fold.train_start:fold.train_end]
    oos = dataset.observations[fold.oos_start:fold.oos_end]
    train_run = simulate_options_strategy(
        dataset, train, strategy_name=strategy,
        strategy_configuration=strategy_configuration,
        config=research_config, base_config=base_config, force_close=True,
    )
    oos_run = simulate_options_strategy(
        dataset, oos, strategy_name=strategy,
        strategy_configuration=strategy_configuration,
        config=research_config, base_config=base_config, force_close=True,
    )
    return {
        "train_run": train_run,
        "oos_run": oos_run,
    }


def run_research(
    strategy: str,
    *,
    data_path: str | Path | None = None,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    underlying: str | None = None,
    parallel_workers: int | None = None,
    replay_only: bool = False,
) -> dict:
    """Replay one options strategy with fixed settings; it never searches or submits orders."""
    strategy = str(strategy).strip().lower()
    supported = {"vrp", "llm_driven", "directional_overlay", "long_volatility"}
    if strategy not in supported:
        raise ValueError(f"strategy must be one of {', '.join(sorted(supported))}")
    config_path_resolved = _project_path(config_path) if config_path else RESEARCH_DEFAULTS_PATH
    research_config = (
        _merge(_read_json(RESEARCH_DEFAULTS_PATH), _read_json(config_path_resolved))
        if config_path_resolved != RESEARCH_DEFAULTS_PATH
        else _read_json(RESEARCH_DEFAULTS_PATH)
    )
    configured_parallel_workers = research_config.get("parallel_workers", 1)
    if (
        isinstance(configured_parallel_workers, bool)
        or not isinstance(configured_parallel_workers, int)
        or configured_parallel_workers < 1
    ):
        raise ValueError("parallel_workers must be a positive integer")
    effective_parallel_workers = configured_parallel_workers if parallel_workers is None else parallel_workers
    if (
        isinstance(effective_parallel_workers, bool)
        or not isinstance(effective_parallel_workers, int)
        or effective_parallel_workers < 1
    ):
        raise ValueError("parallel_workers must be a positive integer")
    loader_worker_count = min(effective_parallel_workers, available_cpu_count())
    selected_data_path = _project_path(
        data_path or research_config.get("data_path", "data/options/research/chains.jsonl")
    )
    output_root = _project_path(output_dir or research_config.get("output_dir", "data/options/research/runs"))
    underlying = str(underlying or research_config.get("underlying", "SPY")).strip().upper()
    if not underlying or not underlying.replace(".", "").isalnum():
        raise ValueError("underlying must be a non-empty ticker symbol")
    live_config_path = Path(__file__).resolve().parents[1] / "config.default.json"
    base_config = live_defaults(live_config_path)
    base_config_digest = hashlib.sha256(live_config_path.read_bytes()).hexdigest()
    strategy_configuration = fixed_strategy_configuration(
        strategy, base_config,
        parameters=research_config.get("strategy_defaults", {}).get(strategy, {}),
    )
    code_digest = _digest_files([
        Path(__file__), Path(__file__).with_name("simulator.py"),
        Path(__file__).with_name("dataset.py"), Path(__file__).with_name("history_store.py"),
        Path(__file__).with_name("strategy_configuration.py"),
        Path(__file__).with_name("validation.py"), Path(__file__).with_name("execution.py"),
        Path(__file__).with_name("strategy_factory.py"), Path(__file__).with_name("reporting.py"),
        Path(__file__).parents[1] / "data.py", Path(__file__).parents[1] / "pricing.py",
        Path(__file__).parents[1] / "models.py", Path(__file__).parents[1] / "structures.py",
        Path(__file__).parents[1] / "assignment.py", live_config_path,
        ROOT / "strategies/options/vrp.py", ROOT / "strategies/options/llm_driven.py",
        ROOT / "strategies/options/directional_overlay.py", ROOT / "strategies/options/long_volatility.py",
        ROOT / "strategies/options/base.py", ROOT / "strategies/llm_driven/models.py",
    ])
    section = deepcopy(base_config[strategy])
    section.update(research_config.get("strategy_defaults", {}).get(strategy, {}))
    section["underlying"] = underlying
    if strategy in {"long_volatility", "directional_overlay"}:
        section["underlyings"] = [underlying]
    config_digest = hashlib.sha256(json.dumps({
        "research_config": research_config,
        "selected_underlying": underlying,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    timestamp = datetime.now(timezone.utc)
    source_digest = _dataset_digest(selected_data_path, underlying) if selected_data_path.is_file() else "missing"
    run_id = (
        f"{strategy}-{underlying.lower()}-{timestamp:%Y%m%dT%H%M%S%fZ}-"
        f"{source_digest[:10]}-{config_digest[:8]}"
    )
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    run_start = {
        "run_id": run_id, "created_at": timestamp.isoformat(), "strategy": strategy,
        "underlying": underlying, "research_config_sha256": config_digest,
        "live_base_config_sha256": base_config_digest, "code_sha256": code_digest,
        "dataset_path": str(selected_data_path), "dataset_sha256": source_digest,
        "status": "started",
    }
    atomic_write_json(
        run_dir / "run.json", {**run_start, "research_config": research_config},
        indent=2, sort_keys=True, allow_nan=False,
    )
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
            workers=loader_worker_count, lazy_chains=replay_only,
        )
        date_range = research_config.get("date_range") or {}
        if date_range.get("start") or date_range.get("end"):
            range_start = date.fromisoformat(date_range["start"]) if date_range.get("start") else None
            range_end = date.fromisoformat(date_range["end"]) if date_range.get("end") else None

            def _as_day(value):
                return value.date() if isinstance(value, datetime) else value

            kept = tuple(
                item for item in dataset.observations
                if (range_start is None or _as_day(item.as_of) >= range_start)
                and (range_end is None or _as_day(item.as_of) <= range_end)
            )
            print(
                f"Options research {strategy}/{underlying}: date range {range_start or 'start'} to "
                f"{range_end or 'end'} keeps {len(kept)} of {len(dataset.observations)} observations",
                flush=True,
            )
            dataset = dataclasses.replace(dataset, observations=kept)

        database_source = selected_data_path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        read_stage = "SQLite read" if database_source else "JSONL parse/normalize"
        read_seconds = dataset.loading_parallelism.get(
            "database_read_elapsed_seconds" if database_source
            else "jsonl_parse_and_normalize_elapsed_seconds", 0,
        )
        print(
            f"Options research {strategy}/{underlying}: loading complete in "
            f"{dataset.loading_parallelism.get('elapsed_seconds', 0)}s total; "
            f"{read_stage} {read_seconds}s, "
            f"chain preparation {dataset.loading_parallelism.get('chain_preparation_elapsed_seconds', 0)}s "
            f"({dataset.loading_parallelism.get('chain_workers_used', 1)} workers)",
            flush=True,
        )

        folds = None
        split = None
        split_error = None
        if not replay_only:
            try:
                folds, split = make_purged_walk_forward(
                    dataset.observations, strategy=strategy,
                    strategy_section=section, config=research_config,
                )
            except ValueError as exc:
                split_error = exc
        if replay_only or split_error is not None:
            short_history = research_config.get("short_history_exploration", {})
            minimum_sessions = int(short_history.get("minimum_sessions", 60))
            can_replay = replay_only or (
                short_history.get("enabled", False)
                and split_error is not None
                and "insufficient history for" in str(split_error)
                and len(dataset.observations) >= minimum_sessions
            )
            if not can_replay:
                if split_error is not None:
                    raise split_error
                raise ValueError("insufficient history for replay-only research")
            if len(dataset.observations) < 2:
                raise ValueError("replay-only research requires at least two observations")
            replay = simulate_options_strategy(
                dataset, dataset.observations, strategy_name=strategy,
                strategy_configuration=strategy_configuration,
                config=research_config, base_config=base_config, force_close=True,
            )
            replay_summary = _aggregate_runs(
                [replay], starting_equity=float(research_config.get("starting_equity", 100000)),
                gamma_vega_threshold=float(
                    research_config.get("robustness", {}).get("gamma_vega_episode_share_threshold", 0.60)
                ),
            )
            report = {
                "run_id": run_id, "created_at": timestamp.isoformat(),
                "strategy": strategy, "underlying": underlying,
                "status": "exploratory_only",
                "research_tier": "fixed_configuration_full_history_replay",
                "dataset_path": str(dataset.source_path), "run_dir": str(run_dir),
                "dataset_sha256": dataset.source_sha256,
                "dataset_observations": len(dataset.observations),
                "dataset_chain_errors": _dataset_chain_errors(dataset),
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
                    "dataset_sharing": "not_applicable_full_history_replay",
                },
                "research_config_path": str(config_path_resolved),
                "research_config_sha256": config_digest,
                "live_base_config_sha256": base_config_digest,
                "code_sha256": code_digest,
                "random_seed": int(research_config.get("random_seed", 1)),
                "split": {
                    "standard_validation_available": False,
                    "available_sessions": len(dataset.observations),
                    "standard_validation_requirement": (
                        "replay_only was requested" if replay_only else str(split_error)
                    ),
                },
                "strategy_configuration": {
                    "mode": "fixed_live_defaults",
                    "parameters": strategy_configuration.parameters,
                    "allowed_regimes": list(strategy_configuration.allowed_regimes),
                },
                "folds": [],
                "exploratory_replay": {
                    "period_start": dataset.observations[0].as_of.isoformat(),
                    "period_end": dataset.observations[-1].as_of.isoformat(),
                    "profile": "checked-in live defaults",
                    "metrics": replay_summary["metrics"],
                    "regime_breakdown": replay_summary["regime_breakdown"],
                    "volatility_breakdown": replay_summary["volatility_breakdown"],
                    "greek_summary": replay_summary["greek_summary"],
                    "equity_curve": replay_summary["equity_curve"],
                    "trades": replay_summary["trades"],
                },
                "walk_forward": {},
                "final_parameters": strategy_configuration.parameters,
                "final_allowed_regimes": list(strategy_configuration.allowed_regimes),
                "robustness": {},
                "validation": {
                    "approved": False,
                    "promotion_eligible": False,
                    "status": "descriptive_only",
                    "reason": "This replay uses fixed live defaults and does not select or promote parameters.",
                },
                "data_assumptions": {
                    "historical_chain_snapshots_required_for_empirical_claims": True,
                    "model_or_synthetic_rows_present": dataset.data_mode.startswith("model_surface") or dataset.data_mode == "model_or_synthetic_chain_rows",
                    "point_in_time_events_and_signals_only": True,
                },
            }
            write_research_report(run_dir, report)
            _write_equity_csv(run_dir / "exploratory_equity_curve.csv", replay_summary["equity_curve"])
            _write_trades_csv(run_dir / "exploratory_trades.csv", replay_summary["trades"])
            parallelism = report["parallelism"]
            atomic_write_json(run_dir / "run.json", {
                **run_start, "research_config": research_config,
                "research_config_sha256": config_digest,
                "live_base_config_sha256": base_config_digest,
                "code_sha256": code_digest,
                "dataset_path": str(dataset.source_path), "dataset_sha256": dataset.source_sha256,
                "status": "exploratory_only", "validation": report["validation"],
                "parallelism": parallelism,
            }, indent=2, sort_keys=True, allow_nan=False)
            _append_run_log(output_root / "research_runs.jsonl", {
                **run_start, "dataset_sha256": dataset.source_sha256,
                "run_dir": str(run_dir), "status": "exploratory_only",
                "validation": report["validation"], "parallelism": parallelism,
            })
            OptionsResearchArtifactStore().store_run(run_dir, report)
            return report

        chain_errors = _dataset_chain_errors(dataset)
        starting_equity = float(research_config.get("starting_equity", 100000))
        gamma_vega_threshold = float(
            research_config.get("robustness", {}).get("gamma_vega_episode_share_threshold", 0.60)
        )
        fold_worker_count = min(effective_parallel_workers, available_cpu_count(), len(folds))
        if fold_worker_count > 1 and "fork" not in mp.get_all_start_methods():
            fold_worker_count = 1

        global _FOLD_EVALUATION_CONTEXT
        _FOLD_EVALUATION_CONTEXT = {
            "dataset": dataset, "folds": folds, "strategy": strategy,
            "strategy_configuration": strategy_configuration,
            "research_config": research_config, "base_config": base_config,
        }
        try:
            if fold_worker_count > 1:
                with ProcessPoolExecutor(
                    max_workers=fold_worker_count,
                    mp_context=mp.get_context("fork"),
                ) as executor:
                    fold_results = list(executor.map(
                        _evaluate_fold_index, range(len(folds)), chunksize=1,
                    ))
            else:
                fold_results = [_evaluate_fold_index(index) for index in range(len(folds))]
        finally:
            _FOLD_EVALUATION_CONTEXT = {}

        train_runs = [result["train_run"] for result in fold_results]
        oos_runs = [result["oos_run"] for result in fold_results]
        fold_records = []
        for fold, train_run, oos_run in zip(folds, train_runs, oos_runs):
            fold_records.append({
                "fold": fold.__dict__,
                "train_metrics": train_run["metrics"],
                "train_equity_summary": _equity_summary(train_run),
                "oos_metrics": oos_run["metrics"],
                "oos_equity_summary": _equity_summary(oos_run),
            })
        walk_forward = _aggregate_runs(
            oos_runs, starting_equity=starting_equity,
            gamma_vega_threshold=gamma_vega_threshold,
        )
        mean_train_score = statistics.mean(
            float(run["metrics"].get("risk_adjusted_score", -5.0)) for run in train_runs
        ) if train_runs else 0.0
        mean_oos_score = float(walk_forward["metrics"]["risk_adjusted_score"])
        efficiency = mean_oos_score / mean_train_score if mean_train_score > 0 else None
        walk_forward["walk_forward_efficiency"] = efficiency
        walk_forward["mean_train_risk_adjusted_score"] = mean_train_score
        walk_forward["mean_oos_risk_adjusted_score"] = mean_oos_score

        stress_span = dataset.observations[folds[0].oos_start:folds[-1].oos_end]
        stress_run = simulate_options_strategy(
            dataset, stress_span, strategy_name=strategy,
            strategy_configuration=strategy_configuration,
            config=research_config, base_config=base_config, force_close=True,
        )
        sensitivity = []
        slippage_scenarios = research_config.get("robustness", {}).get(
            "slippage_multipliers", [1.0, 1.5, 2.0],
        )
        spread_scenarios = research_config.get("robustness", {}).get(
            "spread_width_multipliers", [1.0, 1.5, 2.0],
        )
        for slippage in slippage_scenarios:
            for spread_width in spread_scenarios:
                run = simulate_options_strategy(
                    dataset, stress_span, strategy_name=strategy,
                    strategy_configuration=strategy_configuration,
                    config=research_config, base_config=base_config,
                    slippage_multiplier=float(slippage),
                    spread_width_multiplier=float(spread_width), force_close=True,
                )
                sensitivity.append({
                    "slippage_multiplier": float(slippage),
                    "spread_width_multiplier": float(spread_width),
                    "metrics": run["metrics"],
                })
        assignment_sensitivity = {
            "enabled": bool(research_config.get("robustness", {}).get("run_assignment_sensitivity", True))
        }
        if assignment_sensitivity["enabled"]:
            without_assignment = simulate_options_strategy(
                dataset, stress_span, strategy_name=strategy,
                strategy_configuration=strategy_configuration,
                config=research_config, base_config=base_config,
                assignment_enabled=False, force_close=True,
            )
            assignment_sensitivity.update({
                "with_assignment": stress_run["metrics"],
                "without_early_assignment": without_assignment["metrics"],
                "pnl_difference_usd": starting_equity * (
                    stress_run["metrics"]["total_return"]
                    - without_assignment["metrics"]["total_return"]
                ),
                "assignment_events": len(stress_run.get("assignment_events", [])),
            })
        reshuffle = _sequence_reshuffle(
            walk_forward["trades"],
            paths=int(research_config.get("robustness", {}).get("monte_carlo_paths", 500)),
            seed=int(research_config.get("random_seed", 1)),
            starting_equity=starting_equity,
        )
        minimum_oos_trades = int(
            research_config.get("walk_forward", {}).get("minimum_oos_trades", 8)
        )
        minimum_stress_slippage = float(
            research_config.get("robustness", {}).get("minimum_stress_slippage_multiplier", 1.5)
        )
        minimum_stress_spread = float(
            research_config.get("robustness", {}).get("minimum_stress_spread_width_multiplier", 1.5)
        )
        stress_case = next((
            item for item in sensitivity
            if item["slippage_multiplier"] >= minimum_stress_slippage
            and item["spread_width_multiplier"] >= minimum_stress_spread
        ), None)
        stress_positive = bool(
            stress_case
            and stress_case["metrics"]["risk_adjusted_score"] > 0
            and stress_case["metrics"]["total_return"] > 0
        )
        metrics = walk_forward["metrics"]
        validation = {
            "approved": False,
            "promotion_eligible": False,
            "status": "fixed_configuration_replay",
            "reason": "Fixed settings are replayed for historical evaluation; no parameter search or promotion runs.",
            "minimum_oos_trades": minimum_oos_trades,
            "observed_oos_trades": metrics["trade_count"],
            "minimum_oos_trades_met": metrics["trade_count"] >= minimum_oos_trades,
            "walk_forward_oos_positive": metrics["risk_adjusted_score"] > 0,
            "stress_slippage_multiplier": minimum_stress_slippage,
            "stress_spread_width_multiplier": minimum_stress_spread,
            "stress_case_positive_after_costs": stress_positive,
            "returns_not_dominated_by_gamma_or_vega_episodes": not (
                metrics["returns_dominated_by_gamma_or_vega_episodes"]
            ),
        }
        report = {
            "run_id": run_id, "created_at": timestamp.isoformat(),
            "strategy": strategy, "underlying": underlying, "status": "completed",
            "research_tier": "fixed_configuration_walk_forward",
            "dataset_path": str(dataset.source_path), "run_dir": str(run_dir),
            "dataset_sha256": dataset.source_sha256,
            "dataset_observations": len(dataset.observations),
            "dataset_chain_errors": chain_errors, "data_mode": dataset.data_mode,
            "research_config_path": str(config_path_resolved),
            "research_config_sha256": config_digest,
            "live_base_config_sha256": base_config_digest, "code_sha256": code_digest,
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
            "strategy_configuration": {
                "mode": "fixed_live_defaults",
                "parameters": strategy_configuration.parameters,
                "allowed_regimes": list(strategy_configuration.allowed_regimes),
            },
            "folds": fold_records,
            "final_parameters": strategy_configuration.parameters,
            "final_allowed_regimes": list(strategy_configuration.allowed_regimes),
            "walk_forward": {
                "metrics": metrics, "oos_metrics": metrics,
                "walk_forward_efficiency": efficiency,
                "mean_train_risk_adjusted_score": mean_train_score,
                "mean_oos_risk_adjusted_score": mean_oos_score,
                "regime_breakdown": walk_forward["regime_breakdown"],
                "volatility_breakdown": walk_forward["volatility_breakdown"],
                "greek_summary": walk_forward["greek_summary"],
                "equity_curve": walk_forward["equity_curve"],
                "trades": walk_forward["trades"],
            },
            "robustness": {
                "slippage_sensitivity": sensitivity,
                "assignment_sensitivity": assignment_sensitivity,
                "trade_sequence_reshuffle": reshuffle,
            },
            "validation": validation,
            "execution_assumptions": stress_run["execution"],
            "data_assumptions": {
                "historical_chain_snapshots_required_for_empirical_claims": True,
                "model_or_synthetic_rows_present": dataset.data_mode.startswith("model_surface") or dataset.data_mode == "model_or_synthetic_chain_rows",
                "model_iv_surface_used": dataset.data_mode.startswith("model_surface"),
                "spot_close_fallback_used": dataset.data_mode.endswith("spot_close_proxy"),
                "point_in_time_events_and_signals_only": True,
            },
        }
        write_research_report(run_dir, report)
        _write_equity_csv(run_dir / "equity_curve.csv", walk_forward["equity_curve"])
        _write_trades_csv(run_dir / "oos_trades.csv", walk_forward["trades"])
        atomic_write_json(run_dir / "run.json", {
            **run_start, "research_config": research_config,
            "research_config_sha256": config_digest,
            "live_base_config_sha256": base_config_digest,
            "code_sha256": code_digest,
            "dataset_path": str(dataset.source_path), "dataset_sha256": dataset.source_sha256,
            "status": "completed", "validation": validation,
            "strategy_configuration": report["strategy_configuration"],
            "parallelism": report["parallelism"],
        }, indent=2, sort_keys=True, allow_nan=False)
        _append_run_log(output_root / "research_runs.jsonl", {
            **run_start, "dataset_sha256": dataset.source_sha256,
            "run_dir": str(run_dir), "status": "completed",
            "validation": validation, "parallelism": report["parallelism"],
        })
        OptionsResearchArtifactStore().store_run(run_dir, report)
        return report
    except Exception as exc:
        failure = {
            **run_start, "status": "preflight_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        atomic_write_json(
            run_dir / "run.json", {**failure, "research_config": research_config},
            indent=2, sort_keys=True, allow_nan=False,
        )
        _append_run_log(output_root / "research_runs.jsonl", failure)
        OptionsResearchArtifactStore().store_run(run_dir, failure)
        raise


__all__ = ["run_research"]
