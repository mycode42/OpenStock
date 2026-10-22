"""Read-only, fill-based realized P&L for one Alpaca paper account.

The FILL endpoint supplies execution quantities and prices, including partial
fills. FIFO basis is reconstructed from the complete fetched fill history.
If a closing fill lacks preceding FIFO lots, or broker positions disagree
with the fill-derived inventory, realized P&L is unknown for that symbol.

Only exact mode-tagged journal order IDs, or broker-verified bracket child IDs
linked to such an entry order, can attribute P&L to a strategy. This is gross
trading P&L; fees, dividends, transfers, splits, and tax-lot rules are outside
the FILL endpoint and are not included.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Mapping
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

from broker.accounts import AlpacaAccount, get_account_for_mode


class FillHistoryError(RuntimeError):
    """An incomplete or invalid broker response makes calculation unsafe."""


@dataclass(frozen=True)
class Fill:
    activity_id: str
    order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    transaction_time: datetime


@dataclass(frozen=True)
class _JournalOrder:
    strategy: str
    symbol: str
    side: str


@dataclass
class _Lot:
    qty: Decimal
    price: Decimal
    strategy: str | None
    order_id: str
    transaction_time: datetime


def _money(value: Decimal) -> str:
    return format(value, "f")


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise FillHistoryError(f"{field} is not numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FillHistoryError(f"{field} is not numeric") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise FillHistoryError(f"{field} must be finite" + (" and positive" if positive else ""))
    return number


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise FillHistoryError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FillHistoryError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise FillHistoryError(f"{field} has no timezone")
    return parsed.astimezone(timezone.utc)


def _parse_fill(raw: dict) -> Fill:
    if not isinstance(raw, dict):
        raise FillHistoryError("Alpaca FILL activity is not an object")
    if raw.get("activity_type") not in (None, "FILL"):
        raise FillHistoryError("Non-FILL activity appeared in FILL history")
    activity_id = str(raw.get("id") or "").strip()
    order_id = str(raw.get("order_id") or "").strip()
    symbol = str(raw.get("symbol") or "").upper().strip()
    side = str(raw.get("side") or "").lower().strip()
    if not activity_id or not order_id or not symbol or side not in {
        "buy", "sell", "sell_short", "buy_to_cover",
    }:
        raise FillHistoryError("FILL activity lacks an ID, order ID, symbol, or supported side")
    return Fill(
        activity_id=activity_id,
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=_decimal(raw.get("qty"), "FILL quantity", positive=True),
        price=_decimal(raw.get("price"), "FILL price", positive=True),
        transaction_time=_timestamp(raw.get("transaction_time"), "FILL transaction_time"),
    )


def _get_json(
    account: AlpacaAccount,
    endpoint: str,
    *,
    http_get: Callable,
    params: dict | None = None,
) -> object:
    response = http_get(
        f"{account.base_url.rstrip('/')}/{endpoint.lstrip('/')}",
        headers=account.headers,
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise FillHistoryError(f"Alpaca {endpoint} response is not JSON") from exc


def fetch_fill_activities(
    account: AlpacaAccount,
    *,
    http_get: Callable | None = None,
    page_size: int = 100,
    max_pages: int = 1000,
) -> list[dict]:
    """GET all FILL pages from account inception; never stop at the first 100.

    Alpaca's ascending page_token is the ID of the last activity on the prior
    page. A repeated activity, malformed cursor, or page limit raises rather
    than returning a partial history.
    """
    if not 1 <= page_size <= 100 or max_pages < 1:
        raise ValueError("page_size must be 1..100 and max_pages must be positive")
    getter = http_get or requests.get
    activities: list[dict] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None
    for _ in range(max_pages):
        params: dict[str, object] = {"direction": "asc", "page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        payload = _get_json(account, "account/activities/FILL", http_get=getter, params=params)
        if not isinstance(payload, list) or len(payload) > page_size:
            raise FillHistoryError("Alpaca FILL page is invalid")
        for raw in payload:
            if not isinstance(raw, dict):
                raise FillHistoryError("Alpaca FILL page contains a non-object")
            activity_id = str(raw.get("id") or "").strip()
            if not activity_id or activity_id in seen_ids:
                raise FillHistoryError("Alpaca FILL activity ID is absent or repeated")
            seen_ids.add(activity_id)
            activities.append(raw)
        if len(payload) < page_size:
            return activities
        page_token = str(payload[-1].get("id") or "").strip()
        if not page_token or page_token in seen_tokens:
            raise FillHistoryError("Alpaca FILL pagination cursor is absent or repeated")
        seen_tokens.add(page_token)
    raise FillHistoryError("Alpaca FILL page limit reached; history may be incomplete")


def fetch_positions(
    account: AlpacaAccount,
    *,
    http_get: Callable | None = None,
) -> list[dict]:
    """Read broker holdings to check the fill-derived remaining quantity."""
    payload = _get_json(account, "positions", http_get=http_get or requests.get)
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise FillHistoryError("Alpaca positions response is invalid")
    return payload


def _journal_side(action: object) -> str | None:
    value = str(action or "").lower().strip()
    if value == "buy":
        return "buy"
    if value == "cover":
        return "buy_to_cover"
    if value == "sell":
        return "sell"
    if value == "short":
        return "sell_short"
    return None


def _closing_side_for_open_side(side: str) -> str | None:
    return {"buy": "sell", "sell_short": "buy_to_cover"}.get(side)


def _journal_index(journal_entries: Iterable[dict], mode: str) -> tuple[dict[str, _JournalOrder], list[str]]:
    candidates: dict[str, set[_JournalOrder]] = defaultdict(set)
    for row in journal_entries:
        if not isinstance(row, dict) or row.get("trading_mode") != mode:
            continue
        order_id = str(row.get("order_id") or "").strip()
        strategy = str(row.get("strategy") or "").strip()
        symbol = str(row.get("ticker") or "").upper().strip()
        side = _journal_side(row.get("action"))
        if not order_id or not symbol or not side or strategy.lower() in {"", "unknown", "unspecified"}:
            continue
        candidates[order_id].add(_JournalOrder(strategy, symbol, side))
    conflicts = sorted(order_id for order_id, values in candidates.items() if len(values) > 1)
    return {
        order_id: next(iter(values))
        for order_id, values in candidates.items()
        if len(values) == 1
    }, conflicts


def _broker_quantities(positions: Iterable[dict] | Mapping[str, object]) -> dict[str, Decimal]:
    quantities: dict[str, Decimal] = {}
    if isinstance(positions, Mapping):
        rows = [
            {"symbol": symbol, "qty": value.get("qty") if isinstance(value, dict) else value}
            for symbol, value in positions.items()
        ]
    else:
        rows = list(positions)
    for row in rows:
        if not isinstance(row, dict):
            raise FillHistoryError("Broker position is not an object")
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol or symbol in quantities:
            raise FillHistoryError("Broker position symbol is missing or repeated")
        quantities[symbol] = _decimal(row.get("qty"), f"{symbol} broker quantity")
    return quantities


def _period_bound(value: str | None, field: str) -> datetime | None:
    return _timestamp(value, field) if value is not None else None


def calculate_realized_pnl(
    activities: Iterable[dict],
    journal_entries: Iterable[dict],
    *,
    mode: str,
    current_positions: Iterable[dict] | Mapping[str, object] | None = None,
    bracket_parent_by_child: Mapping[str, str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Compute gross FIFO realized P&L from complete, validated fill history.

    since is inclusive and until exclusive; both filter realized closing
    events only. All earlier fills still establish FIFO cost basis. If basis is
    incomplete for any symbol, total realized_pnl is None.
    """
    if mode not in {"swing", "day", "algo"}:
        raise ValueError("mode must be swing, day, or algo")
    since_at = _period_bound(since, "since")
    until_at = _period_bound(until, "until")
    if since_at and until_at and since_at >= until_at:
        raise ValueError("since must precede until")
    parsed = [_parse_fill(raw) for raw in activities]
    ids = [fill.activity_id for fill in parsed]
    if len(ids) != len(set(ids)):
        raise FillHistoryError("FILL activity IDs are repeated")
    # Keep Alpaca's ascending activity order when executions share a timestamp.
    parsed.sort(key=lambda fill: fill.transaction_time)

    journal_by_order, journal_conflicts = _journal_index(journal_entries, mode)
    parent_by_child = {
        str(child): str(parent)
        for child, parent in (bracket_parent_by_child or {}).items()
        if child and parent and str(child) != str(parent)
    }
    attribution_conflicts: set[str] = set()

    def strategy_for(fill: Fill) -> str | None:
        direct = journal_by_order.get(fill.order_id)
        direct_strategy = None
        if direct:
            if direct.symbol == fill.symbol and direct.side == fill.side:
                direct_strategy = direct.strategy
            else:
                attribution_conflicts.add(fill.order_id)
                return None
        parent_id = parent_by_child.get(fill.order_id)
        parent = journal_by_order.get(parent_id) if parent_id else None
        parent_strategy = None
        if parent:
            if (
                parent.symbol == fill.symbol
                and _closing_side_for_open_side(parent.side) == fill.side
            ):
                parent_strategy = parent.strategy
            else:
                attribution_conflicts.add(fill.order_id)
                return None
        if direct_strategy and parent_strategy and direct_strategy != parent_strategy:
            attribution_conflicts.add(fill.order_id)
            return None
        return direct_strategy or parent_strategy

    long_lots: dict[str, deque[_Lot]] = defaultdict(deque)
    short_lots: dict[str, deque[_Lot]] = defaultdict(deque)
    net_qty: dict[str, Decimal] = defaultdict(Decimal)
    basis_errors: dict[str, str] = {}
    sales: list[dict] = []
    for fill in parsed:
        if fill.side == "buy":
            net_qty[fill.symbol] += fill.qty
            if fill.symbol not in basis_errors:
                long_lots[fill.symbol].append(
                    _Lot(fill.qty, fill.price, strategy_for(fill), fill.order_id, fill.transaction_time)
                )
            continue

        if fill.side == "sell_short":
            net_qty[fill.symbol] -= fill.qty
            if fill.symbol not in basis_errors:
                short_lots[fill.symbol].append(
                    _Lot(fill.qty, fill.price, strategy_for(fill), fill.order_id, fill.transaction_time)
                )
            continue

        is_short_cover = fill.side == "buy_to_cover"
        net_qty[fill.symbol] += fill.qty if is_short_cover else -fill.qty
        sale = {"fill": fill, "pnl": Decimal(0), "portions": []}
        sales.append(sale)
        if fill.symbol in basis_errors:
            continue
        remaining = fill.qty
        exit_strategy = strategy_for(fill)
        inventory_lots = short_lots[fill.symbol] if is_short_cover else long_lots[fill.symbol]
        while remaining > 0 and inventory_lots:
            lot = inventory_lots[0]
            matched = min(remaining, lot.qty)
            pnl = matched * (lot.price - fill.price if is_short_cover else fill.price - lot.price)
            strategy = lot.strategy if lot.strategy and lot.strategy == exit_strategy else None
            if lot.strategy and exit_strategy and lot.strategy != exit_strategy:
                attribution_conflicts.add(fill.order_id)
            sale["portions"].append({
                "qty": matched,
                "pnl": pnl,
                "basis": matched * lot.price,
                "strategy": strategy,
            })
            sale["pnl"] += pnl
            remaining -= matched
            lot.qty -= matched
            if lot.qty == 0:
                inventory_lots.popleft()
        if remaining > 0:
            basis_errors[fill.symbol] = (
                "buy_to_cover quantity exceeds preceding sell_short fills"
                if is_short_cover else "sell quantity exceeds preceding buy fills"
            )

    inventory_mismatches: dict[str, dict[str, str]] = {}
    if current_positions is not None:
        broker_qty = _broker_quantities(current_positions)
        for symbol in sorted(set(net_qty) | set(broker_qty)):
            calculated = net_qty.get(symbol, Decimal(0))
            actual = broker_qty.get(symbol, Decimal(0))
            if calculated != actual:
                inventory_mismatches[symbol] = {
                    "fills_qty": _money(calculated),
                    "broker_qty": _money(actual),
                }
                basis_errors.setdefault(symbol, "fill-derived inventory disagrees with broker")
    invalid_symbols = set(basis_errors)

    period_sales = [
        sale for sale in sales
        if (since_at is None or sale["fill"].transaction_time >= since_at)
        and (until_at is None or sale["fill"].transaction_time < until_at)
    ]
    covered_pnl = Decimal(0)
    unattributed_pnl = Decimal(0)
    unattributed_qty = Decimal(0)
    strategy_pnl: dict[str, Decimal] = defaultdict(Decimal)
    strategy_basis: dict[str, Decimal] = defaultdict(Decimal)
    events = []
    for sale in period_sales:
        fill = sale["fill"]
        if fill.symbol in invalid_symbols:
            events.append({
                "activity_id": fill.activity_id,
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "side": fill.side,
                "qty": _money(fill.qty),
                "transaction_time": fill.transaction_time.isoformat(),
                "realized_pnl": None,
                "basis_status": "unknown",
                "reason": basis_errors[fill.symbol],
            })
            continue
        covered_pnl += sale["pnl"]
        allocations: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {"pnl": Decimal(0), "basis": Decimal(0), "qty": Decimal(0)}
        )
        for portion in sale["portions"]:
            strategy = portion["strategy"]
            if strategy:
                strategy_pnl[strategy] += portion["pnl"]
                strategy_basis[strategy] += portion["basis"]
                allocations[strategy]["pnl"] += portion["pnl"]
                allocations[strategy]["basis"] += portion["basis"]
                allocations[strategy]["qty"] += portion["qty"]
            else:
                unattributed_pnl += portion["pnl"]
                unattributed_qty += portion["qty"]
        events.append({
            "activity_id": fill.activity_id,
            "order_id": fill.order_id,
            "symbol": fill.symbol,
            "side": fill.side,
            "qty": _money(fill.qty),
            "transaction_time": fill.transaction_time.isoformat(),
            "realized_pnl": _money(sale["pnl"]),
            "basis_status": "covered",
            "strategy_pnl": {
                key: _money(value["pnl"]) for key, value in sorted(allocations.items())
            },
            "strategy_basis": {
                key: _money(value["basis"]) for key, value in sorted(allocations.items())
            },
            "strategy_qty": {
                key: _money(value["qty"]) for key, value in sorted(allocations.items())
            },
        })

    if current_positions is None:
        positions_by_symbol: dict[str, dict] = {}
    elif isinstance(current_positions, Mapping):
        positions_by_symbol = {
            str(symbol).upper().strip(): {
                "symbol": symbol,
                "qty": value.get("qty") if isinstance(value, dict) else value,
                **(value if isinstance(value, dict) else {}),
            }
            for symbol, value in current_positions.items()
        }
    else:
        positions_by_symbol = {
            str(row.get("symbol") or "").upper().strip(): row
            for row in current_positions if isinstance(row, dict)
        }

    open_activities = []
    strategy_open_pnl: dict[str, Decimal] = defaultdict(Decimal)
    strategy_open_basis: dict[str, Decimal] = defaultdict(Decimal)
    for symbol in sorted(set(long_lots) | set(short_lots)):
        is_long = bool(long_lots[symbol])
        lots = list(long_lots[symbol] if is_long else short_lots[symbol])
        quantity = sum((lot.qty for lot in lots), Decimal(0))
        if quantity <= 0:
            continue
        position = positions_by_symbol.get(symbol)
        mark_value = None
        if position is not None and symbol not in invalid_symbols:
            try:
                mark_value = abs(_decimal(position.get("market_value"), f"{symbol} market value"))
            except FillHistoryError:
                mark_value = None
        for lot in lots:
            basis = lot.qty * lot.price
            pnl = None
            if mark_value is not None:
                allocated_market_value = mark_value * lot.qty / quantity
                pnl = allocated_market_value - basis if is_long else basis - allocated_market_value
            if lot.strategy:
                strategy_open_basis[lot.strategy] += basis
                if pnl is not None:
                    strategy_open_pnl[lot.strategy] += pnl
            open_activities.append({
                "activity_id": None,
                "order_id": lot.order_id,
                "symbol": symbol,
                "side": "buy" if is_long else "sell_short",
                "qty": _money(lot.qty),
                "transaction_time": lot.transaction_time.isoformat(),
                "strategy": lot.strategy,
                "basis": _money(basis),
                "unrealized_pnl": _money(pnl) if pnl is not None else None,
                "basis_status": "covered" if pnl is not None else "unknown",
            })
    strategy_open_pnl_complete = all(
        item["basis_status"] == "covered" and item["strategy"]
        for item in open_activities
    )

    complete = not invalid_symbols
    return {
        "mode": mode,
        "fill_count": len(parsed),
        "sell_fill_count": sum(sale["fill"].side == "sell" for sale in period_sales),
        "closing_fill_count": len(period_sales),
        "realized_pnl": _money(covered_pnl) if complete else None,
        "covered_realized_pnl": _money(covered_pnl),
        "realized_pnl_complete": complete,
        "basis_unknown_symbols": dict(sorted(basis_errors.items())),
        "position_reconciliation": (
            "not_checked" if current_positions is None else
            "mismatch" if inventory_mismatches else "matched"
        ),
        "inventory_mismatches": inventory_mismatches,
        "strategy_pnl": {key: _money(value) for key, value in sorted(strategy_pnl.items())},
        "strategy_basis": {key: _money(value) for key, value in sorted(strategy_basis.items())},
        "strategy_open_pnl": {key: _money(value) for key, value in sorted(strategy_open_pnl.items())},
        "strategy_open_basis": {key: _money(value) for key, value in sorted(strategy_open_basis.items())},
        "strategy_open_pnl_complete": strategy_open_pnl_complete,
        "unattributed_realized_pnl": _money(unattributed_pnl),
        "unattributed_closed_qty": _money(unattributed_qty),
        "strategy_attribution_complete": (
            complete and unattributed_qty == 0 and not attribution_conflicts
            and not journal_conflicts and strategy_open_pnl_complete
        ),
        "journal_order_conflicts": journal_conflicts,
        "attribution_conflicts": sorted(attribution_conflicts),
        "realized_fills": events,
        "open_fills": open_activities,
        "period": {"since": since, "until": until},
        "basis_assumption": "FILL history includes all position-changing long and short share transactions; fees and corporate actions are excluded",
    }


