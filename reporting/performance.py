from reporting.fills import get_account_fill_report
#!/usr/bin/env python3
"""Algo account performance, benchmark comparison, and broker-fill reports."""

from bisect import bisect_left
from collections import defaultdict
import fcntl
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import requests
from broker.accounts import get_account_for_mode, verify_account_for_mode
from market.calendar import get_market_schedule
from equity_research.validation.drift_monitor import LIVE_FEED_SCHEMA
from reporting import journal_store
from reporting.journal import dedupe_journal_entries
from runtime.atomic import atomic_write_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [perf_v2] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("perf_v2")

API_BASE = "https://paper-api.alpaca.markets/v2"

from runtime.paths import DATA_DIR


RISK_FREE_RATE_ANNUAL = 0.045  # ~4.5% annualised (T-bill proxy)
RISK_FREE_DAILY = RISK_FREE_RATE_ANNUAL / 252
MARKET_TZ = ZoneInfo("America/New_York")
SUBMITTED_STATUSES = {"submitted", "executed", "accepted", "new", "pending_new", "filled", "partially_filled"}


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _alpaca_get(endpoint: str, mode: str = "algo") -> dict | list:
    r = requests.get(
        f"{API_BASE}/{endpoint}",
        headers=get_account_for_mode(mode).headers,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_portfolio(mode: str = "algo") -> dict:
    """Fetch the Algo Alpaca account and its positions."""
    alpaca_account, account = verify_account_for_mode(mode)
    positions_raw = _alpaca_get("positions", mode=mode)
    equity = float(account["equity"])
    cash = float(account["cash"])
    last_equity = float(account.get("last_equity", equity))
    daily_pnl_pct = ((equity - last_equity) / last_equity) if last_equity else 0.0
    broker_account_id = str(account.get("id") or account.get("account_number") or "")
    if not broker_account_id:
        raise ValueError("Alpaca account response omitted its ID")

    return {
        "equity": equity,
        "cash": cash,
        "invested": equity - cash,
        "position_count": len(positions_raw),
        "account_name": alpaca_account.name,
        "account_id": broker_account_id,
        "daily_pnl_pct": daily_pnl_pct,
        "daily_pnl": equity - last_equity,
    }


def _snapshot_dir(mode: str) -> Path:
    """Return the Algo snapshot directory."""
    if mode != "algo":
        raise ValueError("Only the algo account route is supported")
    return DATA_DIR / "snapshots"


def load_snapshots(days: int | None = None, mode: str = "algo", account_id: str | None = None) -> list[dict]:
    """Load snapshots for one broker account and mode when its ID is given."""
    snapshots, _ = load_snapshots_with_skips(days=days, mode=mode, account_id=account_id)
    return snapshots


def load_snapshots_with_skips(
    days: int | None = None, mode: str = "algo", account_id: str | None = None,
) -> tuple[list[dict], int]:
    """Load snapshots and count the files skipped because they were unreadable."""
    snapshot_dir = _snapshot_dir(mode)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(snapshot_dir.glob("*.json"))
    snapshots = []
    skipped = 0
    for f in files:
        try:
            with open(f) as fp:
                snapshot = json.load(fp)
        except (OSError, ValueError) as exc:
            # json.JSONDecodeError and UnicodeDecodeError are both ValueError.
            skipped += 1
            log.warning("Skipping unreadable snapshot %s: %s", f.name, exc)
            continue
        if not isinstance(snapshot, dict):
            skipped += 1
            log.warning("Skipping snapshot %s: not a JSON object", f.name)
            continue
        if account_id is None or (
            snapshot.get("account_id") == account_id
            and snapshot.get("trading_mode") == mode
        ):
            snapshots.append(snapshot)

    if skipped:
        log.warning("Skipped %d of %d snapshot file(s) in %s", skipped, len(files), snapshot_dir)
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        snapshots = [s for s in snapshots if s.get("date", "") >= cutoff]

    return snapshots, skipped


def load_trade_journal(mode: str = "algo", account_id: str | None = None) -> list[dict]:
    """Load submissions for one mode and, when supplied, one broker account, from the state database."""
    trades = []
    unclassified = 0
    try:
        entries = journal_store.read_entries(journal_store.DEFAULT_DATABASE)
    except Exception as e:
        log.warning(f"Could not load trade journal: {e}")
        return trades
    for entry in entries:
        entry_mode = entry.get("trading_mode")
        if entry_mode is None:
            unclassified += 1
        elif entry_mode == mode and (account_id is None or entry.get("account_id") == account_id):
            trades.append(entry)
    if unclassified:
        log.warning(
            "%d legacy journal entries have no trading_mode and are excluded from %s stats",
            unclassified,
            mode,
        )
    return dedupe_journal_entries(trades)


def reconcile_trade_orders(trades: list[dict], mode: str) -> tuple[list[dict], dict]:
    """Attach current broker order status to journal submissions for reporting.

    The journal records the API submission at one point in time. Alpaca is the
    source of truth for fills, cancellations and rejects after that point.
    """
    wanted = {str(row.get("order_id")) for row in trades if row.get("order_id")}
    if not wanted:
        return trades, {"matched": 0, "unmatched": 0, "error": None}
    try:
        account, _ = verify_account_for_mode(mode)
        response = requests.get(
            f"{account.base_url}/orders",
            headers=account.headers,
            params={"status": "all", "limit": 500, "direction": "desc", "nested": "true"},
            timeout=15,
        )
        response.raise_for_status()
        orders = response.json()
        if not isinstance(orders, list):
            raise ValueError("Alpaca returned an invalid order list")
        by_id = {}

        def collect(order: dict) -> None:
            order_id = order.get("id")
            if order_id:
                by_id[str(order_id)] = order
            for leg in order.get("legs") or []:
                if isinstance(leg, dict):
                    collect(leg)

        for order in orders:
            if isinstance(order, dict):
                collect(order)
        matched = 0
        enriched = []
        for row in trades:
            value = dict(row)
            broker = by_id.get(str(value.get("order_id")))
            if broker:
                matched += 1
                value["broker_status"] = broker.get("status")
                value["filled_qty"] = broker.get("filled_qty")
                value["filled_avg_price"] = broker.get("filled_avg_price")
            enriched.append(value)
        return enriched, {
            "matched": matched,
            "unmatched": len(wanted - set(by_id)),
            "order_list_limit_reached": len(orders) >= 500,
            "error": None,
        }
    except (requests.RequestException, ValueError) as exc:
        log.warning("Could not reconcile %s journal orders: %s", mode, exc)
        return trades, {"matched": 0, "unmatched": len(wanted), "error": str(exc)}


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def compute_sharpe_21d(snapshots: list[dict]) -> float | None:
    """
    Compute rolling 21-day Sharpe ratio from daily equity snapshots.

    Sharpe = mean(excess_daily_return) / std(daily_return) * sqrt(252)
    """
    if len(snapshots) < 3:
        return None

    # Use last 21 trading days (or all available)
    window = snapshots[-22:]  # need N+1 to get N returns

    daily_returns = []
    for i in range(1, len(window)):
        prev_eq = window[i - 1].get("equity", 0)
        curr_eq = window[i].get("equity", 0)
        if prev_eq > 0:
            daily_returns.append((curr_eq - prev_eq) / prev_eq)

    if len(daily_returns) < 2:
        return None

    n = len(daily_returns)
    mean_ret = sum(daily_returns) / n
    excess = [r - RISK_FREE_DAILY for r in daily_returns]
    mean_excess = sum(excess) / n
    variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (n - 1)
    std_dev = math.sqrt(variance) if variance > 0 else 0

    if std_dev == 0:
        return None
    return (mean_excess / std_dev) * math.sqrt(252)


def compute_strategy_stats(trades: list[dict]) -> dict[str, dict]:
    """Summarize journal activity by pure strategy attribution."""
    grouped: dict[str, list[dict]] = {}
    for trade in trades:
        strategy = trade.get("strategy") or "unspecified"
        grouped.setdefault(strategy, []).append(trade)
    result = {}
    for strategy, rows in sorted(grouped.items()):
        submitted = [
            row for row in rows
            if row.get("submission_status", row.get("status")) in SUBMITTED_STATUSES
        ]
        filled = [
            row for row in submitted
            if row.get("broker_status") == "filled" or row.get("status") == "filled"
        ]
        result[strategy] = {
            "orders": len(rows),
            "submitted": len(submitted),
            "filled": len(filled),
            "partially_filled": sum(row.get("broker_status") == "partially_filled" for row in submitted),
            "blocked": sum(row.get("status") == "blocked" for row in rows),
            "failed": sum(row.get("status") == "failed" for row in rows),
            "entries": sum(str(row.get("action", "")).lower() in ("buy", "short") for row in submitted),
            "exits": sum(str(row.get("action", "")).lower() in ("sell", "cover") for row in submitted),
            "closed_trades_with_pnl": sum(row.get("pnl") is not None for row in rows),
        }
    return result


def compute_performance(snapshots: list[dict]) -> dict | None:
    """Compute cumulative performance metrics from daily snapshots."""
    if len(snapshots) < 2:
        return None

    first = snapshots[0]
    latest = snapshots[-1]

    base_equity = first.get("equity", 0)
    curr_equity = latest.get("equity", 0)
    portfolio_return = ((curr_equity - base_equity) / base_equity * 100) if base_equity else 0

    base_spy = first.get("spy_price")
    curr_spy = latest.get("spy_price")
    spy_return = ((curr_spy - base_spy) / base_spy * 100) if (base_spy and curr_spy) else None

    base_qqq = first.get("qqq_price")
    curr_qqq = latest.get("qqq_price")
    qqq_return = ((curr_qqq - base_qqq) / base_qqq * 100) if (base_qqq and curr_qqq) else None

    alpha_spy = (portfolio_return - spy_return) if spy_return is not None else None
    alpha_qqq = (portfolio_return - qqq_return) if qqq_return is not None else None

    # Daily returns
    daily_changes = []
    for i in range(1, len(snapshots)):
        prev_eq = snapshots[i - 1].get("equity", 0)
        curr_eq = snapshots[i].get("equity", 0)
        pct = ((curr_eq - prev_eq) / prev_eq * 100) if prev_eq else 0
        daily_changes.append({
            "date": snapshots[i].get("date"),
            "equity": curr_eq,
            "change": curr_eq - prev_eq,
            "change_pct": pct,
        })

    win_days = len([d for d in daily_changes if d["change_pct"] >= 0])
    lose_days = len([d for d in daily_changes if d["change_pct"] < 0])
    best = max(daily_changes, key=lambda x: x["change_pct"]) if daily_changes else None
    worst = min(daily_changes, key=lambda x: x["change_pct"]) if daily_changes else None

    sharpe_21d = compute_sharpe_21d(snapshots)

    return {
        "period_start": first.get("date"),
        "period_end": latest.get("date"),
        "trading_mode": latest.get("trading_mode", "algo"),
        "account_name": latest.get("account_name", "Algo"),
        "snapshots_count": len(snapshots),
        "starting_equity": base_equity,
        "current_equity": curr_equity,
        "portfolio_return_pct": round(portfolio_return, 2),
        "spy_return_pct": round(spy_return, 2) if spy_return is not None else None,
        "qqq_return_pct": round(qqq_return, 2) if qqq_return is not None else None,
        "alpha_vs_spy": round(alpha_spy, 2) if alpha_spy is not None else None,
        "alpha_vs_qqq": round(alpha_qqq, 2) if alpha_qqq is not None else None,
        "sharpe_21d": round(sharpe_21d, 3) if sharpe_21d is not None else None,
        "win_days": win_days,
        "lose_days": lose_days,
        "win_day_rate": round(win_days / (win_days + lose_days) * 100, 1) if (win_days + lose_days) else None,
        "best_day": best,
        "worst_day": worst,
        "cash": latest.get("cash", 0),
        "cash_pct": round(latest.get("cash", 0) / curr_equity * 100, 1) if curr_equity else 0,
        "daily_changes": daily_changes[-10:],
    }


def build_strategy_daily_returns(events: list[dict], snapshots: list[dict]) -> dict:
    """Daily per-strategy returns from fill-level realized P&L.

    ``events`` are the covered realized-fill events from
    ``reporting.fills.get_account_fill_report``. Each fill is counted once and
    split by the strategy that owns its lots. A fill is booked to the first
    snapshot session on or after its market date, and the return divides that
    P&L by account equity at the prior session's snapshot. Sessions without
    realized P&L are zero for any strategy that has realized P&L by then.
    """
    equity_by_day: dict[str, float] = {}
    for snapshot in snapshots:
        day = str(snapshot.get("date") or "")[:10]
        equity = _finite_float(snapshot.get("equity"))
        if day and equity is not None and equity > 0:
            equity_by_day[day] = equity
    sessions = sorted(equity_by_day)
    pnl: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    skipped = {
        "uncovered_fills": 0, "unreadable_time": 0,
        "after_last_snapshot": 0, "before_prior_session": 0,
    }
    for event in events:
        if event.get("basis_status") != "covered":
            skipped["uncovered_fills"] += 1
            continue
        day = _market_day(event.get("transaction_time"))
        if day is None:
            skipped["unreadable_time"] += 1
            continue
        index = bisect_left(sessions, day)
        if index >= len(sessions):
            skipped["after_last_snapshot"] += 1
            continue
        if index == 0:
            skipped["before_prior_session"] += 1
            continue
        for strategy, amount in (event.get("strategy_pnl") or {}).items():
            value = _finite_float(amount)
            if strategy and value is not None:
                pnl[str(strategy)][index] += value

    strategies = {}
    for strategy, by_index in sorted(pnl.items()):
        rows = []
        for index in range(min(by_index), len(sessions)):
            base = equity_by_day[sessions[index - 1]]
            rows.append({
                "date": sessions[index],
                "return": round(by_index.get(index, 0.0) / base, 8),
            })
        strategies[strategy] = rows
    return {"strategies": strategies, "sessions": len(sessions), "skipped": skipped}


def write_live_drift_feed(path: Path, returns_report: dict) -> None:
    """Write the per-strategy return series that the drift monitor consumes."""
    atomic_write_json(
        Path(path),
        {
            "schema": LIVE_FEED_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sessions": returns_report["sessions"],
            "skipped": returns_report["skipped"],
            "strategies": returns_report["strategies"],
        },
        indent=2, sort_keys=True,
    )


def write_live_drift_feed_for_account(path: Path, *, mode: str, account_id: str) -> dict:
    """Build and atomically write the live drift feed for one broker account.

    Same inputs and builders as ``--drift-feed``; used by the daemon's nightly job.
    """
    trades = load_trade_journal(mode=mode, account_id=account_id)
    trades, _ = reconcile_trade_orders(trades, mode=mode)
    fill_report = get_account_fill_report(mode, trades)
    all_snapshots, _ = load_snapshots_with_skips(mode=mode, account_id=account_id)
    feed = build_strategy_daily_returns(fill_report.get("realized_fills") or [], all_snapshots)
    write_live_drift_feed(Path(path), feed)
    return {
        "sessions": feed["sessions"],
        "realized_pnl_complete": bool(fill_report.get("realized_pnl_complete")),
    }


def _finite_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _market_day(value) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(MARKET_TZ).date().isoformat()


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def take_snapshot(
    force: bool = False,
    mode: str = "algo",
    snapshot_kind: str | None = None,
    *,
    now: datetime | None = None,
    portfolio_data: dict | None = None,
    include_benchmarks: bool = True,
) -> dict | None:
    """Record an Algo equity point or the daily close and benchmark data."""
    if snapshot_kind not in (None, "intraday", "close"):
        raise ValueError(f"Unsupported snapshot kind: {snapshot_kind}")
    snapshot_root = _snapshot_dir(mode)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(MARKET_TZ)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Snapshot time must be timezone-aware")
    now = now.astimezone(MARKET_TZ)
    today = now.strftime("%Y-%m-%d")
    effective_kind = snapshot_kind or "intraday"
    if snapshot_kind == "intraday":
        snapshot_dir = snapshot_root / "intraday"
        snap_file = snapshot_dir / f"{today}.jsonl"
    else:
        # Keep the legacy one-off default and the daily close in the root
        # directory. Risk baselines and the daily performance report read here.
        snapshot_dir = snapshot_root
        snap_file = snapshot_root / f"{today}.json"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    if snapshot_kind == "close":
        schedule = get_market_schedule(on_date=now.date(), mode=mode)
        if schedule is None:
            return {"date": today, "skipped": True, "reason": "no equity market session"}
        if now < schedule.closes_at:
            raise ValueError(f"Close snapshot is due after {schedule.closes_at.isoformat()}")

    if portfolio_data is None:
        try:
            portfolio_data = get_portfolio(mode=mode)
        except Exception as e:
            log.error(f"Failed to fetch portfolio: {e}")
            return None
    portfolio = portfolio_data
    if snapshot_kind != "intraday" and snap_file.exists() and not force:
        with open(snap_file) as f:
            existing = json.load(f)
        if (
            existing.get("account_id") == portfolio["account_id"]
            and existing.get("trading_mode") == mode
            and existing.get("snapshot_kind") == effective_kind
        ):
            log.info("Snapshot for %s already exists for this account and kind", snap_file.name)
            return existing

    spy_price = None
    qqq_price = None
    spy_daily = None
    qqq_daily = None

    if include_benchmarks:
        try:
            import yfinance as yf

            spy_ticker = yf.Ticker("SPY")
            spy_hist = spy_ticker.history(period="2d", interval="1d")
            if not spy_hist.empty:
                spy_price = float(spy_hist["Close"].iloc[-1])
                if len(spy_hist) >= 2:
                    spy_daily = (spy_price - float(spy_hist["Close"].iloc[-2])) / float(spy_hist["Close"].iloc[-2])
        except Exception as e:
            log.warning(f"SPY fetch failed: {e}")

        try:
            import yfinance as yf

            qqq_ticker = yf.Ticker("QQQ")
            qqq_hist = qqq_ticker.history(period="2d", interval="1d")
            if not qqq_hist.empty:
                qqq_price = float(qqq_hist["Close"].iloc[-1])
                if len(qqq_hist) >= 2:
                    qqq_daily = (qqq_price - float(qqq_hist["Close"].iloc[-2])) / float(qqq_hist["Close"].iloc[-2])
        except Exception as e:
            log.warning(f"QQQ fetch failed: {e}")

    snapshot = {
        "date": today,
        "timestamp": now.isoformat(),
        "snapshot_kind": effective_kind,
        "trading_mode": mode,
        "account_name": portfolio["account_name"],
        "account_id": portfolio["account_id"],
        "equity": portfolio["equity"],
        "cash": portfolio["cash"],
        "invested": portfolio["invested"],
        "position_count": portfolio["position_count"],
        "daily_pnl": portfolio["daily_pnl"],
        "daily_pnl_pct": round(portfolio["daily_pnl_pct"] * 100, 4),
        "spy_price": spy_price,
        "spy_daily_pct": round(spy_daily * 100, 4) if spy_daily is not None else None,
        "qqq_price": qqq_price,
        "qqq_daily_pct": round(qqq_daily * 100, 4) if qqq_daily is not None else None,
    }

    if snapshot_kind == "intraday":
        line = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with snap_file.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.write(line + "\n")
                stream.flush()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    else:
        atomic_write_json(snap_file, snapshot, indent=2, sort_keys=True)

    spy_label = f"${spy_price:.2f}" if spy_price is not None else "N/A"
    qqq_label = f"${qqq_price:.2f}" if qqq_price is not None else "N/A"
    log.info("Snapshot saved: equity=$%s, SPY=%s, QQQ=%s", f"{portfolio['equity']:,.2f}", spy_label, qqq_label)
    return snapshot


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
