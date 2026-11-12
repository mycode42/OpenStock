"""Nightly options research batch and its standalone report artifacts."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from equity_research.cpu import available_cpu_count
from options.research.artifact_store import OptionsResearchArtifactStore
from options.research.history_store import DEFAULT_DATABASE, OptionsHistoryStore
from options.market_inputs import research_market_inputs
from options.research.dataset import combine_options_research_datasets, load_options_research_dataset
from options.research.runner import RESEARCH_DEFAULTS_PATH, _merge, _project_path, _read_json
from options.research.search import Candidate, candidate_pool
from options.research.simulator import simulate_options_strategy
from options.research.strategy_factory import live_defaults
from runtime.atomic import atomic_write_json
from runtime.paths import ROOT

REPLAY_DATA_WORKER_CAP = 4


ET = ZoneInfo("America/New_York")
SUPPORTED_STRATEGIES = ("vrp", "llm_driven", "directional_overlay", "long_volatility")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _run_portfolio_replay(
    config: dict, *, strategies: list[str], underlyings_by_strategy: dict[str, list[str]],
    data_paths: dict[str, Path], workers: int, progress_callback=None,
) -> tuple[dict | None, dict[str, str], dict[str, dict]]:
    """Load covered symbols sequentially, then replay one shared options account."""
    base_config = live_defaults()
    market_inputs = research_market_inputs(config.get("execution", {}))
    today = datetime.now(timezone.utc).date()
    date_range = config.get("date_range") or {}
    range_start = date.fromisoformat(date_range["start"]) if date_range.get("start") else None
    range_end = date.fromisoformat(date_range["end"]) if date_range.get("end") else None
    datasets = {}
    unavailable = {}
    loading = {}
    requested_symbols = {
        symbol for symbols in underlyings_by_strategy.values() for symbol in symbols
    }
    ordered_symbols = sorted(requested_symbols)
    for symbol_index, symbol in enumerate(ordered_symbols, start=1):
        path = data_paths[symbol]
        if not path.is_file():
            unavailable[symbol] = "dataset file is missing"
            if progress_callback:
                progress_callback({
                    "phase": "dataset_loading", "symbols_done": symbol_index,
                    "symbols_total": len(ordered_symbols), "symbol": symbol,
                })
            continue
        try:
            dataset = load_options_research_dataset(
                path, underlying=symbol,
                rate=market_inputs.rate(today).value,
                dividend_yield=market_inputs.dividend_yield(symbol, today).value,
                data_mode=str(config.get("data_mode", "auto")),
                model_surface_config=config.get("model_surface", {}),
                workers=workers, lazy_chains=True,
            )
            observations = tuple(
                item for item in dataset.observations
                if (range_start is None or item.as_of.date() >= range_start)
                and (range_end is None or item.as_of.date() <= range_end)
            )
            if len(observations) < 2:
                unavailable[symbol] = "fewer than two observations in the selected date range"
                continue
            dataset = dataclasses.replace(dataset, observations=observations)
            datasets[symbol] = dataset
            loading[symbol] = dataset.loading_parallelism
        except (OSError, ValueError) as exc:
            unavailable[symbol] = f"{type(exc).__name__}: {exc}"
        if progress_callback:
            progress_callback({
                "phase": "dataset_loading", "symbols_done": symbol_index,
                "symbols_total": len(ordered_symbols), "symbol": symbol,
                "observations_loaded": sum(len(item.observations) for item in datasets.values()),
            })
    if not datasets:
        return None, unavailable, loading

    covered_by_strategy = {
        strategy: [symbol for symbol in underlyings_by_strategy[strategy] if symbol in datasets]
        for strategy in strategies
    }
    active_strategies = [strategy for strategy in strategies if covered_by_strategy[strategy]]
    candidates = {}
    for strategy in active_strategies:
        pool = candidate_pool(strategy, config, base_config)
        baseline = next((item for item in pool if item.is_baseline), pool[0])
        parameters = _merge(
            config.get("strategy_defaults", {}).get(strategy, {}), baseline.parameters,
        )
        candidates[strategy] = Candidate(
            baseline.candidate_id, parameters, baseline.allowed_regimes,
            baseline.grid_indices, is_baseline=True,
        )
    combined = combine_options_research_datasets(datasets)
    if progress_callback:
        progress_callback({
            "phase": "portfolio_replay", "symbols_done": len(datasets),
            "symbols_total": len(ordered_symbols),
            "sessions_total": len(combined.observations),
        })
    replay = simulate_options_strategy(
        combined, combined.observations,
        strategy_name=active_strategies[0], candidate=candidates[active_strategies[0]],
        strategy_candidates=candidates,
        strategy_underlyings={key: set(covered_by_strategy[key]) for key in active_strategies},
        config=config, base_config=base_config, force_close=True,
        progress_callback=(
            (lambda event: progress_callback({"phase": "portfolio_replay", **event}))
            if progress_callback else None
        ),
    )
    replay.update({
        "period_start": combined.observations[0].as_of.isoformat(),
        "period_end": combined.observations[-1].as_of.isoformat(),
        "underlyings": sorted(datasets),
        "underlyings_by_strategy": covered_by_strategy,
        "data_mode": combined.data_mode,
        "loading_parallelism": loading,
        "starting_equity": float(config.get("starting_equity", 100000)),
    })
    sleeve_metrics = {}
    for strategy in strategies:
        trades = [trade for trade in replay["trades"] if trade.get("strategy") == strategy]
        pnls = [float(trade.get("pnl", 0.0)) for trade in trades]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        sleeve_metrics[strategy] = {
            "trades": len(trades), "net_pnl_usd": round(sum(pnls), 2),
            "win_rate": len(wins) / len(pnls) if pnls else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "underlyings_with_trades": sorted({trade["underlying"] for trade in trades}),
        }
    replay["sleeve_metrics"] = sleeve_metrics
    return replay, unavailable, loading


def _input_fingerprint(paths: dict[str, Path], *, config_hash: str, data_marks: dict[str, str] | None = None) -> str:
    """Fingerprint the inputs so checkpoints are invalidated when research inputs change.

    History databases are written continuously, so they are fingerprinted by their
    content marks rather than file mtime and size.
    """
    metadata = {}
    for symbol, path in sorted(paths.items()):
        if path.suffix == ".sqlite3":
            continue
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            metadata[f"{symbol}:{candidate.name}"] = {
                "path": str(candidate.resolve()), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    payload = json.dumps(
        {"config_sha256": config_hash, "inputs": metadata, "data_marks": data_marks or {}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _return_improvement(baseline: object, tuned: object) -> str:
    """Relative return improvement over the baseline, or the gap in points if the baseline is not positive."""
    if baseline is None or tuned is None:
        return "n/a"
    base, new = float(baseline), float(tuned)
    if base > 0:
        return f"{(new - base) / base:+.1%}"
    return f"n/a (baseline {base:.2%}; {(new - base) * 100:+.2f} pts)"


SIMULATION_LABELS = {"vrp": "Volatility risk premium", "long_volatility": "Long volatility"}


def _simulation_summary(report: dict) -> dict:
    portfolio = report.get("portfolio_replay")
    if portfolio and portfolio.get("metrics"):
        metrics = portfolio["metrics"]
        trades = portfolio.get("trades", [])
        pnls = [float(trade.get("pnl", 0.0)) for trade in trades]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        return {
            "portfolio": {
                "label": "Shared options portfolio",
                "underlyings": len(portfolio.get("underlyings", [])),
                "trades": len(trades),
                "net_pnl_usd": round(sum(pnls), 2),
                "win_rate": len(wins) / len(pnls) if pnls else 0.0,
                "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
                "average_return": float(metrics.get("total_return", 0.0)),
                "median_sharpe": float(metrics.get("annualized_sharpe", 0.0)),
                "worst_drawdown": float(metrics.get("max_drawdown", 0.0)),
                "period_start": portfolio.get("period_start"),
                "period_end": portfolio.get("period_end"),
                "starting_equity": portfolio.get("starting_equity"),
                "sleeve_metrics": portfolio.get("sleeve_metrics", {}),
            },
        }
    """Per-sleeve P/L and KPIs over the full replay history, from the underlyings that have it.

    Every underlying is replayed from its own starting equity, so P/L adds up across underlyings.
    Underlyings without trades have no usable history, so they are left out, not counted as losses.
    """
    sleeves = {}
    for strategy, item in (report.get("strategies") or {}).items():
        if strategy not in SIMULATION_LABELS:
            continue
        replays = []
        for underlying in (item.get("underlyings") or {}).values():
            replay = underlying.get("exploratory_replay") or {}
            # No trades means the underlying has no usable chain history in the dataset; it is not counted.
            if replay.get("metrics") and replay.get("trades"):
                replays.append(replay)
        if not replays:
            sleeves[strategy] = {"label": SIMULATION_LABELS[strategy], "underlyings": 0}
            continue
        pnls = [float(trade.get("pnl", 0.0)) for replay in replays for trade in replay["trades"]]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        returns = sorted(float(replay["metrics"].get("total_return", 0.0)) for replay in replays)
        sharpes = sorted(float(replay["metrics"].get("annualized_sharpe", 0.0)) for replay in replays)
        starts = [str(replay.get("period_start"))[:10] for replay in replays if replay.get("period_start")]
        ends = [str(replay.get("period_end"))[:10] for replay in replays if replay.get("period_end")]
        sleeves[strategy] = {
            "label": SIMULATION_LABELS[strategy],
            "underlyings": len(replays),
            "trades": len(pnls),
            "net_pnl_usd": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
            "average_return": round(sum(returns) / len(returns), 6),
            "median_return": round(returns[len(returns) // 2], 6),
            "median_sharpe": round(sharpes[len(sharpes) // 2], 3),
            "worst_drawdown": round(max(float(replay["metrics"].get("max_drawdown", 0.0)) for replay in replays), 6),
            "period_start": min(starts) if starts else None,
            "period_end": max(ends) if ends else None,
        }
    return sleeves


def _simulation_markdown(summary: dict) -> list[str]:
    lines = ["## Options simulation", ""]
    rows = []
    for item in summary.values():
        if not item.get("trades"):
            rows.append(f"| {item['label']} | {item['underlyings']} | — | — | — | — | — | — | — |")
            continue
        pf = "—" if item["profit_factor"] is None else f"{item['profit_factor']:.2f}"
        rows.append(
            f"| {item['label']} | {item['underlyings']} | {item['trades']} | {item['net_pnl_usd']:+,.0f} | "
            f"{item['win_rate']:.1%} | {pf} | {item['average_return']:+.2%} | {item['median_sharpe']:.2f} | {item['worst_drawdown']:.1%} |"
        )
    periods = [item for item in summary.values() if item.get("period_start")]
    if periods:
        start_equity = periods[0].get("starting_equity")
        equity_label = f"Starting equity: {start_equity:,.0f} USD." if start_equity is not None else ""
        lines.append(f"History: {str(periods[0]['period_start'])[:10]} to {str(periods[0]['period_end'])[:10]}. {equity_label}")
        lines.append("")
    lines.extend([
        "| Portfolio | Covered symbols | Trades | Net P/L (USD) | Win rate | Profit factor | Total return | Annualized Sharpe | Max drawdown |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *rows,
        "",
    ])
    return lines


def _render_markdown(report: dict) -> str:
    dataset_label = (
        f"`{report['dataset_path']}`" if report.get("dataset_path")
        else "; ".join(f"{symbol}: `{path}`" for symbol, path in report.get("dataset_paths", {}).items())
    )
    lines = [
        f"# Options simulation ({report['run_date_et']})",
        "",
        f"Dataset: {dataset_label or 'none'} ({'available' if report['dataset_available'] else 'not available'})  ",
        f"Data mode: `{report.get('data_mode', 'n/a')}`  ",
        "",
    ]
    lines.extend(_simulation_markdown(_simulation_summary(report)))
    portfolio = report.get("portfolio_replay")
    if portfolio:
        lines.extend([
            f"One shared account across {len(portfolio.get('underlyings', []))} covered symbol(s); "
            f"starting equity {float(portfolio.get('starting_equity', 0.0)):,.0f} USD.",
            f"Trades: {portfolio.get('metrics', {}).get('trade_count', 0)}; "
            f"return {float(portfolio.get('metrics', {}).get('total_return', 0.0)):.2%}; "
            f"Sharpe {float(portfolio.get('metrics', {}).get('annualized_sharpe', 0.0)):.3f}; "
            f"max drawdown {float(portfolio.get('metrics', {}).get('max_drawdown', 0.0)):.2%}.",
            "",
        ])
        contributions = portfolio.get("sleeve_metrics", {})
        if contributions:
            lines.extend(["Strategy P/L attribution:", ""])
            lines.extend(
                f"- **{strategy}:** {item.get('trades', 0)} trades, "
                f"{float(item.get('net_pnl_usd', 0.0)):+,.2f} USD"
                for strategy, item in contributions.items()
            )
            lines.append("")
        if report.get("unavailable_symbols"):
            lines.extend(["Symbols without usable history:", ""])
            lines.extend(
                f"- **{symbol}:** {reason}"
                for symbol, reason in report["unavailable_symbols"].items()
            )
            lines.append("")
    eligibility = report.get("eligible_universe")
    if eligibility:
        lines.extend([
            "## Cached eligible universe",
            "",
            f"Rule version: `{eligibility.get('rule_version')}`; as of `{eligibility.get('as_of_date')}`; "
            f"{eligibility.get('eligible_count', 0)} of {eligibility.get('universe_size', 0)} tickers eligible.",
            f"Applied to strategy jobs: **{bool(report.get('eligible_universe_applied'))}**.",
            f"Snapshot: `{eligibility.get('snapshot_location')}`.",
            "",
        ])
        excluded_by_strategy = report.get("selection_excluded_by_strategy", {})
        if excluded_by_strategy:
            lines.extend(["Excluded from strategy runs by the shared cache:", ""])
            lines.extend(
                f"- **{strategy}:** {len(symbols)} ticker(s)"
                for strategy, symbols in excluded_by_strategy.items()
            )
            lines.append("")
    excluded = report.get("excluded_strategies", {})
    if excluded:
        lines.extend(["## Not researched", ""])
        lines.extend(f"- **{name}:** {reason}" for name, reason in excluded.items())
        lines.append("")
    matrix = report.get("underlyings_by_strategy", {})
    if matrix:
        lines.extend(["## Strategy coverage", ""])
        for strategy, symbols in matrix.items():
            lines.append(f"- **{strategy}:** {', '.join(symbols)}")
        lines.append("")
    if report.get("portfolio_replay"):
        lines.extend(["## Parallelism", ""])
        lines.append(
            f"- One sequential portfolio replay; historical-chain preparation used up to "
            f"{report.get('replay_data_workers', 1)} worker(s) per symbol."
        )
        lines.append("")
    for strategy, result in report.get("strategies", {}).items():
        if "portfolio_metrics" in result:
            contribution = result["portfolio_metrics"]
            lines.extend([
                f"## {strategy}", "", f"Status: **{result.get('status')}**",
                f"Shared-account contribution: {contribution.get('trades', 0)} trades, "
                f"{float(contribution.get('net_pnl_usd', 0.0)):+,.2f} USD.", "",
            ])
            continue
        by_underlying = result.get("underlyings") or {result.get("underlying", "configured"): result}
        lines.extend([f"## {strategy}", "", f"Status: **{result.get('status')}**"])
        for underlying, item in by_underlying.items():
            lines.extend(["", f"### {underlying}", "", f"Status: **{item.get('status')}**"])
            if item.get("error"):
                lines.extend(["", f"Error: `{item['error']}`"])
            if item.get("status") == "completed":
                wf = item.get("walk_forward", {})
                lines.extend([
                    "",
                    f"Rolling OOS: trades {wf.get('trade_count', 0)}, return {float(wf.get('total_return', 0.0)):.2%}, Sharpe {float(wf.get('annualized_sharpe', 0.0)):.3f}, max drawdown {float(wf.get('max_drawdown', 0.0)):.2%}  ",
                    f"Parameters: `{json.dumps(item.get('final_parameters', {}), sort_keys=True)}`  ",
                    f"Detailed report: `{item.get('report_path')}`",
                ])
                candidates = item.get("candidate_leaderboard", [])
                if candidates:
                    ranked_candidates = sorted(
                        candidates,
                        key=lambda candidate: (
                            candidate.get("fitness") is not None,
                            float(candidate.get("fitness") or 0.0),
                        ),
                        reverse=True,
                    )
                    display_candidates = ranked_candidates[:5]
                    baseline = next((candidate for candidate in candidates if candidate.get("is_baseline")), None)
                    if baseline and all(
                        candidate.get("candidate_id") != baseline.get("candidate_id")
                        for candidate in display_candidates
                    ):
                        display_candidates.append(baseline)
                    candidate_rows = []
                    for candidate in display_candidates:
                        fitness = candidate.get("fitness")
                        baseline_fitness = candidate.get("baseline_fitness")
                        delta = candidate.get("fitness_delta_vs_baseline")
                        candidate_rows.append([
                            ("Baseline" if candidate.get("is_baseline") else str(candidate.get("candidate_id", ""))),
                            json.dumps(candidate.get("parameters", {}), sort_keys=True),
                            f"{float(fitness):.3f}" if fitness is not None else "n/a",
                            f"{float(baseline_fitness):.3f}" if baseline_fitness is not None else "n/a",
                            f"{float(delta):+.3f}" if delta is not None else "n/a",
                        ])
                    widths = [
                        max(len(header), *(len(row[index]) for row in candidate_rows))
                        for index, header in enumerate([
                            "Candidate", "Parameters", "Fitness", "Baseline", "Delta",
                        ])
                    ]
                    headers = ["Candidate", "Parameters", "Fitness", "Baseline", "Delta"]
                    table = "\n".join([
                        "| " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)) + " |",
                        "| " + " | ".join("-" * widths[index] for index in range(len(headers))) + " |",
                        *[
                            "| " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + " |"
                            for row in candidate_rows
                        ],
                    ])
                    lines.extend([
                        "",
                        "Top five candidates by mean in-sample risk-adjusted score, plus the baseline. Delta is candidate minus baseline; the detailed report has the full candidate ledger.",
                        "",
                        table,
                    ])
                comparison = item.get("baseline_comparison", {})
                comparison_rows = []
                for label, period in (
                    ("Rolling OOS", comparison.get("walk_forward", {})),
                ):
                    baseline_metrics = period.get("baseline_metrics", {})
                    tuned_metrics = period.get("tuned_metrics", {})
                    deltas = period.get("tuned_minus_baseline", {})
                    if not baseline_metrics or not tuned_metrics:
                        continue
                    format_value = lambda value, percent=False: (
                        f"{float(value):.2%}" if percent else f"{float(value):.3f}"
                    ) if value is not None else "n/a"
                    comparison_rows.append([
                        label,
                        format_value(baseline_metrics.get("total_return"), percent=True),
                        format_value(tuned_metrics.get("total_return"), percent=True),
                        format_value(deltas.get("total_return"), percent=True),
                        _return_improvement(baseline_metrics.get("total_return"), tuned_metrics.get("total_return")),
                        format_value(baseline_metrics.get("risk_adjusted_score")),
                        format_value(tuned_metrics.get("risk_adjusted_score")),
                        format_value(deltas.get("risk_adjusted_score")),
                    ])
                if comparison_rows:
                    headers = [
                        "Period", "Baseline return", "Tuned return", "Delta return", "Return improvement",
                        "Baseline score", "Tuned score", "Delta score",
                    ]
                    widths = [
                        max(len(header), *(len(row[index]) for row in comparison_rows))
                        for index, header in enumerate(headers)
                    ]
                    table = "\n".join([
                        "| " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)) + " |",
                        "| " + " | ".join("-" * widths[index] for index in range(len(headers))) + " |",
                        *[
                            "| " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + " |"
                            for row in comparison_rows
                        ],
                    ])
                    lines.extend([
                        "",
                        "Fixed baseline comparison: baseline and tuned use the same dates and execution costs; deltas are tuned minus baseline.",
                        "",
                        table,
                    ])
            elif item.get("status") == "exploratory_only":
                metrics = (item.get("exploratory_replay") or {}).get("metrics", {})
                lines.extend([
                    "",
                    f"Exploratory baseline replay: {metrics.get('trade_count', 0)} trades, {float(metrics.get('total_return', 0.0)):.2%} return, Sharpe {float(metrics.get('annualized_sharpe', 0.0)):.3f}  ",
                    "Replay only: the chosen parameters over the history, with no search. Judge it by its P/L and KPIs.",
                    f"Detailed report: `{item.get('report_path')}`",
                ])
        lines.append("")
    lines.extend([
        "## Data and execution assumptions",
        "",
        "Historical chain snapshots are required for empirical results. A missing dataset skips the strategy runs and is reported as such; a model surface is only used when explicitly represented in the configured dataset. Fills include per-leg spread slippage and commissions. Assignment remains a simplified simulation heuristic.",
        "The eligibility cache and sector/correlation overlays are research-only. They do not change the live portfolio universe or place orders.",
        "",
    ])
    return "\n".join(lines)


def run_automated_options_research(
    *, config_path: str | Path | None = None, workers: int | None = None,
    run_id: str | None = None,
) -> dict:
    """Run the configured options sleeves without touching broker credentials or orders."""
    config_path_resolved = _project_path(config_path) if config_path else RESEARCH_DEFAULTS_PATH
    config = _merge(_read_json(RESEARCH_DEFAULTS_PATH), _read_json(config_path_resolved))
    strategies = [str(item).strip().lower() for item in config.get("automation", {}).get(
        "strategies", ["vrp", "llm_driven", "long_volatility"],
    )]
    if not strategies or any(item not in SUPPORTED_STRATEGIES for item in strategies):
        raise ValueError(f"automation strategies must be selected from {', '.join(SUPPORTED_STRATEGIES)}")
    automation_config = config.get("automation", {})
    default_underlyings = [str(item).strip().upper() for item in automation_config.get(
        "underlyings", [config.get("underlying", "SPY")],
    )]
    if not default_underlyings or any(not item or not item.replace(".", "").isalnum() for item in default_underlyings):
        raise ValueError("automation underlyings must be non-empty ticker symbols")
    default_underlyings = list(dict.fromkeys(default_underlyings))
    configured_by_strategy = automation_config.get("underlyings_by_strategy", {})
    if not isinstance(configured_by_strategy, dict):
        raise ValueError("automation underlyings_by_strategy must be an object")
    underlyings_by_strategy = {}
    for strategy in strategies:
        configured = configured_by_strategy.get(strategy, default_underlyings)
        if not isinstance(configured, list):
            raise ValueError(f"automation underlyings_by_strategy.{strategy} must be a list")
        symbols = [str(item).strip().upper() for item in configured]
        if not symbols or any(not item or not item.replace(".", "").isalnum() for item in symbols):
            raise ValueError(f"automation underlyings for {strategy} must be non-empty ticker symbols")
        underlyings_by_strategy[strategy] = list(dict.fromkeys(symbols))
    underlyings = list(dict.fromkeys(
        symbol for strategy in strategies for symbol in underlyings_by_strategy[strategy]
    ))
    base_data_path = config.get("data_path", "data/options/research/chains.jsonl")
    data_paths = automation_config.get("data_paths", {})
    path_template = automation_config.get("data_path_template")
    underlying_data_paths = {
        symbol: _project_path(
            data_paths.get(symbol)
            or (str(path_template).format(underlying=symbol) if path_template else base_data_path)
        )
        for symbol in underlyings
    }
    configured_fold_workers = config.get("search", {}).get("parallel_workers", 1)
    if isinstance(configured_fold_workers, bool) or not isinstance(configured_fold_workers, int) or configured_fold_workers < 1:
        raise ValueError("search.parallel_workers must be a positive integer")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise ValueError("workers must be a positive integer")
    available_cpus = available_cpu_count()
    replay_data_worker_budget = min(
        configured_fold_workers, REPLAY_DATA_WORKER_CAP, available_cpus,
        workers if workers is not None else REPLAY_DATA_WORKER_CAP,
    )
    workers_requested = workers if workers is not None else configured_fold_workers
    workers_requested_by_strategy = {strategy: 1 for strategy in strategies}
    workers_used_by_strategy = {strategy: 1 for strategy in strategies}
    fold_workers_used_by_strategy = {strategy: 0 for strategy in strategies}
    replay_data_workers_used_by_strategy = {
        strategy: replay_data_worker_budget for strategy in strategies
    }
    output_root = _project_path(config.get("output_dir", "data/options/research/runs"))
    automation_dir = output_root.parent / "simulation"
    automation_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    fingerprint_paths = dict(underlying_data_paths)
    eligibility_fingerprint_config = automation_config.get("eligibility", {})
    configured_rules_path = (
        eligibility_fingerprint_config.get("rules_path")
        if isinstance(eligibility_fingerprint_config, dict) else None
    )
    if configured_rules_path:
        fingerprint_paths["__eligibility_rules__"] = _project_path(configured_rules_path)
    data_marks = {
        symbol: OptionsHistoryStore(path, initialize=False).latest_marks(symbol)
        for symbol, path in underlying_data_paths.items()
        if path.suffix == ".sqlite3" and path.exists()
    }
    input_fingerprint = _input_fingerprint(fingerprint_paths, config_hash=config_hash, data_marks=data_marks)
    run_id = run_id or created_at.strftime("options-%Y%m%dT%H%M%S%fZ")
    checkpoint_store = OptionsHistoryStore(
        _project_path(automation_config.get("checkpoint_database") or DEFAULT_DATABASE)
    )
    checkpoint = checkpoint_store.read_automation_checkpoint(run_id) or {}
    if (
        checkpoint.get("schema") != "swarm_options_automation_checkpoint_v2"
        or checkpoint.get("run_id") != run_id
        or checkpoint.get("config_sha256") != config_hash
        or checkpoint.get("input_fingerprint") != input_fingerprint
    ):
        checkpoint = {
            "schema": "swarm_options_automation_checkpoint_v2",
            "run_id": run_id,
            "config_sha256": config_hash,
            "input_fingerprint": input_fingerprint,
            "created_at": created_at.isoformat(),
            "jobs": {},
        }
    checkpoint["updated_at"] = created_at.isoformat()
    enabled = bool(config.get("automation", {}).get("enabled", True))
    configured_exclusions = automation_config.get("excluded_strategies", {})
    if not isinstance(configured_exclusions, dict):
        raise ValueError("automation excluded_strategies must be an object")
    excluded_strategies = {str(name): str(reason) for name, reason in configured_exclusions.items()}
    strategies_report: dict[str, dict] = {}
    archive_merge = {
        symbol: {
            "underlying": symbol,
            "dataset_path": str(underlying_data_paths[symbol]),
            "skipped": True,
            "reason": "collectors write directly to the SQLite history database",
        }
        for symbol in underlyings
    }
    eligibility_config = automation_config.get("eligibility", {})
    if not isinstance(eligibility_config, dict):
        raise ValueError("automation.eligibility must be an object")
    sqlite_database_paths = {
        path.resolve() for path in underlying_data_paths.values()
        if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
    }
    if len(sqlite_database_paths) > 1:
        raise ValueError("all options research tickers must share one SQLite history database")
    uses_sqlite_datasets = bool(sqlite_database_paths)
    apply_eligibility = bool(eligibility_config.get(
        "apply_to_research", eligibility_config.get("enabled", False),
    ))
    cache_eligibility = bool(eligibility_config.get("cache_enabled", True)) and uses_sqlite_datasets
    if apply_eligibility and not uses_sqlite_datasets:
        raise ValueError("automation.eligibility.apply_to_research requires the SQLite history store")
    uses_history_store = any(
        path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        for path in underlying_data_paths.values()
    ) or cache_eligibility or apply_eligibility
    history_store = None
    if uses_history_store:
        history_store = (
            OptionsHistoryStore(next(iter(sqlite_database_paths)))
            if sqlite_database_paths else OptionsHistoryStore()
        )
    history_store_sync = {
        symbol: {
            "underlying": symbol,
            "status": "database_ready" if history_store.has_underlying(symbol) else "waiting_for_data",
            "coverage": history_store.coverage_summary(symbol),
        }
        for symbol in underlyings
    } if history_store else {}
    eligibility_report = None
    selection_excluded_by_strategy = {}
    if cache_eligibility or apply_eligibility:
        from options.research.universe_selection import build_eligible_cache

        eligibility_report = build_eligible_cache(
            database=history_store.path,
            rules_path=eligibility_config.get("rules_path"),
        )
        eligible = set(eligibility_report.get("eligible_symbols", []))
        if apply_eligibility:
            original_by_strategy = underlyings_by_strategy
            underlyings_by_strategy = {
                strategy: [symbol for symbol in symbols if symbol in eligible]
                for strategy, symbols in original_by_strategy.items()
            }
            selection_excluded_by_strategy = {
                strategy: [symbol for symbol in symbols if symbol not in eligible]
                for strategy, symbols in original_by_strategy.items()
            }
    data_available = any(
        history_store.has_underlying(symbol)
        if history_store and path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        else path.is_file()
        for symbol, path in underlying_data_paths.items()
    )
    data_mode = config.get("data_mode", "auto")

    portfolio_replay = None
    unavailable_symbols = {}
    portfolio_loading = {}
    portfolio_error = None
    if not enabled:
        status = "disabled"
        strategies_report = {strategy: {"status": "disabled"} for strategy in strategies}
    else:
        run_progress_dir = automation_dir / "runs" / run_id
        run_progress_dir.mkdir(parents=True, exist_ok=True)

        def record_portfolio_progress(detail: dict) -> None:
            snapshot = {
                "schema": "swarm_options_automation_progress_v2",
                "run_id": run_id,
                "status": "running",
                "created_at": checkpoint.get("created_at"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "completed_jobs": 0,
                "total_jobs": 1,
                "pending_jobs": ["portfolio_replay"],
                "phase": detail.get("phase"),
                "progress": detail,
                "checkpoint_location": f"{checkpoint_store.path}#{run_id}",
            }
            atomic_write_json(run_progress_dir / "progress.json", snapshot, indent=2, sort_keys=True, allow_nan=False)
            atomic_write_json(run_progress_dir / "partial-report.json", snapshot, indent=2, sort_keys=True, allow_nan=False)
            atomic_write_json(automation_dir / "latest-progress.json", snapshot, indent=2, sort_keys=True, allow_nan=False)

        record_portfolio_progress({"phase": "starting"})
        try:
            portfolio_replay, unavailable_symbols, portfolio_loading = _run_portfolio_replay(
                config, strategies=strategies, underlyings_by_strategy=underlyings_by_strategy,
                data_paths=underlying_data_paths, workers=replay_data_worker_budget,
                progress_callback=record_portfolio_progress,
            )
        except Exception as exc:
            portfolio_error = f"{type(exc).__name__}: {exc}"
        if portfolio_error:
            status = "partial_error"
            strategies_report = {strategy: {"status": "error", "error": portfolio_error} for strategy in strategies}
        elif portfolio_replay is None:
            if apply_eligibility and not any(underlyings_by_strategy.values()):
                status = "eligibility_empty"
                strategies_report = {strategy: {"status": "skipped_eligibility_empty"} for strategy in strategies}
            else:
                status = "waiting_for_data"
                strategies_report = {
                    strategy: {
                        "status": "skipped_missing_dataset",
                        "underlyings": {
                            symbol: {"status": "skipped_missing_dataset", "error": unavailable_symbols.get(symbol)}
                            for symbol in underlyings_by_strategy[strategy]
                        },
                    }
                    for strategy in strategies
                }
        else:
            status = "partial_data" if unavailable_symbols else "completed"
            for strategy in strategies:
                strategies_report[strategy] = {
                    "status": (
                        "skipped_eligibility_empty"
                        if apply_eligibility and not portfolio_replay["underlyings_by_strategy"].get(strategy)
                        else status
                    ),
                    "covered_underlyings": portfolio_replay["underlyings_by_strategy"].get(strategy, []),
                    "portfolio_metrics": portfolio_replay["sleeve_metrics"].get(strategy, {}),
                }
        checkpoint = {
            "schema": "swarm_options_automation_checkpoint_v2",
            "run_id": run_id,
            "config_sha256": config_hash,
            "input_fingerprint": input_fingerprint,
            "created_at": checkpoint.get("created_at", created_at.isoformat()),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "completed_jobs": 1 if portfolio_replay is not None or portfolio_error else 0,
            "total_jobs": 1,
            "portfolio_replay": {
                "status": status,
                "trade_count": (portfolio_replay or {}).get("metrics", {}).get("trade_count", 0),
                "underlyings": (portfolio_replay or {}).get("underlyings", []),
            },
            "unavailable_symbols": unavailable_symbols,
            "portfolio_error": portfolio_error,
        }
        checkpoint_store.write_automation_checkpoint(run_id, checkpoint)
        progress_snapshot = {
            "schema": "swarm_options_automation_progress_v2",
            "run_id": run_id,
            "status": status,
            "created_at": checkpoint["created_at"],
            "updated_at": checkpoint["updated_at"],
            "completed_jobs": checkpoint["completed_jobs"],
            "total_jobs": 1,
            "pending_jobs": [],
            "portfolio_replay": checkpoint["portfolio_replay"],
            "unavailable_symbols": unavailable_symbols,
            "checkpoint_location": f"{checkpoint_store.path}#{run_id}",
        }
        run_progress_dir = automation_dir / "runs" / run_id
        run_progress_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(run_progress_dir / "progress.json", progress_snapshot, indent=2, sort_keys=True, allow_nan=False)
        atomic_write_json(run_progress_dir / "partial-report.json", progress_snapshot, indent=2, sort_keys=True, allow_nan=False)
        atomic_write_json(automation_dir / "latest-progress.json", progress_snapshot, indent=2, sort_keys=True, allow_nan=False)
        print(
            f"Options portfolio replay: {len((portfolio_replay or {}).get('underlyings', []))} covered symbols, "
            f"one shared account, sequential replay; historical-chain preparation workers {replay_data_worker_budget}",
            flush=True,
        )

    today = datetime.now(ET).date().isoformat()
    report = {
        "schema": "swarm_options_nightly_research_v1",
        "simulation_model": "shared_portfolio_replay_v1",
        "status": status,
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "run_date_et": today,
        "config_path": str(config_path_resolved),
        "config_sha256": config_hash,
        "dataset_path": str(underlying_data_paths.get(underlyings[0])) if len(underlyings) == 1 else None,
        "dataset_paths": {symbol: str(path) for symbol, path in underlying_data_paths.items()},
        "dataset_available": data_available,
        "underlyings_by_strategy": underlyings_by_strategy,
        "selection_excluded_by_strategy": selection_excluded_by_strategy,
        "eligible_universe": eligibility_report,
        "eligible_universe_applied": apply_eligibility,
        "excluded_strategies": excluded_strategies,
        "archive_merge": {
            symbol: {
                "updated": not bool(result.get("skipped")),
                "combined_sessions": None,
                "reason": result.get("reason"),
            }
            for symbol, result in archive_merge.items()
        },
        "history_store_sync": {
            symbol: {
                "status": result.get("status"),
                "coverage": result.get("coverage"),
            }
            for symbol, result in history_store_sync.items()
        },
        "data_mode": portfolio_replay.get("data_mode", data_mode) if portfolio_replay else data_mode,
        "workers_requested": workers_requested,
        "workers_used": 1 if enabled and portfolio_replay is not None else 0,
        "replay_data_workers": replay_data_worker_budget if enabled else 0,
        "workers_requested_by_strategy": workers_requested_by_strategy,
        "workers_used_by_strategy": ({strategy: 1 for strategy in strategies} if enabled and portfolio_replay else {}),
        "fold_workers_requested": configured_fold_workers,
        "fold_workers_used_by_strategy": ({strategy: 0 for strategy in strategies} if enabled else {}),
        "replay_data_workers_used_by_strategy": ({strategy: replay_data_worker_budget for strategy in strategies} if enabled else {}),
        "portfolio_replay": portfolio_replay,
        "portfolio_error": portfolio_error,
        "unavailable_symbols": unavailable_symbols,
        "portfolio_loading": portfolio_loading,
        "strategies": strategies_report,
        "research_only": True,
        "trade_or_order_actions": 0,
        "checkpoint_location": f"{checkpoint_store.path}#{run_id}",
    }
    # Per-sleeve P/L and KPIs, stored with the report so the research history can show them without replaying.
    report["simulation_kpis"] = _simulation_summary(report)
    report_path = automation_dir / f"{today}.json"
    atomic_write_json(report_path, report, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(automation_dir / "latest.json", report, indent=2, sort_keys=True, allow_nan=False)
    markdown_path = automation_dir / f"{today}.md"
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    (automation_dir / "latest.md").write_text(_render_markdown(report), encoding="utf-8")
    _append_jsonl(automation_dir / "runs.jsonl", {
        "run_id": run_id,
        "created_at": created_at.isoformat(), "run_date_et": today,
        "status": status, "config_sha256": config_hash,
        "checkpoint_location": f"{checkpoint_store.path}#{run_id}",
        "workers_requested": workers_requested,
        "workers_used": 1 if enabled and portfolio_replay is not None else 0,
        "replay_data_workers": replay_data_worker_budget if enabled else 0,
        "workers_requested_by_strategy": workers_requested_by_strategy,
        "workers_used_by_strategy": workers_used_by_strategy if enabled else {},
        "fold_workers_requested": configured_fold_workers,
        "fold_workers_used_by_strategy": fold_workers_used_by_strategy if enabled else {},
        "replay_data_workers_used_by_strategy": replay_data_workers_used_by_strategy if enabled else {},
        "underlyings_by_strategy": underlyings_by_strategy,
        "eligible_universe": ({
            "status": eligibility_report.get("status"),
            "as_of_date": eligibility_report.get("as_of_date"),
            "eligible_count": eligibility_report.get("eligible_count"),
            "universe_size": eligibility_report.get("universe_size"),
            "snapshot_location": eligibility_report.get("snapshot_location"),
            "rules_sha256": eligibility_report.get("rules_sha256"),
            "applied": apply_eligibility,
        } if eligibility_report else None),
        "dataset_paths": {symbol: str(path) for symbol, path in underlying_data_paths.items()}, "dataset_available": data_available,
        "strategies": {
            name: {
                "status": result.get("status"),
                "error": result.get("error"),
                "covered_underlyings": result.get("covered_underlyings", []),
                "portfolio_metrics": result.get("portfolio_metrics"),
            }
            for name, result in strategies_report.items()
        },
        "portfolio_replay": {
            "trade_count": (portfolio_replay or {}).get("metrics", {}).get("trade_count", 0),
            "total_return": (portfolio_replay or {}).get("metrics", {}).get("total_return"),
            "underlyings": (portfolio_replay or {}).get("underlyings", []),
        },
        "unavailable_symbols": unavailable_symbols,
        "report_path": str(report_path),
    })
    artifact_store = OptionsResearchArtifactStore()
    for document_type, identity, path in (
        ("automation_report", today, report_path),
        ("automation_report_latest", "latest", automation_dir / "latest.json"),
        ("automation_markdown", today, markdown_path),
        ("automation_markdown_latest", "latest", automation_dir / "latest.md"),
        ("automation_history", "runs", automation_dir / "runs.jsonl"),
    ):
        artifact_store.store_document(document_type=document_type, identity=identity, path=path)
    final_path = automation_dir / "runs" / run_id / "final-report.json"
    atomic_write_json(final_path, report, indent=2, sort_keys=True, allow_nan=False)
    checkpoint.update({
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str(final_path.resolve()),
        "completed_jobs": 1 if portfolio_replay is not None or portfolio_error else 0,
    })
    checkpoint_store.write_automation_checkpoint(run_id, checkpoint)
    final_progress = {
        "schema": "swarm_options_automation_progress_v2",
        "run_id": run_id,
        "status": checkpoint["status"],
        "created_at": checkpoint.get("created_at"),
        "updated_at": checkpoint["completed_at"],
        "completed_jobs": checkpoint.get("completed_jobs", 0),
        "total_jobs": checkpoint.get("total_jobs", 0),
        "pending_jobs": [],
        "report_path": str(final_path.resolve()),
        "checkpoint_location": f"{checkpoint_store.path}#{run_id}",
    }
    progress_dir = automation_dir / "runs" / run_id
    atomic_write_json(progress_dir / "progress.json", final_progress, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(progress_dir / "partial-report.json", report, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(automation_dir / "latest-progress.json", final_progress, indent=2, sort_keys=True, allow_nan=False)
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run configured options research sleeves")
    parser.add_argument("--config", help="options research config override")
    parser.add_argument("--workers", type=int, help="historical-chain preparation workers (maximum 4)")
    parser.add_argument("--run-id", help="stable cycle ID used to resume an interrupted research batch")
    args = parser.parse_args()
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    report = run_automated_options_research(
        config_path=args.config, workers=args.workers, run_id=args.run_id,
    )
    print(json.dumps({
        "status": report["status"], "report": str((ROOT / "data/options/research/simulation/latest.json").resolve()),
        "strategies": {name: result.get("status") for name, result in report["strategies"].items()},
        "dataset_available": report["dataset_available"],
        "workers_used": report["workers_used"],
    }, sort_keys=True))
    return 1 if report.get("status") == "partial_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
