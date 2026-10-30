#!/usr/bin/env python3
"""Cap the size of the Algo cron logs by keeping their newest lines.

Each scheduled job writes to its own file under data/algo/logs/. A file over MAX_BYTES keeps its last KEEP_BYTES,
with a marker line where the older text was removed. Cron appends in O_APPEND mode, so a job that writes during
the rewrite continues at the new end of the file.
"""

from __future__ import annotations

from pathlib import Path
import sys

from runtime.paths import ROOT

LOG_DIR = ROOT / "data" / "algo" / "logs"
LEGACY_LOG = ROOT / "data" / "algo" / "cron.log"
MAX_BYTES = 5 * 1024 * 1024
KEEP_BYTES = 1 * 1024 * 1024
MARKER = b"[older lines removed by scripts/rotate_logs.py]\n"


def rotate(path: Path, *, max_bytes: int = MAX_BYTES, keep_bytes: int = KEEP_BYTES) -> bool:
    if not path.is_file() or path.stat().st_size <= max_bytes:
        return False
    with path.open("rb") as stream:
        stream.seek(-keep_bytes, 2)
        tail = stream.read()
    if b"\n" in tail:
        tail = tail[tail.index(b"\n") + 1:]
    path.write_bytes(MARKER + tail)
    return True


def main() -> int:
    paths = sorted(LOG_DIR.glob("*.log")) + ([LEGACY_LOG] if LEGACY_LOG.is_file() else [])
    rotated = [path.name for path in paths if rotate(path)]
    print(f"rotated: {', '.join(rotated) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
