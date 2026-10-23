"""Run equity AutoResearch and the independent options research job concurrently."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from autoresearch.artifacts.files import _json_safe
from autoresearch.settings import DEFAULT_OUTPUT_DIR, ET
from runtime.atomic import atomic_write_json
from runtime.paths import ROOT


logger = logging.getLogger("autoresearch.nightly")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run equity and options research concurrently")
    parser.add_argument("--db", default=None)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--factor-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-options-research", action="store_true")
    return parser


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _attach_options_report(output_dir: Path, options_report: dict, process_summary: dict) -> dict | None:
    latest_path = output_dir / "latest.json"
    report = _read_json(latest_path)
    if report is None:
        return None
    # Keep the equity pipeline's report intact and expose the independent sleeve
    # at a top-level key so the daily summary can present it as a separate section.
    report["options_research"] = options_report
    report["parallel_research"] = process_summary
    report = _json_safe(report)
    today = report.get("run_date_et")
    try:
        datetime.strptime(str(today), "%Y-%m-%d")
    except ValueError:
        today = datetime.now(ET).date().isoformat()
    atomic_write_json(latest_path, report, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(output_dir / f"{today}.json", report, indent=2, sort_keys=True, allow_nan=False)
    invocation_report = report.get("invocation_report_path")
    if invocation_report:
        atomic_write_json(Path(invocation_report), report, indent=2, sort_keys=True, allow_nan=False)
    return report


def _equity_command(args) -> list[str]:
    command = [sys.executable, "-m", "autoresearch.auto_research", "--workers", str(args.workers)]
    if args.db:
        command.extend(("--db", args.db))
    if args.output_dir:
        command.extend(("--output-dir", args.output_dir))
    if args.no_refresh:
        command.append("--no-refresh")
    if args.factor_only:
        command.append("--factor-only")
    if args.force:
        command.append("--force")
    return command


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = (ROOT / output_dir).resolve()
    log_dir = output_dir / "parallel" / uuid4().hex
    log_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    options_path = ROOT / "data/options/research/automation/latest.json"
    options_log_path = log_dir / "options.log"
    equity_log_path = log_dir / "equity.log"
    options_process = None
    equity_process = None
    options_launch_error = None

    with equity_log_path.open("w", encoding="utf-8") as equity_log:
        with options_log_path.open("w", encoding="utf-8") as options_log:
            if not args.skip_options_research:
                # Lower priority protects the existing equity research workload
                # when both CPU-heavy pipelines overlap.
                try:
                    options_process = subprocess.Popen(
                        [
                            sys.executable, "-m", "options.research.automation",
                            "--config", str(ROOT / "options/research/config.portfolio.json"),
                            "--workers", str(args.workers),
                        ],
                        cwd=ROOT, stdout=options_log, stderr=subprocess.STDOUT,
                    )
                except OSError as exc:
                    options_launch_error = f"{type(exc).__name__}: {exc}"
            equity_process = subprocess.Popen(
                _equity_command(args), cwd=ROOT, stdout=equity_log,
                stderr=subprocess.STDOUT,
            )
            equity_code = equity_process.wait()
            options_code = options_process.wait() if options_process is not None else None

    completed_at = datetime.now(timezone.utc)
    options_report = _read_json(options_path)
    if options_report is not None:
        try:
            options_created_at = datetime.fromisoformat(
                str(options_report.get("created_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            options_created_at = None
        if options_created_at is None or options_created_at < started_at:
            options_report = None
    if args.skip_options_research or (options_process is None and options_launch_error is None):
        options_report = {
            "schema": "swarm_options_nightly_research_v1",
            "status": "skipped",
            "run_date_et": datetime.now(ET).date().isoformat(),
            "research_only": True,
            "reason": "disabled by --skip-options-research" if args.skip_options_research else options_launch_error,
        }
    elif options_process is None:
        options_report = {
            "schema": "swarm_options_nightly_research_v1",
            "status": "error",
            "run_date_et": datetime.now(ET).date().isoformat(),
            "research_only": True,
            "reason": options_launch_error or "options runner failed to start",
            "log_path": str(options_log_path),
        }
    elif options_report is None:
        options_report = {
            "schema": "swarm_options_nightly_research_v1",
            "status": "error",
            "run_date_et": datetime.now(ET).date().isoformat(),
            "research_only": True,
            "reason": "options runner exited without a valid standalone report",
            "log_path": str(options_log_path),
        }

    process_summary = {
        "concurrent": not args.skip_options_research,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "equity_exit_code": equity_code,
        "options_exit_code": options_code,
        "equity_log_path": str(equity_log_path),
        "options_log_path": str(options_log_path),
    }
    equity_report = _attach_options_report(output_dir, options_report, process_summary)
    result = {
        "status": "completed" if equity_code == 0 else "error",
        "equity_report": str((output_dir / "latest.json").resolve()),
        "equity_exit_code": equity_code,
        "options_report": str(options_path.resolve()),
        "options_status": options_report.get("status"),
        "options_exit_code": options_code,
        "equity_report_updated": equity_report is not None,
        **process_summary,
    }
    print(json.dumps(result, sort_keys=True))
    if equity_code != 0:
        return equity_code
    if equity_report is None:
        logger.error("Equity AutoResearch exited successfully without a valid report at %s", output_dir / "latest.json")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
