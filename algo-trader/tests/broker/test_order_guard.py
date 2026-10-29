import pytest

from broker import endpoints
from broker.order_guard import MAX_ORDER_USD, check_order


def test_limit_order_at_the_cap_is_allowed():
    check_order({"symbol": "AAPL", "qty": "1000", "type": "limit", "limit_price": "100"})


def test_order_above_the_cap_is_refused_on_paper_too():
    with pytest.raises(ValueError, match="per-order limit"):
        check_order({"symbol": "AAPL", "qty": "1001", "type": "limit", "limit_price": "100"})


def test_notional_orders_are_capped_too():
    check_order({"symbol": "AAPL", "type": "market", "notional": "100000"})
    with pytest.raises(ValueError, match="per-order limit"):
        check_order({"symbol": "AAPL", "type": "market", "notional": "100001"})


def test_orders_without_a_price_or_notional_are_not_valued_here():
    # The Algo builder sends only limit orders; this check only sizes what carries a price or a notional.
    check_order({"symbol": "AAPL", "qty": "1", "type": "market"})


def test_options_value_includes_the_contract_multiplier():
    payload = {"order_class": "mleg", "qty": "1000", "type": "limit", "limit_price": "1.00"}
    check_order(payload, multiplier=100.0)  # 1,000 contracts x $1.00 x 100 = $100,000
    with pytest.raises(ValueError, match="per-order limit"):
        check_order({**payload, "qty": "1001"}, multiplier=100.0)


def test_limit_order_needs_a_positive_quantity_and_price():
    with pytest.raises(ValueError, match="positive quantity and limit price"):
        check_order({"symbol": "AAPL", "qty": "0", "type": "limit", "limit_price": "1"})


def test_cap_is_one_hundred_thousand_dollars():
    assert MAX_ORDER_USD == 100_000.0


def test_non_paper_url_needs_the_live_opt_in(monkeypatch):
    monkeypatch.setenv("ALPACA_EQUITY_BASE_URL", "https://api.alpaca.markets/v2")
    monkeypatch.delenv("ALPACA_ALLOW_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="ALPACA_ALLOW_LIVE=1"):
        endpoints._base("ALPACA_EQUITY_BASE_URL")
    monkeypatch.setenv("ALPACA_ALLOW_LIVE", "1")
    assert endpoints._base("ALPACA_EQUITY_BASE_URL") == "https://api.alpaca.markets/v2"


def test_paper_is_the_default_and_needs_no_opt_in(monkeypatch):
    monkeypatch.delenv("ALPACA_OPTIONS_BASE_URL", raising=False)
    monkeypatch.delenv("ALPACA_ALLOW_LIVE", raising=False)
    assert endpoints._base("ALPACA_OPTIONS_BASE_URL") == endpoints.PAPER_TRADING_BASE
