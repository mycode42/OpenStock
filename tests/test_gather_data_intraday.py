from __future__ import annotations

import gather_data


def test_intraday_bar_fetch_defaults_to_one_minute(monkeypatch):
    calls = []
    bars = [{"t": "2026-10-14T13:31:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}]

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"bars": bars}

    def fake_get(url, *, headers, params, timeout):
        calls.append({"url": url, "params": params})
        return Response()

    monkeypatch.setattr(gather_data, "_alpaca_headers", lambda *args, **kwargs: {})
    monkeypatch.setattr(gather_data, "regular_feed", lambda: "sip")
    monkeypatch.setattr(gather_data.requests, "get", fake_get)

    assert gather_data.get_intraday_bars("SPY", "2026-10-14") == bars
    assert calls[0]["params"]["timeframe"] == "1Min"
    assert calls[0]["params"]["limit"] == 500


def test_intraday_bar_fetch_can_preserve_five_minute_consumers(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"bars": []}

    def fake_get(url, *, headers, params, timeout):
        calls.append(params["timeframe"])
        return Response()

    monkeypatch.setattr(gather_data, "_alpaca_headers", lambda *args, **kwargs: {})
    monkeypatch.setattr(gather_data, "regular_feed", lambda: "sip")
    monkeypatch.setattr(gather_data.requests, "get", fake_get)

    gather_data.get_intraday_bars("SPY", "2026-10-14", timeframe="5Min")
    assert calls == ["5Min"]
