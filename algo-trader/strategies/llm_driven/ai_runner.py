"""Run a read-only analyst pass and publish Algo LLM-driven recommendations."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, time as datetime_time, timedelta, timezone
import io
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from market.data import regular_feed
from strategies.runner.data import fetch_bars, fetch_latest_quotes
from strategies.llm_driven.ai import DEFAULT_AI_SIGNAL_DB, publish_llm_signal_snapshot
from strategies.universe import load_algo_universe
from broker.portfolio import convert_to_portfolio, get_alpaca_account, get_alpaca_positions
from strategies.llm_driven.owned import owned_long_positions
from strategies.llm_driven.analysis.workflow import run_event_analysis
from strategies.llm_driven.analysis.progress import progress


ANALYSTS = [
    "market_regime",
    "news_sentiment_analyst",
    "sentiment_analyst",
    "technical_analyst",
    "fundamentals_analyst",
    "growth_analyst",
]
DEFAULT_TTL_MINUTES = 90
_ACTIVE_DIAGNOSTICS = None
ET = ZoneInfo("America/New_York")
REGIME_SYMBOLS = ("SPY", "QQQ")


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bar_datetime(bar: dict) -> datetime | None:
    raw = bar.get("t")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _market_regime_context(
    *, now: datetime | None = None,
    quotes: dict | None = None,
    intraday_bars: dict | None = None,
    daily_bars: dict | None = None,
) -> tuple[dict[str, dict], dict]:
    """Build live SPY/QQQ price, VWAP, and momentum inputs from Alpaca data."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market context time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    local_now = now.astimezone(ET)
    today = local_now.date()
    quotes = quotes or {}
    intraday_bars = intraday_bars or {}
    daily_bars = daily_bars or {}
    context: dict[str, dict] = {}
    diagnostics: dict[str, dict] = {}

    for symbol in REGIME_SYMBOLS:
        bars = [
            bar for bar in (intraday_bars.get(symbol) or [])
            if isinstance(bar, dict) and _bar_datetime(bar) is not None
            and _bar_datetime(bar) <= now
        ]
        bars.sort(key=lambda bar: _bar_datetime(bar))
        daily = [
            bar for bar in (daily_bars.get(symbol) or [])
            if isinstance(bar, dict) and _bar_datetime(bar) is not None
            and _bar_datetime(bar).astimezone(ET).date() < today
        ]
        daily.sort(key=lambda bar: _bar_datetime(bar))

        quote = quotes.get(symbol)
        quote_time = quote.get("timestamp") if isinstance(quote, dict) else None
        if isinstance(quote_time, str):
            try:
                quote_time = datetime.fromisoformat(quote_time.replace("Z", "+00:00"))
            except ValueError:
                quote_time = None
        if isinstance(quote_time, datetime) and quote_time.tzinfo is not None:
            quote_age = (now - quote_time.astimezone(timezone.utc)).total_seconds()
        else:
            quote_age = None
        quote_price = _finite_float(quote.get("price")) if isinstance(quote, dict) else None
        if quote_price and quote_age is not None and -10 <= quote_age <= 90:
            current_price = quote_price
            price_source = "fresh_quote"
        else:
            current_price = _finite_float(bars[-1].get("c")) if bars else None
            last_bar_time = _bar_datetime(bars[-1]) if bars else None
            if current_price and last_bar_time and 0 <= (now - last_bar_time).total_seconds() <= 300:
                price_source = "latest_1m_bar"
            else:
                current_price = None
                price_source = "unavailable"

        volume_total = 0.0
        price_volume_total = 0.0
        for bar in bars:
            volume = _finite_float(bar.get("v")) or 0.0
            if volume <= 0:
                continue
            bar_vwap = _finite_float(bar.get("vw"))
            if bar_vwap is None or bar_vwap <= 0:
                high = _finite_float(bar.get("h"))
                low = _finite_float(bar.get("l"))
                close = _finite_float(bar.get("c"))
                if high is None or low is None or close is None:
                    continue
                bar_vwap = (high + low + close) / 3
            volume_total += volume
            price_volume_total += bar_vwap * volume
        vwap = price_volume_total / volume_total if volume_total else None
        price_vs_vwap = (
            (current_price / vwap - 1) * 100
            if current_price is not None and vwap is not None and vwap > 0 else None
        )

        closes = [
            close for bar in bars if (close := _finite_float(bar.get("c"))) is not None
        ]
        rsi_14 = None
        if len(closes) >= 15:
            changes = [closes[index] - closes[index - 1] for index in range(len(closes) - 14, len(closes))]
            average_gain = sum(max(change, 0.0) for change in changes) / 14
            average_loss = sum(max(-change, 0.0) for change in changes) / 14
            if average_gain == 0 and average_loss == 0:
                rsi_14 = 50.0
            elif average_loss == 0:
                rsi_14 = 100.0
            else:
                rsi_14 = 100 - 100 / (1 + average_gain / average_loss)

        volume_ratio = None
        if len(bars) >= 60:
            recent_volume = sum(_finite_float(bar.get("v")) or 0.0 for bar in bars[-30:])
            earlier_volume = sum(_finite_float(bar.get("v")) or 0.0 for bar in bars[-60:-30])
            if earlier_volume > 0:
                volume_ratio = recent_volume / earlier_volume

        momentum_30m = None
        if current_price is not None and bars:
            cutoff = now - timedelta(minutes=30)
            prior = [
                bar for bar in bars
                if (bar_time := _bar_datetime(bar)) is not None and bar_time <= cutoff
            ]
            if prior:
                prior_time = _bar_datetime(prior[-1])
                prior_close = _finite_float(prior[-1].get("c"))
                if prior_time and prior_close and 0 <= (cutoff - prior_time).total_seconds() <= 300:
                    momentum_30m = (current_price / prior_close - 1) * 100

        daily_closes = [
            close for bar in daily if (close := _finite_float(bar.get("c"))) is not None and close > 0
        ]
        change_1d = (
            (current_price / daily_closes[-1] - 1) * 100
            if current_price is not None and daily_closes else None
        )
        change_5d = (
            (current_price / daily_closes[-5] - 1) * 100
            if current_price is not None and len(daily_closes) >= 5 else None
        )

        intraday = {
            "vwap": vwap,
            "price_vs_vwap_pct": price_vs_vwap,
            "momentum_30m_pct": momentum_30m,
            "rsi_14": rsi_14,
            "volume_ratio": volume_ratio,
            "high": max((_finite_float(bar.get("h")) or 0 for bar in bars), default=None),
            "low": min((_finite_float(bar.get("l")) or float("inf") for bar in bars), default=None),
            "bars_1min": [
                {key: bar.get(source) for key, source in (
                    ("t", "t"), ("o", "o"), ("h", "h"), ("l", "l"),
                    ("c", "c"), ("v", "v"), ("vw", "vw"),
                )}
                for bar in bars[-30:]
            ],
        }
        if intraday["high"] == 0:
            intraday["high"] = None
        if intraday["low"] == float("inf"):
            intraday["low"] = None
        context[symbol] = {
            "prices": {
                "current": current_price,
                "change_1d": change_1d,
                "change_5d": change_5d,
            },
            "intraday": intraday,
        }
        diagnostics[symbol] = {
            "price_source": price_source,
            "intraday_bar_count": len(bars),
            "daily_bar_count": len(daily),
            "daily_change_data_available": len(daily_closes) >= 5,
            "vwap_available": vwap is not None,
            "momentum_30m_available": momentum_30m is not None,
        }

    status = "ok" if all(
        diagnostics[symbol]["price_source"] != "unavailable"
        and diagnostics[symbol]["daily_change_data_available"]
        and diagnostics[symbol]["vwap_available"]
        and diagnostics[symbol]["momentum_30m_available"]
        for symbol in REGIME_SYMBOLS
    ) else "degraded"
    return context, {"status": status, "symbols": diagnostics}


