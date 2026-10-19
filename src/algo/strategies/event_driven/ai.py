"""Validated, short-lived AI recommendations for the live event strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_AI_SIGNAL_FILE = REPOSITORY_ROOT / "data" / "algo" / "event_driven" / "signals.json"
MAX_SNAPSHOT_AGE = timedelta(hours=4)


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
class AIEventRecommendation:
    ticker: str
    action: str
    confidence: float
    reason: str
    run_id: str
    generated_at: datetime


class AIEventSignalBook:
    """Load one atomic analysis snapshot and fail closed on stale or bad data."""

    def __init__(self, path: str | Path = DEFAULT_AI_SIGNAL_FILE, *, now: datetime | None = None) -> None:
        self.path = Path(path)
        self.status = "missing"
        self.error: str | None = None
        self.generated_at: datetime | None = None
        self.expires_at: datetime | None = None
        self.run_id: str | None = None
        self.universe: frozenset[str] = frozenset()
        self.decisions: dict[str, AIEventRecommendation] = {}
        self._load(now or datetime.now(timezone.utc))

    def _load(self, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("AI signal book check time must be timezone-aware")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or isinstance(payload.get("schema_version"), bool)
                or not isinstance(payload.get("schema_version"), int)
                or payload.get("schema_version") != 1
            ):
                raise ValueError("unsupported AI signal snapshot schema")
            if payload.get("strategy") != "event_driven":
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
            decisions: dict[str, AIEventRecommendation] = {}
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
                decisions[ticker] = AIEventRecommendation(
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
            self.decisions = decisions
        except FileNotFoundError:
            self.status = "missing"
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.status = "invalid"
            self.error = f"{type(exc).__name__}: {exc}"

    def recommendation(self, ticker: str, *, universe: set[str]) -> AIEventRecommendation | None:
        symbol = ticker.upper()
        if self.status != "ready" or symbol not in self.universe or symbol not in universe:
            return None
        return self.decisions.get(symbol)

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


def write_ai_event_snapshot(path: str | Path, payload: dict) -> None:
    """Validate and atomically publish an AI run for the Algo scanner."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        candidate = AIEventSignalBook(temporary_name)
        if candidate.status != "ready":
            raise ValueError(candidate.error or f"generated AI signal snapshot is {candidate.status}")
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


__all__ = [
    "AIEventRecommendation", "AIEventSignalBook", "DEFAULT_AI_SIGNAL_FILE",
    "MAX_SNAPSHOT_AGE", "write_ai_event_snapshot",
]
