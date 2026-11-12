"""Point-in-time options research data loading and validation."""

from __future__ import annotations

import copy
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import multiprocessing as mp
import os
import pickle
from pathlib import Path
import tempfile
from time import perf_counter
from types import SimpleNamespace
from typing import Any

from equity_research.cpu import available_cpu_count
from runtime.paths import ROOT
from options.data import HistoricalChainSnapshotProvider, ModelSurfaceDataProvider, OptionChain, OptionDataProvider
from strategies.llm_driven.models import ScheduledEvent


_CHAIN_PREPARATION_CONTEXT: dict[str, Any] = {}


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class ResearchObservation:
    as_of: datetime
    underlying: str
    spot: float
    regime: str
    volatility_state: str
    feed: str
    events: tuple[ScheduledEvent, ...]
    equity_signals: dict[str, object]
    event_recommendations: dict[str, object]
    dividends: dict[str, float]
    source_line: int
    chain: OptionChain | None = None
    chain_error: str | None = None


@dataclass(frozen=True)
class OptionsResearchDataset:
    observations: tuple[ResearchObservation, ...]
    provider: OptionDataProvider
    source_path: Path
    source_sha256: str
    data_mode: str
    underlying_history: dict[str, tuple[tuple[date, float], ...]]
    loading_parallelism: dict[str, Any]


class ReplayChainBatchProvider:
    """Load one prepared-chain batch at a time for chronological replay."""

    lazy_chains = True

    def __init__(self, locations: dict[datetime, tuple[Path, int]], history: dict[str, list[tuple[date, float]]]):
        self._locations = locations
        self._history = {key.upper(): tuple(value) for key, value in history.items()}
        self._loaded_path: Path | None = None
        self._loaded_results: list[tuple[OptionChain | None, str | None]] = []

    def get_chain_for_observation(self, observation: ResearchObservation) -> OptionChain | None:
        location = self._locations.get(observation.as_of)
        if location is None:
            return None
        path, index = location
        if path != self._loaded_path:
            self._loaded_results = []
            self._loaded_path = None
            with path.open("rb") as handle:
                payload = pickle.load(handle)
            self._loaded_results = payload["results"]
            self._loaded_path = path
        chain, _error = self._loaded_results[index]
        return chain

    def underlying_closes(self, underlying: str, *, as_of: datetime, sessions: int) -> list[tuple[date, float]]:
        return [row for row in self._history.get(underlying.upper(), ()) if row[0] < as_of.date()][-sessions:]


def _load_rows(path: Path) -> tuple[list[dict], dict[str, list[tuple[date, float]]], dict]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        metadata = {}
    else:
        payload = json.loads(raw)
        if isinstance(payload, list):
            rows, metadata = payload, {}
        elif isinstance(payload, dict):
            rows = payload.get("snapshots", payload.get("observations", []))
            metadata = payload
        else:
            raise ValueError("dataset must be JSON, JSONL, or a JSON array")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("dataset must contain at least one object snapshot")
    history: dict[str, list[tuple[date, float]]] = {}
    for symbol, values in (metadata.get("underlying_history") or {}).items():
        if not isinstance(values, list):
            continue
        history[str(symbol).upper()] = [
            (date.fromisoformat(str(item[0])[:10]), float(item[1]))
            for item in values if isinstance(item, (list, tuple)) and len(item) >= 2
        ]
    return rows, history, metadata


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _jsonl_chunk_bounds(path: Path, workers: int) -> list[tuple[int, int]]:
    size = path.stat().st_size
    if size <= 0:
        return []
    boundaries = [0]
    with path.open("rb") as stream:
        for index in range(1, workers):
            target = size * index // workers
            stream.seek(max(0, target - 1))
            previous = stream.read(1)
            if previous != b"\n":
                stream.readline()
            boundary = stream.tell()
            if boundary > boundaries[-1] and boundary < size:
                boundaries.append(boundary)
    boundaries.append(size)
    return list(zip(boundaries, boundaries[1:]))


def _normalize_jsonl_chunk(args: tuple[str, int, int, str, str]) -> tuple[str, int, int, bool]:
    """Parse and normalize one byte range without retaining the range in memory."""
    source_name, start, end, output_name, target = args
    rows_seen = 0
    target_rows = 0
    model_surface_marker = False
    with Path(source_name).open("rb") as source, Path(output_name).open("wb") as output:
        source.seek(start)
        while source.tell() < end:
            raw_line = source.readline()
            if not raw_line:
                break
            if not raw_line.strip():
                continue
            rows_seen += 1
            row = json.loads(raw_line)
            if not isinstance(row, dict):
                raise ValueError("dataset must contain only object snapshots")
            if "iv_surface" in row and not row.get("contracts"):
                model_surface_marker = True
            symbol = str(row.get("underlying", "")).upper()
            if symbol != target:
                continue
            local_line = rows_seen
            as_of = _timestamp(row.get("as_of"), field=f"snapshot[{local_line}].as_of")
            spot = float(row.get("spot", 0))
            if spot <= 0 or not math.isfinite(spot):
                raise ValueError(f"snapshot line {local_line} needs a positive finite spot")
            contracts = row.get("contracts", [])
            normalized_contracts = None
            contracts_is_list = isinstance(contracts, list)
            if contracts_is_list:
                normalized_contracts = [
                    _normalize_contract(contract, as_of=as_of, underlying=target)
                    for contract in contracts
                ]
            close = row.get("underlying_close", row.get("close"))
            output_row = {
                "local_line": local_line,
                "as_of": as_of.isoformat(),
                "spot": spot,
                "regime": str(row.get("regime", "unknown")).lower(),
                "volatility_state": str(row.get("volatility_state", "unknown")).lower(),
                "feed": str(row.get("feed", "historical")),
                "contracts_is_list": contracts_is_list,
                "contracts": normalized_contracts,
                "events": row.get("events", []),
                "equity_signals": row.get("equity_signals", []),
                "event_recommendations": row.get("event_recommendations", []),
                "dividends": row.get("dividends") or {},
                "close": close,
                "raw_iv_surface": row.get("iv_surface"),
                "raw_expirations": row.get("expirations"),
            }
            pickle.dump(output_row, output, protocol=pickle.HIGHEST_PROTOCOL)
            target_rows += 1
    return output_name, rows_seen, target_rows, model_surface_marker


