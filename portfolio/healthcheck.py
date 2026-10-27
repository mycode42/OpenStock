"""Read-only, deterministic health check for the continuous Algo service.

The check uses the SQLite coordinator as its state source and verifies current
exposure against the dedicated Algo paper account. It never submits orders or
modifies coordinator state. OpenClaw can alert on its nonzero exit status.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

from broker.algo import AlgoAlpaca
from portfolio.protection.software import heartbeat_alarm
from market.sessions import ET, TradingWindow, get_trading_window
from portfolio.state_store import AlgoStateStore, StateStoreError
from portfolio.config import ALGO_SERVICE_CONFIG, ALGO_SIGNAL_DATA_CONFIG, CASH_RESERVE, STRATEGY_CONFIG, is_cash_proxy


from runtime.paths import ROOT
from runtime.systemd import service_is_active
STATE_PATH = ROOT / str(ALGO_SERVICE_CONFIG["state_db"])
SERVICE = "algo-trader.service"
WARMUP_GRACE_SECONDS = {"premarket": 10 * 60, "regular": 10 * 60, "overnight": 25 * 60}
WARMUP_PAUSE_REASONS = frozenset({
    "waiting for completed intraday historical bars",
    "no completed intraday historical bars are available yet",
    "the Algo universe has no recent completed signal bars",
    "historical bar scan is warming_up",
})
EMPTY_EXTENDED_DATA_REASONS = WARMUP_PAUSE_REASONS | {
    "the Algo universe has no fresh quotes",
}
SESSION_QUOTE_PAUSE_REASONS = frozenset({
    "owned positions lack fresh protective quotes",
    "the Algo universe has no fresh quotes",
})
EXTENDED_SESSIONS = frozenset({"premarket", "after_hours", "overnight"})
EXTENDED_SESSION_DATA_PAUSE_REASONS = (
    SESSION_QUOTE_PAUSE_REASONS
    | WARMUP_PAUSE_REASONS
    | frozenset({"historical bar scan is stale"})
)
EXPECTED_OVERNIGHT_PAUSE = "overnight entries are disabled by configuration"


def _problem(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail[:240]}


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Existing JSON snapshots may have an ET timestamp without an offset.
    return parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed


def _session_start(window: TradingWindow) -> datetime:
    trade_day = window.regular_open.date()
    if window.name == "overnight":
        return datetime.combine(trade_day - timedelta(days=1), time(20), tzinfo=ET)
    if window.name == "premarket":
        return datetime.combine(trade_day, time(4), tzinfo=ET)
    if window.name == "after_hours":
        return window.regular_close
    return window.regular_open


def _positive(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _signed_quantity(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _quantity(value: object) -> Decimal | None:
    """Parse a nonnegative order quantity; broker position quantities may be signed."""
    number = _signed_quantity(value)
    return number if number is not None and number >= 0 else None


def _expected_data_warmup(
    window: TradingWindow, session_age: int | None, portfolio: dict, pause_reasons: list[str],
) -> bool:
    """Suppress only the normal first incomplete 5-minute bar interval."""
    grace = WARMUP_GRACE_SECONDS.get(window.name)
    data_status = portfolio.get("data_status") or {}
    allowed_pause_reasons = WARMUP_PAUSE_REASONS
    if isinstance(data_status, dict) and data_status.get("quotes") == "session_limited":
        allowed_pause_reasons = allowed_pause_reasons | SESSION_QUOTE_PAUSE_REASONS
    return bool(
        grace is not None
        and session_age is not None
        and 0 <= session_age <= grace
        and isinstance(data_status, dict)
        and data_status.get("historical_bars") == "warming_up"
        and not portfolio.get("scan_errors")
        and pause_reasons
        and all(reason in allowed_pause_reasons for reason in pause_reasons)
    )


def _session_quotes_are_limited(window: TradingWindow, portfolio: dict) -> bool:
    """Identify successful extended-session quote requests with no usable updates."""
    data_status = portfolio.get("data_status") or {}
    diagnostics = data_status.get("owned_position_quote_diagnostics") or [] if isinstance(data_status, dict) else []
    legacy_session_limited = bool(
        isinstance(data_status, dict)
        and data_status.get("quotes") == "degraded"
        and diagnostics
        and all(isinstance(item, dict) and item.get("status") in {"missing", "stale"} for item in diagnostics)
    )
    return bool(
        window.name in EXTENDED_SESSIONS
        and isinstance(data_status, dict)
        and (data_status.get("quotes") == "session_limited" or legacy_session_limited)
        and data_status.get("quote_request_status", "succeeded") == "succeeded"
        and not portfolio.get("scan_errors")
        and not data_status.get("invalid_quote_symbols")
    )


def _expected_extended_session_data(
    window: TradingWindow, portfolio: dict,
) -> bool:
    """Recognize quiet-session quote/bar gaps while preserving real data errors."""
    data_status = portfolio.get("data_status") or {}
    if not isinstance(data_status, dict) or not _session_quotes_are_limited(window, portfolio):
        return False

    historical_status = data_status.get("historical_bars")
    fresh_signal_bar_count = data_status.get("fresh_signal_bar_count")
    if historical_status in {"stale", "warming_up"}:
        spy_age = data_status.get("spy_daily_age_days")
        max_spy_age = int(ALGO_SIGNAL_DATA_CONFIG["max_spy_daily_age_days"])
        if (
            fresh_signal_bar_count not in (None, 0)
            or not isinstance(spy_age, int)
            or not 1 <= spy_age <= max_spy_age
        ):
            return False
    elif historical_status not in {None, "ok"}:
        return False

    return True


def _only_expected_extended_session_data_pause(pause_reasons: list[str]) -> bool:
    # Overnight entry suppression is expected independently of whether
    # extended-session quotes have refreshed for existing holdings.
    data_pause_reasons = [
        reason for reason in pause_reasons
        if reason != EXPECTED_OVERNIGHT_PAUSE
    ]
    return bool(
        data_pause_reasons
        and all(reason in EXTENDED_SESSION_DATA_PAUSE_REASONS for reason in data_pause_reasons)
    )


def _expected_empty_extended_data(
    window: TradingWindow, portfolio: dict, positions: dict[str, dict] | None,
    open_orders: list[dict] | None, pause_reasons: list[str],
) -> bool:
    """No exposure and no free-feed quotes is a paused session, not a fault."""
    data_status = portfolio.get("data_status") or {}
    active_states = any(
        state.get("phase") in {"entry_pending", "open", "exit_pending"}
        for by_symbol in (portfolio.get("strategy_states") or {}).values()
        for state in by_symbol.values()
    )
    return bool(
        window.name in {"premarket", "after_hours"}
        and positions == {}
        and open_orders == []
        and not portfolio.get("position_owners")
        and not active_states
        and (portfolio.get("startup_reconciliation") or {}).get("status") == "verified"
        and portfolio.get("last_scan_status") == "degraded"
        and isinstance(data_status, dict)
        and data_status.get("historical_bars") == "warming_up"
        and data_status.get("quotes") == "unavailable"
        and not portfolio.get("scan_errors")
        and pause_reasons
        and all(reason in EMPTY_EXTENDED_DATA_REASONS for reason in pause_reasons)
    )


def _protection_row_gaps(positions: dict[str, dict], owners: dict[str, str], rows: dict[str, dict]) -> list[str]:
    """Owned, held positions lacking a complete software protection row."""
    gaps: list[str] = []
    for symbol in positions:
        if symbol not in owners:
            continue
        row = rows.get(symbol)
        if not row or row.get("phase") not in {"open", "exit_pending"}:
            gaps.append(symbol)
            continue
        active_stop = row.get("trailing_stop") or row.get("initial_stop")
        if not _positive(active_stop) or not _positive(row.get("target")):
            gaps.append(symbol)
    return sorted(gaps)


def build_report(
    *,
    now: datetime | None = None,
    store: AlgoStateStore | None = None,
    broker: AlgoAlpaca | None = None,
    service_active: bool | None = None,
) -> dict:
    """Return a compact report without changing broker or local state."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Health check requires a timezone-aware clock")
    now_et = now.astimezone(ET)
    problems: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    service_active = service_is_active(SERVICE) if service_active is None else service_active
    if service_active is None:
        problems.append(_problem("service_status_unavailable", "Cannot determine Algo systemd service status"))
    elif not service_active:
        problems.append(_problem("service_inactive", "Algo systemd service is inactive"))

    market_state = "UNKNOWN"
    portfolio: dict = {}
    state_available = False
    try:
        market_state, portfolio = (store or AlgoStateStore(STATE_PATH)).read()
        state_available = True
    except (StateStoreError, OSError, ValueError) as exc:
        problems.append(_problem("sqlite_state_unavailable", f"Cannot read authoritative Algo SQLite state: {exc}"))
    protection_rows: dict[str, dict] = {}
    heartbeat: dict | None = None
    protection_available = False
    if state_available:
        try:
            database = store or AlgoStateStore(STATE_PATH)
            protection_rows = database.read_protection()
            heartbeat = database.read_heartbeat()
            protection_available = True
        except (StateStoreError, OSError, ValueError) as exc:
            problems.append(_problem("sqlite_state_unavailable", f"Cannot read Algo protection rows or heartbeat: {exc}"))
    if market_state in {"CONFIG_ERROR", "DEGRADED", "STATE_ERROR"}:
        problems.append(_problem("coordinator_degraded", f"Persisted Algo coordinator state is {market_state}"))
    incident = portfolio.get("execution_incident") or {}
    incident_status = incident.get("status") if isinstance(incident, dict) else None
    if state_available and incident_status == "open":
        detail = incident.get("reason") or incident.get("detail") or incident.get("error") or "broker reconciliation has not cleared it"
        problems.append(_problem("execution_incident_open", f"Algo execution incident remains open: {detail}"))
    recovery = portfolio.get("protection_recovery_status") or {}
    recovery_status = recovery.get("status") if isinstance(recovery, dict) else None
    recovery_errors = recovery.get("errors") if isinstance(recovery, dict) else None
    if state_available and recovery_status in {"error", "degraded"} and recovery_errors:
        if isinstance(recovery_errors, list):
            detail = "; ".join(str(item) for item in recovery_errors[:2])
        else:
            detail = str(recovery_errors)
        problems.append(_problem("protection_recovery_degraded", f"Algo broker protection repair is {recovery_status}: {detail}"))
    recovery_waiting = recovery.get("waiting") if isinstance(recovery, dict) else None
    recovery_waiting = recovery_waiting if isinstance(recovery_waiting, list) else []
    recovery_waiting_age = recovery.get("waiting_age_seconds") if isinstance(recovery, dict) else None
    if state_available and recovery_waiting and recovery_status in {
        "waiting", "outside_trading_session", "stale_reconciliation", "broker_market_closed",
    }:
        detail = "; ".join(str(item) for item in recovery_waiting[:2])
        age = f"; waiting {int(recovery_waiting_age)}s" if isinstance(recovery_waiting_age, (int, float)) else ""
        problems.append(_problem(
            "protection_recovery_waiting",
            f"Algo broker protection repair is waiting{age}: {detail}",
        ))
    stop_sync = portfolio.get("protection_sync_status") or {}
    stop_sync_status = stop_sync.get("status") if isinstance(stop_sync, dict) else None
    if state_available and stop_sync_status in {"degraded", "waiting"}:
        sync_details = (stop_sync.get("errors") or []) + (stop_sync.get("waiting") or [])
        if sync_details:
            problems.append(_problem(
                "broker_stop_sync_pending",
                "Algo broker stop synchronization needs attention: " + "; ".join(str(item) for item in sync_details[:2]),
            ))

    account_available = False
    window: TradingWindow | None = None
    calendar_available = False
    positions: dict[str, dict] | None = None
    open_orders: list[dict] | None = None
    try:
        broker = broker or AlgoAlpaca()
        broker_account = broker.account()  # Binds these credentials to the configured Algo account.
        account_available = True
        persisted_account_id = portfolio.get("account_id") if state_available else None
        broker_account_id = str(broker_account.get("id") or broker_account.get("account_number") or "")
        if persisted_account_id and persisted_account_id != broker_account_id:
            problems.append(_problem(
                "account_state_mismatch",
                "Persisted Algo strategy state belongs to a different paper account",
            ))
    except Exception as exc:
        problems.append(_problem("broker_account_unavailable", f"Algo paper account verification failed: {type(exc).__name__}: {exc}"))

    if account_available:
        try:
            calendar = broker.calendar(
                (now_et.date() - timedelta(days=2)).isoformat(),
                (now_et.date() + timedelta(days=2)).isoformat(),
            )
            if not isinstance(calendar, list):
                raise ValueError("Broker calendar is not a list")
            window = get_trading_window(now_et, calendar)
            calendar_available = True
        except Exception as exc:
            problems.append(_problem("calendar_unavailable", f"Cannot determine current Algo market session: {type(exc).__name__}: {exc}"))

        try:
            raw_positions = broker.positions()
            if not isinstance(raw_positions, list):
                raise ValueError("Broker positions are not a list")
            raw_positions = [item for item in raw_positions if not is_cash_proxy(item.get("symbol"))]
            positions = {}
            for item in raw_positions:
                if not isinstance(item, dict):
                    raise ValueError("Broker positions include a malformed row")
                symbol = str(item.get("symbol") or "").upper()
                if not symbol or symbol in positions:
                    raise ValueError("Broker positions include a duplicate or unnamed symbol")
                positions[symbol] = item
        except Exception as exc:
            problems.append(_problem("positions_unavailable", f"Cannot verify Algo broker positions: {type(exc).__name__}: {exc}"))

        if positions == {}:
            try:
                open_orders = broker.orders("open")
                if not isinstance(open_orders, list):
                    raise ValueError("Broker open order list is malformed")
                if open_orders:
                    problems.append(_problem(
                        "open_orders_without_positions",
                        "Algo account has open broker orders but no positions",
                    ))
            except Exception as exc:
                problems.append(_problem("orders_unavailable", f"Cannot verify Algo open orders: {type(exc).__name__}: {exc}"))


    scan_at = _timestamp(portfolio.get("last_scan_at")) if state_available else None
    raw_scan_age = int((now - scan_at).total_seconds()) if scan_at else None
    scan_age = max(0, raw_scan_age) if raw_scan_age is not None else None
    scan_limit = max(180, 3 * int(STRATEGY_CONFIG["portfolio_scan_seconds"]))
    active_window = calendar_available and window is not None
    session_age = int((now_et - _session_start(window)).total_seconds()) if active_window else None
    scan_status = portfolio.get("last_scan_status") if state_available else None
    pause_reasons = [str(reason) for reason in portfolio.get("entry_pause_reasons") or []]
    expected_idle_data = False
    if state_available and portfolio.get("last_scan_at") and scan_at is None:
        problems.append(_problem("scan_timestamp_invalid", "Persisted Algo scan timestamp is invalid"))
    if raw_scan_age is not None and raw_scan_age < -scan_limit:
        problems.append(_problem("scan_timestamp_future", "Persisted Algo scan timestamp is in the future"))
    if active_window and state_available:
        # Let a new session begin before treating the previous session's scan
        # and pause reason as a current fault.
        startup_grace_passed = session_age is not None and session_age > scan_limit
        scanned_this_window = scan_at is not None and scan_at.astimezone(ET) >= _session_start(window)
        expected_warmup = bool(
            scanned_this_window and scan_status == "degraded"
            and _expected_data_warmup(window, session_age, portfolio, pause_reasons)
        )
        expected_idle_data = bool(
            scanned_this_window
            and _expected_empty_extended_data(window, portfolio, positions, open_orders, pause_reasons)
        )
        expected_policy_pause = bool(
            scanned_this_window
            and scan_status == "ok"
            and window.name == "overnight"
            and pause_reasons
            and all(reason == EXPECTED_OVERNIGHT_PAUSE for reason in pause_reasons)
        )
        expected_extended_session_data = bool(
            scanned_this_window
            and _expected_extended_session_data(window, portfolio)
        )
        expected_extended_session_bar_gap = bool(
            expected_extended_session_data
            and (portfolio.get("data_status") or {}).get("historical_bars") in {"stale", "warming_up"}
        )
        expected_extended_session_data_pause = bool(
            expected_extended_session_data
            and _only_expected_extended_session_data_pause(pause_reasons)
        )
        if scanned_this_window and _session_quotes_are_limited(window, portfolio):
            diagnostics = (portfolio.get("data_status") or {}).get("owned_position_quote_diagnostics") or []
            affected = sorted(
                str(item.get("symbol")) for item in diagnostics
                if isinstance(item, dict) and item.get("status") != "fresh" and item.get("symbol")
            )
            symbols = (" for " + ", ".join(affected)) if affected else ""
            historical_limited = (
                " and no recent completed signal bars"
                if (portfolio.get("data_status") or {}).get("historical_bars") in {"stale", "warming_up"}
                else ""
            )
            warnings.append(_problem(
                "session_limited_quotes",
                f"Extended-session market data has no quote update within the freshness window{symbols}{historical_limited}; entries remain paused until fresh data arrives",
            ))
        if (scan_age is None or scan_age > scan_limit) and startup_grace_passed:
            problems.append(_problem("scan_stale", f"Algo scan age exceeds {scan_limit}s in the {window.name} session"))
        if (
            (startup_grace_passed or scanned_this_window)
            and scan_status in {"degraded", "error"}
            and not (expected_warmup or expected_idle_data or expected_extended_session_bar_gap)
        ):
            problems.append(_problem("scan_degraded", f"Last Algo scan status is {scan_status}"))
        if (
            (startup_grace_passed or scanned_this_window)
            and portfolio.get("entries_paused")
            and not (
                expected_warmup
                or expected_idle_data
                or expected_policy_pause
                or expected_extended_session_data_pause
            )
        ):
            visible_pause_reasons = [
                reason for reason in pause_reasons
                if not (
                    expected_extended_session_data
                    and reason in EXTENDED_SESSION_DATA_PAUSE_REASONS
                )
            ]
            detail = "; ".join(visible_pause_reasons) or "entry pause has no recorded reason"
            problems.append(_problem("entries_paused", f"Algo entries are paused: {detail}"))

    unowned: list[str] = []
    unsupported_positions: list[str] = []
    unproven_positions: list[str] = []
    software_stop_gaps: list[str] = []
    protection_gaps: list[str] = []
    if positions is not None:
        unsupported_positions = sorted(
            symbol for symbol, position in positions.items()
            if (quantity := _signed_quantity(position.get("qty"))) is None or quantity == 0
        )
        if unsupported_positions:
            problems.append(_problem("unsupported_positions", "Algo account has zero or invalid broker positions: " + ", ".join(unsupported_positions)))
    if positions is not None and state_available:
        owners = portfolio.get("position_owners") or {}
        states = portfolio.get("strategy_states") or {}
        unowned = sorted(set(positions) - set(owners))
        if unowned:
            problems.append(_problem("unowned_positions", "Broker positions have no persisted Algo owner: " + ", ".join(unowned)))
        unproven_positions = sorted(
            symbol for symbol in positions
            if symbol in owners
            and not (states.get(owners[symbol]) or {}).get(symbol, {}).get("entry_order_id")
        )
        if unproven_positions:
            problems.append(_problem("entry_provenance_missing", "Owned broker positions lack an exact persisted entry order ID: " + ", ".join(unproven_positions)))
        software_stop_gaps = sorted(
            symbol for symbol in positions
            if symbol in owners and not _positive((states.get(owners[symbol]) or {}).get(symbol, {}).get("stop_price"))
        )
        if software_stop_gaps:
            problems.append(_problem("software_stops_missing", "Owned positions have no valid software stop: " + ", ".join(software_stop_gaps)))
        protection_gaps = _protection_row_gaps(positions, owners, protection_rows) if protection_available else []
        if protection_gaps:
            problems.append(_problem("software_protection_missing", "Owned positions lack a complete software protection row: " + ", ".join(protection_gaps)))
        startup = portfolio.get("startup_reconciliation") or {}
        if positions and startup.get("status") in {"degraded", "error"}:
            problems.append(_problem("startup_reconciliation_degraded", "Broker attribution or protection did not verify at coordinator startup"))

    heartbeat_detail = None
    if protection_available:
        open_count = len(positions) if positions is not None else len(protection_rows)
        heartbeat_detail = heartbeat_alarm(heartbeat=heartbeat, open_position_count=open_count, now=now)
        if heartbeat_detail:
            problems.append(_problem(
                "daemon_heartbeat_stale",
                f"Algo daemon has not scanned while {heartbeat_detail['open_positions']} position(s) are open "
                f"({heartbeat_detail['kind']}, age {heartbeat_detail['age_seconds']}s, limit {heartbeat_detail['limit_seconds']}s)",
            ))
    data_status = portfolio.get("data_status") if state_available else {}
    if not isinstance(data_status, dict):
        data_status = {}
    quote_diagnostics = data_status.get("owned_position_quote_diagnostics") or []
    if not isinstance(quote_diagnostics, list):
        quote_diagnostics = []

    return {
        "checked_at": now.isoformat(),
        "ok": not problems,
        "service_active": service_active,
        "market_session": window.name if window else "closed" if calendar_available else "unknown",
        "market_state": market_state,
        "sqlite_state_available": state_available,
        "last_scan_at": portfolio.get("last_scan_at") if state_available else None,
        "last_scan_age_seconds": scan_age,
        "scan_freshness_limit_seconds": scan_limit,
        "last_scan_status": scan_status,
        "market_data_idle": expected_idle_data,
        "session_limited_quotes": any(item.get("code") == "session_limited_quotes" for item in warnings),
        "entries_paused": portfolio.get("entries_paused") if state_available else None,
        "entry_pause_reasons": pause_reasons,
        "execution_incident_status": incident_status,
        "protection_recovery_status": recovery_status,
        "protection_recovery_waiting": recovery_waiting,
        "protection_recovery_waiting_age_seconds": recovery_waiting_age,
        "broker_stop_sync_status": stop_sync_status,
        "broker_position_count": len(positions) if positions is not None else None,
        "unsupported_positions": unsupported_positions,
        "unowned_positions": unowned,
        "entry_provenance_missing": unproven_positions,
        "software_stops_missing": software_stop_gaps,
        "owned_position_quote_diagnostics": quote_diagnostics,
        "protection_row_gaps": protection_gaps,
        "daemon_heartbeat": heartbeat,
        "daemon_heartbeat_alarm": heartbeat_detail,
        "warnings": warnings,
        "problems": problems,
    }


def main() -> None:
    load_dotenv(ROOT / ".env")
    try:
        report = build_report()
    except Exception as exc:
        report = {"ok": False, "problems": [_problem("healthcheck_failed", f"Health check failed: {type(exc).__name__}: {exc}")]}
    print(json.dumps(report, separators=(",", ":"), sort_keys=True, default=str))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
