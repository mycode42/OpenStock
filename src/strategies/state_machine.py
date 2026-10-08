"""Persistent lifecycle state for continuously managed rule strategies.

Signal rules remain pure functions in their strategy modules. This module owns
the order/position lifecycle shared by those strategies:
 FLAT -> ENTRY_PENDING -> OPEN -> EXIT_PENDING -> COOLDOWN -> FLAT.
Unverified broker holdings are QUARANTINED until their entry is attributable.
"""

from __future__ import annotations

from datetime import datetime, timezone


FLAT = "flat"
ENTRY_PENDING = "entry_pending"
OPEN = "open"
EXIT_PENDING = "exit_pending"
COOLDOWN = "cooldown"
QUARANTINED = "quarantined"
PENDING_PHASES = {ENTRY_PENDING, EXIT_PENDING}


def _state_for(states: dict, strategy: str, ticker: str) -> dict:
    defaults = {
        "phase": FLAT,
        "strategy": strategy,
        "ticker": ticker,
        "owner": None,
        "pending_action": None,
        "client_order_id": None,
        "order_id": None,
        "last_signal_bar": None,
        "cooldown_bar": None,
        "updated_at": None,
        "last_evaluated_bar": None,
        "stop_price": None,
        "take_profit": None,
        "entry_order_id": None,
        "entry_client_order_id": None,
        "entry_source": None,
        "quarantine_reason": None,
        "submission_outcome": None,
        "last_known_qty": None,
    }
    state = states.setdefault(strategy, {}).setdefault(ticker, {})
    for key, value in defaults.items():
        state.setdefault(key, value)
    return state


