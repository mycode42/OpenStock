"""Validated, short-lived AI recommendations for the live LLM-signal strategy."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3


from runtime.paths import ROOT
from strategies.catalog import canonical_strategy_id

REPOSITORY_ROOT = ROOT
DEFAULT_AI_SIGNAL_DB = REPOSITORY_ROOT / "data" / "algo" / "llm_driven" / "signals.sqlite3"
MAX_SNAPSHOT_AGE = timedelta(hours=4)
# Every published run is kept as a row, so the history stays queryable. Only the newest is read.
SNAPSHOT_TABLE = "llm_signal_snapshot"
SNAPSHOT_SCHEMA = f"""CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""
SNAPSHOT_RETENTION = 200


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class LLMRecommendation:
    ticker: str
    action: str
    confidence: float
    reason: str
    run_id: str
    generated_at: datetime


class LLMSignalBook:
    """Load one atomic analysis snapshot and fail closed on stale or bad data."""

    def __init__(self, source: str | Path = DEFAULT_AI_SIGNAL_DB, *, now: datetime | None = None) -> None:
        self.source = Path(source)
        self._reset()
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("AI signal book check time must be timezone-aware")
        try:
            payload = read_latest_llm_signal_payload(self.source)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.status = "invalid"
            self.error = f"{type(exc).__name__}: {exc}"
            return
        if payload is not None:
            self._load(payload, now)

    @classmethod
    def from_payload(cls, payload: dict, *, now: datetime | None = None) -> "LLMSignalBook":
        """Validate a snapshot that is not stored yet, as the runner does before it publishes one."""
        book = cls.__new__(cls)
        book.source = None
        book._reset()
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("AI signal book check time must be timezone-aware")
        book._load(payload, now)
        return book

    def _reset(self) -> None:
        self.status = "missing"
        self.owned_tickers: frozenset[str] = frozenset()
        self.error: str | None = None
        self.generated_at: datetime | None = None
        self.expires_at: datetime | None = None
        self.run_id: str | None = None
        self.universe: frozenset[str] = frozenset()
        self.decisions: dict[str, LLMRecommendation] = {}

    def _load(self, payload: object, now: datetime) -> None:
        try:
            if (
                not isinstance(payload, dict)
                or isinstance(payload.get("schema_version"), bool)
                or not isinstance(payload.get("schema_version"), int)
                or payload.get("schema_version") != 1
            ):
                raise ValueError("unsupported AI signal snapshot schema")
            if canonical_strategy_id(str(payload.get("strategy") or "")) != "swing_llm_driven":
                raise ValueError("AI signal snapshot has the wrong strategy")
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("AI signal snapshot has no run_id")
            generated = _timestamp(payload.get("generated_at"), "generated_at")
            expires = _timestamp(payload.get("expires_at"), "expires_at")
            checked = now.astimezone(timezone.utc)
            if generated > checked + timedelta(minutes=5):
                raise ValueError("AI signal snapshot is future-dated")
            if expires <= generated or expires - generated > MAX_SNAPSHOT_AGE:
                raise ValueError("AI signal snapshot has an invalid expiry")
            if checked > expires or checked - generated > MAX_SNAPSHOT_AGE:
                self.status = "expired"
                return

            raw_universe = payload.get("universe")
            if not isinstance(raw_universe, list) or not raw_universe:
                raise ValueError("AI signal snapshot has no universe")
            universe = set()
            for ticker in raw_universe:
                if not isinstance(ticker, str) or not ticker or ticker != ticker.upper():
                    raise ValueError("AI signal snapshot has an invalid universe ticker")
                universe.add(ticker)

            raw_decisions = payload.get("decisions")
            if not isinstance(raw_decisions, dict):
                raise ValueError("AI signal snapshot decisions must be an object")
            decisions: dict[str, LLMRecommendation] = {}
            for raw_ticker, raw_decision in raw_decisions.items():
                ticker = str(raw_ticker)
                if ticker != ticker.upper() or ticker not in universe:
                    raise ValueError(f"AI signal snapshot contains an out-of-universe ticker: {ticker}")
                if not isinstance(raw_decision, dict):
                    raise ValueError(f"AI decision for {ticker} must be an object")
                action = raw_decision.get("action")
                if action not in {"buy", "sell", "watch"}:
                    raise ValueError(f"AI decision for {ticker} has an unsupported action")
                confidence = raw_decision.get("confidence")
                if (
                    isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or confidence < 0
                    or confidence > 100
                    or not math.isfinite(confidence)
                ):
                    raise ValueError(f"AI decision for {ticker} has invalid confidence")
                reason = raw_decision.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError(f"AI decision for {ticker} has no evidence-based reason")
                decisions[ticker] = LLMRecommendation(
                    ticker=ticker,
                    action=action,
                    confidence=float(confidence),
                    reason=reason.strip()[:1200],
                    run_id=run_id,
                    generated_at=generated,
                )

            self.status = "ready"
            self.generated_at = generated
            self.expires_at = expires
            self.run_id = run_id
            self.universe = frozenset(universe)
            self.owned_tickers = frozenset(str(item).upper() for item in (payload.get("owned_tickers") or []))
            self.decisions = decisions
        except (TypeError, ValueError) as exc:
            self.status = "invalid"
            self.error = f"{type(exc).__name__}: {exc}"

    def recommendation(self, ticker: str, *, universe: set[str]) -> LLMRecommendation | None:
        symbol = ticker.upper()
        if self.status != "ready" or symbol not in self.universe:
            return None
        decision = self.decisions.get(symbol)
        # A held ticker outside the live universe may only be sold, never added to.
        if symbol not in universe and (symbol not in self.owned_tickers or decision is None or decision.action != "sell"):
            return None
        return decision

    def diagnostics(self) -> dict:
        actionable = sum(
            decision.action in {"buy", "sell"}
            for decision in self.decisions.values()
        )
        return {
            "status": self.status,
            "run_id": self.run_id,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "universe_count": len(self.universe),
            "decision_count": len(self.decisions),
            "actionable_count": actionable,
            "error": self.error,
        }


