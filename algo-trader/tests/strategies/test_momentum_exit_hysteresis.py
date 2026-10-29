"""Momentum holdings exit by rank band; a failed spread gate only blocks entries."""

from __future__ import annotations

from datetime import date, timedelta

from strategies.momentum.swing import generate_signals


ENTRY_DAY = "2026-09-22"
AS_OF = date(2026, 10, 1)


def _closes(growth: float, *, count: int = 60) -> list[dict]:
    sessions = []
    day = AS_OF
    while len(sessions) < count:
        if day.weekday() < 5:
            sessions.append(day)
        day -= timedelta(days=1)
    close = 100.0
    rows = []
    for session in reversed(sessions):
        close *= 1 + growth
        rows.append({"date": session.isoformat(), "c": close, "o": close, "h": close, "l": close, "v": 2_000_000})
    return rows


def _held(bars: list[dict], ticker: str) -> dict:
    entry = next(row["c"] for row in bars if row["date"] == ENTRY_DAY)
    return {"qty": 10, "entry_at": ENTRY_DAY, "entry_price": entry}


def _run(growths: dict[str, float], held: list[str]):
    bars = {ticker: _closes(growth) for ticker, growth in growths.items()}
    held_positions = {ticker: _held(bars[ticker], ticker) for ticker in held}
    signals, diagnostics = generate_signals(
        bars, symbols=list(bars), as_of=AS_OF, regime="range_bound",
        held_positions=held_positions,
    )
    return {signal.ticker: signal for signal in signals.values()}, diagnostics


def test_holding_inside_twice_top_n_is_kept_and_one_outside_is_sold():
    # Nine names with a wide spread: top_n=3, so entries are the top and bottom
    # three while holdings survive until they rank outside the top six.
    growths = {f"T{i}": 0.02 - i * 0.005 for i in range(9)}
    by_symbol, diagnostics = _run(growths, held=["T5", "T6"])

    assert diagnostics["spread_passed"] is True
    assert "T5" not in by_symbol
    assert by_symbol["T6"].action == "sell"
    assert "left the daily cross-sectional momentum selection" in by_symbol["T6"].reason
    assert {"T0", "T1", "T2"} <= set(diagnostics["longs"])


def test_failed_spread_gate_blocks_entries_without_exiting_the_whole_book():
    # Nearly identical trends: the spread gate fails, so nothing new is bought.
    # T8 ranks first and T0 last, so the top six are T8..T3.
    growths = {f"T{i}": 0.001 + i * 0.00001 for i in range(9)}
    by_symbol, diagnostics = _run(growths, held=["T3", "T5", "T1"])

    assert diagnostics["spread_passed"] is False
    assert not any(signal.action in {"buy", "short"} for signal in by_symbol.values())
    assert "T3" not in by_symbol
    assert "T5" not in by_symbol
    assert by_symbol["T1"].action == "sell"


def test_short_eligible_list_blocks_entries_and_keeps_holdings_in_the_band():
    # Four eligible names is below the seven-name minimum; all four sit inside the band.
    growths = {"T0": 0.02, "T1": 0.01, "T2": -0.003, "T3": -0.006}
    by_symbol, diagnostics = _run(growths, held=["T2", "T3"])

    assert diagnostics["eligible_symbols"] == 4
    assert diagnostics["spread_passed"] is False
    assert by_symbol == {}
