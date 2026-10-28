"""Trade journal helpers: record submissions in the state database, read them back, and compute stats."""

from reporting import journal_store
from datetime import datetime, timedelta


BROKER_ORDER_STATUSES = {
    "accepted", "accepted_for_bidding", "calculated", "canceled", "done_for_day",
    "expired", "filled", "held", "new", "partially_filled", "pending_cancel",
    "pending_new", "pending_replace", "rejected", "replaced", "stopped", "suspended",
}
SUBMITTED_STATUSES = {"submitted", "executed", "accepted", "new", "pending_new", "filled", "partially_filled"}




_order_keys = journal_store.order_keys




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
    """Append order submissions and rejections from execution command output."""
    results = execution_result.get("results", [])
    timestamp = execution_result.get("timestamp", datetime.now().isoformat())
    mode = execution_result.get("mode", "unknown")
    account_id = execution_result.get("account_id")

    entries: list[dict] = []
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
            "error": trade.get("error") or (
                trade.get("reason") if raw_status in ("blocked", "failed") else None
            ),
            "rule": trade.get("rule"),
            "filled_qty": trade.get("filled_qty"),
            "filled_avg_price": trade.get("filled_avg_price"),
            "filled_at": trade.get("filled_at"),
            "idempotent_replay": bool(trade.get("idempotent_replay")),
            "pnl": trade.get("pnl"),
        }
        entries.append(entry)

    # The database is the record: a write failure raises, so it can never pass silently.
    written = journal_store.append_entries(journal_store.DEFAULT_DATABASE, entries, recorded_at=timestamp)
    logged = len(written)
    return logged

def read_journal(limit=None, ticker=None, days=None):
    """Read journal entries with optional filters, from the state database."""
    entries = []
    cutoff = None
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    for entry in journal_store.read_entries(journal_store.DEFAULT_DATABASE):
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
