"""Replay event loop methods."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import math
import statistics

from .config import BAR_MINUTES, ET, MIN_REGIME_DAILY_BARS, STRATEGY_NAMES
from src.algo.strategies.catalog import INTRADAY_STRATEGIES
from src.algo.strategies.momentum.swing import generate_signals as generate_swing_momentum_signals
from src.algo.strategies.portfolio_coordinator import strategy_priority
from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS
from src.config import STRATEGY_CONFIG


class ReplayLoopMixin:
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
        swing_momentum_signals_for_day: dict[str, object] = {}
        event_times = list(events)
        for index, timestamp in enumerate(event_times):
            bars = events[timestamp]
            trading_day = timestamp.astimezone(ET).date()
            if last_day != trading_day:
                self._current_regime = self._regime(trading_day)
                factor_signals_for_day, factor_diagnostics = self._multi_factor_rebalance(trading_day)
                factor_signals_date = trading_day if factor_diagnostics and factor_diagnostics.get("minimum_universe_met") else None
                momentum_strategy = "momentum"
                held_momentum = {
                    ticker: {
                        "qty": position.qty, "entry_at": position.entry_at,
                        "entry_price": position.entry_price,
                    }
                    for ticker, position in self.positions.items()
                    if position.strategy == momentum_strategy
                }
                try:
                    swing_momentum_signals_for_day, _ = generate_swing_momentum_signals(
                        self.daily_stock_bars, symbols=self.symbols,
                        as_of=trading_day - timedelta(days=1),
                        regime=str(self._current_regime.get("regime", "unknown")),
                        held_positions=held_momentum,
                        parameters={
                            **ALGO_DEFAULT_PARAMETERS[momentum_strategy],
                            **(self.profile.get(momentum_strategy) or {}),
                            "hard_stop_pct": self.risk["stop_loss_pct_by_strategy"][momentum_strategy],
                            "trailing_stop_pct": self.risk["trailing_stop_pct_by_strategy"][momentum_strategy],
                            "trailing_activation_pct": self.risk["trailing_activation_pct_by_strategy"][momentum_strategy],
                        },
                    )
                except Exception as exc:
                    self.rejections[f"signal_error:{momentum_strategy}:{type(exc).__name__}"] += 1
                    swing_momentum_signals_for_day = {}
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
                    # Multi Factor core exits use the same simulator risk
                    # manager and next-bar fill model as other core exits.
                    self._queue_exit_signal(ticker, factor_signal, bar, timestamp)
                    factor_signals_for_day.pop(ticker, None)
                history = self.histories[(ticker, trading_day)]
                factor_support = self.latest_multi_factor_signals.get(ticker)
                position = self.positions.get(ticker)
                signal = self._signal_for(
                    ticker, history, regime.get("active", []), ranked, timestamp,
                    factor_signal=factor_support,
                    position_qty=position.qty if position is not None else 0,
                    swing_momentum_signals=swing_momentum_signals_for_day,
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
            "research_runtime_overrides": dict(self.research_runtime_overrides),
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
                "Each verification fold liquidates remaining positions at its test-window boundary with modeled exit costs.",
                "Signal event counts are raw five-minute buy-signal observations and may include repeated observations of one setup.",
            ],
        }

