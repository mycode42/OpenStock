#!/usr/bin/env python3
"""Compare SQLite dictionary loading with DuckDB Arrow batches for equity and options.

The equity comparison includes daily OHLCV aggregation in both paths. The
options comparison measures effective snapshot/contract extraction and the
full dataset load, including conversion into OptionChain objects, for both
the current dictionary loader and an Arrow batch prototype.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime
import gc
import json
import math
import multiprocessing as mp
from pathlib import Path
import resource
import statistics
import time
import duckdb
import numpy as np
import pyarrow.compute as pc

from autoresearch.cpu import available_cpu_count
from market.store import DEFAULT_DB, SQLiteBarStore
from options.data import HistoricalChainSnapshotProvider
from options.research import dataset as dataset_module
from options.research.dataset import ResearchObservation, load_options_research_dataset
from options.research.history_store import DEFAULT_DATABASE, OptionsHistoryStore
from runtime.paths import ROOT
from strategies.multi_factor import features


EQUITY_FEED = "sip"
EQUITY_ADJUSTMENT = "split"
EQUITY_TIMEFRAME = "5Min"
_SYMBOL_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


def _symbol(raw: str, *, name: str) -> str:
    value = str(raw).strip().upper()
    if not value or any(char not in _SYMBOL_CHARS for char in value):
        raise ValueError(f"{name} must be a ticker symbol")
    return value


def _path(raw: str | Path) -> Path:
    value = Path(raw).expanduser()
    return value.resolve() if value.is_absolute() else (ROOT / value).resolve()


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _duckdb(database: Path, threads: int):
    connection = duckdb.connect(":memory:")
    connection.execute(f"SET threads={int(threads)}")
    connection.execute("INSTALL sqlite")
    connection.execute("LOAD sqlite")
    return connection


def _consume_arrow(relation, *, batch_size: int, consume) -> dict:
    started = time.perf_counter()
    reader = relation.to_arrow_reader(batch_size=batch_size)
    batches = rows = arrow_bytes = 0
    for batch in reader:
        batches += 1
        rows += batch.num_rows
        arrow_bytes += batch.nbytes
        consume(batch)
    return {
        "seconds": round(time.perf_counter() - started, 3),
        "rows": rows,
        "batches": batches,
        "arrow_bytes_streamed": arrow_bytes,
    }


def _equity_range(database: Path, symbol: str, start: str | None, end: str | None) -> tuple[str, str]:
    import sqlite3

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        bounds = connection.execute("""
            SELECT MIN(timestamp), MAX(timestamp)
            FROM market_bar
            WHERE feed=? AND adjustment=? AND timeframe=? AND symbol=?
        """, (EQUITY_FEED, EQUITY_ADJUSTMENT, EQUITY_TIMEFRAME, symbol)).fetchone()
    finally:
        connection.close()
    if not bounds or bounds[0] is None:
        raise ValueError(f"no split-adjusted SIP {EQUITY_TIMEFRAME} bars for {symbol} in {database}")
    first, last = str(bounds[0])[:10], str(bounds[1])[:10]
    resolved_start = start or first
    resolved_end = end or last
    datetime.fromisoformat(resolved_start)
    datetime.fromisoformat(resolved_end)
    if resolved_start > resolved_end:
        raise ValueError("equity start date must be on or before end date")
    return resolved_start, resolved_end


def _equity_sql(database: Path, symbol: str, start: str, end: str) -> str:
    db = _sql_string(database)
    ticker = _sql_string(symbol)
    start_bound = _sql_string(f"{start}T00:00:00.000000Z")
    end_bound = _sql_string(f"{end}T23:59:59.999999Z")
    return f"""
        WITH typed AS (
            SELECT symbol, open, high, low, close, volume,
                   try_cast(timestamp AS TIMESTAMPTZ) AS ts_utc,
                   try_cast(ingested_at AS TIMESTAMPTZ) AS ingested_utc
            FROM sqlite_scan({db}, 'market_bar')
            WHERE feed={_sql_string(EQUITY_FEED)}
              AND adjustment={_sql_string(EQUITY_ADJUSTMENT)}
              AND timeframe={_sql_string(EQUITY_TIMEFRAME)}
              AND symbol={ticker}
              AND timestamp >= {start_bound}
              AND timestamp <= {end_bound}
        ), valid AS (
            SELECT *, timezone('America/New_York', ts_utc) AS ts_et
            FROM typed
            WHERE ts_utc IS NOT NULL
              AND (ingested_utc IS NULL OR ts_utc + INTERVAL '5 minutes' <= ingested_utc)
              AND open > 0 AND high > 0 AND low > 0 AND close > 0
              AND volume >= 0
              AND high >= greatest(open, close, low)
              AND low <= least(open, close, high)
        ), regular AS (
            SELECT * FROM valid
            WHERE CAST(ts_et AS TIME) >= TIME '09:30:00'
              AND CAST(ts_et AS TIME) < TIME '16:00:00'
        )
        SELECT CAST(ts_et AS DATE) AS session_date,
               arg_min(open, ts_utc) AS open,
               max(high) AS high,
               min(low) AS low,
               arg_max(close, ts_utc) AS close,
               sum(volume) AS volume,
               count(*) AS source_bars
        FROM regular
        GROUP BY session_date
        ORDER BY session_date
    """


def _equity_signature(rows: list[dict]) -> list[dict]:
    return [
        {
            "date": str(row["date"]),
            "o": float(row["o"]), "h": float(row["h"]),
            "l": float(row["l"]), "c": float(row["c"]),
            "v": float(row["v"]),
        }
        for row in rows
    ]


def _compare_daily(left: list[dict], right: list[dict]) -> dict:
    if len(left) != len(right):
        return {"parity": False, "left_rows": len(left), "right_rows": len(right), "mismatch": "row_count"}
    for index, (a, b) in enumerate(zip(left, right)):
        if a["date"] != b["date"]:
            return {"parity": False, "mismatch": "date", "row": index, "left": a["date"], "right": b["date"]}
        for key in ("o", "h", "l", "c", "v"):
            if not math.isclose(a[key], b[key], rel_tol=1e-12, abs_tol=1e-6):
                return {"parity": False, "mismatch": key, "row": index, "date": a["date"], "left": a[key], "right": b[key]}
    return {"parity": True, "rows": len(left), "tolerance": {"rel": 1e-12, "abs": 1e-6}}


def _run_equity(args, database: Path) -> dict:
    symbol = _symbol(args.equity_symbol, name="equity symbol")
    start, end = _equity_range(database, symbol, args.start, args.end)
    store = SQLiteBarStore(database)
    connection = _duckdb(database, args.threads)
    query = _equity_sql(database, symbol, start, end)
    samples = []
    aggregate_engine = "cython" if features._aggregate_intraday_bars_cy is not None else "python"
    try:
        for repeat in range(args.repeats):
            paths = ("dictionary", "arrow") if repeat % 2 == 0 else ("arrow", "dictionary")
            current = {}
            dictionary_rows = None
            arrow_daily_rows = None
            for mode in paths:
                if mode == "dictionary":
                    read_started = time.perf_counter()
                    bars = store.bars(
                        [symbol], feed=EQUITY_FEED, timeframe=EQUITY_TIMEFRAME,
                        start=start, end=end, adjustment=EQUITY_ADJUSTMENT,
                    )
                    read_seconds = time.perf_counter() - read_started
                    aggregate_started = time.perf_counter()
                    daily = features.aggregate_intraday_bars(bars)
                    aggregate_seconds = time.perf_counter() - aggregate_started
                    rows = _equity_signature(daily.get(symbol, []))
                    dictionary_rows = rows
                    current[mode] = {
                        "seconds": round(read_seconds + aggregate_seconds, 3),
                        "read_seconds": round(read_seconds, 3),
                        "aggregation_seconds": round(aggregate_seconds, 3),
                        "intraday_rows": len(bars.get(symbol, [])),
                        "daily_rows": len(rows),
                        "aggregation_engine": aggregate_engine,
                    }
                    del bars, daily
                else:
                    arrow_rows: list[dict] = []

                    def consume(batch) -> None:
                        arrow_rows.extend(batch.to_pylist())

                    result = _consume_arrow(
                        connection.sql(query), batch_size=args.batch_size, consume=consume,
                    )
                    rows = [
                        {
                            "date": str(item["session_date"]),
                            "o": float(item["open"]), "h": float(item["high"]),
                            "l": float(item["low"]), "c": float(item["close"]),
                            "v": float(item["volume"]),
                        }
                        for item in arrow_rows
                    ]
                    arrow_daily_rows = rows
                    result.update({
                        "daily_rows": len(rows),
                        "valid_intraday_rows": sum(int(item["source_bars"]) for item in arrow_rows),
                    })
                    current[mode] = result
                    del arrow_rows
            current["parity"] = _compare_daily(dictionary_rows or [], arrow_daily_rows or [])
            samples.append(current)
    finally:
        connection.close()
    dict_times = [sample["dictionary"]["seconds"] for sample in samples]
    arrow_times = [sample["arrow"]["seconds"] for sample in samples]
    parity = all(sample["parity"].get("parity") for sample in samples)
    return {
        "symbol": symbol,
        "feed": EQUITY_FEED,
        "adjustment": EQUITY_ADJUSTMENT,
        "timeframe": EQUITY_TIMEFRAME,
        "start": start,
        "end": end,
        "repeats": args.repeats,
        "dictionary_median_seconds": round(statistics.median(dict_times), 3),
        "arrow_median_seconds": round(statistics.median(arrow_times), 3),
        "speedup_dictionary_over_arrow": round(statistics.median(dict_times) / statistics.median(arrow_times), 3),
        "parity": parity,
        "parity_details": samples[-1]["parity"],
        "samples": samples,
    }


def _options_sql(database: Path, underlying: str) -> str:
    db = _sql_string(database)
    ticker = _sql_string(underlying)
    return f"""
        WITH ranked AS (
            SELECT s.snapshot_id, s.underlying, s.session_date, s.as_of,
                   s.phase, s.feed, s.spot, s.underlying_close, s.regime,
                   s.volatility_state, s.events_json, s.equity_signals_json,
                   s.event_recommendations_json, s.dividends_json,
                   s.iv_surface_json, s.expirations_json, src.source_kind,
                   row_number() OVER (
                       PARTITION BY s.session_date, s.phase
                       ORDER BY CASE src.source_kind WHEN 'alpaca' THEN 2 WHEN 'theta' THEN 1 ELSE 0 END DESC,
                                s.as_of DESC
                   ) AS source_rank
            FROM sqlite_scan({db}, 'options_history_snapshot') AS s
            JOIN sqlite_scan({db}, 'options_history_active_source') AS a
              ON a.source_id=s.source_id
            JOIN sqlite_scan({db}, 'options_history_source') AS src
              ON src.source_id=s.source_id
            WHERE s.underlying={ticker} AND s.phase='daily_close'
        ), effective AS (
            SELECT * FROM ranked WHERE source_rank=1
        )
        SELECT e.snapshot_id, e.session_date, e.as_of, e.spot, e.underlying_close, e.feed,
               e.regime, e.volatility_state, e.events_json,
               e.equity_signals_json, e.event_recommendations_json,
               e.dividends_json, e.iv_surface_json, e.expirations_json,
               q.contract_symbol, q.underlying_symbol, q.expiration_date,
               q.right, q.strike, q.multiplier, q.exercise_style,
               q.tradable, q.open_interest, q.bid, q.ask, q.quote_at,
               q.iv, q.greeks_json
        FROM effective e
        LEFT JOIN sqlite_scan({db}, 'options_history_contract') AS q
          ON q.snapshot_id=e.snapshot_id
        ORDER BY e.session_date, e.as_of, e.snapshot_id,
                 q.expiration_date, q.right, q.strike
    """


def _run_options_dictionary(store: OptionsHistoryStore, underlying: str, *, threads: int) -> dict:
    snapshots = contracts = 0
    started = time.perf_counter()
    for row in store.iter_effective_snapshots(underlying, threads=threads):
        snapshots += 1
        contracts += len(row.get("contracts", ()))
    return {"seconds": round(time.perf_counter() - started, 3), "snapshots": snapshots, "contracts": contracts}


def _run_options_arrow(connection, query: str, *, batch_size: int) -> dict:
    snapshots = contracts = rows = arrow_bytes = batches = 0
    last_snapshot_id = None
    started = time.perf_counter()
    reader = connection.sql(query).to_arrow_reader(batch_size=batch_size)
    snapshot_index = 0
    contract_index = 14
    for batch in reader:
        batches += 1
        rows += batch.num_rows
        arrow_bytes += batch.nbytes
        ids = batch.column(snapshot_index).to_numpy(zero_copy_only=False)
        if len(ids):
            if last_snapshot_id is None or int(ids[0]) != last_snapshot_id:
                snapshots += 1
            if len(ids) > 1:
                snapshots += int(np.count_nonzero(ids[1:] != ids[:-1]))
            last_snapshot_id = int(ids[-1])
        contracts += int(pc.count(batch.column(contract_index)).as_py() or 0)
    return {
        "seconds": round(time.perf_counter() - started, 3),
        "snapshots": snapshots,
        "contracts": contracts,
        "result_rows": rows,
        "batches": batches,
        "arrow_bytes_streamed": arrow_bytes,
    }


def _run_options_arrow_full(database: Path, underlying: str, *, threads: int, batch_size: int) -> dict:
    """Build the current option objects directly from streamed Arrow batches."""
    read_started = time.perf_counter()
    store = OptionsHistoryStore(database, initialize=False)
    store.import_symbol(underlying)
    store.dataset_fingerprint(underlying)
    connection = _duckdb(database, threads)
    query = _options_sql(database, underlying)
    observations = []
    normalized_snapshots = []
    closes = []

    def finish_snapshot(current: dict | None) -> None:
        if current is None:
            return
        stamp = current["stamp"]
        normalized_snapshots.append({
            "underlying": underlying,
            "spot": current["spot"],
            "as_of": stamp.isoformat(),
            "feed": current["feed"],
            "contracts": current["contracts"],
        })
        if current["close"] is not None:
            closes.append((stamp.date(), float(current["close"])))
        source_line = len(observations) + 1
        observations.append(ResearchObservation(
            as_of=stamp,
            underlying=underlying,
            spot=current["spot"],
            regime=current["regime"],
            volatility_state=current["volatility_state"],
            feed=current["feed"],
            events=dataset_module._events(current["events"], as_of=stamp),
            equity_signals=dataset_module._signals(current["equity_signals"], as_of=stamp),
            event_recommendations=dataset_module._signals(
                current["event_recommendations"], as_of=stamp,
            ),
            dividends={str(key).upper(): float(value) for key, value in current["dividends"].items()},
            source_line=source_line,
        ))

    try:
        reader = connection.sql(query).to_arrow_reader(batch_size=batch_size)
        current_id = None
        current = None
        for batch in reader:
            field_index = {name: batch.schema.get_field_index(name) for name in batch.schema.names}
            ids = batch.column(field_index["snapshot_id"]).to_numpy(zero_copy_only=False)
            contract_fields = (
                "contract_symbol", "underlying_symbol", "expiration_date", "right",
                "strike", "multiplier", "exercise_style", "tradable", "open_interest",
                "bid", "ask", "quote_at", "iv", "greeks_json",
            )
            contract_columns = {
                name: batch.column(field_index[name]).to_pylist()
                for name in contract_fields
            }

            def snapshot_value(name: str, row_index: int):
                return batch.column(field_index[name])[row_index].as_py()

            for index, raw_snapshot_id in enumerate(ids):
                snapshot_id = int(raw_snapshot_id)
                if snapshot_id != current_id:
                    finish_snapshot(current)
                    current_id = snapshot_id
                    raw_stamp = snapshot_value("as_of", index)
                    source_line = len(observations) + 1
                    stamp = dataset_module._timestamp(
                        raw_stamp if isinstance(raw_stamp, str) else raw_stamp.isoformat(),
                        field=f"snapshot[{source_line}].as_of",
                    )
                    spot = float(snapshot_value("spot", index))
                    if spot <= 0 or not math.isfinite(spot):
                        raise ValueError(f"database snapshot {source_line} needs a positive finite spot")
                    current = {
                        "stamp": stamp,
                        "spot": spot,
                        "close": snapshot_value("underlying_close", index),
                        "feed": str(snapshot_value("feed", index) or "historical"),
                        "regime": str(snapshot_value("regime", index) or "unknown").lower(),
                        "volatility_state": str(snapshot_value("volatility_state", index) or "unknown").lower(),
                        "events": _decode_json(snapshot_value("events_json", index), []),
                        "equity_signals": _decode_json(snapshot_value("equity_signals_json", index), []),
                        "event_recommendations": _decode_json(
                            snapshot_value("event_recommendations_json", index), [],
                        ),
                        "dividends": _decode_json(snapshot_value("dividends_json", index), {}),
                        "contracts": [],
                    }
                contract_symbol = contract_columns["contract_symbol"][index]
                if current is not None and contract_symbol is not None:
                    greeks_json = contract_columns["greeks_json"][index]
                    iv = contract_columns["iv"][index]
                    tradable = contract_columns["tradable"][index]
                    current["contracts"].append({
                        "symbol": contract_symbol,
                        "underlying_symbol": contract_columns["underlying_symbol"][index],
                        "expiration_date": contract_columns["expiration_date"][index],
                        "type": contract_columns["right"][index],
                        "strike_price": float(contract_columns["strike"][index]),
                        "size": int(contract_columns["multiplier"][index]),
                        "style": contract_columns["exercise_style"][index],
                        "tradable": bool(tradable),
                        "status": "active" if tradable else "inactive",
                        "open_interest": contract_columns["open_interest"][index],
                        "bid": float(contract_columns["bid"][index]),
                        "ask": float(contract_columns["ask"][index]),
                        "quote_at": contract_columns["quote_at"][index],
                        "iv": iv,
                        "greeks": (
                            json.loads(greeks_json)
                            if iv is None and greeks_json not in (None, "", "{}", "null")
                            else None
                        ),
                    })
        finish_snapshot(current)
    finally:
        connection.close()
    read_seconds = time.perf_counter() - read_started
    if len(observations) < 2:
        raise ValueError(f"dataset has fewer than two {underlying} research observations")
    ordered_times = [item.as_of for item in observations]
    if ordered_times != sorted(ordered_times) or len(set(ordered_times)) != len(ordered_times):
        raise ValueError("Arrow snapshot ordering or timestamp uniqueness differs from loader requirements")
    history = sorted(set(closes))
    if not history:
        history = [(item.as_of.date(), item.spot) for item in observations]
    provider = HistoricalChainSnapshotProvider(normalized_snapshots, {underlying: history})
    workers = min(threads, available_cpu_count(), len(observations))
    chain_started = time.perf_counter()
    prepared_observations = []
    dataset_module._CHAIN_PREPARATION_CONTEXT.update({
        "observations": observations,
        "provider": provider,
        "underlying": underlying,
    })
    try:
        if workers > 1 and "fork" in mp.get_all_start_methods():
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=mp.get_context("fork"),
            ) as executor:
                results = executor.map(
                    dataset_module._prepare_chain_index,
                    range(len(observations)),
                    chunksize=1,
                )
                for observation, (chain, error) in zip(observations, results):
                    prepared_observations.append(replace(
                        observation, chain=chain, chain_error=error,
                    ))
        else:
            for observation in observations:
                try:
                    chain = provider.get_chain(
                        underlying, as_of=observation.as_of, min_dte=0, max_dte=730,
                    )
                    error = None
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    chain, error = None, f"{type(exc).__name__}: {exc}"
                prepared_observations.append(replace(
                    observation, chain=chain, chain_error=error,
                ))
    finally:
        dataset_module._CHAIN_PREPARATION_CONTEXT.clear()
    chain_seconds = time.perf_counter() - chain_started
    prepared_contracts = sum(
        len(item.chain.contracts) for item in prepared_observations if item.chain
    )
    chain_errors = sum(item.chain is None for item in prepared_observations)
    signature = [
        (
            item.as_of.isoformat(),
            len(item.chain.contracts) if item.chain else None,
            item.chain_error,
        )
        for item in prepared_observations
    ]
    return {
        "seconds": round(time.perf_counter() - read_started, 3),
        "arrow_read_and_object_conversion_seconds": round(read_seconds, 3),
        "chain_preparation_seconds": round(chain_seconds, 3),
        "observations": len(observations),
        "prepared_contracts": prepared_contracts,
        "chain_errors": chain_errors,
        "workers": workers,
        "signature": signature,
    }


def _decode_json(value, default):
    return default if value is None else json.loads(value)


def _run_options(args, database: Path) -> dict:
    underlying = _symbol(args.option_underlying, name="options underlying")
    store = OptionsHistoryStore(database, initialize=False)
    expected = store.coverage_summary(underlying)
    connection = _duckdb(database, args.threads)
    query = _options_sql(database, underlying)
    samples = []
    try:
        for repeat in range(args.repeats):
            modes = ("dictionary", "arrow") if repeat % 2 == 0 else ("arrow", "dictionary")
            current = {}
            for mode in modes:
                current[mode] = (
                    _run_options_dictionary(store, underlying, threads=args.threads)
                    if mode == "dictionary"
                    else _run_options_arrow(connection, query, batch_size=args.batch_size)
                )
            current["parity"] = {
                "snapshots": current["dictionary"]["snapshots"] == current["arrow"]["snapshots"],
                "contracts": current["dictionary"]["contracts"] == current["arrow"]["contracts"],
                "coverage_snapshots": expected["combined_sessions"],
            }
            samples.append(current)
    finally:
        connection.close()
    dict_times = [sample["dictionary"]["seconds"] for sample in samples]
    arrow_times = [sample["arrow"]["seconds"] for sample in samples]
    parity = all(all(sample["parity"][key] for key in ("snapshots", "contracts")) for sample in samples)

    full_samples = []
    dictionary_full_samples = []
    arrow_full_samples = []
    for repeat in range(args.full_repeats):
        modes = ("dictionary", "arrow") if repeat % 2 == 0 else ("arrow", "dictionary")
        current = {}
        signatures = {}
        for mode in modes:
            if mode == "dictionary":
                load_started = time.perf_counter()
                dataset = load_options_research_dataset(
                    database, underlying=underlying, workers=args.threads,
                )
                current[mode] = {
                    "seconds": round(time.perf_counter() - load_started, 3),
                    "observations": len(dataset.observations),
                    "prepared_contracts": sum(
                        len(item.chain.contracts)
                        for item in dataset.observations if item.chain
                    ),
                    "chain_errors": sum(item.chain is None for item in dataset.observations),
                    "parallelism": dataset.loading_parallelism,
                }
                signatures[mode] = [
                    (
                        item.as_of.isoformat(),
                        len(item.chain.contracts) if item.chain else None,
                        item.chain_error,
                    )
                    for item in dataset.observations
                ]
                del dataset
            else:
                result = _run_options_arrow_full(
                    database, underlying, threads=args.threads,
                    batch_size=args.batch_size,
                )
                signatures[mode] = result.pop("signature")
                current[mode] = result
            gc.collect()
        current["parity"] = (
            current["dictionary"]["observations"] == current["arrow"]["observations"]
            and current["dictionary"]["prepared_contracts"] == current["arrow"]["prepared_contracts"]
            and current["dictionary"]["chain_errors"] == current["arrow"]["chain_errors"]
            and signatures["dictionary"] == signatures["arrow"]
        )
        dictionary_full_samples.append(current["dictionary"])
        arrow_full_samples.append(current["arrow"])
        full_samples.append(current)

    dictionary_full_times = [item["seconds"] for item in dictionary_full_samples]
    arrow_full_times = [item["seconds"] for item in arrow_full_samples]
    full_load = {
        "seconds": round(statistics.median(dictionary_full_times), 3),
        "observations": dictionary_full_samples[-1]["observations"],
        "prepared_contracts": dictionary_full_samples[-1]["prepared_contracts"],
        "chain_errors": dictionary_full_samples[-1]["chain_errors"],
        "parallelism": dictionary_full_samples[-1]["parallelism"],
        "samples": dictionary_full_samples,
    }
    arrow_full = {
        "seconds": round(statistics.median(arrow_full_times), 3),
        "arrow_read_and_object_conversion_seconds": round(statistics.median(
            [item["arrow_read_and_object_conversion_seconds"] for item in arrow_full_samples]
        ), 3),
        "chain_preparation_seconds": round(statistics.median(
            [item["chain_preparation_seconds"] for item in arrow_full_samples]
        ), 3),
        "observations": arrow_full_samples[-1]["observations"],
        "prepared_contracts": arrow_full_samples[-1]["prepared_contracts"],
        "chain_errors": arrow_full_samples[-1]["chain_errors"],
        "workers": arrow_full_samples[-1]["workers"],
        "parity_with_dictionary_pipeline": all(item["parity"] for item in full_samples),
        "samples": arrow_full_samples,
    }
    full_parity = arrow_full["parity_with_dictionary_pipeline"]
    return {
        "underlying": underlying,
        "coverage": expected,
        "repeats": args.repeats,
        "dictionary_median_seconds": round(statistics.median(dict_times), 3),
        "arrow_median_seconds": round(statistics.median(arrow_times), 3),
        "speedup_dictionary_over_arrow": round(statistics.median(dict_times) / statistics.median(arrow_times), 3),
        "parity": parity,
        "parity_details": samples[-1]["parity"],
        "samples": samples,
        "full_pipeline_repeats": args.full_repeats,
        "full_pipeline_samples": full_samples,
        "current_full_dataset_load": full_load,
        "arrow_full_dataset_load": arrow_full,
        "full_pipeline_speedup_dictionary_over_arrow": round(
            statistics.median(dictionary_full_times) / statistics.median(arrow_full_times), 3,
        ),
        "full_pipeline_parity": full_parity,
        "arrow_end_to_end_pipeline_benchmarked": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--equity-db", default=str(DEFAULT_DB))
    parser.add_argument("--options-db", default=str(DEFAULT_DATABASE))
    parser.add_argument("--equity-symbol", default="SPY")
    parser.add_argument("--option-underlying", default="SPY")
    parser.add_argument("--start", help="equity start date; defaults to stored coverage")
    parser.add_argument("--end", help="equity end date; defaults to stored coverage")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--full-repeats", type=int, default=3)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=65536)
    parser.add_argument("--output", help="JSON result path; defaults to a timestamped /tmp file")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if min(args.repeats, args.full_repeats, args.threads, args.batch_size) < 1:
        raise ValueError("repeats, full-repeats, threads, and batch-size must be positive")
    equity_db, options_db = _path(args.equity_db), _path(args.options_db)
    for database in (equity_db, options_db):
        if not database.is_file():
            raise FileNotFoundError(f"benchmark database not found: {database}")
    started_at = datetime.now().astimezone().isoformat()
    started = time.perf_counter()
    report = {
        "schema": "sqlite_duckdb_arrow_batch_benchmark_v3",
        "started_at": started_at,
        "settings": {
            "equity_database": str(equity_db),
            "options_database": str(options_db),
            "threads": args.threads,
            "batch_size": args.batch_size,
            "repeats": args.repeats,
            "full_repeats": args.full_repeats,
            "live_settings_changed": False,
        },
        "equity": _run_equity(args, equity_db),
        "options": _run_options(args, options_db),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
    }
    report["completed_at"] = datetime.now().astimezone().isoformat()
    output = _path(args.output) if args.output else Path("/tmp") / f"arrow-storage-benchmark-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output.resolve()),
        "elapsed_seconds": report["elapsed_seconds"],
        "equity": {
            "dictionary_median_seconds": report["equity"]["dictionary_median_seconds"],
            "arrow_median_seconds": report["equity"]["arrow_median_seconds"],
            "parity": report["equity"]["parity"],
        },
        "options": {
            "dictionary_median_seconds": report["options"]["dictionary_median_seconds"],
            "arrow_median_seconds": report["options"]["arrow_median_seconds"],
            "parity": report["options"]["parity"],
            "current_full_dataset_load_seconds": report["options"]["current_full_dataset_load"]["seconds"],
            "arrow_full_dataset_load_seconds": report["options"]["arrow_full_dataset_load"]["seconds"],
            "arrow_full_dataset_parity": report["options"]["arrow_full_dataset_load"]["parity_with_dictionary_pipeline"],
            "full_pipeline_speedup_dictionary_over_arrow": report["options"]["full_pipeline_speedup_dictionary_over_arrow"],
            "full_pipeline_repeats": report["options"]["full_pipeline_repeats"],
            "arrow_end_to_end_pipeline_benchmarked": report["options"]["arrow_end_to_end_pipeline_benchmarked"],
        },
    }, sort_keys=True))
    return 0 if (
        report["equity"]["parity"]
        and report["options"]["parity"]
        and report["options"]["arrow_full_dataset_load"]["parity_with_dictionary_pipeline"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
