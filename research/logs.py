"""Central research logs. The options run's console output is also appended to options.log."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path


class _Tee:
    def __init__(self, stream, handle) -> None:
        self._stream = stream
        self._handle = handle

    def write(self, text: str) -> int:
        self._stream.write(text)
        self._handle.write(text)
        self._handle.flush()
        return len(text)

    def flush(self) -> None:
        self._stream.flush()
        self._handle.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


@contextmanager
def tee_to(path: Path):
    """Mirror stdout and stderr into ``path`` (appended) while the block runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout = _Tee(original_out, handle)
        sys.stderr = _Tee(original_err, handle)
        try:
            yield
        finally:
            sys.stdout, sys.stderr = original_out, original_err
