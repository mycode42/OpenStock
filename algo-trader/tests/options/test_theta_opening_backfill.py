"""Focused tests for resumable ThetaData opening-quote day failures."""

from datetime import date, datetime, timedelta
import gzip
import json

import pytest

from scripts.options import theta_opening_backfill as opening


START = date(2024, 1, 1)
END = date(2024, 1, 3)


class FakeThetaClient:
    def __init__(self):
        self.calls = []
        self.failures_left = 2

    def option_at_time_quote(self, *, symbol, start_date, **kwargs):
        self.calls.append((symbol, start_date))
        if start_date == START and self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("Corrupt FIT data: record has more fields than established tick width")
        return [{"symbol": symbol, "date": start_date.isoformat(), "bid": 1.0}]


def _read_archive(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def test_failed_day_is_saved_and_only_that_day_is_retried(monkeypatch, tmp_path):
    client = FakeThetaClient()
    research = tmp_path / "theta"
    monkeypatch.setattr(opening, "RESEARCH_DIR", research)
    monkeypatch.setattr(opening, "_load_stock_rows_from_db", lambda *a, **k: [
        {"last_trade": START.isoformat(), "open": 100.0},
        {"last_trade": date(2024, 1, 2).isoformat(), "open": 101.0},
    ])
    monkeypatch.setattr(
        opening,
        "_source_symbol_ranges",
        lambda symbol, start, end: [(symbol, start, end)],
    )
    monkeypatch.setattr(opening, "_records", lambda frame: frame)
    monkeypatch.setattr(opening, "_read_api_key", lambda: "test-secret")
    monkeypatch.setattr(opening.time_module, "sleep", lambda seconds: None)
    monkeypatch.setattr(opening, "_month_chunks", lambda *a, **k: [(START, END)])
    monkeypatch.setattr(
        opening,
        "_compile_opening_symbol",
        lambda symbol, **kwargs: {
            "underlying": symbol,
            "archive_complete": opening._archive_covers_through(
                research / symbol / "archive" / "opening_quote" / "2024-01.json.gz", END,
            ),
            "dataset_sessions": 2,
            "contracts_in_dataset": 0,
            "archive_months_complete": 1,
            "archive_months_expected": 1,
        },
    )

    opening._fetch_symbol(client, "XYZ", start_date=START, end_date=END)
    archive_path = research / "XYZ" / "archive" / "opening_quote" / "2024-01.json.gz"
    first = _read_archive(archive_path)

    assert client.calls == [("XYZ", START), ("XYZ", START), ("XYZ", date(2024, 1, 2))]
    assert len(first["rows"]) == 1  # The later good day was persisted despite the failed day.
    assert [{k: v for k, v in record.items() if k not in {"first_failed_at", "last_failed_at"}} for record in first["failed_requests"]] == [{
        "symbol": "XYZ",
        "date": START.isoformat(),
        "reason": "Corrupt FIT data: record has more fields than established tick width",
        "attempts": 1,
    }]
    assert opening._archive_covers_through(archive_path, END) is False

    opening._fetch_symbol(client, "XYZ", start_date=START, end_date=END)
    second = _read_archive(archive_path)
    assert client.calls[-1] == ("XYZ", START)
    assert len(client.calls) == 4  # The already successful Jan 2 request was not repeated.
    assert second["failed_requests"] == []
    assert len(second["rows"]) == 2
    assert opening._archive_covers_through(archive_path, END) is True


def test_legacy_malformed_no_data_entry_is_not_complete(tmp_path):
    archive_path = tmp_path / "2024-01.json.gz"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive_path, "wt", encoding="utf-8") as stream:
        json.dump({
            "end_date": END.isoformat(),
            "no_data_requests": [{
                "symbol": "XYZ",
                "date": START.isoformat(),
                "reason": "wrong number of data fields in FIT record",
            }],
        }, stream)
    assert opening._archive_covers_through(archive_path, END) is False


class InternalErrorThetaClient:
    def __init__(self, failing_days):
        self.calls = []
        self.failing_days = set(failing_days)

    def option_at_time_quote(self, *, symbol, start_date, **kwargs):
        self.calls.append((symbol, start_date))
        if start_date in self.failing_days:
            raise RuntimeError("<_MultiThreadedRendezvous> status = StatusCode.INTERNAL details = \"Internal server error\"")
        return [{"symbol": symbol, "date": start_date.isoformat(), "bid": 1.0}]


