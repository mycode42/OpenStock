"""Convert daily cross-sectional ranks into bounded long/short targets."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, replace
from datetime import date

import numpy as np

from strategies.multi_factor.config import MultiFactorConfig, SUPPORTED_FACTORS
from strategies.multi_factor.features import FactorCalculator
from strategies.multi_factor.ranking import FactorRanker
from strategies.swing_common import clean_daily_bars, held_sessions


LOG = logging.getLogger("algo_trader.multi_factor")


@dataclass(frozen=True)
class MultiFactorSignal:
    ticker: str
    strategy: str
    action: str
    direction: str
    score: float
    rank: int
    target_weight: float
    as_of: str
    rebalance_key: str
    factor_values: dict[str, float]
    factor_zscores: dict[str, float]
    reason: str
    stop_pct: float = 0.06
    risk_multiplier: float = 0.5
    strategy_group: str = "swing"

    @property
    def strength(self) -> float:
        directional_score = abs(self.score) if self.direction == "short" else self.score
        return max(0.0, min(1.0, 0.5 + directional_score / 6))

    def to_dict(self) -> dict:
        return {**asdict(self), "strength": self.strength}


class SignalGenerator:
    """Build PIT-safe factor evidence and rank candidates for the current regime."""

    def __init__(self, config: MultiFactorConfig | None = None):
        self.config = config or MultiFactorConfig()
        self.calculator = FactorCalculator(self.config)
        self.ranker = FactorRanker(self.config)

    def rebalance_key(self, session: date) -> str:
        if self.config.rebalance_frequency == "daily":
            return session.isoformat()
        year, week, _ = session.isocalendar()
        return f"{year}-W{week:02d}"

    def generate(
        self,
        bars_by_symbol: dict[str, list[dict]],
        *,
        symbols: list[str],
        as_of: date,
        session: date,
        regime: str = "unknown",
        fundamentals_by_symbol: dict[str, list[dict]] | None = None,
        fundamentals_feed_diagnostics: dict | None = None,
        held_positions: set[str] | dict[str, dict] | None = None,
        groups_by_symbol: dict[str, str] | None = None,
        tradable_symbols: set[str] | None = None,
        learned_factor_weights: dict[str, float] | None = None,
        learned_factor_model: dict | None = None,
        factor_model_diagnostics: dict | None = None,
    ) -> tuple[list[MultiFactorSignal], dict]:
        symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        regime_override = None
        learned_weights_status = "not_requested"
        is_tree_model = self.config.factor_weight_mode in {
            "lightgbm_walk_forward", "xgboost_walk_forward",
        }
        if self.config.factor_weight_mode == "ridge_walk_forward":
            learned_factor_weights = learned_factor_weights or {}
            factor_weights = {
                str(name): float(weight)
                for name, weight in learned_factor_weights.items()
                if name in SUPPORTED_FACTORS
            }
            factor_weights = {
                name: weight for name, weight in factor_weights.items()
                if math.isfinite(weight) and weight > 0
            }
            total = sum(factor_weights.values())
            if total > 0:
                factor_weights = {name: weight / total for name, weight in factor_weights.items()}
                learned_weights_status = "loaded"
            else:
                learned_weights_status = "unavailable"
        elif is_tree_model:
            model = learned_factor_model if isinstance(learned_factor_model, dict) else {}
            model_factors = model.get("factor_names")
            expected_format = "lightgbm_text" if self.config.factor_weight_mode == "lightgbm_walk_forward" else "xgboost_json"
            if (
                model.get("model_mode") == self.config.factor_weight_mode
                and isinstance(model_factors, list)
                and model_factors
                and not (set(model_factors) - SUPPORTED_FACTORS)
                and isinstance(model.get("model_payload"), str)
                and model.get("model_format") == expected_format
            ):
                factor_weights = {name: 1.0 for name in model_factors}
                learned_weights_status = "loaded"
            else:
                factor_weights = {}
                learned_weights_status = "unavailable"
        else:
            factor_weights = dict(self.config.factor_weights)
            regime_override = self.config.regime_factor_weights.get(str(regime).lower())
            if regime_override:
                factor_weights.update(regime_override)
        if self.config.factor_weight_mode != "static" and learned_weights_status != "loaded":
            return [], {
                "as_of": as_of.isoformat(),
                "rebalance_key": self.rebalance_key(session),
                "rebalance_frequency": self.config.rebalance_frequency,
                "regime": regime,
                "factor_weight_mode": self.config.factor_weight_mode,
                "factor_weight_source": "unavailable",
                "factor_model": factor_model_diagnostics or {},
                "factor_timing_status": "learned_snapshot_unavailable",
                "skip_reason": "learned_snapshot_unavailable",
                "universe_count": len(symbols),
                "feature_count": 0,
                "ranked_count": 0,
                "minimum_universe_required": self.config.min_universe,
                "minimum_universe_met": False,
                "omitted_factors": [
                    "No learned snapshot met point-in-time fit, purge, and label freshness limits."
                ],
            }
        active_config = replace(self.config, factor_weights=factor_weights)
        calculator = FactorCalculator(active_config)
        ranker = FactorRanker(active_config)
        features, mask_diagnostics = calculator.calculate_with_diagnostics(
            bars_by_symbol, symbols=symbols, as_of=as_of,
            fundamentals_by_symbol=fundamentals_by_symbol,
            tradable_symbols=tradable_symbols,
        )
        ranked = ranker.rank(
            features,
            groups_by_symbol=(groups_by_symbol if self.config.factor_neutralization == "sector" else None),
            scoring_model=learned_factor_model if is_tree_model else None,
        )
        factor_coverage = {
            factor: sum(
                factor in values and isinstance(values[factor], (int, float))
                for values in features.values()
            )
            for factor, weight in factor_weights.items() if weight > 0
        }
        usable_factor_count = sum(
            count >= self.config.min_universe for count in factor_coverage.values()
        )
        required_model_factors = (
            list((learned_factor_model or {}).get("factor_names") or [])
            if is_tree_model else []
        )
        minimum_model_feature_coverage = (
            int((learned_factor_model or {}).get("minimum_factor_coverage") or self.config.min_universe)
            if is_tree_model else self.config.min_universe
        )
        model_feature_coverage = {
            name: factor_coverage.get(name, 0) for name in required_model_factors
        }
        model_features_below_minimum = any(
            count < minimum_model_feature_coverage for count in model_feature_coverage.values()
        )
        selected_factors = sorted({factor for row in ranked for factor in row["factor_values"]})
        group_sizes: dict[str, int] = {}
        for symbol in features:
            group = (groups_by_symbol or {}).get(symbol)
            if group:
                group_sizes[str(group)] = group_sizes.get(str(group), 0) + 1
        omitted_factors = [
            f"{factor}: {count} names with usable values; need {self.config.min_universe}"
            for factor, count in factor_coverage.items()
            if factor not in selected_factors and count < self.config.min_universe
        ]
        for factor, count in factor_coverage.items():
            if count == 0 and (factor.startswith("value_") or factor.startswith("quality_")):
                omitted_factors = [item for item in omitted_factors if not item.startswith(f"{factor}:")]
                omitted_factors.append(f"{factor}: no available point-in-time fundamentals")
        diagnostics = {
            "as_of": as_of.isoformat(),
            "rebalance_key": self.rebalance_key(session),
            "rebalance_frequency": self.config.rebalance_frequency,
            "regime": regime,
            "direction_mode": self.config.direction_mode,
            "factor_weight_mode": self.config.factor_weight_mode,
            "factor_weight_source": (
                "ridge_walk_forward" if self.config.factor_weight_mode == "ridge_walk_forward"
                and learned_weights_status == "loaded"
                else self.config.factor_weight_mode + "_scores" if is_tree_model and learned_weights_status == "loaded"
                else "unavailable" if self.config.factor_weight_mode != "static"
                else "static"
            ),
            "factor_model": factor_model_diagnostics or {},
            "daily_only": True,
            "universe_count": len(symbols),
            "feature_count": len(features),
            "ranked_count": len(ranked),
            "minimum_universe_required": self.config.min_universe,
            "usable_factor_count": usable_factor_count,
            "model_feature_coverage": model_feature_coverage,
            "minimum_model_feature_coverage": minimum_model_feature_coverage if is_tree_model else None,
            "selected_factors": selected_factors,
            "active_factor_weights": factor_weights if not is_tree_model else None,
            "active_factor_features": sorted(factor_weights),
            "model_feature_importance": (
                (learned_factor_model or {}).get("feature_importance") if is_tree_model else None
            ),
            "factor_timing_status": (
                "learned_walk_forward" if self.config.factor_weight_mode != "static"
                else "regime_override" if regime_override else "static_weights"
            ),
            "factor_coverage": factor_coverage,
            "missing_factor_value_policy": "neutral_zero_cross_sectional_zscore",
            "omitted_factors": omitted_factors,
            "fundamentals_feed": fundamentals_feed_diagnostics or {
                "status": "supplied" if fundamentals_by_symbol else "not_requested",
                "source": "caller_supplied_point_in_time_rows" if fundamentals_by_symbol else None,
            },
            "factor_neutralization": {
                "configured": self.config.factor_neutralization,
                "applied": bool(groups_by_symbol) and self.config.factor_neutralization == "sector",
                "group_count": len(group_sizes),
                "neutralized_group_count": sum(size >= 2 for size in group_sizes.values()),
                "singleton_group_count": sum(size == 1 for size in group_sizes.values()),
                "group_map_source": "fixed_universe_sector_map" if groups_by_symbol else "unavailable",
                "historical_membership_point_in_time": False if groups_by_symbol else None,
            },
            "tradability": mask_diagnostics,
            "minimum_universe_met": len(ranked) >= self.config.min_universe,
            "skip_reason": (
                None if len(ranked) >= self.config.min_universe else
                "learned_model_features_below_minimum_coverage" if model_features_below_minimum else
                "no_factor_meets_minimum_coverage" if usable_factor_count == 0 else
                "ranked_universe_below_minimum"
            ),
            "turnover": None,
        }
        if len(ranked) < self.config.min_universe:
            LOG.debug(
                "Multi-factor snapshot skipped (%s): %d ranked names from %d features; need %d; %d usable factors",
                diagnostics["skip_reason"], len(ranked), len(features),
                self.config.min_universe, usable_factor_count,
            )
            return [], diagnostics

        volatile_disabled = regime == "volatile" and self.config.disable_in_volatile
        long_candidates = [
            row for row in ranked[:self.config.top_n]
            if row["score"] > self.config.minimum_score
        ]
        bearish = regime in {"trending_down", "bearish", "risk_off"}
        short_candidates = [
            row for row in ranked[-self.config.top_n:]
            if row["score"] < -self.config.minimum_score
        ]
        if volatile_disabled:
            selected_longs, selected_shorts = [], []
        elif self.config.direction_mode == "long_only":
            selected_longs, selected_shorts = long_candidates, []
        elif self.config.direction_mode == "long_short":
            selected_longs, selected_shorts = long_candidates, short_candidates
        elif bearish:
            selected_longs, selected_shorts = [], short_candidates
        else:
            selected_longs, selected_shorts = long_candidates, []

        long_targets = {row["symbol"] for row in selected_longs}
        short_targets = {row["symbol"] for row in selected_shorts} - long_targets
        if isinstance(held_positions, dict):
            holdings = {str(symbol).upper(): dict(value) for symbol, value in held_positions.items()}
        else:
            holdings = {str(symbol).upper(): {"qty": 1} for symbol in (held_positions or set())}
        held = set(holdings)
        target_weights = {
            symbol: self.config.max_position_pct
            for symbol in long_targets | short_targets
        }
        portfolio_diagnostics = {"method": "equal", "status": "not_requested"}
        if self.config.portfolio_weight_method == "shrinkage_min_variance" and target_weights:
            long_weights, long_status = self._minimum_variance_weights(
                sorted(long_targets), bars_by_symbol, as_of,
            )
            short_weights, short_status = self._minimum_variance_weights(
                sorted(short_targets), bars_by_symbol, as_of,
            )
            target_weights = {**long_weights, **short_weights}
            active_statuses = [
                status for symbols_for_side, status in (
                    (long_targets, long_status), (short_targets, short_status),
                ) if symbols_for_side
            ]
            portfolio_diagnostics = {
                "method": "shrinkage_min_variance",
                "status": "ok" if all(status in {"ok", "single_name_equal_weight"} for status in active_statuses)
                else "equal_weight_fallback",
                "long_covariance_status": long_status,
                "short_covariance_status": short_status,
                "shrinkage": self.config.covariance_shrinkage,
                "lookback_sessions": self.config.covariance_lookback_sessions,
                "sleeve_budget_fraction_of_equal_weight_caps": 0.8,
            }
        diagnostics["portfolio_construction"] = portfolio_diagnostics
        diagnostics.update({
            "selected_long_count": len(long_targets),
            "selected_short_count": len(short_targets),
            "volatile_regime_disabled": volatile_disabled,
        })
        result = []
        rows_by_symbol = {row["symbol"]: row for row in ranked}
        for row in ranked:
            symbol = row["symbol"]
            desired_direction = "long" if symbol in long_targets else "short" if symbol in short_targets else "flat"
            if symbol in held:
                holding = holdings[symbol]
                quantity = float(holding.get("qty", 1) or 0)
                current_direction = "short" if quantity < 0 else "long"
                held_for = held_sessions(clean_daily_bars(bars_by_symbol.get(symbol, [])), holding.get("entry_at"))
                if held_for is not None and held_for >= self.config.maximum_holding_sessions:
                    action, direction = ("cover" if quantity < 0 else "sell"), "flat"
                elif desired_direction == current_direction or (
                    held_for is not None and held_for < self.config.minimum_hold_sessions
                ):
                    action, direction = "hold", current_direction
                else:
                    action, direction = ("cover" if quantity < 0 else "sell"), "flat"
            elif desired_direction == "long":
                action, direction = "buy", "long"
            elif desired_direction == "short":
                action, direction = "short", "short"
            elif symbol in held:
                action, direction = ("cover" if float(holdings[symbol].get("qty", 1) or 0) < 0 else "sell"), "flat"
            else:
                action, direction = "none", "flat"
            result.append(MultiFactorSignal(
                ticker=symbol, strategy="multi_factor", action=action, direction=direction,
                score=row["score"], rank=row["rank"],
                target_weight=target_weights.get(symbol, 0.0),
                as_of=as_of.isoformat(), rebalance_key=diagnostics["rebalance_key"],
                factor_values=row["factor_values"], factor_zscores=row["factor_zscores"],
                reason=(
                    f"{self.config.rebalance_frequency} factor {direction} candidate: "
                    f"rank {row['rank']}/{len(ranked)}, score {row['score']:.3f}, regime {regime}"
                ),
                stop_pct=self.config.hard_stop_pct,
                risk_multiplier=self.config.risk_multiplier,
            ))
        for symbol in sorted(held - set(rows_by_symbol)):
            holding = holdings[symbol]
            quantity = float(holding.get("qty", 1) or 0)
            held_for = held_sessions(clean_daily_bars(bars_by_symbol.get(symbol, [])), holding.get("entry_at"))
            if held_for is not None and held_for < self.config.minimum_hold_sessions:
                action = "hold"
                direction = "short" if quantity < 0 else "long"
            else:
                action = "cover" if quantity < 0 else "sell"
                direction = "flat"
            result.append(MultiFactorSignal(
                ticker=symbol, strategy="multi_factor", action=action, direction=direction,
                score=0.0, rank=len(ranked) + 1, target_weight=0.0,
                as_of=as_of.isoformat(), rebalance_key=diagnostics["rebalance_key"],
                factor_values={}, factor_zscores={},
                reason="held name is no longer eligible for the factor universe",
                stop_pct=self.config.hard_stop_pct,
                risk_multiplier=self.config.risk_multiplier,
            ))
        LOG.debug(
            "Multi-factor snapshot %s: ranked=%d targets=%d factors=%s",
            diagnostics["rebalance_key"], len(ranked), len(long_targets) + len(short_targets),
            diagnostics["selected_factors"],
        )
        return result, diagnostics

    def _minimum_variance_weights(
        self,
        selected_symbols: list[str],
        bars_by_symbol: dict[str, list[dict]],
        as_of: date,
    ) -> tuple[dict[str, float], str]:
        """Use a diagonal-shrunk covariance estimate for a bounded long-only sleeve."""
        if not selected_symbols:
            return {}, "empty_sleeve"
        equal = self.config.max_position_pct
        fallback = {symbol: equal for symbol in selected_symbols}
        if len(selected_symbols) == 1:
            return fallback, "single_name_equal_weight"
        closes_by_symbol = {}
        for symbol in selected_symbols:
            dated = {}
            for bar in bars_by_symbol.get(symbol, []):
                try:
                    session = date.fromisoformat(str(bar.get("date", bar.get("t")))[:10])
                    close = float(bar["c"])
                except (TypeError, ValueError, KeyError):
                    continue
                if session <= as_of and math.isfinite(close) and close > 0:
                    dated[session] = close
            closes_by_symbol[symbol] = dated
        common_dates = sorted(set.intersection(*(set(values) for values in closes_by_symbol.values())))
        common_dates = common_dates[-(self.config.covariance_lookback_sessions + 1):]
        if len(common_dates) < 21:
            return fallback, "insufficient_history"
        returns = np.asarray([
            [
                closes_by_symbol[symbol][current] / closes_by_symbol[symbol][previous] - 1
                for symbol in selected_symbols
            ]
            for previous, current in zip(common_dates, common_dates[1:])
        ], dtype=float)
        if not np.isfinite(returns).all():
            return fallback, "invalid_returns"
        covariance = np.atleast_2d(np.cov(returns, rowvar=False, ddof=1))
        diagonal = np.diag(np.diag(covariance))
        shrunk = (
            (1 - self.config.covariance_shrinkage) * covariance
            + self.config.covariance_shrinkage * diagonal
        )
        shrunk += np.eye(len(selected_symbols)) * 1e-10
        try:
            raw_weights = np.linalg.pinv(shrunk) @ np.ones(len(selected_symbols))
        except np.linalg.LinAlgError:
            return fallback, "covariance_solve_failed"
        raw_weights = np.maximum(raw_weights, 0)
        if not np.isfinite(raw_weights).all() or raw_weights.sum() <= 0:
            return fallback, "invalid_solution"
        # Keep gross exposure below the equal-weight cap budget so optimized
        # names can receive different weights without violating per-name caps.
        budget = len(selected_symbols) * equal * 0.8
        capped = np.zeros(len(selected_symbols), dtype=float)
        # A zero-weight name cannot receive any of the remaining budget. Keep
        # it out of the active set so the capped redistribution never divides
        # by zero after all positive-weight names have hit their cap.
        active = raw_weights > 0
        remaining = budget
        for _ in range(len(selected_symbols) + 1):
            if not active.any() or remaining <= 1e-12:
                break
            active_weights = raw_weights[active]
            active_weight_sum = float(active_weights.sum())
            if not math.isfinite(active_weight_sum) or active_weight_sum <= 1e-12:
                break
            proposed = remaining * active_weights / active_weight_sum
            active_indices = np.flatnonzero(active)
            over = proposed > self.config.max_position_pct
            if not over.any():
                capped[active_indices] = proposed
                break
            capped[active_indices[over]] = self.config.max_position_pct
            active[active_indices[over]] = False
            remaining = budget - capped.sum()
        if not np.isfinite(capped).all() or capped.sum() <= 0:
            return fallback, "invalid_solution"
        return dict(zip(selected_symbols, capped.tolist())), "ok"

    def apply_intraday_size_tilt(
        self, signals: list[MultiFactorSignal], intraday_bars: dict[str, list[dict]],
    ) -> list[MultiFactorSignal]:
        """Optionally trim daily-ranked positions on a contrary short-term move.

        It never creates or removes a ticker target; an adverse three-bar move
        can only halve the already ranked proposal's target weight.
        """
        if not self.config.enable_intraday_tilt:
            return signals
        adjusted = []
        for signal in signals:
            bars = intraday_bars.get(signal.ticker, [])
            if signal.action not in {"buy", "short"} or len(bars) < 3:
                adjusted.append(signal)
                continue
            try:
                first, last = float(bars[-3]["c"]), float(bars[-1]["c"])
            except (KeyError, TypeError, ValueError):
                adjusted.append(signal)
                continue
            if first <= 0 or not math.isfinite(first) or not math.isfinite(last):
                adjusted.append(signal)
                continue
            move = last / first - 1
            adverse = move < -0.003 if signal.action == "buy" else move > 0.003
            adjusted.append(
                replace(signal, target_weight=signal.target_weight * 0.5)
                if adverse else signal
            )
        return adjusted
