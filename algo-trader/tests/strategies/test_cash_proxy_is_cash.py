"""Idle cash in the cash-proxy ETF (SGOV) is cash, not exposure.

The scan's position view and the risk account both leave the proxy out of exposure
and count its market value as free cash. Frozen policy: docs/policy-frozen.md.
"""

from strategies.runner.data import current_portfolio
from portfolio.config import is_cash_proxy


def _fake_request(account, positions, orders=()):
    def request(endpoint, *, mode, base=None, params=None):
        return {"account": account, "positions": list(positions), "orders": list(orders)}[endpoint]
    return request


def test_scan_positions_exclude_the_cash_proxy():
    account = {"equity": "100000", "cash": "20000"}
    positions = [
        {"symbol": "SGOV", "qty": "797", "market_value": "80122"},
        {"symbol": "GS", "qty": "10", "market_value": "6500"},
    ]
    _, visible, _ = current_portfolio("algo", request_fn=_fake_request(account, positions))
    assert "SGOV" not in visible
    assert "GS" in visible


def test_proxy_orders_are_not_scan_orders():
    orders = [{"symbol": "SGOV", "side": "buy"}, {"symbol": "GS", "side": "buy"}]
    _, _, visible = current_portfolio("algo", request_fn=_fake_request({"equity": "1", "cash": "1"}, [], orders))
    assert [order["symbol"] for order in visible] == ["GS"]


def test_the_default_proxy_is_sgov():
    assert is_cash_proxy("SGOV")
    assert not is_cash_proxy("GS")