def _with_chain(observation, chain, chain_error):
    """Equivalent to dataclasses.replace for these two fields, without per-call field introspection."""
    updated = copy.copy(observation)
    object.__setattr__(updated, "chain", chain)
    object.__setattr__(updated, "chain_error", chain_error)
    return updated


def _prepare_chain_index(index: int) -> tuple[OptionChain | None, str | None]:
    observation = _CHAIN_PREPARATION_CONTEXT["observations"][index]
    provider = _CHAIN_PREPARATION_CONTEXT["provider"]
    target = _CHAIN_PREPARATION_CONTEXT["underlying"]
    try:
        return provider.get_chain(target, as_of=observation.as_of, min_dte=0, max_dte=730), None
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _load_jsonl_parallel(
    source: Path,
    *,
    target: str,
    workers: int,
    requested_mode: str,
) -> tuple[list[ResearchObservation], list[dict], dict[str, list[tuple[date, float]]], list[dict], bool, int, float]:
    """Load large JSONL chain archives in bounded, ordered process chunks."""
    started = perf_counter()
    chunks = _jsonl_chunk_bounds(source, workers)
    if not chunks:
        raise ValueError("dataset must contain at least one object snapshot")
    actual_workers = min(workers, len(chunks))
    temp_dir = tempfile.TemporaryDirectory(prefix="options-research-load-")
    try:
        outputs = [str(Path(temp_dir.name) / f"chunk-{index:03d}.pickle") for index in range(len(chunks))]
        tasks = [
            (str(source), start, end, outputs[index], target)
            for index, (start, end) in enumerate(chunks)
        ]
        if actual_workers > 1:
            methods = mp.get_all_start_methods()
            context = mp.get_context("fork" if "fork" in methods else "spawn")
            with ProcessPoolExecutor(max_workers=actual_workers, mp_context=context) as executor:
                chunk_stats = list(executor.map(_normalize_jsonl_chunk, tasks, chunksize=1))
        else:
            chunk_stats = [_normalize_jsonl_chunk(task) for task in tasks]
        use_model_surface = requested_mode in {"model", "model_surface"} or (
            requested_mode == "auto" and any(item[3] for item in chunk_stats)
        )

        observations: list[ResearchObservation] = []
        normalized_snapshots: list[dict] = []
        model_rows: list[dict] = []
        history: dict[str, list[tuple[date, float]]] = {}
        line_offset = 0
        for output_name, rows_seen, _, _ in chunk_stats:
            with Path(output_name).open("rb") as stream:
                while True:
                    try:
                        row = pickle.load(stream)
                    except EOFError:
                        break
                    source_line = line_offset + int(row["local_line"])
                    as_of = datetime.fromisoformat(row["as_of"])
                    spot = float(row["spot"])
                    if use_model_surface:
                        model_rows.append({
                            "as_of": as_of,
                            "spot": spot,
                            "raw": {
                                "iv_surface": row.get("raw_iv_surface"),
                                "expirations": row.get("raw_expirations"),
                            },
                        })
                    else:
                        if not row["contracts_is_list"]:
                            raise ValueError(f"historical snapshot line {source_line} needs a contracts array")
                        normalized_snapshots.append({
                            "underlying": target,
                            "spot": spot,
                            "as_of": as_of.isoformat(),
                            "feed": row["feed"],
                            "contracts": row["contracts"],
                        })
                    if row.get("close") is not None:
                        history.setdefault(target, []).append((as_of.date(), float(row["close"])))
                    observations.append(ResearchObservation(
                        as_of=as_of,
                        underlying=target,
                        spot=spot,
                        regime=row["regime"],
                        volatility_state=row["volatility_state"],
                        feed=("model" if use_model_surface else row["feed"]),
                        events=_events(row.get("events", []), as_of=as_of),
                        equity_signals=_signals(row.get("equity_signals", []), as_of=as_of),
                        event_recommendations=_signals(row.get("event_recommendations", []), as_of=as_of),
                        dividends={str(k).upper(): float(v) for k, v in row.get("dividends", {}).items()},
                        source_line=source_line,
                    ))
            line_offset += rows_seen
        return (
            observations, normalized_snapshots, history, model_rows,
            use_model_surface, actual_workers, perf_counter() - started,
        )
    finally:
        temp_dir.cleanup()


