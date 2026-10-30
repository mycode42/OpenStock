from datetime import date, datetime, timedelta, timezone
import json

import pytest
import requests

from market.quotes import QuoteParseError
from options import market_inputs as mi
from options.data import market_pair, parse_snapshot_quote
from options.models import OptionContract

TODAY = date(2026, 10, 27)
CONFIG = {"rate": 0.04, "dividend_yield": 0.015, "dividend_yields": {"GLD": 0.0}}


def _fred_csv(series: str = "DGS3MO") -> str:
    return (
        f"observation_date,{series}\n"
        f"2026-10-23,3.70\n"
        f"2026-10-26,.\n"
        f"2026-10-26,3.62\n"
        f"2026-10-28,3.99\n"
    )


def test_parse_fred_series_skips_missing_and_future_observations():
    observed, percent = mi.parse_fred_series_csv(_fred_csv(), "DGS3MO", today=TODAY)

    assert observed == date(2026, 10, 26)
    assert percent == pytest.approx(3.62)


def test_parse_fred_series_rejects_missing_column():
    with pytest.raises(ValueError, match="DGS3MO"):
        mi.parse_fred_series_csv("observation_date,OTHER\n2026-10-26,1\n", "DGS3MO", today=TODAY)


def test_refresh_writes_cache_and_rate_reports_source_and_observation_date(tmp_path):
    cache = tmp_path / "market_inputs.json"
    result = mi.refresh_market_inputs(
        CONFIG, cache_path=cache, today=TODAY,
        fetch=lambda series_id, *, today: (date(2026, 10, 26), 3.62),
    )

    assert result["status"] == "refreshed"
    assert json.loads(cache.read_text(encoding="utf-8"))["rate"]["value"] == pytest.approx(0.0362)
    value = mi.MarketInputs(CONFIG, cache_path=cache).rate(TODAY)
    assert value.value == pytest.approx(0.0362)
    assert value.source == "cache:fred:DGS3MO"
    assert value.as_of == "2026-10-26"


def test_refresh_is_skipped_when_already_refreshed_this_session(tmp_path):
    cache = tmp_path / "market_inputs.json"
    calls = []

    def fetch(series_id, *, today):
        calls.append(series_id)
        return date(2026, 10, 26), 3.62

    mi.refresh_market_inputs(CONFIG, cache_path=cache, today=TODAY, fetch=fetch)
    second = mi.refresh_market_inputs(CONFIG, cache_path=cache, today=TODAY, fetch=fetch)

    assert second["status"] == "already_refreshed"
    assert calls == ["DGS3MO"]


def test_network_failure_keeps_config_fallback_and_does_not_write(tmp_path):
    cache = tmp_path / "market_inputs.json"

    def failing(series_id, *, today):
        raise requests.ConnectionError("offline")

    result = mi.refresh_market_inputs(CONFIG, cache_path=cache, today=TODAY, fetch=failing)

    assert result == {"status": "fallback", "error": "ConnectionError"}
    assert not cache.exists()
    value = mi.MarketInputs(CONFIG, cache_path=cache).rate(TODAY)
    assert (value.value, value.source) == (0.04, "config:rate")


def test_stale_cache_and_future_observation_fall_back_to_config(tmp_path):
    cache = tmp_path / "market_inputs.json"
    old_fetch = (TODAY - timedelta(days=10)).isoformat()
    cache.write_text(json.dumps({
        "schema_version": 1,
        "rate": {"value": 0.05, "observed": "2026-10-20", "fetched_on": old_fetch, "source": "fred:DGS3MO"},
    }), encoding="utf-8")

    stale = mi.MarketInputs(CONFIG, cache_path=cache).rate(TODAY)
    assert (stale.value, stale.source) == (0.04, "config:rate")

    cache.write_text(json.dumps({
        "schema_version": 1,
        "rate": {"value": 0.05, "observed": "2026-10-29", "fetched_on": TODAY.isoformat(), "source": "fred:DGS3MO"},
    }), encoding="utf-8")
    future = mi.MarketInputs(CONFIG, cache_path=cache).rate(TODAY)
    assert (future.value, future.source) == (0.04, "config:rate")


def test_dividend_yield_uses_per_underlying_config_then_scalar_then_default():
    inputs = mi.MarketInputs(CONFIG, cache_path=None)

    gld = inputs.dividend_yield("GLD", TODAY)
    spy = inputs.dividend_yield("SPY", TODAY)
    bare = mi.MarketInputs({}, cache_path=None).dividend_yield("SPY", TODAY)

    assert (gld.value, gld.source) == (0.0, "config:dividend_yields")
    assert (spy.value, spy.source) == (0.015, "config:dividend_yield")
    assert (bare.value, bare.source) == (mi.DEFAULT_DIVIDEND_YIELD, "default:dividend_yield")


