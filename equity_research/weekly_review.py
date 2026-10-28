"""Weekly full-history review of live Algo profiles.

The Sunday run verifies every strategy over the whole history. It never changes
the live profile. It writes one review entry per live (strategy, regime) with the
full-history result beside the recent nightly result. The operator then keeps or
demotes each entry with a command:

    python -m equity_research.weekly_review show
    python -m equity_research.weekly_review keep --strategy swing_momentum --regime range_bound
    python -m equity_research.weekly_review demote --strategy swing_momentum --regime range_bound
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from equity_research.profile_versions import ProfileStore
from equity_research.settings import DEFAULT_OUTPUT_DIR
from runtime.atomic import atomic_write_json
from portfolio.profile_store import FORWARD_VALIDATION_PATH, PROMOTED_PROFILE_PATH

SCHEMA = "swarm_algo_weekly_review_v1"
REVIEW_DIR = DEFAULT_OUTPUT_DIR / "weekly-review"
REVIEW_PATH = REVIEW_DIR / "latest.json"


def summarize_verification(summary: dict | None) -> dict[str, Any]:
    """Keep the fields an operator needs to judge a profile."""
    summary = summary or {}
    checks = summary.get("checks") or {}
    fold = summary.get("fold_consistency") or {}
    bootstrap = summary.get("block_bootstrap") or {}
    return {
        "overall_pass": checks.get("overall_pass"),
        "fold_count": fold.get("fold_count"),
        "positive_fold_fraction": fold.get("positive_fold_fraction"),
        "average_fitness_decay": summary.get("average_fitness_decay"),
        "net_return_pct_p50": bootstrap.get("net_return_pct_p50"),
        "probability_of_loss_pct": bootstrap.get("probability_of_loss_pct"),
    }


def _live_versions(database: Path) -> list[dict[str, Any]]:
    """Live version per strategy and regime, with the version it replaced."""
    with closing(sqlite3.connect(database, timeout=60.0)) as connection:
        connection.row_factory = sqlite3.Row
        active = connection.execute(
            "SELECT version_id, strategy, regime, candidate_id, activated_at "
            "FROM profile_versions WHERE status = 'active' ORDER BY strategy, regime"
        ).fetchall()
        rows = []
        for row in active:
            previous = connection.execute(
                "SELECT version_id FROM profile_versions "
                "WHERE strategy = ? AND regime = ? AND status = 'superseded' "
                "ORDER BY activated_at DESC LIMIT 1",
                (row["strategy"], row["regime"]),
            ).fetchone()
            rows.append({
                "strategy": row["strategy"], "regime": row["regime"],
                "live_version_id": row["version_id"], "live_candidate_id": row["candidate_id"],
                "live_activated_at": row["activated_at"],
                "previous_version_id": previous["version_id"] if previous else None,
            })
    return rows


def build_review(
    *, database: Path, full_root: Path, recent_root: Path, run_id: str,
) -> dict[str, Any]:
    """Compare full-history verification against the recent nightly result for each live profile."""
    entries = []
    for live in _live_versions(database):
        strategy = live["strategy"]
        full = _read(full_root / strategy / "latest-walk-forward-summary.json")
        recent = _read(recent_root / strategy / "latest-walk-forward-summary.json")
        entries.append({
            **live,
            "full_history": summarize_verification(full),
            "recent": summarize_verification(recent),
            "decision": None,
        })
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_decision" if entries else "no_live_profiles",
        "entries": entries,
    }


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_review(review: dict[str, Any], path: Path = REVIEW_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, review, indent=2, sort_keys=True, allow_nan=False)
    return path


def load_review(path: Path = REVIEW_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SCHEMA, "status": "no_review", "entries": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _find(review: dict[str, Any], strategy: str, regime: str) -> dict[str, Any]:
    for entry in review.get("entries", []):
        if entry["strategy"] == strategy and entry["regime"] == regime:
            return entry
    raise KeyError(f"no live profile for {strategy}:{regime} in the current review")


def keep(strategy: str, regime: str, *, path: Path = REVIEW_PATH) -> dict[str, Any]:
    review = load_review(path)
    entry = _find(review, strategy, regime)
    entry["decision"] = {"action": "kept", "decided_at": datetime.now(timezone.utc).isoformat()}
    review["status"] = "pending_decision" if any(item["decision"] is None for item in review["entries"]) else "decided"
    write_review(review, path)
    return entry


def demote(
    strategy: str, regime: str, *, database: Path, path: Path = REVIEW_PATH, dry_run: bool = False,
) -> dict[str, Any]:
    """Revert the live profile to the version it replaced, then record the decision."""

    review = load_review(path)
    entry = _find(review, strategy, regime)
    if not entry.get("previous_version_id"):
        raise ValueError(f"{strategy}:{regime} has no earlier version to restore")
    store = ProfileStore(database, promoted_path=PROMOTED_PROFILE_PATH, lock_path=FORWARD_VALIDATION_PATH)
    result = store.revert(
        strategy=strategy, regime=regime, version_id=entry["previous_version_id"],
        reason="weekly full-history review: demoted by operator", actor="weekly_review", dry_run=dry_run,
    )
    if dry_run:
        return result
    entry["decision"] = {
        "action": "demoted", "restored_version_id": entry["previous_version_id"],
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    review["status"] = "pending_decision" if any(item["decision"] is None for item in review["entries"]) else "decided"
    write_review(review, path)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Keep or demote live Algo profiles after the weekly full review")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    for name in ("keep", "demote"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--strategy", required=True)
        cmd.add_argument("--regime", required=True)
        if name == "demote":
            cmd.add_argument("--database", default=str(DEFAULT_OUTPUT_DIR / "research.sqlite3"))
            cmd.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "show":
        print(json.dumps(load_review(), indent=2, sort_keys=True))
    elif args.command == "keep":
        print(json.dumps(keep(args.strategy, args.regime), indent=2, sort_keys=True))
    else:
        result = demote(args.strategy, args.regime, database=Path(args.database), dry_run=args.dry_run)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
