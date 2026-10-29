# cython: language_level=3, boundscheck=False, wraparound=False
"""Cython row-to-bar grouping for SQLiteBarStore.bars. Produces the same dicts as the Python loop."""


def group_bar_rows(object rows, dict output):
    cdef object row, bar, bucket
    cdef object symbol, timestamp, opening, high, low, close, volume, count, vwap, ingested_at
    for row in rows:
        symbol, timestamp, opening, high, low, close, volume, count, vwap, ingested_at = row
        bar = {"t": timestamp, "o": opening, "h": high, "l": low, "c": close, "v": volume}
        bar["_ingested_at"] = ingested_at
        if count is not None:
            bar["n"] = count
        if vwap is not None:
            bar["vw"] = vwap
        bucket = output.get(symbol)
        if bucket is None:
            bucket = []
            output[symbol] = bucket
        bucket.append(bar)
    return output
