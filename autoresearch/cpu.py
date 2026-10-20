"""CPU capacity visible to the current process."""

from __future__ import annotations

import os


def available_cpu_count() -> int:
    """Return CPUs available under process affinity, with portable fallbacks."""
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if callable(process_cpu_count):
        count = process_cpu_count()
        if count:
            return max(1, count)
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)
