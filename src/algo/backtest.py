"""Deterministic regular-session portfolio replay for the live Algo rules.

This is a research simulator, not an order router. Signals use completed bars;
orders fill no earlier than the next bar's open with configured slippage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import statistics
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
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
    evaluate as evaluate_strategy,
    validate_parameter_profile,
)
from src.strategies.regime import rank_strategies
from src.strategies.runner import _make_trade


ET = ZoneInfo("America/New_York")
BAR_MINUTES = 5
DAILY_WARMUP_DAYS = 180


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
    ) -> None:
        if not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("Initial capital must be a positive finite number")
        if not math.isfinite(slippage_bps) or slippage_bps < 0:
            raise ValueError("Slippage basis points must be finite and non-negative")
        if feed not in {"iex", "sip"}:
            raise ValueError("Regular-session replay supports only iex or sip; BOATS requires its own overnight model")
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
        self.risk = get_mode_config("algo")["risk"]
        self.profile = validate_parameter_profile(profile or {})
        self.positions: dict[str, Position] = {}
        self.pending_entries: dict[str, dict] = {}
        self.last_prices: dict[str, float] = {}
        self.histories: dict[tuple[str, date], list[dict]] = defaultdict(list)
        self.closed_trades: list[dict] = []
        self.rejections: Counter[str] = Counter()
        self.entry_counts: Counter[str] = Counter()
        self.daily_curve: list[dict] = []
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
        return self.cash + sum(
            position.qty * self.last_prices.get(ticker, position.entry_price)
            for ticker, position in self.positions.items()
        )

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
            sectors[sector] += market_value
        pending_value = {
            ticker: float(order["qty"]) * float(order["reference_price"])
            for ticker, order in self.pending_entries.items()
        }
        for ticker, value in pending_value.items():
            sectors[self.ticker_sector.get(ticker, "other")] += value
        pending_new = {ticker for ticker in pending_value if ticker not in positions}
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
        exit_value = position.qty * price
        self.cash += exit_value
        pnl = position.qty * (price - position.entry_price)
        self.closed_trades.append({
            "ticker": ticker,
            "strategy": position.strategy,
            "qty": position.qty,
            "entry_at": position.entry_at,
            "entry_price": round(position.entry_price, 6),
            "signal_at": position.signal_at,
            "exit_at": timestamp.isoformat(),
            "exit_price": round(price, 6),
            "exit_reason": reason,
            "pnl": round(pnl, 2),
            "return_pct": round(price / position.entry_price - 1, 6),
        })

    def _check_brackets(self, ticker: str, bar: dict, timestamp: datetime) -> None:
        position = self.positions.get(ticker)
        if position is None:
            return
        opening, high, low = (float(bar[key]) for key in ("o", "h", "l"))
        stop_hit = low <= position.stop_price
        target_hit = high >= position.take_profit
        if opening <= position.stop_price:
            raw_fill, reason, slippage = opening, "stop_gap", self.slippage
        elif opening >= position.take_profit:
            raw_fill, reason, slippage = opening, "target_gap", 0.0
        elif stop_hit:
            # If both bracket legs occur within one bar, assume the adverse leg.
            raw_fill, reason, slippage = position.stop_price, "stop", self.slippage
        elif target_hit:
            # A limit target may receive price improvement at an opening gap.
            raw_fill, reason, slippage = position.take_profit, "target", 0.0
        else:
            return
        self._close_position(ticker, raw_fill * (1 - slippage), timestamp, reason)

    def _fill_pending_entries(self, bars: dict[str, dict], timestamp: datetime) -> None:
        for ticker in list(self.pending_entries):
            bar = bars.get(ticker)
            if bar is None:
                continue
            order = self.pending_entries.pop(ticker)
            fill_price = float(bar["o"]) * (1 + self.slippage)
            required_cash = fill_price * order["qty"]
            if required_cash > self.cash - self._equity() * float(self.risk["min_cash_pct"]):
                self.rejections["fill_cash_gap"] += 1
                continue
            self.cash -= required_cash
            self.positions[ticker] = Position(
                ticker=ticker, strategy=order["strategy"], qty=order["qty"],
                entry_price=fill_price, entry_at=timestamp.isoformat(),
                stop_price=order["stop_price"], take_profit=order["take_profit"],
                signal_at=order["signal_at"],
            )
            self._check_brackets(ticker, bar, timestamp)

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
        return rank_strategies(completed, mode="algo")

    def _signal_for(self, ticker: str, history: list[dict], active: list[str], ranked: dict) -> dict | None:
        options = []
        for strategy in active:
            try:
                signal = evaluate_strategy(
                    strategy, "algo", ticker, history, 0,
                    parameters=self.profile.get(strategy),
                )
            except Exception as exc:
                self.rejections[f"signal_error:{strategy}:{type(exc).__name__}"] += 1
                continue
            if signal is None or signal.action != "buy":
                continue
            item = signal.to_dict()
            item["rank_score"] = ranked.get(strategy, 0)
            options.append(item)
        if not options:
            return None
        return max(options, key=lambda item: (item["rank_score"], item["strength"]))

    def _queue_signal(self, ticker: str, signal: dict, bar: dict, timestamp: datetime) -> None:
        if ticker in self.positions or ticker in self.pending_entries:
            return
        if self.day_key is None:
            return
        if not signal or signal.get("action") != "buy":
            return
        regime = self._current_regime
        if regime.get("regime") == "trending_down":
            self.rejections["downtrend_entry_block"] += 1
            return
        close = float(bar["c"])
        current_equity = self._equity()
        sized = _make_trade(signal, close, current_equity, {}, self.risk)
        if not sized:
            self.rejections["zero_sized"] += 1
            return
        state = self._risk_state()
        result = validate_trade(
            ticker=ticker, action="buy", qty=int(sized["qty"]),
            entry_price=close, portfolio_state=state, mode="algo",
        )
        if not result.approved:
            self.rejections[str(result.rule or "risk_rejected")] += 1
            return
        signal_at = (timestamp + timedelta(minutes=BAR_MINUTES)).isoformat()
        self.pending_entries[ticker] = {
            "ticker": ticker, "strategy": signal["strategy"],
            "qty": int(sized["qty"]), "reference_price": close,
            "stop_price": float(sized["stop_price"]),
            "take_profit": float(sized["take_profit"]),
            "signal_at": signal_at,
        }
        self.entry_counts[self.day_key] += 1

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
        if sum(day < first_day for day in daily_dates) < 22:
            raise ValueError("At least 22 completed SPY daily bars before the replay are required")

        last_day: date | None = None
        event_times = list(events)
        for index, timestamp in enumerate(event_times):
            bars = events[timestamp]
            trading_day = timestamp.astimezone(ET).date()
            if last_day != trading_day:
                self._current_regime = self._regime(trading_day)
                last_day = trading_day
            self._start_periods(trading_day)
            self._fill_pending_entries(bars, timestamp)
            for ticker, bar in bars.items():
                self._check_brackets(ticker, bar, timestamp)
            for ticker, bar in bars.items():
                self.last_prices[ticker] = float(bar["c"])
                history = self.histories[(ticker, trading_day)]
                history.append(bar)
            regime = self._current_regime
            ranked = {
                item["strategy"]: float(item.get("selection_score", item["score"]))
                for item in regime.get("ranked", [])
            }
            for ticker, bar in bars.items():
                if ticker in self.positions or ticker in self.pending_entries:
                    continue
                history = self.histories[(ticker, trading_day)]
                signal = self._signal_for(ticker, history, regime.get("active", []), ranked)
                if signal:
                    self._queue_signal(ticker, signal, bar, timestamp)

            equity = self._equity()
            self.peak_equity = max(self.peak_equity, equity)
            self.max_drawdown = max(self.max_drawdown, (self.peak_equity - equity) / self.peak_equity)
            # Capture the last replay mark for each New York trading date.
            if index + 1 == len(event_times) or event_times[index + 1].astimezone(ET).date() != trading_day:
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
        profile_hash = hashlib.sha256(
            json.dumps(self.profile, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        open_positions = []
        for ticker, position in sorted(self.positions.items()):
            mark = self.last_prices.get(ticker, position.entry_price)
            open_positions.append({
                "ticker": ticker, "strategy": position.strategy, "qty": position.qty,
                "entry_at": position.entry_at, "entry_price": round(position.entry_price, 6),
                "mark": round(mark, 6), "unrealized_pnl": round(position.qty * (mark - position.entry_price), 2),
                "stop_price": position.stop_price, "take_profit": position.take_profit,
            })
        merged_profile = deepcopy(ALGO_DEFAULT_PARAMETERS)
        for strategy, values in self.profile.items():
            merged_profile[strategy].update(values)
        return {
            "simulator": "swarm_algo_portfolio_v1",
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
            "max_drawdown_pct": round(self.max_drawdown * 100, 4),
            "closed_trade_count": len(self.closed_trades),
            "open_position_count": len(open_positions),
            "win_rate_pct": round(100 * len(winners) / len(closed_pnls), 2) if closed_pnls else None,
            "profit_factor": round(sum(winners) / abs(sum(losers)), 4) if losers else (None if not winners else "inf"),
            "average_trade_pnl": round(statistics.mean(closed_pnls), 2) if closed_pnls else None,
            "daily_sharpe_annualized": round(math.sqrt(252) * statistics.mean(daily_returns) / daily_std, 4)
            if daily_std > 0 and daily_returns else None,
            "slippage_bps_each_side": self.slippage_bps,
            "fees": "commissions and regulatory fees are not modeled",
            "parameter_profile": merged_profile,
            "parameter_profile_sha256": profile_hash,
            "risk_config": {
                "risk_per_trade_pct": STRATEGY_CONFIG["risk_per_trade_pct"],
                "max_strategy_position_pct": STRATEGY_CONFIG["max_strategy_position_pct"],
                "stop_loss_pct": self.risk["stop_loss_pct"],
                "take_profit_r_multiple": STRATEGY_CONFIG["take_profit_r_multiple"],
                "max_open_positions": self.risk["max_open_positions"],
                "max_trades_per_day": self.risk["max_trades_per_day"],
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
            "daily_equity": self.daily_curve,
            "closed_trades": self.closed_trades,
            "open_positions": open_positions,
            "unfilled_entries_at_end": sorted(self.pending_entries),
            "assumptions": [
                "Fixed user-supplied universe; no point-in-time membership or delisted-symbol reconstruction.",
                "Completed 5-minute bars only; signals fill at the next available bar open.",
                "Market entries and stop-market exits pay configured adverse slippage; limit targets fill at target or a favorable opening gap.",
                "If stop and target both trade within one bar, the stop is assumed to execute first.",
                "Regular-session only. Overnight and pre/post-market replay requires BOATS bars and a separate session fill model.",
                "LLM regime advice is omitted; replay uses the deterministic historical SPY regime only.",
                "Risk limits are fixed. The profile changes signal-rule parameters only.",
                "Open positions remain marked to market at the end; Algo positions are not forcibly flattened.",
            ],
        }


def _split_symbols(value: str) -> list[str]:
    symbols = list(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))
    if not symbols:
        raise argparse.ArgumentTypeError("Provide at least one comma-separated symbol")
    return symbols


def _read_profile(path: str | None) -> dict:
    if not path:
        return {}
    profile = json.loads(Path(path).read_text())
    return validate_parameter_profile(profile)


def fetch_dataset(args) -> dict:
    symbols = _split_symbols(args.symbols)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        raise ValueError("End date must not be before start date")
    feed = args.feed or regular_feed()
    tier = data_tier()
    store = SQLiteBarStore(args.db)
    request_start = f"{start.isoformat()}T00:00:00Z"
    request_end = f"{end.isoformat()}T23:59:59Z"
    intraday = fetch_alpaca_bars(
        symbols, timeframe="5Min", start=request_start, end=request_end,
        feed=feed, adjustment=args.adjustment,
    )
    intraday_rows = store.write_bars(
        intraday, feed=feed, timeframe="5Min", adjustment=args.adjustment,
        data_tier=tier, requested_start=request_start, requested_end=request_end,
    )
    daily_start = start - timedelta(days=DAILY_WARMUP_DAYS)
    daily_from = f"{daily_start.isoformat()}T00:00:00Z"
    daily = fetch_alpaca_bars(
        ["SPY"], timeframe="1Day", start=daily_from, end=request_end,
        feed=feed, adjustment=args.adjustment,
    )
    daily_rows = store.write_bars(
        daily, feed=feed, timeframe="1Day", adjustment=args.adjustment,
        data_tier=tier, requested_start=daily_from, requested_end=request_end,
    )
    return {
        "database": str(Path(args.db).resolve()), "data_tier": tier, "feed": feed,
        "adjustment": args.adjustment, "intraday_rows": intraday_rows,
        "spy_daily_rows": daily_rows, "symbols": symbols,
    }


def run_backtest(args) -> dict:
    symbols = _split_symbols(args.symbols)
    feed = args.feed or regular_feed()
    store = SQLiteBarStore(args.db)
    intraday = store.bars(
        symbols, feed=feed, timeframe="5Min", start=args.start,
        end=args.end, adjustment=args.adjustment,
    )
    daily_start = (date.fromisoformat(args.start) - timedelta(days=DAILY_WARMUP_DAYS)).isoformat()
    daily = store.bars(
        ["SPY"], feed=feed, timeframe="1Day", start=daily_start,
        end=args.end, adjustment=args.adjustment,
    ).get("SPY", [])
    missing = [ticker for ticker in symbols if not intraday.get(ticker)]
    if missing:
        raise ValueError(f"No stored 5-minute {feed} bars for: {', '.join(missing)}")
    replay = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=daily,
        start=args.start, end=args.end, feed=feed,
        data_adjustment=args.adjustment, initial_capital=args.capital,
        slippage_bps=args.slippage_bps, profile=_read_profile(args.profile),
    )
    report = replay.run()
    report["data_tier_at_run"] = data_tier()
    report["database"] = str(Path(args.db).resolve())
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
        "--feed", choices=["iex", "sip"], default=None,
        help="regular-session feed; defaults to IEX on Basic or SIP on Algo Trader Plus",
    )
    fetch.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default="raw")
    fetch.add_argument("--db", default=str(DEFAULT_DB))
    fetch.set_defaults(handler=fetch_dataset)

    run = commands.add_parser("run", help="run deterministic fixed-universe portfolio replay")
    run.add_argument("--symbols", required=True)
    run.add_argument("--start", required=True)
    run.add_argument("--end", required=True)
    run.add_argument("--feed", choices=["iex", "sip"], default=None, help="regular-session feed stored in the database")
    run.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default="raw")
    run.add_argument("--capital", type=float, default=100_000)
    run.add_argument("--slippage-bps", type=float, default=5)
    run.add_argument("--profile", help="optional JSON with signal parameters only; risk settings cannot be changed")
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
