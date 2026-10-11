"""Serializable signal shape used by rule-based strategy modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Signal:
    ticker: str
    action: str
    strategy: str
    reason: str
    strength: float = 0.0
    stop_pct: float | None = None
    strategy_group: str | None = None
    target_weight: float | None = None
    event_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
