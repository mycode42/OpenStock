from src.strategies.regime_advisor import apply_regime_advisory


def test_advisor_cannot_revive_strategy_blocked_by_spy_filter(monkeypatch):
    monkeypatch.setenv("STRATEGY_REGIME_LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "OpenAI")
    monkeypatch.setenv("LLM_MODEL", "gpt-6-luna")
    monkeypatch.setattr(
        "src.strategies.regime_advisor._openai_suggestion",
        lambda *args, **kwargs: (["trend_following", "event_driven"], None),
    )
    deterministic = {
        "regime": "trending_up",
        "active": ["trend_following", "momentum"],
        "satellites": ["event_driven"],
        "ranked": [
            {"strategy": "trend_following", "score": 1.0},
            {"strategy": "momentum", "score": 0.8},
        ],
    }

    result = apply_regime_advisory(deterministic)

    assert result["active"] == ["trend_following", "momentum"]
    assert result["regime_advisory"]["status"] == "fallback"
    assert "blocked" in result["regime_advisory"]["error"]


def test_advisor_timeout_falls_back_to_the_deterministic_active_set(monkeypatch):
    monkeypatch.setenv("STRATEGY_REGIME_LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "OpenAI")
    monkeypatch.setattr(
        "src.strategies.regime_advisor._openai_suggestion",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    deterministic = {
        "regime": "trending_down", "active": ["trend_following", "momentum"],
        "ranked": [],
    }

    result = apply_regime_advisory(deterministic)

    assert result["active"] == ["trend_following", "momentum"]
    assert result["regime_advisory"]["status"] == "fallback"
    assert result["regime_advisory"]["error"] == "TimeoutError"
