"""The prepared-chain store reuses unchanged sessions and prepares only new or changed ones."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import options.research.dataset as dataset_module
from options.research.dataset import (
    OptionsResearchDataset, ResearchObservation,
    combine_options_research_datasets, load_options_research_dataset,
)
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
    monkeypatch.setattr(dataset_module, "CHAIN_PREPARATION_BATCH_SIZE", 2)
    # Write the store synchronously, so the next load in this test can read it.
    monkeypatch.setattr(dataset_module, "_write_chain_store", dataset_module._write_chain_store_now)
    db = tmp_path / "history.sqlite3"
    days = [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)]
    store = _store_with_days(db, days)

    first = _load(db, tmp_path)
    assert first.loading_parallelism["chains_prepared"] == 3
    assert first.loading_parallelism["chain_preparation_batches"] == 2
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


def test_replay_chain_batches_stay_disk_backed_and_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset_module, "CHAIN_CACHE_DIR", tmp_path / "chains")
    monkeypatch.setattr(dataset_module, "CHAIN_PREPARATION_BATCH_SIZE", 2)
    db = tmp_path / "history.sqlite3"
    _store_with_days(db, [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)])

    dataset = load_options_research_dataset(
        db, underlying="SPY", rate=0.04, dividend_yield=0.015,
        workers=2, lazy_chains=True,
    )

    assert dataset.loading_parallelism["chains_prepared"] == 3
    assert dataset.loading_parallelism["chain_workers_used"] == 2
    assert all(item.chain is None and item.chain_error is None for item in dataset.observations)
    chain = dataset.provider.get_chain_for_observation(dataset.observations[0])
    assert chain is not None and len(chain.contracts) == 3
    assert dataset.provider.get_chain_for_observation(dataset.observations[1]) is not None
    assert dataset.provider._loaded_results == []
    assert len(dataset.provider._loaded_offsets) == 2
    assert dataset.provider.get_chain_for_observation(dataset.observations[2]) is not None
    cached_batch = next((tmp_path / "chains" / "replay-batches").rglob("batch-000000.pkl"))
    import pickle
    with cached_batch.open("rb") as handle:
        header = pickle.load(handle)
        assert header["schema"] == "options_replay_chain_batch_v2"
        results = []
        for offset in header["offsets"]:
            handle.seek(offset)
            results.append(pickle.load(handle))
    with cached_batch.open("wb") as handle:
        pickle.dump({"schema": "options_replay_chain_batch_v1", "refs": header["refs"], "results": results}, handle)

    resumed = load_options_research_dataset(
        db, underlying="SPY", rate=0.04, dividend_yield=0.015,
        workers=2, lazy_chains=True,
    )
    assert resumed.loading_parallelism["chains_prepared"] == 0
    assert resumed.loading_parallelism["chains_reused"] == 3
    assert resumed.provider.get_chain_for_observation(resumed.observations[0]) is not None
    with cached_batch.open("rb") as handle:
        assert pickle.load(handle)["schema"] == "options_replay_chain_batch_v2"


def test_portfolio_dataset_merges_same_time_symbols_without_chain_key_collisions(tmp_path):
    as_of = datetime(2026, 10, 26, 20, tzinfo=timezone.utc)

    class Provider:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_chain_for_observation(self, observation):
            return self.symbol

        def underlying_closes(self, underlying, *, as_of, sessions):
            return [(as_of.date(), float(ord(self.symbol[0])))]

    datasets = {}
    for symbol in ("SPY", "AAPL"):
        observation = ResearchObservation(
            as_of=as_of, underlying=symbol, spot=100.0, regime="range_bound",
            volatility_state="normal", feed="historical", events=(), equity_signals={},
            event_recommendations={}, dividends={}, source_line=1,
        )
        datasets[symbol] = OptionsResearchDataset(
            observations=(observation,), provider=Provider(symbol),
            source_path=tmp_path / "history.sqlite3", source_sha256=symbol,
            data_mode="historical_chains", underlying_history={symbol: ()},
            loading_parallelism={"elapsed_seconds": 1, "chain_workers_used": 1},
        )

    portfolio = combine_options_research_datasets(datasets)

    assert [(item.underlying, item.as_of) for item in portfolio.observations] == [
        ("AAPL", as_of), ("SPY", as_of),
    ]
    assert [portfolio.provider.get_chain_for_observation(item) for item in portfolio.observations] == [
        "AAPL", "SPY",
    ]
    assert portfolio.provider.underlying_closes("SPY", as_of=as_of, sessions=1) == [
        (as_of.date(), float(ord("S"))),
    ]


def test_the_store_keeps_one_file_per_underlying_and_drops_stale_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset_module, "CHAIN_CACHE_DIR", tmp_path / "chains")
    monkeypatch.setattr(dataset_module, "_write_chain_store", dataset_module._write_chain_store_now)
    db = tmp_path / "history.sqlite3"
    _store_with_days(db, [date(2026, 10, 26), date(2026, 10, 27)])
    _load(db, tmp_path)
    files = sorted(path.name for path in (tmp_path / "chains").glob("SPY-*.pkl"))
    assert len(files) == 1


def test_replay_takes_theta_before_the_full_range_alpaca_cutoff(tmp_path):
    """Before 2026-11-11 Alpaca's capped chains lose to Theta's full EOD chains; from it on Alpaca wins."""
    store = OptionsHistoryStore(tmp_path / "history.sqlite3")
    before, after = date(2026, 11, 10), date(2026, 11, 11)
    store.write_snapshots("SPY", "theta", [_snapshot(before, bid=1.0), _snapshot(after, bid=1.0)], replace_source=False)
    store.write_snapshots("SPY", "alpaca", [_snapshot(before, bid=2.0), _snapshot(after, bid=2.0)], replace_source=False)

    bids = {}
    for snapshot in store.iter_effective_snapshots("SPY"):
        bids[str(snapshot["as_of"])[:10]] = snapshot["contracts"][0]["bid"]

    assert bids == {before.isoformat(): 1.0, after.isoformat(): 2.0}
