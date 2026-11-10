"""The prepared-chain store reuses unchanged sessions and prepares only new or changed ones."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import options.research.dataset as dataset_module
from options.research.dataset import load_options_research_dataset
from options.research.history_store import OptionsHistoryStore


def _snapshot(day: date, *, bid: float = 4.9, minute: int = 0) -> dict:
    as_of = datetime(day.year, day.month, day.day, 20, minute, tzinfo=timezone.utc)
    expiration = (day + timedelta(days=30)).isoformat()
    contracts = [
        {
            "symbol": f"SPY{day:%y%m%d}C00{strike}000", "underlying": "SPY", "expiration_date": expiration,
            "type": "call", "strike_price": float(strike), "size": 100, "style": "american", "status": "active",
            "bid": bid, "ask": bid + 0.2, "quote_at": as_of.isoformat(), "iv": 0.2,
        }
        for strike in (480, 500, 520)
    ]
    return {
        "underlying": "SPY", "as_of": as_of.isoformat(), "spot": 500.0, "underlying_close": 500.0,
        "feed": "sip", "regime": "range_bound", "volatility_state": "normal", "contracts": contracts,
    }


def _store_with_days(path: Path, days: list[date], *, bid: float = 4.9) -> OptionsHistoryStore:
    store = OptionsHistoryStore(path)
    store.write_snapshots("SPY", "alpaca", [_snapshot(day, bid=bid) for day in days], replace_source=False)
    return store


def _load(path: Path, cache_dir: Path):
    return load_options_research_dataset(path, underlying="SPY", rate=0.04, dividend_yield=0.015, workers=1)


def test_only_new_or_changed_sessions_are_prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset_module, "CHAIN_CACHE_DIR", tmp_path / "chains")
    # Write the store synchronously, so the next load in this test can read it.
    monkeypatch.setattr(dataset_module, "_write_chain_store", dataset_module._write_chain_store_now)
    db = tmp_path / "history.sqlite3"
    days = [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)]
    store = _store_with_days(db, days)

    first = _load(db, tmp_path)
    assert first.loading_parallelism["chains_prepared"] == 3
    assert [item.chain is not None for item in first.observations] == [True, True, True]

    second = _load(db, tmp_path)
    assert second.loading_parallelism["chains_prepared"] == 0
    assert second.loading_parallelism["chains_reused"] == 3

    store.write_snapshots("SPY", "alpaca", [_snapshot(date(2026, 10, 29))], replace_source=False)
    third = _load(db, tmp_path)
    assert third.loading_parallelism["chains_prepared"] == 1
    assert third.loading_parallelism["chains_reused"] == 3
    assert len(third.observations) == 4

    store.write_snapshots("SPY", "alpaca", [_snapshot(date(2026, 10, 27), bid=5.4, minute=30)], replace_source=False)
    fourth = _load(db, tmp_path)
    assert fourth.loading_parallelism["chains_prepared"] == 1, "only the changed session is prepared again"
    changed = next(item for item in fourth.observations if item.as_of.date() == date(2026, 10, 27))
    assert min(quote.bid for quote in changed.chain.contracts) == 5.4


def test_the_store_keeps_one_file_per_underlying_and_drops_stale_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset_module, "CHAIN_CACHE_DIR", tmp_path / "chains")
    monkeypatch.setattr(dataset_module, "_write_chain_store", dataset_module._write_chain_store_now)
    db = tmp_path / "history.sqlite3"
    _store_with_days(db, [date(2026, 10, 26), date(2026, 10, 27)])
    _load(db, tmp_path)
    files = sorted(path.name for path in (tmp_path / "chains").glob("SPY-*.pkl"))
    assert len(files) == 1
