#!/usr/bin/env python3
"""
Trade Journal — Persistent log of every Cassius trade decision and outcome.

Append mode:
  poetry run python trade_journal.py --log decisions.json    # Log from executor output
  echo '{"results":[...]}' | poetry run python trade_journal.py --log -

Query mode:
  poetry run python trade_journal.py --show                   # Last 10 trades
  poetry run python trade_journal.py --show --limit 50        # Last 50
  poetry run python trade_journal.py --show --ticker NVDA     # Filter by ticker
  poetry run python trade_journal.py --stats                  # Win/loss stats
  poetry run python trade_journal.py --stats --days 7         # Stats for last 7 days
  poetry run python trade_journal.py --telegram               # Summary for Telegram
"""

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

JOURNAL_PATH = Path(__file__).parent / "data" / "trade_journal.jsonl"
BROKER_ORDER_STATUSES = {
    "accepted", "accepted_for_bidding", "calculated", "canceled", "done_for_day",
    "expired", "filled", "held", "new", "partially_filled", "pending_cancel",
    "pending_new", "pending_replace", "rejected", "replaced", "stopped", "suspended",
}
SUBMITTED_STATUSES = {"submitted", "executed", "accepted", "new", "pending_new", "filled", "partially_filled"}


def ensure_dir():
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)


def _order_keys(entry: dict) -> set[tuple[str, str, str]]:
    mode = f"{entry.get('trading_mode') or 'unknown'}:{entry.get('account_id') or 'legacy'}"
    keys = set()
    if entry.get("order_id"):
        keys.add((mode, "order", str(entry["order_id"])))
    if entry.get("client_order_id"):
        keys.add((mode, "client", str(entry["client_order_id"])))
    return keys


def dedupe_journal_entries(entries: list[dict]) -> list[dict]:
    """Keep one submission event per broker order or stable client ID."""
    deduped: list[dict | None] = []
    seen: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        keys = _order_keys(entry)
        matches = {seen[key] for key in keys if key in seen}
        if not matches:
            index = len(deduped)
            deduped.append(dict(entry))
        else:
            index = min(matches)
            combined = dict(deduped[index] or {})
            for old_index in sorted(matches - {index}):
                for field, value in (deduped[old_index] or {}).items():
                    if value is not None and value != "":
                        combined[field] = value
                deduped[old_index] = None
            for field, value in entry.items():
                if value is not None and value != "":
                    combined[field] = value
            deduped[index] = combined
            for old_index in matches:
                for key, mapped_index in list(seen.items()):
                    if mapped_index == old_index:
                        seen[key] = index
        for key in _order_keys(deduped[index] or {}):
            seen[key] = index
    return [entry for entry in deduped if entry is not None]


