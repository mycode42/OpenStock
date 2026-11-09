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
