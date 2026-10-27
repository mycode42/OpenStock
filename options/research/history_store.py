"""Transactional, source-aware store for historical option-chain snapshots.

SQLite is canonical. DuckDB reads normalized SQLite tables for research. The
Theta monthly response cache remains resumable vendor input; assembled chain
JSONL files are legacy migration inputs only.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Iterable, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from runtime.paths import DATA_DIR


ET = ZoneInfo("America/New_York")
DEFAULT_DATABASE = DATA_DIR / "options" / "research" / "options_history.sqlite3"
SCHEMA_VERSION = "options-chain-history-v3-liquidity-summary"
_BATCH_SNAPSHOTS = 32


_SCHEMA = """
CREATE TABLE IF NOT EXISTS options_history_source (
    source_id TEXT PRIMARY KEY,
    underlying TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT,
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    session_count INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS options_history_source_path_idx
    ON options_history_source(source_path, status);

CREATE TABLE IF NOT EXISTS options_history_active_source (
    underlying TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES options_history_source(source_id),
    PRIMARY KEY(underlying, source_kind)
);

CREATE TABLE IF NOT EXISTS options_history_snapshot (
    snapshot_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES options_history_source(source_id) ON DELETE CASCADE,
    underlying TEXT NOT NULL,
    session_date TEXT NOT NULL,
    as_of TEXT NOT NULL,
    phase TEXT NOT NULL,
    feed TEXT NOT NULL,
    spot REAL NOT NULL,
    underlying_close REAL,
    equity_volume INTEGER,
    regime TEXT NOT NULL,
    volatility_state TEXT NOT NULL,
    events_json TEXT NOT NULL,
    equity_signals_json TEXT NOT NULL,
    event_recommendations_json TEXT NOT NULL,
    dividends_json TEXT NOT NULL,
    iv_surface_json TEXT,
    expirations_json TEXT,
    row_sha256 TEXT NOT NULL,
    UNIQUE(source_id, session_date, phase)
);
CREATE INDEX IF NOT EXISTS options_history_snapshot_lookup_idx
    ON options_history_snapshot(underlying, session_date, phase, as_of);
CREATE INDEX IF NOT EXISTS options_history_snapshot_source_idx
    ON options_history_snapshot(source_id, session_date);

CREATE TABLE IF NOT EXISTS options_history_contract (
    snapshot_id INTEGER NOT NULL REFERENCES options_history_snapshot(snapshot_id) ON DELETE CASCADE,
    contract_symbol TEXT NOT NULL,
    underlying_symbol TEXT NOT NULL,
    expiration_date TEXT NOT NULL,
    right TEXT NOT NULL,
    strike REAL NOT NULL,
    multiplier INTEGER NOT NULL,
    exercise_style TEXT NOT NULL,
    tradable INTEGER NOT NULL,
    open_interest INTEGER,
    open_interest_date TEXT,
    volume INTEGER,
    bid REAL NOT NULL,
    ask REAL NOT NULL,
    quote_at TEXT NOT NULL,
    iv REAL,
    greeks_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, contract_symbol)
);
CREATE INDEX IF NOT EXISTS options_history_contract_expiry_idx
    ON options_history_contract(snapshot_id, expiration_date, right, strike);
CREATE TABLE IF NOT EXISTS options_history_liquidity (
    snapshot_id INTEGER PRIMARY KEY REFERENCES options_history_snapshot(snapshot_id) ON DELETE CASCADE,
    session_date TEXT NOT NULL,
    option_volume_60d INTEGER,
    option_volume_coverage REAL,
    total_open_interest_60d INTEGER,
    open_interest_coverage REAL,
    median_option_spread REAL,
    spread_coverage REAL
);
CREATE INDEX IF NOT EXISTS options_history_liquidity_date_idx
    ON options_history_liquidity(session_date);
