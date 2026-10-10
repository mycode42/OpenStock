"""Replay signal generation methods."""

from __future__ import annotations

from dataclasses import replace

from .shared import *
from .shared import _get_universe_maps
from src.algo.strategies.swing_common import CachedDailyBarHistory
from .shared import _make_trade


class ReplaySignalsMixin:
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
        return self._regime_ranker(
            completed,
            mode="algo_research" if self.profile_bank_horizons else "algo",
            event_enabled=self.event_satellite_enabled,
            event_disable_in_volatile=self.event_config.disable_in_volatile,
            intraday_trend_enabled=bool(self.research_runtime_overrides.get(
                "intraday_trend", STRATEGY_CONFIG.get("enable_intraday_trend", False),
            )),
            intraday_momentum_enabled=bool(self.research_runtime_overrides.get(
                "intraday_momentum", STRATEGY_CONFIG.get("enable_intraday_momentum", False),
            )),
        )

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
            if self.strategy_filter is not None and strategy not in self.strategy_filter:
                continue
            if strategy not in CORE_STRATEGIES:
                continue
            if strategy == "intraday_trend" and not self.research_runtime_overrides.get(
                strategy, STRATEGY_CONFIG.get("enable_intraday_trend", False),
            ):
                continue
            if strategy == "intraday_momentum" and not self.research_runtime_overrides.get(
                strategy, STRATEGY_CONFIG.get("enable_intraday_momentum", False),
            ):
                continue
            try:
                if strategy == "swing_momentum" or (
                    strategy == "momentum" and not self.profile_bank_horizons
                ):
                    signal = (swing_momentum_signals or {}).get(ticker)
                    if signal is not None:
                        signal = replace(signal, strategy=strategy)
                else:
                    daily_strategy = strategy in {
                        "swing_trend_following", "swing_momentum", "swing_mean_reversion",
                    }
                    if not self.profile_bank_horizons and strategy == "trend_following":
                        daily_strategy = True
                    signal_bars = (
                        self._completed_daily_history(ticker, timestamp.astimezone(ET).date())
                        if daily_strategy else history
                    )
                    signal_kwargs = {
                        "parameters": self.profile.get(strategy),
                        "position_qty": position_qty,
                        "entry_at": held.entry_at if held is not None else None,
                        "entry_price": held.entry_price if held is not None else None,
                        "position_target": held.take_profit if held is not None else None,
                    }
                    evaluation_name = strategy
                    if self.profile_bank_horizons:
                        evaluation_name = {
                            "trend_following": "intraday_trend",
                            "momentum": "intraday_momentum",
                        }.get(strategy, strategy)
                    if self.profile_bank_horizons and strategy in {"trend_following", "momentum"}:
                        signal_kwargs["runtime_enabled_override"] = True
                    elif strategy in self.research_runtime_overrides:
                        signal_kwargs["runtime_enabled_override"] = self.research_runtime_overrides[strategy]
                    signal = self._signal_evaluator(evaluation_name, ticker, signal_bars, **signal_kwargs)
                    if signal is not None and evaluation_name != strategy:
                        signal = replace(signal, strategy=strategy)
                    elif signal is not None and strategy == "swing_trend_following":
                        signal = replace(signal, strategy=strategy)
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
            daily_timeframe = strategy in {
                "swing_trend_following", "swing_momentum", "swing_mean_reversion",
            } or (
                not self.profile_bank_horizons
                and strategy in {"trend_following", "momentum"}
            )
            item["timeframe"] = "1Day" if daily_timeframe else "5Min"
            options.append(item)

        allowed_satellites = set(self._current_regime.get("satellites", []))
        if (
            not position_qty and factor_signal is not None
            and factor_signal.get("action") in {"buy", "short"}
            and "multi_factor" in allowed_satellites
            and self.multi_factor_enabled
            and (self.strategy_filter is None or "multi_factor" in self.strategy_filter)
        ):
            item = dict(factor_signal)
            item["rank_score"] = -float(item.get("rank") or 1)
            item["timeframe"] = "1Day"
            options.append(item)
        if (
            factor_signal is not None
            and "multi_factor" in allowed_satellites
            and (self.strategy_filter is None or "multi_factor" in self.strategy_filter)
            and position_qty and held is not None and held.strategy == "multi_factor"
            and factor_signal.get("action") in {"sell", "cover"}
        ):
            # Preserve exit management for any legacy satellite-owned holding.
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
            **({"swing_trend_strategy_ids": {"swing_trend_following"}} if self.profile_bank_horizons else {}),
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
            if day == self.day_key and name in (
                RESEARCH_INTRADAY_STRATEGIES if self.profile_bank_horizons else INTRADAY_STRATEGIES
            )
        )
        swing_entries = sum(
            count for (day, name), count in self.entry_counts_by_strategy.items()
            if day == self.day_key and name in (
                RESEARCH_SWING_CORE_STRATEGIES if self.profile_bank_horizons
                else {"trend_following", "momentum", "swing_mean_reversion"}
            )
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
        decision = coordinator_book.review(
            sized, session="regular", timestamp=timestamp,
            regime=str(regime.get("regime", "unknown")),
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

