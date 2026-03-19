#!/usr/bin/env python3
# CANONICAL TRACKER — This is the authoritative performance tracker. performance_tracker.py (V1) has been removed.
"""
Performance Tracker V2 — Swarm Trader daily performance with benchmark comparison.

Tracks portfolio vs SPY and QQQ, computes rolling 21-day Sharpe ratio,
win rate, and average win/loss per trade.  Saves daily JSON snapshots to
snapshots/YYYY-MM-DD.json and prints a pretty summary.

Usage:
  poetry run python performance_tracker_v2.py --snapshot          # Record today
  poetry run python performance_tracker_v2.py --snapshot --force  # Overwrite today
  poetry run python performance_tracker_v2.py --report            # Terminal summary
  poetry run python performance_tracker_v2.py --json              # JSON output
  poetry run python performance_tracker_v2.py --days 21           # Last 21 days
"""

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

import requests
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [perf_v2] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("perf_v2")

API_BASE = "https://paper-api.alpaca.markets/v2"
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
}

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
TRADE_JOURNAL_PATH = Path(__file__).parent / "data" / "trade_journal.jsonl"

RISK_FREE_RATE_ANNUAL = 0.045  # ~4.5% annualised (T-bill proxy)
RISK_FREE_DAILY = RISK_FREE_RATE_ANNUAL / 252


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _alpaca_get(endpoint: str) -> dict | list:
    r = requests.get(f"{API_BASE}/{endpoint}", headers=ALPACA_HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_portfolio() -> dict:
    """Fetch current Alpaca account + positions."""
    account = _alpaca_get("account")
    positions_raw = _alpaca_get("positions")
    equity = float(account["equity"])
    cash = float(account["cash"])
    last_equity = float(account.get("last_equity", equity))
    daily_pnl_pct = ((equity - last_equity) / last_equity) if last_equity else 0.0

    return {
        "equity": equity,
        "cash": cash,
        "invested": equity - cash,
        "position_count": len(positions_raw),
        "daily_pnl_pct": daily_pnl_pct,
        "daily_pnl": equity - last_equity,
    }


def get_benchmark_return(symbol: str, period_days: int = 1) -> float | None:
    """Get benchmark return for the last period_days using yfinance."""
    try:
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


def load_snapshots(days: int | None = None) -> list[dict]:
    """Load all daily snapshots from snapshots/ directory, optionally filtered."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SNAPSHOTS_DIR.glob("*.json"))
    snapshots = []
    for f in files:
        try:
            with open(f) as fp:
                snapshots.append(json.load(fp))
        except Exception:
            continue

    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        snapshots = [s for s in snapshots if s.get("date", "") >= cutoff]

    return snapshots


def load_trade_journal() -> list[dict]:
    """Load all trades from the JSONL trade journal."""
    trades = []
    if not TRADE_JOURNAL_PATH.exists():
        return trades
    try:
        with open(TRADE_JOURNAL_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        log.warning(f"Could not load trade journal: {e}")
    return trades


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

def take_snapshot(force: bool = False) -> dict | None:
    """Record today's equity + SPY + QQQ snapshot to snapshots/YYYY-MM-DD.json."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    snap_file = SNAPSHOTS_DIR / f"{today}.json"

    if snap_file.exists() and not force:
        log.info(f"Snapshot for {today} already exists. Use --force to overwrite.")
        with open(snap_file) as f:
            return json.load(f)

    try:
        portfolio = get_portfolio()
    except Exception as e:
        log.error(f"Failed to fetch portfolio: {e}")
        return None

    spy_price = None
    qqq_price = None
    spy_daily = None
    qqq_daily = None

    try:
        spy_ticker = yf.Ticker("SPY")
        spy_hist = spy_ticker.history(period="2d", interval="1d")
        if not spy_hist.empty:
            spy_price = float(spy_hist["Close"].iloc[-1])
            if len(spy_hist) >= 2:
                spy_daily = (spy_price - float(spy_hist["Close"].iloc[-2])) / float(spy_hist["Close"].iloc[-2])
    except Exception as e:
        log.warning(f"SPY fetch failed: {e}")

    try:
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
        "timestamp": datetime.now().isoformat(),
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

    with open(snap_file, "w") as f:
        json.dump(snapshot, f, indent=2)

    log.info(
        f"Snapshot saved: equity=${portfolio['equity']:,.2f}, "
        f"SPY=${spy_price:.2f}, " if spy_price else "SPY=N/A, "
        f"QQQ=${qqq_price:.2f}" if qqq_price else "QQQ=N/A"
    )
    return snapshot


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def terminal_report(perf: dict, trade_stats: dict) -> None:
    """Pretty-print performance summary to stdout."""
    if not perf:
        print("No performance data yet. Run --snapshot first.")
        return

    print(f"{'='*65}")
    print(f"  SWARM TRADER — PERFORMANCE TRACKER")
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

def main():
    parser = argparse.ArgumentParser(description="Swarm Trader Performance Tracker")
    parser.add_argument("--snapshot", action="store_true", help="Record today's snapshot")
    parser.add_argument("--force", action="store_true", help="Overwrite today's snapshot")
    parser.add_argument("--report", action="store_true", help="Terminal performance report")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--days", type=int, help="Limit to last N days")
    args = parser.parse_args()

    if args.snapshot:
        snap = take_snapshot(force=args.force)
        if snap:
            print(f"Snapshot saved: {snap['date']} — equity=${snap['equity']:,.2f}")
        return

    snapshots = load_snapshots(days=args.days)
    trades = load_trade_journal()
    perf = compute_performance(snapshots)
    trade_stats = compute_trade_stats(trades)

    if args.json:
        output = {"performance": perf, "trade_stats": trade_stats}
        print(json.dumps(output, indent=2, default=str))
    elif args.report or not args.json:
        terminal_report(perf, trade_stats)


if __name__ == "__main__":
    main()