def fetch_bracket_parent_links(
    account: AlpacaAccount,
    *,
    candidate_parent_order_ids: Iterable[str],
    unmatched_fill_order_ids: Iterable[str],
    http_get: Callable | None = None,
    max_requests: int = 500,
) -> tuple[dict[str, str], list[str]]:
    """Resolve only broker-proven bracket child IDs to journaled parents.

    First query each candidate parent with nested=true for its exact legs.
    Then query remaining fill orders for a parent_order_id and verify that
    parent is a bracket order for the same symbol and opposite side.
    Lookup failures leave the fill unattributed; they never guess a strategy.
    """
    if max_requests < 1:
        raise ValueError("max_requests must be positive")
    getter = http_get or requests.get
    parents = {str(value) for value in candidate_parent_order_ids if value}
    unmatched = {str(value) for value in unmatched_fill_order_ids if value}
    links: dict[str, str] = {}
    conflicting_children: set[str] = set()
    errors: list[str] = []
    cache: dict[tuple[str, bool], dict | None] = {}
    requests_made = 0
    limit_reported = False

    def lookup(order_id: str, nested: bool) -> dict | None:
        nonlocal requests_made, limit_reported
        key = (order_id, nested)
        if key in cache:
            return cache[key]
        if requests_made >= max_requests:
            if not limit_reported:
                errors.append("broker order lookup limit reached; attribution is incomplete")
                limit_reported = True
            return None
        requests_made += 1
        try:
            response = getter(
                f"{account.base_url.rstrip('/')}/orders/{quote(order_id, safe='')}",
                headers=account.headers,
                params={"nested": "true"} if nested else None,
                timeout=20,
            )
            if response.status_code == 404:
                cache[key] = None
                return None
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, dict):
                raise FillHistoryError("broker order response is not an object")
        except (requests.RequestException, ValueError, FillHistoryError) as exc:
            errors.append(f"broker order lookup {order_id}: {type(exc).__name__}: {exc}")
            cache[key] = None
            return None
        cache[key] = raw
        return raw

    for parent_id in sorted(parents):
        order = lookup(parent_id, True)
        if not order or str(order.get("id")) != parent_id:
            continue
        if str(order.get("order_class") or "").lower() != "bracket":
            continue
        parent_symbol = str(order.get("symbol") or "").upper()
        parent_side = str(order.get("side") or "").lower()
        for leg in order.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            child_id = str(leg.get("id") or "")
            if (
                child_id in unmatched
                and child_id not in conflicting_children
                and str(leg.get("symbol") or "").upper() == parent_symbol
                and str(leg.get("side") or "").lower() in {"buy", "sell"}
                and str(leg.get("side") or "").lower() != parent_side
            ):
                if child_id in links and links[child_id] != parent_id:
                    errors.append(f"conflicting broker bracket parents for {child_id}")
                    links.pop(child_id, None)
                    conflicting_children.add(child_id)
                else:
                    links[child_id] = parent_id

    for child_id in sorted(unmatched - set(links) - conflicting_children):
        child = lookup(child_id, False)
        if not child or str(child.get("id")) != child_id:
            continue
        parent_id = str(child.get("parent_order_id") or "")
        if parent_id not in parents:
            continue
        parent = lookup(parent_id, True)
        if not parent or str(parent.get("id")) != parent_id:
            continue
        if (
            str(parent.get("order_class") or "").lower() == "bracket"
            and str(child.get("symbol") or "").upper() == str(parent.get("symbol") or "").upper()
            and str(child.get("side") or "").lower() in {"buy", "sell"}
            and str(child.get("side") or "").lower() != str(parent.get("side") or "").lower()
        ):
            legs = parent.get("legs") or []
            if isinstance(legs, list) and legs and not any(
                isinstance(leg, dict) and str(leg.get("id") or "") == child_id
                for leg in legs
            ):
                errors.append(f"broker bracket parent {parent_id} omits claimed child {child_id}")
                continue
            links[child_id] = parent_id
    return links, errors


