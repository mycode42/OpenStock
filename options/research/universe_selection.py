"""Cached, point-in-time eligible universe for options research.

This module is deliberately separate from the live portfolio configuration.
The selector reads only completed daily snapshots, writes a versioned cache, and
never places orders.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import argparse
from bisect import bisect_left, bisect_right
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from runtime.paths import DATA_DIR, ROOT
from strategies.event_driven.calendar import EventCalendar
from options.research.history_store import OptionsHistoryStore


ET = ZoneInfo("America/New_York")
DEFAULT_RULES_PATH = Path(__file__).with_name("eligibility.default.json")
DEFAULT_CACHE_DIR = DATA_DIR / "options" / "research" / "universe_cache"
DEFAULT_DATABASE = DATA_DIR / "options" / "research" / "options_history.sqlite3"
BASE_UNIVERSE_PATH = Path(__file__).with_name("universe.json")


def load_rules(path: str | Path | None = None) -> dict:
    source = Path(path) if path else DEFAULT_RULES_PATH
    if not source.is_absolute():
        source = ROOT / source
    rules = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(rules, dict) or not isinstance(rules.get("hard_filters"), dict):
        raise ValueError(f"invalid eligible-universe rules: {source}")
    if not rules.get("rule_version"):
        raise ValueError("eligible-universe rules need rule_version")
    return rules


def _rules_hash(rules: dict) -> str:
    payload = json.dumps(rules, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _universe_symbols(scope: str = "history_117") -> list[str]:
    payload = json.loads(BASE_UNIVERSE_PATH.read_text(encoding="utf-8"))
    if scope == "original_24":
        groups = [payload["categories"]["existing_algo_portfolio"]]
    elif scope == "history_117":
        groups = payload["categories"].values()
    else:
        raise ValueError("universe scope must be original_24 or history_117")
    symbols = []
    seen = set()
    for group in groups:
        for symbol in group:
            normalized = str(symbol).strip().upper()
            if normalized and normalized not in seen:
                symbols.append(normalized)
                seen.add(normalized)
    return symbols


def _read_history(
    connection: sqlite3.Connection, symbol: str, *, end_date: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read effective daily-close rows and their precomputed liquidity metrics."""
    sql = """
        WITH ranked AS (
            SELECT s.snapshot_id, s.session_date, s.as_of, s.spot,
                   s.underlying_close, s.equity_volume, s.volatility_state,
                   src.source_kind,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.session_date
                       ORDER BY CASE src.source_kind WHEN 'alpaca' THEN 2 WHEN 'theta' THEN 1 ELSE 0 END DESC,
                                s.as_of DESC
                   ) AS source_rank
            FROM options_history_snapshot AS s
            JOIN options_history_active_source AS a ON a.source_id=s.source_id
            JOIN options_history_source AS src ON src.source_id=s.source_id
            WHERE s.underlying=? AND s.phase='daily_close'
              AND (? IS NULL OR s.session_date<=?)
        ), selected AS (
            SELECT * FROM ranked WHERE source_rank=1
        )
        SELECT s.snapshot_id, s.session_date, s.as_of, s.spot,
               s.underlying_close, s.equity_volume, s.volatility_state,
               l.option_volume_60d, l.option_volume_coverage,
               l.total_open_interest_60d, l.open_interest_coverage,
               l.median_option_spread, l.spread_coverage
        FROM selected AS s
        LEFT JOIN options_history_liquidity AS l ON l.snapshot_id=s.snapshot_id
        ORDER BY s.session_date DESC
    """
    params: list[Any] = [symbol, end_date, end_date]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = connection.execute(sql, params).fetchall()
    rows.reverse()
    return [
        {
            "snapshot_id": row[0], "session_date": row[1], "as_of": row[2],
            "spot": float(row[3]) if row[3] is not None else None,
            "close": float(row[4]) if row[4] is not None else None,
            "equity_volume": row[5], "volatility_state": row[6],
            "option_volume_60d": row[7], "option_volume_coverage": row[8],
            "total_open_interest_60d": row[9], "open_interest_coverage": row[10],
            "median_option_spread": row[11], "spread_coverage": row[12],
        }
        for row in rows
    ]


def _latest_spy_date(connection: sqlite3.Connection, symbols: list[str]) -> str:
    if "SPY" not in symbols:
        raise ValueError("SPY is required as the market regime reference")
    rows = _read_history(connection, "SPY", limit=1)
    if not rows:
        raise ValueError("no daily_close SPY history is available in the options history store")
    return rows[-1]["session_date"]


def _mean(values: list[float]) -> float | None:
    values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(values) / len(values) if values else None