CREATE TABLE IF NOT EXISTS options_automation_checkpoint (
    run_id TEXT PRIMARY KEY,
    config_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eligible_universe_snapshot (
    as_of_date TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    eligible_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (as_of_date, rules_sha256)
);
"""


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _finite_or_none(value, *, field: str) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _integer_or_none(value, *, field: str) -> int | None:
    if value is None:
        return None
    result = int(float(value))
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _snapshot_liquidity(contracts: list[dict], *, spot: float, session: str) -> dict:
    volume_values = []
    oi_values = []
    spreads = []
    volume_eligible = oi_eligible = spread_eligible = 0
    session_day = datetime.fromisoformat(session).date()
    for row in contracts:
        try:
            expiry = datetime.fromisoformat(str(row.get("expiration_date", row.get("expiration", "")))[:10]).date()
            dte = (expiry - session_day).days
            strike = float(row.get("strike_price", row.get("strike", 0)))
            bid, ask = float(row.get("bid", 0)), float(row.get("ask", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if not 0 <= dte <= 60:
            continue
        volume_eligible += 1
        oi_eligible += 1
        volume = _integer_or_none(row.get("volume"), field="contract volume")
        oi = _integer_or_none(row.get("open_interest"), field="contract open interest")
        if volume is not None:
            volume_values.append(volume)
        if oi is not None:
            oi_values.append(oi)
        if (
            7 <= dte <= 60 and spot > 0 and 0.8 <= strike / spot <= 1.2
            and bid >= 0 and ask > 0 and ask >= bid
        ):
            spread_eligible += 1
            midpoint = (bid + ask) / 2
            if midpoint > 0:
                spreads.append((ask - bid) / midpoint)

    def coverage(observed: int, eligible: int) -> float | None:
        return observed / eligible if eligible else None

    return {
        "option_volume_60d": sum(volume_values) if volume_values else None,
        "option_volume_coverage": coverage(len(volume_values), volume_eligible),
        "total_open_interest_60d": sum(oi_values) if oi_values else None,
        "open_interest_coverage": coverage(len(oi_values), oi_eligible),
        "median_option_spread": sorted(spreads)[len(spreads) // 2] if spreads else None,
        "spread_coverage": coverage(len(spreads), spread_eligible),
    }


def _source_liquidity_coverage(connection: sqlite3.Connection, source_id: str) -> dict:
    row = connection.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN s.equity_volume IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN l.option_volume_60d IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN l.option_volume_60d IS NOT NULL
                             AND l.option_volume_coverage >= 0.8 THEN 1 ELSE 0 END),
               SUM(CASE WHEN l.total_open_interest_60d IS NOT NULL
                             AND l.open_interest_coverage >= 0.8 THEN 1 ELSE 0 END),
               SUM(CASE WHEN l.median_option_spread IS NOT NULL
                             AND l.spread_coverage >= 0.8 THEN 1 ELSE 0 END)
        FROM options_history_snapshot AS s
        LEFT JOIN options_history_liquidity AS l ON l.snapshot_id=s.snapshot_id
        WHERE s.source_id=? AND s.phase='daily_close'
    """, (source_id,)).fetchone()
    return {
        "daily_close_sessions": int(row[0] or 0),
        "equity_volume_sessions": int(row[1] or 0),
        "option_volume_sessions": int(row[2] or 0),
        "option_volume_sessions_80pct_coverage": int(row[3] or 0),
        "open_interest_sessions_80pct_coverage": int(row[4] or 0),
        "option_spread_sessions_80pct_coverage": int(row[5] or 0),
    }


def _session_date(row: dict) -> tuple[str, str]:
    raw = str(row.get("as_of", "")).replace("Z", "+00:00")
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("snapshot as_of must include a timezone")
    return stamp.astimezone(ET).date().isoformat(), stamp.isoformat()


def _source_kind(value: str) -> str:
    kind = str(value).strip().lower()
    if kind not in {"theta", "alpaca", "theta_opening", "alpaca_opening"}:
        raise ValueError("source_kind must be theta, alpaca, theta_opening, or alpaca_opening")
    return kind


def _contract_values(row: dict, *, underlying: str, as_of: str) -> tuple:
    symbol = str(row.get("symbol") or "").strip().upper()
    expiration = str(row.get("expiration_date", row.get("expiration", "")))[:10]
    right = str(row.get("right", row.get("type", ""))).strip().lower()
    if not symbol or not expiration or right not in {"call", "put"}:
        raise ValueError("contract needs symbol, expiration_date, and call/put type")
    strike = _finite_or_none(row.get("strike", row.get("strike_price")), field="contract strike")
    bid = _finite_or_none(row.get("bid", 0), field="contract bid")
    ask = _finite_or_none(row.get("ask", 0), field="contract ask")
    if strike is None or strike <= 0 or bid is None or ask is None:
        raise ValueError(f"contract {symbol} has invalid strike or quote")
    quote_at_raw = str(row.get("quote_at", row.get("observed_at", as_of))).replace("Z", "+00:00")
    quote_at = datetime.fromisoformat(quote_at_raw)
    if quote_at.tzinfo is None or quote_at.utcoffset() is None:
        raise ValueError(f"contract {symbol} quote_at must include a timezone")
    oi = _integer_or_none(row.get("open_interest"), field="contract open interest")
    volume = _integer_or_none(row.get("volume"), field="contract volume")
    multiplier = int(float(row.get("multiplier", row.get("size", 100)) or 100))
    iv = _finite_or_none(row.get("iv", row.get("implied_volatility")), field="contract IV")
    greeks = row.get("greeks") or {}
    if not isinstance(greeks, dict):
        greeks = {}
    return (
        symbol,
        str(row.get("underlying", row.get("underlying_symbol", underlying)) or underlying).upper(),
        expiration,
        right,
        strike,
        multiplier,
        str(row.get("exercise_style", row.get("style", "american")) or "american").lower(),
        int(bool(row.get("tradable", str(row.get("status", "active")).lower() == "active"))),
        oi,
        str(row.get("open_interest_date")) if row.get("open_interest_date") is not None else None,
        volume,
        bid,
        ask,
        quote_at.isoformat(),
        iv,
        _json(greeks),
    )


