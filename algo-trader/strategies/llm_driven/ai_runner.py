"""Run a read-only analyst pass and publish Algo LLM-driven recommendations."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4

from strategies.llm_driven.ai import DEFAULT_AI_SIGNAL_FILE, write_llm_signal_snapshot
from strategies.universe import configured_universe
from broker.portfolio import convert_to_portfolio, get_alpaca_account, get_alpaca_positions
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
    output_path: str | Path = DEFAULT_AI_SIGNAL_FILE,
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
        universe = configured_universe("algo")["tickers"]
        diagnostics.phase("load_broker_portfolio", universe_count=len(universe))
        account = get_alpaca_account(mode="algo")
        positions = get_alpaca_positions(mode="algo")
        portfolio = convert_to_portfolio(positions, account, universe)

        # The analyst workflow only produces evidence-based recommendations.
        # Its suggested share quantities are intentionally discarded downstream.
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=90)
        model_name = os.getenv("LLM_MODEL") or _resolve_openclaw_model()
        model_provider = os.getenv("LLM_PROVIDER", "Ollama")
        diagnostics.phase(
            "analyst_graph",
            universe_count=len(universe),
            analyst_count=len(ANALYSTS),
            model_provider=model_provider,
            model_name=model_name,
        )

        progress.register_handler(diagnostics.on_progress)
        try:
            with redirect_stdout(io.StringIO()):
                result = run_event_analysis(
                    tickers=universe,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                    portfolio=portfolio,
                    show_reasoning=False,
                    selected_analysts=ANALYSTS,
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
        decisions = _decision_payload(universe, result.get("decisions"))
        analyst_signals = result.get("analyst_signals")
        if not isinstance(analyst_signals, dict):
            analyst_signals = {}

        snapshot = {
            "schema_version": 1,
            "strategy": "swing_llm_driven",
            "run_id": run_id,
            "generated_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "universe": universe,
            "decisions": decisions,
        }
        diagnostics.phase("write_snapshot")
        write_llm_signal_snapshot(output_path, snapshot)

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
    parser.add_argument("--output", type=Path, default=DEFAULT_AI_SIGNAL_FILE)
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
