"""Compressed SQLite persistence for options research runs and outputs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import zlib

from runtime.paths import DATA_DIR


DEFAULT_DATABASE = DATA_DIR / "options" / "research" / "research.sqlite3"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


class OptionsResearchArtifactStore:
    """Store run reports, per-run artifacts, and candidate evaluations in SQLite."""

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
                CREATE TABLE IF NOT EXISTS options_research_candidate (
                    run_id TEXT NOT NULL REFERENCES options_research_run(run_id) ON DELETE CASCADE,
                    candidate_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    fitness REAL,
                    selected_oos_fitness REAL,
                    selected_oos_return REAL,
                    selected_oos_sharpe REAL,
                    selected_oos_max_drawdown REAL,
                    selected_oos_trades INTEGER,
                    selected_oos_folds INTEGER NOT NULL DEFAULT 0,
                    final_candidate INTEGER NOT NULL DEFAULT 0,
                    validation_passed INTEGER,
                    verification_status TEXT NOT NULL DEFAULT 'not_selected',
                    PRIMARY KEY(run_id, candidate_id)
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
            """)
            candidate_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(options_research_candidate)")
            }
            for name, declaration in (
                ("fitness", "REAL"),
                ("selected_oos_fitness", "REAL"),
                ("selected_oos_return", "REAL"),
                ("selected_oos_sharpe", "REAL"),
                ("selected_oos_max_drawdown", "REAL"),
                ("selected_oos_trades", "INTEGER"),
                ("selected_oos_folds", "INTEGER NOT NULL DEFAULT 0"),
                ("final_candidate", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_passed", "INTEGER"),
                ("verification_status", "TEXT NOT NULL DEFAULT 'not_selected'"),
            ):
                if name not in candidate_columns:
                    connection.execute(
                        f"ALTER TABLE options_research_candidate ADD COLUMN {name} {declaration}"
                    )
            connection.execute("""
                CREATE INDEX IF NOT EXISTS options_research_candidate_rank_idx
                ON options_research_candidate(strategy, underlying,
                    selected_oos_fitness DESC, selected_oos_folds DESC)
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
        run_id = str(report.get("run_id") or directory.name)
        strategy = str(report.get("strategy") or "unknown")
        underlying = str(report.get("underlying") or "unknown").upper()
        created_at = str(report.get("created_at") or datetime.now(timezone.utc).isoformat())
        report_bytes = _json(report).encode("utf-8")
        report_sha = hashlib.sha256(report_bytes).hexdigest()
        candidates: dict[str, dict] = {}
        for field in ("candidate_leaderboard", "candidate_reports"):
            values = report.get(field)
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                candidate_id = item.get("candidate_id") or item.get("id")
                if candidate_id:
                    candidates[str(candidate_id)] = {**candidates.get(str(candidate_id), {}), **item}
        candidate_path = directory / "candidate_reports.json"
        try:
            candidate_rows = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidate_rows = []
        if isinstance(candidate_rows, list):
            for item in candidate_rows:
                if isinstance(item, dict) and (item.get("candidate_id") or item.get("id")):
                    key = str(item.get("candidate_id") or item.get("id"))
                    candidates[key] = {**candidates.get(key, {}), **item}
        final_candidate_id = report.get("final_candidate_id")
        run_validation = report.get("validation") or {}

        def numeric(metrics: dict, key: str):
            value = metrics.get(key)
            if value is None:
                return None
            try:
                result = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return result if math.isfinite(result) else None

        artifacts = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name.endswith(".lock"):
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
            connection.execute("DELETE FROM options_research_candidate WHERE run_id=?", (run_id,))
            connection.executemany("""
                INSERT INTO options_research_candidate
                    (run_id, candidate_id, strategy, underlying, parameters_json,
                     metrics_json, validation_json, details_json, fitness,
                     selected_oos_fitness, selected_oos_return, selected_oos_sharpe,
                     selected_oos_max_drawdown, selected_oos_trades, selected_oos_folds,
                     final_candidate, validation_passed, verification_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    run_id, candidate_id, strategy, underlying,
                    _json(item.get("parameters") or {}),
                    _json(item.get("metrics") or item.get("mean_is_metrics") or {}),
                    _json(
                        run_validation if candidate_id == final_candidate_id
                        else item.get("validation") or item.get("validation_metrics") or {}
                    ),
                    _json(item),
                    numeric(item.get("mean_is_metrics") or item.get("metrics") or {}, "risk_adjusted_score"),
                    numeric(item.get("selected_oos_metrics") or {}, "risk_adjusted_score"),
                    numeric(item.get("selected_oos_metrics") or {}, "total_return"),
                    numeric(item.get("selected_oos_metrics") or {}, "annualized_sharpe"),
                    numeric(item.get("selected_oos_metrics") or {}, "max_drawdown"),
                    numeric(item.get("selected_oos_metrics") or {}, "trade_count"),
                    int(item.get("selected_oos_folds") or 0),
                    int(candidate_id == final_candidate_id),
                    (int(bool(run_validation.get("approved"))) if candidate_id == final_candidate_id else None),
                    (
                        "final_pass" if run_validation.get("approved") else "final_fail"
                    ) if candidate_id == final_candidate_id else (
                        "selected_oos" if item.get("selected_oos_metrics") else "not_selected"
                    ),
                )
                for candidate_id, item in candidates.items()
            ])
        return {"run_id": run_id, "artifacts": len(artifacts), "candidates": len(candidates), "report_sha256": report_sha}

    def store_document(self, *, document_type: str, identity: str, path: str | Path) -> dict:
        source = Path(path)
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        media_type = {
            ".json": "application/json", ".md": "text/markdown",
            ".csv": "text/csv", ".jsonl": "application/x-ndjson",
        }.get(source.suffix.lower(), "application/octet-stream")
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