def read_latest_llm_signal_payload(path: str | Path) -> dict | None:
    """The newest published snapshot, or None when none has been published yet."""
    database = Path(path)
    if not database.exists():
        return None
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)) as connection:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SNAPSHOT_TABLE,)).fetchone()
        if table is None:
            return None
        row = connection.execute(f"SELECT payload_json FROM {SNAPSHOT_TABLE} ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else json.loads(row[0])


def publish_llm_signal_snapshot(path: str | Path, payload: dict, *, now: datetime | None = None) -> None:
    """Validate an AI run and append it as the newest snapshot. A rejected run is never stored."""
    book = LLMSignalBook.from_payload(payload, now=now)
    if book.status != "ready":
        raise ValueError(book.error or f"generated AI signal snapshot is {book.status}")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with closing(sqlite3.connect(destination, timeout=30)) as connection, connection:
        connection.execute(SNAPSHOT_SCHEMA)
        connection.execute(
            f"INSERT INTO {SNAPSHOT_TABLE} (run_id, generated_at, expires_at, payload_json) VALUES (?, ?, ?, ?)",
            (payload["run_id"], payload["generated_at"], payload["expires_at"], text),
        )
        connection.execute(
            f"DELETE FROM {SNAPSHOT_TABLE} WHERE id NOT IN (SELECT id FROM {SNAPSHOT_TABLE} ORDER BY id DESC LIMIT ?)",
            (SNAPSHOT_RETENTION,),
        )


def main(argv: list[str] | None = None) -> int:
    """Print the newest snapshot with its freshness, for the Algo briefing and silent-run prompts."""
    parser = argparse.ArgumentParser(description="Print the newest AI signal snapshot as JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_AI_SIGNAL_DB)
    args = parser.parse_args(argv)
    checked = datetime.now(timezone.utc)
    book = LLMSignalBook(args.db, now=checked)
    print(json.dumps({
        "now_utc": checked.isoformat(),
        "status": book.status,
        "fresh": book.status == "ready",
        "expires_at": book.expires_at.isoformat() if book.expires_at else None,
        "error": book.error,
        "snapshot": read_latest_llm_signal_payload(args.db),
    }, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "LLMRecommendation", "LLMSignalBook", "DEFAULT_AI_SIGNAL_DB",
    "MAX_SNAPSHOT_AGE", "publish_llm_signal_snapshot", "read_latest_llm_signal_payload",
]
