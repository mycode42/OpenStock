"""Daily Alpaca universe selection constrained by the existing risk catalogs.

The Alpaca asset master is broad, but risk_manager only has sector/theme caps
for configured symbols. We therefore intersect Alpaca's active, tradable US
equities with each mode's preclassified risk catalog, then rank that candidate
pool using clean daily bars. The configured Swing/Day names remain the seed
pool whenever the live most-active screen is unavailable or sparse.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from statistics import stdev
from zoneinfo import ZoneInfo

import requests

from src.accounts import get_account_for_mode
from src.config import STRATEGY_CONFIG, get_mode_config
from src.market_data import regular_feed
from scan_market import get_most_active
from risk_manager import LEVERAGED_ETFS


DATA_BASE = "https://data.alpaca.markets/v2"
ET = ZoneInfo("America/New_York")
ALLOWED_EXCHANGES = {
    "NYSE", "NASDAQ", "NYSEARCA", "ARCA", "AMEX", "BATS", "NYSEAMERICAN",
}
SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
INSTRUMENT_EXCLUSIONS = re.compile(
    r"\b(?:warrant|warrants|right|rights|unit|units|preferred|preference|"
    r"depositary share|depositary shares|convertible preferred)\b",
    re.IGNORECASE,
)
LEVERAGE_TERMS = re.compile(
    r"leveraged|\bultra\w*|\b(?:1\.5|2|3|4|5)x\b|\binverse\b|"
    r"\bdaily\b.*\b(?:bull|bear)\b|\b(?:bull|bear)\b.*\bdaily\b|"
    r"\b(?:bull|bear)\s+(?:1\.5|2|3|4|5)x\b",
    re.IGNORECASE,
)


def _catalog(mode: str) -> tuple[dict, dict[str, str], list[str]]:
    mode_config = get_mode_config(mode)
    category_by_symbol: dict[str, str] = {}
    symbols: list[str] = []
    for category, data in mode_config["universe"].items():
        for raw_ticker in data.get("tickers", []):
            ticker = str(raw_ticker).upper().strip()
            if not ticker:
                continue
            # Match risk_manager._get_universe_maps: the last category wins
            # when a ticker occurs in more than one configured bucket.
            category_by_symbol[ticker] = category
            if ticker not in symbols:
                symbols.append(ticker)
    return mode_config, category_by_symbol, symbols


def _request_json(url: str, *, mode: str, params: dict | None = None):
    response = requests.get(
        url,
        headers=get_account_for_mode(mode).headers,
        params=params,
        timeout=25,
    )
    response.raise_for_status()
    return response.json()


def _fetch_active_assets(mode: str) -> dict[str, dict]:
    """Return Alpaca's active US-equity assets indexed by ticker."""
    account = get_account_for_mode(mode)
    url = f"{account.base_url.rstrip('/')}/assets"
    payload = _request_json(
        url,
        mode=mode,
        params={"status": "active", "asset_class": "us_equity"},
    )
    if not isinstance(payload, list):
        raise ValueError("Alpaca asset response was not a list")
    return {
        str(asset.get("symbol", "")).upper(): asset
        for asset in payload
        if isinstance(asset, dict) and asset.get("symbol")
    }


