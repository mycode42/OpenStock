"""Shared deterministic portfolio constraints applied before risk_manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import math
from typing import Mapping
from zoneinfo import ZoneInfo

from src.algo.config import ALGO_PORTFOLIO_COORDINATOR_CONFIG
from src.config import STRATEGY_CONFIG, get_mode_config


LOG = logging.getLogger("strategy_runner")
ET = ZoneInfo("America/New_York")

INTRADAY_STRATEGIES = frozenset({
    "mean_reversion", "opening_range_breakout", "intraday_trend", "intraday_momentum",
})
SWING_STRATEGIES = frozenset({
    "trend_following", "momentum", "swing_mean_reversion", "multi_factor", "event_driven",
})
SWING_CORE_STRATEGIES = frozenset({"trend_following", "momentum", "swing_mean_reversion", "multi_factor"})
RESEARCH_INTRADAY_STRATEGIES = frozenset({
    *INTRADAY_STRATEGIES, "trend_following", "momentum",
})
RESEARCH_SWING_CORE_STRATEGIES = frozenset({
    "swing_trend_following", "swing_momentum", "swing_mean_reversion", "multi_factor",
})
RESEARCH_SWING_STRATEGIES = frozenset({
    *RESEARCH_SWING_CORE_STRATEGIES, "event_driven",
})
SATELLITE_STRATEGIES = frozenset({"event_driven"})


@dataclass(frozen=True)
class PortfolioCoordinatorConfig:
    """Portfolio-level ceilings; risk_manager remains the final authority."""

    max_name_pct: float = ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_name_pct"]
    max_gross_pct: float = ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_gross_pct"]
    max_net_long_pct: float = ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_net_long_pct"]
    max_net_short_pct: float = ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_net_short_pct"]
    max_daily_entries: int = ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_daily_entries"]
    max_intraday_entries: int = ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_intraday_entries"]
    max_swing_entries: int = ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_swing_entries"]
    swing_open_delay_minutes: int = ALGO_PORTFOLIO_COORDINATOR_CONFIG["swing_open_delay_minutes"]
    intraday_close_cutoff_minutes: int = ALGO_PORTFOLIO_COORDINATOR_CONFIG["intraday_close_cutoff_minutes"]
    allow_opposite_netting: bool = ALGO_PORTFOLIO_COORDINATOR_CONFIG["allow_opposite_netting"]
    allow_intraday_overnight: bool = ALGO_PORTFOLIO_COORDINATOR_CONFIG["allow_intraday_overnight"]
    regime_throttle_enabled: bool = ALGO_PORTFOLIO_COORDINATOR_CONFIG["regime_throttle_enabled"]
    sector_caps: Mapping[str, float] = field(default_factory=lambda: dict(ALGO_PORTFOLIO_COORDINATOR_CONFIG["sector_caps"]))
    intraday_strategy_ids: tuple[str, ...] = tuple(INTRADAY_STRATEGIES)
    swing_strategy_ids: tuple[str, ...] = tuple(SWING_STRATEGIES)
    swing_core_strategy_ids: tuple[str, ...] = tuple(SWING_CORE_STRATEGIES)
    swing_trend_strategy_ids: tuple[str, ...] = ("trend_following",)

    @classmethod
    def from_dict(cls, values: Mapping | None = None) -> "PortfolioCoordinatorConfig":
        supplied = dict(ALGO_PORTFOLIO_COORDINATOR_CONFIG if values is None else values)
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unsupported portfolio coordinator setting(s): {', '.join(sorted(unknown))}")
        return cls(**supplied)

    def __post_init__(self) -> None:
        limits = (self.max_name_pct, self.max_gross_pct, self.max_net_long_pct, self.max_net_short_pct)
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or value > 1
            for value in limits
        ):
            raise ValueError("coordinator exposure limits must be finite fractions in (0, 1]")
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in (
                self.max_daily_entries, self.max_intraday_entries, self.max_swing_entries,
            ))
            or self.max_daily_entries < 1
            or not 0 <= self.max_intraday_entries <= self.max_daily_entries
            or not 0 <= self.max_swing_entries <= self.max_daily_entries
        ):
            raise ValueError("coordinator entry limits are invalid")
        if (
            isinstance(self.swing_open_delay_minutes, bool) or not isinstance(self.swing_open_delay_minutes, int)
            or isinstance(self.intraday_close_cutoff_minutes, bool) or not isinstance(self.intraday_close_cutoff_minutes, int)
            or self.swing_open_delay_minutes < 0 or self.intraday_close_cutoff_minutes < 0
        ):
            raise ValueError("coordinator session cutoffs must be non-negative")
        if not isinstance(self.allow_opposite_netting, bool) or not isinstance(self.allow_intraday_overnight, bool):
            raise ValueError("coordinator switches must be booleans")
        if not isinstance(self.regime_throttle_enabled, bool):
            raise ValueError("regime_throttle_enabled must be boolean")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 1
            for value in self.sector_caps.values()
        ):
            raise ValueError("sector caps must be finite fractions in (0, 1]")
        strategy_sets = (
            self.intraday_strategy_ids, self.swing_strategy_ids,
            self.swing_core_strategy_ids, self.swing_trend_strategy_ids,
        )
        if any(
            not isinstance(names, tuple)
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)
            for names in strategy_sets
        ):
            raise ValueError("coordinator strategy horizon identifiers are invalid")


@dataclass(frozen=True)
class CoordinationDecision:
    approved: bool
    proposal: dict | None
    reason: str | None = None
    reduced_from_qty: int | None = None


def strategy_priority(
    strategy: str, *, intraday_strategy_ids=None, swing_core_strategy_ids=None,
    swing_strategy_ids=None,
) -> int:
    """Lower numbers have precedence when proposals compete for shared capacity."""
    swing_core = SWING_CORE_STRATEGIES if swing_core_strategy_ids is None else swing_core_strategy_ids
    intraday = INTRADAY_STRATEGIES if intraday_strategy_ids is None else intraday_strategy_ids
    if strategy in swing_core:
        return 0
    if strategy in intraday:
        return 1
    satellites = SATELLITE_STRATEGIES if swing_strategy_ids is None else set(swing_strategy_ids) & SATELLITE_STRATEGIES
    if strategy in satellites:
        return 2
    return 3


def choose_priority(proposals: list[dict]) -> tuple[dict, list[dict]]:
    """Choose one directional proposal and annotate lower-priority competitors."""
    ordered = sorted(proposals, key=lambda item: (
        strategy_priority(str(item.get("strategy", ""))),
        not bool(item.get("software_protection")),
        -float(item.get("rank_score", 0) or 0),
        -float(item.get("strength", 0) or 0),
        str(item.get("ticker", "")),
    ))
    winner = ordered[0]
    losers = [
        {**item, "suppressed_reason": (
            f"portfolio coordinator: {winner.get('strategy')} has priority for "
            f"{winner.get('ticker')} ({winner.get('action')})"
        )}
        for item in ordered[1:]
    ]
    for loser in losers:
        LOG.warning("Portfolio coordinator rejected %s %s %s: %s",
                    loser.get("strategy"), loser.get("action"), loser.get("ticker"),
                    loser["suppressed_reason"])
    return winner, losers


def throttle_signal(
    signal: dict, regime: str, config: PortfolioCoordinatorConfig, *,
    swing_trend_strategy_ids=None,
) -> tuple[dict, str | None]:
    """Apply the two requested half-size regime throttles without changing signals."""
    if not config.regime_throttle_enabled:
        return signal, None
    strategy = str(signal.get("strategy", ""))
    strong_trend = regime in {"trending_up", "trending_down"}
    if strategy == "swing_mean_reversion" and strong_trend:
        reason = f"regime throttle: cyclical mean reversion halved in {regime}"
    elif strategy in (
        config.swing_trend_strategy_ids
        if swing_trend_strategy_ids is None else swing_trend_strategy_ids
    ) and regime == "range_bound":
        reason = "regime throttle: swing trend halved in range_bound"
    else:
        return signal, None
    adjusted = dict(signal)
    adjusted["risk_multiplier"] = float(adjusted.get("risk_multiplier", 1.0)) * 0.5
    return adjusted, reason


class CoordinatorBook:
    """Mutable scan-local exposure ledger used before every risk-manager call."""

    def __init__(
        self,
        config: PortfolioCoordinatorConfig,
        *,
        equity: float,
        positions: Mapping[str, dict] | None = None,
        position_owners: Mapping[str, str] | None = None,
        pending_buy_values: Mapping[str, float] | None = None,
        pending_short_values: Mapping[str, float] | None = None,
        entries_today: int = 0,
        intraday_entries_today: int = 0,
        swing_entries_today: int = 0,
        ticker_groups: Mapping[str, str] | None = None,
    ) -> None:
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("coordinator equity must be positive and finite")
        self.config = config
        self.equity = float(equity)
        self.ticker_groups = dict(ticker_groups or {})
        self.name_signed: dict[str, float] = {}
        self.name_owners: dict[str, str] = dict(position_owners or {})
        self.group_gross: dict[str, float] = {}
        self.pending_names: set[str] = set()
        self.entries_today = int(entries_today)
        self.intraday_entries_today = int(intraday_entries_today)
        self.swing_entries_today = int(swing_entries_today)
        for ticker, position in (positions or {}).items():
            qty = float(position.get("qty", 0) or 0)
            market_value = float(position.get("market_value") or 0)
            if not market_value and qty:
                price = float(position.get("current_price", position.get("avg_entry_price", 0)) or 0)
                market_value = abs(qty) * price
            raw_value = abs(market_value)
            value = raw_value if qty >= 0 else -raw_value
            self.name_signed[ticker.upper()] = value
            self._add_group_value(ticker.upper(), abs(value))
        for source, sign in ((pending_buy_values or {}, 1), (pending_short_values or {}, -1)):
            for ticker, value in source.items():
                amount = max(0.0, float(value))
                key = ticker.upper()
                if amount <= 0:
                    continue
                self.name_signed[key] = self.name_signed.get(key, 0.0) + sign * amount
                self._add_group_value(key, amount)
                self.pending_names.add(key)

    def _group(self, ticker: str) -> str:
        return self.ticker_groups.get(ticker.upper(), "other")

    def _add_group_value(self, ticker: str, value: float) -> None:
        group = self._group(ticker)
        self.group_gross[group] = self.group_gross.get(group, 0.0) + value

    def _reject(self, proposal: dict, reason: str) -> CoordinationDecision:
        LOG.warning("Portfolio coordinator rejected %s %s %s: %s",
                    proposal.get("strategy"), proposal.get("action"), proposal.get("ticker"), reason)
        return CoordinationDecision(False, None, reason)

    def review(
        self,
        proposal: dict,
        *,
        session: str | None,
        timestamp: datetime,
        regime: str = "unknown",
        session_open: datetime | None = None,
        session_close: datetime | None = None,
    ) -> CoordinationDecision:
        """Approve or reduce an already-sized entry. Exits pass through unchanged."""
        action = str(proposal.get("action", "")).lower()
        if action not in {"buy", "short"}:
            return CoordinationDecision(True, dict(proposal))
        ticker = str(proposal.get("ticker", "")).upper()
        strategy = str(proposal.get("strategy", ""))
        if strategy in SATELLITE_STRATEGIES:
            return self._reject(proposal, f"{strategy} is confirmation-only")
        qty = proposal.get("qty")
        price = proposal.get("entry_price")
        if not ticker or isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            return self._reject(proposal, "invalid ticker or quantity")
        try:
            price = float(price)
        except (TypeError, ValueError):
            return self._reject(proposal, "invalid reference price")
        if not math.isfinite(price) or price <= 0:
            return self._reject(proposal, "invalid reference price")

        local = timestamp.astimezone(ET) if timestamp.tzinfo else timestamp.replace(tzinfo=ET)
        session_name = session or "regular"
        if session_name == "overnight" and not STRATEGY_CONFIG.get("overnight_entries_enabled", False):
            return self._reject(proposal, "overnight entries are disabled by configuration")
        is_intraday = strategy in self.config.intraday_strategy_ids
        if is_intraday and session_name != "regular" and not self.config.allow_intraday_overnight:
            return self._reject(proposal, "intraday strategies are regular-session only")
        if is_intraday:
            close_at = session_close or local.replace(hour=16, minute=0, second=0, microsecond=0)
            if close_at.tzinfo is None:
                close_at = close_at.replace(tzinfo=ET)
            close_at = close_at.astimezone(ET)
            if local >= close_at - timedelta(minutes=self.config.intraday_close_cutoff_minutes):
                return self._reject(proposal, "intraday entry cutoff reached; position must have time to flatten")
        if strategy in self.config.swing_strategy_ids and session_name == "regular":
            open_at = session_open or local.replace(hour=9, minute=30, second=0, microsecond=0)
            if open_at.tzinfo is None:
                open_at = open_at.replace(tzinfo=ET)
            open_at = open_at.astimezone(ET)
            if (local - open_at).total_seconds() < self.config.swing_open_delay_minutes * 60:
                return self._reject(proposal, "swing entries are blocked during the first 15 minutes")

        entry_notional = qty * price
        direction = 1 if action == "buy" else -1
        current = self.name_signed.get(ticker, 0.0)
        opposing_existing = bool(current and current * direction < 0)
        if opposing_existing and not self.config.allow_opposite_netting:
            return self._reject(proposal, "opposite direction conflicts with an existing position or pending order")
        if ticker in self.pending_names:
            return self._reject(proposal, "ticker already has a pending entry")
        if (abs(current) > 0 and self.name_owners.get(ticker) not in (None, strategy)
                and not (opposing_existing and self.config.allow_opposite_netting)):
            return self._reject(proposal, "another strategy already owns this ticker")

        if self.entries_today >= self.config.max_daily_entries:
            return self._reject(proposal, "shared daily entry budget is exhausted")
        if is_intraday and self.intraday_entries_today >= self.config.max_intraday_entries:
            return self._reject(proposal, "shared intraday entry slice is exhausted")
        if strategy in self.config.swing_core_strategy_ids and self.swing_entries_today >= self.config.max_swing_entries:
            return self._reject(proposal, "shared swing entry slice is exhausted")

        gross = sum(abs(value) for value in self.name_signed.values())
        current_name_same_direction = max(0.0, current * direction)
        offset = abs(current) if opposing_existing and self.config.allow_opposite_netting else 0.0
        group = self._group(ticker)
        limits = [
            self.equity * self.config.max_name_pct - current_name_same_direction + offset,
            self.equity * self.config.max_gross_pct - gross + offset,
            self.equity * self.config.max_net_long_pct - max(0.0, sum(self.name_signed.values())) if direction > 0 else math.inf,
            self.equity * self.config.max_net_short_pct + min(0.0, sum(self.name_signed.values())) if direction < 0 else math.inf,
            self.equity * self.config.sector_caps.get(group, 0.25) - self.group_gross.get(group, 0.0) + offset,
        ]
        room = max(0.0, min(limits))
        allowed_qty = min(qty, int(room / price))
        if allowed_qty <= 0:
            return self._reject(proposal, "name, sector, gross, or net exposure cap leaves no capacity")

        approved = dict(proposal)
        approved["qty"] = allowed_qty
        if allowed_qty < qty:
            LOG.info("Portfolio coordinator reduced %s %s %s quantity %d -> %d",
                     strategy, action, ticker, qty, allowed_qty)
        amount = allowed_qty * price
        updated = current + direction * amount
        self.name_signed[ticker] = updated
        if opposing_existing and updated * current < 0:
            self.name_owners[ticker] = strategy
        elif opposing_existing and math.isclose(updated, 0.0, abs_tol=1e-8):
            self.name_owners.pop(ticker, None)
        else:
            self.name_owners.setdefault(ticker, strategy)
        self.group_gross[group] = max(
            0.0, self.group_gross.get(group, 0.0) + abs(updated) - abs(current),
        )
        self.pending_names.add(ticker)
        self.entries_today += 1
        if is_intraday:
            self.intraday_entries_today += 1
        if strategy in self.config.swing_core_strategy_ids:
            self.swing_entries_today += 1
        if strategy == "swing_mean_reversion" and regime in {"trending_up", "trending_down"}:
            approved["coordinator_regime_throttle"] = "half_size_in_strong_trend"
        elif strategy == "trend_following" and regime == "range_bound":
            approved["coordinator_regime_throttle"] = "half_size_in_range"
        return CoordinationDecision(True, approved, reduced_from_qty=qty if allowed_qty < qty else None)


class PortfolioCoordinator:
    """Constructs scan-local ledgers and exposes defaults for live and replay."""

    def __init__(self, config: PortfolioCoordinatorConfig | None = None) -> None:
        self.config = config or PortfolioCoordinatorConfig.from_dict()
        universe = get_mode_config("algo")["universe"]
        self.ticker_groups = {
            str(ticker).upper(): group
            for group, row in universe.items()
            for ticker in row.get("tickers", [])
        }

    def new_book(self, **state) -> CoordinatorBook:
        return CoordinatorBook(self.config, ticker_groups=self.ticker_groups, **state)


__all__ = [
    "CoordinatorBook", "CoordinationDecision", "PortfolioCoordinator",
    "PortfolioCoordinatorConfig", "choose_priority", "strategy_priority", "throttle_signal",
    "INTRADAY_STRATEGIES", "SWING_STRATEGIES", "SWING_CORE_STRATEGIES",
    "RESEARCH_INTRADAY_STRATEGIES", "RESEARCH_SWING_CORE_STRATEGIES",
    "RESEARCH_SWING_STRATEGIES",
]
