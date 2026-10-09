"""Deterministic regular-session portfolio replay for the live Algo rules.

This is a research simulator, not an order router. Signals use completed bars;
orders fill no earlier than the next bar's open with configured slippage.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
import hashlib
import json
import logging
import math
import statistics
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

from risk_manager import _get_universe_maps, validate_trade
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from src.config import STRATEGY_CONFIG, get_mode_config
from src.market_data import data_tier, regular_feed
from src.strategies.registry import (
    ALGO_DEFAULT_PARAMETERS,
    validate_parameter_profile,
)
from src.strategies.event_driven import EventCalendar, EventSignalGenerator, EventStrategyConfig
from src.strategies.regime import rank_strategies
from src.strategies.runner import _make_trade
from src.strategies.rule_pipeline import evaluate_rule_signal
from src.strategies.multi_factor.config import MultiFactorConfig
from src.strategies.multi_factor.diagnostics import information_coefficient, turnover
from src.strategies.multi_factor.features import aggregate_intraday_bars
from src.strategies.multi_factor.signals import SignalGenerator
from src.strategies.swing_momentum import generate_signals as generate_swing_momentum_signals
from src.strategies.satellite_overlay import (
    CORE_STRATEGIES, SATELLITE_STRATEGIES, SatelliteOverlayConfig,
    apply_satellite_confirmations,
)
from src.strategies.portfolio_coordinator import (
    INTRADAY_STRATEGIES, PortfolioCoordinator, PortfolioCoordinatorConfig,
    strategy_priority, throttle_signal,
)


ET = ZoneInfo("America/New_York")
BAR_MINUTES = 5
DAILY_WARMUP_DAYS = 180
MIN_REGIME_DAILY_BARS = 22
FACTOR_WARMUP_DAYS = 260
STRATEGY_NAMES = set(ALGO_DEFAULT_PARAMETERS)
RESEARCH_CONTROL_KEYS = {
    "max_entries_per_day", "max_open_positions", "max_positions_per_strategy",
    "max_entries_per_strategy_per_day", "cooldown_minutes",
    "cooldown_minutes_by_strategy",
    "one_orb_attempt_per_ticker_per_day",
}


def multi_factor_warmup_days(profile: dict | None = None) -> int:
    """Calendar-day history needed for the longest configured daily factor."""
    settings = (profile or {}).get("multi_factor") if isinstance(profile, dict) else None
    config = MultiFactorConfig.from_dict(settings)
    return max(FACTOR_WARMUP_DAYS, config.warmup_calendar_days())


def validate_strategy_filter(strategies: list[str] | None) -> list[str] | None:
    if strategies is None:
        return None
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("strategy filter must be a non-empty list")
    if any(not isinstance(name, str) or name not in STRATEGY_NAMES for name in strategies):
        raise ValueError("strategy filter contains an unknown strategy")
    if len(set(strategies)) != len(strategies):
        raise ValueError("strategy filter names must be unique")
    return list(strategies)


def validate_research_controls(controls: dict | None) -> dict:
    if controls is None:
        return {}
    if not isinstance(controls, dict):
        raise ValueError("research controls must be an object")
    unknown = set(controls) - RESEARCH_CONTROL_KEYS
    if unknown:
        raise ValueError(f"unknown research control(s): {', '.join(sorted(unknown))}")
    normalized = dict(controls)
    live_risk = get_mode_config("algo")["risk"]
    limits = {
        "max_entries_per_day": (1, live_risk["max_trades_per_day"]),
        "max_open_positions": (1, live_risk["max_open_positions"]),
        "max_positions_per_strategy": (1, live_risk["max_positions_per_strategy"]),
        "cooldown_minutes": (0, 390),
    }
    for key, (minimum, maximum) in limits.items():
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    per_strategy = normalized.get("max_entries_per_strategy_per_day", {})
    if not isinstance(per_strategy, dict):
        raise ValueError("max_entries_per_strategy_per_day must be an object")
    live_daily_caps = live_risk.get("max_entries_per_strategy_per_day", {})
    for name, value in per_strategy.items():
        maximum = live_daily_caps.get(name, live_risk["max_trades_per_day"])
        if name not in STRATEGY_NAMES or isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"per-strategy daily entry limits must not exceed the live Algo cap of {maximum}")
    normalized["max_entries_per_strategy_per_day"] = per_strategy
    cooldowns = normalized.get("cooldown_minutes_by_strategy", {})
    if not isinstance(cooldowns, dict):
        raise ValueError("cooldown_minutes_by_strategy must be an object")
    for name, value in cooldowns.items():
        maximum = live_risk.get("cooldown_minutes_by_strategy", {}).get(name, 390)
        if name not in STRATEGY_NAMES or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError("strategy cooldowns must use known strategies and stay within the live Algo cap")
    normalized["cooldown_minutes_by_strategy"] = cooldowns
    orb_once = normalized.get("one_orb_attempt_per_ticker_per_day", False)
    if not isinstance(orb_once, bool):
        raise ValueError("one_orb_attempt_per_ticker_per_day must be a boolean")
    normalized["one_orb_attempt_per_ticker_per_day"] = orb_once
    return normalized


@dataclass
class Position:
    ticker: str
    strategy: str
    qty: int
    entry_price: float
    entry_at: str
    stop_price: float
    take_profit: float
    signal_at: str
    bars_held: int = 0
    satellite_confirmations: list[str] = field(default_factory=list)
    entry_commission: float = 0.0
    borrow_cost: float = 0.0


class AlgoPortfolioBacktest:
    """Replay a fixed symbol list through live Algo rules and risk checks."""

    def __init__(
        self,
        *,
        symbols: list[str],
        intraday_bars: dict[str, list[dict]],
        spy_daily_bars: list[dict],
        start: str,
        end: str,
        feed: str,
        data_adjustment: str,
        initial_capital: float = 100_000,
        slippage_bps: float = 5,
        profile: dict | None = None,
        strategies: list[str] | None = None,
        research_controls: dict | None = None,
        event_calendar: EventCalendar | None = None,
        spread_bps: float = 3.0,
        commission_per_share: float = 0.005,
        short_borrow_annual_pct: float = 0.03,
        unknown_borrow_penalty_bps: float = 25.0,
        borrow_availability: dict[tuple[str, str], bool] | None = None,
        coordinator_config: PortfolioCoordinatorConfig | None = None,
        force_close_at_end: bool = False,
    ) -> None:
        if not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("Initial capital must be a positive finite number")
        if not math.isfinite(slippage_bps) or slippage_bps < 0:
            raise ValueError("Slippage basis points must be finite and non-negative")
        if not math.isfinite(spread_bps) or spread_bps < 0:
            raise ValueError("Spread basis points must be finite and non-negative")
        if not math.isfinite(commission_per_share) or commission_per_share < 0:
            raise ValueError("Commission per share must be finite and non-negative")
        if not math.isfinite(short_borrow_annual_pct) or short_borrow_annual_pct < 0:
            raise ValueError("Annual short borrow rate must be finite and non-negative")
        if not math.isfinite(unknown_borrow_penalty_bps) or unknown_borrow_penalty_bps < 0:
            raise ValueError("Unknown borrow penalty must be finite and non-negative")
        if feed != "sip":
            raise ValueError("Plus regular-session replay requires SIP; BOATS requires its own overnight model")
        self.symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        self.intraday_bars = intraday_bars
        self.spy_daily_bars = spy_daily_bars
        self.start = date.fromisoformat(start)
        self.end = date.fromisoformat(end)
        if self.end < self.start:
            raise ValueError("End date must not be before start date")
        self.feed = feed
        self.data_adjustment = data_adjustment
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.slippage = float(slippage_bps) / 10_000
        self.slippage_bps = float(slippage_bps)
        self.spread_bps = float(spread_bps)
        self.commission_per_share = float(commission_per_share)
        self.short_borrow_annual_pct = float(short_borrow_annual_pct)
        self.unknown_borrow_penalty_bps = float(unknown_borrow_penalty_bps)
        self.borrow_availability = {
            (str(ticker).upper(), str(day)): bool(available)
            for (ticker, day), available in (borrow_availability or {}).items()
        }
        self.borrow_coverage: Counter[str] = Counter()
        self.borrow_cost_total = 0.0
        self.commission_total = 0.0
        self.coordinator = PortfolioCoordinator(coordinator_config)
        self.force_close_at_end = bool(force_close_at_end)
        self.risk = get_mode_config("algo")["risk"]
        self.profile = validate_parameter_profile(profile or {})
        self.strategy_filter = validate_strategy_filter(strategies)
        self.satellite_config = SatelliteOverlayConfig.from_dict({
            "multi_factor_enabled": STRATEGY_CONFIG.get("algo_multi_factor_enabled", True),
            "event_driven_enabled": STRATEGY_CONFIG.get("algo_event_driven_enabled", True),
            **self.profile.get("satellite_overlay", {}),
        })
        self.research_controls = validate_research_controls(research_controls)
        self.event_calendar = event_calendar or EventCalendar()
        self.event_config = EventStrategyConfig.from_dict(self.profile.get("event_driven"))
        self.event_satellite_enabled = (
            self.event_config.enabled and self.satellite_config.event_driven_enabled
            and (self.strategy_filter is None or "event_driven" in self.strategy_filter)
        )
        self.event_signal_generator = EventSignalGenerator(self.event_calendar, self.event_config)
        self.event_strategy_state: dict = {}
        self.multi_factor_enabled = (
            self.satellite_config.multi_factor_enabled
            and (self.strategy_filter is None or "multi_factor" in self.strategy_filter)
        )
        self.multi_factor_generator = SignalGenerator(MultiFactorConfig.from_dict(self.profile.get("multi_factor")))
        all_daily_bars = aggregate_intraday_bars(intraday_bars)
        self.previous_closes_by_day: dict[tuple[str, date], float] = {}
        for symbol, rows in all_daily_bars.items():
            for index, row in enumerate(rows):
                if index:
                    self.previous_closes_by_day[(symbol, date.fromisoformat(row["date"]))] = float(rows[index - 1]["c"])
        # Swing trend, momentum, cyclical mean reversion, and Multi Factor all
        # consume the same point-in-time daily aggregates. Each caller filters
        # strictly through the last completed session.
        self.daily_stock_bars = all_daily_bars
        self.daily_stock_series = {
            symbol: (
                [date.fromisoformat(row["date"]) for row in rows],
                [float(row["c"]) for row in rows],
            )
            for symbol, rows in self.daily_stock_bars.items()
        }
        self.last_multi_factor_rebalance_key: str | None = None
        self.multi_factor_snapshots: list[dict] = []
        self.multi_factor_rebalance_diagnostics: list[dict] = []
        self.latest_multi_factor_signals: dict[str, dict] = {}
        self.satellite_confirmation_candidates: Counter[str] = Counter()
        self.satellite_confirmed_entries: Counter[str] = Counter()
        self.positions: dict[str, Position] = {}
        self.pending_entries: dict[str, dict] = {}
        self.pending_exits: dict[str, dict] = {}
        self.last_prices: dict[str, float] = {}
        self.histories: dict[tuple[str, date], list[dict]] = defaultdict(list)
        self.closed_trades: list[dict] = []
        self.rejections: Counter[str] = Counter()
        self.entry_counts: Counter[str] = Counter()
        self.entry_counts_by_strategy: Counter[tuple[str, str]] = Counter()
        self.signal_events_by_strategy: Counter[tuple[str, str]] = Counter()
        self.candidate_setups_by_strategy: Counter[tuple[str, str]] = Counter()
        self.candidate_ticker_days: set[tuple[str, str]] = set()
        self.candidate_strategy_ticker_days: set[tuple[str, str, str]] = set()
        self.last_exit_at: dict[str, datetime] = {}
        self.last_exit_at_by_strategy: dict[tuple[str, str], datetime] = {}
        self.orb_attempts: set[tuple[str, str]] = set()
        self.exposed_event_count = 0
        self.daily_curve: list[dict] = []
        self.daily_strategy_pnl: list[dict] = []
        self.holdings_by_day: dict[str, dict[str, set[tuple[str, str]]]] = defaultdict(lambda: defaultdict(set))
        self._last_strategy_pnl: dict[str, float] = defaultdict(float)
        self.max_drawdown = 0.0
        self.peak_equity = self.initial_capital
        self.day_key: str | None = None
        self.week_key: tuple[int, int] | None = None
        self.day_start_equity = self.initial_capital
        self.week_start_equity = self.initial_capital
        (
            self.ticker_sector,
            self.ticker_max_pct,
            self.tactical_tickers,
            _,
        ) = _get_universe_maps("algo")

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
        events: dict[datetime, dict[str, dict]] = defaultdict(dict)
        for ticker in self.symbols:
            for bar in self.intraday_bars.get(ticker, []):
                timestamp = self._timestamp(bar)
                local = timestamp.astimezone(ET)
                if not self.start <= local.date() <= self.end or not self._is_complete(bar):
                    continue
                local_time = local.time().replace(tzinfo=None)
                if not time(9, 30) <= local_time < time(16, 0):
                    continue
                values = [float(bar[key]) for key in ("o", "h", "l", "c")]
                if not all(math.isfinite(value) and value > 0 for value in values):
                    continue
                events[timestamp][ticker] = bar
        return dict(sorted(events.items()))

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
            result = validate_trade(
                ticker=ticker, action=action, qty=abs(position.qty),
                entry_price=float(bar["c"]), portfolio_state=state, mode="algo",
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
        result = validate_trade(
            ticker=ticker, action=action, qty=abs(position.qty),
            entry_price=float(bar["c"]), portfolio_state=self._risk_state(), mode="algo",
        )
        if not result.approved:
            self.rejections[str(result.rule or "risk_rejected")] += 1
            return
        self.pending_exits[ticker] = {
            "reason": signal["reason"],
            "action": action,
            "signal_at": (timestamp + timedelta(minutes=BAR_MINUTES)).isoformat(),
        }

    def _multi_factor_rebalance(self, session: date) -> tuple[dict[str, dict], dict | None]:
        if not self.multi_factor_enabled:
            return {}, None
        key = self.multi_factor_generator.rebalance_key(session)
        if key == self.last_multi_factor_rebalance_key:
            return {}, None
        held = {
            ticker: {
                "qty": position.qty, "entry_at": position.entry_at,
                "entry_price": position.entry_price,
            }
            for ticker, position in self.positions.items()
            if position.strategy == "multi_factor"
        }
        signals, diagnostics = self.multi_factor_generator.generate(
            self.daily_stock_bars,
            symbols=self.symbols,
            as_of=session - timedelta(days=1),
            session=session,
            regime=str(self._current_regime.get("regime", "unknown")),
            held_positions=held,
        )
        if not diagnostics["minimum_universe_met"]:
            return {}, diagnostics
        prior_intraday = {}
        for symbol, bars in self.intraday_bars.items():
            prior_intraday[symbol] = [
                bar for bar in bars
                if self._timestamp(bar).astimezone(ET).date() < session
            ][-3:]
        signals = self.multi_factor_generator.apply_intraday_size_tilt(signals, prior_intraday)
        self.last_multi_factor_rebalance_key = key
        scores = {item.ticker: item.score for item in signals if item.action not in {"sell", "cover"}}
        factor_scores: dict[str, dict[str, float]] = defaultdict(dict)
        for item in signals:
            if item.action in {"sell", "cover"}:
                continue
            for factor, value in item.factor_zscores.items():
                factor_scores[factor][item.ticker] = value
        targets = {item.ticker for item in signals if item.action in {"buy", "short", "hold"}}
        previous_targets = self.multi_factor_snapshots[-1]["targets"] if self.multi_factor_snapshots else set()
        diagnostics["turnover"] = turnover(previous_targets, targets)
        self.multi_factor_snapshots.append({
            "as_of": session - timedelta(days=1),
            "scores": scores,
            "factor_scores": dict(factor_scores),
            "targets": targets,
        })
        self.multi_factor_rebalance_diagnostics.append(diagnostics)
        for item in signals:
            if item.action not in {"buy", "sell", "cover", "short"}:
                continue
            day_key = session.isoformat()
            self.signal_events_by_strategy[(day_key, "multi_factor")] += 1
        return {
            item.ticker: item.to_dict() for item in signals
            if item.action in {"buy", "sell", "cover", "short"}
        }, diagnostics

    def _multi_factor_ic(self) -> dict:
        ic_values = []
        factor_ic_values: dict[str, list[float]] = defaultdict(list)
        for snapshot in self.multi_factor_snapshots:
            returns = {}
            for symbol in snapshot["scores"]:
                rows = self.daily_stock_series.get(symbol)
                if rows is None:
                    continue
                session_dates, closes = rows
                future_index = bisect_right(session_dates, snapshot["as_of"])
                if future_index > 0 and future_index + 20 < len(closes):
                    start_close = closes[future_index - 1]
                    end_close = closes[future_index + 20]
                    if start_close > 0:
                        returns[symbol] = end_close / start_close - 1
            value = information_coefficient(snapshot["scores"], returns)
            if value is not None:
                ic_values.append(value)
            for factor, scores in snapshot["factor_scores"].items():
                value = information_coefficient(scores, returns)
                if value is not None:
                    factor_ic_values[factor].append(value)
        return {
            "rebalance_count": len(self.multi_factor_snapshots),
            "average_turnover": round(statistics.mean(
                [row["turnover"] for row in self.multi_factor_rebalance_diagnostics]
            ), 4) if self.multi_factor_rebalance_diagnostics else None,
            "mean_spearman_ic_forward_21_sessions": round(statistics.mean(ic_values), 4) if ic_values else None,
            "ic_observation_count": len(ic_values),
            "mean_spearman_ic_forward_21_sessions_by_factor": {
                factor: round(statistics.mean(values), 4)
                for factor, values in sorted(factor_ic_values.items())
            },
            "ic_observation_count_by_factor": {
                factor: len(values) for factor, values in sorted(factor_ic_values.items())
            },
            "ic_horizon_sessions": 21,
            "ic_labels_overlap": True,
            "value_factor_enabled": self.multi_factor_generator.config.factor_weights.get("value_earnings_yield", 0) > 0,
        }

    def _regime(self, trading_day: date) -> dict:
        completed = []
        for bar in self.spy_daily_bars:
            try:
                timestamp = self._timestamp(bar)
                if timestamp.astimezone(ET).date() < trading_day:
                    completed.append(bar)
            except (KeyError, TypeError, ValueError):
                continue
        completed.sort(key=self._timestamp)
        return rank_strategies(
            completed, mode="algo", event_enabled=self.event_satellite_enabled,
            event_disable_in_volatile=self.event_config.disable_in_volatile,
            intraday_trend_enabled=bool(STRATEGY_CONFIG.get("enable_intraday_trend", False)),
            intraday_momentum_enabled=bool(STRATEGY_CONFIG.get("enable_intraday_momentum", False)),
        )

    def _previous_close(self, ticker: str, trading_day: date) -> float | None:
        return self.previous_closes_by_day.get((ticker.upper(), trading_day))

    def _completed_daily_history(self, ticker: str, trading_day: date) -> list[dict]:
        return [
            row for row in self.daily_stock_bars.get(ticker.upper(), [])
            if date.fromisoformat(str(row.get("date", ""))[:10]) < trading_day
        ]

    def _signal_for(
        self, ticker: str, history: list[dict], active: list[str], ranked: dict,
        timestamp: datetime, factor_signal: dict | None = None, position_qty: float = 0,
        momentum_signals: dict[str, object] | None = None,
    ) -> dict | None:
        options = []
        held = self.positions.get(ticker)
        strategies = [held.strategy] if held is not None else active
        for strategy in strategies:
            if self.strategy_filter is not None and strategy not in self.strategy_filter:
                continue
            if strategy not in CORE_STRATEGIES:
                continue
            if strategy == "intraday_trend" and not STRATEGY_CONFIG.get("enable_intraday_trend", False):
                continue
            if strategy == "intraday_momentum" and not STRATEGY_CONFIG.get("enable_intraday_momentum", False):
                continue
            try:
                if strategy == "momentum":
                    signal = (momentum_signals or {}).get(ticker)
                else:
                    daily_strategy = strategy in {
                        "trend_following", "momentum", "swing_mean_reversion",
                    }
                    signal_bars = (
                        self._completed_daily_history(ticker, timestamp.astimezone(ET).date())
                        if daily_strategy else history
                    )
                    signal = evaluate_rule_signal(
                        strategy, ticker, signal_bars, parameters=self.profile.get(strategy),
                        position_qty=position_qty,
                        entry_at=held.entry_at if held is not None else None,
                        entry_price=held.entry_price if held is not None else None,
                        position_target=held.take_profit if held is not None else None,
                    )
            except Exception as exc:
                self.rejections[f"signal_error:{strategy}:{type(exc).__name__}"] += 1
                continue
            if signal is None or signal.action not in ({"sell", "cover"} if position_qty else {"buy", "short"}):
                continue
            if position_qty:
                options.append({**signal.to_dict(), "rank_score": ranked.get(strategy, 0)})
                continue
            self.signal_events_by_strategy[(self.day_key or "", strategy)] += 1
            ticker_day = (self.day_key or "", ticker)
            strategy_ticker_day = (*ticker_day, strategy)
            self.candidate_ticker_days.add(ticker_day)
            if strategy_ticker_day not in self.candidate_strategy_ticker_days:
                self.candidate_strategy_ticker_days.add(strategy_ticker_day)
                self.candidate_setups_by_strategy[(self.day_key or "", strategy)] += 1
            item = signal.to_dict()
            item["rank_score"] = ranked.get(strategy, 0)
            item["timeframe"] = "1Day" if strategy in {
                "trend_following", "momentum", "swing_mean_reversion",
            } else "5Min"
            options.append(item)

        if (
            factor_signal is not None
            and "multi_factor" in (self._current_regime.get("active", []))
            and (self.strategy_filter is None or "multi_factor" in self.strategy_filter)
        ):
            expected_actions = {"sell", "cover"} if position_qty else {"buy", "short"}
            if factor_signal.get("action") in expected_actions:
                if not position_qty:
                    self.signal_events_by_strategy[(self.day_key or "", "multi_factor")] += 1
                    ticker_day = (self.day_key or "", ticker)
                    strategy_ticker_day = (*ticker_day, "multi_factor")
                    self.candidate_ticker_days.add(ticker_day)
                    if strategy_ticker_day not in self.candidate_strategy_ticker_days:
                        self.candidate_strategy_ticker_days.add(strategy_ticker_day)
                        self.candidate_setups_by_strategy[(self.day_key or "", "multi_factor")] += 1
                options.append({
                    **factor_signal, "rank_score": -1.0, "timeframe": "1Day",
                })

        event_evidence = None

        event_signal = None
        if (
            (self.event_satellite_enabled or (held is not None and held.strategy == "event_driven"))
            and (
                "event_driven" in self._current_regime.get("active", [])
                or (held is not None and held.strategy == "event_driven")
            )
            and (self.strategy_filter is None or "event_driven" in self.strategy_filter)
            and (held is None or held.strategy == "event_driven")
        ):
            try:
                signal_as_of = timestamp + timedelta(minutes=BAR_MINUTES)
                event_signal = self.event_signal_generator.generate(
                    ticker,
                    self._completed_daily_history(ticker, timestamp.astimezone(ET).date()),
                    as_of=signal_as_of,
                    universe=set(self.symbols),
                    regime=str(self._current_regime.get("regime", "unknown")),
                    strategy_state=self.event_strategy_state,
                    position_qty=position_qty if held and held.strategy == "event_driven" else 0,
                    entry_at=held.entry_at if held and held.strategy == "event_driven" else None,
                    entry_price=held.entry_price if held and held.strategy == "event_driven" else None,
                )
            except Exception as exc:
                self.rejections[f"signal_error:event_driven:{type(exc).__name__}"] += 1
        if event_signal is not None:
            event_evidence = event_signal.to_dict()
            expected_actions = {"sell", "cover"} if position_qty else {"buy", "short"}
            if event_signal.action in expected_actions:
                self.signal_events_by_strategy[(self.day_key or "", "event_driven")] += 1
                item = dict(event_evidence)
                item["rank_score"] = -1.0
                item["timeframe"] = "1Day"
                if not position_qty:
                    ticker_day = (self.day_key or "", ticker)
                    strategy_ticker_day = (*ticker_day, "event_driven")
                    self.candidate_ticker_days.add(ticker_day)
                    if strategy_ticker_day not in self.candidate_strategy_ticker_days:
                        self.candidate_strategy_ticker_days.add(strategy_ticker_day)
                        self.candidate_setups_by_strategy[(self.day_key or "", "event_driven")] += 1
                options.append(item)

        if not options:
            return None
        chosen = max(options, key=lambda item: (item["rank_score"], item["strength"]))
        chosen = apply_satellite_confirmations(
            chosen,
            factor_signal=(factor_signal if "multi_factor" in active else None),
            event_signal=event_evidence if "event_driven" in active else None,
            config=self.satellite_config,
        )
        for satellite in chosen.get("satellite_confirmations", []):
            self.satellite_confirmation_candidates[satellite] += 1
        return chosen

    def _queue_signal(self, ticker: str, signal: dict, bar: dict, timestamp: datetime) -> None:
        if ticker in self.positions or ticker in self.pending_entries:
            return
        if self.day_key is None:
            return
        if not signal or signal.get("action") not in {"buy", "short"}:
            return
        action = str(signal["action"])
        strategy = str(signal["strategy"])
        controls = self.research_controls
        entries_today = self.entry_counts[self.day_key]
        if entries_today >= controls.get("max_entries_per_day", self.risk["max_trades_per_day"]):
            self.rejections["research_max_entries_per_day"] += 1
            return
        if len(self.positions) + len(self.pending_entries) >= controls.get(
            "max_open_positions", self.risk["max_open_positions"],
        ):
            self.rejections["research_max_open_positions"] += 1
            return
        strategy_positions = sum(position.strategy == strategy for position in self.positions.values())
        strategy_positions += sum(order["strategy"] == strategy for order in self.pending_entries.values())
        strategy_caps = self.risk.get("max_positions_per_strategy_by_strategy", {})
        strategy_position_cap = strategy_caps.get(
            strategy, controls.get("max_positions_per_strategy", self.risk.get("max_positions_per_strategy")),
        )
        configured_global_cap = controls.get("max_positions_per_strategy")
        if configured_global_cap is not None:
            strategy_position_cap = min(int(strategy_position_cap), int(configured_global_cap))
        if strategy_position_cap is not None and strategy_positions >= strategy_position_cap:
            self.rejections["research_max_positions_per_strategy"] += 1
            return
        strategy_daily_cap = controls.get(
            "max_entries_per_strategy_per_day", self.risk.get("max_entries_per_strategy_per_day", {}),
        ).get(strategy)
        if strategy_daily_cap is not None and self.entry_counts_by_strategy[(self.day_key, strategy)] >= strategy_daily_cap:
            self.rejections[f"research_max_entries_per_day:{strategy}"] += 1
            return
        strategy_cooldowns = {
            **self.risk.get("cooldown_minutes_by_strategy", {}),
            **controls.get("cooldown_minutes_by_strategy", {}),
        }
        if strategy in strategy_cooldowns:
            cooldown_minutes = strategy_cooldowns[strategy]
            last_exit = self.last_exit_at_by_strategy.get((ticker, strategy))
        else:
            cooldown_minutes = controls.get("cooldown_minutes", 0)
            last_exit = self.last_exit_at.get(ticker)
        if last_exit and (timestamp - last_exit).total_seconds() < cooldown_minutes * 60:
            self.rejections["research_ticker_cooldown"] += 1
            return
        for group in self.risk.get("strategy_group_limits", {}).values():
            members = set(group.get("strategies", []))
            if strategy not in members:
                continue
            group_open = sum(position.strategy in members for position in self.positions.values())
            group_open += sum(order["strategy"] in members for order in self.pending_entries.values())
            if group_open >= int(group.get("max_open_positions", 10**9)):
                self.rejections["research_strategy_group_open_cap"] += 1
                return
            group_entries = sum(
                count for (day, member), count in self.entry_counts_by_strategy.items()
                if day == self.day_key and member in members
            )
            if group_entries >= int(group.get("max_entries_per_day", 10**9)):
                self.rejections["research_strategy_group_daily_cap"] += 1
                return
        if (
            strategy == "opening_range_breakout"
            and controls.get(
                "one_orb_attempt_per_ticker_per_day",
                self.risk.get("one_orb_attempt_per_ticker_per_day", False),
            )
            and (self.day_key, ticker) in self.orb_attempts
        ):
            self.rejections["research_orb_daily_attempt"] += 1
            return
        regime = self._current_regime
        if action == "buy" and regime.get("regime") == "trending_down":
            self.rejections["downtrend_entry_block"] += 1
            return
        if action == "short":
            regime_name = str(regime.get("regime", "unknown"))
            if regime_name not in {"range_bound", "trending_down"}:
                self.rejections["short_regime_block"] += 1
                return
            if ticker not in self.symbols:
                self.rejections["short_universe_block"] += 1
                return
            if regime_name == "range_bound":
                signal = {**signal, "risk_multiplier": float(self.risk.get("range_regime_short_risk_multiplier", 0.75))}
            borrow_status = self._borrow_status(ticker, timestamp)
            if borrow_status is False:
                self.rejections["hard_to_borrow_short_skip"] += 1
                return
        if strategy == "opening_range_breakout" and controls.get(
            "one_orb_attempt_per_ticker_per_day",
            self.risk.get("one_orb_attempt_per_ticker_per_day", False),
        ):
            self.orb_attempts.add((self.day_key, ticker))
        close = float(bar["c"])
        current_equity = self._equity()
        signal, throttle_note = throttle_signal(
            signal, str(regime.get("regime", "unknown")), self.coordinator.config,
        )
        if throttle_note:
            self.rejections[f"coordinator_regime_throttle:{strategy}"] += 1
        sized = _make_trade(signal, close, current_equity, {}, self.risk)
        if not sized:
            self.rejections["zero_sized"] += 1
            return
        state = self._risk_state()
        intraday_entries = sum(
            count for (day, name), count in self.entry_counts_by_strategy.items()
            if day == self.day_key and name in INTRADAY_STRATEGIES
        )
        coordinator_book = self.coordinator.new_book(
            equity=float(state["equity"]),
            positions=state["positions"],
            position_owners={name: position.strategy for name, position in self.positions.items()},
            pending_buy_values=state["pending_buy_value_by_ticker"],
            pending_short_values=state["pending_short_value_by_ticker"],
            entries_today=int(state["entry_count_today"]),
            intraday_entries_today=intraday_entries,
        )
        decision = coordinator_book.review(
            sized, session="regular", timestamp=timestamp,
            regime=str(regime.get("regime", "unknown")),
        )
        if not decision.approved or decision.proposal is None:
            self.rejections[f"portfolio_coordinator:{decision.reason}"] += 1
            return
        sized = decision.proposal
        result = validate_trade(
            ticker=ticker, action=action, qty=int(sized["qty"]),
            entry_price=close, portfolio_state=state, mode="algo",
        )
        if not result.approved:
            self.rejections[str(result.rule or "risk_rejected")] += 1
            return
        signal_at = (timestamp + timedelta(minutes=BAR_MINUTES)).isoformat()
        self.pending_entries[ticker] = {
            "ticker": ticker, "strategy": signal["strategy"],
            "qty": int(sized["qty"]), "reference_price": close,
            "action": action,
            "stop_price": float(sized["stop_price"]),
            "take_profit": float(sized["take_profit"]),
            "signal_at": signal_at,
            "satellite_confirmations": list(signal.get("satellite_confirmations", [])),
        }
        for satellite in signal.get("satellite_confirmations", []):
            self.satellite_confirmed_entries[satellite] += 1
        if signal.get("event_confirmation"):
            self.event_signal_generator.record_proposal(
                signal["event_confirmation"], as_of=timestamp + timedelta(minutes=BAR_MINUTES),
                strategy_state=self.event_strategy_state,
            )
        elif strategy == "event_driven":
            self.event_signal_generator.record_proposal(
                signal, as_of=timestamp + timedelta(minutes=BAR_MINUTES),
                strategy_state=self.event_strategy_state,
            )
        self.entry_counts[self.day_key] += 1
        self.entry_counts_by_strategy[(self.day_key, strategy)] += 1

    def _buy_hold_report(self, events: dict[datetime, dict[str, dict]]) -> dict:
        """Equal-weight buy-and-hold benchmark using the same symbols and fill costs."""
        first: dict[str, dict] = {}
        last: dict[str, dict] = {}
        for bars in events.values():
            for ticker, bar in bars.items():
                first.setdefault(ticker, bar)
                last[ticker] = bar
        eligible = sorted(set(first) & set(last))
        if not eligible:
            return {"symbol_count": 0, "net_return_pct": 0.0, "net_pnl": 0.0}
        allocation = self.initial_capital / len(eligible)
        net_pnl = 0.0
        invested = 0.0
        for ticker in eligible:
            buy_price = self._fill_price(float(first[ticker]["o"]), "buy")
            qty = int(allocation / buy_price)
            if qty <= 0:
                continue
            sell_price = self._fill_price(float(last[ticker]["c"]), "sell")
            buy_commission = qty * self.commission_per_share
            sell_commission = qty * self.commission_per_share
            net_pnl += qty * (sell_price - buy_price) - buy_commission - sell_commission
            invested += qty * buy_price + buy_commission
        return {
            "method": "equal_weight_buy_and_hold_same_symbols",
            "symbol_count": len(eligible),
            "symbols_with_prices": eligible,
            "symbols_without_prices": sorted(set(self.symbols) - set(eligible)),
            "invested_capital": round(invested, 2),
            "net_pnl": round(net_pnl, 2),
            "net_return_pct": round(net_pnl / self.initial_capital * 100, 4),
            "costs_included": True,
        }

    @staticmethod
    def _pearson(left: list[float], right: list[float]) -> float | None:
        if len(left) < 2 or len(left) != len(right):
            return None
        left_mean, right_mean = statistics.mean(left), statistics.mean(right)
        covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
        left_var = sum((a - left_mean) ** 2 for a in left)
        right_var = sum((b - right_mean) ** 2 for b in right)
        if left_var <= 0 or right_var <= 0:
            return None
        return covariance / math.sqrt(left_var * right_var)

    def _correlation_report(self) -> dict:
        dates = [str(row["date"]) for row in self.daily_curve]
        daily = {str(row["date"]): row["pnl_by_strategy"] for row in self.daily_strategy_pnl}
        active: dict[str, set[str]] = defaultdict(set)
        for day, by_strategy in self.holdings_by_day.items():
            for strategy, holdings in by_strategy.items():
                if holdings:
                    active[strategy].add(day)
        pairs = []
        strategies = sorted(STRATEGY_NAMES)
        for index, left_name in enumerate(strategies):
            for right_name in strategies[index + 1:]:
                left = [float((daily.get(day) or {}).get(left_name, 0.0)) for day in dates]
                right = [float((daily.get(day) or {}).get(right_name, 0.0)) for day in dates]
                correlation = self._pearson(left, right)
                overlap_days = 0
                for day in dates:
                    left_holds = self.holdings_by_day.get(day, {}).get(left_name, set())
                    right_holds = self.holdings_by_day.get(day, {}).get(right_name, set())
                    if left_holds & right_holds:
                        overlap_days += 1
                denominator = min(len(active[left_name]), len(active[right_name]))
                overlap_fraction = overlap_days / denominator if denominator else 0.0
                active_union = len(active[left_name] | active[right_name])
                pairs.append({
                    "strategy_a": left_name,
                    "strategy_b": right_name,
                    "daily_pnl_correlation": round(correlation, 4) if correlation is not None else None,
                    "same_ticker_same_direction_overlap_days": overlap_days,
                    "active_days_a": len(active[left_name]),
                    "active_days_b": len(active[right_name]),
                    "overlap_fraction_of_smaller_active_days": round(overlap_fraction, 4),
                    "overlap_fraction_of_union_active_days": round(overlap_days / active_union, 4) if active_union else 0.0,
                    "flag": bool(correlation is not None and correlation > 0.6 and overlap_fraction > 0.30),
                })
        return {
            "definition": "Pearson correlation on daily strategy P&L changes; overlap is the share of the smaller strategy active-day count where both held the same ticker in the same direction.",
            "thresholds": {"correlation_above": 0.6, "overlap_fraction_above": 0.30},
            "pairs": pairs,
            "flags": [pair for pair in pairs if pair["flag"]],
        }

    def run(self) -> dict:
        logger = logging.getLogger("risk_manager")
        previous_level = logger.level
        logger.setLevel(logging.ERROR)
        try:
            return self._run()
        finally:
            logger.setLevel(previous_level)

    def _run(self) -> dict:
        events = self._timeline()
        if not events:
            raise ValueError("No completed regular-session 5-minute bars in the requested date range")
        daily_dates = {
            self._timestamp(bar).astimezone(ET).date()
            for bar in self.spy_daily_bars
        }
        first_day = min(timestamp.astimezone(ET).date() for timestamp in events)
        if sum(day < first_day for day in daily_dates) < MIN_REGIME_DAILY_BARS:
            raise ValueError(
                f"At least {MIN_REGIME_DAILY_BARS} completed SPY daily bars before the replay are required"
            )

        last_day: date | None = None
        factor_signals_for_day: dict[str, dict] = {}
        factor_signals_date: date | None = None
        momentum_signals_for_day: dict[str, object] = {}
        event_times = list(events)
        for index, timestamp in enumerate(event_times):
            bars = events[timestamp]
            trading_day = timestamp.astimezone(ET).date()
            if last_day != trading_day:
                self._current_regime = self._regime(trading_day)
                factor_signals_for_day, factor_diagnostics = self._multi_factor_rebalance(trading_day)
                factor_signals_date = trading_day if factor_diagnostics and factor_diagnostics.get("minimum_universe_met") else None
                held_momentum = {
                    ticker: {
                        "qty": position.qty, "entry_at": position.entry_at,
                        "entry_price": position.entry_price,
                    }
                    for ticker, position in self.positions.items()
                    if position.strategy == "momentum"
                }
                try:
                    momentum_signals_for_day, _ = generate_swing_momentum_signals(
                        self.daily_stock_bars, symbols=self.symbols,
                        as_of=trading_day - timedelta(days=1),
                        regime=str(self._current_regime.get("regime", "unknown")),
                        held_positions=held_momentum,
                        parameters=self.profile.get("momentum"),
                    )
                except Exception as exc:
                    self.rejections[f"signal_error:momentum:{type(exc).__name__}"] += 1
                    momentum_signals_for_day = {}
                if factor_diagnostics is not None:
                    self.latest_multi_factor_signals = (
                        {
                            ticker: signal for ticker, signal in factor_signals_for_day.items()
                            if signal.get("action") in {"buy", "hold", "short"}
                        }
                        if factor_diagnostics.get("minimum_universe_met") else {}
                    )
                last_day = trading_day
            self._start_periods(trading_day)
            self._record_holdings(trading_day)
            self._fill_pending_entries(bars, timestamp)
            self._fill_pending_exits(bars, timestamp)
            self._record_holdings(trading_day)
            for ticker, bar in bars.items():
                self._check_brackets(ticker, bar, timestamp)
            for ticker, bar in bars.items():
                self.last_prices[ticker] = float(bar["c"])
                history = self.histories[(ticker, trading_day)]
                history.append(bar)
            self._record_holdings(trading_day)
            regime = self._current_regime
            ranked = {
                item["strategy"]: float(item.get("selection_score", item["score"]))
                for item in regime.get("ranked", [])
            }
            candidates = []
            if factor_signals_date == trading_day:
                for ticker, signal in list(factor_signals_for_day.items()):
                    if signal["action"] in {"sell", "cover"} and ticker in bars:
                        self._queue_exit_signal(ticker, signal, bars[ticker], timestamp)
                        factor_signals_for_day.pop(ticker, None)
            for ticker, bar in bars.items():
                if ticker in self.pending_entries or ticker in self.pending_exits:
                    continue
                factor_signal = factor_signals_for_day.get(ticker) if factor_signals_date == trading_day else None
                if factor_signal and factor_signal.get("action") in {"sell", "cover"}:
                    # Satellite exits share the same simulator risk manager
                    # and next-bar fill model as core strategy exits.
                    self._queue_exit_signal(ticker, factor_signal, bar, timestamp)
                    factor_signals_for_day.pop(ticker, None)
                history = self.histories[(ticker, trading_day)]
                factor_support = (
                    self.latest_multi_factor_signals.get(ticker)
                    if "multi_factor" in regime.get("active", []) else None
                )
                position = self.positions.get(ticker)
                signal = self._signal_for(
                    ticker, history, regime.get("active", []), ranked, timestamp,
                    factor_signal=factor_support,
                    position_qty=position.qty if position is not None else 0,
                    momentum_signals=momentum_signals_for_day,
                )
                if signal:
                    if position is not None:
                        self._queue_exit_signal(ticker, signal, bar, timestamp)
                    else:
                        candidates.append((ticker, bar, signal))

            # Resolve same-bar capacity consistently for baseline and research runs.
            candidates.sort(
                key=lambda item: (
                    strategy_priority(str(item[2].get("strategy", ""))),
                    -float(item[2].get("rank_score", 0)),
                    -float(item[2].get("strength", 0)),
                    item[0],
                )
            )
            for ticker, bar, signal in candidates:
                self._queue_signal(ticker, signal, bar, timestamp)

            is_session_end = (
                index + 1 == len(event_times)
                or event_times[index + 1].astimezone(ET).date() != trading_day
            )
            if is_session_end:
                self._record_holdings(trading_day)
                self._charge_daily_borrow()
                if not self.coordinator.config.allow_intraday_overnight:
                    self._flatten_positions(
                        bars, timestamp, strategies=set(INTRADAY_STRATEGIES),
                        reason="coordinator_intraday_eod_flatten",
                    )
                if index + 1 == len(event_times) and self.force_close_at_end:
                    self._flatten_positions(bars, timestamp, reason="walk_forward_window_end")
                self._record_daily_strategy_pnl(trading_day)

            if self.positions:
                self.exposed_event_count += 1
            equity = self._equity()
            self.peak_equity = max(self.peak_equity, equity)
            self.max_drawdown = max(self.max_drawdown, (self.peak_equity - equity) / self.peak_equity)
            # Capture the last replay mark for each New York trading date.
            if is_session_end:
                self.daily_curve.append({"date": trading_day.isoformat(), "equity": round(equity, 2)})

        final_equity = self._equity()
        closed_pnls = [trade["pnl"] for trade in self.closed_trades]
        winners = [value for value in closed_pnls if value > 0]
        losers = [value for value in closed_pnls if value < 0]
        daily_returns = [
            self.daily_curve[index]["equity"] / self.daily_curve[index - 1]["equity"] - 1
            for index in range(1, len(self.daily_curve))
            if self.daily_curve[index - 1]["equity"] > 0
        ]
        daily_std = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
        session_count = len(self.daily_curve)
        entries_per_session = [
            self.entry_counts.get(row["date"], 0) for row in self.daily_curve
        ]
        closed_holds = [float(trade["holding_minutes"]) for trade in self.closed_trades]
        max_consecutive_losses = 0
        current_consecutive_losses = 0
        for trade in sorted(self.closed_trades, key=lambda row: row["exit_at"]):
            current_consecutive_losses = current_consecutive_losses + 1 if trade["pnl"] < 0 else 0
            max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)

        benchmark = self._buy_hold_report(events)
        benchmark_return = float(benchmark["net_return_pct"])
        strategy_totals = self._strategy_total_pnl()
        strategy_metrics = {}
        for strategy in sorted(STRATEGY_NAMES):
            trades = [trade for trade in self.closed_trades if trade["strategy"] == strategy]
            strategy_wins = [trade["pnl"] for trade in trades if trade["pnl"] > 0]
            strategy_losses = [trade["pnl"] for trade in trades if trade["pnl"] < 0]
            durations = [float(trade["holding_minutes"]) for trade in trades]
            strategy_metrics[strategy] = {
                "closed_trade_count": len(trades),
                "entry_count": sum(
                    count for (day, name), count in self.entry_counts_by_strategy.items()
                    if name == strategy
                ),
                "signal_event_count": sum(
                    count for (day, name), count in self.signal_events_by_strategy.items()
                    if name == strategy
                ),
                "candidate_setup_count": sum(
                    count for (day, name), count in self.candidate_setups_by_strategy.items()
                    if name == strategy
                ),
                "win_rate_pct": round(100 * len(strategy_wins) / len(trades), 2) if trades else None,
                "profit_factor": round(sum(strategy_wins) / abs(sum(strategy_losses)), 4)
                if strategy_losses else (None if not strategy_wins else "inf"),
                "average_trade_pnl": round(statistics.mean([trade["pnl"] for trade in trades]), 2) if trades else None,
                "average_holding_minutes": round(statistics.mean(durations), 2) if durations else None,
                "net_pnl": round(strategy_totals.get(strategy, 0.0), 2),
                "net_return_on_initial_book_pct": round(strategy_totals.get(strategy, 0.0) / self.initial_capital * 100, 4),
                "excess_return_vs_buy_hold_pct_points": round(
                    strategy_totals.get(strategy, 0.0) / self.initial_capital * 100 - benchmark_return, 4,
                ),
            }
        correlation_report = self._correlation_report()
        profile_hash = hashlib.sha256(
            json.dumps(self.profile, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        experiment_hash = hashlib.sha256(json.dumps({
            "parameters": self.profile,
            "strategy_filter": self.strategy_filter,
            "research_controls": self.research_controls,
            "transaction_costs": {
                "spread_bps": self.spread_bps,
                "slippage_bps_each_side": self.slippage_bps,
                "commission_per_share": self.commission_per_share,
                "short_borrow_annual_pct": self.short_borrow_annual_pct,
                "unknown_borrow_penalty_bps": self.unknown_borrow_penalty_bps,
            },
            "portfolio_coordinator": {
                "max_name_pct": self.coordinator.config.max_name_pct,
                "sector_caps": dict(self.coordinator.config.sector_caps),
                "max_gross_pct": self.coordinator.config.max_gross_pct,
                "max_net_long_pct": self.coordinator.config.max_net_long_pct,
                "max_net_short_pct": self.coordinator.config.max_net_short_pct,
                "max_daily_entries": self.coordinator.config.max_daily_entries,
                "max_intraday_entries": self.coordinator.config.max_intraday_entries,
            },
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        open_positions = []
        for ticker, position in sorted(self.positions.items()):
            mark = self.last_prices.get(ticker, position.entry_price)
            estimated_exit_cost = abs(position.qty * mark) * (self.spread_bps / 2 + self.slippage_bps) / 10_000
            open_positions.append({
                "ticker": ticker, "strategy": position.strategy, "qty": position.qty,
                "entry_at": position.entry_at, "entry_price": round(position.entry_price, 6),
                "mark": round(mark, 6), "unrealized_pnl": round(
                    position.qty * (mark - position.entry_price) - position.entry_commission
                    - position.borrow_cost - estimated_exit_cost
                    - abs(position.qty) * self.commission_per_share, 2,
                ),
                "stop_price": position.stop_price, "take_profit": position.take_profit,
            })
        merged_profile = deepcopy(ALGO_DEFAULT_PARAMETERS)
        for strategy, values in self.profile.items():
            if strategy == "satellite_overlay":
                merged_profile[strategy] = values
            else:
                merged_profile[strategy].update(values)
        merged_profile.setdefault("satellite_overlay", self.satellite_config.to_dict())
        event_diagnostics = self.event_signal_generator.diagnostics(
            event_times[-1] + timedelta(minutes=BAR_MINUTES),
        )
        # The research CSV intentionally contains future rows relative to an
        # earlier replay window, so freshness of the whole snapshot is not a
        # meaningful historical diagnostic.
        event_diagnostics.pop("freshness", None)
        event_diagnostics["calendar_mode"] = "historical_point_in_time"
        return {
            "simulator": "swarm_algo_portfolio_research_v3",
            "feed": self.feed,
            "data_adjustment": self.data_adjustment,
            "session": "regular",
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": self.symbols,
            "completed_bar_count": sum(len(bars) for bars in events.values()),
            "first_bar_at": event_times[0].isoformat(),
            "last_bar_at": event_times[-1].isoformat(),
            "initial_capital": self.initial_capital,
            "final_equity": round(final_equity, 2),
            "net_return_pct": round((final_equity / self.initial_capital - 1) * 100, 4),
            "buy_and_hold": benchmark,
            "benchmark_net_return_pct": benchmark_return,
            "excess_return_vs_buy_hold_pct_points": round(
                (final_equity / self.initial_capital - 1) * 100 - benchmark_return, 4,
            ),
            "max_drawdown_pct": round(self.max_drawdown * 100, 4),
            "closed_trade_count": len(self.closed_trades),
            "open_position_count": len(open_positions),
            "entry_count": sum(self.entry_counts.values()),
            "average_entries_per_session": round(sum(entries_per_session) / session_count, 3) if session_count else 0,
            "median_entries_per_session": statistics.median(entries_per_session) if entries_per_session else 0,
            "max_entries_per_session": max(entries_per_session, default=0),
            "entry_count_by_strategy": {
                strategy: sum(count for (day, name), count in self.entry_counts_by_strategy.items() if name == strategy)
                for strategy in sorted(STRATEGY_NAMES)
            },
            "signal_event_count_by_strategy": {
                strategy: sum(count for (day, name), count in self.signal_events_by_strategy.items() if name == strategy)
                for strategy in sorted(STRATEGY_NAMES)
            },
            "candidate_setup_count": len(self.candidate_strategy_ticker_days),
            "candidate_ticker_day_count": len(self.candidate_ticker_days),
            "candidate_setup_count_by_strategy": {
                strategy: sum(
                    count for (day, name), count in self.candidate_setups_by_strategy.items()
                    if name == strategy
                )
                for strategy in sorted(STRATEGY_NAMES)
            },
            "average_candidate_ticker_days_per_session": round(
                len(self.candidate_ticker_days) / session_count, 3
            ) if session_count else 0,
            "strategy_metrics": strategy_metrics,
            "strategy_correlation_report": correlation_report,
            "portfolio_coordinator": {
                "max_name_pct": self.coordinator.config.max_name_pct,
                "sector_caps": dict(self.coordinator.config.sector_caps),
                "max_gross_pct": self.coordinator.config.max_gross_pct,
                "max_net_long_pct": self.coordinator.config.max_net_long_pct,
                "max_net_short_pct": self.coordinator.config.max_net_short_pct,
                "max_daily_entries": self.coordinator.config.max_daily_entries,
                "max_intraday_entries": self.coordinator.config.max_intraday_entries,
                "swing_open_delay_minutes": self.coordinator.config.swing_open_delay_minutes,
                "intraday_close_cutoff_minutes": self.coordinator.config.intraday_close_cutoff_minutes,
                "allow_opposite_netting": self.coordinator.config.allow_opposite_netting,
                "allow_intraday_overnight": self.coordinator.config.allow_intraday_overnight,
            },
            "event_strategy_diagnostics": {
                **event_diagnostics,
                "enabled": self.event_satellite_enabled,
                "configured_enabled": self.event_config.enabled,
                "satellite_enabled": self.satellite_config.event_driven_enabled,
            },
            "satellite_confirmation_candidates": dict(self.satellite_confirmation_candidates),
            "satellite_confirmed_entries": dict(self.satellite_confirmed_entries),
            "average_holding_minutes": round(statistics.mean(closed_holds), 2) if closed_holds else None,
            "median_holding_minutes": round(statistics.median(closed_holds), 2) if closed_holds else None,
            "average_calendar_holding_hours": round(statistics.mean(
                (
                    datetime.fromisoformat(str(trade["exit_at"]).replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(trade["entry_at"]).replace("Z", "+00:00"))
                ).total_seconds() / 3600
                for trade in self.closed_trades
            ), 2) if self.closed_trades else None,
            "max_consecutive_losses": max_consecutive_losses,
            "time_in_market_pct": round(100 * self.exposed_event_count / len(event_times), 2) if event_times else 0,
            "win_rate_pct": round(100 * len(winners) / len(closed_pnls), 2) if closed_pnls else None,
            "profit_factor": round(sum(winners) / abs(sum(losers)), 4) if losers else (None if not winners else "inf"),
            "average_trade_pnl": round(statistics.mean(closed_pnls), 2) if closed_pnls else None,
            "daily_sharpe_annualized": round(math.sqrt(252) * statistics.mean(daily_returns) / daily_std, 4)
            if daily_std > 0 and daily_returns else None,
            "transaction_costs": {
                "spread_bps_round_trip_reference": self.spread_bps,
                "spread_cost_applied_each_side_bps": self.spread_bps / 2,
                "slippage_bps_each_side": self.slippage_bps,
                "commission_per_share_each_side": self.commission_per_share,
                "short_borrow_annual_pct": self.short_borrow_annual_pct,
                "unknown_borrow_adverse_fill_penalty_bps": self.unknown_borrow_penalty_bps,
                "commission_paid": round(self.commission_total, 2),
                "short_borrow_paid": round(self.borrow_cost_total, 2),
                "borrow_availability_observations": dict(self.borrow_coverage),
            },
            "slippage_bps_each_side": self.slippage_bps,
            "fees": "Spread, slippage, per-share commissions, modeled annual short borrow, and an adverse fill penalty when historical borrow status is unknown are included.",
            "parameter_profile": merged_profile,
            "parameter_profile_sha256": profile_hash,
            "experiment_sha256": experiment_hash,
            "strategy_filter": self.strategy_filter,
            "research_controls": self.research_controls,
            "risk_config": {
                "risk_per_trade_pct": STRATEGY_CONFIG["risk_per_trade_pct"],
                "max_strategy_position_pct": STRATEGY_CONFIG["max_strategy_position_pct"],
                "stop_loss_pct": self.risk["stop_loss_pct"],
                "take_profit_r_multiple": STRATEGY_CONFIG["take_profit_r_multiple"],
                "max_open_positions": self.risk["max_open_positions"],
                "max_trades_per_day": self.risk["max_trades_per_day"],
                "max_short_positions": self.risk.get("max_short_positions"),
                "max_short_exposure_pct": self.risk.get("max_short_exposure_pct"),
                "allow_short_positions": self.risk.get("allow_short_positions", False),
                "max_sector_pct": self.risk["max_sector_pct"],
                "min_cash_pct": self.risk["min_cash_pct"],
                "daily_loss_limit": self.risk["daily_loss_limit"],
                "no_buy_if_down_pct": self.risk["no_buy_if_down_pct"],
                "weekly_loss_limit": self.risk["weekly_loss_limit"],
                "max_tactical_pct": self.risk["max_tactical_pct"],
            },
            "risk_catalog": {
                ticker: {
                    "sector": self.ticker_sector.get(ticker),
                    "max_position_pct": self.ticker_max_pct.get(ticker, self.risk["max_position_pct"]),
                    "tactical": ticker in self.tactical_tickers,
                }
                for ticker in self.symbols
            },
            "risk_rejections": dict(sorted(self.rejections.items())),
            "multi_factor_diagnostics": self._multi_factor_ic(),
            "daily_equity": self.daily_curve,
            "daily_strategy_pnl": self.daily_strategy_pnl,
            "closed_trades": self.closed_trades,
            "open_positions": open_positions,
            "unfilled_entries_at_end": sorted(self.pending_entries),
            "unfilled_exits_at_end": sorted(self.pending_exits),
            "assumptions": [
                "Fixed user-supplied universe; no point-in-time membership or delisted-symbol reconstruction.",
                "Completed 5-minute bars only; signals fill at the next available bar open.",
                "Market entries and stop-market exits pay configured adverse slippage; limit targets fill at target or a favorable opening gap.",
                "If stop and target both trade within one bar, the stop is assumed to execute first.",
                "Regular-session only. Overnight and pre/post-market replay requires BOATS bars and a separate session fill model.",
                "LLM regime advice is omitted; replay uses the deterministic historical SPY regime only.",
                "Risk limits are fixed. The profile changes signal-rule parameters only.",
                "Open positions remain marked to market at the end; Algo positions are not forcibly flattened.",
                "Rule-based exits observe the configured minimum holding time; hard stop and target brackets can exit sooner.",
                "Historical borrow availability is optional; explicit non-easy-to-borrow shorts are skipped and unknown availability receives an adverse fill penalty.",
                "Short borrow is charged at the configured annual rate on positions still open at each session close.",
                "The walk-forward runner liquidates all remaining positions at each test-window boundary with modeled exit costs.",
                "Signal event counts are raw five-minute buy-signal observations and may include repeated observations of one setup.",
            ],
        }


def _split_symbols(value: str) -> list[str]:
    symbols = list(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))
    if not symbols:
        raise argparse.ArgumentTypeError("Provide at least one comma-separated symbol")
    return symbols


def _regular_session_bars(bars_by_symbol: dict[str, list[dict]]) -> tuple[dict[str, list[dict]], int, int]:
    """Keep only valid regular-session bars; the replay has no extended-hours model."""
    filtered: dict[str, list[dict]] = {}
    outside_session = invalid_ohlc = 0
    for symbol, bars in bars_by_symbol.items():
        kept = []
        for bar in bars:
            try:
                timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                local_time = timestamp.astimezone(ET).time().replace(tzinfo=None)
                if not time(9, 30) <= local_time < time(16, 0):
                    outside_session += 1
                    continue
                opening, high, low, close = (float(bar[key]) for key in ("o", "h", "l", "c"))
                if (
                    not all(math.isfinite(value) and value > 0 for value in (opening, high, low, close))
                    or high < max(opening, close, low)
                    or low > min(opening, close, high)
                ):
                    invalid_ohlc += 1
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid_ohlc += 1
                continue
            kept.append(bar)
        if kept:
            filtered[symbol] = kept
    return filtered, outside_session, invalid_ohlc


def _read_profile(path: str | None) -> dict:
    if not path:
        return {}
    profile = json.loads(Path(path).read_text())
    return validate_parameter_profile(profile)


def read_borrow_availability(path: str | None) -> dict[tuple[str, str], bool]:
    """Read optional point-in-time borrow data with date,symbol,easy_to_borrow columns."""
    if not path:
        return {}
    output: dict[tuple[str, str], bool] = {}
    with Path(path).open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "symbol", "easy_to_borrow"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("Borrow CSV requires date,symbol,easy_to_borrow columns")
        for row in reader:
            day = date.fromisoformat(str(row["date"]).strip()).isoformat()
            ticker = str(row["symbol"]).strip().upper()
            value = str(row["easy_to_borrow"]).strip().lower()
            if not ticker or value not in {"true", "false", "1", "0", "yes", "no", "easy", "hard"}:
                raise ValueError("Borrow CSV has an invalid symbol or easy_to_borrow value")
            output[(ticker, day)] = value in {"true", "1", "yes", "easy"}
    return output


def _read_strategy_filter(value: str | None) -> list[str] | None:
    if not value:
        return None
    return validate_strategy_filter([item.strip() for item in value.split(",") if item.strip()])


def _read_research_controls(args) -> dict:
    controls = {}
    for cli_name, key in (
        ("research_max_entries_per_day", "max_entries_per_day"),
        ("research_max_open_positions", "max_open_positions"),
        ("research_max_positions_per_strategy", "max_positions_per_strategy"),
        ("research_cooldown_minutes", "cooldown_minutes"),
        ("research_max_momentum_entries_per_day", None),
    ):
        value = getattr(args, cli_name, None)
        if value is None:
            continue
        if key is not None:
            controls[key] = value
        else:
            controls["max_entries_per_strategy_per_day"] = {"momentum": value}
    if getattr(args, "research_one_orb_attempt_per_day", False):
        controls["one_orb_attempt_per_ticker_per_day"] = True
    return validate_research_controls(controls)


def fetch_dataset(args) -> dict:
    symbols = _split_symbols(args.symbols)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        raise ValueError("End date must not be before start date")
    feed = args.feed or regular_feed()
    tier = data_tier()
    store = SQLiteBarStore(args.db)
    selected_strategies = _read_strategy_filter(getattr(args, "strategies", None))
    needs_factor_warmup = selected_strategies is None or "multi_factor" in selected_strategies
    needs_split_adjustment = needs_factor_warmup or selected_strategies is None or "event_driven" in selected_strategies
    adjustment = args.adjustment or ("split" if needs_split_adjustment else "raw")
    warmup_days = multi_factor_warmup_days(_read_profile(getattr(args, "profile", None))) if needs_factor_warmup else 0
    data_start = start - timedelta(days=warmup_days) if needs_factor_warmup else start
    request_start = f"{data_start.isoformat()}T00:00:00Z"
    request_end = f"{end.isoformat()}T23:59:59Z"
    intraday = fetch_alpaca_bars(
        symbols, timeframe="5Min", start=request_start, end=request_end,
        feed=feed, adjustment=adjustment,
    )
    intraday, outside_session_bars, invalid_ohlc_bars = _regular_session_bars(intraday)
    intraday_rows = store.write_bars(
        intraday, feed=feed, timeframe="5Min", adjustment=adjustment,
        data_tier=tier, requested_start=request_start, requested_end=request_end,
    )
    daily_start = start - timedelta(days=DAILY_WARMUP_DAYS)
    daily_from = f"{daily_start.isoformat()}T00:00:00Z"
    daily = fetch_alpaca_bars(
        ["SPY"], timeframe="1Day", start=daily_from, end=request_end,
        feed=feed, adjustment=adjustment,
    )
    daily_rows = store.write_bars(
        daily, feed=feed, timeframe="1Day", adjustment=adjustment,
        data_tier=tier, requested_start=daily_from, requested_end=request_end,
    )
    return {
        "database": str(Path(args.db).resolve()), "data_tier": tier, "feed": feed,
        "adjustment": adjustment, "intraday_rows": intraday_rows,
        "intraday_warmup_days": warmup_days,
        "discarded_outside_session_bars": outside_session_bars,
        "discarded_invalid_ohlc_bars": invalid_ohlc_bars,
        "spy_daily_rows": daily_rows, "symbols": symbols,
    }


def run_backtest(args) -> dict:
    symbols = _split_symbols(args.symbols)
    feed = args.feed or regular_feed()
    store = SQLiteBarStore(args.db)
    selected_strategies = _read_strategy_filter(getattr(args, "strategies", None))
    needs_factor_warmup = selected_strategies is None or "multi_factor" in selected_strategies
    needs_split_adjustment = needs_factor_warmup or selected_strategies is None or "event_driven" in selected_strategies
    adjustment = args.adjustment or ("split" if needs_split_adjustment else "raw")
    profile = _read_profile(args.profile)
    warmup_days = multi_factor_warmup_days(profile) if needs_factor_warmup else 0
    data_start = (date.fromisoformat(args.start) - timedelta(days=warmup_days)).isoformat() if needs_factor_warmup else args.start
    intraday = store.bars(
        symbols, feed=feed, timeframe="5Min", start=data_start,
        end=args.end, adjustment=adjustment,
    )
    daily_start = (date.fromisoformat(args.start) - timedelta(days=DAILY_WARMUP_DAYS)).isoformat()
    daily = store.bars(
        ["SPY"], feed=feed, timeframe="1Day", start=daily_start,
        end=args.end, adjustment=adjustment,
    ).get("SPY", [])
    missing = [ticker for ticker in symbols if not intraday.get(ticker)]
    if missing:
        raise ValueError(f"No stored 5-minute {feed} bars for: {', '.join(missing)}")
    replay = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=daily,
        start=args.start, end=args.end, feed=feed,
        data_adjustment=adjustment, initial_capital=args.capital,
        slippage_bps=args.slippage_bps, profile=profile,
        spread_bps=getattr(args, "spread_bps", 3.0),
        commission_per_share=getattr(args, "commission_per_share", 0.005),
        short_borrow_annual_pct=getattr(args, "short_borrow_annual_pct", 3.0) / 100,
        unknown_borrow_penalty_bps=getattr(args, "unknown_borrow_penalty_bps", 25.0),
        borrow_availability=read_borrow_availability(getattr(args, "borrow_availability", None)),
        strategies=selected_strategies,
        research_controls=_read_research_controls(args),
        event_calendar=(EventCalendar.from_csv(args.event_calendar) if getattr(args, "event_calendar", None) else None),
    )
    report = replay.run()
    report["data_tier_at_run"] = data_tier()
    report["database"] = str(Path(args.db).resolve())
    report["multi_factor_warmup_days"] = warmup_days
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
        report["report_file"] = str(output.resolve())
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the live Algo strategy portfolio on stored Alpaca bars")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="download historical bars into the local SQLite data store")
    fetch.add_argument("--symbols", required=True, help="fixed comma-separated Algo catalog symbols")
    fetch.add_argument("--start", required=True, help="first regular-session date, YYYY-MM-DD")
    fetch.add_argument("--end", required=True, help="last regular-session date, YYYY-MM-DD")
    fetch.add_argument(
        "--feed", choices=["sip"], default=None,
        help="Plus SIP regular-session feed",
    )
    fetch.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=None)
    fetch.add_argument("--strategies", help="optional comma-separated strategies; controls factor warmup")
    fetch.add_argument("--profile", help="optional parameter profile; determines factor-history warmup")
    fetch.add_argument("--db", default=str(DEFAULT_DB))
    fetch.set_defaults(handler=fetch_dataset)

    run = commands.add_parser("run", help="run deterministic fixed-universe portfolio replay")
    run.add_argument("--symbols", required=True)
    run.add_argument("--start", required=True)
    run.add_argument("--end", required=True)
    run.add_argument("--feed", choices=["sip"], default=None, help="Plus SIP regular-session feed stored in the database")
    run.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=None)
    run.add_argument("--capital", type=float, default=100_000)
    run.add_argument("--slippage-bps", type=float, default=5)
    run.add_argument("--spread-bps", type=float, default=3, help="assumed full quoted spread; half is charged on each side")
    run.add_argument("--commission-per-share", type=float, default=0.005)
    run.add_argument("--short-borrow-annual-pct", type=float, default=3.0)
    run.add_argument("--unknown-borrow-penalty-bps", type=float, default=25)
    run.add_argument("--borrow-availability", help="CSV with date,symbol,easy_to_borrow columns")
    run.add_argument("--profile", help="optional JSON with signal parameters only; risk settings cannot be changed")
    run.add_argument("--strategies", help="optional comma-separated strategy isolation filter")
    run.add_argument("--event-calendar", help="optional point-in-time event calendar CSV")
    run.add_argument("--research-max-entries-per-day", type=int)
    run.add_argument("--research-max-open-positions", type=int)
    run.add_argument("--research-max-positions-per-strategy", type=int)
    run.add_argument("--research-max-momentum-entries-per-day", type=int)
    run.add_argument("--research-cooldown-minutes", type=int)
    run.add_argument("--research-one-orb-attempt-per-day", action="store_true")
    run.add_argument("--db", default=str(DEFAULT_DB))
    run.add_argument("--output", help="optional JSON report path")
    run.set_defaults(handler=run_backtest)

    coverage = commands.add_parser("coverage", help="show bars available in the local SQLite store")
    coverage.add_argument("--db", default=str(DEFAULT_DB))
    coverage.set_defaults(handler=lambda args: {"database": str(Path(args.db).resolve()), "coverage": SQLiteBarStore(args.db).coverage()})
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = args.handler(args)
    except (ValueError, OSError, requests.RequestException) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
