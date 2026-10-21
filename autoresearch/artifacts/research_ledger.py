"""Durable lineage for Algo research runs, candidates, folds, and promotions.

Large replay artifacts remain JSON files. This SQLite ledger stores indexed,
append-only run and evaluation metadata and links back to those artifacts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .candidate_archive import candidate_id, logic_version, model_family


LEDGER_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _read_json(path: str | Path | None) -> dict | None:
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


class ResearchLedger:
    """Small SQLite event ledger, separate from the market-bar database."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > LEDGER_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Research ledger schema {version} is newer than supported "
                    f"schema {LEDGER_SCHEMA_VERSION}"
                )
            connection.execute(f"PRAGMA user_version={LEDGER_SCHEMA_VERSION}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def start_invocation(self, invocation_id: str, *, request: dict | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO research_run
                   (invocation_id, started_at, status, request_json)
                   VALUES (?, ?, 'running', ?)""",
                (invocation_id, _now(), _json(request or {})),
            )

    def set_run_context(
        self, invocation_id: str, *, cohort_id: str | None = None,
        data_version: str | None = None, pipeline_version: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE research_run SET
                   cohort_id=COALESCE(?, cohort_id),
                   data_version=COALESCE(?, data_version),
                   pipeline_version=COALESCE(?, pipeline_version),
                   metadata_json=CASE WHEN ? IS NULL THEN metadata_json ELSE ? END
                   WHERE invocation_id=?""",
                (
                    cohort_id, data_version, pipeline_version,
                    None if metadata is None else 1,
                    None if metadata is None else _json(metadata), invocation_id,
                ),
            )

    def finish_invocation(
        self, invocation_id: str, *, status: str, report_path: str | None = None,
        scorecard: dict | None = None, error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE research_run SET finished_at=?, status=?, report_path=?,
                   scorecard_json=?, error=? WHERE invocation_id=?""",
                (
                    _now(), status, report_path,
                    None if scorecard is None else _json(scorecard), error, invocation_id,
                ),
            )

    def interrupt_abandoned_invocations(self, *, except_invocation_id: str) -> int:
        """Close stale running rows after the exclusive research lock is acquired."""
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE research_run SET finished_at=?, status='interrupted',
                   error=COALESCE(error, 'Process ended before the invocation report was saved.')
                   WHERE status='running' AND invocation_id<>?""",
                (_now(), except_invocation_id),
            )
            return int(cursor.rowcount)

    def record_stage(
        self, invocation_id: str, *, stage: str, status: str,
        strategy: str | None = None, reused: bool = False,
        source_artifact: str | None = None, summary: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO research_stage
                   (invocation_id, stage, strategy, status, reused, source_artifact,
                    summary_json, error, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(invocation_id, stage, strategy) DO UPDATE SET
                    status=excluded.status, reused=excluded.reused,
                    source_artifact=excluded.source_artifact,
                    summary_json=excluded.summary_json, error=excluded.error,
                    recorded_at=excluded.recorded_at""",
                (
                    invocation_id, stage, strategy or "", status, int(reused),
                    source_artifact,
                    None if summary is None else _json(summary), error, _now(),
                ),
            )
        if source_artifact:
            self.record_artifact(invocation_id, kind=f"{stage}:{strategy or 'all'}", uri=source_artifact)

    def record_artifact(
        self, invocation_id: str, *, kind: str, uri: str,
        metadata: dict | None = None,
    ) -> None:
        path = Path(uri)
        resolved = str(path.resolve()) if not uri.startswith(("http://", "https://")) else uri
        digest = _sha256(Path(resolved)) if not resolved.startswith(("http://", "https://")) else None
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO research_artifact
                   (artifact_id, invocation_id, kind, uri, sha256, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uuid4().hex, invocation_id, kind, resolved, digest,
                 _json(metadata or {}), _now()),
            )

    def record_discovery_stage(
        self, invocation_id: str, discovery: dict, *, data_version: str | None,
    ) -> None:
        strategies = discovery.get("strategies") or {}
        self.record_stage(
            invocation_id, stage="discovery", status=discovery.get("status", "unknown"),
            summary={"strategy_count": len(strategies), "error_strategies": discovery.get("error_strategies", [])},
        )
        for strategy, status in strategies.items():
            artifact = status.get("detail_artifact")
            self.record_stage(
                invocation_id, stage="discovery", strategy=strategy,
                status=status.get("status", "unknown"), reused=False,
                source_artifact=artifact,
                summary={key: status.get(key) for key in (
                    "selection_status", "archive_sequence", "search_seed",
                    "adaptive_improvement", "adaptive_novelty",
                ) if key in status},
                error=status.get("error"),
            )
            detail = _read_json(artifact)
            if not detail:
                continue
            embedded_artifact = detail.get("detail_artifact")
            if embedded_artifact:
                self.record_artifact(invocation_id, kind=f"discovery_detail:{strategy}", uri=embedded_artifact)
            window = detail.get("window") or {}
            baseline = next(
                (row for row in detail.get("candidates", []) if row.get("name") == "baseline"),
                {},
            )
            for index, row in enumerate(detail.get("candidates") or []):
                parameters = row.get("parameters")
                if not isinstance(parameters, dict):
                    continue
                family = row.get("model_family") or model_family(strategy, parameters)
                version = row.get("logic_version") or logic_version(strategy)
                parent_id = row.get("parent_candidate_id")
                cid = candidate_id(strategy, parameters, family=family, version=version)
                self._record_candidate(
                    cid, strategy=strategy, regime=None, parameters=parameters,
                    source_candidate_id=parent_id,
                    metadata={
                        "first_seen_source": "discovery", "model_family": family,
                        "logic_version": version, "parent_candidate_id": parent_id,
                        "data_version": data_version or detail.get("data_version") or (detail.get("data") or {}).get("data_version"),
                        "cost_model_version": row.get("cost_model_version"),
                    },
                )
                metrics = row.get("metrics") or {}
                baseline_metrics = baseline.get("metrics") or {}
                self._record_evaluation(
                    invocation_id=invocation_id, candidate_id_value=cid,
                    stage="discovery", evaluation_kind="training_candidate",
                    data_version=data_version or detail.get("data_version") or (detail.get("data") or {}).get("data_version"),
                    train_start=window.get("start"), train_end=window.get("end"),
                    window_days=window.get("sessions"),
                    search_iteration=row.get("search_iteration") or 0,
                    evaluation_index=index,
                    seed=status.get("search_seed"), sequence=status.get("archive_sequence"),
                    fitness=row.get("fitness"), baseline_fitness=baseline.get("fitness"),
                    metrics=metrics,
                    baseline_metrics=baseline_metrics,
                    eligible=row.get("eligible"),
                    passed=None,
                    gates={
                        "name": row.get("name"),
                        "model_family": family, "logic_version": version,
                        "parent_candidate_id": parent_id,
                        "cost_model_version": row.get("cost_model_version"),
                        "regime_metrics": row.get("regime_metrics") or {},
                        "selection_status": detail.get("selection_status"),
                    },
                    artifact_path=artifact,
                )
            for index, profile in enumerate(
                (detail.get("adaptive_regime_search") or {}).get("profiles") or [],
            ):
                parameters = profile.get("parameters")
                regime = profile.get("regime")
                if not isinstance(parameters, dict) or not regime:
                    continue
                family = profile.get("model_family") or model_family(strategy, parameters)
                version = profile.get("logic_version") or logic_version(strategy)
                parent_id = profile.get("parent_candidate_id") or profile.get("source_candidate_id")
                cid = candidate_id(strategy, parameters, regime, family=family, version=version)
                self._record_candidate(
                    cid, strategy=strategy, regime=regime, parameters=parameters,
                    source_candidate_id=parent_id,
                    metadata={
                        "first_seen_source": "regime_discovery", "model_family": family,
                        "logic_version": version, "parent_candidate_id": parent_id,
                        "data_version": data_version,
                        "cost_model_version": profile.get("cost_model_version"),
                    },
                )
                self._record_evaluation(
                    invocation_id=invocation_id, candidate_id_value=cid,
                    stage="discovery", evaluation_kind="regime_candidate",
                    data_version=data_version,
                    train_start=window.get("start"), train_end=window.get("end"),
                    search_iteration=0, evaluation_index=index,
                    sequence=status.get("archive_sequence"), seed=status.get("search_seed"),
                    fitness=profile.get("fitness"), baseline_fitness=profile.get("baseline_fitness"),
                    metrics=profile.get("metrics") or {}, baseline_metrics={},
                    eligible=bool(profile.get("historical_eligible")),
                    passed=bool(profile.get("improved_over_baseline")),
                    gates={
                        "regime": regime, "improved_over_baseline": profile.get("improved_over_baseline"),
                        "historical_sample_met": profile.get("historical_sample_met"),
                        "historical_positive_after_costs": profile.get("historical_positive_after_costs"),
                        "model_family": family, "logic_version": version,
                        "parent_candidate_id": parent_id,
                        "cost_model_version": profile.get("cost_model_version"),
                    },
                    artifact_path=artifact,
                )

    def record_verification_stage(
        self, invocation_id: str, verification: dict, artifacts: dict,
        *, data_version: str | None,
    ) -> None:
        strategies = verification.get("strategies") or {}
        self.record_stage(
            invocation_id, stage="verification", status=verification.get("status", "unknown"),
            summary={"strategy_count": len(strategies), "error_strategies": verification.get("error_strategies", [])},
        )
        for strategy, status in strategies.items():
            artifact = status.get("detail_artifact") or (artifacts.get(strategy) or {}).get("verification")
            reused = status.get("status") == "reused"
            summary = status.get("summary") or {}
            self.record_stage(
                invocation_id, stage="verification", strategy=strategy,
                status=status.get("status", "unknown"), reused=reused,
                source_artifact=artifact,
                summary={key: summary.get(key) for key in (
                    "fold_count", "stitched_oos", "average_fitness_decay", "checks", "thresholds",
                ) if key in summary},
                error=status.get("error"),
            )
            detail = _read_json(artifact)
            if not detail:
                continue
            embedded_artifact = detail.get("detail_artifact")
            if embedded_artifact:
                self.record_artifact(invocation_id, kind=f"verification_detail:{strategy}", uri=embedded_artifact)
            for index, fold in enumerate(detail.get("folds") or []):
                parameters = fold.get("train_parameters")
                if not isinstance(parameters, dict):
                    continue
                family = model_family(strategy, parameters)
                version = logic_version(strategy)
                cid = candidate_id(strategy, parameters, family=family, version=version)
                self._record_candidate(
                    cid, strategy=strategy, regime=None, parameters=parameters,
                    metadata={
                        "first_seen_source": "rolling_verification",
                        "model_family": family, "logic_version": version,
                        "data_version": data_version or (detail.get("data") or {}).get("data_version"),
                    },
                )
                source_evaluation = None
                if reused:
                    source_evaluation = self._prior_fold_evaluation(
                        cid, data_version or (detail.get("data") or {}).get("data_version"),
                        fold.get("test_start"), fold.get("test_end"),
                    )
                oos_metrics = fold.get("oos_metrics") or {}
                self._record_evaluation(
                    invocation_id=invocation_id, candidate_id_value=cid,
                    stage="verification", evaluation_kind="rolling_policy_fold",
                    data_version=data_version or (detail.get("data") or {}).get("data_version"),
                    train_start=fold.get("train_start"), train_end=fold.get("train_end"),
                    test_start=fold.get("test_start"), test_end=fold.get("test_end"),
                    fold_number=fold.get("fold") or index + 1,
                    search_iteration=0, evaluation_index=index,
                    fitness=fold.get("oos_fitness"), baseline_fitness=None,
                    metrics={
                        "training": fold.get("train_metrics"),
                        "oos": oos_metrics,
                        "fitness_decay": fold.get("fitness_decay"),
                        "adaptive_improvement": fold.get("adaptive_improvement"),
                        "selected_candidate": fold.get("selected_candidate"),
                    },
                    baseline_metrics={}, eligible=None,
                    passed=(summary.get("checks") or {}).get("overall_pass"),
                    gates={
                        "summary_checks": summary.get("checks"),
                        "thresholds": summary.get("thresholds"),
                        "model_family": family, "logic_version": version,
                    },
                    artifact_path=artifact, reused=reused,
                    source_evaluation_id=source_evaluation,
                )

    def record_completed_report(
        self, invocation_id: str, report: dict, *, report_path: str,
    ) -> None:
        orchestration = report.get("orchestration") or {}
        self.set_run_context(
            invocation_id,
            cohort_id=orchestration.get("cohort_id") or orchestration.get("run_id"),
            data_version=orchestration.get("data_version"),
            pipeline_version=orchestration.get("pipeline_version"),
            metadata={"orchestration": orchestration, "run_date_et": report.get("run_date_et")},
        )
        forward = report.get("forward_validation") or {}
        self.record_stage(
            invocation_id, stage="forward_validation", status=forward.get("status", "skipped"),
            summary={key: forward.get(key) for key in (
                "current_regime", "active_trial_count", "staged_candidate_ids",
                "promoted", "rejected_candidate_ids", "expired_candidate_ids",
                "simulation_errors", "staging_errors",
            ) if key in forward},
        )
        portfolio = report.get("portfolio_coordinator_replay") or {}
        for strategy, artifacts in (report.get("adaptive_strategy_artifacts") or {}).items():
            coordinator_artifact = artifacts.get("coordinator_artifact")
            if coordinator_artifact:
                self.record_artifact(
                    invocation_id, kind=f"verification_coordinator:{strategy}",
                    uri=coordinator_artifact,
                )
        self.record_stage(
            invocation_id, stage="portfolio", status="completed" if portfolio else "skipped",
            reused=bool((report.get("stage_timings") or {}).get("portfolio_replay", {}).get("reused")),
            summary=portfolio.get("fold_summary") or report.get("portfolio_summary") or {},
        )
        self.record_artifact(invocation_id, kind="full_report", uri=report_path)
        if report.get("status") == "error":
            self.finish_invocation(
                invocation_id, status="error", report_path=report_path,
                scorecard=report.get("fitness_timeline", {}).get("current"),
                error=report.get("error"),
            )
            return
        for fold_number, fold in enumerate(portfolio.get("folds") or [], start=1):
            window = fold.get("fold") or {}
            metrics = {
                key: value for key, value in fold.items()
                if key in (
                    "net_return_pct", "benchmark_net_return_pct",
                    "excess_return_vs_buy_hold_pct_points", "max_drawdown_pct",
                    "closed_trade_count", "entry_count", "open_position_count",
                )
            }
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO portfolio_fold
                       (portfolio_fold_id, invocation_id, fold_number, test_start, test_end,
                        metrics_json, details_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (uuid4().hex, invocation_id, fold_number,
                     window.get("test_start") or fold.get("start"),
                     window.get("test_end") or fold.get("end"),
                     _json(metrics), _json(fold)),
                )
        self.finish_invocation(
            invocation_id, status=report.get("status", "completed"),
            report_path=report_path,
            scorecard=report.get("fitness_timeline", {}).get("current"),
        )

    def record_forward_cycle(
        self, invocation_id: str, *, active_trials: dict,
        terminal_trials: list[dict], promoted_rows: list[dict],
        data_version: str, data_end: str, observed_trial_ids: set[str] | None = None,
    ) -> None:
        observed_trial_ids = observed_trial_ids or set()
        trials = [
            row for row in [*active_trials.values(), *terminal_trials]
            if isinstance(row, dict) and row.get("candidate_id")
        ]
        with self._connect() as connection:
            for trial in trials:
                trial_id = trial.get("trial_id") or uuid4().hex
                strategy, regime = trial.get("strategy"), trial.get("regime")
                params = trial.get("parameters") or {}
                if not strategy or not isinstance(params, dict):
                    continue
                self._record_candidate(
                    trial.get("candidate_id"), strategy=strategy, regime=regime,
                    parameters=params,
                    source_candidate_id=trial.get("parent_candidate_id") or trial.get("source_candidate_id"),
                    metadata={
                        "discovery_artifact": trial.get("discovery_artifact"),
                        "model_family": trial.get("model_family") or model_family(strategy, params),
                        "logic_version": trial.get("logic_version") or logic_version(strategy),
                        "parent_candidate_id": trial.get("parent_candidate_id") or trial.get("source_candidate_id"),
                        "data_version": data_version,
                        "cost_model_version": trial.get("cost_model_version"),
                        "forward_cost_model_version": trial.get("forward_cost_model_version"),
                        "cohort_id": trial.get("cohort_id"),
                    },
                    connection=connection,
                )
                connection.execute(
                    """INSERT INTO forward_trial
                       (trial_id, candidate_id, source_candidate_id, origin_invocation_id,
                        strategy, regime, status, staged_on, staged_at, finished_at,
                        baseline_profile_sha256, reason, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(trial_id) DO UPDATE SET
                        status=excluded.status, finished_at=excluded.finished_at,
                        baseline_profile_sha256=excluded.baseline_profile_sha256,
                        reason=excluded.reason, metadata_json=excluded.metadata_json""",
                    (
                        trial_id, trial["candidate_id"],
                        trial.get("parent_candidate_id") or trial.get("source_candidate_id"),
                        trial.get("origin_invocation_id"), strategy, regime,
                        trial.get("status", "validating"), trial.get("staged_on"),
                        trial.get("staged_at"), trial.get("finished_at"),
                        trial.get("baseline_profile_sha256"), trial.get("reason"), _json(trial),
                    ),
                )
                simulation = trial.get("latest_simulation") or {}
                reused = bool(simulation.get("reused"))
                observation_status = simulation.get("status") or trial.get("status", "validating")
                if (
                    trial_id in observed_trial_ids
                    and simulation.get("data_end") == data_end
                ):
                    connection.execute(
                        """INSERT OR IGNORE INTO forward_trial_observation
                           (observation_id, trial_id, invocation_id, data_version, data_end,
                            simulation_start, status, reused, candidate_metrics_json,
                            baseline_metrics_json, details_json, recorded_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            uuid4().hex, trial_id, invocation_id,
                            simulation.get("data_version") or data_version,
                            simulation.get("data_end"),
                            simulation.get("simulation_start"), observation_status, int(reused),
                            _json(simulation.get("candidate_metrics") or {}),
                            _json(simulation.get("baseline_metrics") or {}),
                            _json({"trial_status": trial.get("status"), "reason": trial.get("reason")}),
                            _now(),
                        ),
                    )
        for row in promoted_rows:
            trial_id = row.get("trial_id")
            if not trial_id:
                continue
            with self._connect() as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO research_promotion
                       (promotion_id, trial_id, candidate_id, invocation_id, release_id,
                        promoted_at, forward_metrics_json, evidence_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid4().hex, trial_id, row.get("candidate_id"), invocation_id,
                        row.get("release_id"), row.get("promoted_at"),
                        _json(row.get("forward_metrics") or {}), _json(row),
                    ),
                )

    def _record_candidate(
        self, candidate_id_value: str, *, strategy: str, regime: str | None,
        parameters: dict, source_candidate_id: str | None = None,
        metadata: dict | None = None, connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self._connect() as db:
                self._record_candidate(
                    candidate_id_value, strategy=strategy, regime=regime,
                    parameters=parameters, source_candidate_id=source_candidate_id,
                    metadata=metadata, connection=db,
                )
            return
        stamp = _now()
        prior = connection.execute(
            "SELECT metadata_json FROM research_candidate WHERE candidate_id=?",
            (candidate_id_value,),
        ).fetchone()
        prior_metadata = {}
        if prior:
            try:
                prior_metadata = json.loads(prior[0] or "{}")
            except (TypeError, json.JSONDecodeError):
                prior_metadata = {}
        merged_metadata = {**prior_metadata, **(metadata or {})}
        connection.execute(
            """INSERT INTO research_candidate
               (candidate_id, strategy, regime, parameters_json, source_candidate_id,
                first_seen_at, last_seen_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                source_candidate_id=COALESCE(excluded.source_candidate_id, research_candidate.source_candidate_id),
                metadata_json=excluded.metadata_json""",
            (candidate_id_value, strategy, regime, _json(parameters), source_candidate_id,
             stamp, stamp, _json(merged_metadata)),
        )

    def _record_evaluation(
        self, *, invocation_id: str | None, candidate_id_value: str,
        stage: str, evaluation_kind: str, data_version: str | None,
        metrics: dict, baseline_metrics: dict | None,
        train_start: str | None = None, train_end: str | None = None,
        test_start: str | None = None, test_end: str | None = None,
        window_days: int | None = None, fold_number: int | None = None,
        search_iteration: int = 0, evaluation_index: int = 0,
        seed: int | None = None, sequence: int | None = None,
        fitness: float | None = None, baseline_fitness: float | None = None,
        eligible: bool | None = None, passed: bool | None = None,
        gates: dict | None = None, artifact_path: str | None = None,
        reused: bool = False, source_evaluation_id: str | None = None,
        legacy_key: str | None = None,
    ) -> str:
        oos = metrics.get("oos") if isinstance(metrics.get("oos"), dict) else metrics
        evaluation_id = legacy_key or uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO candidate_evaluation
                   (evaluation_id, invocation_id, candidate_id, stage, evaluation_kind,
                    data_version, train_start, train_end, test_start, test_end,
                    window_days, fold_number, search_iteration, evaluation_index,
                    search_seed, search_sequence, fitness, baseline_fitness,
                    net_return_pct, sharpe, max_drawdown_pct, closed_trades,
                    eligible, passed, reused, source_evaluation_id, metrics_json,
                    baseline_metrics_json, gates_json, artifact_path, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evaluation_id, invocation_id, candidate_id_value, stage, evaluation_kind,
                    data_version, train_start, train_end, test_start, test_end,
                    window_days, fold_number, search_iteration, evaluation_index,
                    seed, sequence, fitness, baseline_fitness,
                    oos.get("net_return_pct") if isinstance(oos, dict) else None,
                    oos.get("daily_sharpe_annualized") if isinstance(oos, dict) else None,
                    oos.get("max_drawdown_pct") if isinstance(oos, dict) else None,
                    oos.get("closed_trade_count", oos.get("closed_trades")) if isinstance(oos, dict) else None,
                    None if eligible is None else int(bool(eligible)),
                    None if passed is None else int(bool(passed)), int(reused),
                    source_evaluation_id, _json(metrics), _json(baseline_metrics or {}),
                    _json(gates or {}), artifact_path, _now(),
                ),
            )
        return evaluation_id

    def _prior_fold_evaluation(
        self, candidate_id_value: str, data_version: str | None,
        test_start: str | None, test_end: str | None,
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT evaluation_id FROM candidate_evaluation
                   WHERE candidate_id=? AND stage='verification'
                     AND evaluation_kind='rolling_policy_fold'
                     AND data_version IS ? AND test_start IS ? AND test_end IS ?
                   ORDER BY recorded_at DESC LIMIT 1""",
                (candidate_id_value, data_version, test_start, test_end),
            ).fetchone()
        return row[0] if row else None

    def import_legacy(self, output_dir: str | Path) -> None:
        """One-time import of existing JSON scorecards and candidate memory."""
        root = Path(output_dir)
        with self._connect() as connection:
            done = connection.execute(
                "SELECT value FROM research_meta WHERE key='legacy_json_import_v1'"
            ).fetchone()
            if done:
                return

        history = _read_json(root / "layers" / "fitness-history.json") or {}
        for index, entry in enumerate(history.get("runs") or []):
            invocation_id = entry.get("invocation_id") or f"legacy-history-{index:06d}"
            scorecard = entry.get("scorecard") or {}
            with self._connect() as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO research_run
                       (invocation_id, cohort_id, started_at, finished_at, status,
                        data_version, metadata_json, scorecard_json, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'legacy_fitness_history')""",
                    (
                        invocation_id, entry.get("run_id"),
                        entry.get("created_at") or _now(), entry.get("created_at"),
                        entry.get("status", "unknown"), entry.get("data_version"),
                        _json({"legacy_entry": entry}), _json(scorecard),
                    ),
                )

        archive_path = root / "adaptive" / "archive" / "candidate-archive.json"
        archive = _read_json(archive_path) or {}
        for strategy, candidates in (archive.get("candidates") or {}).items():
            for cid, row in candidates.items():
                parameters = row.get("parameters") or {}
                if not isinstance(parameters, dict):
                    continue
                self._record_candidate(
                    cid, strategy=strategy, regime=None, parameters=parameters,
                    metadata={"legacy_archive": True, "discovery_observations": row.get("discovery_observations")},
                )
                self._record_evaluation(
                    invocation_id=None, candidate_id_value=cid,
                    stage="legacy_archive", evaluation_kind="latest_candidate_snapshot",
                    data_version=None, train_start=(row.get("last_window") or {}).get("start"),
                    train_end=(row.get("last_window") or {}).get("end"),
                    fitness=row.get("fitness"), metrics=row.get("metrics") or {},
                    baseline_metrics={}, eligible=row.get("eligible"), passed=None,
                    gates={
                        "legacy": True,
                        "regime_metrics": row.get("regime_metrics"),
                        "discovery_observations": row.get("discovery_observations"),
                    }, artifact_path=str(archive_path),
                    legacy_key=f"legacy-archive-{cid}",
                )
                for fold_index, fold in enumerate(row.get("verification_folds") or []):
                    self._record_evaluation(
                        invocation_id=None, candidate_id_value=cid,
                        stage="legacy_verification", evaluation_kind="candidate_fold",
                        data_version=None, test_start=fold.get("test_start"),
                        test_end=fold.get("test_end"), fold_number=fold_index + 1,
                        fitness=fold.get("fitness"), metrics=fold.get("metrics") or {},
                        baseline_metrics={}, eligible=None, passed=None,
                        gates={"legacy": True}, artifact_path=str(archive_path),
                        legacy_key=f"legacy-fold-{cid}-{fold_index}",
                    )

        self.record_artifact(
            "legacy-import", kind="legacy_candidate_archive", uri=str(archive_path),
            metadata={"updated_at": archive.get("updated_at")},
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO research_meta(key,value) VALUES ('legacy_json_import_v1',?)",
                (_now(),),
            )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_run (
    invocation_id TEXT PRIMARY KEY,
    cohort_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    data_version TEXT,
    pipeline_version TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    scorecard_json TEXT,
    error TEXT,
    report_path TEXT,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS research_run_cohort_idx ON research_run(cohort_id);
CREATE INDEX IF NOT EXISTS research_run_status_idx ON research_run(status, started_at);
CREATE TABLE IF NOT EXISTS research_stage (
    stage_id INTEGER PRIMARY KEY,
    invocation_id TEXT NOT NULL REFERENCES research_run(invocation_id),
    stage TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reused INTEGER NOT NULL DEFAULT 0,
    source_artifact TEXT,
    summary_json TEXT,
    error TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE(invocation_id, stage, strategy)
);
CREATE TABLE IF NOT EXISTS research_candidate (
    candidate_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    regime TEXT,
    parameters_json TEXT NOT NULL,
    source_candidate_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS research_candidate_lookup_idx ON research_candidate(strategy, regime);
CREATE TABLE IF NOT EXISTS candidate_evaluation (
    evaluation_id TEXT PRIMARY KEY,
    invocation_id TEXT REFERENCES research_run(invocation_id),
    candidate_id TEXT NOT NULL REFERENCES research_candidate(candidate_id),
    stage TEXT NOT NULL,
    evaluation_kind TEXT NOT NULL,
    data_version TEXT,
    train_start TEXT,
    train_end TEXT,
    test_start TEXT,
    test_end TEXT,
    window_days INTEGER,
    fold_number INTEGER,
    search_iteration INTEGER NOT NULL DEFAULT 0,
    evaluation_index INTEGER NOT NULL DEFAULT 0,
    search_seed INTEGER,
    search_sequence INTEGER,
    fitness REAL,
    baseline_fitness REAL,
    net_return_pct REAL,
    sharpe REAL,
    max_drawdown_pct REAL,
    closed_trades INTEGER,
    eligible INTEGER,
    passed INTEGER,
    reused INTEGER NOT NULL DEFAULT 0,
    source_evaluation_id TEXT REFERENCES candidate_evaluation(evaluation_id),
    metrics_json TEXT NOT NULL DEFAULT '{}',
    baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
    gates_json TEXT NOT NULL DEFAULT '{}',
    artifact_path TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS candidate_eval_lookup_idx
    ON candidate_evaluation(candidate_id, stage, data_version, test_start, test_end);
CREATE INDEX IF NOT EXISTS candidate_eval_perf_idx
    ON candidate_evaluation(stage, net_return_pct);
CREATE TABLE IF NOT EXISTS portfolio_fold (
    portfolio_fold_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL REFERENCES research_run(invocation_id),
    fold_number INTEGER NOT NULL,
    test_start TEXT,
    test_end TEXT,
    metrics_json TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS forward_trial (
    trial_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES research_candidate(candidate_id),
    source_candidate_id TEXT,
    origin_invocation_id TEXT REFERENCES research_run(invocation_id),
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    status TEXT NOT NULL,
    staged_on TEXT,
    staged_at TEXT,
    finished_at TEXT,
    baseline_profile_sha256 TEXT,
    reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS forward_trial_candidate_idx ON forward_trial(candidate_id, status);
CREATE TABLE IF NOT EXISTS forward_trial_observation (
    observation_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES forward_trial(trial_id),
    invocation_id TEXT NOT NULL REFERENCES research_run(invocation_id),
    data_version TEXT,
    data_end TEXT,
    simulation_start TEXT,
    status TEXT NOT NULL,
    reused INTEGER NOT NULL DEFAULT 0,
    candidate_metrics_json TEXT NOT NULL DEFAULT '{}',
    baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
    details_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    UNIQUE(trial_id, invocation_id)
);
CREATE TABLE IF NOT EXISTS research_promotion (
    promotion_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES forward_trial(trial_id),
    candidate_id TEXT NOT NULL REFERENCES research_candidate(candidate_id),
    invocation_id TEXT NOT NULL REFERENCES research_run(invocation_id),
    release_id TEXT,
    promoted_at TEXT,
    forward_metrics_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS research_artifact (
    artifact_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(invocation_id, kind, uri)
);
"""
