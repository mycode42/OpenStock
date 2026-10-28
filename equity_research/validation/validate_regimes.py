"""Walk-forward diagnostics for Algo regime features and model variants.

This is a detector validation, not an order simulation. Strategy proxy returns
are simple prior-move contrarian/momentum labels with overlapping horizons;
use the AlgoPortfolioBacktest for execution-aware policy comparisons.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import math
import sqlite3
from pathlib import Path
from statistics import mean, pstdev

from portfolio.config import ALGO_PORTFOLIO_UNIVERSE, ALGO_REGIME_WARMUP_CALENDAR_DAYS
from market.store import DEFAULT_DB
from market.regime import FRED_SERIES
from strategies.regime import _apply_hysteresis, _raw_rule_states
from strategies.regime_features import (
    FEATURE_SETS, MODEL_EXTENDED_FEATURES, compute_regime_feature_history,
)
from strategies.regime_models import infer_regime_model


CONTEXT_SYMBOLS = list(dict.fromkeys(
    [ticker for bucket in ALGO_PORTFOLIO_UNIVERSE.values() for ticker in bucket["tickers"]]
    + ["HYG", "LQD"]
))


def _load_context_daily(db: Path, *, feed: str, adjustment: str, start: str, end: str) -> dict[str, list[dict]]:
    placeholders = ",".join("?" for _ in CONTEXT_SYMBOLS)
    query = f"""
        WITH grouped AS (
            SELECT symbol, substr(timestamp, 1, 10) AS day,
                   MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts,
                   MAX(high) AS high, MIN(low) AS low, SUM(volume) AS volume
            FROM market_bar
            WHERE feed=? AND adjustment=? AND timeframe='5Min'
              AND symbol IN ({placeholders})
              AND timestamp >= ? AND timestamp <= ?
            GROUP BY symbol, substr(timestamp, 1, 10)
        )
        SELECT g.symbol, g.day, first.open, g.high, g.low, last.close, g.volume
        FROM grouped AS g
        CROSS JOIN market_bar AS first
        CROSS JOIN market_bar AS last
        WHERE first.feed=? AND first.adjustment=? AND first.timeframe='5Min'
          AND first.symbol=g.symbol AND first.timestamp=g.first_ts
          AND last.feed=? AND last.adjustment=? AND last.timeframe='5Min'
          AND last.symbol=g.symbol AND last.timestamp=g.last_ts
        ORDER BY g.symbol, g.day
    """
    from_day = f"{start}T00:00:00.000000Z"
    to_day = f"{end}T23:59:59.999999Z"
    params = (
        feed, adjustment, *CONTEXT_SYMBOLS, from_day, to_day,
        feed, adjustment, feed, adjustment,
    )
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()
    result: dict[str, list[dict]] = {symbol: [] for symbol in CONTEXT_SYMBOLS}
    for symbol, day, opening, high, low, close, volume in rows:
        result[symbol].append({"date": day, "o": opening, "h": high, "l": low, "c": close, "v": volume})
    return result


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _feature_distribution(feature_rows: list[dict], *, start: date, end: date) -> dict:
    names = (
        "efficiency_ratio_20d", "realized_vol_percentile_252d", "adx_14",
        "atr_percentile_252d", "vix_percentile_252d", "vix_term_slope",
        "high_yield_spread_percentile_252d", "hyg_lqd_relative_return_20d",
        "breadth_positive_20d", "average_pairwise_corr_20d",
        "average_pairwise_corr_percentile_252d",
        "median_dollar_volume_percentile_252d",
    )
    selected = [
        row for row in feature_rows
        if start <= date.fromisoformat(row["date"]) <= end
    ]
    output = {}
    for name in names:
        values = sorted(
            float(row[name]) for row in selected
            if isinstance(row.get(name), (int, float)) and math.isfinite(float(row[name]))
        )
        if not values:
            output[name] = {"coverage_pct": 0.0, "p10": None, "p50": None, "p90": None}
            continue
        pick = lambda quantile: values[min(len(values) - 1, int(quantile * (len(values) - 1)))]
        output[name] = {
            "coverage_pct": round(100 * len(values) / max(1, len(selected)), 2),
            "p10": round(pick(0.10), 6), "p50": round(pick(0.50), 6), "p90": round(pick(0.90), 6),
        }
    return output


def _load_macro(db: Path) -> tuple[dict[str, list[dict]], dict]:
    connection = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute("""
            SELECT series_id, observation_date, value FROM macro_observation
            WHERE provider='fred' ORDER BY series_id, observation_date
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        connection.close()
    by_id = {series: [] for series in FRED_SERIES}
    for series_id, observed, value in rows:
        if series_id in by_id:
            by_id[series_id].append({"date": observed, "value": float(value)})
    status = "cached" if any(by_id.values()) else "unavailable"
    return {FRED_SERIES[key]: values for key, values in by_id.items()}, {
        "status": status,
        "observations_by_series": {key: len(values) for key, values in by_id.items()},
    }


