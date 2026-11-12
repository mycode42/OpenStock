import json
from datetime import datetime, timezone

from options.research.dataset import OptionsResearchDataset, ResearchObservation, combine_options_research_datasets
from options.research.runner import RESEARCH_DEFAULTS_PATH
from options.research.search import Candidate
from options.research.simulator import simulate_options_strategy
from options.research.strategy_factory import live_defaults


class _Provider:
    def underlying_closes(self, underlying, *, as_of, sessions):
        return []


class _NoSignalStrategy:
    def generate(self, context):
        return None


def test_portfolio_replay_emits_one_shared_equity_row_per_date(monkeypatch, tmp_path):
    def fake_make_strategy(name, *, parameters, allowed_regimes, base_config):
        return _NoSignalStrategy(), {}

    monkeypatch.setattr("options.research.simulator.make_strategy", fake_make_strategy)
    datasets = {}
    for symbol in ("SPY", "AAPL"):
        observations = tuple(
            ResearchObservation(
                as_of=datetime(2026, 10, day, 20, tzinfo=timezone.utc),
                underlying=symbol, spot=100.0, regime="range_bound",
                volatility_state="normal", feed="historical", events=(),
                equity_signals={}, event_recommendations={}, dividends={},
                source_line=day, chain_error="fixture has no prepared chain",
            )
            for day in (26, 27)
        )
        datasets[symbol] = OptionsResearchDataset(
            observations=observations, provider=_Provider(), source_path=tmp_path / f"{symbol}.sqlite3",
            source_sha256=symbol, data_mode="historical_chains",
            underlying_history={symbol: ()}, loading_parallelism={},
        )
    portfolio = combine_options_research_datasets(datasets)
    config = json.loads(RESEARCH_DEFAULTS_PATH.read_text(encoding="utf-8"))
    candidates = {
        name: Candidate(name, {}, ("range_bound",), (), is_baseline=True)
        for name in ("vrp", "llm_driven")
    }

    result = simulate_options_strategy(
        portfolio, portfolio.observations, strategy_name="vrp", candidate=candidates["vrp"],
        strategy_candidates=candidates,
        strategy_underlyings={"vrp": {"SPY"}, "llm_driven": {"AAPL"}},
        config=config, base_config=live_defaults(),
    )

    assert [row["date"] for row in result["equity_curve"]] == ["2026-10-26", "2026-10-27"]
    assert [row["equity"] for row in result["equity_curve"]] == [100000.0, 100000.0]
    assert result["metrics"]["sessions"] == 2
    assert result["metrics"]["quality"]["observations_total"] == 4
    assert result["metrics"]["quality"]["missing_chain_observations"] == 4
