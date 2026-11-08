#!/usr/bin/env python3
"""Safely prune stale, rebuildable equity-research bar-cache versions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from market.bar_cache import prune_obsolete


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="authoritative market database; never modified")
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="report candidates without deleting")
    args = parser.parse_args(argv)
    try:
        result = prune_obsolete(args.db, args.cache_root, dry_run=args.dry_run)
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        # Cache maintenance must never suppress research report generation.
        print(f"WARNING: bar-cache cleanup failed safely: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
