"""Commands for adaptive Algo research and portfolio replay."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

import requests

from src.algo.cpu import available_cpu_count
from src.config import get_algo_runtime_settings

from .adaptive import add_cli_commands as _add_adaptive_commands
from .files import _atomic_json
from .portfolio_simulation import run_coordinated_portfolio
from .settings import DEFAULT_OUTPUT_DIR, LIVE_RESEARCH_STRATEGIES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.algo.research",
        description="Run adaptive Algo discovery, rolling verification, and portfolio replay",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    portfolio = commands.add_parser(
        "portfolio", help="replay adaptive verification folds through the portfolio coordinator",
    )
    portfolio.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_OUTPUT_DIR / "adaptive" / "verification-coordinator"),
    )
    portfolio.add_argument(
        "--strategies", default=",".join(LIVE_RESEARCH_STRATEGIES),
        help="comma-separated active strategy ids",
    )
    portfolio.add_argument("--event-calendar", help="point-in-time historical event calendar CSV")
    portfolio.add_argument(
        "--multi-factor-overlay", action=argparse.BooleanOptionalAction,
        default=get_algo_runtime_settings().algo_multi_factor_enabled,
        help="include the Multi Factor core strategy in this replay",
    )
    portfolio.add_argument(
        "--workers", type=int, default=min(8, available_cpu_count()),
    )
    portfolio.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR / "layers" / "portfolio"),
    )
    portfolio.set_defaults(handler=_portfolio_command)
    _add_adaptive_commands(commands)
    return parser


def _portfolio_command(args) -> dict:
    report = run_coordinated_portfolio(args)
    path = _save_stage(Path(args.output_dir), "portfolio", report)
    return {
        "status": "completed", "stage": report["stage"],
        "artifact": str(path.resolve()), "strategies": report["strategies"],
        "worker_count": report["worker_count"], "fold_summary": report["fold_summary"],
        "research_only": True,
    }


def _save_stage(directory: Path, label: str, report: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now().date().isoformat()
    dated = directory / f"{run_date}-{label}.json"
    latest = directory / "latest.json"
    _atomic_json(dated, report)
    _atomic_json(latest, report)
    return latest


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (ValueError, OSError, requests.RequestException) as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0
