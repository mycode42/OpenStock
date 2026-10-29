"""The Cython row grouping must produce exactly the same bar dicts as the Python loop."""

import pytest

from market import _bars_cy

pytestmark = pytest.mark.skipif(_bars_cy is None, reason="bar extension not built")


def _rows():
    return [
        ("SPY", "2026-10-27T13:30:00Z", 1.0, 2.0, 0.5, 1.5, 100, 7, 1.2, "2026-10-27T13:31:00Z"),
        ("SPY", "2026-10-27T13:35:00Z", 1.5, 2.5, 1.0, 2.0, None, None, None, None),
        ("QQQ", "2026-10-27T13:30:00Z", 3.0, 4.0, 2.5, 3.5, 50, None, 3.2, "x"),
    ]


def _python_group(rows, output):
    for symbol, timestamp, opening, high, low, close, volume, count, vwap, ingested_at in rows:
        bar = {"t": timestamp, "o": opening, "h": high, "l": low, "c": close, "v": volume}
        bar["_ingested_at"] = ingested_at
        if count is not None:
            bar["n"] = count
        if vwap is not None:
            bar["vw"] = vwap
        output.setdefault(symbol, []).append(bar)
    return output


def test_grouping_matches_python_exactly():
    rows = _rows()
    cy_out = _bars_cy.group_bar_rows(rows, {"SPY": [], "QQQ": []})
    py_out = _python_group(rows, {"SPY": [], "QQQ": []})
    assert cy_out == py_out
    assert list(cy_out["SPY"][0].keys()) == list(py_out["SPY"][0].keys())