def test_research_inputs_ignore_live_cache(tmp_path):
    cache = tmp_path / "market_inputs.json"
    cache.write_text(json.dumps({
        "schema_version": 1,
        "rate": {"value": 0.05, "observed": TODAY.isoformat(), "fetched_on": TODAY.isoformat(), "source": "fred:DGS3MO"},
    }), encoding="utf-8")

    value = mi.research_market_inputs({"risk_free_rate": 0.04}).rate(TODAY)

    assert (value.value, value.source) == (0.04, "config:rate")


def test_fetch_helper_requests_series_with_mocked_http(monkeypatch):
    seen = {}

    class Response:
        text = _fred_csv()

        def raise_for_status(self):
            return None

    def fake_get(url, *, params, timeout):
        seen.update(url=url, params=params, timeout=timeout)
        return Response()

    monkeypatch.setattr(mi.requests, "get", fake_get)

    observed, percent = mi._fetch_fred_series("DGS3MO", today=TODAY)

    assert seen["url"] == mi.FRED_CSV_URL
    assert seen["params"] == {"id": "DGS3MO"}
    assert (observed, percent) == (date(2026, 10, 26), pytest.approx(3.62))


def test_market_pair_uses_inputs_when_given_and_scalars_otherwise():
    as_of = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    inputs = mi.MarketInputs(CONFIG, cache_path=None)

    assert market_pair("GLD", as_of, market_inputs=inputs, rate=0.01, dividend_yield=0.02) == (0.04, 0.0)
    assert market_pair("SPY", as_of, market_inputs=None, rate=0.01, dividend_yield=0.02) == (0.01, 0.02)


def _vendor_contract() -> OptionContract:
    return OptionContract(
        symbol="SPY270115C00100000", underlying="SPY", expiration=date(2027, 1, 15),
        right="call", strike=100.0, exercise_style="european",
    )


def _raw_snapshot(greeks: dict) -> dict:
    return {
        "latestQuote": {"bp": 4.0, "ap": 4.4, "t": "2027-01-01T14:00:00Z"},
        "impliedVolatility": 0.25,
        "greeks": greeks,
    }


def test_complete_vendor_greeks_are_used_with_vega_and_rho_scaled_per_unit():
    as_of = datetime(2027, 1, 1, 14, 0, tzinfo=timezone.utc)
    quote = parse_snapshot_quote(
        _vendor_contract(),
        _raw_snapshot({"delta": 0.52, "gamma": 0.03, "theta": -0.04, "vega": 0.12, "rho": 0.05}),
        spot=100.0, as_of=as_of, feed="indicative", rate=0.04, dividend_yield=0.0,
    )

    assert quote.greeks.source == "vendor_snapshot_greeks"
    assert quote.greeks.delta == pytest.approx(0.52)
    assert quote.greeks.theta == pytest.approx(-0.04)
    assert quote.greeks.vega == pytest.approx(12.0)
    assert quote.greeks.rho == pytest.approx(5.0)


def test_partial_or_implausible_vendor_greeks_fall_back_to_model():
    as_of = datetime(2027, 1, 1, 14, 0, tzinfo=timezone.utc)
    partial = parse_snapshot_quote(
        _vendor_contract(), _raw_snapshot({"delta": 0.52}),
        spot=100.0, as_of=as_of, feed="indicative", rate=0.04, dividend_yield=0.0,
    )
    implausible = parse_snapshot_quote(
        _vendor_contract(),
        _raw_snapshot({"delta": 1.7, "gamma": 0.03, "theta": -0.04, "vega": 0.12, "rho": 0.05}),
        spot=100.0, as_of=as_of, feed="indicative", rate=0.04, dividend_yield=0.0,
    )

    assert partial.greeks.source == "crr_iv_surface"
    assert implausible.greeks.source == "crr_iv_surface"
    assert 0 < partial.greeks.delta < 1


def _quote_raw(quote: dict) -> dict:
    return {"latestQuote": quote, "impliedVolatility": 0.25, "greeks": {}}


def test_an_ask_without_a_bid_is_priced_at_the_ask_with_a_zero_bid():
    as_of = datetime(2027, 1, 1, 14, 0, tzinfo=timezone.utc)
    quote = parse_snapshot_quote(
        _vendor_contract(), _quote_raw({"bp": 0, "ap": 0.77, "t": "2027-01-01T14:00:00Z"}),
        spot=100.0, as_of=as_of, feed="indicative", rate=0.04, dividend_yield=0.0,
    )

    assert quote is not None
    assert quote.bid == 0.0
    assert quote.ask == pytest.approx(0.77)
    assert quote.greeks.implied_volatility > 0


def test_quotes_missing_both_sides_or_crossed_are_still_rejected():
    as_of = datetime(2027, 1, 1, 14, 0, tzinfo=timezone.utc)
    for bad in (
        {"bp": 0, "ap": 0, "t": "2027-01-01T14:00:00Z"},
        {"bp": 1.2, "ap": 1.0, "t": "2027-01-01T14:00:00Z"},
    ):
        with pytest.raises(QuoteParseError):
            parse_snapshot_quote(
                _vendor_contract(), _quote_raw(bad),
                spot=100.0, as_of=as_of, feed="indicative", rate=0.04, dividend_yield=0.0,
            )
