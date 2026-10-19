"""Run the Swarm analyst workflow and publish Algo event recommendations."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import math
import os
from pathlib import Path
import sys
from uuid import uuid4

from src.algo.strategies.event_driven.ai import DEFAULT_AI_SIGNAL_FILE, write_ai_event_snapshot
from src.algo.strategies.universe import configured_universe
from src.alpaca_integration import convert_to_portfolio, get_alpaca_account, get_alpaca_positions
from src.main import run_hedge_fund


ANALYSTS = [
    "market_regime",
    "news_sentiment_analyst",
    "sentiment_analyst",
    "technical_analyst",
    "fundamentals_analyst",
    "growth_analyst",
]
DEFAULT_TTL_MINUTES = 90


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
        raise ValueError("Swarm analysts returned no decision object")
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
    output_path: str | Path = DEFAULT_AI_SIGNAL_FILE,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> dict:
    if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or not 5 <= ttl_minutes <= 240:
        raise ValueError("ttl_minutes must be an integer between 5 and 240")

    universe = configured_universe("algo")["tickers"]
    account = get_alpaca_account(mode="algo")
    positions = get_alpaca_positions(mode="algo")
    portfolio = convert_to_portfolio(positions, account, universe)

    # The legacy analyst workflow is reused as an analysis-only library call.
    # Its suggested share quantities are intentionally discarded downstream.
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=90)
    model_name = os.getenv("LLM_MODEL")
    if not model_name:
        from run_hedge_fund import _resolve_openclaw_model
        model_name = _resolve_openclaw_model()
    model_provider = os.getenv("LLM_PROVIDER", "Ollama")

    with redirect_stdout(io.StringIO()):
        result = run_hedge_fund(
            tickers=universe,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            portfolio=portfolio,
            show_reasoning=False,
            selected_analysts=ANALYSTS,
            model_name=model_name,
            model_provider=model_provider,
        )

    if not isinstance(result, dict):
        raise ValueError("Swarm analyst workflow returned an invalid result")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes)
    decisions = _decision_payload(universe, result.get("decisions"))
    analyst_signals = result.get("analyst_signals")
    if not isinstance(analyst_signals, dict):
        analyst_signals = {}

    snapshot = {
        "schema_version": 1,
        "strategy": "event_driven",
        "run_id": f"event-driven-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
        "generated_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "universe": universe,
        "decisions": decisions,
    }
    write_ai_event_snapshot(output_path, snapshot)

    market_regime = analyst_signals.get("market_regime_agent", {})
    news = analyst_signals.get("news_sentiment_agent", {})
    report = {
        "status": "ready",
        "artifact": str(Path(output_path)),
        "run_id": snapshot["run_id"],
        "generated_at": snapshot["generated_at"],
        "expires_at": snapshot["expires_at"],
        "universe_count": len(universe),
        "market_regime": _json_safe(market_regime),
        "news_sentiment": _json_safe(news),
        "decisions": decisions,
        "analysts": ANALYSTS,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Create scheduled AI recommendations for Algo event_driven")
    parser.add_argument("--output", type=Path, default=DEFAULT_AI_SIGNAL_FILE)
    parser.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES)
    args = parser.parse_args()
    try:
        report = run_analysis(output_path=args.output, ttl_minutes=args.ttl_minutes)
    except Exception as exc:
        print(f"event_driven AI analysis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(_json_safe(report), ensure_ascii=False, default=_json_fallback, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
