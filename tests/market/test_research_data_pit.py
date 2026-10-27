import datetime

import pandas as pd

from market import research_data as api_free


def _entry(start, end, val, filed, form):
    row = {"end": end, "val": val, "filed": filed, "form": form}
    if start is not None:
        row["start"] = start
    return row


FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": [
                _entry("2024-01-01", "2024-03-31", 100, "2024-05-10", "10-Q"),
                # Restatement of Q1, filed after the 2024-06-01 as-of date used below.
                _entry("2024-01-01", "2024-03-31", 120, "2025-02-10", "10-K"),
                _entry("2024-01-01", "2024-06-30", 200, "2024-08-05", "10-Q"),  # YTD 6 months
                _entry("2024-10-01", "2024-12-31", 90, "2025-02-15", "10-K"),  # Q4 3 months
                _entry("2024-01-01", "2024-12-31", 400, "2025-02-15", "10-K"),  # full year
            ]}},
            "Assets": {"units": {"USD": [
                _entry(None, "2024-03-31", 5000, "2024-05-10", "10-Q"),
                _entry(None, "2024-03-31", 5100, "2025-02-10", "10-K"),
            ]}},
            "NetIncomeLoss": {"units": {"USD": [
                _entry("2024-01-01", "2024-03-31", 10, "2024-05-10", "10-Q"),
            ]}},
        }
    }
}


class _NoCache:
    def get_financial_metrics(self, key):
        return None

    def set_financial_metrics(self, key, value):
        pass


def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(api_free, "_cache", _NoCache())
    monkeypatch.setattr(api_free, "_disk_cache_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_free, "_disk_cache_set", lambda *args, **kwargs: None)


def _values(rows):
    return [(row["end"], row["value"]) for row in rows]


def test_restatement_filed_after_as_of_is_not_visible():
    before = api_free._extract_xbrl_values(FACTS, "Revenues", period="quarterly", as_of="2024-06-01")
    after = api_free._extract_xbrl_values(FACTS, "Revenues", period="quarterly", as_of="2025-03-01")
    assets_before = api_free._extract_xbrl_values(FACTS, "Assets", period="quarterly", as_of="2024-06-01")
    assets_after = api_free._extract_xbrl_values(FACTS, "Assets", period="quarterly", as_of="2025-03-01")

    assert ("2024-03-31", 100) in _values(before)
    assert 120 not in [row["value"] for row in before]
    assert ("2024-03-31", 120) in _values(after)
    assert 100 not in [row["value"] for row in after]
    assert assets_before[0]["value"] == 5000
    assert assets_after[0]["value"] == 5100


def test_quarterly_annual_and_ytd_facts_do_not_mix():
    quarterly = api_free._extract_xbrl_values(FACTS, "Revenues", period="quarterly", as_of="2025-03-01")
    annual = api_free._extract_xbrl_values(FACTS, "Revenues", period="annual", as_of="2025-03-01")

    assert ("2024-12-31", 90) in _values(quarterly)
    assert 400 not in [row["value"] for row in quarterly]
    assert 200 not in [row["value"] for row in quarterly]
    assert _values(annual) == [("2024-12-31", 400)]


def test_build_historical_metrics_uses_only_facts_known_at_end_date():
    rows = api_free._build_historical_metrics("TEST", FACTS, "2024-06-01", "quarterly", 10)

    assert [row["report_period"] for row in rows] == ["2024-03-31"]
    assert rows[0]["earnings_per_share"] is None
    assert rows[0]["asset_turnover"] == 100 / 5000
    assert rows[0]["operating_margin"] == 10 / 100


def test_past_as_of_never_reads_live_yfinance(monkeypatch):
    _no_disk_cache(monkeypatch)

    def live_forbidden(ticker):
        raise AssertionError("live yfinance read for a past as-of date")

    monkeypatch.setattr(api_free, "_get_yf_ticker", live_forbidden)
    monkeypatch.setattr(api_free, "_resolve_cik", lambda ticker: "123")
    monkeypatch.setattr(api_free, "_get_company_facts_sec", lambda cik: FACTS)

    rows = api_free.get_financial_metrics("TEST", "2024-06-01", period="quarterly", limit=5)

    assert [row.report_period for row in rows] == ["2024-03-31"]
    assert rows[0].market_cap is None
    assert rows[0].price_to_earnings_ratio is None


def test_past_as_of_without_point_in_time_facts_returns_missing_marker(monkeypatch):
    _no_disk_cache(monkeypatch)
    monkeypatch.setattr(api_free, "_get_yf_ticker", lambda ticker: (_ for _ in ()).throw(AssertionError("live read")))
    monkeypatch.setattr(api_free, "_resolve_cik", lambda ticker: None)

    assert api_free.get_financial_metrics("TEST", "2024-06-01") == []


def test_past_as_of_market_cap_is_missing(monkeypatch):
    monkeypatch.setattr(api_free, "_get_yf_ticker", lambda ticker: (_ for _ in ()).throw(AssertionError("live read")))

    assert api_free.get_market_cap("TEST", "2024-06-01") is None


def test_live_as_of_reports_statement_period_end(monkeypatch):
    _no_disk_cache(monkeypatch)
    today = datetime.date.today().isoformat()

    class FakeTicker:
        info = {"currency": "USD", "marketCap": 1}
        financials = pd.DataFrame({pd.Timestamp("2026-06-30"): [1000.0]}, index=["Total Revenue"])
        balance_sheet = pd.DataFrame()
        cashflow = pd.DataFrame()

    monkeypatch.setattr(api_free, "_get_yf_ticker", lambda ticker: FakeTicker())
    monkeypatch.setattr(api_free, "_resolve_cik", lambda ticker: None)

    rows = api_free.get_financial_metrics("TEST", today, period="ttm", limit=1)

    assert [row.report_period for row in rows] == ["2026-06-30"]
