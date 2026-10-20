from decimal import Decimal

import pytest

from src.accounts import AlpacaAccount
from src import fill_analytics


ACCOUNT = AlpacaAccount(
    name="Algo", account_id="paper-account", api_key="test-key", api_secret="test-secret",
)


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise fill_analytics.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def activity(activity_id, order_id, side, qty, price, minute, symbol="AAPL"):
    return {
        "id": activity_id,
        "activity_type": "FILL",
        "type": "partial_fill",
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": f"2026-10-07T14:{minute:02d}:00Z",
    }


def journal(order_id, action, *, strategy="momentum", mode="algo", ticker="AAPL"):
    return {
        "order_id": order_id,
        "action": action,
        "strategy": strategy,
        "trading_mode": mode,
        "ticker": ticker,
    }


def money(value):
    return Decimal(str(value))


def test_fill_fetch_paginates_with_last_activity_id():
    calls = []
    pages = {
        None: [activity("fill-1", "entry", "buy", 1, 100, 0),
               activity("fill-2", "entry", "buy", 1, 101, 1)],
        "fill-2": [activity("fill-3", "exit", "sell", 2, 110, 2)],
    }

    def get(url, *, headers, params, timeout):
        calls.append((url, dict(params)))
        return Response(pages[params.get("page_token")])

    fills = fill_analytics.fetch_fill_activities(
        ACCOUNT, http_get=get, page_size=2,
    )

    assert [row["id"] for row in fills] == ["fill-1", "fill-2", "fill-3"]
    assert calls[0][0].endswith("/account/activities/FILL")
    assert calls[0][1] == {"direction": "asc", "page_size": 2}
    assert calls[1][1]["page_token"] == "fill-2"


def test_fill_fetch_rejects_duplicate_or_incomplete_history():
    first = [activity("fill-1", "entry", "buy", 1, 100, 0)]

    def repeated(url, *, headers, params, timeout):
        return Response(first)

    with pytest.raises(fill_analytics.FillHistoryError, match="repeated"):
        fill_analytics.fetch_fill_activities(ACCOUNT, http_get=repeated, page_size=1)
    with pytest.raises(fill_analytics.FillHistoryError, match="page limit"):
        fill_analytics.fetch_fill_activities(
            ACCOUNT, http_get=repeated, page_size=1, max_pages=1,
        )


def test_partial_fills_use_decimal_fifo_across_entry_lots():
    fills = [
        activity("buy-1", "entry-1", "buy", "1.5", "100", 0),
        activity("buy-2", "entry-2", "buy", "0.5", "110", 1),
        activity("sell-1", "exit-1", "sell", "1.75", "120", 2),
        activity("sell-2", "exit-2", "sell", "0.25", "90", 3),
    ]
    rows = [
        journal("entry-1", "buy"), journal("entry-2", "buy"),
        journal("exit-1", "sell"), journal("exit-2", "sell"),
    ]

    report = fill_analytics.calculate_realized_pnl(
        fills, rows, mode="algo", current_positions=[],
    )

    assert report["realized_pnl_complete"]
    assert report["position_reconciliation"] == "matched"
    assert money(report["realized_pnl"]) == Decimal("27.5")
    assert money(report["strategy_pnl"]["momentum"]) == Decimal("27.5")
    assert report["strategy_attribution_complete"]


def test_fifo_sorts_by_transaction_time_before_matching():
    fills = [
        activity("sell", "exit", "sell", 1, 110, 2),
        activity("buy", "entry", "buy", 1, 100, 1),
    ]

    report = fill_analytics.calculate_realized_pnl(
        fills, [journal("entry", "buy"), journal("exit", "sell")],
        mode="algo", current_positions=[],
    )

    assert money(report["realized_pnl"]) == Decimal("10")


def test_unmatched_sell_marks_total_unknown_without_inventing_basis():
    report = fill_analytics.calculate_realized_pnl(
        [activity("sell", "exit", "sell", 2, 110, 1)],
        [journal("exit", "sell")],
        mode="algo",
        current_positions=[],
    )

    assert report["realized_pnl"] is None
    assert report["covered_realized_pnl"] == "0"
    assert "AAPL" in report["basis_unknown_symbols"]
    assert report["realized_fills"][0]["realized_pnl"] is None
    assert report["strategy_pnl"] == {}


