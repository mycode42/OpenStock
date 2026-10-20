"""Nightly options research batch and its standalone report artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from options.research.runner import RESEARCH_DEFAULTS_PATH, _merge, _project_path, _read_json, run_research
from runtime.atomic import atomic_write_json
from runtime.paths import ROOT


ET = ZoneInfo("America/New_York")
SUPPORTED_STRATEGIES = ("vrp", "event_driven", "directional_overlay")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _strategy_summary(strategy: str, report: dict) -> dict:
    return {
        "status": "completed",
        "run_id": report.get("run_id"),
        "run_dir": report.get("run_dir"),
        "report_path": str(Path(report["run_dir"]) / "report.md") if report.get("run_dir") else None,
        "approved": bool((report.get("validation") or {}).get("approved")),
        "validation": report.get("validation", {}),
        "walk_forward": (report.get("walk_forward") or {}).get("oos_metrics", {}),
        "holdout": report.get("holdout", {}).get("metrics", {}),
        "final_parameters": report.get("final_parameters", {}),
        "data_mode": report.get("data_mode"),
        "underlying": report.get("underlying"),
    }


def _error_summary(
    strategy: str, error: Exception, *, started_at: datetime,
    run_log: Path, output_root: Path,
) -> dict:
    status = "error"
    message = f"{type(error).__name__}: {error}"
    if isinstance(error, FileNotFoundError) and "historical options dataset not found" in str(error):
        status = "skipped_missing_dataset"
    # run_research records preflight failures before raising. Include the artifact
    # path so a malformed or too-short dataset can be diagnosed without guessing.
    latest_run = None
    if run_log.is_file():
        for line in run_log.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("strategy") == strategy and row.get("status") in {"started", "preflight_failed"}:
                try:
                    stamp = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if stamp >= started_at:
                    latest_run = row
    return {
        "status": status,
        "error": message,
        "run_id": (latest_run or {}).get("run_id"),
        "run_dir": str(output_root / latest_run["run_id"]) if latest_run and latest_run.get("run_id") else None,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        f"# Nightly options research ({report['run_date_et']})",
        "",
        f"Status: **{report['status']}**  ",
        f"Dataset: `{report['dataset_path']}` ({'available' if report['dataset_available'] else 'not available'})  ",
        f"Data mode: `{report.get('data_mode', 'n/a')}`  ",
        "",
        "This report is a separate research sleeve alongside equity AutoResearch. Options candidates remain research-only; exported parameters are not applied to paper or live settings.",
        "",
    ]
    for strategy, result in report.get("strategies", {}).items():
        lines.extend([f"## {strategy}", "", f"Status: **{result.get('status')}**"])
        if result.get("error"):
            lines.extend(["", f"Error: `{result['error']}`"])
        if result.get("status") == "completed":
            wf = result.get("walk_forward", {})
            holdout = result.get("holdout", {})
            lines.extend([
                f"  ", f"Approved for paper config export: **{result.get('approved')}**  ",
                f"Rolling OOS: trades {wf.get('trade_count', 0)}, return {float(wf.get('total_return', 0.0)):.2%}, Sharpe {float(wf.get('annualized_sharpe', 0.0)):.3f}, max drawdown {float(wf.get('max_drawdown', 0.0)):.2%}  ",
                f"Untouched holdout: trades {holdout.get('trade_count', 0)}, return {float(holdout.get('total_return', 0.0)):.2%}, Sharpe {float(holdout.get('annualized_sharpe', 0.0)):.3f}  ",
                f"Parameters: `{json.dumps(result.get('final_parameters', {}), sort_keys=True)}`  ",
                f"Detailed report: `{result.get('report_path')}`",
            ])
        lines.append("")
    lines.extend([
        "## Data and execution assumptions",
        "",
        "Historical chain snapshots are required for empirical results. A missing dataset skips the strategy runs and is reported as such; a model surface is only used when explicitly represented in the configured dataset. Fills include per-leg spread slippage and commissions. Assignment remains a simplified simulation heuristic.",
        "",
    ])
    return "\n".join(lines)


def run_automated_options_research(*, config_path: str | Path | None = None) -> dict:
    """Run the configured options sleeves without touching broker credentials or orders."""
    config_path_resolved = _project_path(config_path) if config_path else RESEARCH_DEFAULTS_PATH
    config = _merge(_read_json(RESEARCH_DEFAULTS_PATH), _read_json(config_path_resolved))
    strategies = [str(item).strip().lower() for item in config.get("automation", {}).get("strategies", ["vrp", "event_driven"])]
    if not strategies or any(item not in SUPPORTED_STRATEGIES for item in strategies):
        raise ValueError(f"automation strategies must be selected from {', '.join(SUPPORTED_STRATEGIES)}")
    data_path = _project_path(config.get("data_path", "data/options/research/chains.jsonl"))
    output_root = _project_path(config.get("output_dir", "data/options/research/runs"))
    automation_dir = output_root.parent / "automation"
    automation_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    run_log = output_root / "research_runs.jsonl"
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    enabled = bool(config.get("automation", {}).get("enabled", True))
    strategies_report: dict[str, dict] = {}
    data_available = data_path.is_file()
    data_mode = config.get("data_mode", "auto")

    if not enabled:
        status = "disabled"
        strategies_report = {strategy: {"status": "disabled"} for strategy in strategies}
    elif not data_available:
        status = "waiting_for_data"
        strategies_report = {
            strategy: {
                "status": "skipped_missing_dataset",
                "message": "Add historical option-chain snapshots or dated IV-surface observations to the configured dataset path.",
            }
            for strategy in strategies
        }
    else:
        for strategy in strategies:
            strategy_started = datetime.now(timezone.utc)
            try:
                result = run_research(strategy, config_path=config_path_resolved)
                strategies_report[strategy] = _strategy_summary(strategy, result)
            except Exception as exc:
                strategies_report[strategy] = _error_summary(
                    strategy, exc, started_at=strategy_started, run_log=run_log,
                    output_root=output_root,
                )
        statuses = {item.get("status") for item in strategies_report.values()}
        if "error" in statuses:
            status = "partial_error"
        elif statuses == {"skipped_missing_dataset"}:
            status = "waiting_for_data"
        else:
            status = "completed"

    today = datetime.now(ET).date().isoformat()
    report = {
        "schema": "swarm_options_nightly_research_v1",
        "status": status,
        "created_at": created_at.isoformat(),
        "run_date_et": today,
        "config_path": str(config_path_resolved),
        "config_sha256": config_hash,
        "dataset_path": str(data_path),
        "dataset_available": data_available,
        "data_mode": data_mode,
        "strategies": strategies_report,
        "research_only": True,
        "trade_or_order_actions": 0,
    }
    report_path = automation_dir / f"{today}.json"
    atomic_write_json(report_path, report, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(automation_dir / "latest.json", report, indent=2, sort_keys=True, allow_nan=False)
    markdown_path = automation_dir / f"{today}.md"
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    (automation_dir / "latest.md").write_text(_render_markdown(report), encoding="utf-8")
    _append_jsonl(automation_dir / "runs.jsonl", {
        "created_at": created_at.isoformat(), "run_date_et": today,
        "status": status, "config_sha256": config_hash,
        "dataset_path": str(data_path), "dataset_available": data_available,
        "strategies": {
            name: {
                "status": result.get("status"), "run_id": result.get("run_id"),
                "run_dir": result.get("run_dir"), "error": result.get("error"),
            }
            for name, result in strategies_report.items()
        },
        "report_path": str(report_path),
    })
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run configured options research sleeves")
    parser.add_argument("--config", help="options research config override")
    args = parser.parse_args()
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    report = run_automated_options_research(config_path=args.config)
    print(json.dumps({
        "status": report["status"], "report": str((ROOT / "data/options/research/automation/latest.json").resolve()),
        "strategies": {name: result.get("status") for name, result in report["strategies"].items()},
        "dataset_available": report["dataset_available"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
