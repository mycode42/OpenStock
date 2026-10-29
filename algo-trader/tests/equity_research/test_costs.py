import inspect

from equity_research import costs
from equity_research.backtest import AlgoPortfolioBacktest
from equity_research.pipeline import adaptive_pipeline


def test_shared_equity_cost_values_are_unchanged():
    assert costs.EQUITY_SLIPPAGE_BPS == 5.0
    assert costs.EQUITY_SPREAD_BPS == 3.0
    assert costs.EQUITY_COMMISSION_PER_SHARE == 0.005


def test_backtest_defaults_use_shared_costs():
    parameters = inspect.signature(AlgoPortfolioBacktest.__init__).parameters

    assert parameters["slippage_bps"].default == costs.EQUITY_SLIPPAGE_BPS == 5
    assert parameters["spread_bps"].default == costs.EQUITY_SPREAD_BPS == 3.0
    assert parameters["commission_per_share"].default == costs.EQUITY_COMMISSION_PER_SHARE == 0.005


def test_adaptive_cost_model_uses_shared_costs():
    model = adaptive_pipeline._ADAPTIVE_COST_MODEL

    assert model["slippage_bps"] == 5.0
    assert model["spread_bps"] == 3.0
    assert model["commission_per_share"] == 0.005
