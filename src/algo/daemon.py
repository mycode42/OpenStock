"""Continuous regime-selected, rule-based Algo strategy portfolio service."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.algo.alpaca import AlgoAccountMismatchError, AlgoAlpaca
from src.algo.sessions import ET, get_trading_window, next_session_start
from src.config import ALGO_SERVICE_CONFIG, STRATEGY_CONFIG
from src.strategies.runner import execute_trades as execute_strategy_trades
from src.strategies.runner import scan as scan_strategy_portfolio
from src.strategies.state_machine import RuleStrategyStateMachine
from src.strategies.universe import select_universe


LOG = logging.getLogger("swarm_trader.algo")
STOP = False


def _state_path() -> Path:
    path = Path(ALGO_SERVICE_CONFIG["state_file"])
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def _load_portfolio_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    portfolio = raw.get("strategy_portfolio", {})
    return {
        "session_date": portfolio.get("session_date"),
        "universe": portfolio.get("universe"),
        "active_strategies": portfolio.get("active_strategies", []),
        "market_regime": portfolio.get("market_regime", {"regime": "unknown", "reason": "awaiting first scan"}),
        "position_owners": portfolio.get("position_owners", {}),
        "strategy_states": portfolio.get("strategy_states", {}),
        "last_scan_at": portfolio.get("last_scan_at"),
        "last_signals": portfolio.get("last_signals", []),
        "last_execution": portfolio.get("last_execution"),
        "runtime_mode": "algo",
    }


def _save(path: Path, market_state: str, portfolio_state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({
        "market_state": market_state,
        "strategy_portfolio": portfolio_state,
    }, indent=2, sort_keys=True))
    os.replace(temporary, path)


def _status(path: Path) -> None:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {"market_state": "UNKNOWN", "strategy_portfolio": {}}
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
        **raw,
    }, indent=2))


def _scan_strategy_portfolio(
    portfolio: dict,
    *,
    window,
    persist_before_execution=None,
    dry_run: bool = False,
) -> dict:
    """Evaluate the selected strategies and execute via the shared risk path."""
    result = scan_strategy_portfolio(
        mode="algo",
        universe_result=portfolio.get("universe") or {"tickers": []},
        position_owners=portfolio.setdefault("position_owners", {}),
        strategy_states=portfolio.setdefault("strategy_states", {}),
        session_name=window.name,
        trading_date=window.trade_date,
        extended_hours=window.extended_hours,
        data_feed=window.feed,
        overnight_tradable_tickers=set(
            (portfolio.get("universe") or {}).get("overnight_tradable_tickers", [])
        ),
    )
    portfolio["market_regime"] = result.get("market_regime", {"regime": "unknown"})
    portfolio["active_strategies"] = list(portfolio["market_regime"].get("active", []))
    portfolio["last_signals"] = result.get("signals", [])
    portfolio["last_scan_at"] = datetime.now(ET).isoformat()
    portfolio["last_execution"] = None

    if result.get("trades"):
        machine = RuleStrategyStateMachine(
            portfolio["strategy_states"], portfolio["position_owners"],
        )
        for trade in result["trades"]:
            machine.prepare(trade)
        if persist_before_execution:
            persist_before_execution()
        execution = execute_strategy_trades(result["trades"], mode="algo", dry_run=dry_run)
        portfolio["last_execution"] = execution
        results_by_symbol_action = {
            (str(item.get("ticker", "")).upper(), str(item.get("action", "")).lower()): item
            for item in execution.get("results", [])
        }
        for trade in result["trades"]:
            machine.resolve(
                trade,
                results_by_symbol_action.get((trade["ticker"].upper(), trade["action"].lower())),
            )
        if persist_before_execution:
            persist_before_execution()
    return result


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
    portfolio = _load_portfolio_state(state_path)
    broker = broker_client if broker_client is not None else AlgoAlpaca()
    cycle = 0
    last_scan_monotonic = 0.0
    last_loop_error: str | None = None

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
                _save(state_path, "CLOSED", portfolio)
                if not STOP and (max_cycles is None or cycle < max_cycles):
                    _sleep_before_next_session(now, calendar)
                continue

            account = broker.account()  # validates that Algo keys identify the configured account
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
            if universe.get("selection_date") != session_day:
                try:
                    portfolio["universe"] = select_universe(
                        "algo",
                        max_tickers=int(STRATEGY_CONFIG["universe_target"]),
                    )
                    portfolio["universe"]["selection_date"] = session_day
                    LOG.info(
                        "[ALGO] Universe refreshed: %d/%d tickers",
                        portfolio["universe"].get("selected_count", 0),
                        portfolio["universe"].get("target_count", STRATEGY_CONFIG["universe_target"]),
                    )
                except Exception as exc:
                    portfolio["universe"] = {
                        "tickers": [], "selection_date": session_day,
                        "error": str(exc), "overnight_tradable_tickers": [],
                    }
                    LOG.exception("[ALGO] Universe refresh failed; new entries paused")

            scan_interval = int(STRATEGY_CONFIG["portfolio_scan_seconds"])
            if time.monotonic() - last_scan_monotonic >= scan_interval:
                prior_regime = (portfolio.get("market_regime") or {}).get("regime")
                market_state = window.name.upper()
                save_before_execution = lambda: _save(state_path, market_state, portfolio)
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
                except Exception as exc:
                    portfolio["active_strategies"] = []
                    portfolio["market_regime"] = {
                        "regime": (portfolio.get("market_regime") or {}).get("regime", "unknown"),
                        "reason": f"strategy scan failed; new entries paused: {exc}",
                        "active": [],
                    }
                    LOG.exception("[ALGO] Rule scan failed; new entries paused")
                last_scan_monotonic = time.monotonic()

            _save(state_path, window.name.upper(), portfolio)
            last_loop_error = None
        except AlgoAccountMismatchError as exc:
            LOG.error("[ALGO] %s; stopping until Algo account credentials and account ID agree", exc)
            _save(state_path, "CONFIG_ERROR", portfolio)
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != last_loop_error:
                LOG.exception("[ALGO] Temporary API/loop failure; retrying")
                last_loop_error = message

        if not STOP and (max_cycles is None or cycle < max_cycles):
            time.sleep(max(1, int(ALGO_SERVICE_CONFIG["poll_seconds"])))


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
