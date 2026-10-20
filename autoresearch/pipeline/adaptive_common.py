"""Shared argument validation and artifact writing for adaptive research."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from autoresearch.backtest import read_borrow_availability
from market.store import DEFAULT_DB, SQLiteBarStore
from portfolio.config import ALGO_REGIME_CONFIG
from market.regime import load_fred_regime_series
from market.data import data_tier, regular_feed

from ..artifacts.files import _atomic_json
from ..settings import ADJUSTMENT, DEFAULT_OUTPUT_DIR, FEED
from .adaptive_search import DEFAULT_SEARCH_SPACES, _normalize_weights, _search_space, _symbols

def _prepare_args(args) -> None:
    if args.strategy not in DEFAULT_SEARCH_SPACES:
        raise ValueError(f"unsupported adaptive strategy: {args.strategy}")
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("adaptive Algo research requires Plus SIP data")
    args.symbols_resolved = _symbols(args.symbols)
    args.db = str(args.db or DEFAULT_DB)
    args.adjustment = args.adjustment or ADJUSTMENT
    args.regime_macro_series, args.regime_macro_diagnostics = load_fred_regime_series(
        SQLiteBarStore(args.db), refresh=False,
    )
    if getattr(args, "end", None):
        cutoff = date.fromisoformat(str(args.end)[:10]).isoformat()
        args.regime_macro_series = {
            name: [row for row in rows if str(row.get("date", ""))[:10] <= cutoff]
            for name, rows in args.regime_macro_series.items()
        }
    args.regime_config = dict(ALGO_REGIME_CONFIG)
    args.fitness_weights_data = _normalize_weights(args.fitness_weights)
    args.search_space_data = _search_space(args.strategy, args.search_space)
    args.borrow_availability_data = read_borrow_availability(args.borrow_availability)
    if args.search_method not in {"grid", "random", "bayes"}:
        raise ValueError("search method must be grid, random, or bayes")
    if args.candidates < 1 or args.candidates > 512:
        raise ValueError("candidate count must be between 1 and 512")
    args.search_iterations = getattr(args, "search_iterations", 1)
    if (
        isinstance(args.search_iterations, bool)
        or not isinstance(args.search_iterations, int)
        or not 1 <= args.search_iterations <= 20
    ):
        raise ValueError("search iterations must be an integer from 1 to 20")
    if args.min_trades < 0:
        raise ValueError("minimum trade count cannot be negative")
    if hasattr(args, "window_days") and args.window_days < 2:
        raise ValueError("window-days must be at least two trading sessions")
    for name in ("minimum_oos_sharpe", "maximum_average_decay"):
        if hasattr(args, name) and not math.isfinite(getattr(args, name)):
            raise ValueError(f"{name.replace('_', '-')} must be finite")
    if hasattr(args, "purge_sessions") and (
        isinstance(args.purge_sessions, bool) or args.purge_sessions < 0
    ):
        raise ValueError("purge-sessions must be a non-negative session count")
    if hasattr(args, "minimum_positive_fold_fraction") and not (
        math.isfinite(args.minimum_positive_fold_fraction)
        and 0 <= args.minimum_positive_fold_fraction <= 1
    ):
        raise ValueError("minimum-positive-fold-fraction must be in [0, 1]")
    if hasattr(args, "cost_sensitivity_multipliers") and any(
        not math.isfinite(value) or value < 1 for value in args.cost_sensitivity_multipliers
    ):
        raise ValueError("cost-sensitivity multipliers must be finite and at least 1")
    if hasattr(args, "bootstrap_iterations") and (
        isinstance(args.bootstrap_iterations, bool) or args.bootstrap_iterations < 0
    ):
        raise ValueError("bootstrap-iterations must be a non-negative integer")
    for name in ("capital", "slippage_bps", "spread_bps", "commission_per_share", "short_borrow_annual_pct", "unknown_borrow_penalty_bps"):
        value = getattr(args, name)
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if args.capital <= 0:
        raise ValueError("capital must be positive")

def _read_json_file(path: Path) -> tuple[dict | None, bytes | None]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        return (value if isinstance(value, dict) else None), raw
    except (OSError, json.JSONDecodeError):
        return None, None

def _write_report(output_dir: str, strategy: str, label: str, detail: dict, *, summary: dict | None = None) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(output_dir) / strategy
    directory.mkdir(parents=True, exist_ok=True)
    detail_path = directory / f"{now}-{label}.json"
    latest_path = directory / f"latest-{label}.json"
    detail["detail_artifact"] = str(detail_path.resolve())
    detail["latest_detail_artifact"] = str(latest_path.resolve())
    _atomic_json(detail_path, detail)
    _atomic_json(latest_path, detail)
    result = {
        "status": "completed", "stage": detail.get("stage"), "strategy": strategy,
        "detail_artifact": str(detail_path.resolve()),
        "latest_detail_artifact": str(latest_path.resolve()),
    }
    if summary is not None:
        summary["detail_artifact"] = str(detail_path.resolve())
        summary_path = directory / f"{now}-summary.json"
        summary_latest = directory / f"latest-{label}-summary.json"
        _atomic_json(summary_path, summary)
        _atomic_json(summary_latest, summary)
        _atomic_json(directory / "latest-summary.json", summary)
        result["summary_artifact"] = str(summary_path.resolve())
        result["summary"] = summary
    else:
        summary = {
            "schema": "swarm_algo_adaptive_discovery_summary_v1",
            "stage": detail.get("stage"), "strategy": strategy,
            "window": detail.get("window"), "candidate_count": detail.get("candidate_count_evaluated"),
            "selection_status": detail.get("selection_status"),
            "selected_candidate": detail.get("selected_candidate"),
            "fitness": detail.get("selected_candidate", {}).get("fitness"),
            "detail_artifact": str(detail_path.resolve()),
        }
        summary_path = directory / f"{now}-summary.json"
        summary_latest = directory / f"latest-{label}-summary.json"
        _atomic_json(summary_path, summary)
        _atomic_json(summary_latest, summary)
        _atomic_json(directory / "latest-summary.json", summary)
        result["summary_artifact"] = str(summary_path.resolve())
        result["summary"] = summary
    return result
