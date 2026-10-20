"""Portfolio replay state and construction."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime
import math

from .config import validate_research_controls, validate_strategy_filter
from .models import Position
from .execution import ReplayExecutionMixin
try:
    from ._signals_cy import ReplaySignalsMixin as _CompiledSignalsMixin
    from .signals import ReplaySignalsMixin as _PythonSignalsMixin

    class ReplaySignalsMixin(_CompiledSignalsMixin):
        """Keep compiled signal evaluation while overriding changed routing."""

        _multi_factor_rebalance = _PythonSignalsMixin._multi_factor_rebalance
        _multi_factor_ic = _PythonSignalsMixin._multi_factor_ic
        _regime = _PythonSignalsMixin._regime
        _queue_signal = _PythonSignalsMixin._queue_signal
except ImportError:
    from .signals import ReplaySignalsMixin
from .reporting import ReplayReportingMixin
from .replay import ReplayLoopMixin
from strategies.event_driven import (
    EventCalendar, EventSignalGenerator, EventStrategyConfig,
)
from strategies.multi_factor.config import MultiFactorConfig
from strategies.multi_factor.features import aggregate_intraday_bars
from strategies.multi_factor.signals import SignalGenerator
from strategies.portfolio_coordinator import (
    PortfolioCoordinator, PortfolioCoordinatorConfig,
)
from strategies.regime import _cached_feature_history, rank_strategies
from portfolio.config import ALGO_REGIME_CONFIG
from strategies.registry import ALGO_DEFAULT_PARAMETERS, validate_parameter_profile
from strategies.rule_pipeline import evaluate_rule_signal
from strategies.satellite_overlay import SatelliteOverlayConfig
from strategies.swing_common import CachedDailyBarHistory
from portfolio.config import ALGO_FACTOR_SECTOR_BY_SYMBOL, get_algo_runtime_settings, get_mode_config
from execution.risk import _get_universe_maps, validate_trade


class PortfolioBacktest(ReplayExecutionMixin, ReplaySignalsMixin, ReplayReportingMixin, ReplayLoopMixin):
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
        regime_profiles: dict[str, dict[str, dict]] | None = None,
        event_calendar: EventCalendar | None = None,
        spread_bps: float = 3.0,
        commission_per_share: float = 0.005,
        short_borrow_annual_pct: float = 0.03,
        unknown_borrow_penalty_bps: float = 25.0,
        borrow_availability: dict[tuple[str, str], bool] | None = None,
        coordinator_config: PortfolioCoordinatorConfig | None = None,
        force_close_at_end: bool = False,
        research_runtime_overrides: dict[str, bool] | None = None,
        preaggregated_daily_bars: dict[str, list[dict]] | None = None,
        precomputed_timeline: dict[datetime, dict[str, dict]] | None = None,
        include_extended_hours: bool = False,
        regime_context_bars: dict[str, list[dict]] | None = None,
        regime_macro_series: dict | None = None,
        regime_config: dict | None = None,
        execution_delay_bars: int = 0,
        factor_weight_timeline: list[dict] | None = None,
        _signal_evaluator=None,
        _trade_validator=None,
        _regime_ranker=None,
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
        self._signal_evaluator = _signal_evaluator or evaluate_rule_signal
        self._trade_validator = _trade_validator or validate_trade
        self._regime_ranker = _regime_ranker or rank_strategies
        self.symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        self.intraday_bars = intraday_bars
        self.precomputed_timeline = precomputed_timeline
        self.factor_weight_timeline = list(factor_weight_timeline or [])
        self.include_extended_hours = bool(include_extended_hours)
        if (
            isinstance(execution_delay_bars, bool)
            or not isinstance(execution_delay_bars, int)
            or execution_delay_bars < 0
            or execution_delay_bars > 12
        ):
            raise ValueError("execution_delay_bars must be an integer from 0 to 12")
        self.execution_delay_bars = execution_delay_bars
        self.spy_daily_bars = spy_daily_bars
        self.regime_config = {**ALGO_REGIME_CONFIG, **(regime_config or {})}
        self.regime_macro_series = regime_macro_series or {}
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
        self.profile = validate_parameter_profile(profile or {})
        self.regime_profiles: dict[str, dict[str, dict]] = {}
        for strategy, by_regime in (regime_profiles or {}).items():
            if not isinstance(by_regime, dict):
                raise ValueError(f"{strategy} regime profiles must be an object")
            for regime, values in by_regime.items():
                normalized = validate_parameter_profile({strategy: values})[strategy]
                self.regime_profiles.setdefault(strategy, {})[regime] = normalized
        self.strategy_filter = validate_strategy_filter(strategies)
        self.borrow_availability = {
            (str(ticker).upper(), str(day)): bool(available)
            for (ticker, day), available in (borrow_availability or {}).items()
        }
        self.borrow_coverage: Counter[str] = Counter()
        self.borrow_cost_total = 0.0
        self.commission_total = 0.0
        coordinator_config = coordinator_config or PortfolioCoordinatorConfig.from_dict()
        self.coordinator = PortfolioCoordinator(coordinator_config)
        self.force_close_at_end = bool(force_close_at_end)
        self.risk = deepcopy(get_mode_config("algo")["risk"])
        self.research_runtime_overrides = dict(research_runtime_overrides or {})
        unknown_runtime_overrides = set(self.research_runtime_overrides) - {
            "intraday_trend", "intraday_momentum",
        }
        if unknown_runtime_overrides or any(
            not isinstance(value, bool) for value in self.research_runtime_overrides.values()
        ):
            raise ValueError("research runtime overrides must be boolean intraday strategy enable flags")
        runtime_settings = get_algo_runtime_settings()
        self.runtime_settings = runtime_settings
        profile_satellites = self.profile.get("satellite_overlay", {})
        self.satellite_config = SatelliteOverlayConfig.from_dict({
            **profile_satellites,
            "multi_factor_enabled": runtime_settings.algo_multi_factor_enabled
            and bool(profile_satellites.get("multi_factor_enabled", True)),
            "event_driven_enabled": runtime_settings.algo_event_driven_enabled
            and bool(profile_satellites.get("event_driven_enabled", True)),
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
        self.multi_factor_generator = SignalGenerator(MultiFactorConfig.from_dict(
            self.parameters_for_regime("multi_factor", "unknown")
        ))
        self.multi_factor_config_key = self.multi_factor_generator.config.to_dict()
        self.multi_factor_fundamentals_cache: dict[str, tuple[dict, dict]] = {}
        all_daily_bars = (
            preaggregated_daily_bars
            if preaggregated_daily_bars is not None
            else aggregate_intraday_bars(intraday_bars)
        )
        self.regime_context_bars = dict(all_daily_bars)
        self.regime_context_bars.update(regime_context_bars or {})
        self.regime_context_bars.setdefault("SPY", spy_daily_bars)
        self.regime_feature_history = (
            _cached_feature_history(
                spy_daily_bars, self.regime_context_bars, self.regime_macro_series,
            )
            if self.regime_config.get("engine", "legacy") != "legacy"
            or self.regime_config.get("include_models", False)
            else []
        )
        self.regime_history: list[dict] = []
        self.previous_closes_by_day: dict[tuple[str, date], float] = {}
        for symbol, rows in all_daily_bars.items():
            for index, row in enumerate(rows):
                if index:
                    self.previous_closes_by_day[(symbol, date.fromisoformat(row["date"]))] = float(rows[index - 1]["c"])
        # Swing trend, momentum, cyclical mean reversion, and Multi Factor all
        # consume the same point-in-time daily aggregates. Each caller filters
        # strictly through the last completed session.
        # Swing signals revisit these fixed histories each session, so cache
        # their normalized rows once per symbol.
        self.daily_stock_bars = {
            symbol: CachedDailyBarHistory.from_bars(rows)
            for symbol, rows in all_daily_bars.items()
        }
        # Daily strategy history grows once per session, then is reused across
        # all 5-minute events that session. This avoids rebuilding a years-long
        # history for every symbol on every intraday bar.
        self._daily_history_cache_day: date | None = None
        self._daily_history_cache: dict[str, list[dict]] = {
            symbol: CachedDailyBarHistory() for symbol in all_daily_bars
        }
        self._daily_history_indices: dict[str, int] = {
            symbol: 0 for symbol in all_daily_bars
        }
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
        self.sector_by_symbol = {
            symbol: ALGO_FACTOR_SECTOR_BY_SYMBOL[symbol]
            for symbol in self.symbols if symbol in ALGO_FACTOR_SECTOR_BY_SYMBOL
        }

    def parameters_for_regime(self, strategy: str, regime: str) -> dict:
        """Return the current strategy profile with an optional regime override."""
        values = {
            **ALGO_DEFAULT_PARAMETERS[strategy],
            **(self.profile.get(strategy) or {}),
        }
        return {
            **values,
            **((self.regime_profiles.get(strategy) or {}).get(regime) or {}),
        }