def _fetch_market_regime_context(now: datetime | None = None) -> tuple[dict, dict]:
    """Fetch the two broad-market symbols used by the regime analyst."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market context time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    local_now = now.astimezone(ET)
    feed = regular_feed()
    symbols = list(REGIME_SYMBOLS)
    quote_errors: list[str] = []
    quote_diagnostics: dict[str, dict] = {}
    quotes = fetch_latest_quotes(
        symbols, mode="algo", feed=feed, errors=quote_errors,
        diagnostics=quote_diagnostics,
    )

    intraday_bars: dict = {}
    intraday_error = None
    market_open = datetime.combine(local_now.date(), datetime_time(9, 30), tzinfo=ET)
    if local_now >= market_open:
        try:
            intraday_bars = fetch_bars(
                symbols, mode="algo", timeframe="1Min",
                start=market_open.isoformat(), end=now.isoformat(),
                limit=10000, feed=feed,
            )
        except Exception as exc:
            intraday_error = type(exc).__name__

    daily_error = None
    try:
        daily_bars = fetch_bars(
            symbols, mode="algo", timeframe="1Day",
            start=(now - timedelta(days=14)).date().isoformat(), end=now.isoformat(),
            limit=10000, feed=feed, adjustment="split",
        )
    except Exception as exc:
        daily_bars = {}
        daily_error = type(exc).__name__

    context, diagnostics = _market_regime_context(
        now=now, quotes=quotes, intraday_bars=intraday_bars, daily_bars=daily_bars,
    )
    diagnostics["quote_errors"] = [
        entry.partition(": ")[2].split(":", 1)[0]
        for entry in quote_errors
    ]
    diagnostics["quotes"] = {
        symbol: {
            "status": item.get("status"),
            "quote_timestamp": item.get("quote_timestamp"),
        }
        for symbol, item in quote_diagnostics.items()
    }
    diagnostics["intraday_error"] = intraday_error
    diagnostics["daily_error"] = daily_error
    return context, diagnostics


class _AnalysisDiagnostics:
    """Append phase and analyst progress events for post-timeout diagnosis."""

    def __init__(self, output_path: str | Path, run_id: str):
        output = Path(output_path)
        self.path = output.with_name("analysis-runs.jsonl")
        self.run_id = run_id
        self.started = time.monotonic()
        self.phase_name: str | None = None
        self.phase_started: float | None = None
        self.last_progress: dict | None = None
        self.progress_started: dict[tuple[str, str], float] = {}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.enabled = True
        except OSError:
            self.enabled = False

    def emit(self, event: str, **fields) -> None:
        if not self.enabled:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "elapsed_ms": round((time.monotonic() - self.started) * 1000),
            "event": event,
            **fields,
        }
        try:
            encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            file_descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(file_descriptor, encoded)
            finally:
                os.close(file_descriptor)
        except (OSError, TypeError, ValueError):
            # Diagnostics must never prevent a signal snapshot from completing.
            return

    def phase(self, name: str, **fields) -> None:
        now = time.monotonic()
        if self.phase_name is not None and self.phase_started is not None:
            self.emit(
                "phase_completed",
                phase=self.phase_name,
                duration_ms=round((now - self.phase_started) * 1000),
            )
        self.phase_name = name
        self.phase_started = now
        self.emit("phase_started", phase=name, **fields)

    def on_progress(self, agent: str, ticker: str | None, status: str, _analysis, _timestamp) -> None:
        now = time.monotonic()
        key = (agent, ticker or "")
        previous = self.progress_started.get(key)
        self.progress_started[key] = now
        self.last_progress = {"agent": agent, "ticker": ticker, "status": status}
        fields = {**self.last_progress}
        if previous is not None:
            fields["step_elapsed_ms"] = round((now - previous) * 1000)
        self.emit("analyst_progress", **fields)

    def finish(self, status: str, **fields) -> None:
        now = time.monotonic()
        if self.phase_name is not None and self.phase_started is not None:
            self.emit(
                "phase_completed",
                phase=self.phase_name,
                duration_ms=round((now - self.phase_started) * 1000),
            )
        self.emit("run_finished", status=status, **fields)


def _resolve_openclaw_model(fallback: str = "qwen3.5:397b-cloud") -> str:
    """Resolve the configured OpenClaw model for this scheduled analysis."""
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        agents = config.get("agents", {})
        for agent in agents.get("list", []):
            if agent.get("id") == "drox":
                model = agent["model"]["primary"]
                return model.split("/", 1)[-1] if "/" in model else model
        model = agents.get("defaults", {}).get("model", {}).get("primary", "")
        if model:
            return model.split("/", 1)[-1] if "/" in model else model
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return fallback


def _json_fallback(value: object):
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def _json_safe(value: object):
    """Normalize analyst diagnostics before emitting strict JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (OverflowError, TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except (TypeError, ValueError):
            pass
    return str(value)


def _decision_payload(tickers: list[str], raw_decisions: object) -> dict[str, dict]:
    if not isinstance(raw_decisions, dict):
        raise ValueError("Analyst graph returned no decision object")
    normalized: dict[str, dict] = {}
    for ticker in tickers:
        decision = raw_decisions.get(ticker, raw_decisions.get(ticker.upper(), {}))
        if not isinstance(decision, dict):
            decision = {}
        raw_action = str(decision.get("action", "watch")).strip().lower()
        action = raw_action if raw_action in {"buy", "sell"} else "watch"
        raw_confidence = decision.get("confidence", 0)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        confidence = max(0.0, min(100.0, confidence))
        reason = decision.get("reasoning")
        if not isinstance(reason, str) or not reason.strip():
            reason = "No evidence-based reason was returned; watch only."
            action = "watch"
        normalized[ticker] = {
            "action": action,
            "confidence": confidence,
            "reason": reason.strip()[:1200],
        }
    return normalized


def run_analysis(
    *,
    output_path: str | Path = DEFAULT_AI_SIGNAL_DB,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> dict:
    global _ACTIVE_DIAGNOSTICS

    if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or not 5 <= ttl_minutes <= 240:
        raise ValueError("ttl_minutes must be an integer between 5 and 240")

    started_at = datetime.now(timezone.utc)
    run_id = f"llm-driven-{started_at.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    diagnostics = _AnalysisDiagnostics(output_path, run_id)
    _ACTIVE_DIAGNOSTICS = diagnostics
    diagnostics.emit("run_started")
    try:
        diagnostics.phase("load_universe")
        universe_result = load_algo_universe()
        universe = universe_result["tickers"]
        if not universe:
            raise ValueError("The live Algo universe is empty")
        diagnostics.phase(
            "load_broker_portfolio",
            universe_count=len(universe),
            universe_source=universe_result.get("source", "unknown"),
            selector_as_of=universe_result.get("selector_as_of"),
        )
        account = get_alpaca_account(mode="algo")
        positions = get_alpaca_positions(mode="algo")
        portfolio = convert_to_portfolio(positions, account, universe)
        # Owned longs are reviewed even outside the live universe, so their SELL can be decided.
        owned_positions = owned_long_positions(positions)
        owned_tickers = [item["ticker"] for item in owned_positions]
        analysis_tickers = list(dict.fromkeys([*universe, *owned_tickers]))

        # The analyst workflow only produces evidence-based recommendations.
        # Its suggested share quantities are intentionally discarded downstream.
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=90)
        model_name = os.getenv("LLM_MODEL") or _resolve_openclaw_model()
        model_provider = os.getenv("LLM_PROVIDER", "Ollama")
        diagnostics.phase("load_market_context")
        try:
            ticker_data, market_data_diagnostics = _fetch_market_regime_context()
        except Exception as exc:
            ticker_data = {}
            market_data_diagnostics = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
                "symbols": {},
            }
        diagnostics.phase(
            "analyst_graph",
            universe_count=len(universe),
            analyst_count=len(ANALYSTS),
            model_provider=model_provider,
            model_name=model_name,
            market_data_status=market_data_diagnostics.get("status"),
        )

        progress.register_handler(diagnostics.on_progress)
        try:
            with redirect_stdout(io.StringIO()):
                result = run_event_analysis(
                    tickers=analysis_tickers,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                    portfolio=portfolio,
                    show_reasoning=False,
                    selected_analysts=ANALYSTS,
                    ticker_data=ticker_data,
                    model_name=model_name,
                    model_provider=model_provider,
                )
        finally:
            progress.unregister_handler(diagnostics.on_progress)

        diagnostics.phase("validate_decisions")
        if not isinstance(result, dict):
            raise ValueError("Analyst graph returned an invalid result")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=ttl_minutes)
        decisions = _decision_payload(analysis_tickers, result.get("decisions"))
        analyst_signals = result.get("analyst_signals")
        if not isinstance(analyst_signals, dict):
            analyst_signals = {}

        snapshot = {
            "schema_version": 1,
            "strategy": "swing_llm_driven",
            "run_id": run_id,
            "generated_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "universe": analysis_tickers,
            "owned_tickers": owned_tickers,
            "owned_positions": [
                {**item, "decision": decisions[item["ticker"]]["action"], "confidence": decisions[item["ticker"]]["confidence"]}
                for item in owned_positions
            ],
            "decisions": decisions,
        }
        diagnostics.phase("write_snapshot")
        publish_llm_signal_snapshot(output_path, snapshot)

        market_regime = analyst_signals.get("market_regime_agent", {})
        news = analyst_signals.get("news_sentiment_agent", {})
        diagnostics.finish("ready")
        report = {
            "status": "ready",
            "artifact": str(Path(output_path)),
            "diagnostics": str(diagnostics.path),
            "run_id": snapshot["run_id"],
            "generated_at": snapshot["generated_at"],
            "expires_at": snapshot["expires_at"],
            "universe_count": len(universe),
            "universe_source": universe_result.get("source", "unknown"),
            "selector_as_of": universe_result.get("selector_as_of"),
            "universe": universe,
            "market_data_diagnostics": market_data_diagnostics,
            "market_regime": _json_safe(market_regime),
            "news_sentiment": _json_safe(news),
            "decisions": decisions,
            "analysts": ANALYSTS,
        }
        return report
    except Exception as exc:
        diagnostics.finish("failed", error_type=type(exc).__name__)
        raise
    finally:
        _ACTIVE_DIAGNOSTICS = None


def _handle_sigterm(signum, _frame) -> None:
    diagnostics = _ACTIVE_DIAGNOSTICS
    if diagnostics is not None:
        diagnostics.finish(
            "interrupted",
            signal=signal.Signals(signum).name,
            last_progress=diagnostics.last_progress,
        )
        print(
            f"llm_driven AI analysis interrupted by {signal.Signals(signum).name}; "
            f"last stage={diagnostics.last_progress}; diagnostics={diagnostics.path}",
            file=sys.stderr,
            flush=True,
        )
    raise SystemExit(128 + signum)

def main() -> int:
    parser = argparse.ArgumentParser(description="Create scheduled AI recommendations for Algo llm_driven")
    parser.add_argument("--output", type=Path, default=DEFAULT_AI_SIGNAL_DB, help="signal snapshot database")
    parser.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES)
    args = parser.parse_args()
    previous_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        report = run_analysis(output_path=args.output, ttl_minutes=args.ttl_minutes)
    except Exception as exc:
        print(f"llm_driven AI analysis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    print(json.dumps(_json_safe(report), ensure_ascii=False, default=_json_fallback, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
