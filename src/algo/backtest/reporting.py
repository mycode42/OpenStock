"""Replay reporting methods."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
import statistics

from .config import STRATEGY_NAMES


class ReplayReportingMixin:
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