def _summarize_states(
    states: list[str], volatility: list[str], features: list[dict], *, start: date, end: date,
) -> dict:
    closes = [float(row["c"]) for row in features]
    dates = [date.fromisoformat(str(row.get("date", row.get("t", "")))[:10]) for row in features]
    selected = [index for index, day in enumerate(dates) if start <= day <= end and index + 20 < len(dates)]
    if not selected:
        return {"status": "no_forward_observations"}
    future_vol = []
    future_returns_5 = []
    future_returns_20 = []
    prior_returns_5 = []
    for index in selected:
        returns = [closes[pos] / closes[pos - 1] - 1 for pos in range(index + 1, index + 21)]
        future_vol.append(pstdev(returns) * math.sqrt(252))
        future_returns_5.append(closes[index + 5] / closes[index] - 1)
        future_returns_20.append(closes[index + 20] / closes[index] - 1)
        prior_returns_5.append(closes[index] / closes[index - 5] - 1)
    high_vol_cutoff = sorted(future_vol)[math.floor(0.90 * (len(future_vol) - 1))]
    output = {"observations": len(selected), "future_20d_vol_top_decile_cutoff": high_vol_cutoff}
    for active_name, active_states in (
        ("rule", [states[index] for index in selected]),
        ("volatility_overlay", [volatility[index] for index in selected]),
    ):
        state_counts: dict[str, int] = {}
        for state in active_states:
            state_counts[state] = state_counts.get(state, 0) + 1
        output[f"{active_name}_state_counts"] = state_counts

    predicted_risk_off = [
        states[index] == "volatile" or volatility[index] == "extreme" for index in selected
    ]
    actual_risk_off = [value >= high_vol_cutoff for value in future_vol]
    true_positive = sum(predicted and actual for predicted, actual in zip(predicted_risk_off, actual_risk_off))
    output["risk_off_vs_next_20d_vol_top_decile"] = {
        "precision": _ratio(true_positive, sum(predicted_risk_off)),
        "recall": _ratio(true_positive, sum(actual_risk_off)),
        "predicted_days": sum(predicted_risk_off),
        "actual_top_decile_days": sum(actual_risk_off),
    }
    trend_rows = [
        (states[index], future_returns_5[offset], future_returns_20[offset])
        for offset, index in enumerate(selected)
        if states[index] in {"trending_up", "trending_down"}
    ]
    output["trend_direction_hit_rate"] = {
        "observations": len(trend_rows),
        "forward_5d": _ratio(sum((state == "trending_up") == (ret5 > 0) for state, ret5, _ in trend_rows), len(trend_rows)),
        "forward_20d": _ratio(sum((state == "trending_up") == (ret20 > 0) for state, _, ret20 in trend_rows), len(trend_rows)),
    }
    policies = {
        "structural_mean_reversion_in_range": lambda index: states[index] == "range_bound",
        "high_vol_mean_reversion_only": lambda index: states[index] == "volatile",
        "trend_momentum_only": lambda index: states[index] in {"trending_up", "trending_down"},
    }
    for name, eligible in policies.items():
        proxy_returns = []
        for offset, index in enumerate(selected):
            if index < 5 or index + 5 >= len(closes) or not eligible(index):
                continue
            prior = prior_returns_5[offset]
            future = future_returns_5[offset]
            if name == "trend_momentum_only":
                proxy = (1 if prior > 0 else -1 if prior < 0 else 0) * future
            else:
                proxy = (-1 if prior > 0 else 1 if prior < 0 else 0) * future
            proxy_returns.append(proxy)
        output[name] = {
            "observations": len(proxy_returns),
            "mean_forward_5d_proxy_return_pct": round(mean(proxy_returns) * 100, 5) if proxy_returns else None,
            "win_rate_pct": round(100 * sum(value > 0 for value in proxy_returns) / len(proxy_returns), 3) if proxy_returns else None,
        }
    return output


