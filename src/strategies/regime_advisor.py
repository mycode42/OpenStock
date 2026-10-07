"""Optional local LLM tie-breaker for deterministic regime rankings.

The deterministic regime filter is authoritative. This advisor can only
reorder strategies already marked active; it cannot activate disabled rules,
generate a ticker signal, or call an execution component.
"""

from __future__ import annotations

import json
import logging
import os
import time
from functools import lru_cache

import requests

from src.config import STRATEGY_CONFIG


LOG = logging.getLogger("strategy_regime_advisor")


def _enabled() -> bool:
    raw = os.getenv(
        "STRATEGY_REGIME_LLM_ENABLED",
        str(STRATEGY_CONFIG.get("regime_llm_advisor_enabled", False)),
    )
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=32)
def _ollama_suggestion(
    base_url: str,
    model: str,
    regime: str,
    reason: str,
    active_json: str,
    ranked_json: str,
    return_20d: float | None,
    volatility_20d: float | None,
    timeout_seconds: int,
    cache_bucket: int,
) -> tuple[list[str], str | None]:
    active = json.loads(active_json)
    ranked = json.loads(ranked_json)
    prompt = (
        "You are a regime-ranking advisor for a long-only, rule-based trading system. "
        "Do not invent or enable strategies. Return only JSON with key ranking, "
        "an ordering of the supplied active strategy names.\n"
        f"Deterministic regime: {regime}.\n"
        f"Regime reason: {reason}.\n"
        f"SPY 20-session return: {return_20d}.\n"
        f"SPY 20-session daily volatility: {volatility_20d}.\n"
        f"Deterministic strategy scores: {json.dumps(ranked, separators=(',', ':'))}.\n"
        f"Allowed active strategies: {json.dumps(active)}.\n"
        'Return format: {"ranking":["strategy_name", ...]}.'
    )
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0, "num_predict": 80},
            },
            timeout=(1.0, max(1, timeout_seconds)),
        )
        response.raise_for_status()
        output = response.json().get("response", "")
        parsed = json.loads(output) if isinstance(output, str) else output
        proposed = parsed.get("ranking", []) if isinstance(parsed, dict) else []
        if not isinstance(proposed, list):
            return [], "model response did not contain a ranking list"
        accepted = []
        for item in proposed:
            if isinstance(item, str) and item in active and item not in accepted:
                accepted.append(item)
        if not accepted:
            return [], "model response contained no allowed active strategy"
        return accepted + [name for name in active if name not in accepted], None
    except (requests.RequestException, ValueError, TypeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"


def apply_regime_advisory(regime: dict) -> dict:
    """Attach an advisory order without changing the deterministic active set."""
    active = list(regime.get("active", []))
    regime["regime_advisory"] = {
        "enabled": _enabled(),
        "provider": "ollama",
        "status": "disabled" if not _enabled() else "not_applicable",
        "suggested_order": [],
        "applied_order": active,
    }
    if not active or not _enabled():
        return regime

    model = os.getenv(
        "STRATEGY_REGIME_LLM_MODEL",
        str(STRATEGY_CONFIG["regime_llm_default_model"]),
    ).strip()
    host = os.getenv("OLLAMA_HOST", "localhost")
    base_url = os.getenv("OLLAMA_BASE_URL", f"http://{host}:11434").strip()
    available_rankings = [
        {"strategy": item.get("strategy"), "score": item.get("score")}
        for item in regime.get("ranked", [])
        if item.get("strategy") in active
    ]
    suggestion, error = _ollama_suggestion(
        base_url,
        model,
        str(regime.get("regime", "unknown")),
        str(regime.get("reason", "")),
        json.dumps(active, separators=(",", ":")),
        json.dumps(available_rankings, separators=(",", ":")),
        regime.get("spy_return_20d"),
        regime.get("spy_volatility_20d"),
        int(STRATEGY_CONFIG["regime_llm_timeout_seconds"]),
        int(time.time() // 900),
    )
    if error:
        regime["regime_advisory"].update({"status": "fallback", "error": error})
        LOG.warning("Local regime suggestion unavailable; deterministic strategy order retained: %s", error)
        return regime

    priority = {name: len(suggestion) - index for index, name in enumerate(suggestion)}
    regime["active"] = suggestion
    regime["regime_advisory"].update({
        "status": "applied",
        "suggested_order": suggestion,
        "applied_order": suggestion,
    })
    for item in regime.get("ranked", []):
        name = item.get("strategy")
        if name in priority:
            item["selection_score"] = float(priority[name])
    return regime


__all__ = ["apply_regime_advisory"]
