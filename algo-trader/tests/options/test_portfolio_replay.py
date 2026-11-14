import json
from datetime import datetime, timezone

from options.data import ChainQuote, OptionChain
from options.research.dataset import OptionsResearchDataset, ResearchObservation, combine_options_research_datasets
from options.research.runner import RESEARCH_DEFAULTS_PATH
from options.research.simulator import simulate_options_strategy
from options.research.strategy_configuration import StrategyConfiguration
from options.research.strategy_factory import live_defaults
from options.models import GreeksSnapshot, OptionContract, OptionLeg, OptionSignal, OptionStructure, RiskMetrics


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
    configurations = {
        name: StrategyConfiguration({}, ("range_bound",))
        for name in ("vrp", "llm_driven")
    }

    result = simulate_options_strategy(
        portfolio, portfolio.observations, strategy_name="vrp",
        strategy_configuration=configurations["vrp"],
        strategy_configurations=configurations,
        strategy_underlyings={"vrp": {"SPY"}, "llm_driven": {"AAPL"}},
        config=config, base_config=live_defaults(),
    )

    assert [row["date"] for row in result["equity_curve"]] == ["2026-10-26", "2026-10-27"]
    assert [row["equity"] for row in result["equity_curve"]] == [100000.0, 100000.0]
    assert result["metrics"]["sessions"] == 2
    assert result["metrics"]["quality"]["observations_total"] == 4
    assert result["metrics"]["quality"]["missing_chain_observations"] == 4


def test_expiring_trade_does_not_replace_underlying_with_contract_symbol(monkeypatch, tmp_path):
    first_at = datetime(2026, 10, 26, 20, tzinfo=timezone.utc)
    expiry = first_at.date().replace(day=27)
    contract = OptionContract(
        symbol="THETA:SPY:20261027:C:100", underlying="SPY", expiration=expiry,
        right="call", strike=100, open_interest=1000,
    )
    greeks = GreeksSnapshot(delta=0.001, observed_at=first_at)
    structure = OptionStructure(
        structure_id="expiry-regression", strategy="test", structure_type="long_call",
        underlying="SPY", legs=(OptionLeg(contract, "buy", bid=4.0, ask=4.1, greeks=greeks),),
        net_debit_per_share=4.05, greeks=greeks,
        risk=RiskMetrics(max_loss=100, max_profit=None, estimated_margin=0, premium_at_risk=405),
        created_at=first_at,
    )
    signal = OptionSignal(
        strategy="test", underlying="SPY", structure=structure, desired_contracts=1,
        confidence=1.0, reason="expiration regression", regime="range_bound", created_at=first_at,
    )
    observations = []
    for day in (26, 27):
        as_of = datetime(2026, 10, day, 20, tzinfo=timezone.utc)
        quote = ChainQuote(
            contract=contract, bid=4.0, ask=4.1, greeks=greeks,
            observed_at=as_of, feed="historical",
        )
        observations.append(ResearchObservation(
            as_of=as_of, underlying="SPY", spot=100.0, regime="range_bound",
            volatility_state="normal", feed="historical", events=(), equity_signals={},
            event_recommendations={}, dividends={}, source_line=day,
            chain=OptionChain("SPY", 100.0, as_of, (quote,), "historical"),
        ))
    dataset = OptionsResearchDataset(
        observations=tuple(observations), provider=_Provider(), source_path=tmp_path / "SPY.sqlite3",
        source_sha256="SPY", data_mode="historical_chains", underlying_history={"SPY": ()},
        loading_parallelism={},
    )
    config = json.loads(RESEARCH_DEFAULTS_PATH.read_text(encoding="utf-8"))
    config["market_data"]["minimum_contracts_per_chain"] = 1
    configuration = StrategyConfiguration({}, ("range_bound",))

    class _SignalOnce:
        def __init__(self):
            self.emitted = False

        def generate(self, context):
            if self.emitted:
                return None
            self.emitted = True
            return signal

    strategy = _SignalOnce()
    monkeypatch.setattr("options.research.simulator.make_strategy", lambda *args, **kwargs: (strategy, {}))
    monkeypatch.setattr("options.research.simulator._hash_uniform", lambda *args: 0.0)

    result = simulate_options_strategy(
        dataset, dataset.observations, strategy_name="test",
        strategy_configuration=configuration,
        strategy_configurations={"test": configuration},
        config=config, base_config=live_defaults(),
        force_close=True,
    )

    assert result["metrics"]["trade_count"] == 1
    assert [row["date"] for row in result["equity_curve"]] == ["2026-10-26", "2026-10-27"]
