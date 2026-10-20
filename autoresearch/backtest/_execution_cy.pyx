"""Cython implementation of replay timeline preparation."""

from datetime import datetime, time, timezone, timedelta
import math


cdef object UTC = timezone.utc
cdef object REGULAR_OPEN = time(9, 30)
cdef object REGULAR_CLOSE = time(16, 0)


def build_timeline(list symbols, dict intraday_bars, object start, object end,
                   object eastern, int bar_minutes):
    """Build the ordered replay event map while preserving Python semantics."""
    cdef dict events = {}
    cdef dict bars_by_symbol
    cdef dict event_bars
    cdef object ticker, bars, bar, raw_timestamp, timestamp, local
    cdef object local_date, local_time, ingested_at, ingested
    cdef object events_sorted, ordered_events
    cdef double opening, high, low, close
    cdef bint valid

    for ticker in symbols:
        bars = intraday_bars.get(ticker, [])
        for bar in bars:
            raw_timestamp = str(bar["t"]).replace("Z", "+00:00")
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            timestamp = timestamp.astimezone(UTC)
            local = timestamp.astimezone(eastern)
            local_date = local.date()
            if local_date < start or local_date > end:
                continue

            ingested_at = bar.get("_ingested_at")
            if ingested_at:
                ingested = datetime.fromisoformat(str(ingested_at).replace("Z", "+00:00"))
                if ingested.tzinfo is None:
                    ingested = ingested.replace(tzinfo=UTC)
                if timestamp + timedelta(minutes=bar_minutes) > ingested.astimezone(UTC):
                    continue

            local_time = local.time().replace(tzinfo=None)
            if local_time < REGULAR_OPEN or local_time >= REGULAR_CLOSE:
                continue

            # Convert each field before checking any of them, matching the
            # Python implementation's eager list construction and exceptions.
            opening = float(bar["o"])
            high = float(bar["h"])
            low = float(bar["l"])
            close = float(bar["c"])
            valid = (
                math.isfinite(opening) and opening > 0
                and math.isfinite(high) and high > 0
                and math.isfinite(low) and low > 0
                and math.isfinite(close) and close > 0
            )
            if not valid:
                continue

            event_bars = events.get(timestamp)
            if event_bars is None:
                event_bars = {}
                events[timestamp] = event_bars
            event_bars[ticker] = bar

    events_sorted = sorted(events)
    ordered_events = {}
    for timestamp in events_sorted:
        ordered_events[timestamp] = events[timestamp]
    return ordered_events
