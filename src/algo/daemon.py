"""Continuous regime-selected, rule-based Algo strategy portfolio service."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.algo.alpaca import AlgoAccountMismatchError, AlgoAlpaca
from src.algo.incidents import open_incident_symbols, record_incident
from src.algo.protection_recovery import recover_broker_protection
from src.algo.reconciliation import reconcile_startup
from src.algo.sessions import ET, get_trading_window, next_session_start
from src.algo.state_store import AlgoStateStore, StateStoreError
from src.algo.config import ALGO_SERVICE_CONFIG
from src.config import STRATEGY_CONFIG
from src.algo.strategies.runner import execute_trades as execute_strategy_trades
from src.algo.strategies.runner import scan as scan_strategy_portfolio
from src.algo.strategies.state_machine import ACTIVE_PHASES, RuleStrategyStateMachine
from src.algo.strategies.universe import configured_universe, load_algo_universe


LOG = logging.getLogger("swarm_trader.algo")
STOP = False


@dataclass(frozen=True)
class ReconciliationOrders:
    all_orders: list[dict]
    nested_orders: list[dict]
    exact_entry_orders: dict[str, dict]
    exact_incident_orders: dict[str, dict]


def _state_path() -> Path:
    path = Path(ALGO_SERVICE_CONFIG["state_file"])
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def _load_portfolio_state(path: Path) -> dict:
    return AlgoStateStore(path).load()[1]


def _save(path: Path, market_state: str, portfolio_state: dict) -> None:
    AlgoStateStore(path).save(market_state, portfolio_state)


def _load_reconciliation_orders(portfolio: dict, broker) -> ReconciliationOrders:
    """Load order history and exact references needed for safe reconciliation."""
    all_orders = broker.orders("all")
    recent_order_ids = {str(order.get("id")) for order in all_orders}
    exact_entry_orders = {}
    if len(all_orders) >= 500:
        for ticker, owner in portfolio["position_owners"].items():
            state = (portfolio["strategy_states"].get(owner) or {}).get(ticker, {})
            entry_id = state.get("entry_order_id")
            if entry_id and entry_id not in recent_order_ids:
                try:
                    exact_entry_orders[entry_id] = broker.order(entry_id)
                except Exception as exc:
                    LOG.warning("[ALGO] Exact entry lookup failed for %s: %s", ticker, exc)

    nested_page = broker.orders("all", nested=True)
    if not isinstance(nested_page, list):
        raise ValueError("Broker nested order history is malformed")
    exact_nested: dict[str, dict] = {}
    for ticker, owner in portfolio["position_owners"].items():
        state = (portfolio["strategy_states"].get(owner) or {}).get(ticker, {})
        relevant_ids = [state.get("entry_order_id")]
        recovery = state.get("protection_recovery") or {}
        if isinstance(recovery, dict):
            relevant_ids.append(recovery.get("order_id"))
            if recovery.get("client_order_id") and not recovery.get("order_id"):
                found = broker.order_by_client_id(recovery["client_order_id"])
                if found:
                    relevant_ids.append(found.get("id"))
        for order_id in relevant_ids:
            if not order_id or order_id in exact_nested:
                continue
            exact = broker.order(str(order_id), nested=True)
            if not isinstance(exact, dict) or str(exact.get("id") or "") != str(order_id):
                raise ValueError(f"Exact nested broker order {order_id} is unavailable")
            exact_nested[str(order_id)] = exact
    nested_orders = list(exact_nested.values()) + [
        order for order in nested_page
        if isinstance(order, dict) and str(order.get("id") or "") not in exact_nested
    ]

    exact_incident_orders = {}
    incident = portfolio.get("execution_incident") or {}
    for event in (incident.get("events") or {}).values():
        if not isinstance(event, dict) or not event.get("client_order_id"):
            continue
        found = broker.order_by_client_id(event["client_order_id"])
        if found:
            exact_incident_orders[event["client_order_id"]] = found
    return ReconciliationOrders(
        all_orders=all_orders,
        nested_orders=nested_orders,
        exact_entry_orders=exact_entry_orders,
        exact_incident_orders=exact_incident_orders,
    )


def _reconcile_broker_state(portfolio: dict, broker, store, state_path: Path, window) -> float:
    """Reconcile persisted ownership and protection against one broker snapshot."""
    try:
        orders = _load_reconciliation_orders(portfolio, broker)
        report = reconcile_startup(
            portfolio,
            positions=broker.positions(),
            all_orders=orders.all_orders,
            nested_orders=orders.nested_orders,
            open_orders=broker.orders("open"),
            journal_path=state_path.parent / "trade_journal.jsonl",
            exact_entry_orders=orders.exact_entry_orders,
            exact_incident_orders=orders.exact_incident_orders,
            require_broker_stops=window.name == "regular",
        )
        if not report["unprotected_positions"]:
            portfolio["protection_recovery_status"] = {
                "status": "not_needed", "checked_at": datetime.now(ET).isoformat(),
                "submitted": [], "verified": [], "waiting": [], "errors": [],
            }
        store.save(window.name.upper(), portfolio)
        checked_monotonic = time.monotonic()
        startup_verified = report["status"] == "verified"
        if startup_verified:
            LOG.info(
                "[ALGO] Startup reconciliation verified %d positions; recovered %d owners",
                len(report["verified_positions"]), len(report["recovered_owners"]),
            )
        else:
            LOG.error("[ALGO] Startup reconciliation degraded: %s", "; ".join(report["entry_pause_reasons"]))
        return checked_monotonic
    except Exception as exc:
        portfolio["entries_paused"] = True
        portfolio["entry_pause_reasons"] = [f"startup broker reconciliation unavailable: {type(exc).__name__}: {exc}"]
        portfolio["startup_reconciliation"] = {
            "status": "error", "entries_paused": True,
            "entry_pause_reasons": portfolio["entry_pause_reasons"],
            "checked_at": datetime.now(ET).isoformat(),
        }
        store.save("DEGRADED", portfolio)
        checked_monotonic = time.monotonic()
        LOG.exception("[ALGO] Startup reconciliation failed; new entries paused")
        return checked_monotonic


def _status(path: Path) -> None:
    state_error = None
    try:
        raw = AlgoStateStore(path).read_status()
    except StateStoreError as exc:
        raw = {"market_state": "STATE_ERROR", "strategy_portfolio": {}}
        state_error = str(exc)
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "swarm-trader-algo.service"],
            check=False,
            timeout=5,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        active = None
    portfolio = raw.get("strategy_portfolio") or {}
    universe = portfolio.get("universe") or {}
    print(json.dumps({
        "service": "swarm-trader-algo.service",
        "service_active": active,
        "runtime_mode": portfolio.get("runtime_mode", "algo"),
        "market_state": raw.get("market_state", "UNKNOWN"),
        "market_timezone": "America/New_York",
        "universe": universe.get("tickers", []),
        "state_error": state_error,
        **raw,
    }, indent=2))
    if state_error:
        raise SystemExit(2)


def _scan_strategy_portfolio(
    portfolio: dict,
    *,
    window,
    persist_before_execution=None,
    dry_run: bool = False,
) -> dict:
    """Evaluate the selected strategies and execute via the shared risk path."""
    scan_result = scan_strategy_portfolio(
        mode="algo",
        universe_result=portfolio.get("universe") or {"tickers": []},
        position_owners=portfolio.setdefault("position_owners", {}),
        strategy_states=portfolio.setdefault("strategy_states", {}),
        session_name=window.name,
        trading_date=window.trade_date,
        extended_hours=window.extended_hours,
        regular_open=getattr(window, "regular_open", None),
        regular_close=getattr(window, "regular_close", None),
        data_feed=window.bar_feed,
        quote_feed=window.quote_feed,
        overnight_tradable_tickers=set(
            (portfolio.get("universe") or {}).get("overnight_tradable_tickers", [])
        ),
    )
    portfolio["market_regime"] = scan_result.get("market_regime", {"regime": "unknown"})
    portfolio["active_strategies"] = list(portfolio["market_regime"].get("active", []))
    portfolio["last_signals"] = scan_result.get("signals", [])
    portfolio["multi_factor_execution_mode"] = "live"
    portfolio["multi_factor_parameter_profile"] = scan_result.get("multi_factor_parameter_profile")
    portfolio["last_scan_at"] = datetime.now(ET).isoformat()
    portfolio["last_execution"] = None
    portfolio["scan_errors"] = list(scan_result.get("scan_errors") or [])
    portfolio["data_status"] = scan_result.get("data_status") or {}
    historical_status = portfolio["data_status"].get("historical_bars")
    portfolio["last_scan_status"] = "degraded" if portfolio["scan_errors"] or historical_status != "ok" else "ok"
    if portfolio["last_scan_status"] == "ok":
        portfolio["last_successful_scan_at"] = portfolio["last_scan_at"]
    startup = portfolio.get("startup_reconciliation") or {}
    startup_reasons = startup.get("entry_pause_reasons") or []
    scan_reasons = scan_result.get("entry_pause_reasons") or []
    portfolio["entry_pause_reasons"] = list(dict.fromkeys([*startup_reasons, *scan_reasons]))
    portfolio["entries_paused"] = bool(startup.get("entries_paused") or scan_result.get("entries_paused"))
    if historical_status != "ok":
        portfolio["entries_paused"] = True
        reason = f"historical bar scan is {historical_status or 'unavailable'}"
        if reason not in portfolio["entry_pause_reasons"]:
            portfolio["entry_pause_reasons"].append(reason)
    portfolio["unowned_positions"] = sorted(set(startup.get("unowned_positions") or []) | set(scan_result.get("unowned_positions") or []))
    if portfolio["unowned_positions"] and not portfolio["entries_paused"]:
        portfolio["entries_paused"] = True
        portfolio["entry_pause_reasons"].append("Algo has unattributed broker holdings")
    unproven_owners = sorted(
        ticker for ticker, owner in portfolio["position_owners"].items()
        if (portfolio["strategy_states"].get(owner) or {}).get(ticker, {}).get("phase") in ACTIVE_PHASES
        and not (portfolio["strategy_states"].get(owner) or {}).get(ticker, {}).get("entry_order_id")
    )
    if unproven_owners:
        portfolio["entries_paused"] = True
        portfolio["entry_pause_reasons"].append(
            "Algo entry provenance is unresolved: " + ", ".join(unproven_owners)
        )
    incident_symbols = open_incident_symbols(portfolio)
    if incident_symbols:
        portfolio["entries_paused"] = True
        portfolio["last_scan_status"] = "degraded"
        portfolio["entry_pause_reasons"].append(
            "Algo execution incident needs broker resolution: " + ", ".join(incident_symbols)
        )

    trades = [
        trade for trade in scan_result.get("trades", [])
        if not (portfolio["entries_paused"] and str(trade.get("action", "")).lower() in {"buy", "short"})
    ]
    if any(str(trade.get("action", "")).lower() in {"sell", "cover"} for trade in trades):
        # Defer all entries until the reducing orders have been examined.
        trades = [trade for trade in trades if str(trade.get("action", "")).lower() in {"sell", "cover"}]
    if trades:
        machine = RuleStrategyStateMachine(
            portfolio["strategy_states"], portfolio["position_owners"],
        )
        for trade in trades:
            machine.prepare(trade)
        if persist_before_execution:
            persist_before_execution()
        try:
            execution = execute_strategy_trades(trades, mode="algo", dry_run=dry_run)
        except Exception as exc:
            for trade in trades:
                if not dry_run and str(trade.get("action") or "").lower() in {"sell", "cover"}:
                    record_incident(
                        portfolio, kind="exit_submission", ticker=trade["ticker"],
                        client_order_id=trade.get("client_order_id"),
                        reason=f"executor call failed: {type(exc).__name__}: {exc}",
                    )
            if persist_before_execution:
                persist_before_execution()
            raise
        if not isinstance(execution, dict) or not isinstance(execution.get("results"), list):
            execution = {"status": "invalid_response", "results": []}
            portfolio["entries_paused"] = True
            portfolio["last_scan_status"] = "error"
            portfolio["entry_pause_reasons"].append("Algo executor returned a malformed result")
        portfolio["last_execution"] = execution
        execution_rows = execution["results"]
        results_by_symbol_action = {
            (str(item.get("ticker", "")).upper(), str(item.get("action", "")).lower()): item
            for item in (execution_rows if isinstance(execution_rows, list) else [])
            if isinstance(item, dict)
        }
        for trade in trades:
            order_result = results_by_symbol_action.get((trade["ticker"].upper(), trade["action"].lower()))
            machine.resolve(
                trade,
                order_result,
            )
            if not dry_run and str(trade.get("action") or "").lower() in {"sell", "cover"} and not (
                order_result and order_result.get("status") == "submitted" and order_result.get("order_id")
                and order_result.get("success") is not False
            ):
                reason = (order_result or {}).get("error") or (order_result or {}).get("reason") or "executor did not confirm a position-reducing order"
                record_incident(
                    portfolio, kind="exit_submission", ticker=trade["ticker"],
                    client_order_id=trade.get("client_order_id"),
                    order_id=(order_result or {}).get("order_id"), reason=str(reason),
                )
                portfolio["entries_paused"] = True
                portfolio["last_scan_status"] = "degraded"
                issue = "Algo exit submission failed; broker resolution required"
                if issue not in portfolio["entry_pause_reasons"]:
                    portfolio["entry_pause_reasons"].append(issue)
        if persist_before_execution:
            persist_before_execution()
    return scan_result


def run_loop(
    *,
    dry_run: bool = False,
    max_cycles: int | None = None,
    broker_client=None,
    mode: str = "algo",
) -> None:
    global STOP
    if mode != "algo":
        raise ValueError("Daemon mode must be 'algo'")
    STOP = False
    state_path = _state_path()
    store = AlgoStateStore(state_path)
    _, portfolio = store.load()
    broker = broker_client if broker_client is not None else AlgoAlpaca()
    coordinator_token = secrets.token_urlsafe(32)
    coordinator_pid = os.getpid()
    store.activate_coordinator(coordinator_token, coordinator_pid)
    os.environ["SWARM_ALGO_COORDINATOR_TOKEN"] = coordinator_token
    cycle = 0
    last_scan_monotonic: float | None = None
    last_universe_attempt_monotonic: float | None = None
    last_reconciliation_monotonic: float | None = None
    last_recovery_monotonic: float | None = None
    last_window_name: str | None = None
    last_loop_error: str | None = None

    try:
        while not STOP and (max_cycles is None or cycle < max_cycles):
            cycle += 1
            try:
                now = datetime.now(ET)
                calendar = broker.calendar(
                    (now.date() - timedelta(days=2)).isoformat(),
                    (now.date() + timedelta(days=14)).isoformat(),
                )
                window = get_trading_window(now, calendar)
                if window is None:
                    last_window_name = None
                    store.save("CLOSED", portfolio)
                    if not STOP and (max_cycles is None or cycle < max_cycles):
                        _sleep_before_next_session(now, calendar)
                    continue

                if window.name != last_window_name:
                    last_reconciliation_monotonic = None
                    last_window_name = window.name

                account = broker.account()  # validates that Algo keys identify the configured account
                broker_account_id = str(account.get("id") or account.get("account_number") or "")
                if not broker_account_id:
                    raise AlgoAccountMismatchError("Alpaca omitted the Algo broker account ID")
                persisted_account_id = portfolio.get("account_id")
                if persisted_account_id and persisted_account_id != broker_account_id:
                    raise AlgoAccountMismatchError(
                        "Persisted Algo strategy state belongs to a different paper account"
                    )
                portfolio["account_id"] = broker_account_id
                retry_seconds = max(15, int(STRATEGY_CONFIG.get("universe_retry_seconds", 300)))
                if (
                    last_reconciliation_monotonic is None
                    or time.monotonic() - last_reconciliation_monotonic >= retry_seconds
                ):
                    last_reconciliation_monotonic = _reconcile_broker_state(
                        portfolio, broker, store, state_path, window,
                    )

                # Restore broker-hosted protection before the strategy scan
                # can consider new entries or software exits. A stale OCO
                # parent is not protection until its stop child is verified.
                if (
                    not dry_run
                    and window.name == "regular"
                    and (portfolio.get("startup_reconciliation") or {}).get("unprotected_positions")
                    and (last_recovery_monotonic is None or time.monotonic() - last_recovery_monotonic >= 60)
                ):
                    last_recovery_monotonic = time.monotonic()
                    try:
                        recovery = recover_broker_protection(
                            portfolio, window=window, broker=broker,
                            save=lambda: store.save(window.name.upper(), portfolio),
                            execute=execute_strategy_trades, now=datetime.now(ET),
                        )
                        if recovery.get("submitted") or recovery.get("verified"):
                            LOG.info(
                                "[ALGO] Broker stop repair submitted=%s verified=%s waiting=%s",
                                recovery["submitted"], recovery["verified"], recovery["waiting"],
                            )
                        if recovery.get("errors"):
                            LOG.error("[ALGO] Broker stop repair degraded: %s", "; ".join(recovery["errors"]))
                        if recovery.get("verified"):
                            last_reconciliation_monotonic = None
                    except Exception as exc:
                        reason = f"broker stop repair unavailable: {type(exc).__name__}: {exc}"
                        portfolio["protection_recovery_status"] = {
                            "status": "error", "checked_at": now.isoformat(),
                            "submitted": [], "verified": [], "waiting": [], "errors": [reason],
                        }
                        portfolio["entries_paused"] = True
                        portfolio["entry_pause_reasons"] = list(dict.fromkeys([
                            *(portfolio.get("entry_pause_reasons") or []), reason,
                        ]))
                        LOG.exception("[ALGO] Broker stop repair failed; new entries paused")
                session_day = window.trade_date
                if portfolio.get("session_date") != session_day:
                    portfolio["session_date"] = session_day
                    portfolio["universe"] = None
                    portfolio["active_strategies"] = []
                    portfolio["market_regime"] = {
                        "regime": "unknown",
                        "reason": "waiting for today's completed-bar scan",
                        "active": [],
                    }

                universe = portfolio.get("universe") or {}
                fixed_tickers = configured_universe("algo")["tickers"]
                universe_due = (
                    universe.get("selection_date") != session_day
                    or universe.get("tickers") != fixed_tickers
                )
                universe_retry_due = (
                    last_universe_attempt_monotonic is None
                    or time.monotonic() - last_universe_attempt_monotonic >= retry_seconds
                )
                if universe_due and universe_retry_due:
                    last_universe_attempt_monotonic = time.monotonic()
                    try:
                        portfolio["universe"] = load_algo_universe()
                        portfolio["universe"]["selection_date"] = session_day
                        LOG.info(
                            "[ALGO] Fixed universe loaded: %d tickers",
                            len(portfolio["universe"].get("tickers", [])),
                        )
                    except Exception as exc:
                        portfolio["universe"] = {
                            "tickers": [], "selection_date": session_day,
                            "error": str(exc), "overnight_tradable_tickers": [],
                        }
                        LOG.exception("[ALGO] Universe refresh failed; new entries paused")

                scan_interval = int(STRATEGY_CONFIG["portfolio_scan_seconds"])
                if last_scan_monotonic is None or time.monotonic() - last_scan_monotonic >= scan_interval:
                    prior_regime = (portfolio.get("market_regime") or {}).get("regime")
                    market_state = window.name.upper()
                    save_before_execution = lambda: store.save(market_state, portfolio)
                    try:
                        result = _scan_strategy_portfolio(
                            portfolio,
                            window=window,
                            persist_before_execution=save_before_execution,
                            dry_run=dry_run,
                        )
                        new_regime = (portfolio.get("market_regime") or {}).get("regime")
                        if new_regime != prior_regime:
                            LOG.info(
                                "[ALGO] Session=%s regime=%s active=%s (%s)",
                                window.name,
                                new_regime,
                                ",".join(portfolio.get("active_strategies", [])) or "none",
                                (portfolio.get("market_regime") or {}).get("reason", ""),
                            )
                        if portfolio.get("last_execution"):
                            LOG.info(
                                "[ALGO] Shared executor handled %d strategy orders",
                                len(portfolio["last_execution"].get("results", [])),
                            )
                        if result.get("universe", {}).get("tickers"):
                            portfolio["universe"] = result["universe"]
                        if any(
                            state.get("phase") in ACTIVE_PHASES
                            and not state.get("entry_order_id")
                            for by_symbol in portfolio["strategy_states"].values()
                            for state in by_symbol.values()
                        ):
                            last_reconciliation_monotonic = None
                    except Exception as exc:
                        portfolio["active_strategies"] = []
                        portfolio["entries_paused"] = True
                        portfolio["entry_pause_reasons"] = [f"strategy scan failed: {type(exc).__name__}: {exc}"]
                        portfolio["scan_errors"] = portfolio["entry_pause_reasons"]
                        portfolio["last_scan_status"] = "error"
                        portfolio["market_regime"] = {
                            "regime": (portfolio.get("market_regime") or {}).get("regime", "unknown"),
                            "reason": f"strategy scan failed; new entries paused: {exc}",
                            "active": [],
                        }
                        LOG.exception("[ALGO] Rule scan failed; new entries paused")
                        last_scan_monotonic = time.monotonic() - scan_interval + min(
                            15, max(1, int(ALGO_SERVICE_CONFIG["poll_seconds"]))
                        )
                    else:
                        last_scan_monotonic = time.monotonic()

                store.save(window.name.upper(), portfolio)
                last_loop_error = None
            except AlgoAccountMismatchError as exc:
                LOG.error("[ALGO] %s; stopping until Algo account credentials and account ID agree", exc)
                portfolio["entries_paused"] = True
                portfolio["entry_pause_reasons"] = [str(exc)]
                store.save("CONFIG_ERROR", portfolio)
                return
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                portfolio["entries_paused"] = True
                portfolio["entry_pause_reasons"] = [f"Algo loop failure: {message}"]
                portfolio["last_scan_status"] = "error"
                try:
                    store.save("DEGRADED", portfolio)
                except StateStoreError:
                    LOG.exception("[ALGO] Failed to persist degraded loop status")
                if message != last_loop_error:
                    LOG.exception("[ALGO] Temporary API/loop failure; retrying")
                    last_loop_error = message

            if not STOP and (max_cycles is None or cycle < max_cycles):
                time.sleep(max(1, int(ALGO_SERVICE_CONFIG["poll_seconds"])))
    finally:
        store.clear_coordinator(coordinator_token, coordinator_pid)
        os.environ.pop("SWARM_ALGO_COORDINATOR_TOKEN", None)


def _sleep_before_next_session(now: datetime, calendar: list[dict]) -> None:
    wake_at = next_session_start(now, calendar)
    seconds = max(1, (wake_at - now).total_seconds()) if wake_at else 3600
    time.sleep(min(3600, seconds))


def _request_stop() -> None:
    global STOP
    STOP = True
    LOG.info("[ALGO] shutdown requested; saving state on clean exit")


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuous rule-based strategy portfolio daemon")
    parser.add_argument("--status", action="store_true", help="show the last persisted Algo portfolio state")
    parser.add_argument("--mode", choices=["algo"], default="algo", help="run the Algo strategy portfolio")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.status:
        _status(_state_path())
        return

    lock_path = Path(ALGO_SERVICE_CONFIG["lock_file"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("[ALGO] another daemon instance holds the lock")
            raise SystemExit(3)
        signal.signal(signal.SIGTERM, lambda *_: _request_stop())
        signal.signal(signal.SIGINT, lambda *_: _request_stop())
        run_loop(mode="algo")


if __name__ == "__main__":
    main()
