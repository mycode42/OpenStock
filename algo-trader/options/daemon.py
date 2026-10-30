"""Market-hours daemon for the dedicated Alpaca options paper account.

The daemon reuses the existing paper coordinator and its combined-account risk
checks. Orders can only reach Alpaca's paper endpoint through OptionsPaperClient.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from broker.algo import AlgoAlpaca
from broker.options import OptionsPaperClient
from market.sessions import ET, get_trading_window
from options.paper import run_paper_cycle
from options.snapshots import OptionsSnapshotSchedule
from runtime.paths import DATA_DIR


LOG = logging.getLogger("options.paper.daemon")
STOP = threading.Event()
CALENDAR_REFRESH_SECONDS = 6 * 60 * 60
CALENDAR_RETRY_SECONDS = 60
SESSION_POLL_SECONDS = 60


def _inside_entry_window(
    now: datetime,
    window,
    *,
    opening_delay_minutes: int,
    entry_cutoff_minutes: int,
) -> bool:
    """Allow new paper entries only in the regular session window."""
    if window is None or window.name != "regular":
        return False
    earliest = window.regular_open + timedelta(minutes=opening_delay_minutes)
    latest = window.regular_close - timedelta(minutes=entry_cutoff_minutes)
    return earliest <= now.astimezone(ET) < latest


def _rejection_lines(report: dict, limit: int = 10) -> list[str]:
    """One log line per rejected candidate, with the risk or policy reason it was rejected for."""
    lines = []
    for row in report.get("decisions") or []:
        if not isinstance(row, dict) or row.get("approved"):
            continue
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        lines.append(
            "Options candidate rejected: underlying=%s strategy=%s structure=%s reason=%s"
            % (
                signal.get("underlying") or "?",
                signal.get("strategy") or "?",
                signal.get("structure_type") or "?",
                row.get("reason") or "no reason recorded",
            )
        )
    if len(lines) > limit:
        lines = lines[:limit] + [f"Options candidate rejections truncated: {len(lines) - limit} more not shown"]
    return lines


def _cycle_summary(report: dict) -> tuple[int, int]:
    decisions = report.get("decisions") or []
    approved = sum(bool(row.get("approved")) for row in decisions if isinstance(row, dict))
    submitted = sum(
        bool((row.get("broker_order") or {}).get("id"))
        for row in decisions if isinstance(row, dict)
    )
    return approved, submitted


def run_loop(
    *,
    submit: bool = False,
    config_path: str | Path | None = None,
    interval_seconds: int = 900,
    opening_delay_minutes: int = 10,
    entry_cutoff_minutes: int = 15,
    options_client: OptionsPaperClient | None = None,
    algo_client: AlgoAlpaca | None = None,
) -> None:
    """Scan the account every interval during the regular US options session.

    Calendar lookup failures fail closed: no scan or order is attempted until
    the regular session can be confirmed from Alpaca's market calendar.
    """
    if interval_seconds < 60:
        raise ValueError("interval_seconds must be at least 60")
    if opening_delay_minutes < 0 or entry_cutoff_minutes < 0:
        raise ValueError("session delays cannot be negative")
    STOP.clear()

    client = options_client or OptionsPaperClient()
    algo = algo_client or AlgoAlpaca()
    options_account = client.verify_account()
    algo_account = algo.account()
    options_account_id = str(options_account.get("id") or options_account.get("account_number") or "")
    algo_account_id = str(algo_account.get("id") or algo_account.get("account_number") or "")
    if not options_account_id or not algo_account_id or options_account_id == algo_account_id:
        raise RuntimeError("options and Algo credentials must resolve to distinct, identified paper accounts")
    digest = hashlib.sha256(client.account_id.encode("utf-8")).hexdigest()[:16]
    lock_dir = DATA_DIR / "options" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"daemon-{digest}.lock"

    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another options paper daemon already owns this account") from exc

        LOG.info(
            "Options paper daemon started: submit=%s interval=%ss opening_delay=%sm entry_cutoff=%sm",
            submit, interval_seconds, opening_delay_minutes, entry_cutoff_minutes,
        )
        calendar = None
        calendar_day = None
        calendar_loaded_at = 0.0
        next_cycle_at = 0.0
        snapshots = OptionsSnapshotSchedule(client)

        try:
            while not STOP.is_set():
                now = datetime.now(ET)
                monotonic_now = time.monotonic()
                if (
                    calendar is None
                    or calendar_day != now.date()
                    or monotonic_now - calendar_loaded_at >= CALENDAR_REFRESH_SECONDS
                ):
                    try:
                        calendar = algo.calendar(
                            (now.date() - timedelta(days=2)).isoformat(),
                            (now.date() + timedelta(days=14)).isoformat(),
                        )
                        calendar_day = now.date()
                        calendar_loaded_at = monotonic_now
                    except Exception:
                        calendar = None
                        calendar_day = None
                        LOG.exception("Options daemon could not refresh the market calendar; entries remain paused")
                        STOP.wait(CALENDAR_RETRY_SECONDS)
                        continue

                window = get_trading_window(now, calendar)
                try:
                    captured = snapshots.tick(now, window)
                    if captured:
                        LOG.info("Options %s equity snapshot saved", captured)
                except Exception:
                    LOG.exception("Options equity snapshot failed; the trading loop continues")
                if window is None or window.name != "regular":
                    STOP.wait(SESSION_POLL_SECONDS)
                    continue

                if not _inside_entry_window(
                    now,
                    window,
                    opening_delay_minutes=opening_delay_minutes,
                    entry_cutoff_minutes=entry_cutoff_minutes,
                ):
                    STOP.wait(SESSION_POLL_SECONDS)
                    continue

                if monotonic_now >= next_cycle_at:
                    # Set the next attempt before calling the broker so a slow
                    # or failing scan cannot cause a tight retry loop.
                    next_cycle_at = monotonic_now + interval_seconds
                    try:
                        report = run_paper_cycle(
                            submit=submit,
                            config_path=config_path,
                            options_client=client,
                            algo_client=algo,
                            now=datetime.now(timezone.utc),
                        )
                        approved, submitted = _cycle_summary(report)
                        LOG.info(
                            "Options paper cycle complete: candidates=%d approved=%d submitted=%d regime=%s volatility=%s",
                            len(report.get("decisions") or []),
                            approved,
                            submitted,
                            report.get("regime", "unknown"),
                            report.get("volatility_state", "unknown"),
                        )
                        for line in _rejection_lines(report):
                            LOG.info(line)
                    except Exception:
                        LOG.exception("Options paper cycle failed; risk gates remain in force and the daemon will retry")

                wait_seconds = min(SESSION_POLL_SECONDS, max(1, next_cycle_at - time.monotonic()))
                STOP.wait(wait_seconds)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            LOG.info("Options paper daemon stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description="Regular-session daemon for options paper trading")
    parser.add_argument("--submit", action="store_true", help="submit approved orders to the fixed Alpaca paper endpoint")
    parser.add_argument("--config", type=Path, help="options JSON config override")
    parser.add_argument("--interval-seconds", type=int, default=900, help="time between scans during market hours")
    parser.add_argument("--opening-delay-minutes", type=int, default=10, help="wait after the regular open before scanning")
    parser.add_argument("--entry-cutoff-minutes", type=int, default=15, help="stop submitting new entries this long before close")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [options.paper.daemon] %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        run_loop(
            submit=args.submit,
            config_path=args.config,
            interval_seconds=args.interval_seconds,
            opening_delay_minutes=args.opening_delay_minutes,
            entry_cutoff_minutes=args.entry_cutoff_minutes,
        )
    except Exception:
        LOG.exception("Options paper daemon could not start")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
