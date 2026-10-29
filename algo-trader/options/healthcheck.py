"""Read-only health check for the options paper daemon and latest risk cycle."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from dotenv import dotenv_values, load_dotenv

from broker.algo import AlgoAlpaca
from broker.options import OptionsPaperClient
from market.sessions import ET, get_trading_window
from options.risk import RISK_SNAPSHOT_PATH
from runtime.paths import DATA_DIR, ROOT
from runtime.systemd import service_is_active


SERVICE = "options.service"
CYCLE_FRESHNESS_SECONDS = 40 * 60
OPTION_SYMBOL = re.compile(r"^[A-Z0-9.]{1,6}\d{6}[CP]\d{8}$")


def _problem(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail[:240]}


def _load_healthcheck_environment() -> None:
    """Load local settings and only the broker credentials this check needs."""
    load_dotenv(ROOT / ".env")
    required = (
        "ALPACA_OPTIONS_API_KEY",
        "ALPACA_OPTIONS_API_SECRET",
        "ALPACA_OPTIONS_ACCOUNT_ID",
        "ALPACA_EQUITY_API_KEY",
        "ALPACA_EQUITY_API_SECRET",
        "ALPACA_EQUITY_ACCOUNT_ID",
    )
    shared_env_path = ROOT.parent / ".env"
    if not shared_env_path.is_file():
        return
    shared_values = dotenv_values(shared_env_path)
    for name in required:
        if not os.environ.get(name) and shared_values.get(name):
            os.environ[name] = str(shared_values[name])


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _read_latest_jsonl(path: Path) -> tuple[dict | None, str | None]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            end = stream.tell()
            if end == 0:
                return None, None
            stream.seek(-min(end, 1024 * 1024), 2)
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, type(exc).__name__

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            return row, None
    return None, "invalid_jsonl"


def _read_risk_snapshot(path: Path, account_id: str) -> tuple[dict | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None, "invalid_schema"
    if str(payload.get("account_id") or "") != account_id:
        return None, "account_mismatch"
    stamp = _timestamp(payload.get("snapshot_at"))
    summary = payload.get("summary")
    if stamp is None or not isinstance(summary, dict):
        return None, "invalid_payload"
    data_quality = summary.get("data_quality")
    if not isinstance(data_quality, list):
        return None, "invalid_data_quality"
    return {"snapshot_at": stamp, "summary": summary}, None


def _cycle_window(window, now_et: datetime) -> bool:
    if window is None or window.name != "regular":
        return False
    earliest = window.regular_open + timedelta(minutes=10)
    latest = window.regular_close - timedelta(minutes=15)
    return earliest <= now_et < latest


def _account_journal_path(account_id: str) -> Path:
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
    return DATA_DIR / "options" / "accounts" / digest / "risk_daily.jsonl"


def build_report(
    *,
    now: datetime | None = None,
    options_client: OptionsPaperClient | None = None,
    algo_client: AlgoAlpaca | None = None,
    service_active: bool | None = None,
    risk_snapshot_path: str | Path = RISK_SNAPSHOT_PATH,
    journal_path: str | Path | None = None,
) -> dict:
    """Verify daemon, broker access, and successful risk-cycle freshness."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Health check requires a timezone-aware clock")
    now_utc = now.astimezone(timezone.utc)
    now_et = now_utc.astimezone(ET)
    problems: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    service_active = service_is_active(SERVICE) if service_active is None else service_active
    if service_active is None:
        problems.append(_problem("service_status_unavailable", "Cannot determine options systemd service status"))
    elif not service_active:
        problems.append(_problem("service_inactive", "Options systemd service is inactive"))

    client = None
    algo = None
    options_account: dict | None = None
    algo_account: dict | None = None
    try:
        client = options_client or OptionsPaperClient()
        options_account = client.verify_account()
    except Exception as exc:
        problems.append(_problem("options_account_unavailable", f"Options paper account verification failed: {type(exc).__name__}: {exc}"))
    try:
        algo = algo_client or AlgoAlpaca()
        algo_account = algo.account()
    except Exception as exc:
        problems.append(_problem("equity_account_unavailable", f"Combined-risk equity account verification failed: {type(exc).__name__}: {exc}"))

    options_account_id = ""
    if options_account:
        options_account_id = str(options_account.get("id") or options_account.get("account_number") or "")
    algo_account_id = ""
    if algo_account:
        algo_account_id = str(algo_account.get("id") or algo_account.get("account_number") or "")
    if options_account_id and algo_account_id and options_account_id == algo_account_id:
        problems.append(_problem("paper_account_mismatch", "Options and equity credentials resolve to the same paper account"))

    positions: list[dict] | None = None
    open_orders: list[dict] | None = None
    option_positions: list[dict] | None = None
    if options_account and client is not None:
        try:
            positions = client.positions()
            if not isinstance(positions, list) or any(not isinstance(row, dict) for row in positions):
                raise ValueError("Options positions response is malformed")
            option_positions = [
                row for row in positions
                if str(row.get("asset_class") or "").lower() in {"option", "us_option"}
                or OPTION_SYMBOL.fullmatch(str(row.get("symbol") or ""))
            ]
        except Exception as exc:
            problems.append(_problem("positions_unavailable", f"Cannot verify options broker positions: {type(exc).__name__}: {exc}"))
        try:
            open_orders = client.orders("open")
            if not isinstance(open_orders, list) or any(not isinstance(row, dict) for row in open_orders):
                raise ValueError("Options open orders response is malformed")
        except Exception as exc:
            problems.append(_problem("orders_unavailable", f"Cannot verify options open orders: {type(exc).__name__}: {exc}"))

    window = None
    calendar_available = False
    if algo is not None and algo_account is not None:
        try:
            calendar = algo.calendar(
                (now_et.date() - timedelta(days=2)).isoformat(),
                (now_et.date() + timedelta(days=14)).isoformat(),
            )
            if not isinstance(calendar, list):
                raise ValueError("Broker calendar response is malformed")
            window = get_trading_window(now_et, calendar)
            calendar_available = True
        except Exception as exc:
            problems.append(_problem("calendar_unavailable", f"Cannot determine options market session: {type(exc).__name__}: {exc}"))

    cycle_expected = calendar_available and _cycle_window(window, now_et)
    market_session = window.name if window else ("closed" if calendar_available else "unknown")
    risk_status = "unavailable"
    risk_age: float | None = None
    risk_payload = None
    if options_account_id:
        risk_payload, risk_error = _read_risk_snapshot(Path(risk_snapshot_path), options_account_id)
        if risk_error is None and risk_payload is not None:
            risk_status = "ok"
            risk_age = max(0.0, (now_utc - risk_payload["snapshot_at"]).total_seconds())
            if (risk_payload["snapshot_at"] - now_utc).total_seconds() > 60:
                problems.append(_problem("risk_snapshot_future_dated", "Options risk snapshot timestamp is in the future"))
            elif risk_age > CYCLE_FRESHNESS_SECONDS and cycle_expected:
                problems.append(_problem("risk_snapshot_stale", f"Options risk snapshot is {risk_age:.0f}s old during the active scan window"))
            elif risk_age > CYCLE_FRESHNESS_SECONDS and not cycle_expected:
                warnings.append(_problem("risk_snapshot_stale", f"Options risk snapshot is {risk_age:.0f}s old outside the active scan window"))
            if risk_payload["summary"].get("data_quality"):
                problems.append(_problem("risk_data_quality", "Latest options risk snapshot reports data-quality issues"))
        else:
            risk_status = risk_error or "invalid"
            if cycle_expected:
                problems.append(_problem("risk_snapshot_unavailable", f"Options risk snapshot is {risk_status}"))
            else:
                warnings.append(_problem("risk_snapshot_unavailable", f"Options risk snapshot is {risk_status}"))

    latest_cycle = None
    latest_cycle_error = None
    cycle_age: float | None = None
    if options_account_id and client is not None:
        latest_cycle, latest_cycle_error = _read_latest_jsonl(
            Path(journal_path) if journal_path else _account_journal_path(client.account_id)
        )
    if latest_cycle is not None:
        if str(latest_cycle.get("account_id") or "") != options_account_id:
            problems.append(_problem("cycle_account_mismatch", "Latest options cycle belongs to a different paper account"))
        cycle_at = _timestamp(latest_cycle.get("logged_at"))
        if cycle_at is None:
            latest_cycle_error = "invalid_timestamp"
        else:
            cycle_age = max(0.0, (now_utc - cycle_at).total_seconds())
            if (cycle_at - now_utc).total_seconds() > 60:
                problems.append(_problem("cycle_future_dated", "Latest options cycle timestamp is in the future"))
        if latest_cycle.get("data_quality"):
            problems.append(_problem("cycle_data_quality", "Latest options cycle reports data-quality issues"))
    if latest_cycle is None or latest_cycle_error:
        detail = latest_cycle_error or "no successful options cycle is recorded"
        if cycle_expected:
            problems.append(_problem("cycle_record_unavailable", f"Options cycle record is unavailable: {detail}"))
        else:
            warnings.append(_problem("cycle_record_unavailable", f"Options cycle record is unavailable: {detail}"))
    elif cycle_expected and cycle_age is not None and cycle_age > CYCLE_FRESHNESS_SECONDS:
        problems.append(_problem("cycle_stale", f"Latest options risk cycle is {cycle_age:.0f}s old during the active scan window"))
    elif not cycle_expected and cycle_age is not None and cycle_age > CYCLE_FRESHNESS_SECONDS:
        warnings.append(_problem("cycle_stale", f"Latest options risk cycle is {cycle_age:.0f}s old outside the active scan window"))

    return {
        "checked_at": now_utc.isoformat(),
        "ok": not problems,
        "service_active": service_active,
        "market_session": market_session,
        "cycle_expected": cycle_expected,
        "last_cycle_age_seconds": round(cycle_age, 1) if cycle_age is not None else None,
        "risk_snapshot_status": risk_status,
        "risk_snapshot_age_seconds": round(risk_age, 1) if risk_age is not None else None,
        "broker_position_count": len(option_positions) if option_positions is not None else None,
        "open_option_order_count": len(open_orders) if open_orders is not None else None,
        "warnings": warnings,
        "problems": problems,
    }


def main() -> None:
    _load_healthcheck_environment()
    try:
        report = build_report()
    except Exception as exc:
        report = {"ok": False, "problems": [_problem("healthcheck_failed", f"Options health check failed: {type(exc).__name__}: {exc}")]}
    print(json.dumps(report, separators=(",", ":"), sort_keys=True, default=str))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
