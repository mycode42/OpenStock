from src.algo.research.adaptive import (
    DEFAULT_SEARCH_SPACES,
    _baseline_improvement,
    _candidate_parameters,
    _fit_window_ladder,
    _make_parameters,
    _ordered_window_options,
    _search_space,
)
from src.algo.research.cli import build_parser
from src.algo.research.adaptive_pipeline import _write_adaptive_coordinator_artifact
from src.algo.research.portfolio_simulation import _read_artifact, _validate_artifacts
from src.algo.research.settings import (
    ADAPTIVE_WINDOW_DAYS, RESEARCHABLE_STRATEGIES, STRATEGY_FLAVORS,
)
from src.algo.strategies.registry import PARAMETER_KEYS


def test_adaptive_search_domains_match_and_validate_for_all_research_strategies():
    assert set(DEFAULT_SEARCH_SPACES) == set(RESEARCHABLE_STRATEGIES)

    for strategy in RESEARCHABLE_STRATEGIES:
        space = _search_space(strategy, None)
        assert set(space) <= PARAMETER_KEYS[strategy]
        for parameter, values in space.items():
            assert values
            for value in values:
                _make_parameters(strategy, {parameter: value})

        candidates = _candidate_parameters(strategy, "random", 16, 17, space)
        assert len(candidates) == 16


def test_adaptive_cli_stages_cover_all_research_strategies():
    commands = build_parser()._subparsers._group_actions[0].choices
    for command_name in ("adaptive-search", "adaptive-walk-forward"):
        command = commands[command_name]
        strategy_argument = next(action for action in command._actions if action.dest == "strategy")
        assert set(strategy_argument.choices) == set(RESEARCHABLE_STRATEGIES)


def test_auto_research_window_ranges_match_intraday_and_swing_horizons():
    assert ADAPTIVE_WINDOW_DAYS["intraday"] == (10, 20, 30)
    assert ADAPTIVE_WINDOW_DAYS["swing"] == (30, 60, 90)
    assert set(STRATEGY_FLAVORS) >= set(RESEARCHABLE_STRATEGIES)
    for strategy in RESEARCHABLE_STRATEGIES:
        assert ADAPTIVE_WINDOW_DAYS[STRATEGY_FLAVORS[strategy]]


def test_adaptive_window_order_starts_at_middle_or_previous_winner():
    assert _ordered_window_options([10, 20, 30]) == [20, 10, 30]
    assert _ordered_window_options([30, 60, 90], preferred=90) == [90, 60, 30]


def test_fit_window_ladder_tries_neighbor_when_primary_has_no_improvement(monkeypatch):
    calls = []

    def fake_search(*, start, **kwargs):
        window = 10 if start.day == 21 else 20
        calls.append(window)
        baseline = {
            "name": "baseline", "parameters": {"value": 0}, "fitness": 0.1,
            "eligible": False, "metrics": {"net_return_pct": 0.0},
        }
        selected = {
            "name": "candidate", "parameters": {"value": 1},
            "fitness": 0.2 if window == 10 else 0.1,
            "eligible": window == 10, "metrics": {"net_return_pct": 1.0 if window == 10 else 0.0},
        }
        return {
            "selection_status": "eligible_positive_candidate" if window == 10 else "no_positive_candidate",
            "selected_candidate": selected if window == 10 else baseline,
            "candidates": [baseline, selected],
            "leaderboard": [selected, baseline],
        }

    monkeypatch.setattr("src.algo.research.adaptive._run_search_with_bars", fake_search)
    import datetime

    sessions = [datetime.date(2026, 1, day) for day in range(1, 31)]
    fitted = _fit_window_ladder(
        strategy="momentum", method="random", count=2, seed=1, space={"value": [0, 1]},
        symbols=["SPY"], bars={}, spy_daily=[], train_sessions=sessions,
        args=object(), weights={"return": 1.0}, min_trades=1,
        window_options=[10, 20, 30],
    )

    assert calls == [20, 10]
    assert fitted["fit_window_days"] == 10
    assert fitted["adaptive_improvement"] is True
    assert fitted["selected_candidate"]["name"] == "candidate"


def test_fit_window_ladder_tries_every_length_and_retains_best_baseline(monkeypatch):
    calls = []

    def fake_search(*, start, **kwargs):
        window = 31 - start.day
        calls.append(window)
        baseline_fitness = {10: 0.1, 20: 0.3, 30: 0.2}[window]
        baseline = {
            "name": "baseline", "parameters": {"value": 0},
            "fitness": baseline_fitness, "eligible": True,
            "metrics": {"net_return_pct": 0.1},
        }
        candidate = {
            "name": "candidate", "parameters": {"value": 1},
            "fitness": baseline_fitness - 0.1, "eligible": False,
            "metrics": {"net_return_pct": -0.1},
        }
        return {
            "selection_status": "no_positive_candidate",
            "selected_candidate": baseline, "candidates": [baseline, candidate],
            "leaderboard": [baseline, candidate],
        }

    monkeypatch.setattr("src.algo.research.adaptive._run_search_with_bars", fake_search)
    import datetime

    sessions = [datetime.date(2026, 1, day) for day in range(1, 31)]
    fitted = _fit_window_ladder(
        strategy="momentum", method="random", count=2, seed=2, space={"value": [0, 1]},
        symbols=["SPY"], bars={}, spy_daily=[], train_sessions=sessions,
        args=object(), weights={"return": 1.0}, min_trades=1,
        window_options=[10, 20, 30],
    )

    assert calls == [20, 10, 30]
    assert fitted["adaptive_improvement"] is False
    assert fitted["fit_window_days"] == 20
    assert fitted["selected_candidate"]["name"] == "baseline"


def test_adaptive_improvement_requires_sample_qualified_positive_candidate():
    baseline = {"name": "baseline", "fitness": 0.1, "eligible": False}
    selected = {"name": "new", "fitness": 0.2, "eligible": False}
    comparison = _baseline_improvement({"candidates": [baseline, selected], "selected_candidate": selected})
    assert comparison["improved"] is False


def test_verified_adaptive_artifact_matches_portfolio_coordinator_schema(tmp_path):
    strategy = "momentum"
    detail = {
        "strategy": strategy,
        "data": {
            "database": str(tmp_path / "bars.sqlite"), "symbols": ["SPY", "QQQ"],
            "start": "2016-01-04", "end": "2026-10-09", "feed": "sip",
            "adjustment": "split", "data_tier": "algo_trader_plus",
        },
        "folds": [
            {
                "train_start": f"202{index}-01-01", "train_end": f"202{index}-03-31",
                "test_start": f"202{index}-04-01", "test_end": f"202{index}-06-30",
                "selected_candidate": "adaptive_candidate",
                "train_parameters": {"lookbacks": [5, 10, 20], "top_n": 3},
                "train_controls": {"max_entries_per_day": 10},
                "fit_window_days": 60, "adaptive_improvement": index % 2 == 0,
                "oos_metrics": {"net_return_pct": 0.2},
            }
            for index in range(3, 6)
        ],
    }
    summary = {"checks": {"overall_pass": False}}

    path = _write_adaptive_coordinator_artifact(detail, summary, tmp_path / "portfolio")
    artifact = _read_artifact(path, strategy)
    common = _validate_artifacts({strategy: artifact})

    assert common["start"] == "2016-01-04"
    assert len(common["folds"]) == 3
    assert artifact["verification"]["folds"][0]["selected_parameters"][strategy] == detail["folds"][0]["train_parameters"]
    assert artifact["promotion_gate"]["promotable"] is False
