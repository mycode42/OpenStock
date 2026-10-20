"""Durable Algo execution incidents that require broker evidence to clear."""

from __future__ import annotations

from datetime import datetime, timezone


def record_incident(
    portfolio: dict, *, kind: str, ticker: str, client_order_id: str | None,
    order_id: str | None = None, reason: str,
) -> None:
    symbol = str(ticker).upper()
    if not symbol or kind not in {"exit_submission", "oco_repair"}:
        raise ValueError("Execution incident needs a symbol and known kind")
    incident = portfolio.get("execution_incident")
    if not isinstance(incident, dict) or not isinstance(incident.get("events"), dict):
        incident = {"events": {}}
        portfolio["execution_incident"] = incident
    now = datetime.now(timezone.utc).isoformat()
    key = f"{kind}:{symbol}"
    prior = incident["events"].get(key) or {}
    incident["events"][key] = {
        "kind": kind, "ticker": symbol,
        "client_order_id": client_order_id,
        "order_id": order_id,
        "reason": str(reason)[:240],
        "detected_at": prior.get("detected_at") or now,
        "last_seen_at": now,
    }
    incident["status"] = "open"
    incident.pop("resolved_at", None)


def resolve_incident(portfolio: dict, *, kind: str, ticker: str) -> bool:
    incident = portfolio.get("execution_incident")
    if not isinstance(incident, dict) or not isinstance(incident.get("events"), dict):
        return False
    removed = incident["events"].pop(f"{kind}:{str(ticker).upper()}", None)
    if removed and not incident["events"]:
        incident["status"] = "resolved"
        incident["resolved_at"] = datetime.now(timezone.utc).isoformat()
    return removed is not None


def open_incident_symbols(portfolio: dict) -> list[str]:
    incident = portfolio.get("execution_incident") or {}
    if incident.get("status") != "open":
        return []
    events = incident.get("events") or {}
    return sorted({str(row.get("ticker") or "").upper() for row in events.values() if isinstance(row, dict) and row.get("ticker")})


__all__ = ["record_incident", "resolve_incident", "open_incident_symbols"]
