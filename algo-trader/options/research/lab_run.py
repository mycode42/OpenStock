"""Single-strategy research run for the Research lab page.

Runs the requested underlyings one after another, each with the requested fold
workers, and prints one ``LAB_EVENT {json}`` line per milestone so the server
can show progress. Provider or data failures for one underlying are recorded
and the run continues.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from options.research.runner import run_research

EVENT_PREFIX = "LAB_EVENT "


def emit(event: dict) -> None:
    print(EVENT_PREFIX + json.dumps(event, sort_keys=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="research JSON config for this job")
    parser.add_argument("--strategy", required=True, choices=("vrp", "long_volatility"))
    parser.add_argument("--underlyings", required=True, help="comma-separated tickers")
    parser.add_argument("--workers", type=int, default=8, help="fold workers per underlying")
    parser.add_argument("--replay-only", action="store_true", help="replay the chosen parameters; no search or validation")
    parser.add_argument("--live-config", default=None, help="research JSON with the live settings, replayed for comparison")
    args = parser.parse_args(argv)

    underlyings = [item.strip().upper() for item in args.underlyings.split(",") if item.strip()]
    emit({"event": "started", "strategy": args.strategy, "total": len(underlyings), "underlyings": underlyings})
    # The replay prints its progress lines, labelled so the page can tell the chosen and live replays apart.
    os.environ["ALGO_LAB_PROGRESS"] = "1"
    failures = 0
    for index, underlying in enumerate(underlyings, start=1):
        emit({"event": "underlying_started", "underlying": underlying, "index": index, "total": len(underlyings)})
        try:
            os.environ["ALGO_REPLAY_LABEL"] = "chosen"
            report = run_research(
                args.strategy, config_path=args.config, underlying=underlying,
                parallel_workers=args.workers, replay_only=args.replay_only,
            )
            # The live settings replay over the same history, so the lab can compare the two.
            live_report = None
            if args.live_config:
                os.environ["ALGO_REPLAY_LABEL"] = "live"
                live_report = run_research(
                    args.strategy, config_path=args.live_config, underlying=underlying,
                    parallel_workers=args.workers, replay_only=True,
                )
            validation = report.get("validation") or {}
            emit({
                "event": "underlying_done",
                "underlying": underlying,
                "index": index,
                "total": len(underlyings),
                "status": report.get("status") or "completed",
                "approved": bool(validation.get("approved")),
                "run_dir": report.get("run_dir"),
                "run_id": report.get("run_id"),
                "live_run_dir": (live_report or {}).get("run_dir"),
            })
        except Exception as exc:  # one underlying's failure is data, not a crash
            failures += 1
            traceback.print_exc(file=sys.stderr)
            emit({
                "event": "underlying_error",
                "underlying": underlying,
                "index": index,
                "total": len(underlyings),
                "failure": f"{type(exc).__name__}: {str(exc)[:400]}",
            })
    emit({"event": "finished", "total": len(underlyings), "failures": failures})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
