"""Atomic report writes and single-run locking."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import math
from pathlib import Path

from runtime.atomic import atomic_write_json

@contextmanager
def _research_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("An Algo AutoResearch run is already active") from exc
        yield

def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, _json_safe(payload), default=str, allow_nan=False)


def _json_safe(value):
    """Keep non-finite research metrics representable in strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return None
        return "inf" if value > 0 else "-inf"
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
