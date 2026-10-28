"""Compact pickling of chain models must round-trip exactly and be smaller."""

import pickle
from datetime import date, datetime, timezone

from options.data import ChainQuote
from options.models import GreeksSnapshot, OptionContract


def _quote(index: int) -> ChainQuote:
    as_of = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    contract = OptionContract(
        symbol=f"SPY261120C{index:08d}", underlying="SPY", expiration=date(2026, 11, 20),
        right="call", strike=500.0 + index, open_interest=100 + index,
    )
    greeks = GreeksSnapshot(delta=0.5, gamma=0.01, theta=-0.1, vega=0.2, rho=0.01,
                            implied_volatility=0.2, observed_at=as_of)
    return ChainQuote(contract=contract, bid=1.0 + index, ask=1.1 + index, greeks=greeks,
                      observed_at=as_of, feed="sip")


def test_round_trip_is_exact():
    quotes = [_quote(index) for index in range(50)]
    restored = pickle.loads(pickle.dumps(quotes, protocol=pickle.HIGHEST_PROTOCOL))
    assert restored == quotes
    assert all(type(item) is ChainQuote for item in restored)


def test_compact_form_is_smaller_than_the_default_dict_form():
    quotes = [_quote(index) for index in range(200)]
    compact = len(pickle.dumps(quotes, protocol=pickle.HIGHEST_PROTOCOL))
    default_size = sum(
        len(pickle.dumps(item.__dict__, protocol=pickle.HIGHEST_PROTOCOL)) for item in quotes
    )
    assert compact < default_size + 200 * 60  # well below the per-object dict form
