"""Paper-only book for observing Multi Factor core entries without routing orders."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import statistics


def _event(state: dict, item: dict) -> None:
    events = state.setdefault("events", [])
    events.append(item)
    del events[:-2_000]


def _close_position(
    state: dict, ticker: str, position: dict, *, raw_price: float,
    timestamp: datetime, reason: str, slippage: float, commission_per_share: float,
) -> dict:
    qty = int(position.get("qty", 0) or 0)
    if qty > 0:
        fill = max(0.01, raw_price * (1 - slippage))
    else:
        fill = raw_price * (1 + slippage)
    exit_commission = abs(qty) * commission_per_share
    pnl = qty * (fill - float(position["entry_price"])) - float(position.get("entry_commission", 0)) - exit_commission
    state["realized_pnl"] = float(state.get("realized_pnl", 0)) + qty * (fill - float(position["entry_price"])) - exit_commission
    trade = {
        "ticker": ticker, "direction": "long" if qty > 0 else "short", "qty": abs(qty),
        "entry_at": position.get("entry_at"), "exit_at": timestamp.isoformat(),
        "entry_price": round(float(position["entry_price"]), 4), "exit_price": round(fill, 4),
        "pnl": round(pnl, 4), "exit_reason": reason,
        "entry_commission": round(float(position.get("entry_commission", 0)), 4),
        "exit_commission": round(exit_commission, 4), "execution_mode": "shadow",
    }
    state.setdefault("closed_trades", []).append(trade)
    del state["closed_trades"][:-5_000]
    state.setdefault("positions", {}).pop(ticker, None)
    state.setdefault("last_exit_at_by_ticker", {})[ticker] = timestamp.isoformat()
    _event(state, {"type": "exit", **trade})
    return trade


def _mark(state: dict, quotes: dict, timestamp: datetime, *, slippage: float, commission: float) -> list[dict]:
    triggered = []
    for ticker, position in list((state.get("positions") or {}).items()):
        quote = quotes.get(ticker)
        if not quote:
            continue
        qty = int(position.get("qty", 0) or 0)
        mark = float(quote.get("bid" if qty > 0 else "ask") or 0)
        if mark <= 0:
            continue
        position["last_mark"] = mark
        position["unrealized_pnl"] = round(qty * (mark - float(position["entry_price"])), 4)
        stop = float(position.get("stop_price", 0) or 0)
        target = float(position.get("take_profit", 0) or 0)
        reason = None
        exit_price = mark
        if qty > 0 and stop and mark <= stop:
            reason, exit_price = "shadow stop-loss", min(mark, stop)
        elif qty < 0 and stop and mark >= stop:
            reason, exit_price = "shadow short stop-loss", max(mark, stop)
        elif qty > 0 and target and mark >= target:
            reason, exit_price = "shadow take-profit", target
        elif qty < 0 and target and mark <= target:
            reason, exit_price = "shadow short take-profit", target
        if reason:
            triggered.append(_close_position(
                state, ticker, position, raw_price=exit_price, timestamp=timestamp,
                reason=reason, slippage=slippage, commission_per_share=commission,
            ))
    return triggered


def _equity(state: dict, initial_capital: float) -> float:
    unrealized = sum(float(row.get("unrealized_pnl", 0) or 0) for row in (state.get("positions") or {}).values())
    return float(initial_capital) + float(state.get("realized_pnl", 0) or 0) + unrealized


def _summary(state: dict, initial_capital: float, today: str) -> dict:
    curve = state.get("equity_curve") or []
    returns = []
    previous = float(initial_capital)
    peak = float(initial_capital)
    drawdown = 0.0
    for row in curve:
        equity = float(row.get("equity", 0) or 0)
        if previous > 0:
            returns.append(equity / previous - 1)
        previous = equity
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak if peak > 0 else 0)
    deviation = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    downside = [item for item in returns if item < 0]
    downside_deviation = math.sqrt(sum(item * item for item in downside) / len(downside)) if downside else 0.0
    trades = state.get("closed_trades") or []
    pnls = [float(row.get("pnl", 0) or 0) for row in trades]
    winners = [value for value in pnls if value > 0]
    losers = [value for value in pnls if value < 0]
    current_equity = _equity(state, initial_capital)
    return {
        "execution_mode": "shadow", "initial_equity": round(initial_capital, 2),
        "current_equity": round(current_equity, 2),
        "net_return_pct": round((current_equity / initial_capital - 1) * 100, 4),
        "max_drawdown_pct": round(drawdown * 100, 4),
        "daily_sharpe_annualized": round(math.sqrt(252) * statistics.mean(returns) / deviation, 4)
        if deviation > 0 and returns else None,
        "daily_sortino_annualized": round(math.sqrt(252) * statistics.mean(returns) / downside_deviation, 4)
        if downside_deviation > 0 and returns else None,
        "closed_trade_count": len(trades), "open_position_count": len(state.get("positions") or {}),
        "win_rate_pct": round(len(winners) / len(pnls) * 100, 2) if pnls else None,
        "profit_factor": round(sum(winners) / abs(sum(losers)), 4) if losers else ("inf" if winners else None),
        "entries_today": int((state.get("entries_by_day") or {}).get(today, 0)),
    }


def process_shadow_orders(
    state: dict | None,
    candidates: list[dict],
    *,
    quotes: dict[str, dict],
    real_positions: dict[str, dict],
    today: str,
    timestamp: datetime,
    initial_capital: float,
    max_entries_per_day: int,
    max_positions: int,
    slippage_bps: float,
    commission_per_share: float,
    quote_stale_seconds: int,
    cooldown_minutes: int,
    make_trade,
    risk: dict,
    universe: dict,
    coordinator_book,
    session: str,
    regime: str,
    session_open: datetime | None = None,
    session_close: datetime | None = None,
) -> tuple[dict, list[dict], dict]:
    """Update shadow marks and apply eligible Multi Factor proposals locally."""
    state = state if isinstance(state, dict) else {}
    state.setdefault("schema", "swarm_algo_multi_factor_shadow_v1")
    state.setdefault("initial_capital", float(initial_capital))
    state.setdefault("realized_pnl", 0.0)
    state.setdefault("positions", {})
    state.setdefault("closed_trades", [])
    state.setdefault("entries_by_day", {})
    state.setdefault("last_exit_at_by_ticker", {})
    state.setdefault("equity_curve", [])
    state.setdefault("events", [])
    initial_capital = float(state.get("initial_capital") or initial_capital)
    slippage = max(0.0, float(slippage_bps)) / 10_000
    commission = max(0.0, float(commission_per_share))
    now_utc = timestamp.astimezone(timezone.utc) if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    outcome_events = _mark(state, quotes, now_utc, slippage=slippage, commission=commission)

    exits = sorted((row for row in candidates if row.get("action") in {"sell", "cover"}), key=lambda row: str(row.get("ticker", "")))
    entries = sorted((row for row in candidates if row.get("action") in {"buy", "short"}), key=lambda row: (
        int(row.get("rank") or 10_000), -float(row.get("score") or 0), str(row.get("ticker", "")),
    ))
    for signal in exits:
        ticker = str(signal.get("ticker", "")).upper()
        position = (state.get("positions") or {}).get(ticker)
        quote = quotes.get(ticker)
        if not position or not quote:
            continue
        qty = int(position.get("qty", 0) or 0)
        if (signal.get("action") == "sell" and qty <= 0) or (signal.get("action") == "cover" and qty >= 0):
            continue
        exit_price = float(quote.get("bid" if qty > 0 else "ask") or 0)
        if exit_price > 0:
            outcome_events.append(_close_position(
                state, ticker, position, raw_price=exit_price, timestamp=now_utc,
                reason=str(signal.get("reason") or "factor rebalance exit"),
                slippage=slippage, commission_per_share=commission,
            ))

    entries_today = int(state["entries_by_day"].get(today, 0) or 0)
    recent_days = sorted(state["entries_by_day"])
    for old_day in recent_days[:-45]:
        state["entries_by_day"].pop(old_day, None)
    for signal in entries:
        ticker = str(signal.get("ticker", "")).upper()
        if ticker in state["positions"]:
            continue
        reason = None
        if signal.get("suppressed_reason"):
            reason = str(signal["suppressed_reason"])
        if ticker in real_positions and float((real_positions[ticker] or {}).get("qty", 0) or 0) != 0:
            reason = "ticker already has a live position"
        elif entries_today >= max_entries_per_day:
            reason = "shadow daily entry limit reached"
        elif len(state["positions"]) >= max_positions:
            reason = "shadow open-position limit reached"
        if reason is None:
            raw_exit = state["last_exit_at_by_ticker"].get(ticker)
            if raw_exit:
                try:
                    exited_at = datetime.fromisoformat(str(raw_exit).replace("Z", "+00:00"))
                    if exited_at.tzinfo is None:
                        exited_at = exited_at.replace(tzinfo=timezone.utc)
                    if (now_utc - exited_at.astimezone(timezone.utc)).total_seconds() < cooldown_minutes * 60:
                        reason = "shadow ticker cooldown active"
                except ValueError:
                    pass
        quote = quotes.get(ticker)
        if reason is None and not quote:
            reason = "latest quote unavailable"
        if reason is None:
            age = (now_utc - quote["timestamp"].astimezone(timezone.utc)).total_seconds()
            if age < 0 or age > quote_stale_seconds:
                reason = f"quote stale by {age:.0f}s"
        if reason is not None:
            rejected = {"ticker": ticker, "action": signal.get("action"), "reason": reason, "execution_mode": "shadow"}
            _event(state, {"type": "rejected", "date": today, **rejected})
            outcome_events.append(rejected)
            continue
        reference = float(quote["ask"] if signal["action"] == "buy" else quote["bid"])
        if reference <= 0:
            continue
        shadow_equity = _equity(state, initial_capital)
        proposed = make_trade(signal, reference, shadow_equity, {}, risk, mode_universe=universe)
        if not proposed:
            rejected = {"ticker": ticker, "action": signal.get("action"), "reason": "zero-sized shadow order", "execution_mode": "shadow"}
            _event(state, {"type": "rejected", "date": today, **rejected})
            outcome_events.append(rejected)
            continue
        decision = coordinator_book.review(
            proposed, session=session, timestamp=timestamp, regime=regime,
            session_open=session_open, session_close=session_close,
        )
        if not decision.approved or decision.proposal is None:
            rejected = {"ticker": ticker, "action": signal.get("action"), "reason": decision.reason or "coordinator rejected", "execution_mode": "shadow"}
            _event(state, {"type": "rejected", "date": today, **rejected})
            outcome_events.append(rejected)
            continue
        proposed = decision.proposal
        fill = reference * (1 + slippage if signal["action"] == "buy" else 1 - slippage)
        quantity = int(proposed["qty"])
        signed_quantity = quantity if signal["action"] == "buy" else -quantity
        stop_pct = float(signal.get("stop_pct") or 0.06)
        original_entry = float(proposed.get("entry_price") or reference)
        original_stop = float(proposed.get("stop_price") or 0)
        original_target = float(proposed.get("take_profit") or 0)
        stop_distance = abs(original_entry - original_stop)
        target_distance = abs(original_target - original_entry)
        stop = fill - stop_distance if signed_quantity > 0 else fill + stop_distance
        target = fill + target_distance if signed_quantity > 0 else fill - target_distance
        entry_fee = quantity * commission
        state["realized_pnl"] = float(state.get("realized_pnl", 0)) - entry_fee
        position = {
            "ticker": ticker, "qty": signed_quantity, "entry_price": round(fill, 4),
            "entry_at": now_utc.isoformat(), "entry_day": today,
            "stop_price": round(stop, 4), "take_profit": round(target, 4),
            "entry_commission": round(entry_fee, 4), "last_mark": reference,
            "unrealized_pnl": 0.0, "reason": signal.get("reason"),
            "rank": signal.get("rank"), "score": signal.get("score"),
        }
        state["positions"][ticker] = position
        entries_today += 1
        state["entries_by_day"][today] = entries_today
        event = {
            "type": "entry", "date": today, "ticker": ticker,
            "action": signal["action"], "qty": quantity,
            "entry_price": position["entry_price"], "stop_price": position["stop_price"],
            "take_profit": position["take_profit"], "rank": signal.get("rank"),
            "score": signal.get("score"), "execution_mode": "shadow",
        }
        _event(state, event)
        outcome_events.append({**event, "client_order_id": None, "broker_submission": False})

    equity = _equity(state, initial_capital)
    curve = state["equity_curve"]
    row = {"date": today, "equity": round(equity, 2)}
    if curve and curve[-1].get("date") == today:
        curve[-1] = row
    else:
        curve.append(row)
    del curve[:-5_000]
    summary = _summary(state, initial_capital, today)
    state["summary"] = summary
    state["last_updated_at"] = now_utc.isoformat()
    return state, outcome_events, summary