def _touch(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()


def _recent_transition(state: dict, seconds: int = 180) -> bool:
    try:
        updated = datetime.fromisoformat(str(state.get("updated_at", "")).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return 0 <= age < seconds
    except (TypeError, ValueError):
        return False


class RuleStrategyStateMachine:
    """Reconcile broker state and enforce legal per-strategy transitions."""

    def __init__(self, states: dict, position_owners: dict[str, str]):
        self.states = states
        self.position_owners = position_owners
        normalized = {}
        for ticker, owner in position_owners.items():
            symbol = str(ticker).upper()
            if symbol in normalized and normalized[symbol] != owner:
                raise ValueError(f"Conflicting owners for {symbol}")
            normalized[symbol] = owner
        position_owners.clear()
        position_owners.update(normalized)

    def observe(self, strategy: str, ticker: str, bar_time: str | None) -> None:
        state = _state_for(self.states, strategy, ticker.upper())
        state["last_evaluated_bar"] = bar_time

    def reconcile(self, positions: dict[str, dict], open_orders: list[dict]) -> None:
        """Use Alpaca positions and orders as truth after every portfolio scan."""
        normalized_positions = {str(key).upper(): value for key, value in positions.items()}
        orders_by_symbol: dict[str, list[dict]] = {}
        for order in open_orders:
            ticker = str(order.get("symbol", "")).upper()
            if ticker:
                orders_by_symbol.setdefault(ticker, []).append(order)

        # Restore ownership from an in-flight persisted entry if a restart
        # happened after the broker accepted or filled its order.
        for strategy, by_symbol in self.states.items():
            for ticker, state in by_symbol.items():
                ticker = str(ticker).upper()
                has_provenance = bool(state.get("entry_order_id") or state.get("entry_client_order_id"))
                if (
                    has_provenance
                    and state.get("phase") in {ENTRY_PENDING, OPEN, EXIT_PENDING}
                    and ticker in normalized_positions
                ):
                    self.position_owners.setdefault(ticker, strategy)

        for ticker, owner in list(self.position_owners.items()):
            ticker = str(ticker).upper()
            position = normalized_positions.get(ticker)
            quantity = float((position or {}).get("qty", 0) or 0)
            ticker_orders = orders_by_symbol.get(ticker, [])
            owner_state = _state_for(self.states, owner, ticker)
            matching_order = next(
                (order for order in ticker_orders
                 if (owner_state.get("client_order_id") and order.get("client_order_id") == owner_state.get("client_order_id"))
                 or (owner_state.get("order_id") and order.get("id") == owner_state.get("order_id"))),
                None,
            )

            if quantity > 0:
                owner_state["last_known_qty"] = quantity
                if owner_state.get("phase") == EXIT_PENDING and (matching_order or _recent_transition(owner_state)):
                    continue
                if owner_state.get("phase") == ENTRY_PENDING:
                    entry_order = matching_order if matching_order and matching_order.get("side") == "buy" else None
                    if entry_order and entry_order.get("id"):
                        owner_state["entry_order_id"] = str(entry_order["id"])
                        owner_state["entry_client_order_id"] = entry_order.get("client_order_id")
                        owner_state["entry_source"] = "coordinator"
                    elif owner_state.get("order_id"):
                        owner_state["entry_order_id"] = str(owner_state["order_id"])
                        owner_state["entry_client_order_id"] = owner_state.get("client_order_id")
                        owner_state["entry_source"] = "coordinator"
                owner_state.update({
                    "phase": OPEN,
                    "owner": owner,
                    "pending_action": None,
                    "client_order_id": None,
                    "order_id": None,
                })
                _touch(owner_state)
                continue

            if matching_order:
                continue

            if owner_state.get("phase") == ENTRY_PENDING and _recent_transition(owner_state):
                continue

            # Bracket children and completed strategy exits can close a
            # holding without a strategy market order. Release ownership only
            # after Alpaca reports no position and no remaining open order.
            if owner_state.get("phase") in {OPEN, EXIT_PENDING, ENTRY_PENDING}:
                owner_state.update({
                    "phase": COOLDOWN,
                    "owner": None,
                    "pending_action": None,
                    "client_order_id": None,
                    "order_id": None,
                    "cooldown_bar": owner_state.get("last_signal_bar"),
                    "stop_price": None,
                    "take_profit": None,
                })
                _touch(owner_state)
            self.position_owners.pop(ticker, None)
            owner_state["last_known_qty"] = None

        # Keep pending states until the matching Alpaca order resolves;
        # otherwise return an unfilled entry to a one-bar cooldown.
        for strategy, by_symbol in self.states.items():
            for ticker, state in by_symbol.items():
                ticker = str(ticker).upper()
                if (
                    state.get("phase") == OPEN
                    and ticker not in normalized_positions
                    and ticker not in self.position_owners
                ):
                    state.update({
                        "phase": COOLDOWN,
                        "owner": None,
                        "cooldown_bar": state.get("last_signal_bar"),
                        "stop_price": None,
                        "take_profit": None,
                    })
                    _touch(state)
                if state.get("phase") not in PENDING_PHASES:
                    continue
                if ticker in normalized_positions and state.get("pending_action") == "buy":
                    if state.get("order_id") and not state.get("entry_order_id"):
                        state["entry_order_id"] = str(state["order_id"])
                        state["entry_client_order_id"] = state.get("client_order_id")
                        state["entry_source"] = "coordinator"
                    state["phase"] = OPEN
                    state["owner"] = strategy
                    state["pending_action"] = None
                    state["client_order_id"] = None
                    state["order_id"] = None
                    self.position_owners.setdefault(ticker, strategy)
                    _touch(state)
                    continue
                order_exists = any(
                    (state.get("client_order_id") and order.get("client_order_id") == state["client_order_id"])
                    or (state.get("order_id") and order.get("id") == state["order_id"])
                    for order in orders_by_symbol.get(ticker, [])
                )
                if order_exists:
                    continue
                if _recent_transition(state):
                    continue
                if state.get("phase") == ENTRY_PENDING:
                    state["phase"] = COOLDOWN
                    state["owner"] = None
                    state["cooldown_bar"] = state.get("last_signal_bar")
                    state["stop_price"] = None
                    state["take_profit"] = None
                    if self.position_owners.get(ticker) == strategy:
                        self.position_owners.pop(ticker, None)
                else:
                    state["phase"] = OPEN if ticker in normalized_positions else COOLDOWN
                    state["owner"] = strategy if ticker in normalized_positions else None
                    if state["phase"] == COOLDOWN:
                        state["cooldown_bar"] = state.get("last_signal_bar")
                state["pending_action"] = None
                state["client_order_id"] = None
                state["order_id"] = None
                _touch(state)

        # A strategy-owned position may predate this state file. Seed an OPEN
        # machine so its rule module can continue to produce exits.
        for ticker, position in normalized_positions.items():
            if float(position.get("qty", 0) or 0) <= 0:
                continue
            owner = self.position_owners.get(ticker)
            if owner:
                state = _state_for(self.states, owner, ticker)
                state["last_known_qty"] = float(position.get("qty", 0) or 0)
                if state.get("phase") not in PENDING_PHASES:
                    state.update({"phase": OPEN, "owner": owner, "pending_action": None})

    def allows(self, strategy: str, ticker: str, action: str, bar_time: str | None) -> bool:
        ticker = ticker.upper()
        action = action.lower()
        state = _state_for(self.states, strategy, ticker)
        phase = state.get("phase", FLAT)
        if phase in PENDING_PHASES:
            return False
        if action == "buy":
            if ticker in self.position_owners:
                return False
            if phase == COOLDOWN and bar_time == state.get("cooldown_bar"):
                return False
            return phase in {FLAT, COOLDOWN}
        if action == "sell":
            return (
                phase == OPEN
                and self.position_owners.get(ticker) == strategy
                and bar_time != state.get("last_signal_bar")
            )
        return False

    def allows_protective_exit(self, strategy: str, ticker: str) -> bool:
        """A fresh stop quote may exit an owned holding without a new signal bar."""
        ticker = ticker.upper()
        state = _state_for(self.states, strategy, ticker)
        return state.get("phase") == OPEN and self.position_owners.get(ticker) == strategy

    def adopt_verified_position(
        self, strategy: str, ticker: str, *, entry_order_id: str,
        entry_client_order_id: str | None = None,
        source: str = "journal_broker_match",
        stop_price: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        """Attach a current broker holding only after its exact entry ID is proved."""
        ticker = ticker.upper()
        if not entry_order_id or not strategy or not ticker:
            raise ValueError("Verified adoption requires strategy, ticker, and entry order ID")
        current_owner = self.position_owners.get(ticker)
        if current_owner is not None and current_owner != strategy:
            raise ValueError(f"Cannot reassign {ticker} from {current_owner} to {strategy}")
        for other_strategy, by_symbol in self.states.items():
            if other_strategy == strategy:
                continue
            other = by_symbol.get(ticker)
            if other and other.get("phase") in {ENTRY_PENDING, OPEN, EXIT_PENDING}:
                raise ValueError(f"Another active state owns {ticker}: {other_strategy}")
        state = _state_for(self.states, strategy, ticker)
        old_entry_id = state.get("entry_order_id")
        if old_entry_id and old_entry_id != entry_order_id and state.get("phase") in {ENTRY_PENDING, OPEN, EXIT_PENDING}:
            raise ValueError(f"Conflicting active entry provenance for {strategy}/{ticker}")
        state.update({
            "phase": OPEN,
            "strategy": strategy,
            "ticker": ticker,
            "owner": strategy,
            "pending_action": None,
            "client_order_id": None,
            "order_id": None,
            "entry_order_id": str(entry_order_id),
            "entry_client_order_id": entry_client_order_id,
            "entry_source": source,
            "quarantine_reason": None,
        })
        if state.get("stop_price") is None and stop_price is not None:
            state["stop_price"] = stop_price
        if state.get("take_profit") is None and take_profit is not None:
            state["take_profit"] = take_profit
        self.position_owners[ticker] = strategy
        _touch(state)

    def quarantine_position(self, ticker: str, reason: str) -> None:
        """Release a broker holding whose persisted owner cannot be verified."""
        ticker = ticker.upper()
        owner = self.position_owners.pop(ticker, None)
        if owner is None:
            return
        state = _state_for(self.states, owner, ticker)
        state.update({
            "phase": QUARANTINED,
            "owner": None,
            "pending_action": None,
            "client_order_id": None,
            "order_id": None,
            "quarantine_reason": reason,
        })
        _touch(state)

    def prepare(self, trade: dict) -> None:
        """Persist the pending transition before contacting the executor."""
        strategy = str(trade.get("strategy", "unspecified"))
        ticker = str(trade.get("ticker", "")).upper()
        action = str(trade.get("action", "")).lower()
        if action not in {"buy", "sell"} or not ticker or not strategy:
            raise ValueError("Rule transition requires a buy/sell, strategy, and ticker")
        current_owner = self.position_owners.get(ticker)
        if action == "buy" and current_owner is not None:
            raise ValueError(f"Cannot prepare a new entry for owned {ticker}")
        if action == "sell" and current_owner != strategy:
            raise ValueError(f"Cannot exit {ticker} for non-owner {strategy}")
        state = _state_for(self.states, strategy, ticker)
        state.update({
            "phase": ENTRY_PENDING if action == "buy" else EXIT_PENDING,
            "strategy": strategy,
            "ticker": ticker,
            "owner": strategy,
            "pending_action": action,
            "client_order_id": trade.get("client_order_id"),
            "order_id": None,
            "last_signal_bar": trade.get("signal_bar_time"),
            "submission_outcome": "pending",
        })
        if action == "buy":
            state["stop_price"] = trade.get("stop_price")
            state["take_profit"] = trade.get("take_profit")
            state["entry_order_id"] = None
            state["entry_client_order_id"] = trade.get("client_order_id")
            state["entry_source"] = "coordinator"
            state["quarantine_reason"] = None
        _touch(state)
        if action == "buy":
            self.position_owners[ticker] = strategy

    def resolve(self, trade: dict, result: dict | None) -> None:
        """Retain accepted orders; cool down rejected entries and retry exits on a new bar."""
        strategy = str(trade.get("strategy", "unspecified"))
        ticker = str(trade.get("ticker", "")).upper()
        action = str(trade.get("action", "")).lower()
        state = _state_for(self.states, strategy, ticker)
        result = result or {}
        status = str(result.get("status") or "").lower()
        successful = bool(result.get("order_id")) and (
            result.get("success") is True or status in {"submitted", "executed"}
        )
        if successful:
            state["order_id"] = result.get("order_id")
            state["submission_outcome"] = "accepted"
            if action == "buy":
                self.position_owners[ticker] = strategy
                if result.get("order_id"):
                    state["entry_order_id"] = str(result["order_id"])
                    state["entry_client_order_id"] = trade.get("client_order_id")
                    state["entry_source"] = "coordinator"
            _touch(state)
            return

        # A failed subprocess, missing response row, or failed broker lookup
        # does not prove the broker rejected an order. Keep the persisted
        # client ID pending until Alpaca positions/orders resolve it.
        explicitly_rejected = status in {"blocked", "would_execute"} or (
            result.get("broker_status") in {"rejected", "canceled", "expired"}
        )
        if not explicitly_rejected:
            state["submission_outcome"] = "uncertain"
            _touch(state)
            return

        if action == "buy":
            state.update({
                "phase": COOLDOWN,
                "owner": None,
                "cooldown_bar": trade.get("signal_bar_time"),
                "stop_price": None,
                "take_profit": None,
                "entry_order_id": None,
                "entry_client_order_id": None,
                "entry_source": None,
            })
            if self.position_owners.get(ticker) == strategy:
                self.position_owners.pop(ticker, None)
        else:
            state.update({"phase": OPEN, "owner": strategy})
        state.update({"pending_action": None, "client_order_id": None, "order_id": None})
        state["submission_outcome"] = "rejected"
        _touch(state)


__all__ = ["RuleStrategyStateMachine", "FLAT", "ENTRY_PENDING", "OPEN", "EXIT_PENDING", "COOLDOWN", "QUARANTINED"]
