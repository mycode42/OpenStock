"""Automation definitions render to OpenClaw flags and system cron lines."""

import json
from pathlib import Path

import pytest

from scripts.setup_automations import ROOT, cron_lines, load_jobs, openclaw_args, openclaw_plan, render


def test_definitions_name_every_job_once_and_prompts_exist():
    jobs = load_jobs()["jobs"]
    names = [job["name"] for job in jobs]
    assert len(names) == len(set(names))
    assert "llm-driven" in names
    assert "options-health" not in names
    assert "event-briefing" not in names
    for job in jobs:
        if "agent_turn" in job:
            assert (ROOT / job["agent_turn"]["prompt"]).is_file(), job["name"]


def test_every_automation_is_assigned_to_the_main_agent():
    for job in load_jobs()["jobs"]:
        assert job["agent"] == "main", job["name"]
        args = openclaw_args(job, ROOT, report_to="123")
        assert args[args.index("--agent") + 1] == "main", job["name"]


def test_prompts_use_root_placeholder_not_a_hardcoded_checkout():
    for prompt in (ROOT / "ops" / "openclaw_prompts").glob("*.md"):
        text = prompt.read_text(encoding="utf-8")
        assert "/opt/" not in text, prompt.name


def test_openclaw_args_carry_schedule_timeout_and_delivery_target():
    jobs = {job["name"]: job for job in load_jobs()["jobs"]}
    args = openclaw_args(jobs["equity-research"], ROOT, report_to="123")
    assert args[args.index("--cron") + 1] == "0 20 * * *"
    assert args[args.index("--timeout-seconds") + 1] == "43200"
    assert args[args.index("--to") + 1] == "123"
    assert "--announce" in args and args[args.index("--thinking") + 1] == "medium"
    assert str(ROOT) in args[args.index("--message") + 1]

    health_args = openclaw_args(jobs["health"], ROOT, report_to=None)
    assert "--no-deliver" in health_args


def test_agent_job_without_report_destination_fails_fast():
    jobs = {job["name"]: job for job in load_jobs()["jobs"]}
    with pytest.raises(SystemExit, match="ALGO_REPORT_TELEGRAM_TO"):
        openclaw_args(jobs["equity-research"], ROOT, report_to=None)


def test_openclaw_plan_edits_existing_jobs_and_adds_missing_ones():
    jobs = load_jobs()
    plan = openclaw_plan(jobs, ROOT, existing={"health": "abc"}, report_to="123")
    health = plan[0]  # health is the first definition
    assert health[:4] == ["openclaw", "cron", "edit", "abc"]
    added = [cmd for cmd in plan if cmd[:3] == ["openclaw", "cron", "add"]]
    assert added and all("--declaration-key" in cmd for cmd in added)


def test_cron_fallback_installs_command_jobs_only_and_names_skips():
    block, skipped = cron_lines(load_jobs(), ROOT)
    assert block[0].startswith("# BEGIN") and block[-1].startswith("# END")
    assert "CRON_TZ=America/New_York" in block
    entries = [line for line in block if line and not line.startswith(("#", "CRON_TZ"))]
    assert len(entries) == 4
    assert any("portfolio.healthcheck" in line for line in entries)
    assert not any("options.healthcheck" in line for line in entries)
    assert not any("research.equity" in line for line in entries)
    assert any("llm-driven" in note for note in skipped)
    assert any("25h" in note for note in skipped)


def test_silent_jobs_disable_delivery_and_need_no_report_destination():
    jobs = {job["name"]: job for job in load_jobs()["jobs"]}
    args = openclaw_args(jobs["llm-driven"], ROOT, report_to=None)
    assert "--no-deliver" in args and "--announce" not in args
    assert args[args.index("--cron") + 1] == "0 4-20 * * 1-5"
