"""Replay execution methods."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import math

from .config import BAR_MINUTES, ET, STRATEGY_NAMES
from .models import Position
from src.algo.strategies.catalog import INTRADAY_STRATEGIES

try:
    from ._execution_cy import build_timeline as _build_timeline_cy
except ImportError:
    _build_timeline_cy = None


class ReplayExecutionMixin:
    @staticmethod
    def _timestamp(bar: dict) -> datetime:
        timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)

    @classmethod
    def _is_complete(cls, bar: dict) -> bool:
        ingested_at = bar.get("_ingested_at")
        if not ingested_at:
            return True
        ingested = datetime.fromisoformat(str(ingested_at).replace("Z", "+00:00"))
        if ingested.tzinfo is None:
            ingested = ingested.replace(tzinfo=timezone.utc)
        return cls._timestamp(bar) + timedelta(minutes=BAR_MINUTES) <= ingested.astimezone(timezone.utc)

    def _timeline(self) -> dict[datetime, dict[str, dict]]:
        if self.precomputed_timeline is not None:
            return self.precomputed_timeline
        return build_replay_timeline(
            self.symbols, self.intraday_bars, self.start, self.end,
        )

    def _equity(self) -> float:
        marked = self.cash + sum(
            position.qty * self.last_prices.get(ticker, position.entry_price)
            for ticker, position in self.positions.items()
        )
        liquidation_rate = (self.spread_bps / 2 + self.slippage_bps) / 10_000
        exit_costs = sum(
            abs(position.qty * self.last_prices.get(ticker, position.entry_price)) * liquidation_rate
            + abs(position.qty) * self.commission_per_share
            for ticker, position in self.positions.items()
        )
        return marked - exit_costs

    def _fill_price(self, raw_price: float, action: str, *, extra_bps: float = 0.0) -> float:
        """Apply half-spread plus adverse slippage to a marketable fill."""
        cost = (self.spread_bps / 2 + self.slippage_bps + extra_bps) / 10_000
        return raw_price * (1 + cost if action in {"buy", "cover"} else 1 - cost)

    def _borrow_status(self, ticker: str, timestamp: datetime) -> bool | None:
        day = timestamp.astimezone(ET).date().isoformat()
        status = self.borrow_availability.get((ticker.upper(), day))
        self.borrow_coverage["known_easy" if status is True else "known_not_easy" if status is False else "unknown"] += 1
        return status

    def _record_holdings(self, trading_day: date) -> None:
        day_key = trading_day.isoformat()
        for ticker, position in self.positions.items():
            direction = "short" if position.qty < 0 else "long"
            self.holdings_by_day[day_key][position.strategy].add((ticker, direction))

    def _charge_daily_borrow(self) -> None:
        if self.short_borrow_annual_pct <= 0:
            return
        for ticker, position in self.positions.items():
            if position.qty >= 0 or position.strategy in INTRADAY_STRATEGIES:
                continue
            mark = self.last_prices.get(ticker, position.entry_price)
            fee = abs(position.qty) * mark * self.short_borrow_annual_pct / 252
            self.cash -= fee
            position.borrow_cost += fee
            self.borrow_cost_total += fee

    def _flatten_positions(
        self, bars: dict[str, dict], timestamp: datetime,
        *, strategies: set[str] | None = None, reason: str,
    ) -> None:
        """Close eligible positions at a modeled marketable fill through RM."""
        if strategies is None and not self.force_close_at_end:
            return
        for ticker, position in list(self.positions.items()):
            if strategies is not None and position.strategy not in strategies:
                continue
            bar = bars.get(ticker)
            if bar is None:
                self.rejections["intraday_eod_flatten_missing_bar"] += 1
                continue
            action = "cover" if position.qty < 0 else "sell"
            state = self._risk_state()
            result = self._trade_validator(
                ticker=ticker, action=action, qty=abs(position.qty),
                entry_price=float(bar["c"]), portfolio_state=state, mode="algo",
                strategy=position.strategy,
            )
            if not result.approved:
                self.rejections[f"coordinator_flatten:{result.rule or 'risk_rejected'}"] += 1
                continue
            fill = self._fill_price(float(bar["c"]), action)
            self._close_position(ticker, fill, timestamp, reason)
    def _strategy_total_pnl(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for trade in self.closed_trades:
            totals[str(trade["strategy"])] += float(trade["pnl"])
        exit_cost_rate = (self.spread_bps / 2 + self.slippage_bps) / 10_000
        for ticker, position in self.positions.items():
            mark = self.last_prices.get(ticker, position.entry_price)
            estimated_exit_cost = abs(position.qty * mark) * exit_cost_rate
            estimated_exit_commission = abs(position.qty) * self.commission_per_share
            totals[position.strategy] += (
                position.qty * (mark - position.entry_price)
                - position.entry_commission - estimated_exit_commission
                - estimated_exit_cost - position.borrow_cost
            )
        return totals

    def _record_daily_strategy_pnl(self, trading_day: date) -> None:
        totals = self._strategy_total_pnl()
        daily = {
            strategy: round(totals.get(strategy, 0.0) - self._last_strategy_pnl.get(strategy, 0.0), 4)
            for strategy in sorted(STRATEGY_NAMES)
        }
        self._last_strategy_pnl = defaultdict(float, totals)
        self.daily_strategy_pnl.append({"date": trading_day.isoformat(), "pnl_by_strategy": daily})

    def _start_periods(self, trading_day: date) -> None:
        day_key = trading_day.isoformat()
        week_key = trading_day.isocalendar()[:2]
        if self.day_key != day_key:
            self.day_key = day_key
            self.day_start_equity = self._equity()
        if self.week_key != week_key:
            self.week_key = week_key
            self.week_start_equity = self._equity()

    def _risk_state(self) -> dict:
        equity = self._equity()
        positions = {}
        sectors: dict[str, float] = defaultdict(float)
        for ticker, position in self.positions.items():
            price = self.last_prices.get(ticker, position.entry_price)
            market_value = position.qty * price
            positions[ticker] = {
                "symbol": ticker, "qty": position.qty, "market_value": market_value,
                "current_price": price, "pct_of_equity": market_value / equity if equity else 0,
            }
            sector = self.ticker_sector.get(ticker, "other")
            sectors[sector] += abs(market_value)
        pending_value = {
            ticker: float(order["qty"]) * float(order["reference_price"])
            for ticker, order in self.pending_entries.items()
            if order.get("action", "buy") == "buy"
        }
        pending_short_value = {
            ticker: float(order["qty"]) * float(order["reference_price"])
            for ticker, order in self.pending_entries.items()
            if order.get("action") == "short"
        }
        for ticker, value in pending_value.items():
            sectors[self.ticker_sector.get(ticker, "other")] += value
        for ticker, value in pending_short_value.items():
            sectors[self.ticker_sector.get(ticker, "other")] += value
        pending_new = {ticker for ticker in pending_value if ticker not in positions}
        pending_new.update(ticker for ticker in pending_short_value if ticker not in positions)
        day_pnl = (equity / self.day_start_equity - 1) if self.day_start_equity else 0
        week_pnl = (equity / self.week_start_equity - 1) if self.week_start_equity else 0
        entry_count = self.entry_counts[self.day_key or ""]
        return {
            "equity": equity,
            "cash": self.cash,
            "cash_pct": self.cash / equity if equity else 0,
            "available_cash": self.cash - sum(pending_value.values()),
            "available_cash_pct": (self.cash - sum(pending_value.values())) / equity if equity else 0,
            "pending_buy_reservation": sum(pending_value.values()),
            "pending_buy_value_by_ticker": pending_value,
            "pending_short_value_by_ticker": pending_short_value,
            "shortability_available": True,
            "easy_to_borrow_symbols": list(self.symbols),
            "pending_new_symbols": sorted(pending_new),
            "pending_exposure_available": True,
            "daily_pnl_pct": day_pnl,
            "weekly_pnl_pct": week_pnl,
            "positions": positions,
            "sector_alloc": dict(sectors),
            "trade_count_today": entry_count,
            "entry_count_today": entry_count,
            "entry_count_available": True,
            "open_position_count": len(positions) + len(pending_new),
        }

    def _close_position(self, ticker: str, price: float, timestamp: datetime, reason: str) -> None:
        position = self.positions.pop(ticker)
        self.last_exit_at[ticker] = timestamp
        self.last_exit_at_by_strategy[(ticker, position.strategy)] = timestamp
        exit_value = position.qty * price
        self.cash += exit_value
        exit_commission = abs(position.qty) * self.commission_per_share
        self.cash -= exit_commission
        self.commission_total += exit_commission
        pnl = position.qty * (price - position.entry_price) - position.entry_commission - exit_commission - position.borrow_cost
        self.closed_trades.append({
            "ticker": ticker,
            "strategy": position.strategy,
            "direction": "short" if position.qty < 0 else "long",
            "qty": position.qty,
            "entry_at": position.entry_at,
            "entry_price": round(position.entry_price, 6),
            "signal_at": position.signal_at,
            "exit_at": timestamp.isoformat(),
            "exit_price": round(price, 6),
            "exit_reason": reason,
            "pnl": round(pnl, 2),
            "entry_commission": round(position.entry_commission, 4),
            "exit_commission": round(exit_commission, 4),
            "borrow_cost": round(position.borrow_cost, 4),
            "return_pct": round(
                ((position.entry_price - price) / position.entry_price)
                if position.qty < 0 else (price / position.entry_price - 1),
                6,
            ),
            "holding_minutes": max(BAR_MINUTES, position.bars_held * BAR_MINUTES),
            "satellite_confirmations": list(position.satellite_confirmations),
        })

    def _check_brackets(self, ticker: str, bar: dict, timestamp: datetime) -> None:
        position = self.positions.get(ticker)
        if position is None:
            return
        opening, high, low = (float(bar[key]) for key in ("o", "h", "l"))
        is_short = position.qty < 0
        stop_hit = high >= position.stop_price if is_short else low <= position.stop_price
        target_hit = low <= position.take_profit if is_short else high >= position.take_profit
        if (opening >= position.stop_price if is_short else opening <= position.stop_price):
            raw_fill, reason = opening, "stop_gap"
        elif (opening <= position.take_profit if is_short else opening >= position.take_profit):
            raw_fill, reason = opening, "target_gap"
        elif stop_hit:
            # If both bracket legs occur within one bar, assume the adverse leg.
            raw_fill, reason = position.stop_price, "stop"
        elif target_hit:
            raw_fill, reason = position.take_profit, "target"
        else:
            # A stop tightened from today's high/low is active next bar. This
            # avoids assuming an intrabar price sequence the data cannot prove.
            if position.trailing_stop_pct and position.trailing_activation_pct:
                if is_short:
                    extreme = min(position.favorable_extreme or position.entry_price, low)
                    favorable = (position.entry_price - extreme) / position.entry_price
                    if favorable >= position.trailing_activation_pct:
                        position.stop_price = min(
                            position.stop_price, extreme * (1 + position.trailing_stop_pct),
                        )
                else:
                    extreme = max(position.favorable_extreme or position.entry_price, high)
                    favorable = (extreme - position.entry_price) / position.entry_price
                    if favorable >= position.trailing_activation_pct:
                        position.stop_price = max(
                            position.stop_price, extreme * (1 - position.trailing_stop_pct),
                        )
                position.favorable_extreme = extreme
            position.bars_held += 1
            return
        exit_action = "cover" if is_short else "sell"
        adverse_fill = self._fill_price(raw_fill, exit_action)
        self._close_position(ticker, adverse_fill, timestamp, reason)

    def _fill_pending_entries(self, bars: dict[str, dict], timestamp: datetime) -> None:
        for ticker in list(self.pending_entries):
            bar = bars.get(ticker)
            if bar is None:
                continue
            order = self.pending_entries.pop(ticker)
            action = order.get("action", "buy")
            borrow_status = self._borrow_status(ticker, timestamp) if action == "short" else True
            if action == "short" and borrow_status is False:
                self.rejections["hard_to_borrow_at_fill"] += 1
                continue
            extra_bps = self.unknown_borrow_penalty_bps if action == "short" and borrow_status is None else 0.0
            fill_price = self._fill_price(float(bar["o"]), action, extra_bps=extra_bps)
            signed_qty = int(order["qty"]) if action == "buy" else -int(order["qty"])
            required_cash = fill_price * abs(signed_qty)
            if action == "buy" and required_cash > self.cash - self._equity() * float(self.risk["min_cash_pct"]):
                self.rejections["fill_cash_gap"] += 1
                continue
            commission = abs(signed_qty) * self.commission_per_share
            self.cash += required_cash if action == "short" else -required_cash
            self.cash -= commission
            self.commission_total += commission
            self.positions[ticker] = Position(
                ticker=ticker, strategy=order["strategy"], qty=signed_qty,
                entry_price=fill_price, entry_at=timestamp.isoformat(),
                stop_price=order["stop_price"], take_profit=order["take_profit"],
                signal_at=order["signal_at"], bars_held=0,
                satellite_confirmations=list(order.get("satellite_confirmations", [])),
                trailing_stop_pct=order.get("trailing_stop_pct"),
                trailing_activation_pct=order.get("trailing_activation_pct"),
                favorable_extreme=fill_price,
                entry_commission=commission,
            )

    def _fill_pending_exits(self, bars: dict[str, dict], timestamp: datetime) -> None:
        for ticker in list(self.pending_exits):
            bar = bars.get(ticker)
            if bar is None:
                continue
            order = self.pending_exits[ticker]
            signal_at = datetime.fromisoformat(order["signal_at"])
            if timestamp < signal_at:
                continue
            self.pending_exits.pop(ticker)
            if ticker in self.positions:
                is_short = self.positions[ticker].qty < 0
                action = "cover" if is_short else "sell"
                fill_price = self._fill_price(float(bar["o"]), action)
                self._close_position(ticker, fill_price, timestamp, order["reason"])

    def _queue_exit_signal(self, ticker: str, signal: dict, bar: dict, timestamp: datetime) -> None:
        position = self.positions.get(ticker)
        if position is None or position.strategy != signal.get("strategy") or ticker in self.pending_exits:
            return
        action = "cover" if position.qty < 0 else "sell"
        minimum_hold = int(self.risk.get("minimum_hold_minutes_by_strategy", {}).get(position.strategy, 0))
        entry_at = datetime.fromisoformat(position.entry_at.replace("Z", "+00:00"))
        if (timestamp - entry_at).total_seconds() < minimum_hold * 60:
            self.rejections["minimum_holding_period"] += 1
            return
        result = self._trade_validator(
            ticker=ticker, action=action, qty=abs(position.qty),
            entry_price=float(bar["c"]), portfolio_state=self._risk_state(), mode="algo",
            strategy=position.strategy,
        )
        if not result.approved:
            self.rejections[str(result.rule or "risk_rejected")] += 1
            return
        self.pending_exits[ticker] = {
            "reason": signal["reason"],
            "action": action,
            "signal_at": (timestamp + timedelta(minutes=BAR_MINUTES)).isoformat(),
        }


def build_replay_timeline(
    symbols: list[str], intraday_bars: dict[str, list[dict]], start: date, end: date,
) -> dict[datetime, dict[str, dict]]:
    """Build an event map for replays that share the same bars and date range."""
    if _build_timeline_cy is not None:
        return _build_timeline_cy(
            symbols, intraday_bars, start, end, ET, BAR_MINUTES,
        )
    events: dict[datetime, dict[str, dict]] = defaultdict(dict)
    for ticker in symbols:
        for bar in intraday_bars.get(ticker, []):
            timestamp = ReplayExecutionMixin._timestamp(bar)
            local = timestamp.astimezone(ET)
            if not start <= local.date() <= end or not ReplayExecutionMixin._is_complete(bar):
                continue
            local_time = local.time().replace(tzinfo=None)
            if not time(9, 30) <= local_time < time(16, 0):
                continue
            values = [float(bar[key]) for key in ("o", "h", "l", "c")]
            if not all(math.isfinite(value) and value > 0 for value in values):
                continue
            events[timestamp][ticker] = bar
    return dict(sorted(events.items()))

