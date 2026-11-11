from execution.momentum_paper import plan_rebalance


def _quote(price):
    return {"bid": price, "ask": price + 0.01, "mid": price + 0.005}


def test_exits_come_first_then_trims_and_buys_are_priced_passively_at_the_bid():
    basket = [{"symbol": "AAA", "weight": 0.5}, {"symbol": "BBB", "weight": 0.5}]
    positions = {"AAA": {"qty": "100"}, "CCC": {"qty": "20"}}
    prices = {"AAA": 100.0, "BBB": 50.0, "CCC": 10.0}
    quotes = {s: _quote(p) for s, p in prices.items()}
    intents = plan_rebalance(basket, 10_000.0, positions, prices, quotes)
    assert [i["symbol"] for i in intents][0] == "CCC"
    exit_ = intents[0]
    assert exit_["side"] == "sell" and exit_["qty"] == 20 and exit_["reason"].startswith("exit")
    trim = next(i for i in intents if i["symbol"] == "AAA")
    assert trim["side"] == "sell" and trim["qty"] == 50  # 100 held, 50 is half of 10k at 100
    buy = next(i for i in intents if i["symbol"] == "BBB")
    assert buy["side"] == "buy" and buy["qty"] == 100
    assert buy["limit_price"] == quotes["BBB"]["bid"]  # passive: rests at the bid


def test_marketable_limits_for_exits_sit_below_the_bid():
    basket = [{"symbol": "AAA", "weight": 1.0}]
    intents = plan_rebalance(basket, 1_000.0, {"ZZZ": {"qty": "5"}}, {"AAA": 10.0, "ZZZ": 10.0},
                             {"AAA": _quote(10.0), "ZZZ": _quote(10.0)})
    exit_ = intents[0]
    assert exit_["side"] == "sell" and exit_["limit_price"] < 10.0


def test_rebalance_is_due_once_per_quarter_in_its_first_week_or_at_first_run():
    from datetime import date
    from execution.momentum_paper import rebalance_due
    assert rebalance_due(date(2026, 10, 2), {}) is True  # never rebalanced
    assert rebalance_due(date(2026, 10, 2), {"last_rebalance": "2026-07-01", "last_quarter": "2026-Q3"}) is True
    assert rebalance_due(date(2026, 10, 20), {"last_rebalance": "2026-07-01", "last_quarter": "2026-Q3"}) is False
    assert rebalance_due(date(2026, 10, 2), {"last_quarter": "2026-Q4"}) is False
