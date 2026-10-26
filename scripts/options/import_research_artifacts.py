#!/usr/bin/env python3
"""Backfill existing options reports and run files into the SQLite archive."""

from __future__ import annotations

import json
from pathlib import Path

from options.research.artifact_store import OptionsResearchArtifactStore
from runtime.paths import DATA_DIR


def main() -> int:
    root = DATA_DIR / "options" / "research"
    store = OptionsResearchArtifactStore()
    imported_runs = 0
    for run_dir in sorted((root / "runs").iterdir()):
        if not run_dir.is_dir():
            continue
        report = None
        for name in ("report.json", "run.json"):
            try:
                candidate = json.loads((run_dir / name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict):
                report = candidate
                break
        if report is None:
            continue
        if not report.get("run_id"):
            report["run_id"] = run_dir.name
        store.store_run(run_dir, report)
        imported_runs += 1

    automation_dir = root / "automation"
    imported_documents = 0
    for path in sorted(automation_dir.glob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".jsonl"}:
            continue
        identity = "latest" if path.stem == "latest" else path.stem
        store.store_document(
            document_type=f"automation_{path.suffix.lower().lstrip('.')}",
            identity=identity,
            path=path,
        )
        imported_documents += 1
    print(json.dumps({
        "database": str(store.path),
        "imported_runs": imported_runs,
        "imported_automation_documents": imported_documents,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
