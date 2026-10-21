"""Append-only per-account JSONL paper journal with inter-process locking."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any

from runtime.paths import DATA_DIR


def _json_default(value: Any):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot encode {type(value).__name__} in options journal")


class OptionsJournal:
    def __init__(self, account_id: str, *, root: Path | None = None) -> None:
        if not account_id:
            raise ValueError("account_id is required")
        digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
        self.directory = (root or DATA_DIR / "options" / "accounts") / digest
        self.directory.mkdir(parents=True, exist_ok=True)
        self.account_digest = digest

    def append(self, name: str, payload: dict) -> None:
        if name not in {"orders", "fills", "greeks", "risk_daily", "assignment_events", "decisions", "iv_history", "strategy_greeks"}:
            raise ValueError("unsupported options journal stream")
        path = self.directory / f"{name}.jsonl"
        row = {"logged_at": datetime.now().astimezone().isoformat(), "account_digest": self.account_digest, **payload}
        line = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)
        with path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.write(line + "\n")
                stream.flush()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def latest(self, name: str) -> dict | None:
        if name not in {"orders", "fills", "greeks", "risk_daily", "assignment_events", "decisions", "iv_history", "strategy_greeks"}:
            raise ValueError("unsupported options journal stream")
        path = self.directory / f"{name}.jsonl"
        try:
            with path.open("rb") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                try:
                    stream.seek(0, 2)
                    end = stream.tell()
                    if end == 0:
                        return None
                    size = min(end, 1024 * 1024)
                    stream.seek(-size, 2)
                    lines = stream.read().splitlines()
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            for line in reversed(lines):
                if line.strip():
                    return json.loads(line)
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def latest_matching(self, name: str, field: str, value: str) -> dict | None:
        """Return the newest row matching a field."""
        if name not in {"orders", "fills", "greeks", "risk_daily", "assignment_events", "decisions", "iv_history", "strategy_greeks"}:
            raise ValueError("unsupported options journal stream")
        path = self.directory / f"{name}.jsonl"
        match = None
        try:
            with path.open("r", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                try:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if str(row.get(field)) == str(value):
                            match = row
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            return None
        return match

    def contains(self, name: str, field: str, value: str) -> bool:
        if name not in {"orders", "fills", "greeks", "risk_daily", "assignment_events", "decisions", "iv_history", "strategy_greeks"}:
            raise ValueError("unsupported options journal stream")
        path = self.directory / f"{name}.jsonl"
        try:
            with path.open("r", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                try:
                    for line in stream:
                        try:
                            if str(json.loads(line).get(field)) == str(value):
                                return True
                        except json.JSONDecodeError:
                            continue
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except FileNotFoundError:
            return False
        return False

    def rows(self, name: str) -> list[dict]:
        """Read a journal stream in append order for strategy lifecycle recovery."""
        if name not in {"orders", "fills", "greeks", "risk_daily", "assignment_events", "decisions", "iv_history", "strategy_greeks"}:
            raise ValueError("unsupported options journal stream")
        path = self.directory / f"{name}.jsonl"
        result = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                try:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(row, dict):
                            result.append(row)
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except FileNotFoundError:
            return []
        return result


__all__ = ["OptionsJournal"]
