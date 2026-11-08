"""Shared, memory-mapped 5-minute bar cache for the equity research workers.

Each bar used to be a Python dict of about 590 bytes, and every spawned fold
worker loaded its own copy of the same history. This cache stores the bars once
as flat binary arrays. Workers memory-map those arrays, so the operating system
keeps one copy in the page cache, and bar dicts are built only as they are read.

``CacheView.window`` returns dicts that match ``SQLiteBarStore.bars`` key for
key, value for value, and type for type. The build refuses data it cannot
represent exactly, and the caller then falls back to the dict path.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Sequence
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

FORMAT = 1
ENV_FLAG = "ALGO_RESEARCH_BAR_CACHE"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_CHUNK = 4096
_NUMERIC = ("o", "h", "l", "c", "v")
_FILES = {"t": "<i8", "o": "<f8", "h": "<f8", "l": "<f8", "c": "<f8", "v": "<f8",
          "n": "<f8", "vw": "<f8", "has_n": "|b1", "has_vw": "|b1", "ing": "<i4"}
_CACHE_KEY = re.compile(r"^[0-9a-f]{24}$")
_LOCK_NAME = ".cache-maintenance.lock"


def enabled() -> bool:
    """On by default; set ALGO_RESEARCH_BAR_CACHE=0 to fall back to per-worker dicts."""
    value = os.environ.get(ENV_FLAG, "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _micros(timestamp: str) -> int:
    return int(np.datetime64(timestamp.rstrip("Z"), "us").astype(np.int64))


def _format_micros(values: np.ndarray) -> list[str]:
    return [text + "Z" for text in np.datetime_as_string(values.view("datetime64[us]"), unit="us").tolist()]


def cache_key(db_path, *, feed: str, timeframe: str, adjustment: str, symbols) -> str:
    path = Path(db_path).resolve()
    stat = path.stat()
    wal = Path(f"{path}-wal")
    wal_stamp = [wal.stat().st_size, wal.stat().st_mtime_ns] if wal.exists() else None
    payload = {
        "db": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "wal": wal_stamp,
        "feed": feed, "timeframe": timeframe, "adjustment": adjustment,
        "symbols": sorted({str(symbol).upper() for symbol in symbols}), "format": FORMAT,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def _acquire_cache_lock(cache_root, *, exclusive: bool = False, blocking: bool = True) -> int:
    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / _LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, operation)
    except Exception:
        os.close(fd)
        raise
    return fd


@contextmanager
def _shared_cache_lock(cache_root):
    fd = _acquire_cache_lock(cache_root)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def build(db_path, cache_root, *, feed: str, timeframe: str, adjustment: str, symbols) -> Path:
    """Build or reuse a cache while excluding concurrent cache pruning."""
    with _shared_cache_lock(cache_root):
        return _build_locked(db_path, cache_root, feed=feed, timeframe=timeframe,
                             adjustment=adjustment, symbols=symbols)


def _build_locked(db_path, cache_root, *, feed: str, timeframe: str, adjustment: str, symbols) -> Path:
    """Write the cache for every row of ``symbols`` and return its directory.

    An existing cache with the same key is reused. Concurrent builders write to
    private temporary directories and publish with an atomic rename.
    """
    symbols = sorted({str(symbol).upper() for symbol in symbols})
    key = cache_key(db_path, feed=feed, timeframe=timeframe, adjustment=adjustment, symbols=symbols)
    target = Path(cache_root) / key
    if (target / "manifest.json").exists():
        return target
    staging = Path(cache_root) / f".building-{key}-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True)
    handles = {name: (staging / f"{name}.bin").open("wb") for name in _FILES}
    ingested: list[str | None] = []
    ingest_index: dict[str | None, int] = {}
    layout: dict[str, list[int]] = {}
    offset = 0
    try:
        with closing(sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)) as connection:
            for symbol in symbols:
                rows = connection.execute(
                    """SELECT timestamp, open, high, low, close, volume, trade_count, vwap, ingested_at
                       FROM market_bar
                       WHERE feed=? AND adjustment=? AND timeframe=? AND symbol=?
                       ORDER BY timestamp""",
                    (feed, adjustment, timeframe, symbol),
                ).fetchall()
                if not rows:
                    layout[symbol] = [offset, 0]
                    continue
                stamps = [row[0] for row in rows]
                if not all(_TIMESTAMP.match(stamp) for stamp in stamps):
                    raise ValueError(f"{symbol}: timestamp format is not fixed-width UTC")
                t_us = np.array([stamp[:-1] for stamp in stamps], dtype="datetime64[us]").astype("<i8")
                if _format_micros(t_us) != stamps:
                    raise ValueError(f"{symbol}: timestamps do not round-trip through microseconds")
                numeric = {name: [] for name in _NUMERIC}
                nums = {"n": [], "vw": []}
                has = {"n": [], "vw": []}
                for row in rows:
                    for name, value in zip(_NUMERIC, row[1:6]):
                        if type(value) is not float:
                            raise ValueError(f"{symbol}: {name} is not stored as REAL")
                        numeric[name].append(value)
                    for name, value in (("n", row[6]), ("vw", row[7])):
                        if value is None:
                            nums[name].append(float("nan"))
                            has[name].append(False)
                        elif type(value) is float:
                            nums[name].append(value)
                            has[name].append(True)
                        else:
                            raise ValueError(f"{symbol}: {name} is not stored as REAL")
                ing = []
                for row in rows:
                    stamp = row[8]
                    if stamp not in ingest_index:
                        ingest_index[stamp] = len(ingested)
                        ingested.append(stamp)
                    ing.append(ingest_index[stamp])
                handles["t"].write(t_us.tobytes())
                for name in _NUMERIC:
                    handles[name].write(np.asarray(numeric[name], dtype="<f8").tobytes())
                handles["n"].write(np.asarray(nums["n"], dtype="<f8").tobytes())
                handles["vw"].write(np.asarray(nums["vw"], dtype="<f8").tobytes())
                handles["has_n"].write(np.asarray(has["n"], dtype="|b1").tobytes())
                handles["has_vw"].write(np.asarray(has["vw"], dtype="|b1").tobytes())
                handles["ing"].write(np.asarray(ing, dtype="<i4").tobytes())
                layout[symbol] = [offset, len(rows)]
                offset += len(rows)
    except Exception:
        for handle in handles.values():
            handle.close()
        shutil.rmtree(staging, ignore_errors=True)
        raise
    for handle in handles.values():
        handle.close()
    manifest = {"format": FORMAT, "feed": feed, "timeframe": timeframe, "adjustment": adjustment,
                "rows": offset, "symbols": layout, "ingested": ingested}
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    try:
        os.replace(staging, target)
    except OSError:
        # Another worker published the same key first; its copy is identical.
        shutil.rmtree(staging, ignore_errors=True)
    return target


class LazyBars(Sequence):
    """A read-only list of bar dicts backed by the shared arrays."""

    def __init__(self, view: "CacheView", lo: int, hi: int) -> None:
        self._view = view
        self._lo = lo
        self._hi = hi

    def __len__(self) -> int:
        return self._hi - self._lo

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("bar index out of range")
        return self._view.rows(self._lo + index, self._lo + index + 1)[0]

    def __iter__(self):
        for start in range(self._lo, self._hi, _CHUNK):
            yield from self._view.rows(start, min(start + _CHUNK, self._hi))


class CacheView:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock_fd = _acquire_cache_lock(self.directory.parent)
        self.arrays = {}
        try:
            manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("format") != FORMAT:
                raise ValueError("bar cache format is not supported")
            self.symbols = manifest["symbols"]
            self.ingested = manifest["ingested"]
            rows = manifest["rows"]
            for name, dtype in _FILES.items():
                path = self.directory / f"{name}.bin"
                if rows == 0:
                    self.arrays[name] = np.zeros(0, dtype=dtype)
                else:
                    self.arrays[name] = np.memmap(path, dtype=dtype, mode="r", shape=(rows,))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for array in getattr(self, "arrays", {}).values():
            mmap = getattr(array, "_mmap", None)
            if mmap is not None:
                mmap.close()
        self.arrays = {}
        fd = getattr(self, "_lock_fd", None)
        if fd is not None:
            self._lock_fd = None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def window(self, symbol: str, start: str, end: str) -> LazyBars:
        """Bars for ``symbol`` between two ISO dates, inclusive, as the store would return them."""
        offset, count = self.symbols.get(symbol.upper(), [0, 0])
        if count == 0:
            return LazyBars(self, 0, 0)
        stamps = self.arrays["t"][offset:offset + count]
        lo = int(np.searchsorted(stamps, _micros(f"{start}T00:00:00.000000Z"), side="left"))
        hi = int(np.searchsorted(stamps, _micros(f"{end}T23:59:59.999999Z"), side="right"))
        return LazyBars(self, offset + lo, offset + hi)

    def rows(self, lo: int, hi: int) -> list[dict]:
        a = self.arrays
        stamps = _format_micros(np.asarray(a["t"][lo:hi]))
        columns = {name: np.asarray(a[name][lo:hi]).tolist() for name in _NUMERIC}
        n_values = np.asarray(a["n"][lo:hi]).tolist()
        vw_values = np.asarray(a["vw"][lo:hi]).tolist()
        has_n = np.asarray(a["has_n"][lo:hi]).tolist()
        has_vw = np.asarray(a["has_vw"][lo:hi]).tolist()
        ing = np.asarray(a["ing"][lo:hi]).tolist()
        output = []
        for position in range(hi - lo):
            bar = {"t": stamps[position], "o": columns["o"][position], "h": columns["h"][position],
                   "l": columns["l"][position], "c": columns["c"][position], "v": columns["v"][position]}
            bar["_ingested_at"] = self.ingested[ing[position]]
            if has_n[position]:
                bar["n"] = n_values[position]
            if has_vw[position]:
                bar["vw"] = vw_values[position]
            output.append(bar)
        return output


def window_bars(directory: Path, symbols, start: str, end: str) -> dict[str, LazyBars]:
    """Drop-in for ``SQLiteBarStore.bars``: one entry per requested symbol, upper-cased keys."""
    view = CacheView(directory)
    return {symbol.upper(): view.window(symbol, start, end) for symbol in symbols}

def load_window_bars(db_path, cache_root, *, feed: str, timeframe: str, adjustment: str,
                     symbols, start: str, end: str) -> dict[str, LazyBars]:
    """Build/reuse and map a cache under one lease, closing the prune race."""
    with _shared_cache_lock(cache_root):
        directory = build(db_path, cache_root, feed=feed, timeframe=timeframe,
                          adjustment=adjustment, symbols=symbols)
        return window_bars(directory, symbols, start, end)


def _valid_current_cache(directory: Path, db_path) -> bool:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            return False
        feed, timeframe, adjustment = manifest["feed"], manifest["timeframe"], manifest["adjustment"]
        rows, symbols, ingested = manifest["rows"], manifest["symbols"], manifest["ingested"]
        if not all(isinstance(value, str) for value in (feed, timeframe, adjustment)):
            return False
        if type(rows) is not int or rows < 0 or not isinstance(symbols, dict) or not isinstance(ingested, list):
            return False
        spans = []
        for symbol, span in symbols.items():
            if not isinstance(symbol, str) or not isinstance(span, list) or len(span) != 2:
                return False
            offset, count = span
            if type(offset) is not int or type(count) is not int or offset < 0 or count < 0 or offset + count > rows:
                return False
            if count:
                spans.append((offset, offset + count))
        spans.sort()
        if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
            return False
        if sum(end - start for start, end in spans) != rows:
            return False
        for name, dtype in _FILES.items():
            if (directory / f"{name}.bin").stat().st_size != rows * np.dtype(dtype).itemsize:
                return False
        expected = cache_key(db_path, feed=feed, timeframe=timeframe,
                             adjustment=adjustment, symbols=symbols)
        return directory.name == expected
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _tree_bytes(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory, followlinks=False):
        for name in files:
            try:
                path = Path(root) / name
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                pass
    return total


def prune_obsolete(db_path, cache_root, *, dry_run: bool = False) -> dict:
    """Remove only cache-key directories invalid for the current DB fingerprint.

    The exclusive non-blocking lease makes active builders/readers cause a safe
    skip. Unknown paths and symlinks are always preserved.
    """
    root = Path(cache_root)
    try:
        lock_fd = _acquire_cache_lock(root, exclusive=True, blocking=False)
    except BlockingIOError:
        return {"status": "skipped", "reason": "cache has active reader or builder", "removed": [], "bytes_freed": 0}
    try:
        database = Path(db_path)
        if not database.is_file():
            return {"status": "skipped", "reason": "source database is unavailable", "removed": [], "bytes_freed": 0}
        removals = []
        kept = []
        for entry in sorted(root.iterdir()):
            if entry.name == _LOCK_NAME:
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name.startswith(".building-"):
                reason = "abandoned staging directory (exclusive cache lease acquired)"
            elif _CACHE_KEY.fullmatch(entry.name):
                if _valid_current_cache(entry, database):
                    kept.append(entry.name)
                    continue
                reason = "cache key is stale or cache contents are invalid"
            else:
                continue
            removals.append({"path": str(entry), "reason": reason, "bytes": _tree_bytes(entry)})
        result = {"status": "dry-run" if dry_run else "complete", "kept_current": kept,
                  "removed": removals, "bytes_freed": sum(item["bytes"] for item in removals)}
        if not dry_run:
            for item in removals:
                shutil.rmtree(item["path"])
        return result
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