def test_partial_basis_shortfall_does_not_report_the_matched_fragment():
    fills = [
        activity("buy", "entry", "buy", 1, 100, 0),
        activity("sell", "exit", "sell", 2, 110, 1),
    ]
    report = fill_analytics.calculate_realized_pnl(
        fills, [journal("entry", "buy"), journal("exit", "sell")],
        mode="algo", current_positions=[],
    )

    assert report["realized_pnl"] is None
    assert report["covered_realized_pnl"] == "0"
    assert report["realized_fills"][0]["basis_status"] == "unknown"


def test_broker_inventory_mismatch_invalidates_symbol_pnl():
    fills = [
        activity("buy", "entry", "buy", 1, 100, 0),
        activity("sell", "exit", "sell", 1, 110, 1),
    ]
    report = fill_analytics.calculate_realized_pnl(
        fills, [journal("entry", "buy"), journal("exit", "sell")],
        mode="algo",
        current_positions=[{"symbol": "AAPL", "qty": "1"}],
    )

    assert report["position_reconciliation"] == "mismatch"
    assert report["inventory_mismatches"]["AAPL"] == {"fills_qty": "0", "broker_qty": "1"}
    assert report["realized_pnl"] is None
    assert report["realized_fills"][0]["realized_pnl"] is None


def test_period_filter_uses_prior_buy_for_cost_basis():
    fills = [
        activity("buy", "entry", "buy", 1, 100, 0),
        activity("sell", "exit", "sell", 1, 110, 2),
    ]
    report = fill_analytics.calculate_realized_pnl(
        fills, [journal("entry", "buy"), journal("exit", "sell")],
        mode="algo", current_positions=[],
        since="2026-10-07T14:01:00Z",
        until="2026-10-07T14:03:00Z",
    )

    assert report["sell_fill_count"] == 1
    assert money(report["realized_pnl"]) == Decimal("10")


def test_bracket_child_attribution_requires_parent_link_and_exact_journal_id():
    fills = [
        activity("buy", "parent", "buy", 1, 100, 0),
        activity("sell", "child", "sell", 1, 110, 1),
    ]
    rows = [journal("parent", "buy", strategy="trend_following")]

    linked = fill_analytics.calculate_realized_pnl(
        fills, rows, mode="algo", current_positions=[],
        bracket_parent_by_child={"child": "parent"},
    )
    unlinked = fill_analytics.calculate_realized_pnl(
        fills, rows, mode="algo", current_positions=[],
    )

    assert money(linked["strategy_pnl"]["trend_following"]) == Decimal("10")
    assert linked["strategy_attribution_complete"]
    assert unlinked["strategy_pnl"] == {}
    assert money(unlinked["realized_pnl"]) == Decimal("10")
    assert unlinked["unattributed_closed_qty"] == "1"
    assert not unlinked["strategy_attribution_complete"]


def test_other_account_mode_or_conflicting_journal_id_cannot_attribute():
    fills = [
        activity("buy", "entry", "buy", 1, 100, 0),
        activity("sell", "exit", "sell", 1, 110, 1),
    ]
    wrong_mode = fill_analytics.calculate_realized_pnl(
        fills, [journal("entry", "buy", mode="swing"), journal("exit", "sell", mode="swing")],
        mode="algo", current_positions=[],
    )
    conflict = fill_analytics.calculate_realized_pnl(
        fills, [
            journal("entry", "buy", strategy="momentum"),
            journal("entry", "buy", strategy="mean_reversion"),
            journal("exit", "sell"),
        ],
        mode="algo", current_positions=[],
    )

    assert wrong_mode["strategy_pnl"] == {}
    assert not wrong_mode["strategy_attribution_complete"]
    assert conflict["journal_order_conflicts"] == ["entry"]
    assert conflict["strategy_pnl"] == {}
    assert not conflict["strategy_attribution_complete"]


def test_zero_pnl_without_attribution_is_still_unattributed():
    fills = [
        activity("buy", "entry", "buy", 1, 100, 0),
        activity("sell", "exit", "sell", 1, 100, 1),
    ]
    report = fill_analytics.calculate_realized_pnl(
        fills, [journal("entry", "buy")], mode="algo", current_positions=[],
    )

    assert report["realized_pnl"] == "0"
    assert report["unattributed_closed_qty"] == "1"
    assert not report["strategy_attribution_complete"]


