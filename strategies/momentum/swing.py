"""Low-frequency cross-sectional momentum from completed daily returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import math
from statistics import mean

from src.algo.strategies.swing_common import SwingSignal, clean_daily_bars, held_sessions, position_entry_price


@dataclass(frozen=True)
class SwingMomentumConfig:
    enabled: bool = True
    direction_mode: str = "both"
    momentum_mode: str = "cross_sectional"
    lookbacks: tuple[int, ...] = (5, 10, 20)
    top_n: int = 3
    minimum_score_spread: float = 0.04
    minimum_time_series_return: float = 0.02
    minimum_average_dollar_volume: float = 10_000_000
    minimum_universe: int = 7
    minimum_hold_sessions: int = 3
    maximum_holding_sessions: int = 15
    hard_stop_pct: float = 0.08
    trailing_stop_pct: float = 0.06
    trailing_activation_pct: float = 0.05
    cooldown_days: int = 5
    max_positions: int = 7
    max_entries_per_day: int = 2
    max_position_pct: float = 0.04
    risk_multiplier: float = 0.75

    @classmethod
    def from_dict(cls, values: dict | None = None) -> "SwingMomentumConfig":
        if values is not None and not isinstance(values, dict):
            raise ValueError("momentum settings must be an object")
        supplied = dict(values or {})
        if "lookbacks" in supplied:
            supplied["lookbacks"] = tuple(supplied["lookbacks"])
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unsupported momentum setting(s): {', '.join(sorted(unknown))}")
        config = cls(**supplied)
        config.validate()
        return config

    def to_dict(self) -> dict:
        result = asdict(self)
        result["lookbacks"] = list(self.lookbacks)
        return result

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("momentum enabled must be a boolean")
        if self.direction_mode not in {"both", "long_only", "short_only"}:
            raise ValueError("momentum direction_mode must be both, long_only, or short_only")
        if self.momentum_mode not in {"cross_sectional", "time_series"}:
            raise ValueError("momentum momentum_mode must be cross_sectional or time_series")
        if not self.lookbacks or len(set(self.lookbacks)) != len(self.lookbacks):
            raise ValueError("momentum lookbacks must be unique daily periods")
        if any(isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 252 for value in self.lookbacks):
            raise ValueError("momentum lookbacks must be integers between 3 and 252 sessions")
        for name, low, high in (
            ("top_n", 1, 5), ("minimum_hold_sessions", 2, 5),
            ("minimum_universe", 5, 30),
            ("maximum_holding_sessions", 10, 20), ("cooldown_days", 3, 10),
            ("max_positions", 3, 7), ("max_entries_per_day", 1, 2),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"momentum {name} must be an integer between {low} and {high}")
        if self.maximum_holding_sessions <= self.minimum_hold_sessions:
            raise ValueError("momentum maximum holding period must exceed its minimum")
        for name, low, high in (
            ("minimum_score_spread", 0.01, 0.50),
            ("minimum_time_series_return", 0.0, 0.50),
            ("minimum_average_dollar_volume", 1_000_000, 1_000_000_000),
            ("hard_stop_pct", 0.04, 0.15), ("trailing_stop_pct", 0.03, 0.15),
            ("trailing_activation_pct", 0.01, 0.12), ("max_position_pct", 0.01, 0.05),
            ("risk_multiplier", 0.1, 1.0),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"momentum {name} must be between {low} and {high}")


def _as_of_rows(bars_by_symbol: dict[str, list[dict]], as_of: date) -> dict[str, list[tuple[date, dict, float]]]:
    result = {}
    for ticker, bars in bars_by_symbol.items():
        rows = [row for row in clean_daily_bars(bars) if row[0] <= as_of]
        if rows:
            result[ticker.upper()] = rows
    return result


def _score(rows: list[tuple[date, dict, float]], lookbacks: tuple[int, ...]) -> float | None:
    closes = [row[2] for row in rows]
    if len(closes) <= max(lookbacks):
        return None
    returns = [closes[-1] / closes[-1 - days] - 1 for days in lookbacks]
    return mean(returns)


def generate_signals(
    bars_by_symbol: dict[str, list[dict]], *, symbols: list[str], as_of: date,
    regime: str = "unknown", held_positions: dict[str, dict] | None = None,
    parameters: dict | None = None,
) -> tuple[dict[str, SwingSignal], dict]:
    """Rank a point-in-time universe and propose only top/bottom cross-sectional names."""
    config = SwingMomentumConfig.from_dict(parameters)
    if not config.enabled:
        return {}, {"enabled": False, "reason": "disabled"}
    rows_by_symbol = _as_of_rows(bars_by_symbol, as_of)
    scores: dict[str, float] = {}
    dollar_volume: dict[str, float] = {}
    for ticker in dict.fromkeys(str(symbol).upper() for symbol in symbols):
        rows = rows_by_symbol.get(ticker, [])
        score = _score(rows, config.lookbacks)
        if score is None:
            continue
        liquid_rows = rows[-20:]
        dv = []
        for _, bar, close in liquid_rows:
            try:
                volume = float(bar.get("v", 0) or 0)
            except (TypeError, ValueError):
                volume = 0
            if math.isfinite(volume) and volume > 0:
                dv.append(close * volume)
        average_dv = mean(dv) if len(dv) >= min(15, len(liquid_rows)) else 0.0
        if average_dv >= config.minimum_average_dollar_volume:
            scores[ticker] = score
            dollar_volume[ticker] = average_dv

    ranked = sorted(scores.items(), key=lambda item: (item[1], item[0]))
    spread = ranked[-1][1] - ranked[0][1] if len(ranked) >= 2 else 0.0
    minimum_universe = max(config.minimum_universe, 2 * config.top_n + 1)
    allow_long = config.direction_mode in {"both", "long_only"}
    allow_short = config.direction_mode in {"both", "short_only"}
    if config.momentum_mode == "cross_sectional":
        spread_ok = len(ranked) >= minimum_universe and spread >= config.minimum_score_spread
        selected_longs = {ticker for ticker, _ in ranked[-config.top_n:]} if spread_ok and allow_long else set()
        selected_shorts = {ticker for ticker, _ in ranked[:config.top_n]} if spread_ok and allow_short else set()
    else:
        positive = sorted(
            ((ticker, score) for ticker, score in ranked if score > 0 and score >= config.minimum_time_series_return),
            key=lambda item: (-item[1], item[0]),
        )
        negative = sorted(
            ((ticker, score) for ticker, score in ranked if score < 0 and score <= -config.minimum_time_series_return),
            key=lambda item: (item[1], item[0]),
        )
        universe_ready = len(ranked) >= minimum_universe
        selected_longs = {ticker for ticker, _ in positive[:config.top_n]} if universe_ready and allow_long else set()
        selected_shorts = {ticker for ticker, _ in negative[:config.top_n]} if universe_ready and allow_short else set()
        spread_ok = bool(universe_ready and (selected_longs or selected_shorts))

    signals: dict[str, SwingSignal] = {}
    held_positions = {str(key).upper(): value for key, value in (held_positions or {}).items()}
    for ticker, holding in held_positions.items():
        qty = float(holding.get("qty", 0) or 0)
        if qty == 0:
            continue
        rows = rows_by_symbol.get(ticker, [])
        if not rows:
            continue
        held = held_sessions(rows, holding.get("entry_at"))
        entry = position_entry_price(holding.get("entry_price"))
        if held is None or entry is None:
            continue
        is_short = qty < 0
        current = rows[-1][2]
        action = "cover" if is_short else "sell"
        stop = entry * (1 + config.hard_stop_pct) if is_short else entry * (1 - config.hard_stop_pct)
        if (current >= stop if is_short else current <= stop):
            reason = "daily momentum hard stop"
        else:
            closes = [row[2] for row in rows]
            post = closes[-held - 1:]
            favorable = (entry - min(post)) / entry if is_short else (max(post) - entry) / entry
            trail = None
            if favorable >= config.trailing_activation_pct:
                trail = min(post) * (1 + config.trailing_stop_pct) if is_short else max(post) * (1 - config.trailing_stop_pct)
            if held >= config.minimum_hold_sessions and trail is not None and (current >= trail if is_short else current <= trail):
                reason = "daily momentum trailing stop"
            elif held >= config.maximum_holding_sessions:
                reason = f"maximum {config.maximum_holding_sessions}-session momentum hold reached"
            elif held >= config.minimum_hold_sessions and ticker not in (selected_shorts if is_short else selected_longs):
                reason = "ticker left the daily cross-sectional momentum selection"
            else:
                continue
        signals[ticker] = SwingSignal(
            ticker, action, "momentum", f"{reason}; {held} sessions held",
            stop_pct=config.hard_stop_pct, target_weight=config.max_position_pct,
            risk_multiplier=config.risk_multiplier,
        )

    if spread_ok:
        for ticker in selected_longs:
            if ticker in held_positions:
                continue
            rank = sorted(scores, key=lambda name: (-scores[name], name)).index(ticker) + 1
            signals[ticker] = SwingSignal(
                ticker, "buy", "momentum",
                f"daily {config.momentum_mode.replace('_', ' ')} momentum long rank {rank}/{len(scores)}; blended return {scores[ticker]:+.1%}; top-bottom spread {spread:.1%}",
                stop_pct=config.hard_stop_pct, target_weight=config.max_position_pct,
                risk_multiplier=config.risk_multiplier,
            )
        for ticker in selected_shorts:
            if ticker in held_positions:
                continue
            rank = sorted(scores, key=lambda name: (scores[name], name)).index(ticker) + 1
            signals[ticker] = SwingSignal(
                ticker, "short", "momentum",
                f"daily {config.momentum_mode.replace('_', ' ')} momentum short rank {rank}/{len(scores)}; blended return {scores[ticker]:+.1%}; top-bottom spread {spread:.1%}",
                stop_pct=config.hard_stop_pct, target_weight=config.max_position_pct,
                risk_multiplier=config.risk_multiplier,
            )
    diagnostics = {
        "as_of": as_of.isoformat(), "eligible_symbols": len(scores),
        "minimum_universe": minimum_universe,
        "score_spread": spread, "minimum_score_spread": config.minimum_score_spread,
        "spread_passed": spread_ok, "longs": sorted(selected_longs),
        "shorts": sorted(selected_shorts), "regime": regime,
        "momentum_mode": config.momentum_mode,
        "lookbacks": list(config.lookbacks),
        "average_dollar_volume": dollar_volume,
    }
    return signals, diagnostics


__all__ = ["SwingMomentumConfig", "generate_signals"]