def _evaluate_hard_filters(
    symbol: str, history: list[dict], *, as_of_date: str, rules: dict,
) -> dict:
    hard = rules["hard_filters"]
    lookback = int(rules.get("lookback_sessions", 20))
    minimum_sessions = int(rules.get("min_valid_sessions", lookback))
    coverage_floor = float(rules.get("minimum_coverage", 0.8))
    window = [row for row in history if row["session_date"] <= as_of_date][-lookback:]
    reasons: list[str] = []

    option_volume_rows = [
        row for row in window
        if row["option_volume_60d"] is not None
        and (row["option_volume_coverage"] or 0.0) >= coverage_floor
    ]
    equity_rows = [row for row in window if row["equity_volume"] is not None and row["spot"]]
    spread_rows = [
        row for row in window if row["median_option_spread"] is not None
        and (row["spread_coverage"] or 0.0) >= coverage_floor
    ]
    avg_option_volume = _mean([row["option_volume_60d"] for row in option_volume_rows])
    avg_equity_volume = _mean([row["equity_volume"] for row in equity_rows])
    avg_equity_dollar_volume = _mean([row["equity_volume"] * row["spot"] for row in equity_rows])
    median_spread = median([row["median_option_spread"] for row in spread_rows]) if spread_rows else None
    latest = window[-1] if window else None
    latest_oi = latest["total_open_interest_60d"] if latest else None
    oi_coverage = (latest or {}).get("open_interest_coverage") or 0.0

    if len(window) < minimum_sessions:
        reasons.append("insufficient_daily_snapshots")
    elif latest is not None and (
        date.fromisoformat(as_of_date) - date.fromisoformat(latest["session_date"])
    ).days > 5:
        reasons.append("stale_snapshot")
    if len(option_volume_rows) < minimum_sessions:
        reasons.append("insufficient_option_volume_coverage")
    if len(equity_rows) < minimum_sessions:
        reasons.append("insufficient_equity_volume_coverage")
    if len(spread_rows) < minimum_sessions:
        reasons.append("insufficient_option_spread_coverage")

    min_volume = float(hard.get("min_average_option_volume_60d", 0))
    min_oi = int(hard.get("min_latest_total_open_interest_60d", 0))
    activity_mode = str(hard.get("activity_mode", "volume_or_oi")).lower()
    volume_pass = avg_option_volume is not None and avg_option_volume >= min_volume
    oi_pass = latest_oi is not None and oi_coverage >= coverage_floor and latest_oi >= min_oi
    if activity_mode == "volume_or_oi":
        if not (volume_pass or oi_pass):
            reasons.append("options_activity_below_threshold")
    elif activity_mode == "volume_and_oi":
        if not volume_pass:
            reasons.append("average_option_volume_below_threshold")
        if not oi_pass:
            reasons.append("total_open_interest_below_threshold")
    else:
        raise ValueError("hard_filters.activity_mode must be volume_or_oi or volume_and_oi")

    minimum_equity_volume = float(hard.get("min_average_equity_volume", 0))
    if avg_equity_volume is None or avg_equity_volume < minimum_equity_volume:
        reasons.append("average_equity_volume_below_threshold")
    minimum_dollar_volume = float(hard.get("min_average_equity_dollar_volume", 0))
    if avg_equity_dollar_volume is None or avg_equity_dollar_volume < minimum_dollar_volume:
        reasons.append("average_equity_dollar_volume_below_threshold")
    maximum_spread = hard.get("max_median_option_spread_fraction")
    if maximum_spread is not None and (median_spread is None or median_spread > float(maximum_spread)):
        reasons.append("median_option_spread_above_threshold")
    if hard.get("max_equity_spread_fraction") is not None:
        reasons.append("equity_spread_data_unavailable")

    return {
        "ticker": symbol,
        "latest_session": latest["session_date"] if latest else None,
        "average_option_volume_60d_20d": avg_option_volume,
        "option_volume_valid_sessions": len(option_volume_rows),
        "latest_total_open_interest_60d": latest_oi,
        "open_interest_coverage": oi_coverage,
        "average_equity_volume_20d": avg_equity_volume,
        "average_equity_dollar_volume_20d": avg_equity_dollar_volume,
        "median_option_spread_fraction_20d": median_spread,
        "spread_valid_sessions": len(spread_rows),
        "hard_eligible": not reasons,
        "hard_reasons": reasons,
    }


def _market_state(history: dict[str, list[dict]], *, as_of_date: str, rules: dict) -> dict:
    regime_rules = rules["regime"]
    spy = [row for row in history.get("SPY", []) if row["session_date"] <= as_of_date]
    trend_period = int(regime_rules.get("trend_lookback_sessions", 200))
    closes = [row["close"] or row["spot"] for row in spy if row["close"] or row["spot"]]
    trend = "unknown"
    trend_average = None
    if len(closes) >= trend_period:
        trend_average = _mean(closes[-trend_period:])
        trend = "risk_on" if closes[-1] >= trend_average else "risk_off"
    latest = spy[-1] if spy else {}
    vol = str(latest.get("volatility_state") or "unknown").lower()
    volatility = "high" if vol in {"volatile", "high"} else "normal" if vol in {"normal", "low"} else "unknown"
    return {
        "trend": trend, "trend_average": trend_average,
        "spy_close": closes[-1] if closes else None,
        "volatility": volatility, "as_of_date": as_of_date,
    }


def _bucket_for(symbol: str, rules: dict, universe: dict) -> str:
    overrides = rules.get("ticker_buckets", {})
    if symbol in overrides:
        return str(overrides[symbol])
    categories = universe.get("categories", {})
    category_buckets = rules.get("category_buckets", {})
    for category, tickers in categories.items():
        if symbol in tickers and category in category_buckets:
            return str(category_buckets[category])
    return "unmapped"


