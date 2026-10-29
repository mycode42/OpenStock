"""A research fill needs a two-sided quote; an unquoted leg must not abort the sleeve."""

from dataclasses import fields

from options.data import ChainQuote
from options.research.execution import is_two_sided


def _quote(bid, ask):
    base = {field.name: 0 for field in fields(ChainQuote)}
    base.update(bid=bid, ask=ask)
    return ChainQuote(**base)


def test_two_sided_needs_a_positive_bid_and_an_ask_at_or_above_it():
    assert is_two_sided(_quote(1.0, 1.2))
    assert not is_two_sided(_quote(0.0, 1.2))
    assert not is_two_sided(_quote(1.3, 1.2))
