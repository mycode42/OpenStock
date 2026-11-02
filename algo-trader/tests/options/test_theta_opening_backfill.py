"""Focused tests for resumable ThetaData opening-quote day failures."""

from datetime import date
import gzip
import json

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
    assert first["failed_requests"] == [{
        "symbol": "XYZ",
        "date": START.isoformat(),
        "reason": "Corrupt FIT data: record has more fields than established tick width",
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