def _fetch_daily_bars(
    symbols: list[str], *, mode: str, start: str, end: str, feed: str,
) -> dict[str, list[dict]]:
    """Fetch split-adjusted daily bars in bounded batches from Alpaca."""
    result: dict[str, list[dict]] = {}
    for offset in range(0, len(symbols), 50):
        chunk = symbols[offset:offset + 50]
        page_token = None
        while True:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "start": start,
                "end": end,
                "limit": 10000,
                "sort": "asc",
                "adjustment": "split",
                "feed": feed,
            }
            if page_token:
                params["page_token"] = page_token
            payload = _request_json(f"{DATA_BASE}/stocks/bars", mode=mode, params=params)
            for ticker, bars in (payload.get("bars") or {}).items():
                if isinstance(bars, list):
                    result.setdefault(ticker.upper(), []).extend(bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
    return result


def _is_allowed_asset(
    ticker: str,
    asset: dict | None,
    *,
    mode: str,
    configured_symbols: set[str],
) -> tuple[bool, str | None]:
    if not asset:
        return False, "not present in Alpaca's active asset master"
    if str(asset.get("status", "")).lower() != "active":
        return False, "asset is not active"
    if asset.get("tradable") is not True:
        return False, "asset is not tradable"
    asset_class = str(asset.get("class", asset.get("asset_class", ""))).lower()
    if asset_class != "us_equity":
        return False, "asset is not a US equity"
    exchange = str(asset.get("exchange", "")).upper()
    if exchange not in ALLOWED_EXCHANGES:
        return False, "asset is not listed on an allowed US exchange"
    if not SYMBOL_PATTERN.fullmatch(ticker):
        return False, "non-standard ticker format (possible OTC, right, unit, or preferred)"

    name = str(asset.get("name", ""))
    if INSTRUMENT_EXCLUSIONS.search(name):
        return False, "warrant, right, unit, or preferred instrument"
    # Listed warrants commonly use a five-character root ending W/U/R. Four
    # letter common stocks such as SNOW and CARR are intentionally unaffected.
    if len(ticker) >= 5 and ticker[-1] in {"W", "U", "R"}:
        return False, "ticker suffix is commonly used for warrants, units, or rights"

    is_leveraged = ticker in LEVERAGED_ETFS or bool(LEVERAGE_TERMS.search(name))
    day_allowed = (
        mode == "day"
        and bool(get_mode_config(mode)["risk"].get("allow_leveraged_etfs"))
        and ticker in configured_symbols
        and ticker in LEVERAGED_ETFS
    )
    if is_leveraged and not day_allowed:
        return False, "leveraged or inverse ETF is not allowed in this mode"
    return True, None


def _overnight_tradable(asset: dict | None) -> bool:
    if not asset:
        return False
    attributes = asset.get("attributes") or []
    listed = "overnight_tradable" in attributes if isinstance(attributes, list) else False
    return bool(asset.get("overnight_tradable") or listed) and not bool(asset.get("overnight_halted"))


def _clean_daily_bars(bars: list[dict], *, today: str) -> list[dict]:
    """Keep completed, unique daily bars with internally consistent OHLCV."""
    clean_by_date: dict[str, dict] = {}
    for bar in bars:
        try:
            timestamp = str(bar["t"])
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            session_date = timestamp[:10]
            if session_date >= today:
                continue
            opening = float(bar["o"])
            high = float(bar["h"])
            low = float(bar["l"])
            close = float(bar["c"])
            volume = float(bar["v"])
            if not all(math.isfinite(value) for value in (opening, high, low, close, volume)):
                continue
            if min(opening, high, low, close) <= 0 or volume <= 0:
                continue
            if high < max(opening, close, low) or low > min(opening, close, high):
                continue
            clean_by_date[session_date] = {
                "date": session_date,
                "open": opening,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        except (KeyError, TypeError, ValueError):
            continue
    return [clean_by_date[key] for key in sorted(clean_by_date)]


def _metrics(bars: list[dict], market_sessions: list[str]) -> dict | None:
    if len(bars) < int(STRATEGY_CONFIG["universe_min_clean_daily_bars"]):
        return None
    by_date = {bar["date"]: bar for bar in bars}
    recent_sessions = market_sessions[-21:]
    if len(recent_sessions) < 21 or any(session not in by_date for session in recent_sessions):
        return None
    recent_bars = [by_date[session] for session in recent_sessions]
    last_20 = recent_bars[-20:]
    average_dollar_volume = sum(bar["close"] * bar["volume"] for bar in last_20) / 20
    last_price = recent_bars[-1]["close"]
    if (
        average_dollar_volume < float(STRATEGY_CONFIG["universe_min_avg_dollar_volume_20d"])
        or not float(STRATEGY_CONFIG["universe_min_price"]) <= last_price <= float(STRATEGY_CONFIG["universe_max_price"])
    ):
        return None

    closes = [bar["close"] for bar in recent_bars]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    if any(not math.isfinite(value) for value in returns):
        return None
    daily_volatility = stdev(returns) if len(returns) > 1 else 0.0
    annualized_volatility = daily_volatility * math.sqrt(252)
    if not math.isfinite(annualized_volatility):
        return None
    # Liquidity is the primary rank; a modest penalty prefers stable, moderate
    # volatility around 35% annualized without imposing an extra hard cutoff.
    score = math.log10(max(average_dollar_volume, 1.0)) - abs(
        annualized_volatility - float(STRATEGY_CONFIG["universe_moderate_volatility_target_annualized"])
    ) * float(STRATEGY_CONFIG["universe_volatility_penalty"])
    return {
        "last_price": round(last_price, 4),
        "average_dollar_volume_20d": round(average_dollar_volume, 2),
        "clean_daily_bars": len(bars),
        "annualized_volatility_20d": round(annualized_volatility, 4),
        "rank_score": round(score, 6),
    }


def _select_under_theme_caps(
    ranked: list[dict],
    *,
    category_by_symbol: dict[str, str],
    mode_config: dict,
    max_tickers: int,
) -> list[dict]:
    per_category_cap = _theme_quotas(mode_config, max_tickers)

    selected = []
    category_counts: dict[str, int] = {}
    for item in ranked:
        category = category_by_symbol[item["symbol"]]
        if category_counts.get(category, 0) >= per_category_cap.get(category, 0):
            continue
        selected.append(item)
        category_counts[category] = category_counts.get(category, 0) + 1
        if len(selected) >= max_tickers:
            break
    return selected


def _theme_quotas(mode_config: dict, max_tickers: int) -> dict[str, int]:
    weights = {
        category: max(
            0.0,
            float(config.get("max_sector_pct", mode_config["risk"]["max_sector_pct"])),
        )
        for category, config in mode_config["universe"].items()
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {category: 0 for category in weights}
    raw = {category: max_tickers * weight / total_weight for category, weight in weights.items()}
    quotas = {category: math.floor(value) for category, value in raw.items()}
    remaining = max_tickers - sum(quotas.values())
    order = sorted(raw, key=lambda category: (-(raw[category] - quotas[category]), category))
    for category in order[:remaining]:
        quotas[category] += 1
    return quotas


def select_universe(mode: str, max_tickers: int = 50) -> dict:
    """Select up to 60 risk-catalog symbols after Alpaca and bar filters.

    The continuous daemon refreshes this at pre-open and retries an empty
    selection on the configured retry cadence. Configured mode tickers remain
    the seed candidates when Alpaca's most-active screen is unavailable; every
    candidate still has to pass the same tradability, price, dollar-volume,
    and history rules. The default target is 50, with no hard-filter bypass to
    reach the reported 40-symbol minimum.
    """
    if mode not in {"swing", "day", "algo"}:
        raise ValueError("Dynamic strategy universe supports swing, day, and algo portfolio modes")
    max_tickers = max(1, min(int(max_tickers), int(STRATEGY_CONFIG["universe_max_target"])))
    mode_config, category_by_symbol, catalog_symbols = _catalog(mode)
    today = datetime.now(ET).date().isoformat()

    # Alpaca's most-actives endpoint supplies today's dynamic candidates. The
    # configured mode lists remain a broader fallback/seed pool, and all
    # symbols must still belong to risk_manager's capped catalog.
    try:
        active_rows = get_most_active(
            top=int(STRATEGY_CONFIG["universe_active_candidate_count"]), mode=mode,
        )
    except Exception:
        active_rows = []
    active_symbols = {
        str(item.get("symbol", "")).upper()
        for item in active_rows
        if isinstance(item, dict) and item.get("symbol")
    } & set(catalog_symbols)

    # If Alpaca's asset master is unavailable, fail closed: a static seed alone
    # cannot verify current tradability or exchange status.
    try:
        assets = _fetch_active_assets(mode)
    except (requests.RequestException, ValueError) as exc:
        return {
            "tickers": [],
            "categories": {},
            "candidate_count": len(catalog_symbols),
            "screened_count": 0,
            "selected_count": 0,
            "active_screen_count": len(active_symbols),
            "fallback_seed_count": len(catalog_symbols),
            "rejected": {"asset_master_unavailable": str(exc)},
            "selection_date": today,
            "timestamp": datetime.now(ET).isoformat(),
            "refresh": "daily before market open; retry while selection is empty",
        }

    configured_set = set(catalog_symbols)
    metadata_rejected: dict[str, str] = {}
    allowed = []
    for ticker in catalog_symbols:
        okay, reason = _is_allowed_asset(
            ticker,
            assets.get(ticker),
            mode=mode,
            configured_symbols=configured_set,
        )
        if okay:
            allowed.append(ticker)
        else:
            metadata_rejected[ticker] = reason or "asset metadata rejected"

    if not allowed:
        return {
            "tickers": [],
            "categories": {},
            "candidate_count": len(catalog_symbols),
            "screened_count": 0,
            "selected_count": 0,
            "active_screen_count": len(active_symbols),
            "fallback_seed_count": len(catalog_symbols),
            "rejected": {"asset_metadata": metadata_rejected},
            "selection_date": today,
            "timestamp": datetime.now(ET).isoformat(),
            "refresh": "daily before market open; retry while selection is empty",
        }

    now_utc = datetime.now(timezone.utc)
    bar_start = (
        now_utc - timedelta(days=int(STRATEGY_CONFIG["universe_history_days"]))
    ).date().isoformat()
    requested_feed = regular_feed()
    actual_feed = requested_feed
    feed_fallback_reason = None
    try:
        bar_payload = _fetch_daily_bars(
            allowed, mode=mode, start=bar_start, end=now_utc.isoformat(), feed=requested_feed,
        )
    except (requests.RequestException, ValueError) as primary_error:
        fallback_feed = "iex" if requested_feed == "sip" else ""
        if not fallback_feed or fallback_feed == requested_feed:
            exc = primary_error
            bar_payload = None
        else:
            try:
                bar_payload = _fetch_daily_bars(
                    allowed, mode=mode, start=bar_start, end=now_utc.isoformat(), feed=fallback_feed,
                )
                actual_feed = fallback_feed
                feed_fallback_reason = str(primary_error)
            except (requests.RequestException, ValueError) as fallback_error:
                exc = fallback_error
                bar_payload = None
    if bar_payload is None:
        return {
            "tickers": [],
            "categories": {},
            "candidate_count": len(catalog_symbols),
            "tradable_candidate_count": len(allowed),
            "screened_count": 0,
            "selected_count": 0,
            "active_screen_count": len(active_symbols),
            "fallback_seed_count": len(catalog_symbols),
            "rejected": {"daily_bars_unavailable": str(exc)},
            "selection_date": today,
            "timestamp": datetime.now(ET).isoformat(),
            "refresh": "daily before market open; retry while selection is empty",
        }

    rejected: dict[str, str] = dict(metadata_rejected)
    eligible = []
    market_sessions = [bar["date"] for bar in _clean_daily_bars(bar_payload.get("SPY", []), today=today)]
    minimum_history = int(STRATEGY_CONFIG["universe_min_clean_daily_bars"])
    fallback_feed = "iex" if requested_feed == "sip" else ""
    if len(market_sessions) < minimum_history and fallback_feed and actual_feed != fallback_feed:
        try:
            bar_payload = _fetch_daily_bars(
                allowed, mode=mode, start=bar_start, end=now_utc.isoformat(), feed=fallback_feed,
            )
            actual_feed = fallback_feed
            feed_fallback_reason = "primary feed returned fewer than 60 clean completed SPY bars"
            market_sessions = [
                bar["date"]
                for bar in _clean_daily_bars(bar_payload.get("SPY", []), today=today)
            ]
        except (requests.RequestException, ValueError) as fallback_error:
            feed_fallback_reason = str(fallback_error)
    if len(market_sessions) < int(STRATEGY_CONFIG["universe_min_clean_daily_bars"]):
        return {
            "tickers": [],
            "categories": {},
            "candidate_count": len(catalog_symbols),
            "tradable_candidate_count": len(allowed),
            "screened_count": 0,
            "selected_count": 0,
            "active_screen_count": len(active_symbols),
            "fallback_seed_count": len(catalog_symbols),
            "rejected": {"benchmark_calendar_unavailable": "fewer than 60 clean completed SPY bars"},
            "selection_date": today,
            "timestamp": datetime.now(ET).isoformat(),
            "refresh": "daily before market open; retry while selection is empty",
        }
    for ticker in allowed:
        clean = _clean_daily_bars(bar_payload.get(ticker, []), today=today)
        minimum_bars = int(STRATEGY_CONFIG["universe_min_clean_daily_bars"])
        if len(clean) < minimum_bars:
            rejected[ticker] = f"only {len(clean)} clean completed daily bars; need {minimum_bars}"
            continue
        recent_sessions = market_sessions[-21:]
        clean_dates = {bar["date"] for bar in clean}
        if any(session not in clean_dates for session in recent_sessions):
            rejected[ticker] = "missing one or more bars from the latest 20 trading sessions"
            continue
        metric = _metrics(clean, market_sessions)
        if metric is None:
            latest = clean[-1]["close"] if clean else 0.0
            dv20 = sum(bar["close"] * bar["volume"] for bar in clean[-20:]) / min(20, len(clean)) if clean else 0.0
            minimum_price = float(STRATEGY_CONFIG["universe_min_price"])
            maximum_price = float(STRATEGY_CONFIG["universe_max_price"])
            if not minimum_price <= latest <= maximum_price:
                rejected[ticker] = f"last completed close ${latest:.2f} is outside ${minimum_price:g}–${maximum_price:g}"
            else:
                minimum_dollar_volume = float(STRATEGY_CONFIG["universe_min_avg_dollar_volume_20d"])
                if dv20 < minimum_dollar_volume:
                    rejected[ticker] = f"20-day average dollar volume ${dv20:,.0f} is below ${minimum_dollar_volume:,.0f}"
                else:
                    rejected[ticker] = "recent return volatility could not be ranked cleanly"
            continue
        eligible.append({
            "symbol": ticker,
            "category": category_by_symbol[ticker],
            "source": "alpaca_most_active" if ticker in active_symbols else "risk_catalog_seed",
            **metric,
        })

    eligible.sort(key=lambda item: (-item["rank_score"], -item["average_dollar_volume_20d"], item["symbol"]))
    selected = _select_under_theme_caps(
        eligible,
        category_by_symbol=category_by_symbol,
        mode_config=mode_config,
        max_tickers=max_tickers,
    )
    selected_symbols = [item["symbol"] for item in selected]
    return {
        "tickers": selected_symbols,
        "categories": {item["symbol"]: item["category"] for item in selected},
        # An owned holding may drop out of today's selected set. Keep its
        # broker-verified overnight eligibility available for protective exits.
        "overnight_tradable_tickers": sorted(
            ticker for ticker in allowed if _overnight_tradable(assets.get(ticker))
        ),
        "candidate_count": len(catalog_symbols),
        "tradable_candidate_count": len(allowed),
        "screened_count": len(eligible),
        "selected_count": len(selected_symbols),
        "target_count": max_tickers,
        "minimum_target": int(STRATEGY_CONFIG["universe_min_target"]),
        "minimum_target_met": len(selected_symbols) >= int(STRATEGY_CONFIG["universe_min_target"]),
        "active_screen_count": len(active_symbols),
        "fallback_seed_count": len(catalog_symbols) - len(active_symbols),
        "fallback_used": len(selected_symbols) < max_tickers or any(item["source"] == "risk_catalog_seed" for item in selected),
        "selection": selected,
        "rejected_count": len(rejected),
        "rejected_sample": dict(list(rejected.items())[:20]),
        "selection_date": today,
        "timestamp": datetime.now(ET).isoformat(),
        "refresh": "daily before market open; retry while selection is empty",
        "hard_filters": {
            "asset_status": "active",
            "tradable": True,
            "asset_class": "us_equity",
            "exchange_allowlist": sorted(ALLOWED_EXCHANGES),
            "average_dollar_volume_20d_min": STRATEGY_CONFIG["universe_min_avg_dollar_volume_20d"],
            "last_completed_close_min": STRATEGY_CONFIG["universe_min_price"],
            "last_completed_close_max": STRATEGY_CONFIG["universe_max_price"],
            "minimum_clean_completed_daily_bars": STRATEGY_CONFIG["universe_min_clean_daily_bars"],
            "bar_feed": actual_feed,
            "requested_bar_feed": requested_feed,
            "bar_feed_fallback_reason": feed_fallback_reason,
            "leveraged_etfs": "excluded except existing configured Day-mode allowances",
        },
        "theme_selection_quotas": _theme_quotas(mode_config, max_tickers),
        "risk_manager_allocation_caps": {
            category: config.get("max_sector_pct", mode_config["risk"]["max_sector_pct"])
            for category, config in mode_config["universe"].items()
        },
    }
