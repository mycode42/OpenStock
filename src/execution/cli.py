"""Command-line interface for trade execution."""

import argparse
import os

from src.execution import runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute Agent's trade decisions")
    parser.add_argument("--file", type=str, help="JSON file with trade decisions")
    parser.add_argument("--dry-run", action="store_true", help="Validate but don't execute")
    parser.add_argument(
        "--flatten",
        action="store_true",
        help="Market-sell all open positions (end-of-day flatten). Ignores --file.",
    )
    parser.add_argument(
        "--flatten-if-due",
        action="store_true",
        help="Day guard: flatten at the broker-calendar deadline, otherwise no-op.",
    )
    parser.add_argument(
        "--mode",
        choices=["swing", "day", "algo"],
        default=None,
        help="Trading mode (overrides TRADING_MODE env, default: swing)",
    )
    args = parser.parse_args()

    mode = args.mode or os.environ.get("TRADING_MODE", "swing")
    if args.flatten_if_due and mode != "day":
        parser.error("--flatten-if-due requires --mode day")
    if args.flatten and args.flatten_if_due:
        parser.error("Choose --flatten or --flatten-if-due")
    with runner.account_execution_lock(mode):
        return runner._run_main(args, mode)
