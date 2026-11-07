import json
from datetime import date, timedelta
import math

import pytest

from equity_research import auto_research
from equity_research.factor_research import FACTOR_RESEARCH_START, _feature_panel, train_factor_model
from equity_research.settings import INITIAL_HISTORY_DATE


def _sessions(start: date, count: int) -> list[date]:
    result = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _synthetic_daily_history(session_count: int = 850) -> dict[str, list[dict]]:
    sessions = _sessions(date(2023, 4, 17), session_count)
    output = {}
    for symbol_index in range(26):
        name = f"T{symbol_index:02d}"
        rows = []
        close = 25.0 + symbol_index
        for index, session in enumerate(sessions):
            daily_return = (
                (symbol_index - 12) * 0.00008
                + 0.0015 * math.sin(index / (4 + symbol_index % 9) + symbol_index)
                + 0.0008 * math.sin(index / 31 + symbol_index * 0.7)
            )
            close *= 1 + daily_return
            rows.append({"date": session.isoformat(), "c": close})
        output[name] = rows
    spy_close = 200.0
    spy = []
    for index, session in enumerate(sessions):
        spy_close *= 1 + 0.0002 + 0.003 * math.sin(index / 22)
        spy.append({"date": session.isoformat(), "c": spy_close})
    output["SPY"] = spy
    return output




def _selected_research_universe():
    return {
        "tickers": [f"T{index:02d}" for index in range(40)],
        "selector_as_of": "2026-10-30",
        "selector_rules_sha256": "rules-hash",
        "selector_snapshot": "selector-snapshot.json",
    }


def test_auto_research_uses_latest_live_selector_universe(monkeypatch):
    monkeypatch.setattr(
        "equity_research.data.selected_universe", _selected_research_universe,
    )
    symbols, metadata = auto_research.load_algo_universe()

    assert symbols == [f"T{index:02d}" for index in range(40)]
    assert metadata == {
        "universe_source": "live_eligible_selector",
        "selector_as_of": "2026-10-30",
        "selector_rules_sha256": "rules-hash",
        "selector_snapshot": "selector-snapshot.json",
        "selected_universe_count": 40,
    }


def test_auto_research_fails_closed_for_empty_live_selector_universe(monkeypatch):
    monkeypatch.setattr(
        "equity_research.data.selected_universe", lambda: {"tickers": []},
    )
    with pytest.raises(ValueError, match="empty research universe"):
        auto_research.load_algo_universe()


def test_auto_research_backfills_symbols_missing_from_the_market_store(monkeypatch):
    end_date = date(2026, 10, 8)
    monkeypatch.setattr(
        "equity_research.data.selected_universe", _selected_research_universe,
    )
    symbols = auto_research.load_algo_universe()[0]
    missing_symbol = symbols[-1]
    coverage = [
        {
            "symbol": symbol, "feed": "sip", "adjustment": "split", "timeframe": "5Min",
            "first": "2023-04-16", "last": end_date.isoformat(),
        }
        for symbol in symbols[:-1]
    ]
    coverage.append({
        "symbol": "SPY", "feed": "sip", "adjustment": "split", "timeframe": "1Day",
        "first": "2023-04-16", "last": end_date.isoformat(),
    })

    class Store:
        def coverage(self):
            return coverage

        def write_bars(self, *args, **kwargs):
            return 0

    calls = []
    monkeypatch.setattr(auto_research, "data_tier", lambda: "algo_trader_plus")
    monkeypatch.setattr(auto_research, "regular_feed", lambda: "sip")
    monkeypatch.setattr(
        auto_research, "fetch_alpaca_bars",
        lambda requested, **kwargs: calls.append((list(requested), kwargs)) or {},
    )

    report = auto_research._refresh_incremental(
        Store(), selected_symbols=symbols, end_date=end_date,
    )

    assert report["missing_selected_history"] == [missing_symbol]
    assert calls[0][0] == [missing_symbol]
    assert calls[0][1]["start"] == f"{INITIAL_HISTORY_DATE.isoformat()}T00:00:00Z"


def test_factor_features_are_as_of_and_forward_labels_are_purged():
    history = _synthetic_daily_history(500)
    sessions = [date.fromisoformat(row["date"]) for row in history["SPY"]]
    panel = _feature_panel(history, session_dates=sessions, min_universe=20)
    assert panel
    first = panel[0]
    assert (first["label_end"] - first["date"]).days >= 27
    assert (panel[1]["date"] - first["date"]).days >= 27

    changed = {symbol: [dict(row) for row in rows] for symbol, rows in history.items()}
    ticker = next(symbol for symbol in changed if symbol != "SPY")
    for row in changed[ticker]:
        if row["date"] > first["date"].isoformat():
            row["c"] *= 3
    revised = _feature_panel(changed, session_dates=sessions, min_universe=20)[0]
    assert revised["zscores"][ticker] == first["zscores"][ticker]
    assert revised["returns"][ticker] != first["returns"][ticker]


def test_ridge_factor_candidate_reports_recent_rolling_evaluation():
    report = train_factor_model(_synthetic_daily_history(), min_universe=20)
    weights = report["current_candidate_weights"]
    assert set(weights) == {"momentum_1m", "momentum_3m", "momentum_6m", "low_volatility"}
    assert all(value >= 0 for value in weights.values())
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    assert report["ridge_alpha"] == 1.0
    assert report["research_start"] == FACTOR_RESEARCH_START.isoformat()
    assert report["training_lookback_observations"] == 12
    assert report["adaptive_prequential_ic"]["sample_count"] >= 8
    assert "strictly before" in report["purge_rule"]
    assert report["current_training_label_cutoff"] < report["session_end"]


