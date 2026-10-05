"""Long-running, paper-only Opening Dip service."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import DIP_CONFIG
from src.dip.alpaca import DipAccountMismatchError, DipAlpaca
from src.dip.strategy import DipConfig, DipEngine


ET = ZoneInfo("America/New_York")
LOG = logging.getLogger("swarm_trader.dip")
STOP = False


def _config() -> DipConfig:
    return DipConfig(
        dip_percent=float(DIP_CONFIG["dip_percent"]),
        confirmation_seconds=int(DIP_CONFIG["confirmation_seconds"]),
        target_percent=float(DIP_CONFIG["target_percent"]),
        stop_loss_percent=float(DIP_CONFIG["stop_loss_percent"]),
        reentry_enabled=bool(DIP_CONFIG["reentry_enabled"]),
        minimum_dip_from_last_sell_percent=float(DIP_CONFIG["minimum_dip_from_last_sell_percent"]),
        max_opening_gap_percent=DIP_CONFIG["max_opening_gap_percent"],
        max_trades_per_day=int(DIP_CONFIG["max_trades_per_day"]),
        max_trades_per_instrument_per_day=int(DIP_CONFIG["max_trades_per_instrument_per_day"]),
        max_position_size=float(DIP_CONFIG["max_position_size"]),
        max_total_exposure=float(DIP_CONFIG["max_total_exposure"]),
        max_daily_loss=DIP_CONFIG["max_daily_loss"],
        reference=str(DIP_CONFIG["reference"]),
    )


def _state_path() -> Path:
    path = Path(DIP_CONFIG["state_file"])
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def _load_engine(path: Path) -> DipEngine:
    try:
        return DipEngine.restore(_config(), json.loads(path.read_text()))
    except FileNotFoundError:
        return DipEngine(_config())


def _save(engine: DipEngine, path: Path, market_state: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    snapshot = engine.snapshot()
    if market_state is not None:
        snapshot["market_state"] = market_state
    temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    os.replace(temporary, path)


def _status(path: Path) -> None:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raw = {"session_date": None, "market_state": "UNKNOWN", "total_trades_today": 0, "realized_pnl_today": 0.0, "instruments": {}}
    for symbol, instrument in raw.get("instruments", {}).items():
        price = instrument.get("current_price")
        reference = instrument.get("reference_price")
        entry = instrument.get("entry_price")
        qty = int(instrument.get("quantity", 0) or 0)
        instrument["dip_percent"] = ((float(price) / float(reference)) - 1) * 100 if price and reference else None
        instrument["unrealized_pnl"] = (float(price) - float(entry)) * qty if price and entry and qty else 0.0
        status = instrument.get("status", "PRE_OPEN")
        instrument["next_action"] = {
            "POSITION_OPEN": "monitor target/stop",
            "PENDING_BUY": "reconcile buy order",
            "PENDING_SELL": "reconcile sell order",
            "WAITING_FOR_DIP": "wait for confirmed dip",
            "WAITING_FOR_NEXT_DIP": "wait for re-entry threshold",
            "WINDOW_EXPIRED": "no new entries today",
        }.get(status, "wait for market session")
    try:
        active = subprocess.run(["systemctl", "is-active", "--quiet", "swarm-trader-dip.service"], check=False).returncode == 0
    except OSError:
        active = None
    print(json.dumps({
        "service": "swarm-trader-dip",
        "service_active": active,
        "enabled": DIP_CONFIG["enabled"],
        "market_timezone": "America/New_York",
        "universe": DIP_CONFIG["universe"],
        **raw,
    }, indent=2))


def _parse_hhmm(value: str, day: datetime) -> datetime:
    hour, minute = map(int, value.split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _client_id(day: str, symbol: str, side: str, trade_no: int) -> str:
    return f"dip-{day.replace('-', '')}-{symbol.lower()}-{side[0]}-{trade_no}"[:48]


def run_loop(*, dry_run: bool = False, max_cycles: int | None = None, broker_client=None) -> None:
    global STOP
    STOP = False
    state_path = _state_path()
    engine = _load_engine(state_path)
    symbols = [str(s).upper() for s in DIP_CONFIG["universe"]]
    for symbol in symbols:
        engine.state(symbol)

    broker = broker_client if broker_client is not None else (None if dry_run else DipAlpaca())
    if str(DIP_CONFIG["reference"]) != "previous_close":
        raise ValueError("Dip currently supports reference='previous_close' only")
    cycle = 0
    last_skip: dict[str, str] = {}
    last_loop_error: str | None = None
    logged_session_start: str | None = None
    while not STOP and (max_cycles is None or cycle < max_cycles):
        cycle += 1
        try:
            now = datetime.now(ET)
            session_day = now.date().isoformat()
            if broker is None:
                raise RuntimeError("Dry-run daemon needs injected data; use --simulate for offline verification")
            calendar = broker.calendar((now.date() - timedelta(days=7)).isoformat(), (now.date() + timedelta(days=14)).isoformat())
            today = next((row for row in calendar if row.get("date") == session_day), None)
            if today is None:
                LOG.info("[DIP] No market session today; waiting for next session")
                _save(engine, state_path, "CLOSED")
                _sleep_before_next_session(now, calendar)
                continue

            market_open_at = datetime.fromisoformat(f"{session_day}T{today['open']}").replace(tzinfo=ET)
            market_close_at = datetime.fromisoformat(f"{session_day}T{today['close']}").replace(tzinfo=ET)
            start_setting = str(DIP_CONFIG["start"])
            trading_start_at = market_open_at if start_setting == "market_open" else _parse_hhmm(start_setting, now)
            start_at = trading_start_at - timedelta(minutes=int(DIP_CONFIG["pre_open_minutes"]))
            stop_at = _parse_hhmm(str(DIP_CONFIG["stop"]), now)
            if now < start_at:
                _save(engine, state_path, "PRE_OPEN")
                time.sleep(min(300, max(1, (start_at - now).total_seconds())))
                continue
            if now >= market_close_at:
                _save(engine, state_path, "CLOSED")
                _sleep_before_next_session(now, calendar)
                continue

            engine.begin_session(session_day)
            account = broker.account()  # validate account before market-data work or order handling
            # Capture references during pre-open, independent of the other modes.
            for symbol in symbols:
                state = engine.state(symbol)
                if state.reference_price is None or state.reference_date != session_day:
                    try:
                        ref = broker.previous_close(symbol, session_day)
                        engine.set_reference(symbol, ref, session_day)
                        state.session_date = session_day
                        LOG.info("[DIP] %s reference price: %.4f", symbol, ref)
                    except Exception as exc:
                        _log_skip(last_skip, symbol, f"reference unavailable: {exc}")

            positions = broker.positions()
            orders = broker.orders("open")
            pos_by_symbol = {p["symbol"]: p for p in positions}
            open_by_symbol = {o["symbol"]: o for o in orders}
            exposure = sum(abs(float(p.get("market_value", 0))) for p in positions if p.get("symbol") in symbols)
            buying_power = float(account.get("buying_power", 0))
            now_open = now >= market_open_at and now < market_close_at
            in_window = max(market_open_at, trading_start_at) <= now <= min(stop_at, market_close_at)
            market_state = "PRE_OPEN" if not now_open else ("ENTRY_WINDOW" if in_window else "POSITION_MONITORING")
            if now_open and logged_session_start != session_day:
                LOG.info("[DIP] Market session starting for %s; entry window ends at %s ET", session_day, stop_at.strftime("%H:%M"))
                logged_session_start = session_day
            prices = {}
            price_error = None
            if now_open:
                try:
                    prices = broker.latest_prices(symbols)
                except Exception as exc:
                    price_error = exc
            for symbol in symbols:
                state = engine.state(symbol)
                # Resolve a persisted client order ID first, including a fill that
                # completed just before a restart and is no longer an open order.
                if state.pending_client_order_id:
                    try:
                        order = broker.order_by_client_id(state.pending_client_order_id)
                        if order:
                            engine.order_update(symbol, order)
                    except Exception as exc:
                        _log_skip(last_skip, symbol, f"order reconciliation unavailable: {exc}")
                remote_order = open_by_symbol.get(symbol)
                if remote_order:
                    engine.set_pending(symbol, remote_order["side"], remote_order.get("client_order_id", "recovered"), remote_order.get("id"))
                remote_position = pos_by_symbol.get(symbol)
                if remote_position:
                    qty = int(float(remote_position.get("qty", 0)))
                    entry = float(remote_position.get("avg_entry_price", 0))
                    engine.mark_existing_position(symbol, qty, entry)
                if state.pending_order_id and state.pending_side and not remote_order:
                    try:
                        order = broker.order(state.pending_order_id)
                        engine.order_update(symbol, order)
                    except Exception:
                        pass
                if not now_open:
                    continue
                try:
                    if price_error:
                        raise price_error
                    price, traded_at = prices[symbol]
                    fresh = (datetime.now(traded_at.tzinfo) - traded_at).total_seconds() <= int(DIP_CONFIG["stale_data_seconds"])
                except Exception as exc:
                    _log_skip(last_skip, symbol, f"market data unavailable: {exc}")
                    continue
                action = engine.on_price(
                    symbol, price, traded_at,
                    in_trading_window=in_window,
                    market_open=now_open,
                    data_fresh=fresh,
                    existing_position=bool(remote_position),
                    pending_order=bool(remote_order),
                    available_buying_power=buying_power,
                    total_exposure=exposure,
                    daily_loss=(float(account.get("equity", 0)) - float(account.get("last_equity", account.get("equity", 0)))),
                )
                if not fresh:
                    _log_skip(last_skip, symbol, "market data is stale")
                if action:
                    if action["action"] == "buy":
                        qty = action["quantity"]
                        trade_no = state.trades_today + 1
                        client_id = _client_id(session_day, symbol, "buy", trade_no)
                        LOG.info("[DIP] %s dip confirmed at %.4f; BUY %s shares", symbol, price, qty)
                    else:
                        qty = state.quantity
                        trade_no = max(1, state.trades_today)
                        client_id = _client_id(session_day, symbol, "sell", trade_no)
                        LOG.info("[DIP] %s %s reached at %.4f; SELL %s shares", symbol, action["reason"], price, qty)
                    # Persist deterministic id before network call; retries first query that ID.
                    engine.set_pending(symbol, action["action"], client_id)
                    _save(engine, state_path, market_state)
                    try:
                        existing = broker.order_by_client_id(client_id)
                        order = existing or broker.submit_market_order(symbol, action["action"], qty, client_id)
                        engine.set_pending(symbol, action["action"], client_id, order.get("id"))
                        engine.order_update(symbol, order)
                        order_status = str(order.get("status", "unknown")).lower()
                        if order_status == "filled":
                            LOG.info("[DIP] %s %s filled %s shares at %.4f", symbol, action["action"].upper(), order.get("filled_qty", qty), float(order.get("filled_avg_price") or price))
                            if action["action"] == "sell" and DIP_CONFIG["reentry_enabled"]:
                                LOG.info("[DIP] %s eligible for re-entry after a qualifying dip", symbol)
                        elif order_status == "partially_filled":
                            LOG.info("[DIP] %s %s partially filled %s/%s shares", symbol, action["action"].upper(), order.get("filled_qty", 0), qty)
                        else:
                            LOG.info("[DIP] %s %s submitted as order %s; status=%s", symbol, action["action"].upper(), order.get("id", client_id), order_status)
                    except Exception as exc:
                        LOG.error("[DIP] %s order submission/reconciliation failed: %s", symbol, exc)
                elif state.last_reason:
                    _log_skip(last_skip, symbol, state.last_reason)
            _save(engine, state_path, market_state)
            last_loop_error = None
        except DipAccountMismatchError as exc:
            LOG.error("[DIP] %s; stopping until ALPACA_DIP_ACCOUNT_ID and Dip API credentials identify the same paper account", exc)
            _save(engine, state_path, "CONFIG_ERROR")
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != last_loop_error:
                LOG.exception("[DIP] Temporary API/loop failure; retrying")
                last_loop_error = message
        if not STOP and (max_cycles is None or cycle < max_cycles):
            time.sleep(max(1, int(DIP_CONFIG["poll_seconds"])))


def _log_skip(cache: dict[str, str], symbol: str, reason: str) -> None:
    if cache.get(symbol) != reason:
        LOG.info("[DIP] %s entry/action skipped: %s", symbol, reason)
        cache[symbol] = reason


def _sleep_before_next_session(now: datetime, calendar: list[dict]) -> None:
    upcoming = sorted((row for row in calendar if row.get("date", "") > now.date().isoformat()), key=lambda row: row["date"])
    if upcoming:
        next_open = datetime.fromisoformat(f"{upcoming[0]['date']}T{upcoming[0]['open']}").replace(tzinfo=ET)
        wake_at = next_open - timedelta(minutes=int(DIP_CONFIG["pre_open_minutes"]))
        seconds = max(60, (wake_at - now).total_seconds())
    else:
        seconds = 3600
    time.sleep(min(3600, seconds))


def simulate() -> None:
    """Offline synthetic sequence: open, dip, buy, recovery, sell, re-entry."""
    from datetime import timezone
    from src.dip.strategy import DipConfig, DipEngine

    engine = DipEngine(DipConfig(confirmation_seconds=30, max_position_size=5000, max_total_exposure=10000))
    engine.begin_session("2026-10-05")
    engine.set_reference("NVDA", 100)
    base = datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc)
    seq = [(100, 0), (98.8, 1), (98.8, 32), (100.3, 40), (100.3, 41), (99.4, 50), (99.4, 81)]
    for price, seconds in seq:
        event = engine.on_price("NVDA", price, base + timedelta(seconds=seconds), in_trading_window=True,
                                market_open=True, available_buying_power=5000, total_exposure=0)
        if event:
            print(json.dumps(event))
            if event["action"] == "buy":
                engine.set_pending("NVDA", "buy", f"sim-buy-{seconds}")
                engine.order_update("NVDA", {"side": "buy", "status": "filled", "filled_qty": str(event["quantity"]), "filled_avg_price": str(price)})
            else:
                engine.set_pending("NVDA", "sell", f"sim-sell-{seconds}")
                engine.order_update("NVDA", {"side": "sell", "status": "filled", "filled_qty": str(engine.state("NVDA").quantity), "filled_avg_price": str(price)})
    print(json.dumps(engine.snapshot(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Dip Mode paper-trading daemon")
    parser.add_argument("--status", action="store_true", help="show last persisted Dip state")
    parser.add_argument("--simulate", action="store_true", help="run offline synthetic sequence")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.status:
        _status(_state_path())
        return
    if args.simulate:
        simulate()
        return
    if not DIP_CONFIG["enabled"]:
        LOG.info("[DIP] disabled by src/config.py; service will exit without placing orders")
        return
    lock_path = Path("/run/lock/swarm-trader-dip.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("[DIP] another daemon instance holds the lock")
            raise SystemExit(3)
        signal.signal(signal.SIGTERM, lambda *_: _request_stop())
        signal.signal(signal.SIGINT, lambda *_: _request_stop())
        run_loop()


def _request_stop() -> None:
    global STOP
    STOP = True
    LOG.info("[DIP] shutdown requested; saving state on clean exit")


if __name__ == "__main__":
    main()
