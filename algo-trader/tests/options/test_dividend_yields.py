from datetime import date
import json

import pytest
import requests

from options import market_inputs as mi

AS_OF = date(2026, 10, 27)
HEADERS = {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _dividend(ex_date, rate):
    return {"symbol": "XYZ", "rate": rate, "special": False, "ex_date": ex_date}


def test_trailing_yield_sums_only_ex_dates_in_last_365_days_over_close():
    rows = [
        _dividend("2025-10-26", 9.0),  # 366 days before as_of: outside the window
        _dividend("2025-10-27", 1.0),  # exactly 365 days before: excluded (exclusive start)
        _dividend("2025-12-19", 2.0),
        _dividend("2026-03-20", 1.0),
        _dividend("2026-10-28", 7.0),  # after as_of: ignored
    ]

    value, count = mi.trailing_dividend_yield(rows, close=100.0, as_of=AS_OF)

    assert count == 2
    assert value == pytest.approx(0.03)


def test_no_dividend_rows_is_a_covered_zero_yield_and_missing_close_is_unresolved():
    assert mi.trailing_dividend_yield([], close=371.99, as_of=AS_OF) == (0.0, 0)
    assert mi.trailing_dividend_yield([_dividend("2026-03-20", 1.0)], close=None, as_of=AS_OF) is None
    assert mi.trailing_dividend_yield([_dividend("2026-03-20", 1.0)], close=0.0, as_of=AS_OF) is None


def test_fetch_cash_dividends_paginates_and_requests_cash_dividends_window():
    calls = []
    pages = [
        FakeResponse({"corporate_actions": {"cash_dividends": [_dividend("2026-03-20", 1.8)]},
                      "next_page_token": "p2"}),
        FakeResponse({"corporate_actions": {"cash_dividends": [_dividend("2026-06-18", 1.9)]},
                      "next_page_token": None}),
    ]

    def fake_get(url, *, headers, params, timeout):
        calls.append((url, dict(params), headers))
        return pages.pop(0)

    rows = mi.fetch_cash_dividends("SPY", as_of=AS_OF, headers=HEADERS, get=fake_get)

    assert [row["ex_date"] for row in rows] == ["2026-03-20", "2026-06-18"]
    assert calls[0][0] == mi.ALPACA_CORPORATE_ACTIONS_URL
    assert calls[0][1]["types"] == "cash_dividend"
    assert calls[0][1]["symbols"] == "SPY"
    assert calls[0][1]["start"] == "2025-10-27" and calls[0][1]["end"] == "2026-10-27"
    assert "page_token" not in calls[0][1] and calls[1][1]["page_token"] == "p2"
    assert calls[0][2] == HEADERS


def test_resolver_uses_alpaca_then_yfinance_then_omits_uncovered_symbols():
    def fake_get(url, *, headers, params, timeout):
        if url == mi.ALPACA_STOCK_SNAPSHOTS_URL:
            return FakeResponse({
                "KO": {"dailyBar": {"c": 100.0}},
                "SPY": {"dailyBar": {"c": 500.0}},
            })
        if params["symbols"] == "KO":
            return FakeResponse({"corporate_actions": {"cash_dividends": [_dividend("2026-01-02", 2.0)]}})
        if params["symbols"] == "SPY":
            return FakeResponse({}, status=503)
        return FakeResponse({"corporate_actions": {"cash_dividends": []}})  # no dividends and no close price

    yfinance_values = {"SPY": 0.011, "BRKX": None}

    table = mi.resolve_dividend_yields(
        ["KO", "SPY", "BRKX"], as_of=AS_OF, headers=HEADERS, get=fake_get,
        yfinance_lookup=lambda symbol: yfinance_values.get(symbol),
    )

    assert table["KO"]["value"] == pytest.approx(0.02)
    assert table["KO"]["source"] == mi.DIVIDEND_SOURCE_ALPACA
    assert table["SPY"] == {
        "value": 0.011, "source": mi.DIVIDEND_SOURCE_YFINANCE,
        "observed": "2026-10-27", "dividend_rows": None,
    }
    assert "BRKX" not in table


def test_refresh_writes_dividend_block_and_keeps_rate_block(tmp_path):
    cache = tmp_path / "market_inputs.json"
    cache.write_text(json.dumps({
        "schema_version": 1,
        "rate": {"value": 0.036, "observed": "2026-10-26", "fetched_on": "2026-10-27", "source": "fred:DGS3MO"},
    }), encoding="utf-8")
    table = {"GLD": {"value": 0.0, "source": mi.DIVIDEND_SOURCE_ALPACA, "observed": "2026-10-27", "dividend_rows": 0}}

    result = mi.refresh_dividend_yields(
        ["GLD"], as_of=AS_OF, headers=HEADERS, cache_path=cache, resolver=lambda *a, **k: table,
    )
    again = mi.refresh_dividend_yields(
        ["GLD"], as_of=AS_OF, headers=HEADERS, cache_path=cache, resolver=lambda *a, **k: pytest.fail("refetched"),
    )

    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert result["status"] == "refreshed" and again["status"] == "already_refreshed"
    assert payload["rate"]["value"] == 0.036
    assert payload["dividend_yield"]["GLD"]["fetched_on"] == "2026-10-27"
    value = mi.MarketInputs({"dividend_yield": 0.015}, cache_path=cache).dividend_yield("GLD", AS_OF)
    assert (value.value, value.source) == (0.0, "cache:" + mi.DIVIDEND_SOURCE_ALPACA)


def test_snapshot_round_trip_and_research_reads_it_without_network(tmp_path, monkeypatch):
    path = tmp_path / "dividend_yields_2026-10-27.json"
    table = {"TLT": {"value": 0.0477, "source": mi.DIVIDEND_SOURCE_ALPACA, "observed": "2026-10-27"}}
    mi.write_dividend_snapshot(table, as_of=AS_OF, path=path)

    def no_network(*args, **kwargs):
        raise AssertionError("research must not touch the network")

    monkeypatch.setattr(mi.requests, "get", no_network)
    monkeypatch.setattr(mi, "ROOT", tmp_path)
    inputs = mi.research_market_inputs({
        "dividend_snapshot_path": "dividend_yields_2026-10-27.json", "dividend_yield": 0.015,
        "dividend_yields": {"GLD": 0.0},
    })

    tlt = inputs.dividend_yield("TLT", AS_OF)
    gld = inputs.dividend_yield("GLD", AS_OF)
    spcx = inputs.dividend_yield("SPCX", AS_OF)
    assert (tlt.value, tlt.source, tlt.as_of) == (0.0477, "snapshot:" + mi.DIVIDEND_SOURCE_ALPACA, "2026-10-27")
    assert (gld.value, gld.source) == (0.0, "config:dividend_yields")
    assert (spcx.value, spcx.source) == (0.015, "config:dividend_yield")


def test_missing_or_malformed_snapshot_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "other"}), encoding="utf-8")

    with pytest.raises(ValueError, match="schema"):
        mi.load_dividend_snapshot(bad)
    with pytest.raises(ValueError, match="unreadable"):
        mi.load_dividend_snapshot(tmp_path / "missing.json")


def test_configured_symbols_cover_every_sleeve_and_overrides():
    config = {
        "vrp": {"underlyings": ["SPY", "QQQ"]},
        "llm_driven": {},
        "directional_overlay": {"underlyings": ["KO"]},
        "long_volatility": {"underlyings": ["spy", "TLT"]},
        "dividend_yields": {"GLD": 0.0},
    }

    assert mi.configured_dividend_symbols(config) == ["GLD", "KO", "QQQ", "SPY", "TLT"]