def _sector_scores(
    history: dict[str, list[dict]], *, as_of_date: str, rules: dict,
) -> dict[str, dict]:
    lookback = int(rules["regime"].get("sector_momentum_lookback_sessions", 20))
    proxies = rules.get("bucket_proxies", {})
    spy_rows = [row for row in history.get("SPY", []) if row["session_date"] <= as_of_date]
    scores = {}
    for bucket, proxy in proxies.items():
        rows = [row for row in history.get(proxy, []) if row["session_date"] <= as_of_date]
        if len(rows) <= lookback:
            continue
        latest = rows[-1]
        if latest["session_date"] != as_of_date:
            try:
                if (date.fromisoformat(as_of_date) - date.fromisoformat(latest["session_date"])).days > 5:
                    continue
            except ValueError:
                continue
        start = rows[-lookback - 1]
        start_px = start["close"] or start["spot"]
        end_px = latest["close"] or latest["spot"]
        if not start_px or not end_px:
            continue
        relative = end_px / start_px - 1.0
        spy_latest_rows = [row for row in spy_rows if row["session_date"] <= latest["session_date"]]
        if len(spy_latest_rows) <= lookback:
            continue
        spy_start, spy_end = spy_latest_rows[-lookback - 1], spy_latest_rows[-1]
        spy_start_px = spy_start["close"] or spy_start["spot"]
        spy_end_px = spy_end["close"] or spy_end["spot"]
        if spy_start_px and spy_end_px:
            relative -= spy_end_px / spy_start_px - 1.0
        scores[bucket] = {
            "proxy": proxy, "relative_momentum": relative,
            "proxy_session": latest["session_date"],
        }
    return scores


def _earnings_reasons(
    symbols: list[str], *, as_of_date: str, rules: dict, calendar: EventCalendar | None,
) -> tuple[dict[str, str], dict]:
    settings = rules.get("earnings_blackout", {})
    if not settings.get("enabled"):
        return {}, {"enabled": False, "status": "disabled"}
    if calendar is None:
        return {symbol: "earnings_calendar_missing" for symbol in symbols}, {
            "enabled": True, "status": "missing",
        }
    now = datetime.now(ET)
    freshness = calendar.freshness(
        now, max_age=timedelta(hours=float(settings.get("calendar_max_age_hours", 96))),
    )
    if settings.get("require_fresh_calendar", True) and not freshness["fresh"]:
        return {symbol: "earnings_calendar_stale" for symbol in symbols}, {
            "enabled": True, "status": "stale", "freshness": freshness,
        }

    cutoff = datetime.combine(date.fromisoformat(as_of_date), time(16, 0), tzinfo=ET)
    visible = calendar.known_by(cutoff)
    before = int(settings.get("days_before", 3))
    after = int(settings.get("days_after", 1))
    reasons = {}
    for symbol in symbols:
        for event in visible:
            if event.ticker != symbol or not event.is_earnings:
                continue
            days = (event.scheduled_at.date() - cutoff.date()).days
            if -after <= days <= before:
                reasons[symbol] = "earnings_blackout"
                break
    return reasons, {
        "enabled": True, "status": "active" if freshness["fresh"] else "stale_allowed",
        "freshness": freshness, "known_events": len(visible),
        "blackout_days_before": before, "blackout_days_after": after,
    }


def _apply_sector_overlay(
    hard_rows: list[dict], *, history: dict[str, list[dict]], as_of_date: str,
    rules: dict, universe: dict, ignore_regime: bool = False,
) -> tuple[list[dict], dict]:
    state = _market_state(history, as_of_date=as_of_date, rules=rules)
    scores = _sector_scores(history, as_of_date=as_of_date, rules=rules)
    regime_rules = rules["regime"]
    top_n = int(regime_rules.get("top_sector_buckets", 4))
    if ignore_regime:
        allowed = set(scores)
    elif state["trend"] == "risk_off" and state["volatility"] == "high":
        allowed = set(regime_rules.get("risk_off_high_vol_buckets", []))
    elif state["trend"] == "risk_off":
        allowed = set(regime_rules.get("risk_off_normal_vol_buckets", []))
    else:
        allowed = set(scores)
    ranked = sorted(
        (bucket for bucket in allowed if bucket in scores),
        key=lambda bucket: scores[bucket]["relative_momentum"], reverse=True,
    )
    if not ranked:
        ranked = sorted(scores, key=lambda bucket: scores[bucket]["relative_momentum"], reverse=True)
    favored = ranked[:top_n]
    always = {str(item).upper() for item in regime_rules.get("always_eligible", [])}
    fallback = not scores and bool(regime_rules.get("hard_only_when_proxy_data_missing", True))
    selected = []
    for row in hard_rows:
        symbol = row["ticker"]
        bucket = _bucket_for(symbol, rules, universe)
        is_core = symbol in always
        soft_ok = fallback or is_core or bucket in favored
        row.update({
            "bucket": bucket, "always_eligible_core": is_core,
            "soft_eligible": soft_ok,
            "soft_reason": "core" if is_core else "favored_sector" if bucket in favored else (
                "proxy_data_missing_hard_only" if fallback else "sector_not_favored"
            ),
            "sector_relative_momentum": scores.get(bucket, {}).get("relative_momentum"),
        })
        if soft_ok:
            selected.append(row)
    selected.sort(
        key=lambda row: (
            0 if row["always_eligible_core"] else 1,
            -(row["sector_relative_momentum"] if row["sector_relative_momentum"] is not None else -999),
            row["ticker"],
        )
    )
    max_names = int(regime_rules.get("max_eligible_tickers", 40))
    selected = selected[:max_names]
    return selected, {
        "market_state": state,
        "ranked_sector_buckets": [
            {"bucket": bucket, **scores[bucket]} for bucket in ranked
        ],
        "favored_buckets": favored,
        "sector_proxy_data_missing": not bool(scores),
        "hard_only_fallback": fallback,
        "regime_overlay_ignored": ignore_regime,
        "always_eligible_core": sorted(always),
        "max_eligible_tickers": max_names,
    }


