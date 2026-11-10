"""Yield on idle cash in the replay: the three-month Treasury bill rate, the proxy for SGOV."""

from __future__ import annotations

import csv
from bisect import bisect_right
from datetime import date
from functools import lru_cache
from pathlib import Path

# Annual expense ratio of SGOV, taken off the bill rate.
SGOV_EXPENSE_RATIO_PCT = 0.09
# Tracked with the code, so every checkout earns the same idle-cash yield.
RATE_FILE = Path(__file__).resolve().parents[1] / "reference" / "dgs3mo.csv"


@lru_cache(maxsize=1)
def _rate_series() -> tuple[tuple[date, ...], tuple[float, ...]]:
    """(dates, percent rates) from the FRED DGS3MO file, sorted, with missing values dropped."""
    if not RATE_FILE.exists():
        return (), ()
    rows = []
    with RATE_FILE.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            value = (row.get("DGS3MO") or "").strip()
            if value in ("", "."):
                continue
            rows.append((date.fromisoformat(row["observation_date"]), float(value)))
    rows.sort()
    return tuple(day for day, _ in rows), tuple(rate for _, rate in rows)


def cash_yield_on(day: date) -> float | None:
    """The bill rate in percent on the last published day on or before ``day``; None before the series starts."""
    dates, rates = _rate_series()
    index = bisect_right(dates, day) - 1
    if index < 0:
        return None
    return rates[index]
