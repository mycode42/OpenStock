"""Cython implementation of the ordered intraday OHLCV aggregation loop."""

from datetime import datetime, time, timezone, timedelta
import math


cdef object REGULAR_OPEN = time(9, 30)
cdef object REGULAR_CLOSE = time(16, 0)
cdef object BAR_DELTA = timedelta(minutes=5)
cdef object UTC = timezone.utc


def aggregate_intraday_bars(dict bars_by_symbol, object eastern, object unordered_fallback):
    """Aggregate ordered regular-session bars while retaining Python semantics."""
    cdef dict output = {}
    cdef object raw_symbol, bars, bar, stamp, previous_stamp, local
    cdef object local_time, ingested_at, ingested, session, state
    cdef object daily
    cdef dict sessions
    cdef bint unordered
    cdef double opening, high, low, close, volume

    for raw_symbol, bars in bars_by_symbol.items():
        sessions = {}
        previous_stamp = None
        unordered = False
        for bar in bars:
            try:
                stamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                if previous_stamp is not None and stamp < previous_stamp:
                    unordered = True
                    break
                previous_stamp = stamp
                local = stamp.astimezone(eastern)
                local_time = local.time().replace(tzinfo=None)
                if not REGULAR_OPEN <= local_time < REGULAR_CLOSE:
                    continue
                ingested_at = bar.get("_ingested_at")
                if ingested_at:
                    ingested = datetime.fromisoformat(str(ingested_at).replace("Z", "+00:00"))
                    if ingested.tzinfo is None:
                        ingested = ingested.replace(tzinfo=UTC)
                    if stamp.astimezone(UTC) + BAR_DELTA > ingested.astimezone(UTC):
                        continue
                opening = float(bar["o"])
                high = float(bar["h"])
                low = float(bar["l"])
                close = float(bar["c"])
                volume = float(bar.get("v", 0) or 0)
                if (
                    not math.isfinite(opening) or opening <= 0
                    or not math.isfinite(high) or high <= 0
                    or not math.isfinite(low) or low <= 0
                    or not math.isfinite(close) or close <= 0
                    or not math.isfinite(volume) or volume < 0
                    or high < max(opening, close, low)
                    or low > min(opening, close, high)
                ):
                    continue
                session = local.date()
                state = sessions.get(session)
                if state is None:
                    sessions[session] = {
                        "first_stamp": stamp, "open": opening,
                        "last_stamp": stamp, "close": close,
                        "high": high, "low": low, "volume": volume,
                    }
                else:
                    if high > state["high"]:
                        state["high"] = high
                    if low < state["low"]:
                        state["low"] = low
                    state["volume"] += volume
                    if stamp < state["first_stamp"]:
                        state["first_stamp"] = stamp
                        state["open"] = opening
                    if stamp >= state["last_stamp"]:
                        state["last_stamp"] = stamp
                        state["close"] = close
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if unordered:
            output[raw_symbol.upper()] = unordered_fallback(raw_symbol, bars)
            continue
        daily = []
        for session in sorted(sessions):
            state = sessions[session]
            daily.append({
                "date": session.isoformat(), "o": state["open"],
                "h": state["high"], "l": state["low"],
                "c": state["close"], "v": state["volume"],
            })
        output[raw_symbol.upper()] = daily
    return output