def _pearson(left: dict[str, float], right: dict[str, float]) -> float | None:
    common = sorted(set(left) & set(right))
    if len(common) < 10:
        return None
    x, y = [left[day] for day in common], [right[day] for day in common]
    mean_x, mean_y = _mean(x), _mean(y)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    variance_x = sum((a - mean_x) ** 2 for a in x)
    variance_y = sum((b - mean_y) ** 2 for b in y)
    if variance_x <= 0 or variance_y <= 0:
        return None
    return covariance / math.sqrt(variance_x * variance_y)


def _returns(history: list[dict], *, as_of_date: str, lookback: int) -> dict[str, float]:
    rows = [row for row in history if row["session_date"] <= as_of_date]
    rows = [row for row in rows if row["close"] or row["spot"]][-(lookback + 1):]
    return {
        current["session_date"]: math.log(after / before)
        for previous, current in zip(rows, rows[1:])
        if (before := previous["close"] or previous["spot"]) > 0
        and (after := current["close"] or current["spot"]) > 0
    }


def apply_portfolio_caps(
    symbols: list[str], *, sector_by_symbol: dict[str, str],
    history: dict[str, list[dict]], as_of_date: str, config: dict,
) -> dict:
    """Allocate equal desired weights with sector and rolling-correlation caps."""
    if not symbols:
        return {"weights": {}, "sector_exposure": {}, "cluster_exposure": {}, "binding": []}
    max_sector = float(config.get("max_sector_exposure", 0.3))
    max_cluster = float(config.get("max_correlation_cluster_exposure", 0.4))
    lookback = int(config.get("correlation_lookback_sessions", 60))
    threshold = float(config.get("correlation_threshold", 0.85))
    if not 0 < max_sector <= 1 or not 0 < max_cluster <= 1 or not -1 <= threshold <= 1:
        raise ValueError("portfolio caps must have valid fractions and correlation threshold")
    desired = 1.0 / len(symbols)
    returns = {symbol: _returns(history.get(symbol, []), as_of_date=as_of_date, lookback=lookback)
               for symbol in symbols}
    clusters: list[dict] = []
    weights: dict[str, float] = {}
    sector_exposure: dict[str, float] = {}
    cluster_exposure: dict[str, float] = {}
    binding = []
    for symbol in symbols:
        best_cluster, best_correlation = None, None
        for cluster in clusters:
            correlations = [
                _pearson(returns[symbol], returns[member]) for member in cluster["members"]
            ]
            correlations = [value for value in correlations if value is not None]
            corr = max(correlations) if correlations else None
            if corr is not None and corr >= threshold and (best_correlation is None or corr > best_correlation):
                best_cluster, best_correlation = cluster, corr
        new_cluster = best_cluster is None
        if best_cluster is None:
            cluster_name = f"cluster_{len(clusters) + 1}"
            clusters.append({"name": cluster_name, "representative": symbol, "members": []})
            cluster_exposure[cluster_name] = 0.0
            best_cluster = clusters[-1]
        cluster_name = best_cluster["name"]
        sector = sector_by_symbol.get(symbol, "unmapped")
        sector_left = max(0.0, max_sector - sector_exposure.get(sector, 0.0))
        cluster_left = max(0.0, max_cluster - cluster_exposure.get(cluster_name, 0.0))
        allocation = min(desired, sector_left, cluster_left)
        if allocation < desired:
            binding.append({
                "ticker": symbol, "desired_weight": desired, "allocated_weight": allocation,
                "sector": sector, "cluster": cluster_name,
                "correlation_to_cluster": best_correlation,
                "sector_cap_binding": sector_left < min(desired, cluster_left) - 1e-12,
                "correlation_cap_binding": cluster_left < min(desired, sector_left) - 1e-12,
            })
        if allocation > 0:
            if not best_cluster["members"]:
                best_cluster["representative"] = symbol
            best_cluster["members"].append(symbol)
            weights[symbol] = allocation
            sector_exposure[sector] = sector_exposure.get(sector, 0.0) + allocation
            cluster_exposure[cluster_name] += allocation
        elif new_cluster:
            clusters.remove(best_cluster)
            cluster_exposure.pop(cluster_name, None)
    return {
        "weights": weights, "sector_exposure": sector_exposure,
        "cluster_exposure": cluster_exposure, "binding": binding,
        "max_sector_exposure": max_sector,
        "max_correlation_cluster_exposure": max_cluster,
        "correlation_threshold": threshold,
    }