def _force_failed_cycle(monkeypatch, output_dir):
    def boom(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'lightgbm'")

    monkeypatch.setattr(auto_research, "run_daily_research", boom)
    # Keep the progress log out of the real research log directory.
    monkeypatch.setattr(auto_research, "EQUITY_LOG_PATH", output_dir / "equity.log")
    # These tests cover latest.json error fallback, not Sunday's weekly-review routing.
    return auto_research.main([
        "--output-dir", str(output_dir), "--no-refresh",
        "--verification-history", "recent",
    ])


def test_failed_cycle_keeps_the_last_good_report_and_records_the_error_separately(tmp_path, monkeypatch):
    import json

    good = {"status": "completed", "run_id": "session-20261028-abc", "note": "last good"}
    (tmp_path / "latest.json").write_text(json.dumps(good), encoding="utf-8")

    assert _force_failed_cycle(monkeypatch, tmp_path) == 2

    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8")) == good
    error = json.loads((tmp_path / "latest-error.json").read_text(encoding="utf-8"))
    assert error["status"] == "error"
    assert error["error"].startswith("ModuleNotFoundError")
    assert error["run_id"] == f"error-{error['invocation_id']}"


def test_failed_cycle_fills_latest_when_no_good_report_exists(tmp_path, monkeypatch):
    import json

    assert _force_failed_cycle(monkeypatch, tmp_path) == 2

    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "error"
    assert (tmp_path / "latest-error.json").exists()


def test_completed_cycle_cli_summary_handles_missing_optional_data(monkeypatch, tmp_path, capsys):
    import json

    class Ledger:
        def __init__(self, *args, **kwargs):
            pass

        def interrupt_abandoned_invocations(self, **kwargs):
            return 0

        def start_invocation(self, *args, **kwargs):
            pass

        def import_legacy(self, *args, **kwargs):
            pass

        def record_completed_report(self, *args, **kwargs):
            pass

    monkeypatch.setattr(auto_research, "ResearchLedger", Ledger)
    monkeypatch.setattr(auto_research, "_configure_progress_logging", lambda *_, **__: None)
    monkeypatch.setattr(
        auto_research, "run_daily_research",
        lambda _args: {"status": "completed", "weekly_review": {"status": "pending_decision"}},
    )

    assert auto_research.main([
        "--output-dir", str(tmp_path), "--no-refresh", "--verification-history", "recent",
    ]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed"
    assert summary["range"] is None
    assert summary["stored_coverage"] is None


def test_lab_run_is_isolated_and_can_be_scoped_to_chosen_strategies(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_research, "RESEARCHABLE_STRATEGIES", auto_research.RESEARCHABLE_STRATEGIES)
    parser = auto_research.build_parser()
    args = parser.parse_args([
        "--lab-run", "--output-dir", str(tmp_path),
        "--strategies", "intraday_trend_following,swing_momentum",
        "--verification-history", "full",
    ])
    # A lab run never takes the full-history path, even when asked for it: that path writes the shared weekly review.
    assert auto_research._full_history_verification(args) is False
    auto_research._apply_strategy_scope(args, parser)
    assert auto_research.RESEARCHABLE_STRATEGIES == ("swing_momentum", "intraday_trend_following")
    assert auto_research._configure_progress_logging(tmp_path, lab_run=True) == tmp_path / "equity.log"


def test_unknown_strategy_is_refused_before_a_run_starts(monkeypatch):
    monkeypatch.setattr(auto_research, "RESEARCHABLE_STRATEGIES", auto_research.RESEARCHABLE_STRATEGIES)
    parser = auto_research.build_parser()
    args = parser.parse_args(["--strategies", "swing_momentum,made_up"])
    with pytest.raises(SystemExit):
        auto_research._apply_strategy_scope(args, parser)


def test_lab_events_use_the_prefix_the_research_lab_reads(capsys):
    auto_research._lab_emit({"event": "underlying_done", "underlying": "swing_momentum", "status": "completed", "approved": False})
    line = capsys.readouterr().out.strip()
    assert line.startswith("LAB_EVENT ")
    assert json.loads(line[len("LAB_EVENT "):])["underlying"] == "swing_momentum"


def test_run_scope_takes_dates_and_tickers_and_refuses_bad_ones(monkeypatch):
    monkeypatch.setattr(auto_research, "RESEARCHABLE_STRATEGIES", auto_research.RESEARCHABLE_STRATEGIES)
    monkeypatch.setattr(auto_research, "RESEARCH_START", auto_research.RESEARCH_START)
    parser = auto_research.build_parser()
    args = parser.parse_args(["--underlyings", "aapl, msft", "--start", "2023-01-01", "--end", "2026-06-30"])
    auto_research._apply_strategy_scope(args, parser)
    assert args.underlying_list == ["AAPL", "MSFT"]
    assert auto_research.RESEARCH_START.isoformat() == "2023-01-01"
    assert args.end_date.isoformat() == "2026-06-30"
    with pytest.raises(SystemExit):
        auto_research._apply_strategy_scope(parser.parse_args(["--underlyings", "bad ticker!"]), parser)
    with pytest.raises(SystemExit):
        auto_research._apply_strategy_scope(parser.parse_args(["--start", "2019-12-31"]), parser)
    with pytest.raises(SystemExit):
        auto_research._apply_strategy_scope(parser.parse_args(["--start", "2024-02-30"]), parser)
