"""Optional OpenAI tie-breaker for deterministic regime rankings.

The deterministic regime filter is authoritative. The advisor can only reorder
strategies already marked active; it cannot activate rules or submit orders.
"""

from __future__ import annotations

import json
import logging
import os
import time
from functools import lru_cache

from openai import OpenAI, OpenAIError

from src.config import STRATEGY_CONFIG


LOG = logging.getLogger("strategy_regime_advisor")


def _enabled() -> bool:
    raw = os.getenv(
        "STRATEGY_REGIME_LLM_ENABLED",
        str(STRATEGY_CONFIG.get("regime_llm_advisor_enabled", False)),
    )
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=4)
def _openai_client(base_url: str, timeout_seconds: int) -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    options = {"api_key": api_key, "timeout": timeout_seconds, "max_retries": 0}
    if base_url:
        options["base_url"] = base_url
    return OpenAI(**options)


@lru_cache(maxsize=32)
def _openai_suggestion(
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
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return [], "OPENAI_API_KEY is not configured"

    prompt = (
        f"Deterministic regime: {regime}.\n"
        f"Regime reason: {reason}.\n"
        f"SPY 20-session return: {return_20d}.\n"
        f"SPY 20-session daily volatility: {volatility_20d}.\n"
        f"Deterministic strategy scores: {json.dumps(ranked, separators=(',', ':'))}.\n"
        f"Allowed active strategies: {json.dumps(active)}."
    )
    schema = {
        "type": "object",
        "properties": {
            "ranking": {
                "type": "array",
                "items": {"type": "string", "enum": active},
            },
        },
        "required": ["ranking"],
        "additionalProperties": False,
    }
    try:
        client = _openai_client(base_url, timeout_seconds)
        response = client.responses.create(
            model=model,
            instructions=(
                "Rank the supplied active strategies for the supplied regime. "
                "Return each allowed strategy at most once. Do not enable any strategy."
            ),
            input=prompt,
            reasoning={"effort": "none"},
            max_output_tokens=64,
            store=False,
            text={"format": {
                "type": "json_schema",
                "name": "strategy_ranking",
                "strict": True,
                "schema": schema,
            }},
        )
        parsed = json.loads(response.output_text)
        proposed = parsed.get("ranking", []) if isinstance(parsed, dict) else []
        if not isinstance(proposed, list):
            return [], "model response did not contain a ranking list"
        if any(not isinstance(item, str) or item not in active for item in proposed):
            return [], "model response included a strategy blocked by deterministic regime checks"
        if len(set(proposed)) != len(proposed):
            return [], "model response repeated a strategy"
        accepted = list(proposed)
        if not accepted:
            return [], "model response contained no allowed active strategy"
        return accepted + [name for name in active if name not in accepted], None
    except OpenAIError as exc:
        # SDK error messages can include credential fragments; log only the class.
        return [], type(exc).__name__
    except (ValueError, TypeError, AttributeError) as exc:
        return [], type(exc).__name__
    except Exception as exc:
        # Keep advisor/network failures strictly advisory; deterministic rules
        # remain available even if a provider raises outside the SDK errors.
        return [], type(exc).__name__


def apply_regime_advisory(regime: dict) -> dict:
    """Attach an OpenAI advisory order without changing the active strategy set."""
    active = list(regime.get("active", []))
    enabled = _enabled()
    regime["regime_advisory"] = {
        "enabled": enabled,
        "provider": "openai",
        "model": os.getenv("LLM_MODEL", STRATEGY_CONFIG["regime_llm_default_model"]).strip(),
        "status": "disabled" if not enabled else "not_applicable",
        "suggested_order": [],
        "applied_order": active,
    }
    if not active or not enabled:
        return regime

    provider = os.getenv("LLM_PROVIDER", "OpenAI").strip().lower()
    model = os.getenv("LLM_MODEL", STRATEGY_CONFIG["regime_llm_default_model"]).strip()
    if provider != "openai":
        suggestion, error = [], "LLM_PROVIDER must be OpenAI for the regime advisor"
    else:
        available_rankings = [
            {"strategy": item.get("strategy"), "score": item.get("score")}
            for item in regime.get("ranked", [])
            if item.get("strategy") in active
        ]
        try:
            suggestion, error = _openai_suggestion(
                os.getenv("OPENAI_API_BASE", "").strip(),
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
        except Exception as exc:
            suggestion, error = [], type(exc).__name__
    if error:
        regime["regime_advisory"].update({"status": "fallback", "error": error})
        LOG.warning("OpenAI regime suggestion unavailable; deterministic order retained: %s", error)
        return regime

    deterministic_active = list(regime.get("active", []))
    # Validate again at the boundary so a malformed provider, test double, or
    # future client cannot revive a strategy blocked by the deterministic gate.
    if (
        not isinstance(suggestion, list)
        or any(not isinstance(name, str) or name not in deterministic_active for name in suggestion)
        or len(set(suggestion)) != len(suggestion)
    ):
        regime["regime_advisory"].update({
            "status": "fallback",
            "error": "advisor ranking included a strategy blocked by deterministic regime checks",
        })
        LOG.warning("Invalid OpenAI regime ranking; deterministic allowed set retained")
        return regime
    suggestion = suggestion + [name for name in deterministic_active if name not in suggestion]
    priority = {name: len(suggestion) - index for index, name in enumerate(suggestion)}
    regime["active"] = suggestion
    regime["regime_advisory"].update({
        "status": "applied",
        "suggested_order": suggestion,
        "applied_order": suggestion,
    })
    LOG.info("OpenAI regime ranking applied within deterministic allowed set: %s", suggestion)
    for item in regime.get("ranked", []):
        name = item.get("strategy")
        if name in priority:
            item["selection_score"] = float(priority[name])
    return regime


__all__ = ["apply_regime_advisory"]
