"""Persistent lifecycle state for continuously managed rule strategies.

Signal rules remain pure functions in their strategy modules. This module owns
the order/position lifecycle shared by those strategies:
 FLAT -> ENTRY_PENDING -> OPEN -> EXIT_PENDING -> COOLDOWN -> FLAT.
Unverified broker holdings are QUARANTINED until their entry is attributable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo


class LifecyclePhase(str, Enum):
    FLAT = "flat"
    ENTRY_PENDING = "entry_pending"
    OPEN = "open"
    EXIT_PENDING = "exit_pending"
    COOLDOWN = "cooldown"
    QUARANTINED = "quarantined"


PHASES = frozenset(phase.value for phase in LifecyclePhase)
ACTIVE_PHASES = frozenset({
    LifecyclePhase.ENTRY_PENDING.value,
    LifecyclePhase.OPEN.value,
    LifecyclePhase.EXIT_PENDING.value,
})
PENDING_PHASES = frozenset({
    LifecyclePhase.ENTRY_PENDING.value, LifecyclePhase.EXIT_PENDING.value,
})

# Persisted state remains string-compatible; these names keep transitions
# readable while LifecyclePhase owns the legal serialized values.
FLAT = LifecyclePhase.FLAT.value
ENTRY_PENDING = LifecyclePhase.ENTRY_PENDING.value
OPEN = LifecyclePhase.OPEN.value
EXIT_PENDING = LifecyclePhase.EXIT_PENDING.value
COOLDOWN = LifecyclePhase.COOLDOWN.value
QUARANTINED = LifecyclePhase.QUARANTINED.value
ET = ZoneInfo("America/New_York")


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
        "entry_action": None,
        "research_profile_id": None,
        "research_profile_regime": None,
        "entry_market_regime": None,
        "opened_at": None,
        "cooldown_until": None,
        "entry_day": None,
        "entries_today": 0,
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

    def __init__(
        self, states: dict, position_owners: dict[str, str], *,
        cooldown_minutes: dict[str, int] | None = None,
        minimum_hold_minutes: dict[str, int] | None = None,
        max_entries_per_day: dict[str, int] | None = None,
        max_positions_per_strategy: int | None = None,
        max_positions_per_strategy_by_strategy: dict[str, int] | None = None,
    ):
        from src.config import get_mode_config

        risk = get_mode_config("algo")["risk"]
        self.states = states
        self.position_owners = position_owners
        self.cooldown_minutes = dict(cooldown_minutes if cooldown_minutes is not None else risk.get("cooldown_minutes_by_strategy", {}))
        self.minimum_hold_minutes = dict(minimum_hold_minutes if minimum_hold_minutes is not None else risk.get("minimum_hold_minutes_by_strategy", {}))
        self.max_entries_per_day = dict(max_entries_per_day if max_entries_per_day is not None else risk.get("max_entries_per_strategy_per_day", {}))
        self.max_positions_per_strategy = (
            max_positions_per_strategy if max_positions_per_strategy is not None
            else risk.get("max_positions_per_strategy")
        )
        self.max_positions_per_strategy_by_strategy = dict(
            max_positions_per_strategy_by_strategy
            if max_positions_per_strategy_by_strategy is not None
            else risk.get("max_positions_per_strategy_by_strategy", {})
        )
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

            if quantity != 0:
                owner_state["last_known_qty"] = quantity
                if owner_state.get("phase") == EXIT_PENDING and (matching_order or _recent_transition(owner_state)):
                    continue
                if owner_state.get("phase") == ENTRY_PENDING:
                    expected_side = "sell" if owner_state.get("entry_action") == "short" else "buy"
                    entry_order = matching_order if matching_order and matching_order.get("side") == expected_side else None
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
                if not owner_state.get("opened_at"):
                    owner_state["opened_at"] = self._opened_at_from_signal(owner_state) or datetime.now(timezone.utc).isoformat()
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
                    "entry_action": None,
                    "opened_at": None,
                })
                self._set_cooldown(owner, owner_state)
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
                    self._set_cooldown(strategy, state)
                    _touch(state)
                if state.get("phase") not in PENDING_PHASES:
                    continue
                if ticker in normalized_positions and state.get("pending_action") in {"buy", "short"}:
                    quantity = float((normalized_positions[ticker] or {}).get("qty", 0) or 0)
                    expects_short = state.get("pending_action") == "short"
                    if (quantity < 0) != expects_short:
                        continue
                    if state.get("order_id") and not state.get("entry_order_id"):
                        state["entry_order_id"] = str(state["order_id"])
                        state["entry_client_order_id"] = state.get("client_order_id")
                        state["entry_source"] = "coordinator"
                    state["phase"] = OPEN
                    state["owner"] = strategy
                    state["last_known_qty"] = quantity
                    state["opened_at"] = self._opened_at_from_signal(state) or datetime.now(timezone.utc).isoformat()
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
                    self._set_cooldown(strategy, state)
                    if self.position_owners.get(ticker) == strategy:
                        self.position_owners.pop(ticker, None)
                else:
                    state["phase"] = OPEN if ticker in normalized_positions else COOLDOWN
                    state["owner"] = strategy if ticker in normalized_positions else None
                    if state["phase"] == COOLDOWN:
                        state["cooldown_bar"] = state.get("last_signal_bar")
                        self._set_cooldown(strategy, state)
                state["pending_action"] = None
                state["client_order_id"] = None
                state["order_id"] = None
                _touch(state)

        # A strategy-owned position may predate this state file. Seed an OPEN
        # machine so its rule module can continue to produce exits.
        for ticker, position in normalized_positions.items():
            if float(position.get("qty", 0) or 0) == 0:
                continue
            owner = self.position_owners.get(ticker)
            if owner:
                state = _state_for(self.states, owner, ticker)
                state["last_known_qty"] = float(position.get("qty", 0) or 0)
                if state.get("phase") not in PENDING_PHASES:
                    state.update({"phase": OPEN, "owner": owner, "pending_action": None})

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except (TypeError, ValueError):
            return None

    def _set_cooldown(self, strategy: str, state: dict) -> None:
        minutes = int(self.cooldown_minutes.get(strategy, 0))
        state["cooldown_until"] = (
            (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            if minutes else None
        )

    @classmethod
    def _opened_at_from_signal(cls, state: dict) -> str | None:
        signal_time = cls._parse_time(state.get("last_signal_bar"))
        return (signal_time + timedelta(minutes=5)).isoformat() if signal_time else None

    def allows(self, strategy: str, ticker: str, action: str, bar_time: str | None) -> bool:
        ticker = ticker.upper()
        action = action.lower()
        state = _state_for(self.states, strategy, ticker)
        phase = state.get("phase", FLAT)
        if phase in PENDING_PHASES:
            return False
        current_time = self._parse_time(bar_time)
        if action in {"buy", "short"}:
            if ticker in self.position_owners:
                return False
            if phase == COOLDOWN and bar_time == state.get("cooldown_bar"):
                return False
            if phase not in {FLAT, COOLDOWN}:
                return False
            cooldown_until = self._parse_time(state.get("cooldown_until"))
            if phase == COOLDOWN and cooldown_until and current_time and current_time < cooldown_until:
                return False
            if current_time:
                session_day = current_time.astimezone(ET).date().isoformat()
                entries_today = sum(
                    int(item.get("entries_today", 0) or 0)
                    for by_symbol in self.states.values() for item in by_symbol.values()
                    if item.get("strategy") == strategy and item.get("entry_day") == session_day
                )
                if entries_today >= int(self.max_entries_per_day.get(strategy, 10**9)):
                    return False
            position_cap = self.max_positions_per_strategy_by_strategy.get(
                strategy, self.max_positions_per_strategy,
            )
            if position_cap is not None and sum(
                owner == strategy for owner in self.position_owners.values()
            ) >= position_cap:
                return False
            return True
        if action in {"sell", "cover"}:
            if not (
                phase == OPEN
                and self.position_owners.get(ticker) == strategy
                and bar_time != state.get("last_signal_bar")
            ):
                return False
            quantity = float(state.get("last_known_qty") or 0)
            if action == "sell" and quantity < 0:
                return False
            if action == "cover" and quantity > 0:
                return False
            minimum_minutes = int(self.minimum_hold_minutes.get(strategy, 0))
            opened_at = self._parse_time(state.get("opened_at") or state.get("last_signal_bar"))
            if minimum_minutes and opened_at and current_time:
                if (current_time - opened_at).total_seconds() < minimum_minutes * 60:
                    return False
            return True
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
        entry_action: str = "buy",
        entry_at: str | None = None,
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
            "entry_action": entry_action,
            "opened_at": entry_at or state.get("opened_at") or self._opened_at_from_signal(state),
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
        if action not in {"buy", "sell", "short", "cover"} or not ticker or not strategy:
            raise ValueError("Rule transition requires a trade action, strategy, and ticker")
        is_entry = action in {"buy", "short"}
        current_owner = self.position_owners.get(ticker)
        if is_entry and current_owner is not None:
            raise ValueError(f"Cannot prepare a new entry for owned {ticker}")
        if not is_entry and current_owner != strategy:
            raise ValueError(f"Cannot exit {ticker} for non-owner {strategy}")
        state = _state_for(self.states, strategy, ticker)
        state.update({
            "phase": ENTRY_PENDING if is_entry else EXIT_PENDING,
            "strategy": strategy,
            "ticker": ticker,
            "owner": strategy,
            "pending_action": action,
            "client_order_id": trade.get("client_order_id"),
            "order_id": None,
            "last_signal_bar": trade.get("signal_bar_time"),
            "submission_outcome": "pending",
        })
        if is_entry:
            state["stop_price"] = trade.get("stop_price")
            state["take_profit"] = trade.get("take_profit")
            state["trailing_stop_pct"] = trade.get("trailing_stop_pct")
            state["trailing_activation_pct"] = trade.get("trailing_activation_pct")
            state["trailing_peak_price"] = None
            state["trailing_trough_price"] = None
            state["entry_order_id"] = None
            state["entry_client_order_id"] = trade.get("client_order_id")
            state["entry_source"] = "coordinator"
            state["research_profile_id"] = trade.get("research_profile_id")
            state["research_profile_regime"] = trade.get("research_profile_regime")
            state["entry_market_regime"] = trade.get("market_regime")
            state["entry_action"] = action
            state["opened_at"] = None
            state["quarantine_reason"] = None
            signal_time = self._parse_time(trade.get("signal_bar_time"))
            session_day = signal_time.astimezone(ET).date().isoformat() if signal_time else None
            if session_day != state.get("entry_day"):
                state["entry_day"] = session_day
                state["entries_today"] = 0
            state["entries_today"] = int(state.get("entries_today", 0) or 0) + 1
        _touch(state)
        if is_entry:
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
            if action in {"buy", "short"}:
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

        if action in {"buy", "short"}:
            self._set_cooldown(strategy, state)
            state.update({
                "phase": COOLDOWN,
                "owner": None,
                "cooldown_bar": trade.get("signal_bar_time"),
                "stop_price": None,
                "take_profit": None,
                "entry_order_id": None,
                "entry_client_order_id": None,
                "entry_source": None,
                "entry_action": None,
                "opened_at": None,
            })
            if self.position_owners.get(ticker) == strategy:
                self.position_owners.pop(ticker, None)
        else:
            state.update({"phase": OPEN, "owner": strategy})
        state.update({"pending_action": None, "client_order_id": None, "order_id": None})
        state["submission_outcome"] = "rejected"
        _touch(state)


__all__ = ["RuleStrategyStateMachine", "FLAT", "ENTRY_PENDING", "OPEN", "EXIT_PENDING", "COOLDOWN", "QUARANTINED"]