def build_eligible_cache(
    *, database: str | Path = DEFAULT_DATABASE, as_of_date: str | None = None,
    rules_path: str | Path | None = None, cache_dir: str | Path = DEFAULT_CACHE_DIR,
    event_calendar_path: str | Path | None = None,
) -> dict:
    """Compute one timestamped cache that all research strategies can share."""
    rules = load_rules(rules_path)
    universe_payload = json.loads(BASE_UNIVERSE_PATH.read_text(encoding="utf-8"))
    symbols = _universe_symbols()
    db_path = Path(database).resolve()
    OptionsHistoryStore(db_path)
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        connection.row_factory = sqlite3.Row
        target = as_of_date or _latest_spy_date(connection, symbols)
        date.fromisoformat(target)
        max_lookback = max(
            int(rules["lookback_sessions"]),
            int(rules["regime"].get("trend_lookback_sessions", 200)),
            int(rules["regime"].get("sector_momentum_lookback_sessions", 20)) + 1,
            int(rules["portfolio_caps"].get("correlation_lookback_sessions", 60)) + 1,
        )
        history = {
            symbol: _read_history(connection, symbol, end_date=target, limit=max_lookback)
            for symbol in symbols
        }
    finally:
        connection.close()

    raw_metrics = [
        _evaluate_hard_filters(symbol, history[symbol], as_of_date=target, rules=rules)
        for symbol in symbols
    ]
    hard_rows = [row for row in raw_metrics if row["hard_eligible"]]
    event_path = Path(event_calendar_path) if event_calendar_path else ROOT / "data/event_calendar.csv"
    calendar = EventCalendar.from_csv(event_path) if event_path.is_file() else None
    event_reasons, event_status = _earnings_reasons(
        [row["ticker"] for row in hard_rows], as_of_date=target,
        rules=rules, calendar=calendar,
    )
    for row in hard_rows:
        if row["ticker"] in event_reasons:
            row["hard_eligible"] = False
            row["hard_reasons"].append(event_reasons[row["ticker"]])
    hard_rows = [row for row in hard_rows if row["hard_eligible"]]
    selected, overlay = _apply_sector_overlay(
        hard_rows, history=history, as_of_date=target, rules=rules, universe=universe_payload,
    )
    buckets = {row["ticker"]: row["bucket"] for row in selected}
    caps = apply_portfolio_caps(
        [row["ticker"] for row in selected], sector_by_symbol=buckets,
        history=history, as_of_date=target, config=rules["portfolio_caps"],
    )
    selected_symbols = [row["ticker"] for row in selected]
    selected_set = set(selected_symbols)
    for row in raw_metrics:
        row["eligible"] = row["ticker"] in selected_set
        if row["hard_eligible"] and row.get("soft_eligible") and not row["eligible"]:
            row["soft_reason"] = "ranked_out_max_eligible_tickers"
        elif row.get("eligible"):
            row["soft_reason"] = "selected"
    result = {
        "schema": "options_eligible_universe_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": target,
        "universe_size": len(symbols),
        "hard_eligible_count": len(hard_rows),
        "eligible_count": len(selected_symbols),
        "eligible_symbols": selected_symbols,
        "rule_version": rules["rule_version"],
        "rules_sha256": _rules_hash(rules),
        "rules_path": str(Path(rules_path or DEFAULT_RULES_PATH).resolve()),
        "status": "ready" if selected_symbols else "no_eligible_tickers",
        "event_calendar": event_status,
        "sector_overlay": overlay,
        "portfolio_cap_preview": caps,
        "ticker_decisions": sorted(raw_metrics, key=lambda row: row["ticker"]),
        "limitations": [
            "Underlying NBBO spread history is not currently stored; the enabled spread gate uses option quote spreads.",
            "Earnings blackout is configurable and defaults off until a fresh point-in-time calendar is available.",
            "The sector and exposure rules are research filters; they do not alter live portfolios or orders.",
        ],
    }
    target_dir = Path(cache_dir)
    if not target_dir.is_absolute():
        target_dir = ROOT / target_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    cache_path = target_dir / f"eligible-{target}.json"
    result["cache_path"] = str(cache_path)
    temp_path = cache_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp_path.replace(cache_path)
    return result


def _sweep_rules(base: dict) -> list[dict]:
    """One-factor-at-a-time grid: report alternatives without choosing a winner."""
    sweep = base.get("parameter_sweep", {})
    configurations = [json.loads(json.dumps(base))]
    paths = {
        "min_average_option_volume_60d": ("hard_filters", "min_average_option_volume_60d"),
        "min_latest_total_open_interest_60d": ("hard_filters", "min_latest_total_open_interest_60d"),
        "min_average_equity_volume": ("hard_filters", "min_average_equity_volume"),
        "min_average_equity_dollar_volume": ("hard_filters", "min_average_equity_dollar_volume"),
        "max_median_option_spread_fraction": ("hard_filters", "max_median_option_spread_fraction"),
        "top_sector_buckets": ("regime", "top_sector_buckets"),
        "max_sector_exposure": ("portfolio_caps", "max_sector_exposure"),
        "correlation_threshold": ("portfolio_caps", "correlation_threshold"),
    }
    for parameter, values in sweep.items():
        target = paths.get(parameter)
        if not target:
            continue
        section, key = target
        parent = base[section] if section != "regime" else base["regime"]
        baseline = parent[key]
        for value in values:
            if value == baseline:
                continue
            config = json.loads(json.dumps(base))
            config[section][key] = value
            configurations.append(config)
    return configurations


