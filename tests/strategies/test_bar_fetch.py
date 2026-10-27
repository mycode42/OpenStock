"""Chunked bar fetching must keep every symbol and every page."""

from strategies.runner.data import BAR_CHUNK_SIZE, fetch_bars


def test_fetch_bars_merges_concurrent_chunks_and_follows_pagination():
    symbols = [f"T{index:03d}" for index in range(BAR_CHUNK_SIZE * 2 + 7)]
    calls = []

    def request_fn(endpoint, *, mode, params):
        calls.append(params["symbols"])
        requested = params["symbols"].split(",")
        if params.get("page_token") is None:
            return {
                "bars": {symbol: [{"t": "p1", "c": 1.0}] for symbol in requested},
                "next_page_token": "page-2",
            }
        return {"bars": {symbol: [{"t": "p2", "c": 2.0}] for symbol in requested}}

    output = fetch_bars(
        symbols, mode="algo", timeframe="1Day", start="2026-01-01", end="2026-01-02",
        limit=10, feed="sip", request_fn=request_fn,
    )

    assert set(output) == set(symbols)
    assert all([bar["t"] for bar in bars] == ["p1", "p2"] for bars in output.values())
    assert len(calls) == 6
