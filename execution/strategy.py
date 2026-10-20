"""Bridge strategy proposals into the guarded execution command."""

from __future__ import annotations

import json
import subprocess
import sys

from runtime.paths import ROOT


def execute_strategy_trades(trades: list[dict], *, mode: str, dry_run: bool = False) -> dict:
    command = [sys.executable, "-m", "execution", "--mode", mode]
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(
        command,
        input=json.dumps({"trades": trades}),
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"python -m execution exited {completed.returncode}: {completed.stderr[-1000:]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("python -m execution returned invalid JSON") from exc
