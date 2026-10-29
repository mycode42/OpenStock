"""Install the Algo automations from ops/automations.json.

Backend ``openclaw`` creates or updates OpenClaw cron jobs by name. Backend
``cron`` writes a marked block to the user's crontab; it can run command jobs
only, because agent turns need OpenClaw. ``auto`` picks openclaw when the CLI
is on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS = ROOT / "ops" / "automations.json"
CRON_BEGIN = "# BEGIN openstock-algo-automations"
CRON_END = "# END openstock-algo-automations"


def load_jobs(path: Path = DEFINITIONS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def render(text: str, root: Path = ROOT) -> str:
    return text.replace("{ROOT}", str(root))


def slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")


def _prompt(job: dict, root: Path) -> str:
    return render((root / job["agent_turn"]["prompt"]).read_text(encoding="utf-8").rstrip() + "\n", root)


def openclaw_args(job: dict, root: Path, report_to: str | None) -> list[str]:
    """Flags shared by ``openclaw cron add`` and ``openclaw cron edit``."""
    args: list[str] = []
    schedule = job["schedule"]
    if "cron" in schedule:
        args += ["--cron", schedule["cron"], "--tz", "America/New_York"]
    else:
        args += ["--every", schedule["every"]]
    if "description" in job:
        args += ["--description", job["description"]]
    if "agent" in job:
        args += ["--agent", job["agent"]]
    if "command" in job:
        command = job["command"]
        args += ["--command-argv", json.dumps(command["argv"]), "--command-cwd", str(root),
                 "--timeout-seconds", str(command["timeout_seconds"]), "--no-deliver"]
        if "no_output_timeout_seconds" in command:
            args += ["--no-output-timeout-seconds", str(command["no_output_timeout_seconds"])]
        if "output_max_bytes" in command:
            args += ["--output-max-bytes", str(command["output_max_bytes"])]
    if "agent_turn" in job:
        turn = job["agent_turn"]
        args += ["--message", _prompt(job, root), "--timeout-seconds", str(turn["timeout_seconds"]),
                 "--tools", ",".join(turn["tools"])]
        if "thinking" in turn:
            args += ["--thinking", turn["thinking"]]
        delivery = job.get("delivery")
        if delivery == "none":
            # Silent run: the agent reports only through its own prompt, never to a chat.
            args += ["--no-deliver"]
        elif delivery:
            if report_to is None:
                raise SystemExit(f"{job['name']}: set {delivery['to_env']} to the report destination")
            args += ["--announce", "--channel", delivery["channel"], "--to", report_to]
    return args


def openclaw_plan(jobs: dict, root: Path, existing: dict[str, str], report_to: str | None) -> list[list[str]]:
    plan = []
    for job in jobs["jobs"]:
        flags = openclaw_args(job, root, report_to)
        if job["name"] in existing:
            plan.append(["openclaw", "cron", "edit", existing[job["name"]], *flags])
        else:
            plan.append(["openclaw", "cron", "add", "--name", job["name"],
                         "--declaration-key", f"openstock-algo:{slug(job['name'])}:v1", *flags])
    return plan


def cron_lines(jobs: dict, root: Path) -> tuple[list[str], list[str]]:
    lines = [CRON_BEGIN, "CRON_TZ=America/New_York"]
    skipped = []
    for job in jobs["jobs"]:
        if "command" not in job:
            skipped.append(f"{job['name']}: agent turn needs OpenClaw; not installed in crontab")
            continue
        schedule = job["schedule"]
        expr = schedule.get("cron") or schedule.get("cron_fallback")
        if expr is None:
            skipped.append(f"{job['name']}: no cron expression for system cron")
            continue
        if "cron" not in schedule:
            skipped.append(f"{job['name']}: {schedule['every']} interval approximated as '{expr}'")
        argv = job["command"]["argv"]
        log = root / "data" / "automations" / f"{slug(job['name'])}.log"
        command = f"timeout {job['command']['timeout_seconds']} {shlex.join(argv)}"
        lines.append(f"# {job['name']}")
        lines.append(f"{expr} cd {shlex.quote(str(root))} && mkdir -p {shlex.quote(str(log.parent))} "
                     f"&& {command} >> {shlex.quote(str(log))} 2>&1")
    lines.append(CRON_END)
    return lines, skipped


def install_crontab(block: list[str]) -> None:
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = current.stdout if current.returncode == 0 else ""
    kept, inside = [], False
    for line in existing.splitlines():
        if line == CRON_BEGIN:
            inside = True
        elif line == CRON_END:
            inside = False
        elif not inside:
            kept.append(line)
    text = "\n".join([*kept, *block]) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".cron", delete=False) as handle:
        handle.write(text)
        path = handle.name
    try:
        subprocess.run(["crontab", path], check=True)
    finally:
        os.unlink(path)


def existing_openclaw_jobs() -> dict[str, str]:
    out = subprocess.run(["openclaw", "cron", "list", "--all", "--json"], capture_output=True, text=True, check=True).stdout
    return {job["name"]: job["id"] for job in json.loads(out[out.index("{"):])["jobs"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", choices=["auto", "openclaw", "cron"], default="auto")
    parser.add_argument("--dry-run", action="store_true", help="print what would be installed")
    args = parser.parse_args(argv)

    jobs = load_jobs()
    backend = args.backend
    if backend == "auto":
        backend = "openclaw" if shutil.which("openclaw") else "cron"
    report_to = os.environ.get("ALGO_REPORT_TELEGRAM_TO")

    if backend == "openclaw":
        plan = openclaw_plan(jobs, ROOT, existing_openclaw_jobs(), report_to)
        for command in plan:
            shown = [part if len(part) < 120 else part[:117] + "..." for part in command]
            print(shlex.join(shown) if args.dry_run else f"run: {shlex.join(shown)}")
            if not args.dry_run:
                subprocess.run(command, check=True)
        return 0

    block, skipped = cron_lines(jobs, ROOT)
    for note in skipped:
        print(f"skipped: {note}", file=sys.stderr)
    if args.dry_run:
        print("\n".join(block))
    else:
        install_crontab(block)
        print(f"installed {len(block) - 3} crontab entries under {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
