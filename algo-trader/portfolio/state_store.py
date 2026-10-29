"""Transactional Algo coordinator and per-strategy state storage.

The SQLite database is the only persisted state. A daily online backup guards
against corruption; restore by copying a backup over the database while the
daemon is stopped.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from portfolio.protection.software import ensure_protection_schema
from strategies.catalog import STRATEGY_IDS, canonical_strategy_id
from strategies.state_machine import ACTIVE_PHASES, PHASES


SCHEMA_VERSION = 1


def _canonicalize_strategy_values(value):
    if isinstance(value, dict):
        return {
            key: _canonicalize_strategy_values(canonical_strategy_id(item) if isinstance(item, str) else item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_strategy_values(item) for item in value]
    if isinstance(value, str):
        return canonical_strategy_id(value)
    return value


def _canonicalize_strategy_keyed_map(values: dict) -> dict:
    normalized = {}
    for key, value in values.items():
        strategy = canonical_strategy_id(key) if isinstance(key, str) else key
        if strategy in normalized and normalized[strategy] != value:
            raise StateCorruptionError(f"Conflicting legacy and canonical strategy state for {strategy}")
        normalized[strategy] = value
    return normalized


def _canonicalize_portfolio_strategy_ids(portfolio: dict) -> dict:
    """Translate persisted pre-prefix IDs without changing position ownership."""
    normalized = _canonicalize_strategy_values(portfolio)
    states = portfolio.get("strategy_states") or {}
    canonical_states = {}
    for old_strategy, by_symbol in states.items():
        strategy = canonical_strategy_id(old_strategy)
        target = canonical_states.setdefault(strategy, {})
        for ticker, state in by_symbol.items():
            if ticker in target:
                raise StateCorruptionError(
                    f"Multiple stored strategy states normalize to {strategy}/{ticker}"
                )
            target[ticker] = state
            target[ticker]["strategy"] = strategy
            if target[ticker].get("owner"):
                target[ticker]["owner"] = canonical_strategy_id(target[ticker]["owner"])
    normalized["strategy_states"] = canonical_states
    normalized["position_owners"] = {
        ticker: canonical_strategy_id(strategy)
        for ticker, strategy in (portfolio.get("position_owners") or {}).items()
    }
    regime = normalized.get("market_regime")
    if isinstance(regime, dict):
        for key in ("strategy_risk_multipliers", "strategy_weights"):
            if isinstance(regime.get(key), dict):
                regime[key] = _canonicalize_strategy_keyed_map(regime[key])
    return normalized


class StateStoreError(RuntimeError):
    """The coordinator cannot safely read or persist its state."""


class StateCorruptionError(StateStoreError):
    """Existing state is malformed or internally inconsistent."""


def empty_portfolio() -> dict:
    return {
        "account_id": None,
        "session_date": None,
        "universe": None,
        "active_strategies": [],
        "market_regime": {"regime": "unknown", "reason": "awaiting first scan", "active": []},
        "position_owners": {},
        "strategy_states": {},
        "last_scan_at": None,
        "last_signals": [],
        "last_execution": None,
        "runtime_mode": "algo",
        "entries_paused": True,
        "entry_pause_reasons": ["startup broker reconciliation is pending"],
        "startup_reconciliation": {"status": "pending"},
        "unowned_positions": [],
    }


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)


def validate_portfolio(portfolio: dict) -> None:
    """Reject state that could silently change the owner of a live position."""
    if not isinstance(portfolio, dict):
        raise StateCorruptionError("Algo portfolio state is not an object")
    if portfolio.get("runtime_mode", "algo") != "algo":
        raise StateCorruptionError("Algo portfolio has a non-Algo runtime mode")
    account_id = portfolio.get("account_id")
    if account_id is not None and (not isinstance(account_id, str) or not account_id):
        raise StateCorruptionError("Algo state has an invalid broker account ID")
    owners = portfolio.get("position_owners")
    states = portfolio.get("strategy_states")
    if not isinstance(owners, dict) or not isinstance(states, dict):
        raise StateCorruptionError("Algo owner map or strategy states are missing")
    if not isinstance(portfolio.get("active_strategies", []), list):
        raise StateCorruptionError("Algo active_strategies is not a list")
    if not isinstance(portfolio.get("market_regime", {}), dict):
        raise StateCorruptionError("Algo market_regime is not an object")
    if not isinstance(portfolio.get("last_signals", []), list):
        raise StateCorruptionError("Algo last_signals is not a list")
    if portfolio.get("last_execution") is not None and not isinstance(portfolio["last_execution"], dict):
        raise StateCorruptionError("Algo last_execution is not an object")
    if portfolio.get("universe") is not None and not isinstance(portfolio["universe"], dict):
        raise StateCorruptionError("Algo universe is not an object")
    incident = portfolio.get("execution_incident")
    if incident is not None:
        if (
            not isinstance(incident, dict)
            or incident.get("status") not in {"open", "resolved"}
            or not isinstance(incident.get("events"), dict)
            or (incident.get("status") == "open" and not incident["events"])
        ):
            raise StateCorruptionError("Malformed Algo execution incident")
        for key, event in incident["events"].items():
            if (
                not isinstance(key, str) or not isinstance(event, dict)
                or event.get("kind") not in {"exit_submission", "oco_repair"}
                or not isinstance(event.get("ticker"), str)
                or key != f"{event['kind']}:{event['ticker']}"
            ):
                raise StateCorruptionError("Malformed Algo execution incident event")
    recovery_status = portfolio.get("protection_recovery_status")
    if recovery_status is not None and (
        not isinstance(recovery_status, dict)
        or not isinstance(recovery_status.get("status"), str)
        or not isinstance(recovery_status.get("errors", []), list)
    ):
        raise StateCorruptionError("Malformed Algo protection recovery status")

    seen_active: dict[str, str] = {}
    for strategy, by_symbol in states.items():
        if strategy not in STRATEGY_IDS or not isinstance(by_symbol, dict):
            raise StateCorruptionError(f"Unknown or malformed Algo strategy: {strategy}")
        for ticker, state in by_symbol.items():
            if not isinstance(ticker, str) or not ticker or ticker != ticker.upper():
                raise StateCorruptionError(f"Malformed strategy ticker: {ticker!r}")
            if not isinstance(state, dict) or state.get("phase", "flat") not in PHASES:
                raise StateCorruptionError(f"Malformed {strategy}/{ticker} lifecycle state")
            if state.get("strategy", strategy) != strategy or state.get("ticker", ticker) != ticker:
                raise StateCorruptionError(f"Mismatched {strategy}/{ticker} state identity")
            if state.get("phase") in ACTIVE_PHASES:
                prior = seen_active.setdefault(ticker, strategy)
                if prior != strategy:
                    raise StateCorruptionError(f"Multiple active Algo owners for {ticker}")
                if owners.get(ticker) != strategy:
                    raise StateCorruptionError(f"Active Algo state has no matching owner: {strategy}/{ticker}")
            if state.get("entry_order_id") is not None and not isinstance(state["entry_order_id"], str):
                raise StateCorruptionError(f"Malformed entry order ID for {strategy}/{ticker}")
            recovery = state.get("protection_recovery")
            if recovery is not None and (
                not isinstance(recovery, dict)
                or not isinstance(recovery.get("client_order_id"), str)
                or not recovery["client_order_id"]
                or len(recovery["client_order_id"]) > 48
                or (recovery.get("order_id") is not None and not isinstance(recovery["order_id"], str))
            ):
                raise StateCorruptionError(f"Malformed OCO recovery decision for {strategy}/{ticker}")
    for ticker, strategy in owners.items():
        if not isinstance(ticker, str) or not ticker or ticker != ticker.upper():
            raise StateCorruptionError(f"Malformed owned ticker: {ticker!r}")
        if strategy not in STRATEGY_IDS:
            raise StateCorruptionError(f"Unknown Algo position owner for {ticker}: {strategy!r}")
        state = (states.get(strategy) or {}).get(ticker)
        if not state or state.get("phase") not in ACTIVE_PHASES:
            raise StateCorruptionError(f"Owned Algo position lacks an active state: {strategy}/{ticker}")
    try:
        _json(portfolio)
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError("Algo portfolio is not valid finite JSON") from exc


class AlgoStateStore:
    """One-writer SQLite store for the Algo coordinator."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(path or self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE coordinator (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                market_state TEXT NOT NULL,
                state_json TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE strategy_state (
                strategy TEXT NOT NULL,
                ticker TEXT NOT NULL,
                state_json TEXT NOT NULL,
                PRIMARY KEY (strategy, ticker)
            );
            CREATE TABLE position_owner (
                ticker TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                FOREIGN KEY (strategy, ticker) REFERENCES strategy_state (strategy, ticker)
            );
            CREATE TABLE entry_provenance (
                order_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                client_order_id TEXT,
                source TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE execution_lease (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                token_digest TEXT NOT NULL,
                parent_pid INTEGER NOT NULL,
                activated_at TEXT NOT NULL
            );
            CREATE TRIGGER entry_provenance_immutable_update
                BEFORE UPDATE ON entry_provenance
                BEGIN SELECT RAISE(ABORT, 'entry provenance is immutable'); END;
            CREATE TRIGGER entry_provenance_immutable_delete
                BEFORE DELETE ON entry_provenance
                BEGIN SELECT RAISE(ABORT, 'entry provenance is immutable'); END;
            PRAGMA user_version=1;
        """)
        ensure_protection_schema(connection)

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise StateCorruptionError(f"Unsupported Algo state schema version: {version}")
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise StateCorruptionError(f"Algo SQLite integrity check failed: {integrity}")

    def load(self) -> tuple[str, dict]:
        """Read the database, creating an empty one on first start."""
        if self.db_path.exists():
            return self.read()
        market_state, portfolio = "UNKNOWN", empty_portfolio()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.db_path.stem}.migrate-", suffix=".sqlite3", dir=self.db_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with closing(self._connect(temporary_path)) as connection:
                self._create_schema(connection)
                self._write_rows(connection, market_state, portfolio)
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.db_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return self.read()

    def read(self) -> tuple[str, dict]:
        """Read authoritative state without initializing or modifying it."""
        if not self.db_path.is_file():
            raise StateStoreError(f"Algo SQLite state is missing: {self.db_path}")
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                row = connection.execute(
                    "SELECT market_state, state_json FROM coordinator WHERE id=1"
                ).fetchone()
                if row is None:
                    raise StateCorruptionError("Algo SQLite coordinator row is missing")
                portfolio = json.loads(row["state_json"])
                owners = {
                    item["ticker"]: item["strategy"]
                    for item in connection.execute("SELECT ticker, strategy FROM position_owner")
                }
                states: dict[str, dict] = {}
                for item in connection.execute("SELECT strategy, ticker, state_json FROM strategy_state"):
                    states.setdefault(item["strategy"], {})[item["ticker"]] = json.loads(item["state_json"])
                portfolio["position_owners"] = owners
                portfolio["strategy_states"] = states
                portfolio = _canonicalize_portfolio_strategy_ids(portfolio)
                validate_portfolio(portfolio)
                # Validate immutable order provenance against the normalized
                # strategy IDs exposed by this read. Persisted SQLite rows may
                # still use pre-prefix IDs until the next successful save.
                for strategy, by_symbol in portfolio["strategy_states"].items():
                    for ticker, state in by_symbol.items():
                        order_id = state.get("entry_order_id")
                        if not order_id:
                            continue
                        proven = connection.execute(
                            "SELECT ticker, strategy FROM entry_provenance WHERE order_id=?", (order_id,)
                        ).fetchone()
                        if proven is None or (
                            proven["ticker"], canonical_strategy_id(proven["strategy"])
                        ) != (ticker, strategy):
                            raise StateCorruptionError(f"Missing immutable provenance for {strategy}/{ticker}")
                return row["market_state"], portfolio
        except (sqlite3.DatabaseError, json.JSONDecodeError, TypeError) as exc:
            raise StateCorruptionError(f"Cannot read Algo SQLite state: {exc}") from exc

    @staticmethod
    def _write_rows(connection: sqlite3.Connection, market_state: str, portfolio: dict) -> None:
        validate_portfolio(portfolio)
        if not isinstance(market_state, str) or not market_state:
            raise StateCorruptionError("Algo market state must be a nonempty string")
        coordinator = {key: value for key, value in portfolio.items() if key not in {"position_owners", "strategy_states"}}
        connection.execute("BEGIN IMMEDIATE")
        try:
            old = connection.execute("SELECT revision FROM coordinator WHERE id=1").fetchone()
            revision = int(old["revision"]) + 1 if old else 1
            connection.execute(
                "INSERT OR REPLACE INTO coordinator (id, market_state, state_json, revision, updated_at) VALUES (1, ?, ?, ?, ?)",
                (market_state, _json(coordinator), revision, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute("DELETE FROM position_owner")
            connection.execute("DELETE FROM strategy_state")
            for strategy, by_symbol in portfolio["strategy_states"].items():
                for ticker, state in by_symbol.items():
                    connection.execute(
                        "INSERT INTO strategy_state (strategy, ticker, state_json) VALUES (?, ?, ?)",
                        (strategy, ticker, _json(state)),
                    )
                    order_id = state.get("entry_order_id")
                    if order_id:
                        client_id = state.get("entry_client_order_id")
                        source = str(state.get("entry_source") or "coordinator")
                        existing = connection.execute(
                            "SELECT ticker, strategy, client_order_id, source FROM entry_provenance WHERE order_id=?",
                            (order_id,),
                        ).fetchone()
                        if existing is None:
                            connection.execute(
                                "INSERT INTO entry_provenance (order_id, ticker, strategy, client_order_id, source, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (order_id, ticker, strategy, client_id, source, datetime.now(timezone.utc).isoformat()),
                            )
                        elif (
                            existing["ticker"], canonical_strategy_id(existing["strategy"])
                        ) != (ticker, strategy):
                            raise StateCorruptionError(f"Conflicting immutable Algo provenance for order {order_id}")
            for ticker, strategy in portfolio["position_owners"].items():
                connection.execute(
                    "INSERT INTO position_owner (ticker, strategy) VALUES (?, ?)", (ticker, strategy)
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def save(self, market_state: str, portfolio: dict) -> None:
        """Commit all strategy and owner changes atomically."""
        if not self.db_path.exists():
            raise StateStoreError("Algo SQLite state has not been initialized")
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                self._write_rows(connection, market_state, portfolio)
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot persist Algo SQLite state: {exc}") from exc

    def sync_protection(self, rows: list[dict]) -> None:
        """Upsert protection levels and drop rows for positions no longer held.

        Exit-in-progress markers are not part of ``rows`` and are preserved.
        """
        columns = (
            "ticker", "strategy", "side", "quantity", "entry_price", "entry_order_id",
            "initial_stop", "target", "hard_stop_pct", "trailing_stop_pct",
            "trailing_activation_pct", "trailing_stop", "best_price", "phase",
            "last_evaluated_at", "updated_at",
        )
        assignments = ", ".join(f"{name}=excluded.{name}" for name in columns[1:])
        placeholders = ", ".join("?" for _ in columns)
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                ensure_protection_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    tickers = [row["ticker"] for row in rows]
                    for row in rows:
                        connection.execute(
                            f"INSERT INTO position_protection ({', '.join(columns)}) VALUES ({placeholders}) "
                            f"ON CONFLICT(ticker) DO UPDATE SET {assignments}",
                            tuple(row.get(name) for name in columns),
                        )
                    if tickers:
                        marks = ", ".join("?" for _ in tickers)
                        connection.execute(f"DELETE FROM position_protection WHERE ticker NOT IN ({marks})", tickers)
                    else:
                        connection.execute("DELETE FROM position_protection")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot persist protection rows: {exc}") from exc

    def upsert_protection(self, row: dict) -> None:
        """Insert or replace one protection row without touching other positions."""
        columns = (
            "ticker", "strategy", "side", "quantity", "entry_price", "entry_order_id",
            "initial_stop", "target", "hard_stop_pct", "trailing_stop_pct",
            "trailing_activation_pct", "trailing_stop", "best_price", "phase",
            "last_evaluated_at", "updated_at",
        )
        assignments = ", ".join(f"{name}=excluded.{name}" for name in columns[1:])
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                ensure_protection_schema(connection)
                connection.execute(
                    f"INSERT INTO position_protection ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                    f"ON CONFLICT(ticker) DO UPDATE SET {assignments}",
                    tuple(row.get(name) for name in columns),
                )
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot persist protection row: {exc}") from exc

    def read_protection(self) -> dict[str, dict]:
        """Return protection rows keyed by ticker; empty when none exist."""
        if not self.db_path.is_file():
            raise StateStoreError(f"Algo SQLite state is missing: {self.db_path}")
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                ensure_protection_schema(connection)
                return {
                    row["ticker"]: dict(row)
                    for row in connection.execute("SELECT * FROM position_protection")
                }
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot read protection rows: {exc}") from exc

    def set_exit_marker(self, ticker: str, client_order_id: str, order_id: str | None, submitted_at: str) -> None:
        """Record an in-progress exit so later scans do not submit a duplicate."""
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                ensure_protection_schema(connection)
                connection.execute(
                    "UPDATE position_protection SET phase='exit_pending', exit_client_order_id=?, "
                    "exit_order_id=?, exit_submitted_at=? WHERE ticker=?",
                    (client_order_id, order_id, submitted_at, ticker),
                )
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot record protection exit marker: {exc}") from exc

    def clear_exit_marker(self, ticker: str) -> None:
        """Release an exit marker once its order is terminal and the position is still held."""
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                ensure_protection_schema(connection)
                connection.execute(
                    "UPDATE position_protection SET phase='open', exit_client_order_id=NULL, "
                    "exit_order_id=NULL, exit_submitted_at=NULL WHERE ticker=?",
                    (ticker,),
                )
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot clear protection exit marker: {exc}") from exc

    def write_heartbeat(self, scanned_at: datetime, session: str | None, pid: int) -> None:
        """Record that the daemon completed a scan."""
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                ensure_protection_schema(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO daemon_heartbeat (id, scanned_at, session, pid) VALUES (1, ?, ?, ?)",
                    (scanned_at.isoformat(), session, int(pid)),
                )
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot write daemon heartbeat: {exc}") from exc

    def read_heartbeat(self) -> dict | None:
        """Return the last scan heartbeat, or None when the daemon never wrote one."""
        if not self.db_path.is_file():
            raise StateStoreError(f"Algo SQLite state is missing: {self.db_path}")
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                ensure_protection_schema(connection)
                row = connection.execute("SELECT scanned_at, session, pid FROM daemon_heartbeat WHERE id=1").fetchone()
                return dict(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot read daemon heartbeat: {exc}") from exc

    def backup_daily(self, backup_dir: Path, day: date, keep: int = 30) -> Path | None:
        """Write one consistent copy per day via SQLite's online backup API.

        Returns the backup path, or None when today's copy already exists.
        Only the newest ``keep`` backups are retained.
        """
        target = Path(backup_dir) / f"algo_state-{day.isoformat()}.sqlite3"
        if target.exists():
            return None
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".algo_state-", suffix=".tmp", dir=backup_dir,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(temporary_path)) as copy:
                source.backup(copy)
            with closing(sqlite3.connect(temporary_path)) as copy:
                self._check_schema(copy)
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        backups = sorted(backup_dir.glob("algo_state-*.sqlite3"))
        for stale in backups[:-keep]:
            stale.unlink(missing_ok=True)
        return target

    def activate_coordinator(self, token: str, parent_pid: int) -> None:
        """Bind live Algo execution to this daemon process and one run token."""
        if not token or parent_pid <= 0:
            raise ValueError("Coordinator lease requires a token and process ID")
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR REPLACE INTO execution_lease (id, token_digest, parent_pid, activated_at) VALUES (1, ?, ?, ?)",
                    (digest, int(parent_pid), datetime.now(timezone.utc).isoformat()),
                )
                connection.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot activate Algo coordinator lease: {exc}") from exc

    def verify_coordinator_token(self, token: str | None, parent_pid: int) -> bool:
        """Validate a subprocess capability without creating state or orders."""
        if not token or parent_pid <= 0 or not self.db_path.is_file():
            return False
        try:
            with closing(self._connect()) as connection:
                self._check_schema(connection)
                row = connection.execute(
                    "SELECT token_digest, parent_pid FROM execution_lease WHERE id=1"
                ).fetchone()
                return bool(
                    row is not None
                    and int(row["parent_pid"]) == int(parent_pid)
                    and hmac.compare_digest(row["token_digest"], hashlib.sha256(token.encode()).hexdigest())
                )
        except (sqlite3.DatabaseError, StateStoreError, ValueError, TypeError):
            return False

    def clear_coordinator(self, token: str, parent_pid: int) -> None:
        """Retire only the lease issued to this daemon invocation."""
        if not self.verify_coordinator_token(token, parent_pid):
            return
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM execution_lease WHERE id=1 AND token_digest=? AND parent_pid=?",
                (digest, int(parent_pid)),
            )
            connection.execute("COMMIT")


__all__ = ["AlgoStateStore", "StateStoreError", "StateCorruptionError", "empty_portfolio", "validate_portfolio"]
