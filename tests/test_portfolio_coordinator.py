from datetime import datetime
from zoneinfo import ZoneInfo

from src.algo.backtest import AlgoPortfolioBacktest
from src.algo.walk_forward import evaluate_promotion_gate, parameter_neighbors
from src.strategies.portfolio_coordinator import (
    PortfolioCoordinator, PortfolioCoordinatorConfig, choose_priority, throttle_signal,
)


ET = ZoneInfo("America/New_York")


def _proposal(strategy="trend_following", ticker="NVDA", action="buy", qty=100, price=100):
    return {
        "strategy": strategy, "ticker": ticker, "action": action,
        "qty": qty, "entry_price": price,
    }


def test_blocks_opposite_direction_against_existing_book_position():
    coordinator = PortfolioCoordinator()
    book = coordinator.new_book(
        equity=100_000,
        positions={"NVDA": {"qty": 50, "market_value": 5_000}},
        position_owners={"NVDA": "trend_following"},
    )
    result = book.review(
        _proposal(strategy="momentum", action="short"),
        session="regular", timestamp=datetime(2026, 10, 9, 10, 0, tzinfo=ET),
        regime="trending_down",
    )
    assert not result.approved
    assert result.reason == "opposite direction conflicts with an existing position or pending order"


def test_blocks_second_strategy_on_an_already_owned_name():
    coordinator = PortfolioCoordinator()
    book = coordinator.new_book(
        equity=100_000,
        positions={"NVDA": {"qty": 50, "market_value": 5_000}},
        position_owners={"NVDA": "trend_following"},
    )
    result = book.review(
        _proposal(strategy="momentum", action="buy"),
        session="regular", timestamp=datetime(2026, 10, 9, 10, 0, tzinfo=ET),
        regime="trending_up",
    )
    assert not result.approved
    assert result.reason == "another strategy already owns this ticker"


def test_same_strategy_add_is_shrunk_to_the_eight_percent_name_cap():
    coordinator = PortfolioCoordinator()
    book = coordinator.new_book(
        equity=100_000,
        positions={"NVDA": {"qty": 70, "market_value": 7_000}},
        position_owners={"NVDA": "trend_following"},
    )
    result = book.review(
        _proposal(strategy="trend_following", action="buy", qty=100),
        session="regular", timestamp=datetime(2026, 10, 9, 10, 0, tzinfo=ET),
        regime="trending_up",
    )
    assert result.approved
    assert result.proposal["qty"] == 10
    assert result.reduced_from_qty == 100


def test_competing_name_proposals_keep_the_higher_priority_strategy():
    winner, rejected = choose_priority([
        _proposal(strategy="multi_factor", ticker="NVDA", action="buy"),
        _proposal(strategy="trend_following", ticker="NVDA", action="short"),
    ])
    assert winner["strategy"] == "trend_following"
    assert winner["action"] == "short"
    assert rejected[0]["suppressed_reason"].startswith("portfolio coordinator:")


def test_shared_intraday_budget_rejects_fourth_opening():
    config = PortfolioCoordinatorConfig(max_intraday_entries=1)
    coordinator = PortfolioCoordinator(config)
    book = coordinator.new_book(equity=100_000)
    stamp = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
    accepted = book.review(
        _proposal(strategy="mean_reversion", ticker="NVDA", qty=10),
        session="regular", timestamp=stamp,
    )
    rejected = book.review(
        _proposal(strategy="opening_range_breakout", ticker="SPY", qty=10),
        session="regular", timestamp=stamp,
    )
    assert accepted.approved
    assert not rejected.approved
    assert rejected.reason == "shared intraday entry slice is exhausted"


def test_shared_daily_budget_is_ten_and_logs_rejection(caplog):
    coordinator = PortfolioCoordinator()
    book = coordinator.new_book(equity=100_000, entries_today=10)
    result = book.review(
        _proposal(strategy="trend_following"), session="regular",
        timestamp=datetime(2026, 10, 9, 10, 0, tzinfo=ET), regime="trending_up",
    )

    assert coordinator.config.max_daily_entries == 10
    assert not result.approved
    assert result.reason == "shared daily entry budget is exhausted"
    assert "shared daily entry budget is exhausted" in caplog.text


def test_shared_swing_slice_allows_only_three_entries_per_day():
    coordinator = PortfolioCoordinator()
    book = coordinator.new_book(equity=100_000)
    stamp = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
    accepted = [
        book.review(
            _proposal(strategy="trend_following", ticker=ticker, qty=1),
            session="regular", timestamp=stamp, regime="trending_up",
        )
        for ticker in ("NVDA", "AAPL", "JPM")
    ]
    rejected = book.review(
        _proposal(strategy="momentum", ticker="SPY", qty=1),
        session="regular", timestamp=stamp, regime="trending_up",
    )

    assert all(item.approved for item in accepted)
    assert not rejected.approved
    assert rejected.reason == "shared swing entry slice is exhausted"


