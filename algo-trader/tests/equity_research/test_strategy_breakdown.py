from equity_research.validation.portfolio_simulation import strategy_breakdown


def test_breakdown_totals_trades_per_strategy_and_regime_across_folds():
    folds = [
        {
            "entry_count_by_strategy": {"swing_momentum": 3, "intraday_momentum": 1},
            "closed_trade_records": [
                {"strategy": "swing_momentum", "entry_regime": "volatile", "pnl": 100.0},
                {"strategy": "swing_momentum", "entry_regime": "range_bound", "pnl": -40.0},
            ],
        },
        {
            "entry_count_by_strategy": {"swing_momentum": 2},
            "closed_trade_records": [{"strategy": "intraday_momentum", "entry_regime": "range_bound", "pnl": -10.0}],
        },
    ]
    out = strategy_breakdown(folds, initial_capital=100_000.0)
    momentum = out["by_strategy"]["swing_momentum"]
    assert momentum["closed_trades"] == 2
    assert momentum["pnl_usd"] == 60.0
    assert momentum["win_rate_pct"] == 50.0
    assert momentum["profit_factor"] == 2.5
    assert out["by_strategy"]["intraday_momentum"]["pnl_usd"] == -10.0
    assert out["entries_by_strategy"] == {"intraday_momentum": 1, "swing_momentum": 5}
    assert out["by_entry_regime"]["range_bound"]["closed_trades"] == 2
    assert out["by_entry_regime"]["volatile"]["pnl_pct_of_initial_capital"] == 0.1


def test_breakdown_splits_gross_pnl_into_spread_slippage_commission_and_borrow():
    folds = [{
        "closed_trade_records": [
            {"strategy": "intraday_momentum", "entry_regime": "range_bound", "pnl": 90.0, "gross_pnl": 100.0,
             "spread_slippage_cost": 4.0, "entry_commission": 3.0, "exit_commission": 2.0, "borrow_cost": 1.0},
            # A record from before the split has no gross figure: its gross is its net plus the costs it recorded.
            {"strategy": "intraday_momentum", "entry_regime": "range_bound", "pnl": -10.0,
             "entry_commission": 1.0, "exit_commission": 1.0, "borrow_cost": 0.0},
        ],
    }]
    group = strategy_breakdown(folds, initial_capital=100_000.0)["by_strategy"]["intraday_momentum"]
    assert group["gross_pnl_usd"] == 92.0
    assert group["spread_slippage_usd"] == 4.0
    assert group["commission_usd"] == 7.0
    assert group["borrow_cost_usd"] == 1.0
    assert group["pnl_usd"] == 80.0
