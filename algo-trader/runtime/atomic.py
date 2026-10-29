"""Durable, same-directory atomic file writers shared by runtime artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = True,
    default=None,
    allow_nan: bool = True,
    mode: int | None = None,
) -> None:
    """Write JSON through a unique temporary file and atomically replace target.

    Existing file permissions are preserved unless ``mode`` is supplied.
    New files remain private by default, matching ``mkstemp``'s 0600 mode.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(destination.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                payload, stream, indent=indent, sort_keys=sort_keys,
                default=default, allow_nan=allow_nan,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        elif existing_mode is not None:
            os.chmod(temporary, existing_mode)
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["atomic_write_json"]
