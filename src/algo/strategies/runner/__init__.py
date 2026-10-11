"""Internal strategy scanner used by the continuous Algo portfolio.

The live scanner receives Algo ownership and lifecycle state from
``src.algo.daemon``. Swing and Day analysis remains in Swarm Trader's agent
workflows and does not use this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import cast

from dotenv import load_dotenv

load_dotenv()

from src.config import STRATEGY_CONFIG, get_algo_runtime_settings, get_mode_config
from src.algo.config import ALGO_RUNTIME_CONFIG, ALGO_RUNTIME_SETTINGS, ALGO_SIGNAL_DATA_CONFIG
from . import data as _runner_data
from . import factors as _runner_factors
from . import sizing as _runner_sizing
from . import scan as _runner_scan
from .event_context import load_event_context
from src.algo.strategies.regime import rank_strategies
from src.algo.strategies.multi_factor import MultiFactorConfig, MultiFactorSignal, SignalGenerator
from src.algo.strategies.regime_advisor import apply_regime_advisory
from src.market_data import feeds_for_session, historical_delay_seconds, regular_feed
from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS, ALGO_RULE_MODULES, RULE_MODULES, evaluate as evaluate_strategy
from src.algo.strategies.rule_pipeline import evaluate_rule_signal
from src.algo.strategies.event_driven import EventCalendar, EventSignalGenerator, EventStrategyConfig
from src.algo.strategies.state_machine import ACTIVE_PHASES, RuleStrategyStateMachine
from src.algo.strategies.universe import configured_universe, load_algo_universe
from src.algo.strategies.momentum.swing import generate_signals as generate_swing_momentum_signals
from src.algo.strategies.satellite_overlay import (
    CORE_STRATEGIES, SATELLITE_STRATEGIES, SatelliteOverlayConfig,
    apply_satellite_confirmations,
)
from src.algo.strategies.catalog import STRATEGY_CATALOG
from src.algo.strategies.portfolio_coordinator import (
    INTRADAY_STRATEGIES, PortfolioCoordinator, PortfolioCoordinatorConfig,
    choose_priority, strategy_priority, throttle_signal,
)


LOG = logging.getLogger("strategy_runner")
ET = ZoneInfo("America/New_York")
# Free-plan BOATS historical data is available only when the requested end is
# at least 15 minutes old. Keep a small margin for clock differences.
MAX_SPY_DAILY_AGE_DAYS = ALGO_SIGNAL_DATA_CONFIG["max_spy_daily_age_days"]
DATA_BASE = _runner_data.DATA_BASE
TRADE_BASE = _runner_data.TRADE_BASE
_request = _runner_data._request
_completed_daily = _runner_data._completed_daily
_completed_intraday = _runner_data._completed_intraday
_latest_bar_age_seconds = _runner_data._latest_bar_age_seconds
_is_protective_sell_order = _runner_sizing._is_protective_sell_order
_positive_price_level = _runner_sizing._positive_price_level
_ticker_max_position_pct = _runner_sizing._ticker_max_position_pct
_make_trade = _runner_sizing._make_trade
_update_trailing_stop = _runner_sizing._update_trailing_stop
_multi_factor_rebalance_signals = _runner_factors._multi_factor_rebalance_signals
_multi_factor_current_targets = _runner_factors._multi_factor_current_targets
_multi_factor_parameters_for_session = _runner_factors.multi_factor_parameters_for_session


def _current_portfolio(mode: str) -> tuple[dict, dict[str, dict], list[dict]]:
    """Keep broker request monkeypatching compatible with the former module."""
    return _runner_data.current_portfolio(mode, request_fn=_request)


def _fetch_bars(*args, **kwargs):
    """Keep request injection visible to existing scanner callers and tests."""
    return _runner_data.fetch_bars(*args, request_fn=_request, **kwargs)


def _fetch_latest_quotes(*args, **kwargs):
    return _runner_data.fetch_latest_quotes(*args, request_fn=_request, **kwargs)


def execute_trades(trades: list[dict], *, mode: str, dry_run: bool = False) -> dict:
    repo_root = Path(__file__).resolve().parents[4]
    command = [sys.executable, str(repo_root / "execute_trades.py"), "--mode", mode]
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(
        command,
        input=json.dumps({"trades": trades}),
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"execute_trades.py exited {completed.returncode}: {completed.stderr[-1000:]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("execute_trades.py returned invalid JSON") from exc


def scan(
    mode: str = "swing",
    *,
    universe_result: dict | None = None,
    excluded_symbols: set[str] | None = None,
    position_owners: dict[str, str] | None = None,
    strategy_states: dict | None = None,
    session_name: str | None = None,
    trading_date: str | None = None,
    extended_hours: bool = False,
    regular_open: datetime | None = None,
    regular_close: datetime | None = None,
    data_feed: str | None = None,
    quote_feed: str | None = None,
    overnight_tradable_tickers: set[str] | None = None,
) -> dict:
    """Compatibility facade; dependencies remain patchable at this module."""
    return _runner_scan.scan(
        mode=mode,
        universe_result=universe_result,
        excluded_symbols=excluded_symbols,
        position_owners=position_owners,
        strategy_states=strategy_states,
        session_name=session_name,
        trading_date=trading_date,
        extended_hours=extended_hours,
        regular_open=regular_open,
        regular_close=regular_close,
        data_feed=data_feed,
        quote_feed=quote_feed,
        overnight_tradable_tickers=overnight_tradable_tickers,
        _dependencies=cast(_runner_scan.ScanDependencies, globals()),
    )


def main() -> int:
    print(
        "The Algo scanner requires coordinator-owned lifecycle state. "
        "Run the service with `python -m src.algo.daemon`; "
        "Swing and Day workflows use their Swarm Trader agents.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [strategy_runner] %(levelname)s %(message)s")
    sys.exit(main())