def test_provider_internal_error_is_retried_then_kept_as_unresolved_day(monkeypatch):
    days = [START, date(2024, 1, 2), END]
    client = InternalErrorThetaClient(failing_days=[date(2024, 1, 2)])
    monkeypatch.setattr(opening.time_module, "sleep", lambda seconds: None)
    monkeypatch.setattr(opening, "_records", lambda frame: frame)
    monkeypatch.setattr(opening, "_read_api_key", lambda: "test-secret")

    rows, no_data, failures = opening._download_open_quotes(
        client, [("XYZ", START, END)], days, underlying_symbol="XYZ",
    )

    assert [row["date"] for row in rows] == [START.isoformat(), END.isoformat()]  # The symbol kept going past the bad day.
    assert no_data == []
    assert [record["date"] for record in failures] == [date(2024, 1, 2).isoformat()]
    assert client.calls.count(("XYZ", date(2024, 1, 2))) == opening.PROVIDER_ERROR_RETRIES + 1


def test_consecutive_provider_failures_stop_the_symbol(monkeypatch):
    days = [START + timedelta(days=offset) for offset in range(opening.CONSECUTIVE_FAILED_DAYS_LIMIT + 5)]
    client = InternalErrorThetaClient(failing_days=days)
    monkeypatch.setattr(opening.time_module, "sleep", lambda seconds: None)
    monkeypatch.setattr(opening, "_records", lambda frame: frame)
    monkeypatch.setattr(opening, "_read_api_key", lambda: "test-secret")

    with pytest.raises(RuntimeError, match="failed in a row"):
        opening._download_open_quotes(client, [("XYZ", START, days[-1])], days, underlying_symbol="XYZ")


class AlwaysCorruptThetaClient(FakeThetaClient):
    def __init__(self):
        super().__init__()
        self.failures_left = 10**6


def _patch_symbol_fetch(monkeypatch, research):
    monkeypatch.setattr(opening, "RESEARCH_DIR", research)
    monkeypatch.setattr(opening, "_load_stock_rows_from_db", lambda *a, **k: [
        {"last_trade": START.isoformat(), "open": 100.0},
        {"last_trade": date(2024, 1, 2).isoformat(), "open": 101.0},
    ])
    monkeypatch.setattr(opening, "_source_symbol_ranges", lambda symbol, start, end: [(symbol, start, end)])
    monkeypatch.setattr(opening, "_records", lambda frame: frame)
    monkeypatch.setattr(opening, "_read_api_key", lambda: "test-secret")
    monkeypatch.setattr(opening.time_module, "sleep", lambda seconds: None)
    monkeypatch.setattr(opening, "_month_chunks", lambda *a, **k: [(START, END)])
    monkeypatch.setattr(opening, "_compile_opening_symbol", lambda symbol, **kwargs: {
        "underlying": symbol,
        "archive_complete": opening._archive_covers_through(
            research / symbol / "archive" / "opening_quote" / "2024-01.json.gz", END,
        ),
        "known_provider_gaps": 0,
        "dataset_sessions": 2,
        "contracts_in_dataset": 0,
        "archive_months_complete": 1,
        "archive_months_expected": 1,
    })


def test_day_failing_in_three_runs_becomes_a_known_gap_and_is_retried_weekly(monkeypatch, tmp_path):
    research = tmp_path / "theta"
    _patch_symbol_fetch(monkeypatch, research)
    client = AlwaysCorruptThetaClient()
    archive_path = research / "XYZ" / "archive" / "opening_quote" / "2024-01.json.gz"

    for run in (1, 2, 3):
        opening._fetch_symbol(client, "XYZ", start_date=START, end_date=END)
        record = _read_archive(archive_path)["failed_requests"][0]
        assert record["attempts"] == run
        assert opening._archive_covers_through(archive_path, END) is (run == 3)

    # Known gap: the archive is complete, and the day is not requested again inside the retry window.
    calls_before = len(client.calls)
    opening._fetch_symbol(client, "XYZ", start_date=START, end_date=END)
    assert client.calls[calls_before:] == []
    assert opening._archive_covers_through(archive_path, END) is True
    assert _read_archive(archive_path)["failed_requests"][0]["attempts"] == 3

    # After the retry window the day is requested again, and the gap stays known.
    payload = _read_archive(archive_path)
    old = (datetime.now(opening.ET) - timedelta(days=opening.KNOWN_GAP_RETRY_DAYS + 1)).isoformat()
    payload["failed_requests"][0]["last_failed_at"] = old
    with gzip.open(archive_path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream)
    opening._fetch_symbol(client, "XYZ", start_date=START, end_date=END)
    assert client.calls[-1] == ("XYZ", START)
    record = _read_archive(archive_path)["failed_requests"][0]
    assert record["attempts"] == 4
    assert opening._archive_covers_through(archive_path, END) is True

