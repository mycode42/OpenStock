#!/usr/bin/env python3
"""Run the whole ThetaData history backfill, unattended, until it is finished.

Order, for every research symbol:
  1. the last 20 SPY sessions (so options trading has recent history at once),
  2. the full EOD archive from 2020 (the existing backfill, resumable),
  3. the opening-quote archive (the opening backfill, resumable).

Each daily run resumes where the last one stopped. Once both archives cover yesterday for every symbol, the run
records completion, sends one Telegram message asking for the subscription to be cancelled and the key removed, and
never calls ThetaData again. Status is written to pipeline_status.json for the research page.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv

from market.sessions import ET
from runtime.paths import ROOT
from scripts.llm_report import send_telegram
from scripts.options import theta_opening_backfill as opening
from scripts.options.theta_historical_backfill import (
    FIRST_DATA_DATE,
    LOCK_PATH,
    RESEARCH_DIR,
    _atomic_json,
    _effective_start_date,
    _read_api_key,
    _research_symbols,
    _run_symbols,
    _theta_client,
)

STATUS_PATH = RESEARCH_DIR / "pipeline_status.json"
RECENT_SESSIONS = 20

CANCEL_MESSAGE = (
    "ThetaData history backfill is complete for all {count} research symbols (EOD and opening archives "
    "through {end}). Next steps: cancel the ThetaData subscription, then remove THETA_DATA_API_KEY from .env. "
    "The pipeline will not call ThetaData again."
)


def _load_status() -> dict[str, Any]:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_status(status: dict[str, Any]) -> None:
    status["updated_at"] = datetime.now(ET).isoformat()
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_json(STATUS_PATH, status)


def _archive_complete(path: Path) -> bool:
    """The archive writer sets this only after every month in its planned range is downloaded.

    The planned range ends at the newest session ThetaData has published, which can lag yesterday by several
    sessions, so the check does not compare against yesterday.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("archive_complete") is True
    except (OSError, ValueError):
        return False


def eod_complete(symbol: str) -> bool:
    return _archive_complete(RESEARCH_DIR / symbol / "manifest.json")


def opening_complete(symbol: str) -> bool:
    return _archive_complete(RESEARCH_DIR / symbol / "opening_manifest.json")


def _yesterday() -> date:
    return datetime.now(ET).date() - timedelta(days=1)


def _redact(message: str) -> str:
    try:
        key = _read_api_key()
    except Exception:
        return message[:400]
    return message.replace(key, "[redacted]")[:400]


def _notify(text: str) -> str | None:
    """Send to Telegram. A delivery failure is recorded, not raised, so the backfill keeps its own state."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("ALGO_REPORT_TELEGRAM_TO") or os.getenv("TELEGRAM_CHAT_ID") or ""
    if not (token and chat_id):
        return "telegram is not configured"
    try:
        send_telegram(text, token, chat_id)
    except Exception as error:
        return f"telegram failed: {type(error).__name__}"
    return None


def run(*, end_date: date | None = None) -> int:
    end = end_date or _yesterday()
    status = _load_status()
    if status.get("phase") == "complete":
        # Completion is final: later days come from the Alpaca capture, and ThetaData is never called again.
        if status.get("notify_pending"):
            return _complete(status, int(status.get("symbols", 0)), date.fromisoformat(status["end_date"]))
        print(f"ThetaData pipeline already complete through {status.get('end_date')}; nothing to do.")
        return 0

    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ThetaData backfill is already running; this pipeline run skipped.")
            return 0
        return _run_locked(end)


def _run_locked(end: date) -> int:
    symbols = _research_symbols()
    status = _load_status()
    status.update({"end_date": end.isoformat(), "symbols": len(symbols), "last_error": None})
    _save_status(status)

    try:
        if not all(eod_complete(symbol) for symbol in symbols):
            status["phase"] = "eod" if status.get("recent_pass_complete_at") else "eod_recent"
            _save_status(status)

            def recent_done() -> None:
                status["recent_pass_complete_at"] = datetime.now(ET).isoformat()
                status["phase"] = "eod"
                _save_status(status)

            code = _run_symbols(
                symbols, FIRST_DATA_DATE, end, recent_sessions=RECENT_SESSIONS, on_recent_complete=recent_done,
            )
            if code != 0 or not all(eod_complete(symbol) for symbol in symbols):
                return _fail(status, "EOD backfill is not complete yet; the next run resumes it")

        if not all(opening_complete(symbol) for symbol in symbols):
            status["phase"] = "opening"
            _save_status(status)
            client = _theta_client(_read_api_key())
            failures = []
            for symbol in symbols:
                if opening_complete(symbol):
                    continue
                try:
                    opening._fetch_symbol(
                        client, symbol,
                        start_date=_effective_start_date(symbol, FIRST_DATA_DATE), end_date=end,
                    )
                except Exception as error:
                    failures.append(f"{symbol}: {type(error).__name__}")
                    print(f"ThetaData opening {symbol} stopped: {_redact(str(error))}", flush=True)
            if failures or not all(opening_complete(symbol) for symbol in symbols):
                return _fail(status, f"opening backfill is not complete yet ({', '.join(failures) or 'resuming'})")

        return _complete(status, len(symbols), end)
    except Exception as error:
        return _fail(status, f"{type(error).__name__}: {_redact(str(error))}")


def _complete(status: dict[str, Any], count: int, end: date) -> int:
    status.update({
        "phase": "complete",
        "completed_at": status.get("completed_at") or datetime.now(ET).isoformat(),
        "last_error": None,
    })
    if not status.get("completion_notified_at"):
        problem = _notify(CANCEL_MESSAGE.format(count=count, end=end.isoformat()))
        if problem is None:
            status.pop("notify_pending", None)
            status["completion_notified_at"] = datetime.now(ET).isoformat()
        else:
            status["notify_pending"] = True
            status["notify_error"] = problem
            print(f"ThetaData completion notice not sent: {problem}")
    _save_status(status)
    print(f"ThetaData pipeline complete through {end}.")
    return 0


def _fail(status: dict[str, Any], message: str) -> int:
    message = _redact(message)
    status["last_error"] = message
    if status.get("error_notified") != message:
        problem = _notify(f"ThetaData backfill stopped: {message}. It resumes on the next scheduled run.")
        if problem is None:
            status["error_notified"] = message
    _save_status(status)
    print(f"ThetaData pipeline stopped: {message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT.parent / ".env", override=False)
    parser = argparse.ArgumentParser(description="Run the ThetaData history backfill until complete")
    parser.add_argument("--status", action="store_true", help="print the pipeline status and exit")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(_load_status(), indent=2, sort_keys=True))
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
