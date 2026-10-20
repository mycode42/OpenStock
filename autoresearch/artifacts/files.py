"""Atomic report writes and single-run locking."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
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
    atomic_write_json(path, payload, default=str)