def _normalize_contract(row: dict, *, as_of: datetime, underlying: str) -> dict:
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        raise ValueError("each option contract needs its original symbol")
    expiration = row.get("expiration_date", row.get("expiration"))
    right = str(row.get("right", row.get("type", ""))).lower()
    strike = float(row.get("strike", row.get("strike_price")))
    bid, ask = float(row.get("bid", 0)), float(row.get("ask", 0))
    stamp = row.get("quote_at", row.get("observed_at", as_of.isoformat()))
    # The provider consumes Alpaca-shaped rows while preserving the original
    # historical bid, ask, IV, open interest, and exercise style.
    normalized = {
        "symbol": symbol,
        "underlying_symbol": str(row.get("underlying", row.get("underlying_symbol", underlying))).upper(),
        "expiration_date": str(expiration)[:10],
        "type": right,
        "strike_price": strike,
        "size": int(row.get("multiplier", row.get("size", 100))),
        "style": str(row.get("exercise_style", row.get("style", "american"))).lower(),
        "status": "active" if row.get("tradable", True) else "inactive",
        "tradable": bool(row.get("tradable", True)),
        "open_interest": row.get("open_interest"),
        "bid": bid,
        "ask": ask,
        "quote_at": _timestamp(stamp, field="contract.quote_at").isoformat(),
        "iv": row.get("iv", row.get("implied_volatility")),
        "greeks": row.get("greeks", {}),
    }
    if not normalized["underlying_symbol"]:
        raise ValueError(f"contract {symbol} has no underlying")
    if not normalized["expiration_date"] or right not in {"call", "put"}:
        raise ValueError(f"contract {symbol} has invalid expiration or right")
    return normalized


def _signals(raw: Any, *, as_of: datetime) -> dict[str, object]:
    output = {}
    rows = raw.items() if isinstance(raw, dict) else (
        (str(item.get("ticker", item.get("symbol", ""))), item)
        for item in (raw or []) if isinstance(item, dict)
    )
    for ticker, item in rows:
        if not isinstance(item, dict):
            continue
        known = item.get("known_at", item.get("created_at"))
        if known and _timestamp(known, field="signal.known_at") > as_of:
            continue
        # Preserve only point-in-time briefing fields used by the options
        # expression; no LLM or equity model is run during research.
        output[str(ticker).upper()] = SimpleNamespace(
            action=item.get("action", item.get("side", "")),
            confidence=item.get("confidence", item.get("strength", 0.0)),
            strategy=item.get("strategy"),
            run_id=item.get("run_id", item.get("signal_id", item.get("event_id"))),
            reason=item.get("reason", item.get("reasoning", "")),
            generated_at=item.get("generated_at", known),
        )
    return output