def get_account_fill_report(
    mode: str,
    journal_entries: Iterable[dict],
    *,
    since: str | None = None,
    until: str | None = None,
    http_get: Callable | None = None,
    max_fill_pages: int = 1000,
    max_order_requests: int = 500,
) -> dict:
    """GET and reconcile a complete read-only paper-account FILL report."""
    if mode not in {"swing", "day", "algo"}:
        raise ValueError("mode must be swing, day, or algo")
    getter = http_get or requests.get
    account = get_account_for_mode(mode)
    if not account.account_id or account.account_id == "unknown":
        raise FillHistoryError(f"{mode} account ID is not configured")
    broker_account = _get_json(account, "account", http_get=getter)
    if not isinstance(broker_account, dict):
        raise FillHistoryError("Alpaca account response is invalid")
    broker_ids = {
        str(broker_account[key])
        for key in ("id", "account_number")
        if broker_account.get(key)
    }
    if account.account_id not in broker_ids:
        raise FillHistoryError(f"{mode} credentials resolve to a different Alpaca account")

    journal_rows = list(journal_entries)
    activities = fetch_fill_activities(
        account, http_get=getter, max_pages=max_fill_pages,
    )
    positions = fetch_positions(account, http_get=getter)
    journal_by_order, _ = _journal_index(journal_rows, mode)
    fill_order_ids = {
        str(row.get("order_id") or "")
        for row in activities if isinstance(row, dict)
    }
    unmatched_ids = fill_order_ids - set(journal_by_order)
    parent_ids = {
        order_id for order_id, row in journal_by_order.items()
        if row.side in {"buy", "sell_short"}
    }
    parent_links, lookup_errors = ({}, [])
    if parent_ids and unmatched_ids:
        parent_links, lookup_errors = fetch_bracket_parent_links(
            account,
            candidate_parent_order_ids=parent_ids,
            unmatched_fill_order_ids=unmatched_ids,
            http_get=getter,
            max_requests=max_order_requests,
        )
    report = calculate_realized_pnl(
        activities,
        journal_rows,
        mode=mode,
        current_positions=positions,
        bracket_parent_by_child=parent_links,
        since=since,
        until=until,
    )
    report["order_lookup_errors"] = lookup_errors
    if lookup_errors:
        report["strategy_attribution_complete"] = False
    report["source"] = "Alpaca paper account FILL activities"
    report["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return report


__all__ = [
    "FillHistoryError",
    "calculate_realized_pnl",
    "fetch_bracket_parent_links",
    "fetch_fill_activities",
    "fetch_positions",
    "get_account_fill_report",
]
