#!/usr/bin/env python3
"""Algo account performance, benchmark comparison, and broker-fill reports."""

import argparse
import fcntl
import json
import logging
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(override=True)

import requests
from broker.accounts import get_account_for_mode, verify_account_for_mode
from market.calendar import get_market_schedule
from reporting.fills import FillHistoryError, get_account_fill_report
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

TRADE_JOURNAL_PATH = DATA_DIR / "trade_journal.jsonl"

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


def get_benchmark_return(symbol: str, period_days: int = 1) -> float | None:
    """Get benchmark return for the last period_days using yfinance."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        # Request enough history for the window
        lookback = max(period_days + 5, 10)
        hist = ticker.history(period=f"{lookback}d", interval="1d")
        if hist.empty or len(hist) < 2:
            return None
        if period_days == 1:
            prev = float(hist["Close"].iloc[-2])
            curr = float(hist["Close"].iloc[-1])
        else:
            # Use earliest close in window as base
            window = hist.tail(period_days + 1)
            prev = float(window["Close"].iloc[0])
            curr = float(window["Close"].iloc[-1])
        return (curr - prev) / prev if prev else None
    except Exception as e:
        log.warning(f"Benchmark fetch failed ({symbol}): {e}")
        return None


def _snapshot_dir(mode: str) -> Path:
    """Return the Algo snapshot directory."""
    if mode != "algo":
        raise ValueError("Only the algo account route is supported")
    return DATA_DIR / "snapshots"


def load_snapshots(days: int | None = None, mode: str = "algo", account_id: str | None = None) -> list[dict]:
    """Load snapshots for one broker account and mode when its ID is given."""
    snapshot_dir = _snapshot_dir(mode)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(snapshot_dir.glob("*.json"))
    snapshots = []
    for f in files:
        try:
            with open(f) as fp:
                snapshot = json.load(fp)
                if account_id is None or (
                    snapshot.get("account_id") == account_id
                    and snapshot.get("trading_mode") == mode
                ):
                    snapshots.append(snapshot)
        except Exception:
            continue

    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        snapshots = [s for s in snapshots if s.get("date", "") >= cutoff]

    return snapshots


def load_trade_journal(mode: str = "algo", account_id: str | None = None) -> list[dict]:
    """Load submissions for one mode and, when supplied, one broker account."""
    trades = []
    unclassified = 0
    if not TRADE_JOURNAL_PATH.exists():
        return trades
    try:
        with open(TRADE_JOURNAL_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        entry_mode = entry.get("trading_mode")
                        if entry_mode is None:
                            unclassified += 1
                        elif entry_mode == mode and (
                            account_id is None or entry.get("account_id") == account_id
                        ):
                            trades.append(entry)
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        log.warning(f"Could not load trade journal: {e}")
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


def compute_trade_stats(trades: list[dict]) -> dict:
    """
    Compute win rate and average win/loss from trade journal entries.

    Expects entries that have a 'pnl' or 'unrealized_pl' field, or skips
    if unavailable.  Filters to 'sell' side with a known outcome.
    """
    closed_trades = [
        t for t in trades
        if t.get("action", "").lower() in ("sell", "cover")
        and (t.get("broker_status") == "filled" or t.get("status") == "filled")
        and t.get("pnl") is not None
    ]

    if not closed_trades:
        return {
            "total_trades": len(trades),
            "closed_trades": 0,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "profit_factor": None,
        }

    wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]

    win_rate = len(wins) / len(closed_trades) if closed_trades else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    total_wins_pnl = sum(t["pnl"] for t in wins)
    total_loss_pnl = abs(sum(t["pnl"] for t in losses))
    profit_factor = (total_wins_pnl / total_loss_pnl) if total_loss_pnl > 0 else None

    return {
        "total_trades": len(trades),
        "closed_trades": len(closed_trades),
        "win_rate": round(win_rate * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor else None,
    }


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

def terminal_report(perf: dict, trade_stats: dict, mode: str = "algo") -> None:
    """Pretty-print performance summary to stdout."""
    if not perf:
        print("No performance data yet. Run --snapshot first.")
        return

    print(f"{'='*65}")
    print(f"  ALGO TRADER — {mode.upper()} PERFORMANCE TRACKER ({perf.get('account_name', mode.title())})")
    print(f"  {perf['period_start']} → {perf['period_end']}  ({perf['snapshots_count']} snapshots)")
    print(f"{'='*65}")
    print(f"  Starting Equity:    ${perf['starting_equity']:>13,.2f}")
    print(f"  Current Equity:     ${perf['current_equity']:>13,.2f}")
    print(f"  Cash:               ${perf['cash']:>13,.2f}  ({perf['cash_pct']:.1f}%)")
    print()
    print(f"  Portfolio Return:    {perf['portfolio_return_pct']:>+12.2f}%")

    if perf.get("spy_return_pct") is not None:
        spy_beat = "✅ BEATING" if (perf.get("alpha_vs_spy") or 0) > 0 else "❌ LAGGING"
        print(f"  SPY Return:         {perf['spy_return_pct']:>+12.2f}%")
        print(f"  Alpha vs SPY:       {perf['alpha_vs_spy']:>+12.2f}%  {spy_beat}")

    if perf.get("qqq_return_pct") is not None:
        qqq_beat = "✅ BEATING" if (perf.get("alpha_vs_qqq") or 0) > 0 else "❌ LAGGING"
        print(f"  QQQ Return:         {perf['qqq_return_pct']:>+12.2f}%")
        print(f"  Alpha vs QQQ:       {perf['alpha_vs_qqq']:>+12.2f}%  {qqq_beat}")

    print()

    if perf.get("sharpe_21d") is not None:
        sharpe = perf["sharpe_21d"]
        sharpe_label = "Excellent" if sharpe > 1.5 else ("Good" if sharpe > 1.0 else ("OK" if sharpe > 0.5 else "Poor"))
        print(f"  21-Day Sharpe:      {sharpe:>+12.3f}  ({sharpe_label})")

    print(f"  Win Days:           {perf['win_days']:>13}  ({perf.get('win_day_rate', 0):.1f}%)")
    print(f"  Lose Days:          {perf['lose_days']:>13}")

    if perf.get("best_day"):
        print(f"  Best Day:           {perf['best_day']['date']}  ({perf['best_day']['change_pct']:+.2f}%)")
    if perf.get("worst_day"):
        print(f"  Worst Day:          {perf['worst_day']['date']}  ({perf['worst_day']['change_pct']:+.2f}%)")

    print()

    # Trade stats
    if trade_stats.get("closed_trades", 0) > 0:
        print(f"  TRADE STATS ({trade_stats['closed_trades']} closed / {trade_stats['total_trades']} total):")
        if trade_stats.get("win_rate") is not None:
            print(f"    Win Rate:         {trade_stats['win_rate']:>8.1f}%")
        if trade_stats.get("avg_win") is not None:
            print(f"    Avg Win:          ${trade_stats['avg_win']:>8.2f}")
        if trade_stats.get("avg_loss") is not None:
            print(f"    Avg Loss:         ${trade_stats['avg_loss']:>8.2f}")
        if trade_stats.get("profit_factor") is not None:
            print(f"    Profit Factor:    {trade_stats['profit_factor']:>8.2f}x")
        print()

    # Recent daily P&L
    if perf.get("daily_changes"):
        print(f"  Recent Daily P&L:")
        for dc in perf["daily_changes"]:
            dot = "🟢" if dc["change_pct"] >= 0 else "🔴"
            print(f"    {dot} {dc['date']}: ${dc['change']:>+9,.2f}  ({dc['change_pct']:>+6.2f}%)  → ${dc['equity']:,.2f}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Algo Trader Performance Tracker")
    parser.add_argument("--snapshot", action="store_true", help="Record today's snapshot")
    parser.add_argument("--force", action="store_true", help="Overwrite the daily snapshot; intraday points always append")
    parser.add_argument("--snapshot-kind", choices=["intraday", "close"],
                        help="Write a timestamped intraday point or a broker-calendar-verified close")
    parser.add_argument("--report", action="store_true", help="Terminal performance report")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--fill-report", action="store_true",
                        help="Reconcile gross realized P&L from broker FILL activities")
    parser.add_argument("--days", type=int, help="Limit to last N days")
    parser.add_argument("--mode", choices=["algo"], default="algo",
                        help="Account route (only the Algo paper account is supported)")
    args = parser.parse_args()
    if args.snapshot_kind and not args.snapshot:
        parser.error("--snapshot-kind requires --snapshot")
    if args.fill_report and args.snapshot:
        parser.error("--fill-report cannot be combined with --snapshot")

    if args.snapshot:
        try:
            snap = take_snapshot(force=args.force, mode=args.mode, snapshot_kind=args.snapshot_kind)
        except (requests.RequestException, ValueError) as exc:
            log.error("Cannot record %s snapshot: %s", args.mode, exc)
            return 1
        if snap is None:
            return 1
        if snap.get("skipped"):
            print(f"{args.mode.upper()} close snapshot skipped: {snap['reason']}")
            return 0
        print(
            f"{args.mode.upper()} snapshot saved: {snap['date']} — "
            f"equity=${snap['equity']:,.2f} "
            f"({snap.get('account_name', args.mode.title())})"
        )
        return 0

    try:
        _, current_account = verify_account_for_mode(args.mode)
        current_account_id = str(current_account.get("id") or current_account.get("account_number") or "")
        if not current_account_id:
            raise ValueError("Alpaca account response omitted its ID")
    except (requests.RequestException, ValueError) as exc:
        log.error("Cannot verify current %s account for reporting: %s", args.mode, exc)
        return 1
    snapshots = load_snapshots(days=args.days, mode=args.mode, account_id=current_account_id)
    trades = load_trade_journal(mode=args.mode, account_id=current_account_id)
    trades, order_reconciliation = reconcile_trade_orders(trades, mode=args.mode)
    perf = compute_performance(snapshots)
    trade_stats = compute_trade_stats(trades)
    strategy_stats = compute_strategy_stats(trades)
    fill_report = None
    if args.fill_report:
        try:
            fill_report = get_account_fill_report(args.mode, trades)
        except (requests.RequestException, FillHistoryError, ValueError) as exc:
            log.error("Cannot reconcile %s broker fills: %s", args.mode, exc)
            return 1
        # The summary is concise enough for cron reports. Full per-fill
        # details remain available through reporting.fills directly.
        fill_report.pop("realized_fills", None)

    if args.json:
        output = {
            "trading_mode": args.mode,
            "performance": perf,
            "trade_stats": trade_stats,
            "strategy_stats": strategy_stats,
            "order_reconciliation": order_reconciliation,
        }
        if fill_report is not None:
            output["fill_report"] = fill_report
        print(json.dumps(output, indent=2, default=str))
    elif args.report or not args.json:
        terminal_report(perf, trade_stats, mode=args.mode)
        if strategy_stats:
            print("  STRATEGY ATTRIBUTION:")
            for name, values in strategy_stats.items():
                print(
                    f"    {name:<28} {values['orders']:>4} orders  "
                    f"{values['submitted']} submitted / {values['filled']} filled  "
                    f"{values['blocked']} blocked / {values['failed']} failed"
                )
        if order_reconciliation["error"]:
            print("  Broker order reconciliation unavailable; fill counts may be stale.")
        elif order_reconciliation["unmatched"]:
            print(f"  {order_reconciliation['unmatched']} journal order(s) not found in recent broker history.")
        if fill_report is not None:
            pnl = fill_report.get("realized_pnl")
            pnl_label = f"${float(pnl):+,.2f}" if pnl is not None else "unknown"
            print(f"  Gross realized P&L from broker fills: {pnl_label}")
            print(f"  By strategy: {fill_report.get('strategy_pnl') or {}}")
            print(f"  Unattributed gross P&L: ${float(fill_report['unattributed_realized_pnl']):+,.2f}")
            if not fill_report.get("realized_pnl_complete"):
                print("  Broker fill basis is incomplete; realized total is unknown.")
    return 0 if fill_report is None or fill_report.get("realized_pnl_complete") else 1


if __name__ == "__main__":
    sys.exit(main())
