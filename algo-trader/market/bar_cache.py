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

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import numpy as np

FORMAT = 1
ENV_FLAG = "ALGO_RESEARCH_BAR_CACHE"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_CHUNK = 4096
_NUMERIC = ("o", "h", "l", "c", "v")
_FILES = {"t": "<i8", "o": "<f8", "h": "<f8", "l": "<f8", "c": "<f8", "v": "<f8",
          "n": "<f8", "vw": "<f8", "has_n": "|b1", "has_vw": "|b1", "ing": "<i4"}


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


def build(db_path, cache_root, *, feed: str, timeframe: str, adjustment: str, symbols) -> Path:
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
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            raise ValueError("bar cache format is not supported")
        self.symbols = manifest["symbols"]
        self.ingested = manifest["ingested"]
        rows = manifest["rows"]
        self.arrays = {}
        for name, dtype in _FILES.items():
            path = self.directory / f"{name}.bin"
            if rows == 0:
                self.arrays[name] = np.zeros(0, dtype=dtype)
            else:
                self.arrays[name] = np.memmap(path, dtype=dtype, mode="r", shape=(rows,))

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
