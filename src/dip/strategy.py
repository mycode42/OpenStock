"""Pure Dip Mode decision state machine; no broker, clock, or logging calls."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, date
from typing import Any


@dataclass
class DipConfig:
    dip_percent: float = 1.0
    confirmation_seconds: int = 30
    target_percent: float = 1.5
    stop_loss_percent: float = 1.0
    reentry_enabled: bool = True
    minimum_dip_from_last_sell_percent: float = 0.8
    max_opening_gap_percent: float | None = 3.0
    max_trades_per_day: int = 5
    max_trades_per_instrument_per_day: int = 2
    max_position_size: float = 5000.0
    max_total_exposure: float = 10000.0
    max_daily_loss: float | None = None
    reference: str = "previous_close"


@dataclass
class InstrumentState:
    symbol: str
    session_date: str = ""
    status: str = "PRE_OPEN"
    reference_price: float | None = None
    reference_date: str | None = None
    opening_price: float | None = None
    current_price: float | None = None
    last_price_time: str | None = None
    dip_since: str | None = None
    last_sell_price: float | None = None
    entry_price: float | None = None
    quantity: int = 0
    position_value: float = 0.0
    pending_order_id: str | None = None
    pending_client_order_id: str | None = None
    pending_side: str | None = None
    pending_since: str | None = None
    pending_filled_qty: int = 0
    pending_fill_counted: bool = False
    last_completed_order_id: str | None = None
    last_completed_client_order_id: str | None = None
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    gap_rejected: bool = False
    entry_rejected: bool = False
    last_reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstrumentState":
        allowed = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class DipEngine:
    config: DipConfig
    states: dict[str, InstrumentState] = field(default_factory=dict)
    session_date: str = ""
    total_trades_today: int = 0
    realized_pnl_today: float = 0.0

    def state(self, symbol: str) -> InstrumentState:
        state = self.states.setdefault(symbol, InstrumentState(symbol=symbol))
        if not state.session_date and self.session_date:
            state.session_date = self.session_date
        return state

    def begin_session(self, day: date | str) -> None:
        value = day.isoformat() if isinstance(day, date) else str(day)
        if value == self.session_date:
            return
        self.session_date = value
        self.total_trades_today = 0
        self.realized_pnl_today = 0.0
        for state in self.states.values():
            state.session_date = value
            if state.quantity:
                state.status = "POSITION_OPEN"
            elif state.pending_side:
                state.status = "PENDING_BUY" if state.pending_side == "buy" else "PENDING_SELL"
            else:
                state.status = "OPENING"
            state.opening_price = None
            state.dip_since = None
            state.trades_today = 0
            state.realized_pnl_today = 0.0
            state.gap_rejected = False
            state.entry_rejected = False
            state.last_reason = None

    def set_reference(self, symbol: str, price: float, session_date: str | None = None) -> None:
        if price <= 0:
            raise ValueError("Reference price must be positive")
        s = self.state(symbol)
        s.reference_price = float(price)
        s.reference_date = session_date or self.session_date or None
        if s.status == "PRE_OPEN":
            s.status = "OPENING"

    def mark_existing_position(self, symbol: str, qty: int, entry_price: float) -> None:
        """Recover broker-held position on startup; prevents another buy."""
        s = self.state(symbol)
        if qty:
            s.quantity = abs(int(qty))
            s.entry_price = float(entry_price)
            s.position_value = s.quantity * s.entry_price
            s.status = "POSITION_OPEN"

    def set_pending(self, symbol: str, side: str, client_order_id: str, order_id: str | None = None) -> None:
        s = self.state(symbol)
        if s.pending_client_order_id != client_order_id:
            s.pending_filled_qty = 0
            s.pending_fill_counted = False
        s.pending_side = side
        s.pending_client_order_id = client_order_id
        s.pending_order_id = order_id
        s.pending_since = datetime.now().astimezone().isoformat()
        s.status = "PENDING_BUY" if side == "buy" else "PENDING_SELL"

    def order_update(self, symbol: str, order: dict[str, Any]) -> None:
        s = self.state(symbol)
        order_id = order.get("id")
        client_order_id = order.get("client_order_id")
        status = str(order.get("status", "")).lower()
        side = str(order.get("side", s.pending_side or "")).lower()
        if status == "filled" and (
            (order_id and order_id == s.last_completed_order_id)
            or (client_order_id and client_order_id == s.last_completed_client_order_id)
        ):
            self._clear_pending(s)
            s.status = "POSITION_OPEN" if s.quantity else ("WAITING_FOR_NEXT_DIP" if self.config.reentry_enabled and s.trades_today else "WAITING_FOR_DIP")
            return
        filled_qty = int(float(order.get("filled_qty") or 0))
        fill_price = float(order.get("filled_avg_price") or s.current_price or 0)
        if status in {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace"}:
            s.status = "PENDING_BUY" if side == "buy" else "PENDING_SELL"
            if filled_qty and side == "buy":
                s.quantity = filled_qty
                s.entry_price = fill_price or s.entry_price
                s.position_value = s.quantity * (s.entry_price or 0)
                if not s.pending_fill_counted:
                    s.trades_today += 1
                    self.total_trades_today += 1
                    s.pending_fill_counted = True
            elif filled_qty and side == "sell":
                delta = max(0, filled_qty - s.pending_filled_qty)
                if delta:
                    pnl = (fill_price - (s.entry_price or fill_price)) * delta
                    s.realized_pnl_today += pnl
                    self.realized_pnl_today += pnl
                    s.quantity = max(0, s.quantity - delta)
                    s.position_value = s.quantity * (s.entry_price or 0)
                s.pending_filled_qty = filled_qty
            return
        if status == "filled":
            if side == "buy":
                s.quantity = filled_qty or s.quantity
                s.entry_price = fill_price or s.current_price
                s.position_value = s.quantity * (s.entry_price or 0)
                if not s.pending_fill_counted:
                    s.trades_today += 1
                    self.total_trades_today += 1
                s.status = "POSITION_OPEN"
                s.entry_rejected = False
            elif side == "sell":
                qty = max(0, (filled_qty or s.quantity) - s.pending_filled_qty)
                pnl = (fill_price - (s.entry_price or fill_price)) * qty
                s.realized_pnl_today += pnl
                self.realized_pnl_today += pnl
                s.last_sell_price = fill_price
                s.quantity = max(0, s.quantity - qty)
                s.position_value = s.quantity * (s.entry_price or 0)
                if s.quantity == 0:
                    s.entry_price = None
                    s.status = "WAITING_FOR_NEXT_DIP" if self.config.reentry_enabled else "DONE"
                else:
                    s.status = "POSITION_OPEN"
            s.last_completed_order_id = order_id or s.last_completed_order_id
            s.last_completed_client_order_id = client_order_id or s.last_completed_client_order_id
            self._clear_pending(s)
        elif status in {"canceled", "cancelled", "rejected", "expired", "done_for_day", "replaced"}:
            s.status = "POSITION_OPEN" if s.quantity else "WAITING_FOR_DIP"
            if side == "sell" and s.quantity == 0:
                s.last_sell_price = fill_price or s.last_sell_price
                s.status = "WAITING_FOR_NEXT_DIP" if self.config.reentry_enabled else "DONE"
            s.last_reason = f"order {status}"
            if side == "buy":
                s.dip_since = None
                s.entry_rejected = True
            self._clear_pending(s)

    def on_price(
        self,
        symbol: str,
        price: float,
        timestamp: datetime,
        *,
        in_trading_window: bool,
        market_open: bool,
        data_fresh: bool = True,
        existing_position: bool = False,
        pending_order: bool = False,
        available_buying_power: float = float("inf"),
        total_exposure: float = 0.0,
        daily_loss: float = 0.0,
    ) -> dict[str, Any] | None:
        s = self.state(symbol)
        if price <= 0:
            return None
        s.current_price = float(price)
        s.last_price_time = timestamp.isoformat()
        if not data_fresh or not market_open:
            s.last_reason = "stale market data" if not data_fresh else "market closed"
            return None
        if s.reference_price is None:
            s.last_reason = "reference price unavailable"
            return None

        if s.opening_price is None:
            s.opening_price = price
            gap = (price / s.reference_price - 1) * 100
            if self.config.max_opening_gap_percent is not None and gap <= -abs(self.config.max_opening_gap_percent):
                s.gap_rejected = True
                s.status = "WAITING_FOR_DIP"
                s.last_reason = "opening gap exceeds configured limit"
                return None
            s.status = "WAITING_FOR_DIP"

        if pending_order or s.pending_side:
            s.last_reason = "pending order"
            return None

        if s.quantity and s.entry_price:
            s.status = "POSITION_OPEN"
            change = (price / (s.entry_price or price) - 1) * 100
            if change >= self.config.target_percent - 1e-9:
                return {"action": "sell", "reason": "take_profit", "symbol": symbol, "price": price}
            if change <= -abs(self.config.stop_loss_percent) + 1e-9:
                return {"action": "sell", "reason": "stop_loss", "symbol": symbol, "price": price}
            return None
        if existing_position:
            s.status = "POSITION_OPEN"
            s.last_reason = "existing position"
            return None

        if s.entry_rejected:
            threshold = (
                s.last_sell_price * (1 - abs(self.config.minimum_dip_from_last_sell_percent) / 100)
                if s.trades_today and s.last_sell_price
                else s.reference_price * (1 - abs(self.config.dip_percent) / 100)
            )
            if price > threshold:
                s.entry_rejected = False
                s.dip_since = None
            else:
                s.last_reason = "previous entry rejected; waiting for threshold reset"
                return None

        if not in_trading_window:
            s.status = "WINDOW_EXPIRED"
            s.last_reason = "trading window expired"
            return None
        if s.gap_rejected:
            return None
        if self.total_trades_today >= self.config.max_trades_per_day:
            s.last_reason = "maximum daily trades reached"
            return None
        if s.trades_today >= self.config.max_trades_per_instrument_per_day:
            s.last_reason = "maximum instrument trades reached"
            return None
        if self.config.max_daily_loss is not None and daily_loss <= -abs(self.config.max_daily_loss):
            s.last_reason = "maximum daily loss reached"
            return None

        dip_pct = (price / s.reference_price - 1) * 100
        required_dip = self.config.dip_percent
        if s.trades_today:
            if not self.config.reentry_enabled:
                s.status = "DONE"
                return None
            if s.last_sell_price is None:
                return None
            from_last_sell = (price / s.last_sell_price - 1) * 100
            if from_last_sell > -abs(self.config.minimum_dip_from_last_sell_percent):
                s.status = "WAITING_FOR_NEXT_DIP"
                s.last_reason = "re-entry threshold not met"
                s.dip_since = None
                return None
            required_dip = abs(self.config.minimum_dip_from_last_sell_percent)
            s.status = "WAITING_FOR_NEXT_DIP"
        threshold_met = (
            from_last_sell <= -abs(required_dip)
            if s.trades_today
            else dip_pct <= -abs(required_dip)
        )
        if not threshold_met:
            s.status = "WAITING_FOR_DIP" if s.trades_today == 0 else "WAITING_FOR_NEXT_DIP"
            s.dip_since = None
            return None

        if s.dip_since is None:
            s.dip_since = timestamp.isoformat()
            s.status = "WAITING_FOR_DIP" if s.trades_today == 0 else "WAITING_FOR_NEXT_DIP"
            if self.config.confirmation_seconds > 0:
                return None
        elapsed = (timestamp - datetime.fromisoformat(s.dip_since)).total_seconds()
        if elapsed < self.config.confirmation_seconds:
            return None

        qty = int(min(self.config.max_position_size, max(0.0, available_buying_power), max(0.0, self.config.max_total_exposure - total_exposure)) // price)
        if qty <= 0:
            s.last_reason = "insufficient buying power or exposure limit"
            return None
        s.last_reason = None
        return {"action": "buy", "reason": "dip_confirmed", "symbol": symbol, "price": price, "quantity": qty, "notional": qty * price}

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "total_trades_today": self.total_trades_today,
            "realized_pnl_today": self.realized_pnl_today,
            "instruments": {k: asdict(v) for k, v in self.states.items()},
        }

    @classmethod
    def restore(cls, config: DipConfig, raw: dict[str, Any]) -> "DipEngine":
        engine = cls(config=config)
        engine.session_date = raw.get("session_date", "")
        engine.total_trades_today = int(raw.get("total_trades_today", 0))
        engine.realized_pnl_today = float(raw.get("realized_pnl_today", 0))
        engine.states = {k: InstrumentState.from_dict(v) for k, v in raw.get("instruments", {}).items()}
        return engine

    @staticmethod
    def _clear_pending(state: InstrumentState) -> None:
        state.pending_order_id = None
        state.pending_client_order_id = None
        state.pending_side = None
        state.pending_since = None
        state.pending_filled_qty = 0
        state.pending_fill_counted = False
