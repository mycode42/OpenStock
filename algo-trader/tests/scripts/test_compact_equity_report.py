"""The prompt projection stays small while the full report remains untouched."""

import json

from scripts import compact_equity_report, llm_report


def _report():
    return {
        "schema": "test-v1",
        "invocation_id": "inv-1",
        "run_date_et": "2026-11-07",
        "status": "completed",
        "research_only": True,
        "trade_or_order_actions": 0,
        "orchestration": {
            "run_id": "run-1", "cache_hit": False, "feed": "sip",
            "strategies": ["swing_mean_reversion"], "symbols": ["SPY"],
            "private_blob": "x" * 50_000,
        },
        "data": {
            "feed": "sip", "tier": "Plus", "latest_session_age_days": 1,
            "stored_coverage": {"start": "2024-01-01", "end": "2026-11-06"},
            "refresh_private_rows": ["x" * 10_000],
        },
        "fitness_timeline": {
            "current": {"strategies": {"swing_mean_reversion": {"discovery": {"attempts": [{"fitness_delta_vs_baseline": 0.12}]}, "verification": {"folds": 7, "pass": False}}}},
            "comparison_to_previous": {"comparable": False, "reason": "scope changed"},
            "recent_runs": [{"run_date_et": "2026-11-07"}],
            "next_run_plan": {"policy": "rolling OOS required"},
        },
        "factor_model": {
            "session_end": "2026-11-06", "latest_factor_observation_date": "2026-10-30",
            "weight_timeline": [{"weights": {"x": 1}}] * 1000,
        },
        "portfolio_summary": {"count": 7, "compounded_fold_return_pct": -1.5},
        "portfolio_coordinator_replay": {
            "stage": "portfolio_coordinator_simulation",
            "fold_summary": {"count": 7, "positive_out_of_sample_folds": 2},
            "folds": [{"detail": "x" * 10_000}] * 100,
            "strategy_artifacts": {"detail": "x" * 10_000},
        },
        "adaptive_research": {
            "discovery": {"status": "completed", "error_strategies": []},
            "verification": {"status": "completed", "verification_range": {"start": "2024-01-01"}},
        },
    }


def test_projection_preserves_report_metrics_and_omits_bulk():
    full = _report()
    compact = compact_equity_report.compact_equity_report(full)

    assert compact["fitness_timeline"] == full["fitness_timeline"]
    assert compact["orchestration"]["strategies"] == ["swing_mean_reversion"]
    assert compact["data"]["stored_coverage"]["end"] == "2026-11-06"
    assert compact["factor_model"]["session_end"] == "2026-11-06"
    assert "weight_timeline" not in compact["factor_model"]
    assert compact["portfolio_coordinator_replay"]["fold_summary"]["count"] == 7
    assert "folds" not in compact["portfolio_coordinator_replay"]
    assert "strategy_artifacts" not in compact["portfolio_coordinator_replay"]
    assert "private_blob" not in compact["orchestration"]
    assert len(json.dumps(compact).encode()) < compact_equity_report.MAX_COMPACT_BYTES


def test_writer_keeps_extended_source_unchanged_and_prompt_context_compact(tmp_path, monkeypatch, capsys):
    source = tmp_path / "latest.json"
    output = tmp_path / "latest-prompt.json"
    original = json.dumps(_report(), separators=(",", ":")).encode()
    source.write_bytes(original)

    size = compact_equity_report.write_compact_report(source, output)
    assert source.read_bytes() == original
    compact = json.loads(output.read_text(encoding="utf-8"))
    assert size == output.stat().st_size
    assert output.stat().st_mode & 0o777 == 0o600

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Summarise run {ROOT}.", encoding="utf-8")
    monkeypatch.setattr(llm_report, "load_dotenv", lambda *args, **kwargs: None)
    assert llm_report.main([
        "--prompt", str(prompt), "--context", str(output), "--dry-run",
    ]) == 0
    assembled = capsys.readouterr().out
    assert "fitness_delta_vs_baseline" in assembled
    assert "compounded_fold_return_pct" in assembled
    assert len(assembled.encode()) < 20_000
    assert compact["status"] == "completed"


def test_projection_fails_closed_when_still_oversized(tmp_path, monkeypatch):
    source = tmp_path / "large.json"
    output = tmp_path / "compact.json"
    report = _report()
    report["fitness_timeline"] = {"current": "x" * (compact_equity_report.MAX_COMPACT_BYTES + 1)}
    source.write_text(json.dumps(report), encoding="utf-8")
    try:
        compact_equity_report.write_compact_report(source, output)
    except ValueError as exc:
        assert "safety limit" in str(exc)
    else:
        raise AssertionError("oversized prompt projection must fail closed")
    assert not output.exists()
