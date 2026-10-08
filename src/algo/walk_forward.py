"""Chronological research-only profile selection and out-of-sample replay."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from src.algo.backtest import AlgoPortfolioBacktest, DAILY_WARMUP_DAYS
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.market_data import data_tier, regular_feed
from src.strategies.registry import validate_parameter_profile


ET = ZoneInfo("America/New_York")
METRICS = (
    "net_return_pct", "max_drawdown_pct", "closed_trade_count", "win_rate_pct",
    "profit_factor", "daily_sharpe_annualized", "final_equity",
)


def walk_forward_windows(
    trading_dates: list[date], *, train_days: int, test_days: int, step_days: int,
) -> list[dict[str, str | int]]:
    """Return rolling, chronological windows; the final test window may be shorter."""
    if min(train_days, test_days, step_days) < 1:
        raise ValueError("train_days, test_days, and step_days must be positive")
    if step_days < test_days:
        raise ValueError("step_days must be at least test_days so out-of-sample folds do not overlap")
    dates = sorted(set(trading_dates))
    windows = []
    test_index = train_days
    while test_index < len(dates):
        train = dates[max(0, test_index - train_days):test_index]
        test = dates[test_index:test_index + test_days]
        if not train or not test:
            break
        windows.append({
            "train_start": train[0].isoformat(), "train_end": train[-1].isoformat(),
            "test_start": test[0].isoformat(), "test_end": test[-1].isoformat(),
            "train_session_count": len(train), "test_session_count": len(test),
        })
        test_index += step_days
    return windows


def _read_profiles(path: str) -> list[dict]:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list) or not payload:
        raise ValueError("Profiles file must contain a non-empty JSON array")
    output = []
    names = set()
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Each profile requires a string name and parameters object")
        name = item["name"].strip()
        if not name or name in names:
            raise ValueError("Profile names must be non-empty and unique")
        names.add(name)
        parameters = validate_parameter_profile(item.get("parameters", {}))
        output.append({"name": name, "parameters": parameters})
    if not any(not profile["parameters"] for profile in output):
        output.insert(0, {"name": "baseline", "parameters": {}})
    return output


def _metrics(report: dict) -> dict:
    return {key: report.get(key) for key in METRICS}


def _selection_score(report: dict) -> float:
    # A simple, visible train-only score. A drawdown penalty discourages selecting
    # a high-return profile that took materially more risk in the fit window.
    return float(report["net_return_pct"]) - 0.5 * float(report["max_drawdown_pct"])


def _run_portfolio(
    *, symbols: list[str], intraday: dict, spy_daily: list[dict], start: str, end: str,
    feed: str, adjustment: str, capital: float, slippage_bps: float, profile: dict,
) -> dict:
    return AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy_daily,
        start=start, end=end, feed=feed, data_adjustment=adjustment,
        initial_capital=capital, slippage_bps=slippage_bps, profile=profile,
    ).run()


def run_walk_forward(args) -> dict:
    symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(",") if s.strip()))
    if not symbols:
        raise ValueError("Provide at least one comma-separated symbol")
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end < start:
        raise ValueError("End date must not be before start date")
    if args.min_train_trades < 0 or args.max_train_drawdown_pct < 0:
        raise ValueError("Training eligibility limits must be non-negative")

    store = SQLiteBarStore(args.db)
    intraday = store.bars(
        symbols, feed=args.feed, timeframe="5Min", start=args.start, end=args.end,
        adjustment=args.adjustment,
    )
    missing = [symbol for symbol in symbols if not intraday.get(symbol)]
    if missing:
        raise ValueError(f"No stored 5-minute {args.feed} bars for: {', '.join(missing)}")
    daily_start = (start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat()
    spy_daily = store.bars(
        ["SPY"], feed=args.feed, timeframe="1Day", start=daily_start, end=args.end,
        adjustment=args.adjustment,
    ).get("SPY", [])

    seed = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy_daily,
        start=args.start, end=args.end, feed=args.feed, data_adjustment=args.adjustment,
        initial_capital=args.capital, slippage_bps=args.slippage_bps, profile={},
    )
    trading_dates = sorted({timestamp.astimezone(ET).date() for timestamp in seed._timeline()})
    windows = walk_forward_windows(
        trading_dates, train_days=args.train_days,
        test_days=args.test_days, step_days=args.step_days,
    )
    if not windows:
        raise ValueError(
            f"Need more sessions than the {args.train_days}-session training window; "
            f"found {len(trading_dates)}"
        )
    profiles = _read_profiles(args.profiles)
    folds = []
    for window in windows:
        train_runs = []
        for candidate in profiles:
            report = _run_portfolio(
                symbols=symbols, intraday=intraday, spy_daily=spy_daily,
                start=window["train_start"], end=window["train_end"], feed=args.feed,
                adjustment=args.adjustment, capital=args.capital,
                slippage_bps=args.slippage_bps, profile=candidate["parameters"],
            )
            train_runs.append({
                "name": candidate["name"], "parameters": candidate["parameters"],
                "score": round(_selection_score(report), 6), "metrics": _metrics(report),
                "eligible": (
                    report["closed_trade_count"] >= args.min_train_trades
                    and report["max_drawdown_pct"] <= args.max_train_drawdown_pct
                ),
            })
        eligible = [item for item in train_runs if item["eligible"]]
        baseline = next(item for item in train_runs if not item["parameters"])
        selected = max(eligible, key=lambda item: (item["score"], -item["metrics"]["max_drawdown_pct"])) if eligible else baseline

        test_report = _run_portfolio(
            symbols=symbols, intraday=intraday, spy_daily=spy_daily,
            start=window["test_start"], end=window["test_end"], feed=args.feed,
            adjustment=args.adjustment, capital=args.capital,
            slippage_bps=args.slippage_bps, profile=selected["parameters"],
        )
        folds.append({
            **window, "selected_profile": selected["name"],
            "selected_parameters": selected["parameters"],
            "eligible_training_profiles": len(eligible),
            "training_candidates": train_runs,
            "out_of_sample": _metrics(test_report),
        })

    test_returns = [fold["out_of_sample"]["net_return_pct"] for fold in folds]
    return {
        "simulator": "swarm_algo_walk_forward_v1",
        "data_tier_at_run": data_tier(), "feed": args.feed,
        "data_adjustment": args.adjustment, "symbols": symbols,
        "universe_model": "fixed supplied symbol list for every train and test window",
        "start": args.start, "end": args.end,
        "trading_session_count": len(trading_dates),
        "candidate_profile_count": len(profiles),
        "selection_rule": (
            "Among profiles meeting minimum training trade count and maximum training drawdown, "
            "maximize training net_return_pct - 0.5 * max_drawdown_pct; otherwise use baseline."
        ),
        "fold_summary": {
            "count": len(folds), "positive_out_of_sample_folds": sum(value > 0 for value in test_returns),
            "mean_out_of_sample_return_pct": round(statistics.mean(test_returns), 4),
            "median_out_of_sample_return_pct": round(statistics.median(test_returns), 4),
        },
        "folds": folds,
        "limitations": [
            "Profiles are selected from a supplied, bounded candidate list; this does not generate new parameters.",
            "Each fold is replayed as a flat portfolio with fresh capital; positions and risk state do not carry across fold boundaries.",
            "Universe membership is fixed for all folds; historical point-in-time selections are not available.",
            "LLM advice and overnight sessions are omitted; free IEX data has incomplete venue coverage.",
            "This report cannot update live settings and is not a performance forecast.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chronological Algo profile selection and out-of-sample replay")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--profiles", required=True, help="JSON array of named signal parameter profiles")
    parser.add_argument("--feed", choices=["iex", "sip"], default=regular_feed())
    parser.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default="raw")
    parser.add_argument("--train-days", type=int, default=30, help="number of prior trading sessions per fit window")
    parser.add_argument("--test-days", type=int, default=10, help="number of later trading sessions per holdout")
    parser.add_argument("--step-days", type=int, default=10, help="advance in trading sessions between holdouts")
    parser.add_argument("--min-train-trades", type=int, default=10)
    parser.add_argument("--max-train-drawdown-pct", type=float, default=10)
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_walk_forward(args)
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
