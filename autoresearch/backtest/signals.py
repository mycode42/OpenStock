"""Replay signal generation methods."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import statistics

from .config import BAR_MINUTES, ET
from .market_sessions import market_session
from strategies.multi_factor.config import MultiFactorConfig
from strategies.multi_factor.signals import SignalGenerator
from strategies.catalog import (
    CORE_STRATEGIES, INTRADAY_STRATEGIES, STRATEGY_CATALOG,
    SWING_CORE_STRATEGIES,
)
from strategies.multi_factor.diagnostics import information_coefficient, turnover
from strategies.portfolio_coordinator import throttle_signal
from strategies.satellite_overlay import apply_satellite_confirmations
from strategies.runner.sizing import _make_trade
from strategies.swing_common import CachedDailyBarHistory


class ReplaySignalsMixin:
    def _multi_factor_rebalance(self, session: date) -> tuple[dict[str, dict], dict | None]:
        if not self.multi_factor_enabled:
            return {}, None
        regime_name = str((self._current_regime or {}).get("regime", "unknown"))
        parameters = self.parameters_for_regime("multi_factor", regime_name)
        if parameters != self.multi_factor_config_key:
            self.multi_factor_generator = SignalGenerator(MultiFactorConfig.from_dict(parameters))
            self.multi_factor_config_key = parameters
            self.last_multi_factor_rebalance_key = None
        key = self.multi_factor_generator.rebalance_key(session)
        if key == self.last_multi_factor_rebalance_key:
            return {}, None
        as_of = session - timedelta(days=1)
        learned_factor_weights = None
        learned_factor_model = None
        factor_model_diagnostics = None
        model_mode = self.multi_factor_generator.config.factor_weight_mode
        if model_mode == "ridge_walk_forward":
            from strategies.multi_factor.learned_weights import resolve_walk_forward_weights

            learned_factor_weights, factor_model_diagnostics = resolve_walk_forward_weights(
                getattr(self, "factor_weight_timeline", None), as_of=as_of,
            )
        elif model_mode in {"lightgbm_walk_forward", "xgboost_walk_forward"}:
            from strategies.multi_factor.learned_weights import resolve_walk_forward_tree_model

            learned_factor_model, factor_model_diagnostics = resolve_walk_forward_tree_model(
                getattr(self, "factor_weight_timeline", None), model_mode=model_mode, as_of=as_of,
            )
        from strategies.multi_factor.fundamentals import (
            load_point_in_time_fundamentals, requested_fundamental_factors,
        )

        fundamentals_by_symbol = None
        fundamentals_diagnostics = {"status": "not_requested", "source": None}
        if requested_fundamental_factors(
            self.multi_factor_generator.config, regime_name, learned_factor_weights,
            learned_factor_model,
        ):
            cache_key = as_of.isoformat()
            cached_fundamentals = self.multi_factor_fundamentals_cache.get(cache_key)
            if cached_fundamentals is not None:
                fundamentals_by_symbol, fundamentals_diagnostics = cached_fundamentals
            else:
                try:
                    fundamentals_by_symbol, fundamentals_diagnostics = load_point_in_time_fundamentals(
                        [symbol for symbol in self.symbols if symbol in getattr(self, "sector_by_symbol", {})]
                        or self.symbols,
                        as_of=as_of,
                    )
                except Exception as exc:
                    fundamentals_by_symbol = {}
                    fundamentals_diagnostics = {
                        "status": "unavailable", "source": "sec_edgar_companyfacts",
                        "failure_types": {"feed": type(exc).__name__},
                    }
                self.multi_factor_fundamentals_cache[cache_key] = (
                    fundamentals_by_symbol, fundamentals_diagnostics,
                )
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
            as_of=as_of,
            session=session,
            regime=str(self._current_regime.get("regime", "unknown")),
            fundamentals_by_symbol=fundamentals_by_symbol,
            fundamentals_feed_diagnostics=fundamentals_diagnostics,
            held_positions=held,
            groups_by_symbol=getattr(self, "sector_by_symbol", None),
            learned_factor_weights=learned_factor_weights,
            learned_factor_model=learned_factor_model,
            factor_model_diagnostics=factor_model_diagnostics,
        )
        if not diagnostics["minimum_universe_met"]:
            self.last_multi_factor_rebalance_key = key
            self.multi_factor_rebalance_diagnostics.append(diagnostics)
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
        target_directions = {
            item.ticker: item.direction for item in signals
            if item.target_weight > 0 and item.direction in {"long", "short"}
        }
        target_weights = {
            item.ticker: item.target_weight for item in signals
            if item.target_weight > 0 and item.direction in {"long", "short"}
        }
        previous_targets = self.multi_factor_snapshots[-1]["targets"] if self.multi_factor_snapshots else set()
        diagnostics["turnover"] = turnover(previous_targets, targets)
        self.multi_factor_snapshots.append({
            "as_of": session - timedelta(days=1),
            "scores": scores,
            "factor_scores": dict(factor_scores),
            "targets": targets,
            "target_directions": target_directions,
            "target_weights": target_weights,
            "regime": diagnostics.get("regime", "unknown"),
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
        net_ic_values = []
        factor_ic_values: dict[str, list[float]] = defaultdict(list)
        factor_net_ic_values: dict[str, list[float]] = defaultdict(list)
        ic_by_regime: dict[str, list[float]] = defaultdict(list)
        net_ic_by_regime: dict[str, list[float]] = defaultdict(list)
        net_portfolio_returns = []
        net_portfolio_returns_by_regime: dict[str, list[float]] = defaultdict(list)
        for snapshot in self.multi_factor_snapshots:
            returns = {}
            net_returns = {}
            start_prices = {}
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
                        start_prices[symbol] = start_close
                        round_trip_cost = 2 * (self.spread_bps / 2 + self.slippage_bps) / 10_000
                        round_trip_cost += 2 * self.commission_per_share / start_close
                        net_returns[symbol] = returns[symbol] - round_trip_cost
            value = information_coefficient(snapshot["scores"], returns)
            if value is not None:
                ic_values.append(value)
                ic_by_regime[str(snapshot.get("regime", "unknown"))].append(value)
            net_value = information_coefficient(snapshot["scores"], net_returns)
            if net_value is not None:
                net_ic_values.append(net_value)
                net_ic_by_regime[str(snapshot.get("regime", "unknown"))].append(net_value)
            for factor, scores in snapshot["factor_scores"].items():
                value = information_coefficient(scores, returns)
                if value is not None:
                    factor_ic_values[factor].append(value)
                value = information_coefficient(scores, net_returns)
                if value is not None:
                    factor_net_ic_values[factor].append(value)
            side_returns = []
            for symbol, direction in snapshot.get("target_directions", {}).items():
                if symbol not in returns or symbol not in start_prices:
                    continue
                sign = 1 if direction == "long" else -1
                start_price = start_prices[symbol]
                round_trip_cost = 2 * (self.spread_bps / 2 + self.slippage_bps) / 10_000
                round_trip_cost += 2 * self.commission_per_share / start_price
                borrow_cost = (
                    self.short_borrow_annual_pct * 21 / 252
                    if direction == "short" else 0.0
                )
                weight = float(snapshot.get("target_weights", {}).get(symbol, 0.0))
                if weight > 0:
                    side_returns.append((weight, sign * returns[symbol] - round_trip_cost - borrow_cost))
            if side_returns:
                total_weight = sum(weight for weight, _ in side_returns)
                portfolio_return = sum(weight * value for weight, value in side_returns) / total_weight
                net_portfolio_returns.append(portfolio_return)
                net_portfolio_returns_by_regime[str(snapshot.get("regime", "unknown"))].append(portfolio_return)

        def summary(values: list[float]) -> dict:
            return {
                "sample_count": len(values),
                "mean": round(statistics.mean(values), 6) if values else None,
                "median": round(statistics.median(values), 6) if values else None,
                "standard_deviation": round(statistics.pstdev(values), 6) if values else None,
                "positive_sample_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
            }

        realized_trades = [
            trade for trade in self.closed_trades
            if trade.get("strategy") == "multi_factor"
        ]
        factor_model_rows = [
            row.get("factor_model") or {}
            for row in self.multi_factor_rebalance_diagnostics
            if (row.get("factor_model") or {}).get("status") == "loaded"
        ]
        latest_weight_diagnostics = {}
        latest_active_factor_weights = {}
        skipped_rebalances = [
            row for row in self.multi_factor_rebalance_diagnostics
            if row.get("skip_reason")
        ]
        skip_reason_counts = Counter(str(row["skip_reason"]) for row in skipped_rebalances)
        ranked_counts = [
            int(row["ranked_count"])
            for row in self.multi_factor_rebalance_diagnostics
            if isinstance(row.get("ranked_count"), int)
        ]
        if self.multi_factor_rebalance_diagnostics:
            latest_diagnostics = self.multi_factor_rebalance_diagnostics[-1]
            latest_weight_diagnostics = dict(latest_diagnostics.get("factor_model") or {})
            latest_active_factor_weights = dict(latest_diagnostics.get("active_factor_weights") or {})
        by_factor = {
            factor: {
                "gross": summary(factor_ic_values[factor]),
                "net_of_costs": summary(factor_net_ic_values[factor]),
            }
            for factor in sorted(set(factor_ic_values) | set(factor_net_ic_values))
        }
        return {
            "rebalance_count": len(self.multi_factor_snapshots),
            "attempted_rebalance_count": len(self.multi_factor_rebalance_diagnostics),
            "skipped_rebalance_count": len(skipped_rebalances),
            "skip_reason_counts": dict(sorted(skip_reason_counts.items())),
            "ranked_universe_count_range": {
                "minimum": min(ranked_counts) if ranked_counts else None,
                "median": statistics.median(ranked_counts) if ranked_counts else None,
                "maximum": max(ranked_counts) if ranked_counts else None,
            },
            "minimum_universe_required": next((
                row.get("minimum_universe_required")
                for row in reversed(self.multi_factor_rebalance_diagnostics)
                if row.get("minimum_universe_required") is not None
            ), None),
            "factor_weight_mode": self.multi_factor_generator.config.factor_weight_mode,
            "factor_weight_source_counts": dict(Counter(
                str(row.get("factor_weight_source", "unknown"))
                for row in self.multi_factor_rebalance_diagnostics
            )),
            "learned_weight_snapshot_count": len(factor_model_rows),
            "latest_factor_model": latest_weight_diagnostics,
            "latest_active_factor_weights": latest_active_factor_weights,
            "average_turnover": round(statistics.mean(
                [row["turnover"] for row in self.multi_factor_rebalance_diagnostics if row.get("turnover") is not None]
            ), 4) if any(row.get("turnover") is not None for row in self.multi_factor_rebalance_diagnostics) else None,
            "gross_ic_forward_21_sessions": summary(ic_values),
            "net_of_costs_ic_forward_21_sessions": summary(net_ic_values),
            "gross_ic_forward_21_sessions_by_regime": {
                regime: summary(values) for regime, values in sorted(ic_by_regime.items())
            },
            "net_of_costs_ic_forward_21_sessions_by_regime": {
                regime: summary(values) for regime, values in sorted(net_ic_by_regime.items())
            },
            "factor_ic_forward_21_sessions": by_factor,
            "after_cost_target_portfolio_forward_21_session_return": summary(net_portfolio_returns),
            "after_cost_target_portfolio_forward_21_session_return_by_regime": {
                regime: summary(values) for regime, values in sorted(net_portfolio_returns_by_regime.items())
            },
            "realized_strategy_economics": {
                "closed_trade_count": len(realized_trades),
                "net_pnl": round(sum(float(trade.get("pnl", 0.0)) for trade in realized_trades), 2),
                "win_rate_pct": round(100 * sum(float(trade.get("pnl", 0.0)) > 0 for trade in realized_trades) / len(realized_trades), 2)
                if realized_trades else None,
                "costs_included": True,
            },
            "ic_horizon_sessions": 21,
            "ic_labels_overlap": True,
            "target_return_cost_model": {
                "spread_bps_round_trip_reference": self.spread_bps,
                "slippage_bps_each_side": self.slippage_bps,
                "commission_per_share_each_side": self.commission_per_share,
                "short_borrow_annual_pct": self.short_borrow_annual_pct,
            },
            "value_factor_enabled": latest_active_factor_weights.get(
                "value_earnings_yield",
                self.multi_factor_generator.config.factor_weights.get("value_earnings_yield", 0),
            ) > 0,
        }

    def _regime(self, trading_day: date) -> dict:
        cached = self.regime_cache.get(trading_day) if self.regime_cache is not None else None
        if cached is not None:
            self.regime_history.append({
                "date": trading_day.isoformat(),
                "regime": cached.get("regime", "unknown"),
                "volatility_state": cached.get("volatility_state", "unknown"),
                "features": cached.get("regime_features", {}),
                "models": cached.get("regime_shadow", {}),
            })
            return cached
        completed = []
        for bar in self.spy_daily_bars:
            try:
                timestamp = self._timestamp(bar)
                if timestamp.astimezone(ET).date() < trading_day:
                    completed.append(bar)
            except (KeyError, TypeError, ValueError):
                continue
        completed.sort(key=self._timestamp)
        config = self.regime_config
        feature_rows = [
            row for row in self.regime_feature_history
            if row["date"] < trading_day.isoformat()
        ]
        result = self._regime_ranker(
            completed,
            mode="algo",
            event_enabled=self.event_satellite_enabled,
            event_disable_in_volatile=self.event_config.disable_in_volatile,
            intraday_trend_enabled=bool(self.research_runtime_overrides.get(
                "intraday_trend", self.runtime_settings.enable_intraday_trend,
            )),
            intraday_momentum_enabled=bool(self.research_runtime_overrides.get(
                "intraday_momentum", self.runtime_settings.enable_intraday_momentum,
            )),
            feature_set=config["feature_set"],
            engine=config["engine"],
            strategy_policy=config["strategy_policy"],
            context_bars=self.regime_context_bars,
            macro_series=self.regime_macro_series,
            include_models=bool(config.get("include_models", False)),
            model_training_window=int(config.get("model_training_window", 756)),
            model_min_confidence=float(config.get("min_model_confidence", 0.50)),
            precomputed_features=feature_rows,
        )
        result["regime_macro_data_status"] = str(config.get("macro_data_status", "provided"))
        if self.regime_cache is not None:
            self.regime_cache[trading_day] = result
        self.regime_history.append({
            "date": trading_day.isoformat(),
            "regime": result.get("regime", "unknown"),
            "volatility_state": result.get("volatility_state", "unknown"),
            "features": result.get("regime_features", {}),
            "models": result.get("regime_shadow", {}),
        })
        return result

    def _previous_close(self, ticker: str, trading_day: date) -> float | None:
        return self.previous_closes_by_day.get((ticker.upper(), trading_day))

    def _completed_daily_history(self, ticker: str, trading_day: date) -> list[dict]:
        if (
            self._daily_history_cache_day is not None
            and trading_day < self._daily_history_cache_day
        ):
            # Keep direct, out-of-order calls correct too. The replay itself is
            # chronological, so this reset is only needed by callers that move
            # the requested session backwards.
            for history in self._daily_history_cache.values():
                history.clear()
            self._daily_history_indices = {
                symbol: 0 for symbol in self.daily_stock_bars
            }
        self._daily_history_cache_day = trading_day
        key = ticker.upper()
        history = self._daily_history_cache.get(key)
        if history is None:
            history = CachedDailyBarHistory()
            self._daily_history_cache[key] = history
        rows = self.daily_stock_bars.get(key, [])
        index = self._daily_history_indices.get(key, 0)
        while index < len(rows):
            row = rows[index]
            if date.fromisoformat(str(row.get("date", ""))[:10]) >= trading_day:
                break
            history.append(row)
            index += 1
        self._daily_history_indices[key] = index
        return history

    def _signal_for(
        self, ticker: str, history: list[dict], active: list[str], ranked: dict,
        timestamp: datetime, factor_signal: dict | None = None, position_qty: float = 0,
        swing_momentum_signals: dict[str, object] | None = None,
    ) -> dict | None:
        options = []
        held = self.positions.get(ticker)
        strategies = [held.strategy] if held is not None else active
        for strategy in strategies:
            strategy_spec = STRATEGY_CATALOG.get(strategy)
            if self.strategy_filter is not None and strategy not in self.strategy_filter:
                continue
            if strategy not in CORE_STRATEGIES:
                continue
            # Multi Factor has a dedicated rebalance generator below; it is a
            # core strategy, but it has no rule-pipeline evaluator.
            if strategy == "multi_factor":
                continue
            runtime_setting = strategy_spec.runtime_setting if strategy_spec else None
            if runtime_setting and not self.research_runtime_overrides.get(
                strategy, getattr(self.runtime_settings, runtime_setting),
            ):
                continue
            try:
                if strategy in {"momentum", "swing_momentum"}:
                    signal = (swing_momentum_signals or {}).get(ticker)
                    if signal is not None:
                        signal = replace(signal, strategy=strategy)
                else:
                    daily_strategy = bool(strategy_spec and strategy_spec.bar_interval == "1Day")
                    signal_bars = (
                        self._completed_daily_history(ticker, self._trading_date(timestamp))
                        if daily_strategy else history
                    )
                    signal_kwargs = {
                        "parameters": self.parameters_for_regime(
                            strategy, str((self._current_regime or {}).get("regime", "unknown")),
                        ),
                        "position_qty": position_qty,
                        "entry_at": held.entry_at if held is not None else None,
                        "entry_price": held.entry_price if held is not None else None,
                        "position_target": held.take_profit if held is not None else None,
                    }
                    if strategy in self.research_runtime_overrides:
                        signal_kwargs["runtime_enabled_override"] = self.research_runtime_overrides[strategy]
                    signal = self._signal_evaluator(strategy, ticker, signal_bars, **signal_kwargs)
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
            daily_timeframe = bool(strategy_spec and strategy_spec.bar_interval == "1Day")
            item["timeframe"] = "1Day" if daily_timeframe else "5Min"
            options.append(item)

        allowed_satellites = set(self._current_regime.get("satellites", []))
        if (
            not position_qty and factor_signal is not None
            and factor_signal.get("action") in {"buy", "short"}
            and "multi_factor" in self._current_regime.get("active", [])
            and self.multi_factor_enabled
            and (self.strategy_filter is None or "multi_factor" in self.strategy_filter)
        ):
            item = dict(factor_signal)
            item["rank_score"] = -float(item.get("rank") or 1)
            item["timeframe"] = "1Day"
            options.append(item)
        if (
            factor_signal is not None
            and (self.strategy_filter is None or "multi_factor" in self.strategy_filter)
            and position_qty and held is not None and held.strategy == "multi_factor"
            and factor_signal.get("action") in {"sell", "cover"}
        ):
            # Multi Factor is a core swing strategy and owns its exits.
            options.append({**factor_signal, "rank_score": -1.0, "timeframe": "1Day"})

        event_evidence = None

        event_signal = None
        if (
            (self.event_satellite_enabled or (held is not None and held.strategy == "event_driven"))
            and (
                "event_driven" in allowed_satellites
                or (held is not None and held.strategy == "event_driven")
            )
            and (self.strategy_filter is None or "event_driven" in self.strategy_filter)
            and (held is None or held.strategy == "event_driven")
        ):
            try:
                signal_as_of = timestamp + timedelta(minutes=BAR_MINUTES)
                event_signal = self.event_signal_generator.generate(
                    ticker,
                    self._completed_daily_history(ticker, self._trading_date(timestamp)),
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
            self.signal_events_by_strategy[(self.day_key or "", "event_driven")] += 1
            if (
                position_qty and held is not None and held.strategy == "event_driven"
                and event_signal.action in {"sell", "cover"}
            ):
                options.append({**event_evidence, "rank_score": -1.0, "timeframe": "1Day"})

        if not options:
            return None
        chosen = max(options, key=lambda item: (item["rank_score"], item["strength"]))
        chosen = apply_satellite_confirmations(
            chosen,
            factor_signal=(factor_signal if "multi_factor" in allowed_satellites else None),
            event_signal=event_evidence if "event_driven" in allowed_satellites else None,
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
        if strategy == "event_driven":
            self.rejections["event_driven_disabled"] += 1
            return
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
            group_entries = sum(
                count for (day, member), count in self.entry_counts_by_strategy.items()
                if day == self.day_key and member in members
            )
            if group_entries >= int(group.get("max_entries_per_day", 10**9)):
                self.rejections["research_strategy_group_daily_cap"] += 1
                return
        regime = self._current_regime
        if action == "short":
            regime_name = str(regime.get("regime", "unknown"))
            if ticker not in self.symbols:
                self.rejections["short_universe_block"] += 1
                return
            if regime_name == "range_bound":
                signal = {**signal, "risk_multiplier": float(self.risk.get("range_regime_short_risk_multiplier", 0.75))}
            borrow_status = self._borrow_status(ticker, timestamp)
            if borrow_status is False:
                self.rejections["hard_to_borrow_short_skip"] += 1
                return
        close = float(bar["c"])
        current_equity = self._equity()
        signal, throttle_note = throttle_signal(
            signal, str(regime.get("regime", "unknown")), self.coordinator.config,
            risk_multiplier=float(regime.get("strategy_risk_multipliers", {}).get(strategy, 1.0)),
        )
        if getattr(self, "research_strategy_weights", None):
            allocation_weight = float(self.research_strategy_weights.get(strategy, 1.0))
            signal = {
                **signal,
                "risk_multiplier": float(signal.get("risk_multiplier", 1.0)) * allocation_weight,
            }
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
        swing_entries = sum(
            count for (day, name), count in self.entry_counts_by_strategy.items()
            if day == self.day_key and name in SWING_CORE_STRATEGIES
        )
        coordinator_book = self.coordinator.new_book(
            equity=float(state["equity"]),
            positions=state["positions"],
            position_owners={name: position.strategy for name, position in self.positions.items()},
            pending_buy_values=state["pending_buy_value_by_ticker"],
            pending_short_values=state["pending_short_value_by_ticker"],
            entries_today=int(state["entry_count_today"]),
            intraday_entries_today=intraday_entries,
            swing_entries_today=swing_entries,
        )
        session_name = market_session(timestamp) if self.include_extended_hours else "regular"
        session_close = None
        if self.include_extended_hours:
            session_name = session_name or "closed"
            session_close = datetime.combine(
                self._trading_date(timestamp), time(20, 0), tzinfo=ET,
            )
        decision = coordinator_book.review(
            sized, session=session_name, timestamp=timestamp,
            regime=str(regime.get("regime", "unknown")),
            volatility_state=str(regime.get("volatility_state", "unknown")),
            session_close=session_close,
            allow_extended_hours=self.include_extended_hours,
        )
        if not decision.approved or decision.proposal is None:
            self.rejections[f"portfolio_coordinator:{decision.reason}"] += 1
            return
        sized = decision.proposal
        result = self._trade_validator(
            ticker=ticker, action=action, qty=int(sized["qty"]),
            entry_price=close, portfolio_state=state, mode="algo", strategy=strategy,
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
            "trailing_stop_pct": sized.get("trailing_stop_pct"),
            "trailing_activation_pct": sized.get("trailing_activation_pct"),
            "signal_at": signal_at,
            "delay_bars_remaining": int(getattr(self, "execution_delay_bars", 0)),
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

