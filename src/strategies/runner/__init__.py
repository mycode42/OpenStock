"""Alpaca scanner shared by Swing/Day reports and the continuous Algo portfolio.

Examples:
  poetry run python -m src.strategies.runner --mode swing
  poetry run python -m src.strategies.runner --mode day --execute --dry-run
  poetry run python -m src.strategies.runner --mode swing --execute
The long-lived strategy portfolio is hosted by src.algo.daemon --mode algo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from src.config import STRATEGY_CONFIG, get_mode_config
from src.strategies import pairs
from src.algo.config import ALGO_SIGNAL_DATA_CONFIG
from . import data as _runner_data
from . import factors as _runner_factors
from . import sizing as _runner_sizing
from . import scan as _runner_scan
from src.strategies.regime import rank_strategies
from src.strategies.multi_factor import MultiFactorConfig, MultiFactorSignal, SignalGenerator
from src.strategies.regime_advisor import apply_regime_advisory
from src.market_data import feeds_for_session, historical_delay_seconds, regular_feed
from src.strategies.registry import ALGO_DEFAULT_PARAMETERS, ALGO_RULE_MODULES, RULE_MODULES, evaluate as evaluate_strategy
from src.strategies.rule_pipeline import evaluate_rule_signal
from src.strategies.event_driven import EventCalendar, EventSignalGenerator, EventStrategyConfig
from src.strategies.state_machine import RuleStrategyStateMachine
from src.strategies.universe import configured_universe, load_algo_universe
from src.strategies.momentum.swing import generate_signals as generate_swing_momentum_signals
from src.strategies.satellite_overlay import (
    CORE_STRATEGIES, SATELLITE_STRATEGIES, SatelliteOverlayConfig,
    apply_satellite_confirmations,
)
from src.strategies.portfolio_coordinator import (
    INTRADAY_STRATEGIES, PortfolioCoordinator, PortfolioCoordinatorConfig,
    choose_priority, strategy_priority, throttle_signal,
)


LOG = logging.getLogger("strategy_runner")
ET = ZoneInfo("America/New_York")
PAIR_DATA_SYMBOLS = sorted({symbol for pair in pairs.DEFAULT_PAIRS for symbol in pair})
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
_multi_factor_satellite_signals = _runner_factors._multi_factor_satellite_signals


def _current_portfolio(mode: str) -> tuple[dict, dict[str, dict], list[dict]]:
    """Keep broker request monkeypatching compatible with the former module."""
    return _runner_data.current_portfolio(mode, request_fn=_request)


def _fetch_bars(*args, **kwargs):
    """Keep request injection visible to existing scanner callers and tests."""
    return _runner_data.fetch_bars(*args, request_fn=_request, **kwargs)


def _fetch_latest_quotes(*args, **kwargs):
    return _runner_data.fetch_latest_quotes(*args, request_fn=_request, **kwargs)


def execute_trades(trades: list[dict], *, mode: str, dry_run: bool = False) -> dict:
    repo_root = Path(__file__).resolve().parents[3]
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
        _dependencies=globals(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan the configured universe with rule-based strategies")
    parser.add_argument("--mode", choices=["swing", "day"], default="swing")
    parser.add_argument("--execute", action="store_true", help="Submit generated trades through execute_trades.py")
    parser.add_argument("--dry-run", action="store_true", help="Validate generated trades without sending orders")
    args = parser.parse_args()
    if args.dry_run and not args.execute:
        parser.error("--dry-run requires --execute")

    result = scan(args.mode)
    if args.execute and result["trades"]:
        result["execution"] = execute_trades(result["trades"], mode=args.mode, dry_run=args.dry_run)
    elif args.execute:
        result["execution"] = {"status": "no_trades", "results": []}
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [strategy_runner] %(levelname)s %(message)s")
    sys.exit(main())
