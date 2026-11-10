from datetime import date

from equity_research.backtest.cash_yield import cash_yield_on


def test_the_bill_rate_is_the_last_published_value_on_or_before_the_day():
    assert cash_yield_on(date(2020, 1, 2)) == 1.54
    # A weekend takes the Friday rate.
    assert cash_yield_on(date(2020, 1, 4)) == cash_yield_on(date(2020, 1, 3))


def test_no_rate_before_the_series_starts():
    assert cash_yield_on(date(2019, 1, 1)) is None
