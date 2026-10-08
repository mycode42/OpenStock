"""Measure how the same Algo replay changes under explicit slippage assumptions."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import requests

from src.algo.backtest import DEFAULT_DB, run_backtest
from src.market_data import regular_feed


METRICS = (
    "final_equity", "net_return_pct", "max_drawdown_pct", "closed_trade_count",
    "win_rate_pct", "profit_factor", "daily_sharpe_annualized",
)


def parse_slippage_grid(value: str) -> list[float]:
    try:
        values = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("Slippage grid must be comma-separated numbers") from exc
    if not values or any(not math.isfinite(item) or item < 0 for item in values):
        raise ValueError("Slippage values must be finite, non-negative numbers")
    if len(set(values)) != len(values):
        raise ValueError("Slippage grid values must be unique")
    return sorted(values)


def run_sensitivity(args) -> dict:
    symbols = list(dict.fromkeys(item.strip().upper() for item in args.symbols.split(",") if item.strip()))
    if not symbols:
        raise ValueError("Provide at least one comma-separated symbol")
    slippages = parse_slippage_grid(args.slippage_grid)
    if args.baseline_slippage_bps not in slippages:
        raise ValueError("The baseline slippage must be included in the slippage grid")

    reports = []
    for slippage in slippages:
        report = run_backtest(argparse.Namespace(
            symbols=",".join(symbols), start=args.start, end=args.end,
            feed=args.feed, adjustment=args.adjustment, capital=args.capital,
            slippage_bps=slippage, profile=args.profile, db=args.db, output=None,
        ))
        reports.append({
            "slippage_bps_each_side": slippage,
            "data_adjustment": report["data_adjustment"],
            "parameter_profile_sha256": report["parameter_profile_sha256"],
            **{key: report[key] for key in METRICS},
        })

    baseline = next(row for row in reports if row["slippage_bps_each_side"] == args.baseline_slippage_bps)
    return {
        "simulator": "swarm_algo_slippage_sensitivity_v1",
        "data_tier_at_run": reports[0].get("data_tier_at_run"), "feed": args.feed,
        "data_adjustment": reports[0]["data_adjustment"], "symbols": symbols,
        "start": args.start, "end": args.end,
        "baseline_slippage_bps_each_side": args.baseline_slippage_bps,
        "profile": args.profile,
        "parameter_profile_sha256": reports[0]["parameter_profile_sha256"],
        "runs": [
            {
                **row,
                "net_return_delta_vs_baseline_pct": round(
                    row["net_return_pct"] - baseline["net_return_pct"], 4,
                ),
                "equity_delta_vs_baseline": round(row["final_equity"] - baseline["final_equity"], 2),
            }
            for row in reports
        ],
        "limitations": [
            "Slippage is a fixed adverse basis-point assumption on market entries and stop-market exits.",
            "Fees, spread variation, latency, market impact, and quote-level execution are not modeled.",
            "BOATS is unsupported here; overnight replay requires a separate session and fill model.",
            "Results are research estimates, not performance forecasts.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the same Algo portfolio replay over a slippage grid")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--feed", choices=["sip"], default=regular_feed())
    parser.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=None)
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--slippage-grid", default="0,2,5,10,20")
    parser.add_argument("--baseline-slippage-bps", type=float, default=5)
    parser.add_argument("--profile", help="optional validated signal-parameter profile JSON")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_sensitivity(args)
    except (ValueError, OSError, requests.RequestException) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
        report["report_file"] = str(output.resolve())
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