class OptionsHistoryStore:
    """Source-aware SQLite store with a DuckDB read path for research."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE, *, initialize: bool = True) -> None:
        self.path = Path(path).resolve()
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(_SCHEMA)
                snapshot_columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(options_history_snapshot)"
                )}
                if "equity_volume" not in snapshot_columns:
                    connection.execute(
                        "ALTER TABLE options_history_snapshot ADD COLUMN equity_volume INTEGER"
                    )
                contract_columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(options_history_contract)"
                )}
                if "volume" not in contract_columns:
                    connection.execute(
                        "ALTER TABLE options_history_contract ADD COLUMN volume INTEGER"
                    )
                connection.commit()

    def write_eligible_snapshot(self, result: dict) -> None:
        """Persist one selector run; re-running the same date and rules replaces it."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO eligible_universe_snapshot
                    (as_of_date, rules_sha256, created_at, status, eligible_json, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(as_of_date, rules_sha256) DO UPDATE SET
                    created_at = excluded.created_at,
                    status = excluded.status,
                    eligible_json = excluded.eligible_json,
                    payload_json = excluded.payload_json
                """,
                (
                    str(result["as_of_date"]),
                    str(result["rules_sha256"]),
                    str(result["created_at"]),
                    str(result["status"]),
                    json.dumps(result.get("eligible_symbols", []), allow_nan=False),
                    json.dumps(result, sort_keys=True, allow_nan=False),
                ),
            )
            connection.commit()

    def latest_eligible_snapshot(self) -> dict | None:
        """Return the most recent selector run, or None before the first run."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM eligible_universe_snapshot "
                "ORDER BY as_of_date DESC, created_at DESC LIMIT 1",
            ).fetchone()
        return json.loads(row[0]) if row else None

    def read_automation_checkpoint(self, run_id: str) -> dict | None:
        """Return the saved automation checkpoint for a run, if one exists."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM options_automation_checkpoint WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        return payload if isinstance(payload, dict) else None

    def write_automation_checkpoint(self, run_id: str, payload: dict) -> None:
        """Upsert one run's checkpoint; runs are resumable across process restarts."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO options_automation_checkpoint (run_id, config_sha256, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    config_sha256 = excluded.config_sha256,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    str(payload.get("config_sha256") or ""),
                    json.dumps(payload, sort_keys=True, allow_nan=False),
                    str(payload.get("updated_at") or ""),
                ),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=60.0)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=60000")
        return connection

    @staticmethod
    def _persist_snapshot(
        connection: sqlite3.Connection, *, source_id: str, underlying: str,
        source_kind: str, row: dict,
    ) -> bool:
        """Upsert one normalized snapshot and its contracts in the current transaction."""
        session, as_of = _session_date(row)
        phase = str(row.get("snapshot_phase", "daily_close")).strip().lower()
        if phase not in {"daily_close", "opening"}:
            phase = "daily_close"
        previous = connection.execute(
            "SELECT snapshot_id, as_of FROM options_history_snapshot "
            "WHERE source_id=? AND session_date=? AND phase=?",
            (source_id, session, phase),
        ).fetchone()
        if previous:
            old_as_of = datetime.fromisoformat(previous[1].replace("Z", "+00:00"))
            new_as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if old_as_of.astimezone(timezone.utc) >= new_as_of.astimezone(timezone.utc):
                return False

        spot = _finite_or_none(row.get("spot"), field="snapshot spot")
        if spot is None or spot <= 0:
            raise ValueError(f"{underlying} {session} needs a positive finite spot")
        close = _finite_or_none(row.get("underlying_close", row.get("close")), field="underlying close")
        contracts = row.get("contracts") or []
        if not isinstance(contracts, list):
            raise ValueError(f"{underlying} {session} contracts must be an array")
        serialized = _json(row)
        row_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        connection.execute("""
            INSERT INTO options_history_snapshot
                (source_id, underlying, session_date, as_of, phase, feed, spot,
                 underlying_close, regime, volatility_state, events_json,
                 equity_signals_json, event_recommendations_json, dividends_json,
                 iv_surface_json, expirations_json, row_sha256, equity_volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, session_date, phase) DO UPDATE SET
                as_of=excluded.as_of, feed=excluded.feed, spot=excluded.spot,
                underlying_close=excluded.underlying_close, regime=excluded.regime,
                equity_volume=excluded.equity_volume,
                volatility_state=excluded.volatility_state,
                events_json=excluded.events_json,
                equity_signals_json=excluded.equity_signals_json,
                event_recommendations_json=excluded.event_recommendations_json,
                dividends_json=excluded.dividends_json,
                iv_surface_json=excluded.iv_surface_json,
                expirations_json=excluded.expirations_json,
                row_sha256=excluded.row_sha256
        """, (
            source_id, underlying, session, as_of, phase, str(row.get("feed", source_kind)),
            spot, close, str(row.get("regime", "unknown")).lower(),
            str(row.get("volatility_state", "unknown")).lower(),
            _json(row.get("events") or []), _json(row.get("equity_signals") or []),
            _json(row.get("event_recommendations") or []), _json(row.get("dividends") or {}),
            _json(row.get("iv_surface")) if row.get("iv_surface") is not None else None,
            _json(row.get("expirations")) if row.get("expirations") is not None else None,
            row_hash, _integer_or_none(row.get("equity_volume"), field="equity volume"),
        ))
        snapshot_id = connection.execute(
            "SELECT snapshot_id FROM options_history_snapshot WHERE source_id=? AND session_date=? AND phase=?",
            (source_id, session, phase),
        ).fetchone()[0]
        connection.execute("DELETE FROM options_history_contract WHERE snapshot_id=?", (snapshot_id,))
        valid_contracts = [contract for contract in contracts if isinstance(contract, dict)]
        connection.executemany("""
            INSERT INTO options_history_contract
                (snapshot_id, contract_symbol, underlying_symbol, expiration_date,
                 right, strike, multiplier, exercise_style, tradable, open_interest,
                 open_interest_date, volume, bid, ask, quote_at, iv, greeks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (snapshot_id, *_contract_values(contract, underlying=underlying, as_of=as_of))
            for contract in valid_contracts
        ])
        liquidity = _snapshot_liquidity(valid_contracts, spot=spot, session=session)
        connection.execute("""
            INSERT INTO options_history_liquidity
                (snapshot_id, session_date, option_volume_60d,
                 option_volume_coverage, total_open_interest_60d,
                 open_interest_coverage, median_option_spread, spread_coverage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                session_date=excluded.session_date,
                option_volume_60d=excluded.option_volume_60d,
                option_volume_coverage=excluded.option_volume_coverage,
                total_open_interest_60d=excluded.total_open_interest_60d,
                open_interest_coverage=excluded.open_interest_coverage,
                median_option_spread=excluded.median_option_spread,
                spread_coverage=excluded.spread_coverage
        """, (
            snapshot_id, session, liquidity["option_volume_60d"],
            liquidity["option_volume_coverage"], liquidity["total_open_interest_60d"],
            liquidity["open_interest_coverage"], liquidity["median_option_spread"],
            liquidity["spread_coverage"],
        ))
        return True

    def write_snapshots(
        self, underlying: str, source_kind: str, snapshots: Iterable[dict],
        *, replace_source: bool,
    ) -> dict:
        """Write collector snapshots without creating an intermediate JSONL file.

        Full historical backfills stage a new source and switch it active only
        after every snapshot is committed. Daily/live collectors upsert into
        the existing active source, retaining all earlier sessions.
        """
        symbol = str(underlying).strip().upper()
        kind = _source_kind(source_kind)
        if replace_source and kind not in {"theta", "theta_opening"}:
            raise ValueError("only complete ThetaData backfills may replace a source")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            if replace_source:
                source_id = uuid4().hex
                connection.execute("""
                    INSERT INTO options_history_source
                        (source_id, underlying, source_kind, source_path, source_sha256,
                         source_size, source_mtime_ns, status, session_count, imported_at)
                    VALUES (?, ?, ?, ?, NULL, 0, 0, 'importing', 0, ?)
                """, (source_id, symbol, kind, f"database://{symbol}/{kind}", now))
                connection.commit()
            else:
                active = connection.execute("""
                    SELECT s.source_id FROM options_history_active_source a
                    JOIN options_history_source s ON s.source_id=a.source_id
                    WHERE a.underlying=? AND a.source_kind=?
                """, (symbol, kind)).fetchone()
                source_id = active[0] if active else uuid4().hex
                if not active:
                    connection.execute("""
                        INSERT INTO options_history_source
                            (source_id, underlying, source_kind, source_path, source_sha256,
                             source_size, source_mtime_ns, status, session_count, imported_at)
                        VALUES (?, ?, ?, ?, NULL, 0, 0, 'complete', 0, ?)
                    """, (source_id, symbol, kind, f"database://{symbol}/{kind}", now))
                    connection.execute("""
                        INSERT INTO options_history_active_source(underlying, source_kind, source_id)
                        VALUES (?, ?, ?)
                        ON CONFLICT(underlying, source_kind) DO UPDATE SET source_id=excluded.source_id
                    """, (symbol, kind, source_id))
                    connection.commit()

        sessions_written = 0
        last_session = ""
        connection = self._connect()
        try:
            for row in snapshots:
                if not isinstance(row, dict) or str(row.get("underlying", "")).strip().upper() != symbol:
                    raise ValueError(f"snapshot stream contains a non-{symbol} row")
                session, _as_of = _session_date(row)
                if replace_source and last_session and session < last_session:
                    raise ValueError("replacement snapshots must be ordered by New York session")
                last_session = session
                if self._persist_snapshot(
                    connection, source_id=source_id, underlying=symbol,
                    source_kind=kind, row=row,
                ):
                    sessions_written += 1
                if sessions_written and sessions_written % _BATCH_SNAPSHOTS == 0:
                    connection.commit()
            connection.commit()
            source_digest = hashlib.sha256(SCHEMA_VERSION.encode("ascii"))
            for session, phase, row_hash in connection.execute(
                "SELECT session_date, phase, row_sha256 FROM options_history_snapshot "
                "WHERE source_id=? ORDER BY session_date, phase", (source_id,),
            ):
                source_digest.update(f"\0{session}\0{phase}\0{row_hash}".encode("utf-8"))
            source_hash = source_digest.hexdigest()
            if replace_source:
                if sessions_written == 0:
                    raise ValueError(f"refusing to replace {symbol}/{kind} with an empty history")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE options_history_source SET status='complete', session_count=?, source_sha256=? WHERE source_id=?",
                    (sessions_written, source_hash, source_id),
                )
                old = connection.execute(
                    "SELECT source_id FROM options_history_active_source WHERE underlying=? AND source_kind=?",
                    (symbol, kind),
                ).fetchone()
                connection.execute("""
                    INSERT INTO options_history_active_source(underlying, source_kind, source_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(underlying, source_kind) DO UPDATE SET source_id=excluded.source_id
                """, (symbol, kind, source_id))
                if old and old[0] != source_id:
                    connection.execute("DELETE FROM options_history_source WHERE source_id=?", (old[0],))
                connection.commit()
            else:
                total = connection.execute(
                    "SELECT COUNT(*) FROM options_history_snapshot WHERE source_id=?", (source_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE options_history_source SET status='complete', session_count=?, source_sha256=? WHERE source_id=?",
                    (int(total), source_hash, source_id),
                )
                connection.commit()
        except Exception:
            connection.rollback()
            if replace_source:
                with closing(self._connect()) as mark:
                    mark.execute("UPDATE options_history_source SET status='failed' WHERE source_id=?", (source_id,))
                    mark.commit()
            raise
        finally:
            connection.close()
        with closing(self._connect()) as coverage_connection:
            coverage = _source_liquidity_coverage(coverage_connection, source_id)
        return {
            "underlying": symbol, "source_kind": kind,
            "status": "replaced" if replace_source else "updated",
            "sessions_written": sessions_written,
            "sessions": int(total) if not replace_source else sessions_written,
            "source_id": source_id,
            **coverage,
        }

    def import_jsonl(self, path: str | Path, *, underlying: str, source_kind: str) -> dict:
        """Import one legacy JSONL source, then atomically make it active.

        Active collectors use :meth:`write_snapshots`; this importer supports
        migration from existing JSONL archives. An interrupted import remains
        inactive, so readers continue to see the last complete version.
        """
        source_path = Path(path).resolve()
        symbol = str(underlying).strip().upper()
        kind = _source_kind(source_kind)
        if not source_path.is_file():
            return {"underlying": symbol, "source_kind": kind, "status": "missing", "sessions": 0}
        stat = source_path.stat()
        expected_source_id = hashlib.sha256(
            f"{SCHEMA_VERSION}\0{symbol}\0{kind}\0{source_path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()
        ).hexdigest()
        with closing(self._connect()) as connection:
            active = connection.execute("""
                SELECT s.source_id, s.source_size, s.source_mtime_ns, s.session_count
                FROM options_history_active_source a
                JOIN options_history_source s ON s.source_id=a.source_id
                WHERE a.underlying=? AND a.source_kind=?
            """, (symbol, kind)).fetchone()
            if (
                active and active[0] == expected_source_id
                and int(active[1]) == stat.st_size and int(active[2]) == stat.st_mtime_ns
            ):
                return {
                    "underlying": symbol, "source_kind": kind, "status": "unchanged",
                    "sessions": int(active[3]), "source_id": active[0],
                    **_source_liquidity_coverage(connection, active[0]),
                }

            # Remove abandoned generations from a previous interrupted import.
            abandoned = connection.execute("""
                SELECT source_id FROM options_history_source
                WHERE source_path=? AND status IN ('importing', 'failed')
            """, (str(source_path),)).fetchall()
            for (source_id,) in abandoned:
                connection.execute("DELETE FROM options_history_source WHERE source_id=?", (source_id,))

            source_id = expected_source_id
            connection.execute("""
                INSERT OR REPLACE INTO options_history_source
                    (source_id, underlying, source_kind, source_path, source_sha256,
                     source_size, source_mtime_ns, status, session_count, imported_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?, 'importing', 0, ?)
            """, (
                source_id, symbol, kind, str(source_path), stat.st_size,
                stat.st_mtime_ns, datetime.now(timezone.utc).isoformat(),
            ))
            connection.commit()

        digest = hashlib.sha256()
        sessions = 0
        seen_snapshots: set[tuple[str, str]] = set()
        previous_session = ""
        snapshot_insert = """
            INSERT INTO options_history_snapshot
                (source_id, underlying, session_date, as_of, phase, feed, spot,
                 underlying_close, regime, volatility_state, events_json,
                 equity_signals_json, event_recommendations_json, dividends_json,
                 iv_surface_json, expirations_json, row_sha256, equity_volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, session_date, phase) DO UPDATE SET
                as_of=excluded.as_of, feed=excluded.feed, spot=excluded.spot,
                underlying_close=excluded.underlying_close, regime=excluded.regime,
                equity_volume=excluded.equity_volume,
                volatility_state=excluded.volatility_state,
                events_json=excluded.events_json,
                equity_signals_json=excluded.equity_signals_json,
                event_recommendations_json=excluded.event_recommendations_json,
                dividends_json=excluded.dividends_json,
                iv_surface_json=excluded.iv_surface_json,
                expirations_json=excluded.expirations_json,
                row_sha256=excluded.row_sha256
        """
        quote_insert = """
            INSERT INTO options_history_contract
                (snapshot_id, contract_symbol, underlying_symbol, expiration_date,
                 right, strike, multiplier, exercise_style, tradable, open_interest,
                 open_interest_date, volume, bid, ask, quote_at, iv, greeks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        batch_since_commit = 0
        connection = self._connect()
        try:
            with source_path.open("rb") as stream:
                for line_number, raw_line in enumerate(stream, 1):
                    digest.update(raw_line)
                    if not raw_line.strip():
                        continue
                    row = json.loads(raw_line)
                    if not isinstance(row, dict):
                        raise ValueError(f"{source_path}:{line_number} is not a JSON object")
                    row_symbol = str(row.get("underlying", "")).strip().upper()
                    if row_symbol != symbol:
                        raise ValueError(f"{source_path}:{line_number} contains underlying {row_symbol!r}")
                    session, as_of = _session_date(row)
                    if previous_session and session < previous_session:
                        raise ValueError(f"{source_path}:{line_number} is not ordered by New York session")
                    previous_session = session
                    phase = str(row.get("snapshot_phase", "daily_close")).lower()
                    if phase not in {"daily_close", "opening"}:
                        phase = "daily_close"
                    snapshot_key = (session, phase)
                    if snapshot_key not in seen_snapshots:
                        seen_snapshots.add(snapshot_key)
                        sessions += 1
                    existing = connection.execute(
                        "SELECT snapshot_id, as_of FROM options_history_snapshot "
                        "WHERE source_id=? AND session_date=? AND phase=?",
                        (source_id, session, phase),
                    ).fetchone()
                    if existing:
                        previous_as_of = datetime.fromisoformat(existing[1].replace("Z", "+00:00"))
                        current_as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                        if previous_as_of.astimezone(timezone.utc) >= current_as_of.astimezone(timezone.utc):
                            continue
                    spot = _finite_or_none(row.get("spot"), field="snapshot spot")
                    if spot is None or spot <= 0:
                        raise ValueError(f"{source_path}:{line_number} needs a positive finite spot")
                    close = _finite_or_none(
                        row.get("underlying_close", row.get("close")),
                        field="underlying close",
                    )
                    contracts = row.get("contracts", [])
                    if contracts is None:
                        contracts = []
                    if not isinstance(contracts, list):
                        raise ValueError(f"{source_path}:{line_number} contracts must be an array")

                    connection.execute("""
                        INSERT INTO options_history_snapshot
                            (source_id, underlying, session_date, as_of, phase, feed, spot,
                             underlying_close, regime, volatility_state, events_json,
                             equity_signals_json, event_recommendations_json, dividends_json,
                             iv_surface_json, expirations_json, row_sha256, equity_volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_id, session_date, phase) DO UPDATE SET
                            as_of=excluded.as_of, feed=excluded.feed, spot=excluded.spot,
                            underlying_close=excluded.underlying_close, regime=excluded.regime,
                            equity_volume=excluded.equity_volume,
                            volatility_state=excluded.volatility_state,
                            events_json=excluded.events_json,
                            equity_signals_json=excluded.equity_signals_json,
                            event_recommendations_json=excluded.event_recommendations_json,
                            dividends_json=excluded.dividends_json,
                            iv_surface_json=excluded.iv_surface_json,
                            expirations_json=excluded.expirations_json,
                            row_sha256=excluded.row_sha256
                    """, (
                        source_id, symbol, session, as_of, phase,
                        str(row.get("feed", kind)), spot, close,
                        str(row.get("regime", "unknown")).lower(),
                        str(row.get("volatility_state", "unknown")).lower(),
                        _json(row.get("events") or []),
                        _json(row.get("equity_signals") or []),
                        _json(row.get("event_recommendations") or []),
                        _json(row.get("dividends") or {}),
                        _json(row.get("iv_surface")) if row.get("iv_surface") is not None else None,
                        _json(row.get("expirations")) if row.get("expirations") is not None else None,
                        hashlib.sha256(raw_line).hexdigest(),
                        _integer_or_none(row.get("equity_volume"), field="equity volume"),
                    ))
                    snapshot_id = connection.execute(
                        "SELECT snapshot_id FROM options_history_snapshot WHERE source_id=? AND session_date=? AND phase=?",
                        (source_id, session, phase),
                    ).fetchone()[0]
                    connection.execute("DELETE FROM options_history_contract WHERE snapshot_id=?", (snapshot_id,))
                    values = []
                    valid_contracts = []
                    for contract in contracts:
                        if not isinstance(contract, dict):
                            continue
                        valid_contracts.append(contract)
                        values.append((snapshot_id, *_contract_values(
                            contract, underlying=symbol, as_of=as_of,
                        )))
                    connection.executemany(quote_insert, values)
                    liquidity = _snapshot_liquidity(valid_contracts, spot=spot, session=session)
                    connection.execute("""
                        INSERT INTO options_history_liquidity
                            (snapshot_id, session_date, option_volume_60d,
                             option_volume_coverage, total_open_interest_60d,
                             open_interest_coverage, median_option_spread, spread_coverage)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(snapshot_id) DO UPDATE SET
                            session_date=excluded.session_date,
                            option_volume_60d=excluded.option_volume_60d,
                            option_volume_coverage=excluded.option_volume_coverage,
                            total_open_interest_60d=excluded.total_open_interest_60d,
                            open_interest_coverage=excluded.open_interest_coverage,
                            median_option_spread=excluded.median_option_spread,
                            spread_coverage=excluded.spread_coverage
                    """, (
                        snapshot_id, session, liquidity["option_volume_60d"],
                        liquidity["option_volume_coverage"], liquidity["total_open_interest_60d"],
                        liquidity["open_interest_coverage"], liquidity["median_option_spread"],
                        liquidity["spread_coverage"],
                    ))
                    batch_since_commit += 1
                    if batch_since_commit >= _BATCH_SNAPSHOTS:
                        connection.commit()
                        batch_since_commit = 0
                connection.commit()
            final_stat = source_path.stat()
            if (final_stat.st_size, final_stat.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise RuntimeError(f"source changed while importing: {source_path}")
            file_hash = digest.hexdigest()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                UPDATE options_history_source SET source_sha256=?, status='complete', session_count=?
                WHERE source_id=?
            """, (file_hash, sessions, source_id))
            old = connection.execute("""
                SELECT source_id FROM options_history_active_source
                WHERE underlying=? AND source_kind=?
            """, (symbol, kind)).fetchone()
            connection.execute("""
                INSERT INTO options_history_active_source(underlying, source_kind, source_id)
                VALUES (?, ?, ?)
                ON CONFLICT(underlying, source_kind) DO UPDATE SET source_id=excluded.source_id
            """, (symbol, kind, source_id))
            if old and old[0] != source_id:
                connection.execute("DELETE FROM options_history_source WHERE source_id=?", (old[0],))
            connection.commit()
        except Exception:
            connection.rollback()
            with closing(self._connect()) as mark:
                mark.execute(
                    "UPDATE options_history_source SET status='failed' WHERE source_id=?",
                    (source_id,),
                )
                mark.commit()
            raise
        finally:
            connection.close()
        with closing(self._connect()) as coverage_connection:
            coverage = _source_liquidity_coverage(coverage_connection, source_id)
        return {
            "underlying": symbol, "source_kind": kind, "status": "imported",
            "sessions": sessions, "source_id": source_id, "sha256": file_hash,
            **coverage,
        }

    def import_symbol(self, underlying: str) -> dict:
        symbol = str(underlying).strip().upper()
        lock_path = self.path.parent / f".{self.path.name}.{symbol.lower()}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                result = {"underlying": symbol, "sources": []}
                for kind in ("theta", "alpaca"):
                    source_path = DATA_DIR / "options" / "research" / kind / symbol / "chains.jsonl"
                    result["sources"].append(
                        self.import_jsonl(source_path, underlying=symbol, source_kind=kind)
                    )
                result["status"] = "ready" if self.has_underlying(symbol) else "waiting_for_data"
                return result
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def has_underlying(self, underlying: str, *, phase: str = "daily_close") -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT 1 FROM options_history_snapshot s
                JOIN options_history_active_source a ON a.source_id=s.source_id
                WHERE s.underlying=? AND s.phase=? LIMIT 1
            """, (str(underlying).strip().upper(), phase)).fetchone()
        return row is not None

    def has_model_surface(self, underlying: str, *, phase: str = "daily_close") -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT 1 FROM options_history_snapshot s
                JOIN options_history_active_source a ON a.source_id=s.source_id
                WHERE s.underlying=? AND s.phase=? AND s.iv_surface_json IS NOT NULL
                LIMIT 1
            """, (str(underlying).strip().upper(), phase)).fetchone()
        return row is not None

    def coverage_summary(self, underlying: str) -> dict:
        symbol = str(underlying).strip().upper()
        with closing(self._connect()) as connection:
            source_counts = dict(connection.execute("""
                SELECT a.source_kind, COUNT(DISTINCT s.session_date)
                FROM options_history_active_source a
                JOIN options_history_snapshot s ON s.source_id=a.source_id
                WHERE a.underlying=? AND s.phase='daily_close'
                GROUP BY a.source_kind
            """, (symbol,)).fetchall())
            coverage = connection.execute("""
                WITH ranked AS (
                    SELECT s.session_date, s.equity_volume,
                           l.option_volume_60d, l.option_volume_coverage,
                           l.total_open_interest_60d, l.open_interest_coverage,
                           l.median_option_spread, l.spread_coverage,
                           ROW_NUMBER() OVER (
                               PARTITION BY s.session_date
                               ORDER BY CASE src.source_kind WHEN 'alpaca' THEN 2 WHEN 'theta' THEN 1 ELSE 0 END DESC,
                                        s.as_of DESC
                           ) AS source_rank
                    FROM options_history_snapshot s
                    JOIN options_history_active_source a ON a.source_id=s.source_id
                    JOIN options_history_source src ON src.source_id=s.source_id
                    LEFT JOIN options_history_liquidity l ON l.snapshot_id=s.snapshot_id
                    WHERE s.underlying=? AND s.phase='daily_close'
                )
                SELECT COUNT(*), MIN(session_date), MAX(session_date),
                       SUM(CASE WHEN equity_volume IS NOT NULL THEN 1 ELSE 0 END),
                       SUM(CASE WHEN option_volume_60d IS NOT NULL THEN 1 ELSE 0 END),
                       SUM(CASE WHEN option_volume_60d IS NOT NULL
                                     AND option_volume_coverage >= 0.8 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN total_open_interest_60d IS NOT NULL
                                     AND open_interest_coverage >= 0.8 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN median_option_spread IS NOT NULL
                                     AND spread_coverage >= 0.8 THEN 1 ELSE 0 END)
                FROM ranked WHERE source_rank=1
            """, (symbol,)).fetchone()
        return {
            "underlying": symbol,
            "theta_sessions": int(source_counts.get("theta", 0)),
            "alpaca_sessions": int(source_counts.get("alpaca", 0)),
            "combined_sessions": int(coverage[0] or 0),
            "first_date": coverage[1],
            "last_date": coverage[2],
            "equity_volume_sessions": int(coverage[3] or 0),
            "option_volume_sessions": int(coverage[4] or 0),
            "option_volume_sessions_80pct_coverage": int(coverage[5] or 0),
            "open_interest_sessions_80pct_coverage": int(coverage[6] or 0),
            "option_spread_sessions_80pct_coverage": int(coverage[7] or 0),
            "dataset_path": str(self.path),
        }

    def dataset_fingerprint(self, underlying: str, *, phase: str = "daily_close") -> str:
        symbol = str(underlying).strip().upper()
        digest = hashlib.sha256(SCHEMA_VERSION.encode("ascii"))
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT session_date, as_of, source_kind, row_sha256 FROM (
                    SELECT s.session_date, s.as_of, s.row_sha256, src.source_kind,
                           ROW_NUMBER() OVER (
                               PARTITION BY s.session_date, s.phase
                               ORDER BY CASE src.source_kind WHEN 'alpaca_opening' THEN 4 WHEN 'theta_opening' THEN 3 WHEN 'alpaca' THEN 2 WHEN 'theta' THEN 1 ELSE 0 END DESC,
                                        s.as_of DESC
                           ) AS source_rank
                    FROM options_history_snapshot s
                    JOIN options_history_active_source a ON a.source_id=s.source_id
                    JOIN options_history_source src ON src.source_id=s.source_id
                    WHERE s.underlying=? AND s.phase=?
                ) WHERE source_rank=1 ORDER BY session_date, as_of
            """, (symbol, phase))
            count = 0
            for session, as_of, kind, row_hash in rows:
                digest.update(f"\0{session}\0{as_of}\0{kind}\0{row_hash}".encode("utf-8"))
                count += 1
        if count == 0:
            raise ValueError(f"no {phase} options history for {symbol} in {self.path}")
        return digest.hexdigest()

    def iter_effective_snapshots(
        self, underlying: str, *, phase: str = "daily_close", threads: int = 1,
    ) -> Iterator[dict]:
        """Read an effective chain through DuckDB in bounded result batches."""
        import duckdb

        if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
            raise ValueError("DuckDB threads must be a positive integer")
        symbol = str(underlying).strip().upper()
        path_literal = str(self.path).replace("'", "''")
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(f"SET threads={threads}")
            connection.execute("INSTALL sqlite")
            connection.execute("LOAD sqlite")
            query = f"""
                WITH ranked AS (
                    SELECT s.snapshot_id, s.underlying, s.session_date, s.as_of,
                           s.phase, s.feed, s.spot, s.underlying_close, s.regime,
                           s.volatility_state, s.events_json, s.equity_signals_json,
                           s.event_recommendations_json, s.dividends_json,
                           s.iv_surface_json, s.expirations_json, src.source_kind,
                           ROW_NUMBER() OVER (
                               PARTITION BY s.session_date, s.phase
                               ORDER BY CASE src.source_kind WHEN 'alpaca_opening' THEN 4 WHEN 'theta_opening' THEN 3 WHEN 'alpaca' THEN 2 WHEN 'theta' THEN 1 ELSE 0 END DESC,
                                        s.as_of DESC
                           ) AS source_rank
                    FROM sqlite_scan('{path_literal}', 'options_history_snapshot') AS s
                    JOIN sqlite_scan('{path_literal}', 'options_history_active_source') AS a
                      ON a.source_id=s.source_id
                    JOIN sqlite_scan('{path_literal}', 'options_history_source') AS src
                      ON src.source_id=s.source_id
                    WHERE s.underlying=? AND s.phase=?
                ), effective AS (
                    SELECT * FROM ranked WHERE source_rank=1
                )
                SELECT e.snapshot_id, e.underlying, e.session_date, e.as_of,
                       e.phase, e.feed, e.spot, e.underlying_close, e.regime,
                       e.volatility_state, e.events_json, e.equity_signals_json,
                       e.event_recommendations_json, e.dividends_json,
                       e.iv_surface_json, e.expirations_json,
                       q.contract_symbol, q.underlying_symbol, q.expiration_date,
                       q.right, q.strike, q.multiplier, q.exercise_style, q.tradable,
                       q.open_interest, q.bid, q.ask,
                       q.quote_at, q.iv, q.greeks_json
                FROM effective e
                LEFT JOIN sqlite_scan('{path_literal}', 'options_history_contract') AS q
                  ON q.snapshot_id=e.snapshot_id
                ORDER BY e.session_date, e.as_of, q.expiration_date, q.right, q.strike
            """
            cursor = connection.execute(query, (symbol, phase))
            headers = (
                "underlying", "spot", "underlying_close", "as_of", "feed", "regime",
                "volatility_state", "events", "equity_signals", "event_recommendations",
                "dividends", "iv_surface", "expirations", "contracts",
            )
            current_id = None
            current = None
            source_line = 0
            while batch := cursor.fetchmany(8192):
                for row in batch:
                    snapshot_id = row[0]
                    if snapshot_id != current_id:
                        if current is not None:
                            source_line += 1
                            current["source_line"] = source_line
                            yield {key: current[key] for key in headers if key in current} | {
                                "source_line": source_line,
                            }
                        current_id = snapshot_id
                        current = {
                            "underlying": row[1], "spot": float(row[6]),
                            "underlying_close": row[7], "as_of": row[3],
                            "feed": row[5], "regime": row[8],
                            "volatility_state": row[9],
                            "events": json.loads(row[10] or "[]"),
                            "equity_signals": json.loads(row[11] or "[]"),
                            "event_recommendations": json.loads(row[12] or "[]"),
                            "dividends": json.loads(row[13] or "{}"),
                            "iv_surface": json.loads(row[14]) if row[14] else None,
                            "expirations": json.loads(row[15]) if row[15] else None,
                            "contracts": [],
                        }
                    if row[16] is not None:
                        current["contracts"].append({
                            "symbol": row[16], "underlying_symbol": row[17],
                            "expiration_date": row[18], "type": row[19],
                            "strike_price": float(row[20]), "size": int(row[21]),
                            "style": row[22], "tradable": bool(row[23]),
                            "status": "active" if row[23] else "inactive",
                            "open_interest": row[24],
                            "bid": float(row[25]), "ask": float(row[26]),
                            "quote_at": row[27], "iv": row[28],
                            # Historical parsing reads only IV from Greeks. Most
                            # vendor chain rows store an empty object, so avoid
                            # decoding millions of identical '{}' payloads.
                            "greeks": (
                                json.loads(row[29])
                                if row[28] is None and row[29] not in (None, "", "{}", "null")
                                else None
                            ),
                        })
            if current is not None:
                source_line += 1
                yield {key: current[key] for key in headers if key in current} | {
                    "source_line": source_line,
                }
        finally:
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Theta/Alpaca option-chain archives into SQLite")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--underlying", action="append", help="ticker; repeatable; defaults to the options universe")
    args = parser.parse_args()
    if args.underlying:
        symbols = list(dict.fromkeys(str(value).strip().upper() for value in args.underlying))
    else:
        from options.research.universe import history_underlyings
        symbols = history_underlyings()
    store = OptionsHistoryStore(args.database)
    started = perf_counter()
    for symbol in symbols:
        print(json.dumps(store.import_symbol(symbol), sort_keys=True), flush=True)
    print(json.dumps({
        "database": str(store.path), "symbols": len(symbols),
        "elapsed_seconds": round(perf_counter() - started, 3),
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