def _events(raw: Any, *, as_of: datetime) -> tuple[ScheduledEvent, ...]:
    output = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        try:
            known_at = _timestamp(item["known_at"], field="event.known_at")
            if known_at > as_of:
                continue
            output.append(ScheduledEvent(
                event_id=str(item["event_id"]),
                event_type=str(item["event_type"]),
                ticker=str(item["ticker"]),
                scheduled_at=_timestamp(item["scheduled_at"], field="event.scheduled_at"),
                known_at=known_at,
                reaction_start_at=_timestamp(item["reaction_start_at"], field="event.reaction_start_at"),
                reaction_end_at=_timestamp(item["reaction_end_at"], field="event.reaction_end_at"),
                source=str(item.get("source", "research-dataset")),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(output)


CHAIN_STORE_VERSION = "options-chain-store-v2"
CHAIN_CACHE_DIR = ROOT / ".cache" / "options-chains"
CHAIN_PREPARATION_BATCH_SIZE = 16


def _replay_batch_cache_dir(source: Path, target: str, rate: float, dividend_yield: float, requested_mode: str) -> Path:
    key = json.dumps({
        "version": "options-replay-chain-batches-v1",
        "source": str(source.resolve()), "underlying": target,
        "rate": rate, "dividend_yield": dividend_yield, "data_mode": requested_mode,
    }, sort_keys=True, default=str)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return CHAIN_CACHE_DIR / "replay-batches" / f"{target}-{digest}"


def _write_replay_chain_batch(path: Path, refs: list[tuple[int, str]], results: list[tuple[OptionChain | None, str | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            pickle.dump({"schema": "options_replay_chain_batch_v1", "refs": refs, "results": results},
                        handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_historical_database_replay(
    source: Path,
    *,
    target: str,
    rate: float,
    dividend_yield: float,
    requested_mode: str,
    workers: int,
    source_sha256: str,
    pairs: list[tuple[ResearchObservation, tuple[int, str]]],
    history: dict[str, list[tuple[date, float]]],
    close_proxy: bool,
    database_elapsed_seconds: float,
    store,
) -> OptionsResearchDataset:
    """Prepare replay chains in bounded batches and keep them disk-backed."""
    chain_started = perf_counter()
    requested_loader_workers = min(workers, available_cpu_count())
    fork_available = "fork" in mp.get_all_start_methods()
    cache_dir = _replay_batch_cache_dir(source, target, rate, dividend_yield, requested_mode)
    locations: dict[datetime, tuple[Path, int]] = {}
    observations: list[ResearchObservation] = []
    prepared_count = 0
    reused_count = 0
    stored_count = 0
    chain_worker_count = 0
    preparation_batches = 0

    for batch_start in range(0, len(pairs), CHAIN_PREPARATION_BATCH_SIZE):
        batch = pairs[batch_start:batch_start + CHAIN_PREPARATION_BATCH_SIZE]
        refs = [reference for _, reference in batch]
        cache_path = cache_dir / f"batch-{batch_start:06d}.pkl"
        results = None
        if cache_path.is_file():
            try:
                with cache_path.open("rb") as handle:
                    payload = pickle.load(handle)
                if (
                    payload.get("schema") == "options_replay_chain_batch_v1"
                    and payload.get("refs") == refs
                    and isinstance(payload.get("results"), list)
                    and len(payload["results"]) == len(batch)
                ):
                    results = payload["results"]
            except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError, ValueError, TypeError):
                results = None
            finally:
                if "payload" in locals():
                    del payload

        if results is None:
            prepared_count += len(batch)
            preparation_batches += 1
            contracts = store.contracts_for_snapshots([reference[0] for reference in refs])
            normalized_snapshots = [
                {
                    "underlying": target,
                    "spot": observation.spot,
                    "as_of": observation.as_of.isoformat(),
                    "feed": observation.feed,
                    "contracts": [
                        _normalize_contract(contract, as_of=observation.as_of, underlying=target)
                        for contract in contracts.get(reference[0], [])
                    ],
                }
                for (observation, reference) in batch
            ]
            provider = HistoricalChainSnapshotProvider(
                normalized_snapshots, history, rate=rate, dividend_yield=dividend_yield,
            )
            batch_workers = min(requested_loader_workers, len(batch)) if fork_available else 1
            chain_worker_count = max(chain_worker_count, batch_workers)
            if batch_workers > 1:
                _CHAIN_PREPARATION_CONTEXT.update({
                    "observations": [observation for observation, _ in batch],
                    "provider": provider,
                    "underlying": target,
                })
                try:
                    with ProcessPoolExecutor(
                        max_workers=batch_workers, mp_context=mp.get_context("fork"),
                    ) as executor:
                        results = list(executor.map(_prepare_chain_index, range(len(batch)), chunksize=1))
                finally:
                    _CHAIN_PREPARATION_CONTEXT.clear()
            else:
                results = []
                for observation, _reference in batch:
                    try:
                        chain = provider.get_chain(
                            target, as_of=observation.as_of, min_dte=0, max_dte=730,
                        )
                        results.append((chain, None))
                    except (KeyError, TypeError, ValueError, OverflowError) as exc:
                        results.append((None, f"{type(exc).__name__}: {exc}"))
            del contracts, normalized_snapshots, provider
            stored_count += sum(chain is not None and error is None for chain, error in results)
            _write_replay_chain_batch(cache_path, refs, results)
        else:
            reused_count += len(batch)
            stored_count += sum(chain is not None and error is None for chain, error in results)

        for index, ((observation, _reference), (_chain, error)) in enumerate(zip(batch, results)):
            observations.append(_with_chain(observation, None, error))
            locations[observation.as_of] = (cache_path, index)
        del results

    data_mode_name = "historical_chains_spot_close_proxy" if close_proxy else "historical_chains"
    return OptionsResearchDataset(
        observations=tuple(observations),
        provider=ReplayChainBatchProvider(locations, history),
        source_path=source.resolve(),
        source_sha256=source_sha256,
        data_mode=(
            "model_or_synthetic_chain_rows"
            if any(item.feed in {"model", "synthetic"} for item in observations)
            else data_mode_name
        ),
        underlying_history={key: tuple(values) for key, values in history.items()},
        loading_parallelism={
            "cache": "replay_chain_batches",
            "chains_reused": reused_count,
            "chains_prepared": prepared_count,
            "chains_stored": stored_count,
            "workers_requested": requested_loader_workers,
            "workers_used": requested_loader_workers,
            "chain_workers_used": chain_worker_count,
            "chain_preparation_batches": preparation_batches,
            "source_format": "sqlite_batches",
            "database_read_elapsed_seconds": round(database_elapsed_seconds, 3),
            "jsonl_parse_and_normalize_elapsed_seconds": 0.0,
            "chain_preparation_elapsed_seconds": round(perf_counter() - chain_started, 3),
            "elapsed_seconds": round(database_elapsed_seconds + perf_counter() - chain_started, 3),
        },
    )


def load_options_research_dataset(
    path: str | Path,
    *,
    underlying: str = "SPY",
    rate: float = 0.04,
    dividend_yield: float = 0.015,
    data_mode: str = "auto",
    model_surface_config: dict | None = None,
    workers: int = 1,
    lazy_chains: bool = False,
) -> OptionsResearchDataset:
    """Load a prepared dataset. Prepared chains are kept per snapshot, so a load only prepares new or changed days.

    A chain depends only on its own session's snapshot and the rate inputs. The store is keyed by snapshot id and
    row hash, so a snapshot that changes is prepared again and nothing else is. Model-surface datasets are not
    stored because their provider holds closures.
    """
    source = Path(path)
    target = underlying.upper()
    requested_mode = str(data_mode or "auto").lower()
    if source.suffix.lower() in {".sqlite", ".sqlite3", ".db"} and requested_mode not in {"model", "model_surface"}:
        dataset = _load_historical_database(
            source, target=target, rate=rate, dividend_yield=dividend_yield,
            requested_mode=requested_mode, workers=workers, lazy_chains=lazy_chains,
        )
        if dataset is not None:
            return dataset
    return _load_options_research_dataset_uncached(
        path, underlying=underlying, rate=rate, dividend_yield=dividend_yield,
        data_mode=data_mode, model_surface_config=model_surface_config, workers=workers,
    )


def _chain_store_path(source: Path, target: str, rate: float, dividend_yield: float, requested_mode: str) -> Path:
    key = json.dumps({
        "version": CHAIN_STORE_VERSION, "source": str(source.resolve()), "underlying": target,
        "rate": rate, "dividend_yield": dividend_yield, "data_mode": requested_mode,
    }, sort_keys=True, default=str)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return CHAIN_CACHE_DIR / f"{target}-chains-{digest}.pkl"


def _read_chain_store(path: Path) -> dict:
    """Prepared chains by (snapshot_id, row_sha256). Missing or unreadable stores are empty."""
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != CHAIN_STORE_VERSION:
        return {}
    chains = payload.get("chains")
    return chains if isinstance(chains, dict) else {}


def _write_chain_store(entries: dict, path: Path) -> None:
    """Write the store in a forked child so the parent continues. A failed write only means the next load prepares again."""
    if not hasattr(os, "fork"):
        _write_chain_store_now(entries, path)
        return
    pid = os.fork()
    if pid:
        return
    status = 0
    try:
        _write_chain_store_now(entries, path)
    except Exception:  # noqa: BLE001 - a missing store entry is not an error
        status = 1
    finally:
        os._exit(status)


def _write_chain_store_now(entries: dict, path: Path) -> None:
    CHAIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    with temp.open("wb") as handle:
        pickle.dump({"version": CHAIN_STORE_VERSION, "chains": entries}, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(temp, path)
    prune_stale_chain_caches(CHAIN_CACHE_DIR, path)


def prune_stale_chain_caches(cache_dir: Path, keep: Path) -> int:
    """Delete older prepared-chain files for the same underlying; the store is replaced whenever its snapshots change."""
    underlying = keep.name.rsplit("-", 1)[0]
    removed = 0
    for path in cache_dir.glob(f"{underlying}-*.pkl"):
        if path == keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _load_historical_database(
    source: Path, *, target: str, rate: float, dividend_yield: float, requested_mode: str, workers: int,
    lazy_chains: bool = False,
) -> OptionsResearchDataset | None:
    """Historical chains from SQLite, with prepared chains reused from the store. Returns None for model-surface data."""
    from options.research.history_store import OptionsHistoryStore

    load_started = perf_counter()
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("dataset loading workers must be a positive integer")
    if requested_mode not in {"auto", "historical", "historical_chains"}:
        raise ValueError("data_mode must be auto, historical_chains, or model_surface")
    store = OptionsHistoryStore(source, initialize=False)
    if not store.has_underlying(target):
        raise ValueError(f"no daily_close options history for {target} in {source}")
    if requested_mode == "auto" and store.has_model_surface(target):
        return None
    source_sha256 = store.dataset_fingerprint(target)
    pairs: list[tuple[ResearchObservation, tuple[int, str]]] = []
    history: dict[str, list[tuple[date, float]]] = {}
    for row in store.iter_effective_headers(target, phase="daily_close"):
        as_of = _timestamp(row.get("as_of"), field=f"snapshot[{row['source_line']}].as_of")
        spot = float(row.get("spot", 0))
        if spot <= 0 or not math.isfinite(spot):
            raise ValueError(f"database snapshot {row['source_line']} needs a positive finite spot")
        close = row.get("underlying_close")
        if close is not None:
            history.setdefault(target, []).append((as_of.date(), float(close)))
        observation = ResearchObservation(
            as_of=as_of,
            underlying=target,
            spot=spot,
            regime=str(row.get("regime", "unknown")).lower(),
            volatility_state=str(row.get("volatility_state", "unknown")).lower(),
            feed=str(row.get("feed", "historical")),
            events=_events(row.get("events", []), as_of=as_of),
            equity_signals=_signals(row.get("equity_signals", []), as_of=as_of),
            event_recommendations=_signals(row.get("event_recommendations", []), as_of=as_of),
            dividends={str(k).upper(): float(v) for k, v in (row.get("dividends") or {}).items()},
            source_line=int(row["source_line"]),
        )
        pairs.append((observation, (int(row["snapshot_id"]), str(row["row_sha256"]))))
    database_elapsed_seconds = perf_counter() - load_started
    if len(pairs) < 2:
        raise ValueError(f"dataset has fewer than two {target} research observations")
    pairs.sort(key=lambda item: item[0].as_of)
    if len({item[0].as_of for item in pairs}) != len(pairs):
        raise ValueError("duplicate as_of timestamps are not allowed")
    history[target] = sorted(set(history.get(target, [])))
    if not history.get(target):
        history[target] = [(item.as_of.date(), item.spot) for item, _ in pairs]
        close_proxy = True
    else:
        close_proxy = False

    if lazy_chains:
        return _load_historical_database_replay(
            source, target=target, rate=rate, dividend_yield=dividend_yield,
            requested_mode=requested_mode, workers=workers, source_sha256=source_sha256,
            pairs=pairs, history=history, close_proxy=close_proxy,
            database_elapsed_seconds=database_elapsed_seconds, store=store,
        )

    store_path = _chain_store_path(source, target, rate, dividend_yield, requested_mode)
    stored = _read_chain_store(store_path)
    current_keys = {reference for _, reference in pairs}
    missing = [(index, observation) for index, (observation, reference) in enumerate(pairs) if reference not in stored]
    print(
        f"Options dataset {target}: reusing {len(pairs) - len(missing)} stored chains, "
        f"preparing {len(missing)} new or changed sessions",
        flush=True,
    )
    chain_started = perf_counter()
    prepared: dict[int, tuple[OptionChain | None, str | None]] = {}
    fork_available = "fork" in mp.get_all_start_methods()
    requested_loader_workers = min(workers, available_cpu_count())
    chain_worker_count = min(requested_loader_workers, len(missing)) if fork_available else 1
    preparation_batches = 0
    for batch_start in range(0, len(missing), CHAIN_PREPARATION_BATCH_SIZE):
        batch = missing[batch_start:batch_start + CHAIN_PREPARATION_BATCH_SIZE]
        preparation_batches += 1
        contracts = store.contracts_for_snapshots([pairs[index][1][0] for index, _ in batch])
        normalized_snapshots = [
            {
                "underlying": target,
                "spot": observation.spot,
                "as_of": observation.as_of.isoformat(),
                "feed": observation.feed,
                "contracts": [
                    _normalize_contract(contract, as_of=observation.as_of, underlying=target)
                    for contract in contracts.get(pairs[index][1][0], [])
                ],
            }
            for index, observation in batch
        ]
        provider = HistoricalChainSnapshotProvider(
            normalized_snapshots, history, rate=rate, dividend_yield=dividend_yield,
        )
        if chain_worker_count > 1:
            _CHAIN_PREPARATION_CONTEXT.update({
                "observations": [observation for _, observation in batch],
                "provider": provider,
                "underlying": target,
            })
            try:
                context = mp.get_context("fork")
                with ProcessPoolExecutor(
                    max_workers=min(chain_worker_count, len(batch)), mp_context=context,
                ) as executor:
                    results = executor.map(_prepare_chain_index, range(len(batch)), chunksize=1)
                    for (index, _), result in zip(batch, results):
                        prepared[index] = result
            finally:
                _CHAIN_PREPARATION_CONTEXT.clear()
        else:
            for index, observation in batch:
                try:
                    chain = provider.get_chain(target, as_of=observation.as_of, min_dte=0, max_dte=730)
                    prepared[index] = (chain, None)
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    prepared[index] = (None, f"{type(exc).__name__}: {exc}")
        del contracts, normalized_snapshots, provider
    observations: list[ResearchObservation] = []
    entries: dict = {}
    for index, (observation, reference) in enumerate(pairs):
        if reference in stored:
            chain, error = stored[reference], None
        else:
            chain, error = prepared[index]
        if chain is not None and error is None:
            entries[reference] = chain
        observations.append(_with_chain(observation, chain, error))
    stale = set(stored) - current_keys
    if missing or stale:
        _write_chain_store(entries, store_path)
    data_mode_name = "historical_chains_spot_close_proxy" if close_proxy else "historical_chains"
    return OptionsResearchDataset(
        observations=tuple(observations),
        # Replay observations already own their prepared chains. Keep only
        # underlying-close history here instead of a second full copy of every
        # raw historical contract snapshot.
        provider=HistoricalChainSnapshotProvider([], history, rate=rate, dividend_yield=dividend_yield),
        source_path=source.resolve(),
        source_sha256=source_sha256,
        data_mode=("model_or_synthetic_chain_rows" if any(item.feed in {"model", "synthetic"} for item in observations) else data_mode_name),
        underlying_history={key: tuple(values) for key, values in history.items()},
        loading_parallelism={
            "cache": "chain_store",
            "chains_reused": len(pairs) - len(missing),
            "chains_prepared": len(missing),
            "chains_stored": len(entries),
            "workers_requested": requested_loader_workers,
            "workers_used": requested_loader_workers,
            "chain_workers_used": chain_worker_count,
            "chain_preparation_batches": preparation_batches,
            "source_format": "sqlite_batches",
            "database_read_elapsed_seconds": round(database_elapsed_seconds, 3),
            "jsonl_parse_and_normalize_elapsed_seconds": 0.0,
            "chain_preparation_elapsed_seconds": round(perf_counter() - chain_started, 3),
            "elapsed_seconds": round(perf_counter() - load_started, 3),
        },
    )


def _load_options_research_dataset_uncached(
    path: str | Path,
    *,
    underlying: str = "SPY",
    rate: float = 0.04,
    dividend_yield: float = 0.015,
    data_mode: str = "auto",
    model_surface_config: dict | None = None,
    workers: int = 1,
) -> OptionsResearchDataset:
    load_started = perf_counter()
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"historical options dataset not found: {source}")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("dataset loading workers must be a positive integer")
    target = underlying.upper()
    requested_mode = str(data_mode or "auto").lower()
    if requested_mode not in {"auto", "historical", "historical_chains", "model", "model_surface"}:
        raise ValueError("data_mode must be auto, historical_chains, or model_surface")
    requested_loader_workers = min(workers, available_cpu_count())
    parallel_loaded = None
    jsonl_elapsed_seconds = 0.0
    database_elapsed_seconds = 0.0
    database_loaded = source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
    if database_loaded:
        database_started = perf_counter()
        from options.research.history_store import OptionsHistoryStore

        store = OptionsHistoryStore(source, initialize=False)
        if not store.has_underlying(target):
            raise ValueError(f"no daily_close options history for {target} in {source}")
        source_sha256 = store.dataset_fingerprint(target)
        observations: list[ResearchObservation] = []
        normalized_snapshots = []
        model_rows = []
        history: dict[str, list[tuple[date, float]]] = {}
        use_model_surface = requested_mode in {"model", "model_surface"} or (
            requested_mode == "auto" and store.has_model_surface(target)
        )
        loader_workers_used = requested_loader_workers
        metadata = {}
        for row in store.iter_effective_snapshots(
            target, phase="daily_close",
        ):
            as_of = _timestamp(row.get("as_of"), field=f"snapshot[{row['source_line']}].as_of")
            spot = float(row.get("spot", 0))
            if spot <= 0 or not math.isfinite(spot):
                raise ValueError(f"database snapshot {row['source_line']} needs a positive finite spot")
            if use_model_surface:
                points = row.get("iv_surface") or []
                if not isinstance(points, (list, dict)) or not points:
                    raise ValueError(f"model surface snapshot {row['source_line']} needs iv_surface points")
                model_rows.append({"as_of": as_of, "spot": spot, "raw": row})
            else:
                normalized_snapshots.append({
                    "underlying": target,
                    "spot": spot,
                    "as_of": as_of.isoformat(),
                    "feed": str(row.get("feed", "historical")),
                    "contracts": row.get("contracts", []),
                })
            close = row.get("underlying_close")
            if close is not None:
                history.setdefault(target, []).append((as_of.date(), float(close)))
            observations.append(ResearchObservation(
                as_of=as_of,
                underlying=target,
                spot=spot,
                regime=str(row.get("regime", "unknown")).lower(),
                volatility_state=str(row.get("volatility_state", "unknown")).lower(),
                feed=("model" if use_model_surface else str(row.get("feed", "historical"))),
                events=_events(row.get("events", []), as_of=as_of),
                equity_signals=_signals(row.get("equity_signals", []), as_of=as_of),
                event_recommendations=_signals(row.get("event_recommendations", []), as_of=as_of),
                dividends={str(k).upper(): float(v) for k, v in (row.get("dividends") or {}).items()},
                source_line=int(row["source_line"]),
            ))
        database_elapsed_seconds = perf_counter() - database_started
        parallel_loaded = True
    elif source.suffix.lower() == ".jsonl" and requested_loader_workers > 1:
        source_sha256 = _hash_file(source)
        parallel_loaded = _load_jsonl_parallel(
            source, target=target, workers=requested_loader_workers,
            requested_mode=requested_mode,
        )
        (
            observations, normalized_snapshots, history, model_rows,
            use_model_surface, loader_workers_used, jsonl_elapsed_seconds,
        ) = parallel_loaded
        metadata = {}
        rows = None
    else:
        source_sha256 = _hash_file(source)
        rows, history, metadata = _load_rows(source)
        observations: list[ResearchObservation] = []
        normalized_snapshots = []
        model_rows = []
        use_model_surface = requested_mode in {"model", "model_surface"} or (
            requested_mode == "auto" and (
                str(metadata.get("data_mode", "")).lower() in {"model", "model_surface"}
                or any(("iv_surface" in row or metadata.get("iv_surface")) and not row.get("contracts") for row in rows)
            )
        )
        loader_workers_used = 1

    if parallel_loaded is None:
        for line, row in enumerate(rows, start=1):
            symbol = str(row.get("underlying", "")).upper()
            if symbol != target:
                continue
            as_of = _timestamp(row.get("as_of"), field=f"snapshot[{line}].as_of")
            spot = float(row.get("spot", 0))
            contracts = row.get("contracts", [])
            if spot <= 0 or not math.isfinite(spot):
                raise ValueError(f"snapshot line {line} needs a positive finite spot")
            if use_model_surface:
                points = row.get("iv_surface", metadata.get("iv_surface", []))
                if not isinstance(points, (list, dict)) or not points:
                    raise ValueError(f"model surface snapshot line {line} needs iv_surface points")
                model_rows.append({"as_of": as_of, "spot": spot, "raw": row})
            else:
                if not isinstance(contracts, list):
                    raise ValueError(f"historical snapshot line {line} needs a contracts array")
                normalized_contracts = [
                    _normalize_contract(contract, as_of=as_of, underlying=target)
                    for contract in contracts
                ]
                normalized_snapshots.append({
                    "underlying": target,
                    "spot": spot,
                    "as_of": as_of.isoformat(),
                    "feed": str(row.get("feed", "historical")),
                    "contracts": normalized_contracts,
                })
            close = row.get("underlying_close", row.get("close"))
            if close is not None:
                history.setdefault(target, []).append((as_of.date(), float(close)))
            observations.append(ResearchObservation(
                as_of=as_of,
                underlying=target,
                spot=spot,
                regime=str(row.get("regime", "unknown")).lower(),
                volatility_state=str(row.get("volatility_state", "unknown")).lower(),
                feed=("model" if use_model_surface else str(row.get("feed", "historical"))),
                events=_events(row.get("events", []), as_of=as_of),
                equity_signals=_signals(row.get("equity_signals", []), as_of=as_of),
                event_recommendations=_signals(row.get("event_recommendations", []), as_of=as_of),
                dividends={str(k).upper(): float(v) for k, v in (row.get("dividends") or {}).items()},
                source_line=line,
            ))
    if len(observations) < 2:
        raise ValueError(f"dataset has fewer than two {target} research observations")
    observations.sort(key=lambda item: item.as_of)
    if len({item.as_of for item in observations}) != len(observations):
        raise ValueError("duplicate as_of timestamps are not allowed")
    history[target] = sorted(set(history.get(target, [])))
    if not history.get(target):
        # Treating end-of-session spot as a close is only a fallback. The report
        # records this mode because intraday spot cannot safely proxy a close.
        history[target] = [(item.as_of.date(), item.spot) for item in observations]
        close_proxy = True
    else:
        close_proxy = False
    if use_model_surface:
        model_settings = dict(model_surface_config or {})
        indexed_rows = sorted(model_rows, key=lambda item: item["as_of"])

        def row_at(as_of: datetime):
            eligible = [item for item in indexed_rows if item["as_of"] <= as_of]
            if not eligible:
                raise ValueError(f"no point-in-time IV surface at or before {as_of.isoformat()}")
            return eligible[-1]

        def spot_provider(symbol: str, as_of: datetime) -> float:
            return float(row_at(as_of)["spot"])

        def surface_points(item: dict) -> list[dict]:
            points = item["raw"].get("iv_surface", metadata.get("iv_surface", []))
            if isinstance(points, dict):
                points = points.get("points", [])
            return [point for point in points if isinstance(point, dict)] if isinstance(points, list) else []

        def iv_surface(symbol: str, expiration: date, strike: float, right: str, as_of: datetime) -> float:
            item = row_at(as_of)
            spot = float(item["spot"])
            ranked = []
            for point in surface_points(item):
                try:
                    if str(point.get("right", "")).lower() != right:
                        continue
                    point_expiration = date.fromisoformat(str(point.get("expiration", point.get("expiration_date")))[:10])
                    point_strike = float(point["strike"])
                    iv = float(point.get("iv", point.get("implied_volatility")))
                    if iv > 5:
                        iv /= 100.0
                    if iv <= 0 or not math.isfinite(iv) or point_strike <= 0:
                        continue
                    tenor_distance = abs((point_expiration - expiration).days) / 30.0
                    moneyness_distance = abs(math.log(point_strike / spot) - math.log(strike / spot)) / 0.10
                    distance = math.sqrt(tenor_distance * tenor_distance + moneyness_distance * moneyness_distance)
                    ranked.append((distance, iv))
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if not ranked:
                raise ValueError(f"IV surface has no usable {right} points at {as_of.isoformat()}")
            ranked.sort(key=lambda value: value[0])
            nearest = ranked[:4]
            if nearest[0][0] <= 1e-12:
                return nearest[0][1]
            weights = [1.0 / max(distance, 0.05) ** 2 for distance, _ in nearest]
            return sum(weight * value for weight, (_, value) in zip(weights, nearest)) / sum(weights)

        def expirations_provider(symbol: str, as_of: datetime, min_dte: int, max_dte: int):
            item = row_at(as_of)
            raw_expirations = item["raw"].get("expirations")
            if not raw_expirations:
                raw_expirations = sorted({
                    str(point.get("expiration", point.get("expiration_date")))[:10]
                    for point in surface_points(item)
                    if point.get("expiration", point.get("expiration_date"))
                })
            return [date.fromisoformat(str(value)[:10]) for value in raw_expirations]

        def closes_provider(symbol: str, as_of: datetime, sessions: int):
            return [value for value in history.get(symbol.upper(), ()) if value[0] < as_of.date()][-sessions:]

        settings = model_settings
        exercise_style = str(settings.get(
            "exercise_style", "european" if target == "SPX" else "american",
        )).lower()
        provider = ModelSurfaceDataProvider(
            spot_provider=spot_provider,
            iv_surface=iv_surface,
            expirations_provider=expirations_provider,
            rate=rate,
            dividend_yield=dividend_yield,
            strike_steps=int(settings.get("strike_steps", 15)),
            strike_step_pct=float(settings.get("strike_step_pct", 0.025)),
            synthetic_spread_pct=float(settings.get("synthetic_spread_pct", 0.04)),
            exercise_style=exercise_style,
            underlying_closes_provider=closes_provider,
        )
        data_mode_name = "model_surface_iv_spot_close_proxy" if close_proxy else "model_surface_iv"
    else:
        provider = HistoricalChainSnapshotProvider(
            normalized_snapshots, history, rate=rate, dividend_yield=dividend_yield,
        )
        data_mode_name = "historical_chains_spot_close_proxy" if close_proxy else "historical_chains"
    prepared_observations = []
    fork_available = "fork" in mp.get_all_start_methods()
    chain_worker_count = min(loader_workers_used, len(observations)) if fork_available else 1
    chain_started = perf_counter()
    print(
        f"Options dataset {target}: preparing {len(observations)} point-in-time chains "
        f"with {chain_worker_count} worker(s)",
        flush=True,
    )
    if chain_worker_count > 1:
        _CHAIN_PREPARATION_CONTEXT.update({
            "observations": observations,
            "provider": provider,
            "underlying": target,
        })
        try:
            context = mp.get_context("fork")
            with ProcessPoolExecutor(max_workers=chain_worker_count, mp_context=context) as executor:
                chain_results = executor.map(_prepare_chain_index, range(len(observations)), chunksize=1)
                for observation, (chain, error) in zip(observations, chain_results):
                    prepared_observations.append(_with_chain(observation, chain, error))
        finally:
            _CHAIN_PREPARATION_CONTEXT.clear()
    else:
        for observation in observations:
            try:
                chain = provider.get_chain(
                    target, as_of=observation.as_of, min_dte=0, max_dte=730,
                )
                prepared_observations.append(_with_chain(observation, chain, None))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                prepared_observations.append(_with_chain(
                    observation, None, f"{type(exc).__name__}: {exc}",
                ))
    return OptionsResearchDataset(
        observations=tuple(prepared_observations),
        provider=provider,
        source_path=source.resolve(),
        source_sha256=source_sha256,
        data_mode=("model_or_synthetic_chain_rows" if any(row.feed in {"model", "synthetic"} for row in observations) and not use_model_surface else data_mode_name),
        underlying_history={key: tuple(values) for key, values in history.items()},
        loading_parallelism={
            "workers_requested": requested_loader_workers,
            "workers_used": loader_workers_used,
            "chain_workers_used": chain_worker_count,
            "source_format": (
                "sqlite_batches" if database_loaded
                else "jsonl_process_chunks" if loader_workers_used > 1
                else source.suffix.lower().lstrip(".") or "json"
            ),
            "database_read_elapsed_seconds": round(database_elapsed_seconds, 3),
            "jsonl_parse_and_normalize_elapsed_seconds": round(jsonl_elapsed_seconds, 3),
            "chain_preparation_elapsed_seconds": round(perf_counter() - chain_started, 3),
            "elapsed_seconds": round(perf_counter() - load_started, 3),
        },
    )


__all__ = ["OptionsResearchDataset", "ResearchObservation", "load_options_research_dataset"]
