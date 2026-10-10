"""Chronological profile optimization for one Algo strategy family."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from src.algo.backtest import read_borrow_availability
from src.algo.market_store import DEFAULT_DB
from src.market_data import data_tier, regular_feed
from src.algo.strategies.universe import configured_universe

from .files import _atomic_json
from .live_manifest import live_configuration_manifest
from .profiles import candidate_profiles, profile_search_manifest
from .settings import (
    ADJUSTMENT, FEED, ISOLATED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION,
    RESEARCHABLE_STRATEGIES, STRATEGY_FLAVORS,
)


def run_strategy_optimization(args) -> dict:
    """Run one strategy's candidate selection with chronological holdout folds."""
    if args.strategy not in RESEARCHABLE_STRATEGIES:
        raise ValueError(f"Unsupported Algo strategy: {args.strategy}")
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("Algo strategy optimization is restricted to Plus SIP data")
    symbols = _symbols(args.symbols)
    db = str(args.db or DEFAULT_DB)
    output_dir = Path(args.output_dir)
    strategy_dir = output_dir / args.strategy
    strategy_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    run_date = datetime.now().date().isoformat()

    discovered_from = None
    if args.research_artifact:
        research_path = Path(args.research_artifact).resolve()
        raw_research = research_path.read_bytes()
        research_artifact = json.loads(raw_research)
        profiles = _profiles_from_research(
            research_artifact, strategy=args.strategy, optimization_start=args.start,
            symbols=symbols, database=db,
        )
        discovered_from = {
            "path": str(research_path),
            "sha256": hashlib.sha256(raw_research).hexdigest(),
            "research_end": research_artifact["data"]["end"],
        }
    else:
        profiles = candidate_profiles(args.strategy)
    profile_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f"{args.strategy}-profiles-",
            suffix=".json", dir=strategy_dir, delete=False,
        ) as handle:
            profile_path = Path(handle.name)
            json.dump(profiles, handle, indent=2)
            handle.write("\n")

        from src.algo.walk_forward import build_parser, run_walk_forward, _read_profiles

        _read_profiles(str(profile_path))
        wf_argv = [
            "--symbols", ",".join(symbols),
            "--start", args.start, "--end", args.end,
            "--profiles", str(profile_path),
            "--feed", FEED, "--adjustment", ADJUSTMENT,
            "--train-days", str(args.train_days),
            "--test-days", str(args.test_days),
            "--step-days", str(args.step_days),
            "--min-train-trades", str(args.min_train_trades),
            "--max-train-drawdown-pct", str(args.max_train_drawdown_pct),
            "--max-candidate-ticker-days-per-session",
            str(ISOLATED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION),
            "--workers", str(args.workers), "--db", db,
            "--slippage-bps", str(args.slippage_bps),
            "--spread-bps", str(args.spread_bps),
            "--commission-per-share", str(args.commission_per_share),
            "--short-borrow-annual-pct", str(args.short_borrow_annual_pct),
            "--unknown-borrow-penalty-bps", str(args.unknown_borrow_penalty_bps),
        ]
        if args.borrow_availability:
            wf_argv.extend(["--borrow-availability", args.borrow_availability])
        wf_args = build_parser().parse_args(wf_argv)
        wf_args.borrow_availability_data = read_borrow_availability(args.borrow_availability)
        wf_args.research_runtime_overrides = (
            {args.strategy: True} if args.strategy in {"intraday_trend", "intraday_momentum"} else None
        )
        report = run_walk_forward(wf_args)
    finally:
        if profile_path is not None:
            profile_path.unlink(missing_ok=True)

    artifact = {
        "schema": "swarm_algo_strategy_optimization_v1",
        "stage": "strategy_optimization",
        "research_only": True,
        "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "created_at": created_at,
        "strategy": args.strategy,
        "strategy_flavor": STRATEGY_FLAVORS[args.strategy],
        "research_runtime_overrides": (
            {args.strategy: True} if args.strategy in {"intraday_trend", "intraday_momentum"} else {}
        ),
        "direction_scope": "both",
        "profile_search": profile_search_manifest(args.strategy),
        "live_configuration": live_configuration_manifest([args.strategy]),
        "source": {
            "database": str(Path(db).resolve()),
            "symbols": symbols,
            "borrow_availability": (
                str(Path(args.borrow_availability).resolve()) if args.borrow_availability else None
            ),
            "candidate_profiles": profiles,
            "candidate_profiles_artifact": discovered_from,
        },
        "optimization": report,
        "latest_training_selection": _latest_selection(report),
        "promotion_gate": report.get("promotion_gate"),
    }
    dated_path = strategy_dir / f"{run_date}-optimization.json"
    latest_path = strategy_dir / "latest.json"
    _atomic_json(dated_path, artifact)
    _atomic_json(latest_path, artifact)
    return {**artifact, "artifact": str(latest_path.resolve()), "dated_artifact": str(dated_path.resolve())}


def _latest_selection(report: dict) -> dict:
    folds = report.get("folds") or []
    if not folds:
        raise ValueError("Walk-forward optimization completed without any selection folds")
    latest = folds[-1]
    return {
        "train_start": latest["train_start"], "train_end": latest["train_end"],
        "test_start": latest["test_start"], "test_end": latest["test_end"],
        "profile": latest["selected_profile"],
        "parameters": latest["selected_parameters"],
        "strategies": latest["selected_strategies"],
        "controls": latest["selected_controls"],
        "reason": latest["selected_reason"],
    }


def _profiles_from_research(
    artifact: dict, *, strategy: str, optimization_start: str,
    symbols: list[str], database: str,
) -> list[dict]:
    if artifact.get("schema") != "swarm_algo_strategy_research_v1":
        raise ValueError("--research-artifact must point to a strategy research artifact")
    if artifact.get("strategy") != strategy or artifact.get("selection_performed") is not False:
        raise ValueError("Research artifact must contain non-selecting discovery results for this strategy")
    data = artifact.get("data") or {}
    research_end = data.get("end")
    if not research_end or date.fromisoformat(research_end) >= date.fromisoformat(optimization_start):
        raise ValueError(
            "Candidate discovery must end before optimization starts; use non-overlapping date ranges"
        )
    if data.get("requested_symbols") != symbols:
        raise ValueError("Research and optimization must use the same symbol universe")
    if data.get("database") != str(Path(database).resolve()):
        raise ValueError("Research and optimization must use the same market database")
    profiles = artifact.get("candidate_profiles")
    if not isinstance(profiles, list) or len(profiles) < 2:
        raise ValueError("Research artifact has no candidate profile bank")
    names = set()
    for profile in profiles:
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            raise ValueError("Candidate profiles must have unique names")
        if profile["name"] in names:
            raise ValueError("Candidate profile names must be unique")
        names.add(profile["name"])
        if profile.get("strategies") != [strategy] or set(profile.get("parameters", {})) != {strategy}:
            raise ValueError("Each discovered profile must contain only the requested strategy")
    if "baseline" not in names:
        raise ValueError("Research candidate bank must include a baseline profile")
    return profiles


def _symbols(value: str | None) -> list[str]:
    if value:
        symbols = list(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))
    else:
        symbols = configured_universe("algo")["tickers"]
    if not symbols:
        raise ValueError("The Algo research universe is empty")
    return symbols
