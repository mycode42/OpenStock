#!/usr/bin/env python3
"""Embed existing AutoResearch artifacts in its SQLite ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from equity_research.artifacts.research_ledger import ResearchLedger
from runtime.paths import DATA_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=str(DATA_DIR / "research" / "algo-auto" / "research.sqlite3"),
    )
    args = parser.parse_args()
    ledger = ResearchLedger(Path(args.database))
    ledger.import_legacy(Path(args.database).resolve().parent)
    result = ledger.backfill_artifact_payloads()
    print(json.dumps({"database": str(Path(args.database).resolve()), **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
