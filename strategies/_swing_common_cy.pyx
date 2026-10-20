"""Cython implementation of completed-daily-bar normalization."""

from datetime import date, datetime
import math


def clean_daily_bars(object bars):
    """Normalize daily bars in order, sorting only when input is unordered."""
    cdef list rows = []
    cdef object previous_session = None
    cdef object bar, raw, session
    cdef double close
    cdef bint already_sorted = True

    for bar in bars:
        # Match _clean_daily_bar's lookup placement and exception handling.
        raw = bar.get("date", bar.get("t"))
        try:
            if isinstance(raw, date) and not isinstance(raw, datetime):
                session = raw
            else:
                session = date.fromisoformat(str(raw)[:10])
            close = float(bar["c"])
        except (KeyError, TypeError, ValueError):
            continue

        if math.isfinite(close) and close > 0:
            if previous_session is not None and session < previous_session:
                already_sorted = False
            previous_session = session
            rows.append((session, bar, close))

    if not already_sorted:
        rows.sort(key=lambda item: item[0])
    return rows
