"""CLI for one-strategy-at-a-time options research."""

from __future__ import annotations

import argparse
import json

from options.research.runner import run_research


def main() -> int:
    parser = argparse.ArgumentParser(description="Purged walk-forward options strategy research")
    parser.add_argument(
        "--strategy", required=True,
        choices=("vrp", "llm_driven", "directional_overlay", "long_volatility"),
    )
    parser.add_argument("--data", help="JSON/JSONL historical chain dataset (overrides config)")
    parser.add_argument("--config", help="research JSON config override")
    parser.add_argument("--output-dir", help="run output directory (overrides config)")
    parser.add_argument("--underlying", help="underlying ticker to research (overrides config)")
    args = parser.parse_args()
    report = run_research(
        args.strategy, data_path=args.data, config_path=args.config,
        output_dir=args.output_dir, underlying=args.underlying,
    )
    print(json.dumps({
        "run_id": report["run_id"],
        "run_dir": report["run_dir"],
        "validation": report["validation"],
        "final_parameters": report["final_parameters"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
