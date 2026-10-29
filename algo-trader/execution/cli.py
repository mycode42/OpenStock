"""Command-line interface for trade execution."""

import argparse

from execution import runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute validated trade decisions")
    parser.add_argument("--file", type=str, help="JSON file with trade decisions")
    parser.add_argument("--dry-run", action="store_true", help="Validate but don't execute")
    parser.add_argument(
        "--flatten",
        action="store_true",
        help="Market-sell all open positions (end-of-day flatten). Ignores --file.",
    )
    parser.add_argument(
        "--mode",
        choices=["algo"],
        default="algo",
        help="Account route (only the Algo paper account is supported)",
    )
    args = parser.parse_args()

    mode = args.mode
    with runner.account_execution_lock(mode):
        return runner._run_main(args, mode)