def append_trades(execution_result: dict):
    """Append order submissions and rejections from execute_trades.py output."""
    ensure_dir()
    results = execution_result.get("results", [])
    timestamp = execution_result.get("timestamp", datetime.now().isoformat())
    mode = execution_result.get("mode", "unknown")
    account_id = execution_result.get("account_id")

    logged = 0
    with open(JOURNAL_PATH, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        existing_keys = set()
        for line in f:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(existing, dict):
                existing_keys.update(_order_keys(existing))
        f.seek(0, os.SEEK_END)
        for trade in results:
            raw_status = trade.get("status")
            if raw_status in ("skipped", "would_execute"):
                continue
            submission_status = trade.get("submission_status")
            if not submission_status:
                submission_status = "submitted" if raw_status in SUBMITTED_STATUSES else raw_status
            broker_status = trade.get("broker_status")
            if not broker_status and raw_status in BROKER_ORDER_STATUSES:
                broker_status = raw_status
            entry = {
                "timestamp": timestamp,
                "mode": mode,
                "trading_mode": execution_result.get("trading_mode", "unknown"),
                "account_id": account_id,
                "strategy": trade.get("strategy", "unspecified"),
                "strategy_group": trade.get("strategy_group"),
                "research_profile_id": trade.get("research_profile_id"),
                "research_profile_regime": trade.get("research_profile_regime"),
                "market_regime": trade.get("market_regime"),
                "client_order_id": trade.get("client_order_id"),
                "ticker": trade.get("ticker"),
                "action": trade.get("action"),
                "qty": trade.get("qty", 0),
                "status": submission_status,
                "submission_status": submission_status,
                "broker_status": broker_status,
                "reasoning": trade.get("reasoning") or trade.get("reason", ""),
                "order_id": trade.get("order_id"),
                "error": trade.get("error"),
                "filled_qty": trade.get("filled_qty"),
                "filled_avg_price": trade.get("filled_avg_price"),
                "filled_at": trade.get("filled_at"),
                "idempotent_replay": bool(trade.get("idempotent_replay")),
                "pnl": trade.get("pnl"),
            }
            keys = _order_keys(entry)
            # A replay may supply the broker order ID after an earlier
            # uncertain submission supplied only its stable client ID.
            # Preserve that enrichment as an append event; readers merge it.
            known_order_id = (
                f"{entry.get('trading_mode') or 'unknown'}:{entry.get('account_id') or 'legacy'}",
                "order", str(entry.get("order_id")),
            )
            if keys & existing_keys and (not entry.get("order_id") or known_order_id in existing_keys):
                continue
            f.write(json.dumps(entry) + "\n")
            existing_keys.update(keys)
            logged += 1

    return logged


def read_journal(limit=None, ticker=None, days=None):
    """Read journal entries with optional filters."""
    if not JOURNAL_PATH.exists():
        return []

    entries = []
    cutoff = None
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    with open(JOURNAL_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if ticker and entry.get("ticker") != ticker.upper():
                continue
            if cutoff and entry.get("timestamp", "") < cutoff:
                continue
            entries.append(entry)

    if limit:
        entries = entries[-limit:]
    return entries


def compute_stats(entries):
    """Count API submissions separately from broker-confirmed fills."""
    entries = dedupe_journal_entries(entries)
    submitted = [e for e in entries if e.get("submission_status", e.get("status")) in SUBMITTED_STATUSES]
    filled = [e for e in submitted if e.get("broker_status") == "filled" or e.get("status") == "filled"]
    partially_filled = [e for e in submitted if e.get("broker_status") == "partially_filled"]
    blocked = [e for e in entries if e.get("status") == "blocked"]
    failed = [e for e in entries if e.get("status") == "failed"]

    buys = [e for e in submitted if e.get("action") in ("buy", "cover")]
    sells = [e for e in submitted if e.get("action") in ("sell", "short")]
    filled_buys = [e for e in filled if e.get("action") in ("buy", "cover")]
    filled_sells = [e for e in filled if e.get("action") in ("sell", "short")]

    # Ticker frequency
    ticker_counts = {}
    for e in submitted:
        t = e.get("ticker", "?")
        ticker_counts[t] = ticker_counts.get(t, 0) + 1

    # Action frequency by day
    daily = {}
    for e in submitted:
        day = e.get("timestamp", "")[:10]
        if day not in daily:
            daily[day] = {"buys": 0, "sells": 0, "total": 0}
        daily[day]["total"] += 1
        if e.get("action") in ("buy", "cover"):
            daily[day]["buys"] += 1
        else:
            daily[day]["sells"] += 1

    return {
        "total_entries": len(entries),
        "submitted": len(submitted),
        "filled": len(filled),
        "partially_filled": len(partially_filled),
        "blocked": len(blocked),
        "failed": len(failed),
        "buys": len(buys),
        "sells": len(sells),
        "filled_buys": len(filled_buys),
        "filled_sells": len(filled_sells),
        "unique_tickers": len(ticker_counts),
        "most_traded": sorted(ticker_counts.items(), key=lambda x: -x[1])[:5],
        "active_days": len(daily),
        "daily_breakdown": dict(sorted(daily.items(), reverse=True)[:10]),
    }


def show_trades(entries):
    """Display trades in a readable format."""
    if not entries:
        print("No trades in journal.")
        return

    print(f"{'Timestamp':<22} {'Ticker':<7} {'Action':<6} {'Qty':>6} {'Status':<10} {'Reasoning'}")
    print("-" * 100)
    for e in entries:
        ts = e.get("timestamp", "")[:19]
        reasoning = (e.get("reasoning") or "")[:40]
        print(f"{ts:<22} {e.get('ticker','?'):<7} {e.get('action','?'):<6} {e.get('qty',0):>6} {e.get('status','?'):<10} {reasoning}")


def show_stats(stats):
    """Display stats in terminal."""
    print(f"{'='*50}")
    print(f"  CASSIUS TRADE JOURNAL — STATS")
    print(f"{'='*50}")
    print(f"  Total entries:   {stats['total_entries']}")
    print(f"  Submitted:       {stats['submitted']}")
    print(f"  Confirmed fills: {stats['filled']}")
    print(f"  Partial fills:   {stats['partially_filled']}")
    print(f"  Blocked:         {stats['blocked']}")
    print(f"  Failed:          {stats['failed']}")
    print(f"  Buys:            {stats['buys']}")
    print(f"  Sells:           {stats['sells']}")
    print(f"  Unique tickers:  {stats['unique_tickers']}")
    print(f"  Active days:     {stats['active_days']}")

    if stats["most_traded"]:
        print(f"\n  Most Traded:")
        for ticker, count in stats["most_traded"]:
            print(f"    {ticker}: {count} trades")

    if stats["daily_breakdown"]:
        print(f"\n  Recent Daily Activity:")
        for day, d in stats["daily_breakdown"].items():
            print(f"    {day}: {d['total']} trades ({d['buys']}B / {d['sells']}S)")
    print()


def telegram_summary(stats, entries):
    """Telegram-formatted summary."""
    last_5 = entries[-5:] if entries else []

    print("📓 Cassius Trade Journal")
    print(f"📊 {stats['submitted']} submitted | {stats['filled']} broker-confirmed filled | {stats['blocked']} blocked | {stats['failed']} failed")
    print(f"📈 {stats['buys']} buy submissions | 📉 {stats['sells']} sell submissions | 🎯 {stats['unique_tickers']} tickers")
    print(f"📅 {stats['active_days']} active trading days")

    if stats["most_traded"]:
        top = ", ".join(f"{t} ({c})" for t, c in stats["most_traded"][:3])
        print(f"🔥 Most traded: {top}")

    if last_5:
        print(f"\n🕐 Last {len(last_5)} trades:")
        for e in last_5:
            emoji = "🟢" if e.get("action") in ("buy", "cover") else "🔴"
            print(f"  {emoji} {e.get('ticker')} — {e.get('action')} {e.get('qty',0)} ({e.get('status','?')})")


def main():
    parser = argparse.ArgumentParser(description="Cassius Trade Journal")
    parser.add_argument("--log", type=str, help="Log trades from executor JSON output (file or - for stdin)")
    parser.add_argument("--show", action="store_true", help="Show recent trades")
    parser.add_argument("--stats", action="store_true", help="Show trade statistics")
    parser.add_argument("--telegram", action="store_true", help="Telegram-formatted summary")
    parser.add_argument("--limit", type=int, default=10, help="Number of trades to show")
    parser.add_argument("--ticker", type=str, help="Filter by ticker")
    parser.add_argument("--days", type=int, help="Filter to last N days")
    args = parser.parse_args()

    if args.log:
        if args.log == "-":
            data = json.load(sys.stdin)
        else:
            with open(args.log) as f:
                data = json.load(f)
        logged = append_trades(data)
        print(f"✅ Logged {logged} trades to journal")
        return

    entries = read_journal(
        limit=args.limit if args.show else None,
        ticker=args.ticker,
        days=args.days,
    )

    if args.telegram:
        stats = compute_stats(entries)
        telegram_summary(stats, entries)
    elif args.stats:
        stats = compute_stats(entries)
        show_stats(stats)
    elif args.show:
        show_trades(entries)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