def validate(db: Path, *, start: str, end: str, feed: str, adjustment: str) -> dict:
    requested_start = date.fromisoformat(start)
    requested_end = date.fromisoformat(end)
    warmup_start = requested_start - timedelta(days=ALGO_REGIME_WARMUP_CALENDAR_DAYS)
    spy_daily = _load_spy_daily(db, feed=feed, adjustment=adjustment, start=warmup_start.isoformat(), end=end)
    contexts = _load_context_daily(
        db, feed=feed, adjustment=adjustment, start=warmup_start.isoformat(), end=end,
    )
    contexts["SPY"] = spy_daily
    macro, macro_diagnostics = _load_macro(db)
    feature_rows = compute_regime_feature_history(
        spy_daily, context_bars=contexts, macro_series=macro,
    )
    index_by_date = {str(row.get("date", row.get("t", "")))[:10]: index for index, row in enumerate(spy_daily)}
    results = {}
    for feature_set in FEATURE_SETS:
        raw, volatility = _raw_rule_states(feature_rows, feature_set)
        smoothed = _apply_hysteresis(raw)
        aligned_states = ["unknown"] * len(spy_daily)
        aligned_volatility = ["unknown"] * len(spy_daily)
        for row_index, feature in enumerate(feature_rows):
            target = index_by_date.get(feature["date"])
            if target is not None:
                aligned_states[target] = smoothed[row_index]
                aligned_volatility[target] = volatility[row_index]
        results[feature_set] = _summarize_states(
            aligned_states, aligned_volatility, spy_daily,
            start=requested_start, end=requested_end,
        )

    model_result = {}
    sample_dates = [
        index for index, row in enumerate(feature_rows)
        if requested_start <= date.fromisoformat(row["date"]) <= requested_end
        and index + 20 < len(feature_rows)
    ]
    for engine in ("hmm", "gmm"):
        model_states = ["unknown"] * len(spy_daily)
        statuses: dict[str, int] = {}
        confidences = []
        convergence = []
        for feature_index in sample_dates:
            result = infer_regime_model(
                feature_rows[:feature_index + 1], engine=engine,
                feature_names=MODEL_EXTENDED_FEATURES,
            )
            status = str(result.get("status", "unknown"))
            statuses[status] = statuses.get(status, 0) + 1
            if status == "ok":
                target_index = index_by_date.get(feature_rows[feature_index]["date"])
                if target_index is not None:
                    model_states[target_index] = str(result["regime"])
                confidences.append(float(result.get("confidence", 0.0)))
                convergence.append(bool(result.get("converged")))
        aligned_volatility = ["unknown"] * len(spy_daily)
        model_summary = _summarize_states(
            model_states, aligned_volatility, spy_daily,
            start=requested_start, end=requested_end,
        )
        model_result[engine] = {
            **model_summary,
            "fit_status_counts": statuses,
            "mean_posterior_confidence": round(mean(confidences), 4) if confidences else None,
            "converged_fit_fraction": round(sum(convergence) / len(convergence), 4) if convergence else None,
            "note": "monthly walk-forward fit; status counts include warmup and feature-coverage failures",
        }
    counts = {symbol: len(rows) for symbol, rows in contexts.items()}
    return {
        "window": {"start": start, "end": end, "feature_warmup_start": warmup_start.isoformat()},
        "data": {"feed": feed, "adjustment": adjustment, "spy_daily_rows": len(spy_daily), "context_daily_rows": counts, "macro_cache": macro_diagnostics},
        "feature_set_results": results,
        "feature_distributions": _feature_distribution(
            feature_rows, start=requested_start, end=requested_end,
        ),
        "marketwide_hmm_gmm": model_result,
        "notes": [
            "Forward 5/20-session returns overlap, so counts are not independent observations.",
            "Mean-reversion and momentum columns are simple signal proxies without transaction costs or execution rules.",
            "High-yield OAS history in FRED is currently limited to three years; the HYG/LQD relative-return proxy covers earlier dates.",
        ],
    }


def _load_spy_daily(db: Path, *, feed: str, adjustment: str, start: str, end: str) -> list[dict]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = connection.execute("""
            SELECT timestamp, open, high, low, close, volume
            FROM market_bar WHERE feed=? AND adjustment=? AND timeframe='1Day'
              AND symbol='SPY' AND timestamp>=? AND timestamp<=? ORDER BY timestamp
        """, (
            feed, adjustment, f"{start}T00:00:00.000000Z", f"{end}T23:59:59.999999Z",
        )).fetchall()
    finally:
        connection.close()
    return [{"t": row[0], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5]} for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--feed", default="sip")
    parser.add_argument("--adjustment", default="split")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args.db, start=args.start, end=args.end, feed=args.feed, adjustment=args.adjustment)
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
