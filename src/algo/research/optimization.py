"""Independent strategy and direction optimization jobs."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
import json
import os
from pathlib import Path
import sys
import tempfile

from .shared import *
from .settings import *
from .files import _atomic_json
from .reporting import _summarize_walk_forward
def _isolated_direction_profiles(strategy: str, direction: str) -> list[dict]:
    """Build fixed candidate profiles for one core strategy and direction scope."""
    if strategy not in CORE_STRATEGIES or direction not in {"long", "short", "both"}:
        raise ValueError("isolated optimization requires a core strategy and long/short/both direction")
    allow = {"allow_long": direction != "short", "allow_short": direction != "long"}
    if strategy == "mean_reversion":
        variants = [
            ("live_opening_dip", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("patient_opening_dip", {
                "signal_model": "opening_dip", "minimum_dip_pct": 0.018,
                "minimum_rip_pct": 0.018, "minimum_bounce_pct": 0.0025,
                "minimum_fade_pct": 0.0025, "minimum_volume_ratio": 1.4,
                "entry_window_minutes": 45, "higher_low_bars": 1, "lower_high_bars": 1,
            }),
            ("bollinger_reversal", {
                "signal_model": "bollinger", "window": 20, "band_width": 2.0,
                "entry_rsi": 30, "exit_rsi": 58,
                "short_entry_rsi": 68, "short_exit_rsi": 42,
            }),
        ]
    elif strategy == "trend_following":
        variants = [
            ("live_daily_sma_20_50", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("daily_ema_20_50", {
                "average_type": "ema", "fast_period": 20, "slow_period": 50,
                "minimum_slope_pct": 0.002,
            }),
            ("daily_sma_20_60_strict_slope", {
                "average_type": "sma", "fast_period": 20, "slow_period": 60,
                "minimum_slope_pct": 0.004,
            }),
        ]
    elif strategy == "momentum":
        variants = [
            ("live_daily_5_10_20", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("daily_5_10_rank2", {
                "lookbacks": [5, 10], "top_n": 2,
                "minimum_score_spread": 0.03,
            }),
            ("patient_daily_10_20_rank2", {
                "lookbacks": [10, 20], "top_n": 2,
                "minimum_score_spread": 0.05,
            }),
        ]
    elif strategy == "swing_mean_reversion":
        variants = [
            ("live_cyclical_6pct", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("persistent_8pct", {"stretch_pct": 0.08, "minimum_stretch_days": 2}),
            ("patient_8pct_3days", {"stretch_pct": 0.08, "minimum_stretch_days": 3}),
        ]
    else:
        variants = [
            ("live_30_minute_range", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("confirmed_30_minute_range", {
                "opening_range_bars": 6, "post_range_delay_bars": 2,
                "minimum_volume_ratio": 1.5, "breakout_buffer_pct": 0.001,
            }),
            ("strict_30_minute_range", {
                "opening_range_bars": 6, "post_range_delay_bars": 3,
                "minimum_volume_ratio": 2.5, "breakout_buffer_pct": 0.0025,
            }),
        ]

    profiles = []
    for variant, parameters in variants:
        if strategy in {"trend_following", "momentum", "swing_mean_reversion"}:
            parameters = {
                **parameters,
                "direction_mode": "both" if direction == "both" else f"{direction}_only",
            }
        else:
            parameters = {**parameters, **allow}
        profiles.append({
            "name": "baseline" if variant.startswith("live_") else f"{strategy}_{direction}_{variant}",
            "strategies": [strategy],
            "parameters": {strategy: parameters},
            "controls": CORE_RESEARCH_CONTROLS,
        })
    return profiles

def _run_isolated_direction_job(payload: tuple) -> tuple[str, dict]:
    """Run one strategy/direction candidate bank in a single fold worker."""
    strategy, direction, symbols, start, end, db, profiles_path, artifact_path = payload
    from src.algo.walk_forward import build_parser as build_walk_forward_parser, run_walk_forward

    argv = [
        "--symbols", ",".join(symbols), "--start", start, "--end", end,
        "--profiles", str(profiles_path), "--feed", FEED, "--adjustment", ADJUSTMENT,
        "--train-days", "756", "--test-days", "63", "--step-days", "63",
        "--min-train-trades", "10", "--max-train-drawdown-pct", "12",
        "--max-candidate-ticker-days-per-session", str(ISOLATED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION),
        "--workers", "1", "--db", str(db),
    ]
    args = build_walk_forward_parser().parse_args(argv)
    report = run_walk_forward(args)
    _atomic_json(Path(artifact_path), report)
    return f"{strategy}.{direction}", _summarize_walk_forward(report, artifact=Path(artifact_path))

def _run_isolated_core_research(
    *, symbols: list[str], start: date, end: date, db: str, output_dir: Path,
    run_date: date, workers: int,
) -> dict:
    """Tune every core side alone before running the combined-portfolio WFA."""
    from src.algo.walk_forward import _read_profiles

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary_profiles: list[Path] = []
    jobs = []
    for strategy in CORE_STRATEGIES:
        for direction in ("long", "short"):
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f"{strategy}-{direction}-",
                suffix=".json", dir=output_dir, delete=False,
            )
            profile_path = Path(handle.name)
            temporary_profiles.append(profile_path)
            profiles = _isolated_direction_profiles(strategy, direction)
            json.dump(profiles, handle, indent=2)
            handle.write("\n")
            handle.close()
            # Validate the generated candidates before launching worker processes.
            _read_profiles(str(profile_path))
            artifact = output_dir / f"{run_date.isoformat()}-{strategy}-{direction}-walk-forward.json"
            jobs.append((strategy, direction, symbols, start.isoformat(), end.isoformat(), db, profile_path, artifact))

    workers_used = min(max(1, workers), len(jobs), os.cpu_count() or 1)
    results = {}
    try:
        with ProcessPoolExecutor(max_workers=workers_used) as executor:
            futures = [executor.submit(_run_isolated_direction_job, job) for job in jobs]
            for completed, future in enumerate(as_completed(futures), start=1):
                key, report = future.result()
                results[key] = report
                print(
                    f"Completed isolated Algo optimization {completed}/{len(jobs)}: {key}",
                    file=sys.stderr, flush=True,
                )
    finally:
        for profile_path in temporary_profiles:
            profile_path.unlink(missing_ok=True)

    ordered_results = {
        f"{strategy}.{direction}": results[f"{strategy}.{direction}"]
        for strategy in CORE_STRATEGIES for direction in ("long", "short")
    }
    return {
        "status": "completed",
        "strategy_count": len(CORE_STRATEGIES),
        "direction_count": 2,
        "candidate_profiles_per_strategy_direction": 3,
        "worker_count": workers_used,
        "selection_policy": (
            "Each fit contains exactly one core strategy and one entry direction. "
            "Only training-window metrics select a profile; later sessions remain out of sample. "
            "No candidate changes live settings."
        ),
        "results": ordered_results,
    }

