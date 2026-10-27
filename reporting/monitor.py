"""Read-only snapshot for concise Algo portfolio monitoring automations."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


from runtime.atomic import atomic_write_json
from runtime.paths import DATA_DIR, ROOT
from runtime.systemd import service_is_active
load_dotenv(ROOT / ".env")

from execution.risk import get_portfolio_state  # noqa: E402
from broker.algo import AlgoAlpaca  # noqa: E402
from portfolio.state_store import AlgoStateStore, StateStoreError  # noqa: E402
from portfolio.config import (
    ALGO_SERVICE_CONFIG,
    ALGO_SIGNAL_DATA_CONFIG,
    get_mode_config,
)  # noqa: E402


ET = ZoneInfo("America/New_York")
STATE_PATH = ROOT / str(ALGO_SERVICE_CONFIG["state_db"])
SNAPSHOT_PATH = DATA_DIR / "algo_monitor_snapshot.json"
ALGO_JOURNAL_MODES = {"algo"}
EXTENDED_MARKET_STATES = {"OVERNIGHT", "PREMARKET", "AFTER_HOURS"}


def _expected_extended_data_limited(portfolio: dict, market_state: str | None) -> bool:
    """Avoid calling a quiet extended-hours feed a stale strategy scan."""
    data_status = portfolio.get("data_status") or {}
    if (
        market_state not in EXTENDED_MARKET_STATES
        or not isinstance(data_status, dict)
        or portfolio.get("scan_errors")
    ):
        return False

    diagnostics = data_status.get("owned_position_quote_diagnostics") or []
    legacy_session_limited = bool(
        data_status.get("quotes") == "degraded"
        and diagnostics
        and all(
            isinstance(item, dict) and item.get("status") in {"missing", "stale"}
            for item in diagnostics
        )
    )
    if (
        data_status.get("quotes") != "session_limited"
        and not legacy_session_limited
    ) or data_status.get("quote_request_status", "succeeded") != "succeeded":
        return False
    if data_status.get("invalid_quote_symbols"):
        return False

    historical_status = data_status.get("historical_bars")
    if historical_status in {"stale", "warming_up"}:
        spy_age = data_status.get("spy_daily_age_days")
        return bool(
            data_status.get("fresh_signal_bar_count") in (None, 0)
            and isinstance(spy_age, int)
            and 1 <= spy_age <= int(ALGO_SIGNAL_DATA_CONFIG["max_spy_daily_age_days"])
        )
    return historical_status in {None, "ok"}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _journal_rows(path: Path) -> tuple[list[str], list[dict]]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return [], []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            rows.append({})
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            rows.append({})
    return lines, rows


def _strategy_summary(states: dict) -> dict:
    summary = {}
    for name, by_symbol in states.items():
        phases: dict[str, int] = {}
        symbols: dict[str, list[str]] = {}
        for ticker, state in by_symbol.items():
            phase = str(state.get("phase", "unknown"))
            phases[phase] = phases.get(phase, 0) + 1
            if phase != "flat":
                symbols.setdefault(phase, []).append(str(ticker).upper())
        summary[name] = {
            "phases": phases,
            "symbols": {phase: sorted(tickers) for phase, tickers in symbols.items()},
        }
    return summary


def _trade_updates(
    rows: list[dict],
    cursor: int | None,
    line_count: int,
    alpaca_orders: list[dict],
    previous_statuses: dict,
    *,
    today: str,
    account_id: str | None,
) -> tuple[list[dict], dict[str, str]]:
    if not account_id:
        return [], {}
    if cursor is None or cursor < 0 or cursor > line_count:
        new_rows = [
            row for row in rows
            if str(row.get("timestamp", ""))[:10] == today
            and row.get("trading_mode") in ALGO_JOURNAL_MODES
            and row.get("account_id") == account_id
        ]
    else:
        # Journal rows are append-only. Row growth includes old modes, then we
        # filter by Algo's account route below.
        new_rows = []
        for row_index, row in enumerate(rows):
            if (
                row_index >= cursor
                and row.get("trading_mode") in ALGO_JOURNAL_MODES
                and row.get("account_id") == account_id
            ):
                new_rows.append(row)

    order_map = {str(order.get("id")): order for order in alpaca_orders if order.get("id")}
    journal_ids = {
        str(row.get("order_id")) for row in rows
        if row.get("trading_mode") in ALGO_JOURNAL_MODES
        and row.get("account_id") == account_id and row.get("order_id")
    }
    current_statuses = {
        order_id: str(order.get("status", "unknown"))
        for order_id, order in order_map.items()
        if order_id in journal_ids
    }

    updates = []
    included_order_ids = set()
    for row in new_rows:
        order_id = str(row.get("order_id") or "")
        order = order_map.get(order_id, {})
        updates.append({
            "timestamp": row.get("timestamp"),
            "ticker": row.get("ticker"),
            "action": row.get("action"),
            "qty": row.get("qty"),
            "strategy": row.get("strategy"),
            "journal_status": row.get("status"),
            "broker_status": order.get("status"),
            "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
            "reason": row.get("reasoning") or row.get("error"),
            "order_id": order_id or None,
        })
        if order_id:
            included_order_ids.add(order_id)

    for order_id, status in current_statuses.items():
        prior = previous_statuses.get(order_id)
        if prior is None or prior == status or order_id in included_order_ids:
            continue
        order = order_map[order_id]
        matching = next((
            row for row in rows
            if row.get("account_id") == account_id
            and str(row.get("order_id") or "") == order_id
        ), {})
        updates.append({
            "timestamp": order.get("updated_at") or order.get("submitted_at"),
            "ticker": order.get("symbol") or matching.get("ticker"),
            "action": order.get("side") or matching.get("action"),
            "qty": order.get("qty") or matching.get("qty"),
            "strategy": matching.get("strategy"),
            "journal_status": matching.get("status"),
            "broker_status": status,
            "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
            "reason": "Broker order status changed since the previous report",
            "order_id": order_id,
        })
    updates.sort(key=lambda row: str(row.get("timestamp") or ""))
    return updates, current_statuses


def build_report() -> dict:
    now = datetime.now(timezone.utc)
    today = now.astimezone(ET).date().isoformat()
    data_errors = []
    try:
        market_state, portfolio = AlgoStateStore(STATE_PATH).read()
    except StateStoreError as exc:
        market_state, portfolio = "STATE_ERROR", {}
        data_errors.append(f"Algo coordinator state: {exc}")
    previous = _read_json(SNAPSHOT_PATH)
    universe = portfolio.get("universe") or {}
    symbols = sorted({str(symbol).upper() for symbol in universe.get("tickers", [])})
    previous_symbols = set(previous.get("universe_tickers") or [])

    account_state = {}
    broker = None
    broker_account_id: str | None = None
    alpaca_orders: list[dict] = []
    latest_prices = {}
    try:
        broker = AlgoAlpaca()
        verified_account = broker.account()  # validate that the Algo credentials still identify this account
        broker_account_id = str(verified_account.get("id") or verified_account.get("account_number") or "")
        if not broker_account_id:
            raise ValueError("Alpaca account response omitted its ID")
        account_state = get_portfolio_state(mode="algo")
        if account_state.get("account_id") != broker_account_id:
            raise ValueError("Algo risk snapshot belongs to a different broker account")
        alpaca_orders = broker.orders("all")
        quote_symbols = sorted(set(symbols) | set(account_state.get("positions", {})))
        if quote_symbols:
            latest_prices = broker.latest_prices(quote_symbols, stale_after_seconds=72 * 60 * 60)
    except Exception as exc:
        data_errors.append(f"Algo account data: {type(exc).__name__}: {exc}")

    if not broker_account_id or previous.get("account_id") != broker_account_id:
        previous = {}

    lines, journal = _journal_rows(ROOT / "data" / "trade_journal.jsonl")
    old_statuses = previous.get("order_statuses") or {}
    trade_updates, current_statuses = _trade_updates(
        journal,
        previous.get("journal_line_count"),
        len(lines),
        alpaca_orders,
        old_statuses,
        today=today,
        account_id=broker_account_id,
    )

    previous_prices = previous.get("prices") or {}
    prices = {}
    price_moves = []
    for symbol, (price, timestamp) in latest_prices.items():
        price = float(price)
        prices[symbol] = {"price": price, "timestamp": timestamp.isoformat()}
        prior = previous_prices.get(symbol, {}).get("price")
        if prior:
            change_pct = (price / float(prior) - 1) * 100
            if math.isfinite(change_pct) and abs(change_pct) >= 0.75:
                price_moves.append({"ticker": symbol, "change_pct": round(change_pct, 2), "price": round(price, 2)})
    price_moves.sort(key=lambda row: abs(row["change_pct"]), reverse=True)

    positions = []
    owner_map = portfolio.get("position_owners") or {}
    prior_positions = previous.get("position_prices") or {}
    for symbol, position in sorted((account_state.get("positions") or {}).items()):
        current_price = float(position.get("current_price", 0) or 0)
        entry = float(position.get("avg_entry_price", 0) or 0)
        qty = float(position.get("qty", 0) or 0)
        owner = owner_map.get(symbol)
        previous_price = (prior_positions.get(symbol) or {}).get("price")
        change_since_report = (
            round((current_price / float(previous_price) - 1) * 100, 2)
            if current_price > 0 and previous_price else None
        )
        positions.append({
            "ticker": symbol,
            "strategy": owner or "existing_or_unattributed",
            "qty": qty,
            "avg_entry_price": entry,
            "current_price": current_price,
            "change_since_previous_report_pct": change_since_report,
            "unrealized_pl": round((current_price - entry) * qty, 2),
            "unrealized_pl_pct": round(float(position.get("unrealized_plpc", 0) or 0) * 100, 2),
            "pct_of_equity": round(float(position.get("pct_of_equity", 0) or 0) * 100, 2),
        })

    risk = get_mode_config("algo")["risk"]
    if account_state:
        daily_pnl_pct = float(account_state.get("daily_pnl_pct", 0) or 0)
        raw_weekly_pnl = account_state.get("weekly_pnl_pct")
        weekly_pnl_pct = float(raw_weekly_pnl) if raw_weekly_pnl is not None else None
        risk_summary = {
            "equity": round(float(account_state.get("equity", 0) or 0), 2),
            "daily_baseline_equity": (
                round(float(account_state["daily_baseline_equity"]), 2)
                if account_state.get("daily_baseline_equity") is not None else None
            ),
            "daily_baseline_available": account_state.get("daily_baseline_equity") is not None,
            "daily_pnl_dollars": (
                round(float(account_state["daily_pnl_dollars"]), 2)
                if account_state.get("daily_pnl_dollars") is not None else None
            ),
            "entry_orders_today": account_state.get("entry_count_today"),
            "entry_order_cap": risk["max_trades_per_day"],
            "entry_count_available": account_state.get("entry_count_available", True),
            "entry_count_error": account_state.get("entry_count_error"),
            "all_buy_orders_today": account_state.get("trade_count_today"),
            "open_positions": account_state.get("open_position_count"),
            "max_open_positions": risk["max_open_positions"],
            "cash_pct": round(float(account_state.get("cash_pct", 0) or 0) * 100, 2),
            "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
            "weekly_pnl_pct": round(weekly_pnl_pct * 100, 2) if weekly_pnl_pct is not None else None,
            "weekly_baseline_equity": (
                round(float(account_state["weekly_baseline_equity"]), 2)
                if account_state.get("weekly_baseline_equity") is not None else None
            ),
            "weekly_baseline_date": account_state.get("weekly_baseline_date"),
            "weekly_baseline_kind": account_state.get("weekly_baseline_kind"),
            "weekly_pnl_dollars": (
                round(float(account_state["weekly_pnl_dollars"]), 2)
                if account_state.get("weekly_pnl_dollars") is not None else None
            ),
            "weekly_baseline_available": weekly_pnl_pct is not None,
            "daily_loss_breaker": daily_pnl_pct <= -float(risk["daily_loss_limit"]),
            "no_buy_breaker": daily_pnl_pct <= -float(risk["no_buy_if_down_pct"]),
            "weekly_loss_breaker": (
                weekly_pnl_pct <= -float(risk["weekly_loss_limit"])
                if weekly_pnl_pct is not None else None
            ),
        }
    else:
        risk_summary = {"error": "unavailable"}

    active_units = ["algo-trader.service"] if service_is_active("algo-trader.service") is True else []
    last_scan_at = portfolio.get("last_scan_at")
    last_successful_scan_at = portfolio.get("last_successful_scan_at") or last_scan_at
    expected_session_data_limited = _expected_extended_data_limited(
        portfolio, market_state,
    )
    scan_age_reference = (
        last_scan_at
        if expected_session_data_limited
        else last_successful_scan_at
    )
    scan_age_seconds = None
    if scan_age_reference:
        try:
            last_scan = datetime.fromisoformat(str(scan_age_reference))
            if last_scan.tzinfo is None:
                last_scan = last_scan.replace(tzinfo=ET)
            scan_age_seconds = max(0, int((now - last_scan.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            data_errors.append("Strategy scan timestamp is invalid")
    market_state = market_state
    scan_is_stale = market_state in {"OVERNIGHT", "PREMARKET", "REGULAR", "AFTER_HOURS"} and (
        scan_age_seconds is None
        or scan_age_seconds > 180
        or (
            portfolio.get("last_scan_status") in {"degraded", "error"}
            and not expected_session_data_limited
        )
    )
    report = {
        "reported_at": now.isoformat(),
        "service": {
            "unit": "algo-trader.service",
            "active": bool(active_units),
            "active_units": active_units,
            "persisted_mode": portfolio.get("runtime_mode", "unknown"),
            "market_state": market_state,
            "last_strategy_scan_at": last_scan_at,
            "last_successful_scan_at": last_successful_scan_at,
            "last_scan_status": portfolio.get("last_scan_status", "unknown"),
            "strategy_scan_age_seconds": scan_age_seconds,
            "strategy_scan_stale": scan_is_stale,
            "expected_session_data_limited": expected_session_data_limited,
            "entries_paused": portfolio.get("entries_paused", True),
            "entry_pause_reasons": portfolio.get("entry_pause_reasons") or [],
            "scan_errors": portfolio.get("scan_errors") or [],
        },
        "universe": {
            "selected": len(symbols),
            "target": len(symbols),
            "selection_date": universe.get("selection_date"),
            "comparison_available": bool(previous),
            "tickers": symbols,
            "added_since_previous_report": sorted(set(symbols) - previous_symbols) if previous else [],
            "removed_since_previous_report": sorted(previous_symbols - set(symbols)) if previous else [],
            "price_moves_since_previous_report": price_moves[:5],
        },
        "regime": portfolio.get("market_regime") or {"regime": "unknown", "active": []},
        "active_strategies": portfolio.get("active_strategies", []),
        "strategy_lifecycle": _strategy_summary(portfolio.get("strategy_states") or {}),
        "startup_reconciliation": portfolio.get("startup_reconciliation") or {},
        "unowned_positions": portfolio.get("unowned_positions") or [],
        "data_status": portfolio.get("data_status") or {},
        "positions": positions,
        "risk": risk_summary,
        "orders_since_previous_report": trade_updates,
        "latest_signals": (portfolio.get("last_signals") or [])[:8],
        "data_errors": data_errors,
    }

    next_snapshot = {
        "reported_at": now.isoformat(),
        "account_id": broker_account_id,
        "universe_tickers": symbols,
        "prices": prices,
        "position_prices": {
            position["ticker"]: {"price": position["current_price"]}
            for position in positions if position["current_price"] > 0
        },
        "journal_line_count": len(lines),
        "order_statuses": current_statuses,
    }
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SNAPSHOT_PATH, next_snapshot, indent=2, sort_keys=True)
    return report


if __name__ == "__main__":
    print(json.dumps(build_report(), indent=2, default=str))