def _forward_return(
    rows: list[dict], *, start_date: str, target_date: str,
) -> float | None:
    dates = [row["session_date"] for row in rows]
    start_index = bisect_right(dates, start_date) - 1
    end_index = bisect_left(dates, target_date)
    if start_index < 0 or end_index >= len(rows):
        return None
    start = rows[start_index]["close"] or rows[start_index]["spot"]
    end = rows[end_index]["close"] or rows[end_index]["spot"]
    if not start or not end or start <= 0:
        return None
    return end / start - 1.0


def _evaluate_period(values: list[dict], *, label: str, sector_cap: float) -> dict:
    returns = [float(row["return"]) for row in values if row["return"] is not None]
    turnover = [float(row["turnover"]) for row in values if row["turnover"] is not None]
    selected_counts = [int(row["eligible_count"]) for row in values]
    concentrations = [float(row["max_sector_exposure"]) for row in values if row["max_sector_exposure"] is not None]
    cluster_concentrations = [
        float(row["max_correlation_cluster_exposure"])
        for row in values if row["max_correlation_cluster_exposure"] is not None
    ]
    regime_compared = [row for row in values if row["regime_overlap"] is not None]
    regime_states: dict[str, list[dict]] = {}
    for row in regime_compared:
        regime_states.setdefault(row["regime_state"], []).append(row)
    return {
        "period": label,
        "observations": len(returns),
        "mean_forward_return": _mean(returns),
        "median_forward_return": median(returns) if returns else None,
        "positive_fraction": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "mean_turnover_per_selection": _mean(turnover),
        "mean_eligible_tickers": _mean(selected_counts),
        "mean_max_sector_exposure": _mean(concentrations),
        "maximum_observed_sector_exposure": max(concentrations, default=None),
        "sector_exposure_cap": sector_cap,
        "sector_cap_binding_fraction": (
            sum(row["sector_cap_binding"] for row in values) / len(values) if values else None
        ),
        "sector_cap_breach_sessions": sum(
            value > sector_cap + 1e-9 for value in concentrations
        ),
        "mean_max_correlation_cluster_exposure": _mean(cluster_concentrations),
        "correlation_cap_binding_fraction": (
            sum(row["correlation_cap_binding"] for row in values) / len(values) if values else None
        ),
        "regime_compared_selections": len(regime_compared),
        "regime_selection_changed_fraction": (
            sum(bool(row["regime_selection_changed"]) for row in regime_compared) / len(regime_compared)
            if regime_compared else None
        ),
        "mean_regime_neutral_jaccard": _mean([row["regime_overlap"] for row in regime_compared]),
        "regime_effect_by_state": {
            state: {
                "selections": len(rows),
                "changed_fraction": sum(bool(row["regime_selection_changed"]) for row in rows) / len(rows),
                "mean_jaccard": _mean([row["regime_overlap"] for row in rows]),
            }
            for state, rows in regime_states.items()
        },
    }


