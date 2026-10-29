"""Daily research cycle: options research first, then equity AutoResearch.

Run it with ``python -m research``. The parts also run alone:
``python -m research.options`` and ``python -m research.equity``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from equity_research.artifacts.files import _json_safe
from equity_research.artifacts.research_ledger import ResearchLedger
from equity_research.settings import DEFAULT_OUTPUT_DIR, ET
from runtime.atomic import atomic_write_json
from runtime.paths import ROOT


logger = logging.getLogger("research.cycle")
SCHEDULE_TZ = ZoneInfo("Europe/Berlin")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run options and equity research sequentially")
    parser.add_argument("--db", default=None)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--factor-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-options-research", action="store_true")
    parser.add_argument(
        "--new-run", action="store_true",
        help="start a fresh cycle instead of resuming this scheduled cycle",
    )
    return parser


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _attach_options_report(
    output_dir: Path, options_report: dict, process_summary: dict,
    report: dict | None = None,
) -> dict | None:
    latest_path = output_dir / "latest.json"
    report = report or _read_json(latest_path)
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
    command = [sys.executable, "-m", "research.equity", "--workers", str(args.workers)]
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
    if getattr(args, "invocation_id", None):
        command.extend(("--invocation-id", args.invocation_id))
    return command


def _cycle_date(now: datetime | None = None) -> date:
    """Use the most recent 22:00 Berlin slot so overnight retries retain their cycle."""
    current = now or datetime.now(SCHEDULE_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SCHEDULE_TZ)
    else:
        current = current.astimezone(SCHEDULE_TZ)
    scheduled_today = datetime.combine(current.date(), time(22), tzinfo=SCHEDULE_TZ)
    return current.date() if current >= scheduled_today else current.date() - timedelta(days=1)


def _request_fingerprint(args) -> dict:
    return {
        "db": args.db,
        "output_dir": str(args.output_dir),
        "workers": args.workers,
        "no_refresh": bool(args.no_refresh),
        "factor_only": bool(args.factor_only),
        "force": bool(args.force),
        "skip_options_research": bool(args.skip_options_research),
    }


def _new_cycle_state(cycle_date: date, args, run_id: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema": "swarm_autoresearch_nightly_checkpoint_v1",
        "run_id": run_id,
        "cycle_date_et": cycle_date.isoformat(),
        "started_at": now,
        "updated_at": now,
        "status": "running",
        "request": _request_fingerprint(args),
        "equity_invocation_id": uuid4().hex,
        "stages": {
            "options": {"status": "skipped" if args.skip_options_research else "pending"},
            "equity": {"status": "pending"},
        },
    }


def _report_created_at(report: dict | None) -> datetime | None:
    if not report:
        return None
    value = report.get("created_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _report_written_after(path: Path, started_at: str | None) -> dict | None:
    report = _read_json(path)
    if report is None:
        return None
    started = None
    if started_at:
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            else:
                started = started.astimezone(timezone.utc)
        except ValueError:
            return None
    created = _report_created_at(report)
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    if started and (created is None or created < started or modified < started):
        return None
    return report


def _equity_report_for_invocation(output_dir: Path, invocation_id: str) -> dict | None:
    report_path = output_dir / "invocations" / invocation_id / "report.json"
    report = _read_json(report_path)
    if report and report.get("invocation_id") == invocation_id:
        return report
    report = _read_json(output_dir / "latest.json")
    if report and report.get("invocation_id") == invocation_id:
        return report
    return None


def _checkpoint_store(output_dir: Path) -> ResearchLedger:
    """Nightly cycle state lives in the equity research database."""
    return ResearchLedger(output_dir / "research.sqlite3")


def _checkpoint_location(output_dir: Path, run_id: str) -> str:
    return f"{(output_dir / 'research.sqlite3').resolve()}#{run_id}"


def _checkpoint_progress(output_dir: Path, run_dir: Path, state: dict, *, current_stage: str | None = None) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _checkpoint_store(output_dir).write_nightly_checkpoint(state["run_id"], state)
    checkpoint_location = _checkpoint_location(output_dir, state["run_id"])
    progress = {
        "schema": "swarm_autoresearch_nightly_progress_v1",
        "run_id": state["run_id"],
        "cycle_date_et": state["cycle_date_et"],
        "status": state.get("status", "running"),
        "current_stage": current_stage or state.get("current_stage"),
        "updated_at": state["updated_at"],
        "equity_invocation_id": state.get("equity_invocation_id"),
        "stages": {
            name: {key: value for key, value in stage.items() if key != "report"}
            for name, stage in (state.get("stages") or {}).items()
        },
        "checkpoint_location": checkpoint_location,
    }
    atomic_write_json(run_dir / "progress.json", progress, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(output_dir / "latest-progress.json", progress, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(run_dir / "partial-report.json", {
        "schema": "swarm_autoresearch_nightly_partial_v1",
        "status": state.get("status", "running"),
        "run_id": state["run_id"],
        "cycle_date_et": state["cycle_date_et"],
        "current_stage": progress.get("current_stage"),
        "updated_at": state["updated_at"],
        "options_research": (state.get("stages", {}).get("options") or {}).get("report"),
        "equity_research": {
            "status": (state.get("stages", {}).get("equity") or {}).get("status", "pending"),
            "invocation_id": state.get("equity_invocation_id"),
            "progress_path": str((output_dir / "invocations" / str(state.get("equity_invocation_id")) / "progress.json").resolve()),
        },
        "stages": progress["stages"],
        "checkpoint_location": checkpoint_location,
    }, indent=2, sort_keys=True, allow_nan=False)


def _state_report_hash(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _cycle_checkpoint(output_dir: Path, args) -> tuple[dict, bool]:
    """Return the cycle state to run, and whether a finished cycle can be reused."""
    cycle_date = _cycle_date()
    request = _request_fingerprint(args)
    base_id = f"nightly-{cycle_date:%Y%m%d}"
    if args.new_run:
        run_id = f"{base_id}-{uuid4().hex[:10]}"
        return _new_cycle_state(cycle_date, args, run_id), False
    existing = _checkpoint_store(output_dir).read_nightly_checkpoint(base_id)
    if existing and existing.get("request") == request:
        if existing.get("status") == "completed" and existing.get("final_result"):
            return existing, True
        return existing, False
    return _new_cycle_state(cycle_date, args, base_id), False


def _valid_options_checkpoint(stage: dict, report_path: Path, run_id: str) -> dict | None:
    report = stage.get("report") if isinstance(stage.get("report"), dict) else None
    if (
        report and report.get("run_id") == run_id
        and report.get("status") not in {"error", "partial_error", "running"}
        and stage.get("report_sha256") == _state_report_hash(report_path)
    ):
        return report
    recovered = _report_written_after(report_path, stage.get("started_at"))
    if (
        recovered
        and recovered.get("schema") == "swarm_options_nightly_research_v1"
        and recovered.get("run_id") == run_id
        and recovered.get("status") not in {"error", "partial_error", "running"}
    ):
        return recovered
    return None


def _run_equity_stage(
    args, state: dict, output_dir: Path, log_path: Path, run_dir: Path,
) -> tuple[int, dict | None, bool]:
    stage = state.setdefault("stages", {}).setdefault("equity", {"status": "pending"})
    invocation_id = state["equity_invocation_id"]
    report = _equity_report_for_invocation(output_dir, invocation_id)
    if stage.get("status") == "completed" and report:
        return 0, report, True
    # Recover a completed AutoResearch report if the process exited between
    # writing its immutable invocation report and updating the nightly state.
    if stage.get("status") == "running" and report and report.get("status") not in {"error", "partial_error"}:
        stage.update({"status": "completed", "exit_code": 0, "recovered": True})
        _checkpoint_progress(output_dir, run_dir, state, current_stage="equity")
        return 0, report, True

    stage.update({
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": None,
    })
    state["current_stage"] = "equity"
    _checkpoint_progress(output_dir, run_dir, state, current_stage="equity")
    args.invocation_id = invocation_id
    launch_error = None
    try:
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                _equity_command(args), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            )
            exit_code = process.wait()
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
        exit_code = 127
    report = _equity_report_for_invocation(output_dir, invocation_id)
    completed = exit_code == 0 and report is not None and report.get("status") not in {"error", "partial_error"}
    stage.update({
        "status": "completed" if completed else "failed",
        "exit_code": exit_code,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str((output_dir / "invocations" / invocation_id / "report.json").resolve()),
        "report_sha256": _state_report_hash(output_dir / "invocations" / invocation_id / "report.json"),
        "error": launch_error or (report.get("error") if report else None),
    })
    _checkpoint_progress(output_dir, run_dir, state, current_stage="equity")
    return exit_code, report, False


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = (ROOT / output_dir).resolve()
    args.output_dir = str(output_dir)
    lock_path = output_dir / "locks" / "nightly-cycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    options_path = ROOT / "data/options/research/automation/latest.json"

    # Cron can retry a timed-out agent run while its old command is still
    # unwinding. Serialize the cycle so retries wait for the checkpoint owner.
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state, cached_complete = _cycle_checkpoint(output_dir, args)
        run_id = state["run_id"]
        run_dir = output_dir / "parallel" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if cached_complete:
            result = state["final_result"]
            print(json.dumps(result, sort_keys=True))
            return int(result.get("exit_code", 0))

        _checkpoint_store(output_dir).write_nightly_checkpoint(run_id, state)
        started_at = datetime.fromisoformat(state["started_at"].replace("Z", "+00:00"))
        options_log_path = run_dir / "options.log"
        equity_log_path = run_dir / "equity.log"
        options_stage = state["stages"].setdefault("options", {"status": "pending"})
        options_code: int | None = None
        options_started_at = options_stage.get("started_at")
        options_completed_at = options_stage.get("completed_at")
        options_reused = False
        if args.skip_options_research:
            options_report = {
                "schema": "swarm_options_nightly_research_v1",
                "status": "skipped", "run_date_et": datetime.now(ET).date().isoformat(),
                "research_only": True, "reason": "disabled by --skip-options-research",
            }
            options_stage.update({"status": "skipped", "report": options_report})
            _checkpoint_progress(output_dir, run_dir, state, current_stage="equity")
        else:
            recovered = _valid_options_checkpoint(options_stage, options_path, run_id)
            if options_stage.get("status") == "completed" and recovered:
                options_report = recovered
                options_code = int(options_stage.get("exit_code", 0))
                options_completed_at = options_stage.get("completed_at")
                options_reused = True
            elif options_stage.get("status") == "running" and recovered:
                options_report = recovered
                options_code = 0
                options_completed_at = datetime.now(timezone.utc).isoformat()
                options_stage.update({
                    "status": "completed", "exit_code": 0,
                    "completed_at": options_completed_at,
                    "report": options_report,
                    "report_sha256": _state_report_hash(options_path),
                    "recovered": True,
                })
                options_reused = True
                _checkpoint_progress(output_dir, run_dir, state, current_stage="equity")
            else:
                options_started_at = datetime.now(timezone.utc).isoformat()
                options_stage.update({"status": "running", "started_at": options_started_at})
                state["current_stage"] = "options"
                _checkpoint_progress(output_dir, run_dir, state, current_stage="options")
                launch_error = None
                try:
                    with options_log_path.open("a", encoding="utf-8") as options_log:
                        options_process = subprocess.Popen(
                            [
                                sys.executable, "-m", "research.options",
                                "--config", str(ROOT / "options/research/config.portfolio.json"),
                                "--workers", str(args.workers),
                                "--run-id", run_id,
                            ],
                            cwd=ROOT, stdout=options_log, stderr=subprocess.STDOUT,
                        )
                        options_code = options_process.wait()
                except OSError as exc:
                    launch_error = f"{type(exc).__name__}: {exc}"
                    options_code = 127
                options_report = _report_written_after(options_path, options_started_at)
                options_ok = (
                    options_code == 0 and options_report is not None
                    and options_report.get("schema") == "swarm_options_nightly_research_v1"
                    and options_report.get("run_id") == run_id
                    and options_report.get("status") not in {"error", "partial_error", "running"}
                )
                options_completed_at = datetime.now(timezone.utc).isoformat()
                options_stage.update({
                    "status": "completed" if options_ok else "failed",
                    "exit_code": options_code,
                    "completed_at": options_completed_at,
                    "report": options_report,
                    "report_sha256": _state_report_hash(options_path),
                    "error": launch_error or (
                        "options runner exited without a fresh valid report" if not options_ok else None
                    ),
                })
                _checkpoint_progress(output_dir, run_dir, state, current_stage="equity")
                if options_report is None:
                    options_report = {
                        "schema": "swarm_options_nightly_research_v1", "status": "error",
                        "run_date_et": datetime.now(ET).date().isoformat(), "research_only": True,
                        "reason": launch_error or "options runner exited without a fresh valid report",
                        "log_path": str(options_log_path.resolve()),
                    }

        equity_stage = state["stages"].setdefault("equity", {"status": "pending"})
        equity_started_at = equity_stage.get("started_at")
        equity_reused = equity_stage.get("status") == "completed"
        equity_code, base_equity_report, equity_recovered = _run_equity_stage(
            args, state, output_dir, equity_log_path, run_dir,
        )
        equity_reused = equity_reused or equity_recovered
        if equity_started_at is None:
            equity_started_at = equity_stage.get("started_at")
        completed_at = datetime.now(timezone.utc).isoformat()
        process_summary = {
            "concurrent": False,
            "execution_order": "options_then_equity" if not args.skip_options_research else "equity_only",
            "run_id": run_id,
            "started_at": state["started_at"],
            "options_started_at": options_started_at,
            "options_completed_at": options_completed_at,
            "options_reused": options_reused,
            "equity_started_at": equity_started_at,
            "equity_reused": equity_reused,
            "completed_at": completed_at,
            "equity_exit_code": equity_code,
            "options_exit_code": options_code,
            "equity_log_path": str(equity_log_path.resolve()),
            "options_log_path": str(options_log_path.resolve()),
            "checkpoint_location": _checkpoint_location(output_dir, run_id),
            "progress_path": str((run_dir / "progress.json").resolve()),
        }
        equity_report = _attach_options_report(
            output_dir, options_report, process_summary, report=base_equity_report,
        )
        result_code = equity_code if equity_code != 0 else (options_code or 0)
        result = {
            "status": "completed" if result_code == 0 and equity_report is not None else "error",
            "exit_code": result_code if result_code != 0 else (0 if equity_report else 3),
            "equity_report": str((output_dir / "latest.json").resolve()),
            "equity_exit_code": equity_code,
            "options_report": str(options_path.resolve()),
            "options_status": options_report.get("status"),
            "options_exit_code": options_code,
            "equity_report_updated": equity_report is not None,
            **process_summary,
        }
        atomic_write_json(run_dir / "partial-report.json", {
            "schema": "swarm_autoresearch_nightly_partial_v1",
            "status": result["status"], "run_id": run_id,
            "cycle_date_et": state["cycle_date_et"],
            "options_research": options_report,
            "equity_research": equity_report or {
                "status": "error", "exit_code": equity_code,
                "report_path": str((output_dir / "invocations" / state["equity_invocation_id"] / "report.json").resolve()),
            },
            "process_summary": process_summary,
        }, indent=2, sort_keys=True, allow_nan=False)
        state["current_stage"] = None
        state["status"] = "completed" if result["status"] == "completed" else "failed"
        state["final_result"] = result if state["status"] == "completed" else None
        _checkpoint_progress(output_dir, run_dir, state)
        print(json.dumps(result, sort_keys=True))
        if equity_code != 0:
            return equity_code
        if options_code not in (None, 0):
            return options_code
        if equity_report is None:
            logger.error("Equity AutoResearch exited successfully without a valid report at %s", output_dir / "latest.json")
            return 3
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
