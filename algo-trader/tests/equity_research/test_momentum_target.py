from datetime import date, timedelta

from equity_research.momentum_target import LOOKBACK_SESSIONS, SKIP_SESSIONS, target_basket


def _series(start_price, end_price, days=LOOKBACK_SESSIONS + 30):
    base = date(2026, 1, 1)
    values = []
    for i in range(days):
        price = start_price + (end_price - start_price) * i / (days - 1)
        values.append((base + timedelta(days=i), price))
    return values


def test_ranks_by_six_month_momentum_and_holds_the_top_names_equally_weighted():
    series = {f"S{i}": _series(100.0, 100.0 + i * 10) for i in range(8)}
    basket = target_basket(series, top_n=3)
    assert [row["symbol"] for row in basket] == ["S7", "S6", "S5"]
    assert all(row["weight"] == round(1 / 3, 4) for row in basket)
    assert basket[0]["momentum_6m"] > basket[1]["momentum_6m"]


def test_names_without_enough_history_are_left_out():
    series = {"NEW": _series(100.0, 200.0, days=LOOKBACK_SESSIONS - 5), "OLD": _series(100.0, 110.0)}
    assert [row["symbol"] for row in target_basket(series, top_n=2)] == ["OLD"]