def simulate_selection_walk_forward(
    *, database: str | Path = DEFAULT_DATABASE, rules_path: str | Path | None = None,
    output_path: str | Path | None = None, hold_period_sessions: int = 5,
    universe_scope: str | None = None,
) -> dict:
    """Walk-forward test selection composition and exposure controls.

    This evaluates only the selected universe and equal-notional candidate
    basket. It does not model strategy entries, exits, or option fills.
    """
    if hold_period_sessions < 1:
        raise ValueError("hold_period_sessions must be positive")
    rules = load_rules(rules_path)
    walkforward_config = rules.get("walk_forward", {})
    universe_scope = universe_scope or str(walkforward_config.get("universe_scope", "original_24"))
    symbols = _universe_symbols(universe_scope)
    db_path = Path(database).resolve()
    OptionsHistoryStore(db_path)
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        history = {symbol: _read_history(connection, symbol) for symbol in symbols}
    finally:
        connection.close()
    dates = sorted({row["session_date"] for rows in history.values() for row in rows})
    lookback = int(rules.get("lookback_sessions", 20))
    coverage_floor = float(rules.get("minimum_coverage", 0.8))
    minimum_ready_symbols = int(
        rules.get("walk_forward", {}).get("minimum_symbols_with_complete_liquidity_history", 20)
    )
    coverage_report = {
        symbol: {
            "sessions": len(rows),
            "volume_sessions": sum(
                row["option_volume_60d"] is not None
                and (row["option_volume_coverage"] or 0.0) >= coverage_floor for row in rows
            ),
            "equity_volume_sessions": sum(row["equity_volume"] is not None for row in rows),
            "open_interest_sessions": sum(
                row["total_open_interest_60d"] is not None
                and (row["open_interest_coverage"] or 0.0) >= coverage_floor for row in rows
            ),
            "spread_sessions": sum(
                row["median_option_spread"] is not None
                and (row["spread_coverage"] or 0.0) >= coverage_floor for row in rows
            ),
        }
        for symbol, rows in history.items() if rows
    }
    ready_symbols = [
        symbol for symbol, coverage in coverage_report.items()
        if coverage["sessions"] >= lookback
        and coverage["volume_sessions"] >= lookback
        and coverage["equity_volume_sessions"] >= lookback
        and coverage["spread_sessions"] >= lookback
    ]
    minimum_sessions = int(rules.get("walk_forward", {}).get("minimum_sessions", 100))
    if rules.get("earnings_blackout", {}).get("enabled"):
        result = {
            "schema": "options_selection_walk_forward_v1",
            "status": "insufficient_event_history",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rules_sha256": _rules_hash(rules),
            "universe_scope": universe_scope,
            "universe_size": len(symbols),
            "session_count": len(dates),
            "ready_symbol_count": len(ready_symbols),
            "minimum_ready_symbol_count": minimum_ready_symbols,
            "data_coverage": coverage_report,
            "message": (
                "Earnings blackout is enabled, but point-in-time event-calendar snapshots do not cover "
                "the full evaluation history. The historical simulation is withheld to avoid event lookahead."
            ),
        }
    elif len(dates) < minimum_sessions or len(ready_symbols) < minimum_ready_symbols:
        enough_sessions = len(dates) >= minimum_sessions
        status = "insufficient_liquidity_history" if enough_sessions else "insufficient_history"
        result = {
            "schema": "options_selection_walk_forward_v1", "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rules_sha256": _rules_hash(rules),
            "universe_scope": universe_scope,
            "universe_size": len(symbols),
            "session_count": len(dates),
            "ready_symbol_count": len(ready_symbols),
            "minimum_ready_symbol_count": minimum_ready_symbols,
            "minimum_sessions": minimum_sessions,
            "message": (
                f"Need {minimum_ready_symbols} symbols with at least {lookback} covered sessions of "
                f"option volume, option spreads, and equity volume; found {len(ready_symbols)}."
                if enough_sessions else
                f"Need at least {minimum_sessions} shared history dates; found {len(dates)}."
            ),
            "data_coverage": coverage_report,
        }
    else:
        walkforward = rules.get("walk_forward", {})
        train_fraction = float(walkforward.get("train_fraction", 0.6))
        validation_fraction = float(walkforward.get("validation_fraction", 0.2))
        if train_fraction <= 0 or validation_fraction <= 0 or train_fraction + validation_fraction >= 1:
            raise ValueError("walk_forward train and validation fractions must be positive and sum to less than one")
        split_one = int(len(dates) * train_fraction)
        split_two = int(len(dates) * (train_fraction + validation_fraction))
        split_dates = {"train": dates[split_one], "validation": dates[split_two]}
        universe_payload = json.loads(BASE_UNIVERSE_PATH.read_text(encoding="utf-8"))
        max_window = max(
            int(rules["regime"].get("trend_lookback_sessions", 200)),
            int(rules["regime"].get("sector_momentum_lookback_sessions", 20)) + 1,
            int(rules["portfolio_caps"].get("correlation_lookback_sessions", 60)) + 1,
            int(rules.get("lookback_sessions", 20)),
        )
        day_indices = {symbol: [row["session_date"] for row in rows] for symbol, rows in history.items()}
        configurations = _sweep_rules(rules)
        sweep_results = []
        for config_index, candidate_rules in enumerate(configurations):
            previous_selected: set[str] = set()
            observations: list[dict] = []
            for day_index in range(0, len(dates) - hold_period_sessions, hold_period_sessions):
                current_date = dates[day_index]
                recent = {}
                for symbol, rows in history.items():
                    end = bisect_right(day_indices[symbol], current_date)
                    recent[symbol] = rows[max(0, end - max_window):end]
                hard_rows = [
                    metric for symbol in symbols
                    if (metric := _evaluate_hard_filters(
                        symbol, recent[symbol], as_of_date=current_date, rules=candidate_rules,
                    ))["hard_eligible"]
                ]
                selected, _overlay = _apply_sector_overlay(
                    [dict(row) for row in hard_rows], history=recent, as_of_date=current_date,
                    rules=candidate_rules, universe=universe_payload,
                )
                regime_neutral_selected, _ = _apply_sector_overlay(
                    [dict(row) for row in hard_rows], history=recent, as_of_date=current_date,
                    rules=candidate_rules, universe=universe_payload, ignore_regime=True,
                )
                selected_symbols = [row["ticker"] for row in selected]
                neutral_symbols = [row["ticker"] for row in regime_neutral_selected]
                weights = apply_portfolio_caps(
                    selected_symbols,
                    sector_by_symbol={row["ticker"]: row["bucket"] for row in selected},
                    history=recent, as_of_date=current_date,
                    config=candidate_rules["portfolio_caps"],
                )
                active = weights["weights"]
                target_date = dates[day_index + hold_period_sessions]
                basket_return = 0.0
                returned_weight = 0.0
                for symbol, weight in active.items():
                    forward = _forward_return(
                        history[symbol], start_date=current_date, target_date=target_date,
                    )
                    if forward is not None:
                        basket_return += weight * forward
                        returned_weight += weight
                selected_set = set(selected_symbols)
                neutral_set = set(neutral_symbols)
                regime_union = selected_set | neutral_set
                regime_overlap = (
                    len(selected_set & neutral_set) / len(regime_union) if regime_union else None
                )
                market_state = _market_state(recent, as_of_date=current_date, rules=candidate_rules)
                regime_state = f"{market_state['trend']}|{market_state['volatility']}"
                denominator = max(1, len(previous_selected | selected_set))
                turnover = len(previous_selected ^ selected_set) / denominator
                previous_selected = selected_set
                observations.append({
                    "date": current_date, "return": basket_return if returned_weight else None,
                    "turnover": turnover, "eligible_count": len(selected_symbols),
                    "max_sector_exposure": max(weights["sector_exposure"].values(), default=None),
                    "max_correlation_cluster_exposure": max(weights["cluster_exposure"].values(), default=None),
                    "sector_cap_binding": any(item["sector_cap_binding"] for item in weights["binding"]),
                    "correlation_cap_binding": any(item["correlation_cap_binding"] for item in weights["binding"]),
                    "regime_state": regime_state,
                    "regime_overlap": regime_overlap,
                    "regime_selection_changed": (selected_set != neutral_set) if regime_union else None,
                    "eligible_symbols": selected_symbols,
                    "portfolio_weights": active,
                })
            train = [row for row in observations if row["date"] < split_dates["train"]]
            validation = [row for row in observations if split_dates["train"] <= row["date"] < split_dates["validation"]]
            holdout = [row for row in observations if row["date"] >= split_dates["validation"]]
            changed_parameters = {}
            for section, values in candidate_rules.items():
                baseline_values = rules.get(section)
                if not isinstance(values, dict) or not isinstance(baseline_values, dict):
                    if values != baseline_values:
                        changed_parameters[section] = values
                    continue
                for key, value in values.items():
                    if value != baseline_values.get(key):
                        changed_parameters[f"{section}.{key}"] = value
            sweep_results.append({
                "configuration": config_index,
                "rules_sha256": _rules_hash(candidate_rules),
                "changed_parameters": changed_parameters,
                "all": _evaluate_period(
                    observations, label="all",
                    sector_cap=float(candidate_rules["portfolio_caps"]["max_sector_exposure"]),
                ),
                "train": _evaluate_period(
                    train, label="train",
                    sector_cap=float(candidate_rules["portfolio_caps"]["max_sector_exposure"]),
                ),
                "validation": _evaluate_period(
                    validation, label="validation",
                    sector_cap=float(candidate_rules["portfolio_caps"]["max_sector_exposure"]),
                ),
                "holdout": _evaluate_period(
                    holdout, label="holdout",
                    sector_cap=float(candidate_rules["portfolio_caps"]["max_sector_exposure"]),
                ),
            })
        result = {
            "schema": "options_selection_walk_forward_v1", "status": "completed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "universe_scope": universe_scope,
            "universe_size": len(symbols), "session_count": len(dates),
            "first_date": dates[0], "last_date": dates[-1],
            "split_dates": split_dates, "hold_period_sessions": hold_period_sessions,
            "sampling_stride_sessions": hold_period_sessions,
            "rules_sha256": _rules_hash(rules),
            "evaluated_configurations": len(sweep_results),
            "selection_rules": sweep_results,
            "ready_symbol_count": len(ready_symbols),
            "minimum_ready_symbol_count": minimum_ready_symbols,
            "data_coverage": coverage_report,
            "method_notes": [
                "Selection at each date uses only that date and earlier daily snapshots.",
                "The chronological 60/20/20 splits are train, validation, and untouched holdout periods.",
                "Reported returns are close-to-close candidate-basket returns over the configured holding horizon, not strategy P&L.",
                "The grid changes one parameter at a time and reports every result; it does not select or promote a winner.",
                "Regime composition compares the configured selector with an otherwise identical counterfactual that ignores only trend/volatility bucket restrictions.",
                "Historical earnings blackouts require point-in-time event snapshots; the current calendar has limited historical coverage.",
            ],
        }
    if output_path is None:
        output_path = DATA_DIR / "options" / "research" / "selection_simulation" / "latest.json"
    path = Path(output_path)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)
    result["report_path"] = str(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or simulate the options research eligible universe")
    parser.add_argument("mode", choices=("cache", "simulate"))
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--rules", help="eligibility rules JSON")
    parser.add_argument("--as-of-date", help="session date for a cache build (YYYY-MM-DD)")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--output", help="simulation report path")
    parser.add_argument("--hold-period-sessions", type=int, default=5)
    parser.add_argument(
        "--universe-scope", choices=("original_24", "history_117"),
        help="walk-forward ticker subset (defaults to the rule file's scope)",
    )
    parser.add_argument("--event-calendar", help="point-in-time event calendar CSV")
    args = parser.parse_args()
    if args.mode == "cache":
        result = build_eligible_cache(
            database=args.database, as_of_date=args.as_of_date, rules_path=args.rules,
            cache_dir=args.cache_dir, event_calendar_path=args.event_calendar,
        )
        print(json.dumps({
            "status": result["status"], "as_of_date": result["as_of_date"],
            "eligible_count": result["eligible_count"], "eligible_symbols": result["eligible_symbols"],
            "cache_path": result["cache_path"], "rules_sha256": result["rules_sha256"],
        }, sort_keys=True))
    else:
        result = simulate_selection_walk_forward(
            database=args.database, rules_path=args.rules, output_path=args.output,
            hold_period_sessions=args.hold_period_sessions, universe_scope=args.universe_scope,
        )
        print(json.dumps({
            "status": result["status"], "session_count": result.get("session_count"),
            "universe_scope": result.get("universe_scope"),
            "evaluated_configurations": result.get("evaluated_configurations", 0),
            "report_path": result.get("report_path"),
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