def test_satellite_cannot_own_an_entry_and_boats_entries_are_disabled():
    coordinator = PortfolioCoordinator()
    book = coordinator.new_book(equity=100_000)
    stamp = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
    satellite = book.review(
        _proposal(strategy="multi_factor"), session="regular", timestamp=stamp,
    )
    overnight = book.review(
        _proposal(strategy="trend_following", ticker="AAPL"), session="overnight",
        timestamp=datetime(2026, 10, 9, 21, 0, tzinfo=ET), regime="trending_up",
    )

    assert not satellite.approved
    assert "confirmation-only" in satellite.reason
    assert not overnight.approved
    assert overnight.reason == "overnight entries are disabled by configuration"


def test_swing_opening_delay_and_regime_throttle():
    coordinator = PortfolioCoordinator()
    book = coordinator.new_book(equity=100_000)
    result = book.review(
        _proposal(), session="regular",
        timestamp=datetime(2026, 10, 9, 9, 40, tzinfo=ET), regime="trending_up",
    )
    assert not result.approved
    adjusted, note = throttle_signal(
        {**_proposal(strategy="swing_mean_reversion"), "risk_multiplier": 0.8},
        "trending_down", coordinator.config,
    )
    assert adjusted["risk_multiplier"] == 0.4
    assert "halved" in note


def test_neighbor_parameter_center_spike_fails_promotion_gate():
    neighbors = parameter_neighbors({
        "swing_mean_reversion": {"stretch_pct": 0.06},
    })
    assert {(row["parameter_key"], row["direction"]) for row in neighbors} == {
        ("swing_mean_reversion.stretch_pct", "lower"),
        ("swing_mean_reversion.stretch_pct", "upper"),
    }
    folds = []
    for index in range(3):
        folds.append({
            "selected_profile": "fixed_candidate",
            "out_of_sample": {
                "net_return_pct": 1.0, "excess_return_vs_buy_hold_pct_points": 0.5,
                "closed_trade_count": 4,
            },
            "neighbor_tests": [
                {
                    "parameter_key": "swing_mean_reversion.stretch_pct",
                    "direction": side,
                    "net_return_pct": -0.1,
                    "excess_return_vs_buy_hold_pct_points": -0.2,
                    "closed_trade_count": 4,
                }
                for side in ("lower", "upper")
            ],
        })
    gate = evaluate_promotion_gate(folds, minimum_test_trades_per_fold=3)
    assert not gate["promotable"]
    assert gate["checks"]["positive_after_costs_in_majority_of_folds"]
    assert not gate["checks"]["neighbor_parameter_stability"]
    assert gate["unstable_parameters"] == ["swing_mean_reversion.stretch_pct"]


def test_fill_costs_apply_spread_and_open_slippage_on_the_adverse_side():
    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={}, spy_daily_bars=[],
        start="2026-10-01", end="2026-10-01", feed="sip", data_adjustment="raw",
        slippage_bps=5, spread_bps=4,
    )
    assert replay._fill_price(100, "buy") == 100.07
    assert round(replay._fill_price(100, "short"), 2) == 99.93


def test_backtest_reports_correlated_and_overlapping_strategy_pairs():
    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={}, spy_daily_bars=[],
        start="2026-10-01", end="2026-10-03", feed="sip", data_adjustment="raw",
    )
    days = ["2026-10-01", "2026-10-02", "2026-10-03"]
    replay.daily_curve = [{"date": day, "equity": 100_000} for day in days]
    replay.daily_strategy_pnl = [
        {"date": day, "pnl_by_strategy": {"trend_following": value, "momentum": value}}
        for day, value in zip(days, (1, 2, 3))
    ]
    for day in days[:2]:
        replay.holdings_by_day[day]["trend_following"].add(("AAPL", "long"))
        replay.holdings_by_day[day]["momentum"].add(("AAPL", "long"))
    report = replay._correlation_report()
    flagged = [
        row for row in report["flags"]
        if {row["strategy_a"], row["strategy_b"]} == {"trend_following", "momentum"}
    ]
    assert len(flagged) == 1
    assert flagged[0]["daily_pnl_correlation"] == 1.0
    assert flagged[0]["same_ticker_same_direction_overlap_days"] == 2
