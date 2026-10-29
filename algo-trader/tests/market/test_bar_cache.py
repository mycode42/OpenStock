"""The shared bar cache returns the same bar dicts as SQLiteBarStore.bars."""

import sqlite3
from pathlib import Path

import pytest

from market import bar_cache
from market.store import SQLiteBarStore

ROWS = [
    # symbol, timestamp, o, h, l, c, v, trade_count, vwap, ingested_at
    ("AAA", "2024-01-02T14:30:00.000000Z", 10.0, 10.5, 9.9, 10.2, 1000.0, 42.0, 10.1, "2026-01-01T00:00:00+00:00"),
    ("AAA", "2024-01-02T14:35:00.000000Z", 10.2, 10.6, 10.1, 10.4, 1500.0, None, None, "2026-01-01T00:00:00+00:00"),
    ("AAA", "2024-01-03T14:30:00.123456Z", 10.4, 10.9, 10.3, 10.8, 900.0, 7.0, 10.6, "2026-02-01T12:00:00.5+00:00"),
    ("BBB", "2024-01-02T14:30:00.000000Z", 50.0, 51.0, 49.0, 50.5, 2e6, None, 50.2, "2026-01-01T00:00:00+00:00"),
    ("BBB", "2024-06-03T13:30:00.000000Z", 55.0, 56.0, 54.0, 55.5, 3e6, 99.0, None, None),
]


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "market.sqlite3"
    SQLiteBarStore(path)  # creates the schema
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """INSERT INTO market_bar (feed, adjustment, timeframe, symbol, timestamp, open, high, low, close,
               volume, trade_count, vwap, ingested_at) VALUES ('sip','split','5Min',?,?,?,?,?,?,?,?,?,?)""",
            [(s, t, o, h, l, c, v, n, vw, ing or "") for s, t, o, h, l, c, v, n, vw, ing in ROWS],
        )
    return path


def test_cache_bars_equal_store_bars_for_every_window(database, tmp_path):
    cache_root = tmp_path / "cache"
    directory = bar_cache.build(database, cache_root, feed="sip", timeframe="5Min",
                                adjustment="split", symbols=["AAA", "BBB", "CCC"])
    store = SQLiteBarStore(database)
    for start, end in [("2024-01-01", "2024-12-31"), ("2024-01-03", "2024-01-03"), ("2025-01-01", "2025-02-01")]:
        expected = store.bars(["AAA", "BBB", "CCC"], feed="sip", timeframe="5Min", start=start, end=end,
                              adjustment="split")
        actual = bar_cache.window_bars(directory, ["AAA", "BBB", "CCC"], start, end)
        assert list(actual) == list(expected)
        for symbol in expected:
            assert list(actual[symbol]) == expected[symbol], (symbol, start, end)
            assert [type(bar.get("n")) for bar in actual[symbol]] == [type(bar.get("n")) for bar in expected[symbol]]


def test_lazy_bars_support_indexing_slicing_and_get(database, tmp_path):
    directory = bar_cache.build(database, tmp_path / "cache", feed="sip", timeframe="5Min",
                                adjustment="split", symbols=["AAA"])
    bars = bar_cache.window_bars(directory, ["AAA"], "2024-01-01", "2024-12-31")["AAA"]
    assert len(bars) == 3
    assert bars[-1]["t"] == "2024-01-03T14:30:00.123456Z"
    assert bars[0:2] == list(bars)[0:2]
    assert bars[1].get("n") is None and "vw" not in bars[1]
    with pytest.raises(IndexError):
        bars[3]


def test_build_reuses_an_existing_cache(database, tmp_path):
    first = bar_cache.build(database, tmp_path / "cache", feed="sip", timeframe="5Min", adjustment="split",
                            symbols=["AAA"])
    second = bar_cache.build(database, tmp_path / "cache", feed="sip", timeframe="5Min", adjustment="split",
                             symbols=["AAA"])
    assert first == second


def test_build_refuses_data_it_cannot_represent_exactly(tmp_path):
    path = tmp_path / "market.sqlite3"
    SQLiteBarStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("""INSERT INTO market_bar (feed, adjustment, timeframe, symbol, timestamp, open, high,
            low, close, volume, trade_count, vwap, ingested_at) VALUES ('sip','split','5Min','AAA',
            '2024-01-02T14:30:00.000000Z', 'abc', 2, 0, 1, 1000, NULL, NULL, 'x')""")
    with pytest.raises(ValueError, match="not stored as REAL"):
        bar_cache.build(path, tmp_path / "cache", feed="sip", timeframe="5Min", adjustment="split",
                        symbols=["AAA"])
