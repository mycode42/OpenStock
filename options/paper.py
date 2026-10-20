"""Independent Alpaca paper runner for the options sleeves.

Run ``python -m options.paper`` for a dry-run report. ``--submit`` enables
orders only at Alpaca's fixed paper endpoint. The Algo equity service remains a
separate process and account; it is queried read-only for combined risk.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import wraps
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from broker.algo import AlgoAlpaca
from broker.options import OptionsPaperClient
from options.config import load_options_config
from options.costs import OptionsCostModel
from options.data import AlpacaOptionDataProvider, OptionChain, parse_snapshot_quote
from options.journal import OptionsJournal
from options.models import GreeksSnapshot, OptionContract, OptionLeg
from options.assignment import simulate_early_assignment
from options.risk import (
    PortfolioRiskManager,
    PositionExposure,
    _safe_number,
    pnl_attribution,
    publish_risk_snapshot,
    summarize_portfolio,
)
from options.symbols import parse_occ_symbol
from portfolio.config import ALGO_SERVICE_CONFIG
from runtime.paths import DATA_DIR, ROOT
from strategies.event_driven.ai import AIEventSignalBook
from strategies.options.base import OptionsStrategyContext
from strategies.options.base import regime_risk_multiplier
from strategies.options.event_driven import EventDrivenOptionsStrategy
from strategies.options.vrp import VolatilityRiskPremiumStrategy


_OCC_LIKE = re.compile(r"^[A-Z0-9.]{1,6}\d{6}[CP]\d{8}$")
_US_EQUITY_TZ = ZoneInfo("America/New_York")


def _serialized_options_account(function):
    """Serialize paper scans by account so order sizing cannot race another run."""
    @wraps(function)
    def wrapped(*args, options_client=None, **kwargs):
        client = options_client or OptionsPaperClient()
        digest = hashlib.sha256(client.account_id.encode("utf-8")).hexdigest()[:16]
        lock_dir = DATA_DIR / "options" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{digest}.lock"
        with lock_path.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("an options paper cycle is already running for this account") from exc
            try:
                return function(*args, options_client=client, **kwargs)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return wrapped


def _state_regime() -> tuple[str, str]:
    path = ROOT / ALGO_SERVICE_CONFIG["state_file"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        portfolio = payload.get("strategy_portfolio", payload)
        market = portfolio.get("market_regime") or {}
        return str(market.get("regime") or "unknown"), str(market.get("volatility_state") or "unknown")
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return "unknown", "unknown"


def _load_ai_recommendations(tickers: set[str] | None = None) -> tuple[dict, dict]:
    book = AIEventSignalBook()
    results = {}
    for ticker in (tickers if tickers is not None else set(book.universe)):
        recommendation = book.recommendation(ticker, universe=set(book.universe))
        if recommendation is not None:
            results[ticker] = recommendation
    return results, book.diagnostics()


def _stock_position_exposures(client: OptionsPaperClient, positions: list[dict]) -> list[PositionExposure]:
    result = []
    for row in positions:
        qty = _safe_number(row.get("qty"))
        if qty == 0:
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or _OCC_LIKE.fullmatch(symbol):
            continue
        spot = _safe_number(row.get("current_price"))
        if spot <= 0:
            market_value = abs(_safe_number(row.get("market_value")))
            spot = market_value / abs(qty) if market_value > 0 else _safe_number(row.get("avg_entry_price"))
        if spot <= 0:
            spot = client.underlying_price(symbol)
        result.append(PositionExposure(
            underlying=symbol,
            spot=spot,
            delta_shares=qty,
            source="broker_equity_position",
        ))
    return result


def _option_position_exposures(
    client: OptionsPaperClient,
    positions: list[dict],
    *,
    as_of: datetime,
    feed: str,
    config: dict,
    maintenance_margin: float = 0.0,
) -> tuple[list[PositionExposure], list[dict], int, list[str]]:
    option_rows = [
        row for row in positions
        if _safe_number(row.get("qty")) != 0 and _OCC_LIKE.fullmatch(str(row.get("symbol") or "").upper())
    ]
    output, audit_rows, quality = [], [], []
    symbols = [str(row["symbol"]).upper() for row in option_rows]
    snapshots: dict = {}
    for offset in range(0, len(symbols), 100):
        snapshots.update(client.option_snapshots(symbols[offset:offset + 100], feed=feed))
    for row in option_rows:
        symbol = str(row["symbol"]).upper()
        quantity = _safe_number(row.get("qty"))
        if not float(quantity).is_integer():
            quality.append(f"fractional_option_position:{symbol}")
            continue
        try:
            raw_contract = client.contract(symbol)
            contract_row = raw_contract.get("option_contract", raw_contract)
            contract = OptionContract(
                symbol=symbol,
                underlying=str(contract_row.get("underlying_symbol") or parse_occ_symbol(symbol).underlying).upper(),
                expiration=datetime.fromisoformat(str(contract_row.get("expiration_date"))).date(),
                right=str(contract_row.get("type") or parse_occ_symbol(symbol).right).lower(),
                strike=_safe_number(contract_row.get("strike_price"), parse_occ_symbol(symbol).strike),
                multiplier=int(_safe_number(contract_row.get("size"), 100)),
                exercise_style=str(contract_row.get("style") or "american").lower(),
                tradable=bool(contract_row.get("tradable", True)),
            )
            spot = client.underlying_price(contract.underlying)
            quote = parse_snapshot_quote(
                contract, snapshots.get(symbol) or {}, spot=spot, as_of=as_of,
                feed=feed, rate=float(config["rate"]),
                dividend_yield=float(config["dividend_yield"]),
                max_quote_age_seconds=int(config["max_quote_age_seconds"]),
            )
            if quote is None:
                raise ValueError("option position has no fresh two-sided quote")
            scale = quantity * contract.multiplier
            assignment_shares = abs(int(quantity)) * contract.multiplier if quantity < 0 and contract.exercise_style == "american" else 0
            market_value = abs(_safe_number(row.get("market_value")))
            simulated_assignment = None
            if quantity < 0 and float(quantity).is_integer():
                simulated_assignment = simulate_early_assignment(
                    OptionLeg(contract, "sell", int(abs(quantity)), bid=quote.bid, ask=quote.ask, greeks=quote.greeks),
                    contracts=1,
                    underlying_price=spot,
                    as_of=as_of.date(),
                    expected_dividend_per_share=_safe_number((config.get("dividends") or {}).get(contract.underlying)),
                    extrinsic_threshold=float(config.get("assignment_extrinsic_threshold", 0.02)),
                )
            output.append(PositionExposure(
                underlying=contract.underlying,
                spot=spot,
                delta_shares=quote.greeks.delta * scale,
                gamma=quote.greeks.gamma * scale,
                theta_per_day=quote.greeks.theta * scale,
                vega_per_volatility=quote.greeks.vega * scale,
                # Individual short legs can be temporarily uncovered after early assignment.
                max_loss=market_value if quantity > 0 else 0.0,
                margin=0.0,
                assignment_shares=assignment_shares,
                source="broker_option_position",
            ))
            if simulated_assignment:
                output.append(PositionExposure(
                    underlying=contract.underlying,
                    spot=spot,
                    delta_shares=simulated_assignment.resulting_share_change,
                    source="simulated_early_assignment_scenario",
                ))
            audit_rows.append({
                "symbol": symbol,
                "underlying": contract.underlying,
                "qty": quantity,
                "market_value": _safe_number(row.get("market_value")),
                "bid": quote.bid,
                "ask": quote.ask,
                "midpoint": quote.midpoint,
                "greeks": quote.greeks,
                "multiplier": contract.multiplier,
                "spot": spot,
                "expiration": contract.expiration,
                "quote_at": quote.observed_at,
                "simulated_assignment": simulated_assignment,
            })
        except Exception as exc:
            quality.append(f"open_option_position_unpriced:{symbol}:{type(exc).__name__}")
    # Account-level maintenance margin is attached to a synthetic row to avoid
    # multiplying it once per contract. This is a conservative fallback, not
    # the broker's spread payoff calculation.
    if maintenance_margin > 0:
        output.append(PositionExposure(
            underlying="OPTIONS_MARGIN", spot=1.0, delta_shares=0.0,
            max_loss=maintenance_margin, margin=maintenance_margin, source="broker_account_margin",
        ))
    if any(_safe_number(row.get("qty")) < 0 for row in option_rows) and maintenance_margin <= 0:
        quality.append("short_option_positions_without_broker_maintenance_margin")
    return output, audit_rows, len({(item["underlying"], item["expiration"]) for item in audit_rows}), quality


def _position_attributions(journal: OptionsJournal, audit_rows: list[dict], now: datetime) -> list[dict]:
    previous = journal.latest("greeks") or {}
    old_by_symbol = {
        str(item.get("symbol")): item
        for item in previous.get("positions", [])
        if isinstance(item, dict) and item.get("symbol")
    }
    result = []
    for current in audit_rows:
        old = old_by_symbol.get(current["symbol"])
        if not old:
            continue
        try:
            old_greeks = old.get("greeks") or {}
            stamp = datetime.fromisoformat(str(old.get("quote_at")).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            current_qty = _safe_number(current.get("qty"))
            old_qty = _safe_number(old.get("qty"))
            if current_qty != old_qty:
                continue
            multiplier = int(current.get("multiplier") or 100)
            pnl = (
                (float(current["midpoint"]) - float(old["midpoint"]))
                * current_qty * multiplier
            )
            greeks = GreeksSnapshot(
                delta=_safe_number(old_greeks.get("delta")) * (1 if current_qty > 0 else -1),
                gamma=_safe_number(old_greeks.get("gamma")) * (1 if current_qty > 0 else -1),
                theta=_safe_number(old_greeks.get("theta")) * (1 if current_qty > 0 else -1),
                vega=_safe_number(old_greeks.get("vega")) * (1 if current_qty > 0 else -1),
                rho=_safe_number(old_greeks.get("rho")) * (1 if current_qty > 0 else -1),
                implied_volatility=_safe_number(old_greeks.get("implied_volatility")),
                observed_at=stamp,
                source=str(old_greeks.get("source") or "journal"),
            )
            elapsed_days = max(0.0, (now.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() / 86400)
            result.append({
                "symbol": current["symbol"],
                "attribution": pnl_attribution(
                    previous_greeks=greeks,
                    underlying_move=float(current["spot"]) - float(old["spot"]),
                    implied_volatility_move=(
                        _safe_number((current.get("greeks") or {}).get("implied_volatility"))
                        - greeks.implied_volatility
                    ),
                    days_elapsed=elapsed_days,
                    multiplier=multiplier,
                    contracts=int(abs(current_qty)),
                    realized_pnl=pnl,
                ),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _order_payload(signal, contracts: int, limit_price: float, client_order_id: str) -> dict:
    structure = signal.structure
    if not all(leg.contract.tradable for leg in structure.legs):
        raise ValueError("cannot submit model or historical synthetic contracts")
    if len(structure.legs) == 1:
        leg = structure.legs[0]
        return {
            "symbol": leg.contract.symbol,
            "qty": str(contracts * leg.ratio_qty),
            "side": leg.side,
            "type": "limit",
            "limit_price": str(max(0.01, round(abs(limit_price), 2))),
            "time_in_force": "day",
            "position_intent": leg.position_intent,
            "client_order_id": client_order_id,
        }
    return {
        "order_class": "mleg",
        "qty": str(contracts),
        "type": "limit",
        "limit_price": str(round(limit_price, 2)),
        "time_in_force": "day",
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": leg.contract.symbol,
                "ratio_qty": str(leg.ratio_qty),
                "side": leg.side,
                "position_intent": leg.position_intent,
            }
            for leg in structure.legs
        ],
    }


def _daily_order_count(orders: list[dict], now: datetime) -> int:
    count = 0
    for order in orders:
        raw = order.get("submitted_at") or order.get("created_at")
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            if stamp.astimezone(_US_EQUITY_TZ).date() == now.astimezone(_US_EQUITY_TZ).date():
                if str(order.get("status", "")).lower() not in {"rejected", "expired"}:
                    count += 1
        except ValueError:
            continue
    return count


@_serialized_options_account
def run_paper_cycle(
    *,
    submit: bool = False,
    config_path: str | Path | None = None,
    options_client: OptionsPaperClient | None = None,
    algo_client: AlgoAlpaca | None = None,
    now: datetime | None = None,
) -> dict:
    config = load_options_config(config_path)
    if not config.get("enabled", True):
        raise RuntimeError("options paper route is disabled in config")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("cycle time must be timezone-aware")
    client = options_client or OptionsPaperClient()
    account = client.verify_account()
    algo = algo_client or AlgoAlpaca()
    algo_account = algo.account()
    if str(account.get("id") or account.get("account_number")) == str(algo_account.get("id") or algo_account.get("account_number")):
        raise RuntimeError("Options and Algo API credentials resolve to the same account")
    option_account_equity = _safe_number(account.get("equity"))
    algo_equity = _safe_number(algo_account.get("equity"))
    combined_equity = option_account_equity + algo_equity
    if min(option_account_equity, algo_equity, combined_equity) <= 0:
        raise RuntimeError("account equity is unavailable for combined options risk checks")
    previous_options_equity = _safe_number(account.get("last_equity"))
    previous_algo_equity = _safe_number(algo_account.get("last_equity"))
    previous_combined_equity = (
        previous_options_equity + previous_algo_equity
        if previous_options_equity > 0 and previous_algo_equity > 0 else 0.0
    )
    combined_daily_pnl_pct = (
        (combined_equity - previous_combined_equity) / previous_combined_equity
        if previous_combined_equity > 0 else None
    )
    # Trading level may be intentionally downgraded below approval level.
    trading_level_raw = account.get("options_trading_level")
    approved_level_raw = account.get("options_approved_level")
    active_level_raw = trading_level_raw if trading_level_raw is not None else approved_level_raw
    option_level = int(_safe_number(active_level_raw, -1.0)) if active_level_raw is not None else None
    approved_level = int(_safe_number(approved_level_raw, -1.0)) if approved_level_raw is not None else None
    algo_positions = algo.positions()
    option_positions_all = client.positions()
    open_orders = client.orders("open")
    all_orders = client.orders("all")
    entries_today = _daily_order_count(all_orders, now)
    feed = str(config["market_data_feed"])
    provider = AlpacaOptionDataProvider(
        client,
        feed=feed,
        max_quote_age_seconds=int(config["max_quote_age_seconds"]),
        rate=float(config["rate"]),
        dividend_yield=float(config["dividend_yield"]),
    )
    journal = OptionsJournal(client.account_id)
    for order in all_orders:
        filled_qty = _safe_number(order.get("filled_qty"))
        client_order_id = str(order.get("client_order_id") or "")
        if filled_qty <= 0 or not client_order_id or journal.contains("fills", "client_order_id", client_order_id):
            continue
        journal.append("fills", {
            "source": "broker_reconciliation",
            "order_id": order.get("id"),
            "client_order_id": client_order_id,
            "status": order.get("status"),
            "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
            "legs": order.get("legs") or order.get("symbol"),
        })
    regime, volatility_state = _state_regime()
    candidates: list[tuple[object, OptionChain, list[tuple]]] = []
    diagnostics = {
        "regime": regime,
        "volatility_state": volatility_state,
        "options_trading_level": option_level,
        "options_approved_level": approved_level,
        "combined_daily_pnl_pct": combined_daily_pnl_pct,
    }
    diagnostics["options_entries_today"] = entries_today

    if config["vrp"].get("enabled", True):
        symbol = str(config["vrp"].get("underlying", "SPY")).upper()
        vrp_chain = provider.get_chain(
            symbol, as_of=now, min_dte=int(config["vrp"]["min_dte"]), max_dte=int(config["vrp"]["max_dte"]),
        )
        closes = provider.underlying_closes(
            symbol, as_of=now, sessions=int(config["vrp"]["realized_lookback_sessions"]) + 1,
        )
        strategy_config = {
            **config["vrp"],
            "max_quote_spread_pct": config["max_quote_spread_pct"],
            "min_open_interest": config["min_open_interest"],
            "regime_risk_multipliers": config.get("regime_risk_multipliers", {}),
        }
        signal = VolatilityRiskPremiumStrategy(strategy_config).generate(
            OptionsStrategyContext(vrp_chain, closes, regime, volatility_state, as_of=now),
        )
        if signal:
            structure = replace(signal.structure, metadata={**signal.structure.metadata, "underlying_spot": vrp_chain.spot})
            candidates.append((replace(signal, structure=structure), vrp_chain, closes))
        diagnostics["vrp"] = {"chain_contracts": len(vrp_chain.contracts), "signal": signal is not None, "feed": feed}

    recommendations, ai_status = _load_ai_recommendations()
    event_config = {
        **config["event_driven"],
        "regime_risk_multipliers": config.get("regime_risk_multipliers", {}),
    }
    min_confidence = float(event_config.get("min_llm_confidence", 0.60))
    eligible_recommendations = [
        recommendation
        for recommendation in recommendations.values()
        if recommendation.action in {"buy", "sell"}
        and recommendation.confidence / 100.0 >= min_confidence
    ]
    eligible_recommendations.sort(key=lambda item: (-item.confidence, item.ticker))
    max_symbols = max(0, int(event_config.get("max_symbols_per_cycle", 3)))
    selected_recommendations = eligible_recommendations[:max_symbols]
    diagnostics["event_driven"] = {
        "signal_source": "event_driven_llm_briefing",
        "ai_signal_book": ai_status,
        "eligible_recommendations": len(eligible_recommendations),
        "selected_symbols": [item.ticker for item in selected_recommendations],
        "max_symbols_per_cycle": max_symbols,
    }
    event_regime_weight = regime_risk_multiplier(event_config, regime)
    diagnostics["event_driven"]["regime_risk_multiplier"] = event_regime_weight
    if config["event_driven"].get("enabled", True) and event_regime_weight > 0:
        strategy = EventDrivenOptionsStrategy(event_config)
        event_errors = []
        for recommendation in selected_recommendations:
            symbol = recommendation.ticker
            try:
                chain = provider.get_chain(
                    symbol,
                    as_of=now,
                    min_dte=int(event_config.get("min_dte", 3)),
                    max_dte=int(event_config.get("max_dte", 35)),
                )
                signal = strategy.generate(
                    OptionsStrategyContext(
                        chain,
                        (),
                        regime,
                        volatility_state,
                        event_recommendations={symbol: recommendation},
                        as_of=now,
                    ),
                )
                if signal:
                    structure = replace(signal.structure, metadata={**signal.structure.metadata, "underlying_spot": chain.spot})
                    candidates.append((replace(signal, structure=structure), chain, []))
            except Exception as exc:
                event_errors.append({"underlying": symbol, "error": type(exc).__name__})
                LOG.warning("Options event sleeve could not build %s from LLM briefing: %s", symbol, type(exc).__name__)
        diagnostics["event_driven"]["chain_errors"] = event_errors
    elif event_regime_weight <= 0:
        diagnostics["event_driven"]["entries_disabled_for_regime"] = True

    # A flat options account may be used immediately; existing unrecognized
    # holdings prevent new entries until the risk adapter can price them.
    options_positions, position_audit, existing_structures, data_quality = _option_position_exposures(
        client, option_positions_all, as_of=now, feed=feed, config=config,
        maintenance_margin=_safe_number(account.get("maintenance_margin")),
    )
    equity_exposures = _stock_position_exposures(client, algo_positions)
    option_account_stock_exposures = _stock_position_exposures(client, option_positions_all)
    if data_quality:
        raise RuntimeError("cannot value current option holdings: " + "; ".join(data_quality))
    open_option_order_exposure = [
        order for order in open_orders
        if str(order.get("asset_class") or "us_option") == "us_option"
    ]
    if open_option_order_exposure:
        raise RuntimeError("open option orders exist; reconcile or cancel before another strategy entry")

    # Use broker account maintenance margin as a conservative fallback for the
    # current book; broker margin remains the final authority.
    cost_model = OptionsCostModel(**config.get("costs", {}))
    manager = PortfolioRiskManager(config["risk"], cost_model)
    staged_options = list(options_positions)
    staged_structures = existing_structures
    decisions = []
    submitted_exposure = []
    account_id = str(account.get("id") or account.get("account_number") or client.account_id)
    assignments = []
    try:
        assignments = client.recent_option_activities(after=(now.date() - timedelta(days=5)).isoformat())
    except Exception as exc:
        diagnostics["assignment_activity"] = {"available": False, "error": type(exc).__name__}
    for event in assignments:
        journal.append("assignment_events", {"broker_activity": event})
    simulated_assignments = [
        row["simulated_assignment"]
        for row in position_audit
        if row.get("simulated_assignment") is not None
    ]
    for event in simulated_assignments:
        journal.append("assignment_events", {"simulation": event})
    mark_attributions = _position_attributions(journal, position_audit, now)
    journal.append("greeks", {
        "kind": "open_positions", "positions": position_audit,
        "regime": regime, "pnl_attribution_since_previous_mark": mark_attributions,
    })

    # Event trades outrank VRP if both propose the same underlying.
    candidates.sort(key=lambda entry: (0 if entry[0].strategy == "options_event_driven" else 1, entry[0].underlying))
    seen_underlyings = set()
    for signal, chain, _closes in candidates:
        max_daily_loss_pct = float(config["risk"].get("max_daily_loss_pct", 0.02))
        if combined_daily_pnl_pct is not None and combined_daily_pnl_pct <= -max_daily_loss_pct:
            decisions.append({
                "signal": signal.to_dict(),
                "approved": False,
                "reason": "combined portfolio daily loss limit reached",
                "combined_daily_pnl_pct": combined_daily_pnl_pct,
            })
            continue
        if entries_today >= int(config["risk"].get("max_entries_per_day", 2)):
            decisions.append({"signal": signal.to_dict(), "approved": False, "reason": "options daily entry limit reached"})
            continue
        if signal.underlying in seen_underlyings:
            decisions.append({"signal": signal.to_dict(), "approved": False, "reason": "another options sleeve already proposed this underlying"})
            continue
        seen_underlyings.add(signal.underlying)
        identity = (
            f"{account_id}:{now.astimezone(_US_EQUITY_TZ).date()}:{signal.strategy}:"
            f"{signal.structure.structure_id}:{signal.event_id or ''}"
        )
        client_order_id = "opt-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        prior_order = next(
            (order for order in all_orders if str(order.get("client_order_id") or "") == client_order_id),
            None,
        )
        prior_intent = journal.latest_matching("orders", "client_order_id", client_order_id)
        journal_blocks_retry = prior_intent is not None and (
            not submit or prior_intent.get("status") != "dry_run"
        )
        if prior_order is not None or journal_blocks_retry:
            result = {
                "signal": signal.to_dict(),
                "approved": False,
                "reason": "this signal already has an order intent for the current options session",
                "client_order_id": client_order_id,
                "existing_order": prior_order,
            }
            decisions.append(result)
            journal.append("decisions", result)
            continue
        if any(
            str(position.get("symbol") or "").upper() in {leg.contract.symbol for leg in signal.structure.legs}
            for position in option_positions_all
        ):
            decisions.append({"signal": signal.to_dict(), "approved": False, "reason": "one or more target contracts are already held"})
            continue
        required_level = 3 if len(signal.structure.legs) > 1 else 2
        if option_level is not None and option_level < required_level:
            decisions.append({
                "signal": signal.to_dict(),
                "approved": False,
                "reason": f"options account level {option_level} is below required level {required_level}",
            })
            continue
        buying_power = _safe_number(account.get("options_buying_power", account.get("buying_power")), math.inf)
        decision = manager.size_and_validate(
            signal,
            combined_equity=combined_equity,
            options_equity=option_account_equity,
            equity_positions=equity_exposures,
            options_equity_positions=option_account_stock_exposures,
            option_positions=staged_options,
            existing_option_structures=staged_structures,
            options_buying_power=buying_power,
        )
        result = {
            "signal": signal.to_dict(),
            "approved": decision.approved,
            "contracts": decision.contracts,
            "reason": decision.reason,
            "risk_per_structure": decision.risk_per_structure,
            "total_risk": decision.total_risk,
            "margin_after": decision.margin_after,
            "order_limit_price": decision.order_limit_price,
            "projected_risk": decision.projected_risk,
        }
        if decision.approved:
            payload = _order_payload(signal, decision.contracts, decision.order_limit_price, client_order_id)
            result["order"] = payload
            result["estimated_costs"] = cost_model.estimate(signal.structure, decision.contracts)
            if submit:
                journal.append("orders", {
                    "status": "submitting",
                    "client_order_id": client_order_id,
                    "order": payload,
                    "signal": signal.to_dict(),
                    "estimated_costs": result["estimated_costs"],
                })
                response = client.submit_order(payload)
                result["broker_order"] = response
                journal.append("orders", {
                    "client_order_id": client_order_id,
                    "order": payload,
                    "broker_response": response,
                    "estimated_costs": result["estimated_costs"],
                    "signal": signal.to_dict(),
                })
                if str(response.get("status", "")).lower() == "filled":
                    journal.append("fills", {
                        "order_id": response.get("id"),
                        "client_order_id": client_order_id,
                        "filled_qty": response.get("filled_qty"),
                        "filled_avg_price": response.get("filled_avg_price"),
                        "legs": payload.get("legs") or [payload.get("symbol")],
                    })
                submitted_exposure.append(manager._candidate_exposure(signal.structure, decision.contracts))
            else:
                journal.append("orders", {
                    "status": "dry_run",
                    "client_order_id": client_order_id,
                    "order": payload,
                    "signal": signal.to_dict(),
                    "estimated_costs": result["estimated_costs"],
                })
            # Reserve candidate capacity in this scan even in dry-run mode so
            # simultaneous sleeves see one another in the shared risk book.
            staged_options.append(manager._candidate_exposure(signal.structure, decision.contracts))
            staged_structures += 1
            entries_today += 1
        decisions.append(result)
        journal.append("decisions", result)

    # Keep a per-account record of every successful scan, including cycles
    # where no sleeve generated a signal. This lets status reports distinguish
    # an idle strategy from an options runner that never executed.
    journal.append("decisions", {
        "kind": "cycle_summary",
        "status": "signals_evaluated" if decisions else "no_signal",
        "candidate_count": len(candidates),
        "decision_count": len(decisions),
        "approved_count": sum(bool(row.get("approved")) for row in decisions),
        "submitted_count": sum(bool((row.get("broker_order") or {}).get("id")) for row in decisions),
        "dry_run": not submit,
        "regime": regime,
        "volatility_state": volatility_state,
        "diagnostics": diagnostics,
    })
    journal.append("orders", {
        "kind": "cycle_summary",
        "status": "cycle_no_order" if not any(row.get("order") for row in decisions) else "cycle_order_intent_recorded",
        "candidate_count": len(candidates),
        "decision_count": len(decisions),
        "approved_count": sum(bool(row.get("approved")) for row in decisions),
        "submitted_count": sum(bool((row.get("broker_order") or {}).get("id")) for row in decisions),
        "dry_run": not submit,
        "regime": regime,
        "volatility_state": volatility_state,
    })

    # Record account and option marks every run. This summary is option-account
    # only in the shared snapshot; equity risk is added when the coordinator reads it.
    published_rows = [*options_positions, *option_account_stock_exposures]
    if submit:
        published_rows.extend(submitted_exposure)
    option_only = summarize_portfolio(
        combined_equity=combined_equity,
        options_equity=option_account_equity,
        positions=published_rows,
        data_quality=data_quality,
    )
    publish_risk_snapshot(option_only, account_id=account_id)
    full_summary = summarize_portfolio(
        combined_equity=combined_equity,
        options_equity=option_account_equity,
        positions=[*equity_exposures, *published_rows],
        data_quality=data_quality,
    )
    daily = {
        "account_id": account_id,
        "dry_run": not submit,
        "regime": regime,
        "volatility_state": volatility_state,
        "options_equity": option_account_equity,
        "algo_equity": algo_equity,
        "combined_daily_pnl_pct": combined_daily_pnl_pct,
        "summary": full_summary.to_dict(),
        "options_account_summary": option_only.to_dict(),
        "decisions": decisions,
        "open_option_positions": position_audit,
        "pnl_attribution_since_previous_mark": mark_attributions,
        "assignment_activities": assignments,
        "simulated_assignment_scenarios": simulated_assignments,
        "data_quality": data_quality,
        "diagnostics": diagnostics,
    }
    journal.append("risk_daily", daily)
    return daily


def main() -> int:
    parser = argparse.ArgumentParser(description="Options paper sleeve; dry-run by default")
    parser.add_argument("--submit", action="store_true", help="send approved orders to Alpaca paper trading")
    parser.add_argument("--config", type=Path, help="JSON config override (defaults to data/options/config.json)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [options.paper] %(levelname)s %(message)s")
    try:
        report = run_paper_cycle(submit=args.submit, config_path=args.config)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2
    print(json.dumps(report, indent=2, default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