def test_invalid_fill_or_duplicate_id_fails_closed():
    invalid = activity("buy", "entry", "buy", "NaN", 100, 0)
    with pytest.raises(fill_analytics.FillHistoryError, match="quantity"):
        fill_analytics.calculate_realized_pnl([invalid], [], mode="algo")
    duplicate = activity("same", "entry", "buy", 1, 100, 0)
    with pytest.raises(fill_analytics.FillHistoryError, match="repeated"):
        fill_analytics.calculate_realized_pnl([duplicate, duplicate], [], mode="algo")


def test_bracket_lookup_uses_broker_nested_child_ids_only():
    calls = []
    parent = {
        "id": "parent", "order_class": "bracket", "symbol": "AAPL", "side": "buy",
        "legs": [{"id": "child", "symbol": "AAPL", "side": "sell"}],
    }

    def get(url, *, headers, params, timeout):
        calls.append((url, params))
        if url.endswith("/parent"):
            return Response(parent)
        return Response({}, status_code=404)

    links, errors = fill_analytics.fetch_bracket_parent_links(
        ACCOUNT,
        candidate_parent_order_ids={"parent"},
        unmatched_fill_order_ids={"child"},
        http_get=get,
    )

    assert links == {"child": "parent"}
    assert errors == []
    assert calls == [(f"{ACCOUNT.base_url}/orders/parent", {"nested": "true"})]


def test_non_bracket_or_cross_symbol_order_does_not_create_parent_link():
    parent = {
        "id": "parent", "order_class": "oco", "symbol": "AAPL", "side": "buy",
        "legs": [{"id": "child", "symbol": "AAPL", "side": "sell"}],
    }

    def get(url, *, headers, params, timeout):
        if url.endswith("/parent"):
            return Response(parent)
        return Response({}, status_code=404)

    links, _ = fill_analytics.fetch_bracket_parent_links(
        ACCOUNT,
        candidate_parent_order_ids={"parent"},
        unmatched_fill_order_ids={"child"},
        http_get=get,
    )

    assert links == {}


def test_child_parent_id_fallback_requires_verified_bracket_parent():
    child = {
        "id": "child", "parent_order_id": "parent",
        "symbol": "AAPL", "side": "sell",
    }
    parent = {
        "id": "parent", "order_class": "bracket",
        "symbol": "AAPL", "side": "buy", "legs": [],
    }

    def get(url, *, headers, params, timeout):
        return Response(parent if url.endswith("/parent") else child)

    links, errors = fill_analytics.fetch_bracket_parent_links(
        ACCOUNT,
        candidate_parent_order_ids={"parent"},
        unmatched_fill_order_ids={"child"},
        http_get=get,
    )

    assert links == {"child": "parent"}
    assert errors == []


def test_account_report_verifies_account_and_reads_only_broker_gets(monkeypatch):
    monkeypatch.setattr(fill_analytics, "get_account_for_mode", lambda mode: ACCOUNT)
    calls = []
    fills = [
        activity("buy", "parent", "buy", 1, 100, 0),
        activity("sell", "child", "sell", 1, 110, 1),
    ]
    parent = {
        "id": "parent", "order_class": "bracket", "symbol": "AAPL", "side": "buy",
        "legs": [{"id": "child", "symbol": "AAPL", "side": "sell"}],
    }

    def get(url, *, headers, params, timeout):
        calls.append(url)
        if url.endswith("/account"):
            return Response({"id": "paper-account"})
        if url.endswith("/account/activities/FILL"):
            return Response(fills)
        if url.endswith("/positions"):
            return Response([])
        if url.endswith("/orders/parent"):
            return Response(parent)
        raise AssertionError(f"Unexpected GET {url}")

    report = fill_analytics.get_account_fill_report(
        "algo", [journal("parent", "buy", strategy="trend_following")],
        http_get=get,
    )

    assert money(report["realized_pnl"]) == Decimal("10")
    assert money(report["strategy_pnl"]["trend_following"]) == Decimal("10")
    assert report["source"] == "Alpaca paper account FILL activities"
    assert all(url.startswith(ACCOUNT.base_url) for url in calls)


def test_account_report_rejects_wrong_configured_account(monkeypatch):
    monkeypatch.setattr(fill_analytics, "get_account_for_mode", lambda mode: ACCOUNT)
    calls = []

    def get(url, *, headers, params, timeout):
        calls.append(url)
        return Response({"id": "different-account"})

    with pytest.raises(fill_analytics.FillHistoryError, match="different Alpaca account"):
        fill_analytics.get_account_fill_report("algo", [], http_get=get)

    assert calls == [f"{ACCOUNT.base_url}/account"]
