"""Command line interface for stored-bar portfolio replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

from .shared import DEFAULT_DB, SQLiteBarStore
from .data import fetch_dataset, run_backtest
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the live Algo strategy portfolio on stored Alpaca bars")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="download historical bars into the local SQLite data store")
    fetch.add_argument("--symbols", required=True, help="fixed comma-separated Algo catalog symbols")
    fetch.add_argument("--start", required=True, help="first regular-session date, YYYY-MM-DD")
    fetch.add_argument("--end", required=True, help="last regular-session date, YYYY-MM-DD")
    fetch.add_argument(
        "--feed", choices=["sip"], default=None,
        help="Plus SIP regular-session feed",
    )
    fetch.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=None)
    fetch.add_argument("--strategies", help="optional comma-separated strategies; controls factor warmup")
    fetch.add_argument("--profile", help="optional parameter profile; determines factor-history warmup")
    fetch.add_argument("--db", default=str(DEFAULT_DB))
    fetch.set_defaults(handler=fetch_dataset)

    run = commands.add_parser("run", help="run deterministic fixed-universe portfolio replay")
    run.add_argument("--symbols", required=True)
    run.add_argument("--start", required=True)
    run.add_argument("--end", required=True)
    run.add_argument("--feed", choices=["sip"], default=None, help="Plus SIP regular-session feed stored in the database")
    run.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=None)
    run.add_argument("--capital", type=float, default=100_000)
    run.add_argument("--slippage-bps", type=float, default=5)
    run.add_argument("--spread-bps", type=float, default=3, help="assumed full quoted spread; half is charged on each side")
    run.add_argument("--commission-per-share", type=float, default=0.005)
    run.add_argument("--short-borrow-annual-pct", type=float, default=3.0)
    run.add_argument("--unknown-borrow-penalty-bps", type=float, default=25)
    run.add_argument("--borrow-availability", help="CSV with date,symbol,easy_to_borrow columns")
    run.add_argument("--profile", help="optional JSON with signal parameters only; risk settings cannot be changed")
    run.add_argument("--strategies", help="optional comma-separated strategy isolation filter")
    run.add_argument("--event-calendar", help="optional point-in-time event calendar CSV")
    run.add_argument("--research-max-entries-per-day", type=int)
    run.add_argument("--research-max-open-positions", type=int)
    run.add_argument("--research-max-positions-per-strategy", type=int)
    run.add_argument("--research-max-momentum-entries-per-day", type=int)
    run.add_argument("--research-cooldown-minutes", type=int)
    run.add_argument("--research-one-orb-attempt-per-day", action="store_true")
    run.add_argument("--db", default=str(DEFAULT_DB))
    run.add_argument("--output", help="optional JSON report path")
    run.set_defaults(handler=run_backtest)

    coverage = commands.add_parser("coverage", help="show bars available in the local SQLite store")
    coverage.add_argument("--db", default=str(DEFAULT_DB))
    coverage.set_defaults(handler=lambda args: {"database": str(Path(args.db).resolve()), "coverage": SQLiteBarStore(args.db).coverage()})
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = args.handler(args)
    except (ValueError, OSError, requests.RequestException) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0

