"""Compressed SQLite persistence for options research runs and outputs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib

from options.research.reporting import report_for_history
from runtime.paths import DATA_DIR


DEFAULT_DATABASE = DATA_DIR / "options" / "research" / "research.sqlite3"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


class OptionsResearchArtifactStore:
    """Store options replay history and selected supporting artifacts in SQLite."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS options_research_run (
                    run_id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    report_sha256 TEXT,
                    report_json_zlib BLOB,
                    report_bytes INTEGER,
                    run_dir TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS options_research_run_latest_idx
                    ON options_research_run(created_at DESC, strategy, underlying);
                CREATE TABLE IF NOT EXISTS options_research_artifact (
                    run_id TEXT NOT NULL REFERENCES options_research_run(run_id) ON DELETE CASCADE,
                    artifact_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    payload_zlib BLOB NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, artifact_path)
                );
                CREATE TABLE IF NOT EXISTS options_research_document (
                    document_type TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    payload_zlib BLOB NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(document_type, identity)
                );
                DROP TABLE IF EXISTS options_research_candidate;
                DROP TABLE IF EXISTS options_champion_decision;
                DROP TABLE IF EXISTS options_champion;
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=60)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def store_run(self, run_dir: str | Path, report: dict | None = None) -> dict:
        directory = Path(run_dir).resolve()
        if report is None:
            try:
                report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = {}
        if not isinstance(report, dict):
            raise ValueError("options research report must be a JSON object")
        report = report_for_history(report)
        run_id = str(report.get("run_id") or directory.name)
        strategy = str(report.get("strategy") or "unknown")
        underlying = str(report.get("underlying") or "unknown").upper()
        created_at = str(report.get("created_at") or datetime.now(timezone.utc).isoformat())
        report_bytes = _json(report).encode("utf-8")
        report_sha = hashlib.sha256(report_bytes).hexdigest()
        artifacts = []
        for path in sorted(directory.rglob("*")):
            if (
                not path.is_file()
                or path.name.endswith(".lock")
                or path.name in {"report.json", "candidate_reports.json"}
            ):
                continue
            raw = path.read_bytes()
            relative = str(path.relative_to(directory))
            media_type = {
                ".json": "application/json", ".md": "text/markdown",
                ".csv": "text/csv", ".txt": "text/plain",
            }.get(path.suffix.lower(), "application/octet-stream")
            artifacts.append((
                relative, media_type, hashlib.sha256(raw).hexdigest(), len(raw),
                zlib.compress(raw, level=6),
            ))

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO options_research_run
                    (run_id, strategy, underlying, status, created_at,
                     report_sha256, report_json_zlib, report_bytes, run_dir)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    strategy=excluded.strategy, underlying=excluded.underlying,
                    status=excluded.status, created_at=excluded.created_at,
                    report_sha256=excluded.report_sha256,
                    report_json_zlib=excluded.report_json_zlib,
                    report_bytes=excluded.report_bytes, run_dir=excluded.run_dir
            """, (
                run_id, strategy, underlying, str(report.get("status", "unknown")),
                created_at, report_sha, zlib.compress(report_bytes, level=6),
                len(report_bytes), str(directory),
            ))
            connection.execute("DELETE FROM options_research_artifact WHERE run_id=?", (run_id,))
            connection.executemany("""
                INSERT INTO options_research_artifact
                    (run_id, artifact_path, media_type, sha256, byte_size, payload_zlib, stored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [(run_id, *item, now) for item in artifacts])
        return {"run_id": run_id, "artifacts": len(artifacts), "report_sha256": report_sha}

    def store_document(self, *, document_type: str, identity: str, path: str | Path) -> dict:
        source = Path(path)
        raw = source.read_bytes()
        media_type = {
            ".json": "application/json", ".md": "text/markdown",
            ".csv": "text/csv", ".jsonl": "application/x-ndjson",
        }.get(source.suffix.lower(), "application/octet-stream")
        return self._store_document_bytes(
            document_type=document_type, identity=identity, raw=raw,
            media_type=media_type,
        )

    def store_document_payload(self, *, document_type: str, identity: str, payload: dict) -> dict:
        """Store a JSON document directly, without writing a duplicate full-size file."""
        payload = report_for_history(payload)
        return self._store_document_bytes(
            document_type=document_type, identity=identity,
            raw=_json(payload).encode("utf-8"), media_type="application/json",
        )

    def _store_document_bytes(
        self, *, document_type: str, identity: str, raw: bytes, media_type: str,
    ) -> dict:
        digest = hashlib.sha256(raw).hexdigest()
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO options_research_document
                    (document_type, identity, media_type, sha256, byte_size, payload_zlib, stored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_type, identity) DO UPDATE SET
                    media_type=excluded.media_type, sha256=excluded.sha256,
                    byte_size=excluded.byte_size, payload_zlib=excluded.payload_zlib,
                    stored_at=excluded.stored_at
            """, (
                document_type, identity, media_type, digest, len(raw),
                zlib.compress(raw, level=6), datetime.now(timezone.utc).isoformat(),
            ))
        return {"identity": identity, "byte_size": len(raw), "sha256": digest}
