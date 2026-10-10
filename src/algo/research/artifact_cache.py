"""Versioned artifact checks for the scheduled Algo research workflow."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def research_data_version(
    database: str | Path, *, start: date, end: date,
    symbols: list[str], feed: str, adjustment: str,
) -> str:
    """Fingerprint ingestions that overlap the exact range used by the run.

    New bars after ``end`` do not invalidate a weekly cohort. Updates to an
    overlapping request do. Older stores without ingestion metadata fall back
    to their coverage manifest, which is conservative and may invalidate more
    often.
    """
    requested = {symbol.upper() for symbol in symbols} | {"SPY"}
    lower, upper = start.isoformat(), end.isoformat()
    with sqlite3.connect(database) as connection:
        try:
            rows = connection.execute(
                """SELECT id, timeframe, symbols, requested_start, requested_end,
                          rows_written, fetched_at
                   FROM ingestion
                   WHERE feed=? AND adjustment=?
                   ORDER BY id""",
                (feed, adjustment),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        relevant = []
        for row_id, timeframe, symbol_csv, requested_start, requested_end, rows_written, fetched_at in rows:
            ingest_symbols = {part.strip().upper() for part in str(symbol_csv).split(",") if part.strip()}
            if timeframe == "1Day":
                if "SPY" not in requested:
                    continue
            elif not ingest_symbols.intersection(requested):
                continue
            start_day = str(requested_start or "")[:10]
            end_day = str(requested_end or "")[:10]
            if start_day and end_day and (end_day < lower or start_day > upper):
                continue
            relevant.append((
                row_id, timeframe, sorted(ingest_symbols), requested_start,
                requested_end, rows_written, fetched_at,
            ))
    if relevant:
        return stable_hash(relevant)

    # Pre-ingestion legacy stores still get a useful, conservative fingerprint.
    with sqlite3.connect(database) as connection:
        try:
            coverage = connection.execute(
                """SELECT feed, adjustment, timeframe, symbol, COUNT(*),
                          MIN(timestamp), MAX(timestamp)
                   FROM market_bar
                   WHERE feed=? AND adjustment=?
                   GROUP BY feed, adjustment, timeframe, symbol
                   ORDER BY timeframe, symbol""",
                (feed, adjustment),
            ).fetchall()
        except sqlite3.OperationalError:
            coverage = []
    selected = [row for row in coverage if row[3].upper() in requested]
    if not selected:
        raise ValueError(f"No versionable {feed}/{adjustment} market data found in {database}")
    return stable_hash(selected)


def read_json(path: str | Path) -> tuple[dict | None, bytes | None]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, None
    return (value, raw) if isinstance(value, dict) else (None, raw)


def valid_cohort_research(path: Path, *, strategy: str, fingerprint: str) -> dict | None:
    artifact, _ = read_json(path)
    if not artifact:
        return None
    if (
        artifact.get("schema") != "swarm_algo_strategy_research_v1"
        or artifact.get("strategy") != strategy
        or artifact.get("orchestration", {}).get("fingerprint") != fingerprint
    ):
        return None
    return artifact


def valid_cohort_optimization(
    path: Path, *, strategy: str, fingerprint: str,
    research_path: Path, research_bytes: bytes,
) -> dict | None:
    artifact, _ = read_json(path)
    if not artifact:
        return None
    source = artifact.get("source") or {}
    discovered = source.get("candidate_profiles_artifact") or {}
    optimization = artifact.get("optimization") or {}
    if (
        artifact.get("schema") != "swarm_algo_strategy_optimization_v1"
        or artifact.get("strategy") != strategy
        or artifact.get("orchestration", {}).get("fingerprint") != fingerprint
        or discovered.get("sha256") != hashlib.sha256(research_bytes).hexdigest()
        or not optimization.get("folds")
        or not source.get("candidate_profiles")
        or not research_path.is_file()
    ):
        return None
    return artifact


def usable_standalone_pair(
    research_path: Path, optimization_path: Path, *, strategy: str,
    context: dict, now: datetime | None = None,
) -> tuple[dict, dict] | None:
    """Accept a recent standalone CLI pair when it exactly matches this cohort."""
    research, research_bytes = read_json(research_path)
    optimization, _ = read_json(optimization_path)
    if not research or not optimization or research_bytes is None:
        return None
    expected_research = context["research"]
    expected_optimization = context["optimization"]
    research_data = research.get("data") or {}
    opt_data = optimization.get("optimization") or {}
    source = optimization.get("source") or {}
    discovered = source.get("candidate_profiles_artifact") or {}
    if (
        research.get("schema") != "swarm_algo_strategy_research_v1"
        or research.get("strategy") != strategy
        or research_data.get("start") != expected_research["start"]
        or research_data.get("end") != expected_research["end"]
        or research_data.get("requested_symbols") != context["symbols"]
        or research_data.get("database") != context["database"]
        or research.get("profile_search", {}).get("version") != context["search_version"]
        or research.get("live_configuration", {}).get("sha256") != context["live_config_sha256"]
        or optimization.get("schema") != "swarm_algo_strategy_optimization_v1"
        or optimization.get("strategy") != strategy
        or opt_data.get("start") != expected_optimization["start"]
        or opt_data.get("end") != expected_optimization["end"]
        or opt_data.get("symbols") != context["symbols"]
        or source.get("database") != context["database"]
        or source.get("symbols") != context["symbols"]
        or optimization.get("profile_search", {}).get("version") != context["search_version"]
        or optimization.get("live_configuration", {}).get("sha256") != context["live_config_sha256"]
        or not optimization.get("optimization", {}).get("folds")
        or discovered.get("sha256") != hashlib.sha256(research_bytes).hexdigest()
    ):
        return None

    timestamp = optimization.get("created_at") or research.get("created_at")
    try:
        created = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current - created.astimezone(timezone.utc) > timedelta(days=7):
        return None
    return research, optimization
