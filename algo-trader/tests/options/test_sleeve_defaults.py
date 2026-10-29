from options.config import load_options_config
from strategies.options.long_volatility import long_volatility_exit_reason


def test_default_config_does_not_close_on_regime_exit(tmp_path):
    config = load_options_config(tmp_path / "missing.json")

    assert config["long_volatility"]["close_on_regime_exit"] is False


def test_regime_change_does_not_close_unless_enabled(tmp_path):
    lv = dict(load_options_config(tmp_path / "missing.json")["long_volatility"])
    kwargs = dict(
        entry_debit=1.0, current_value=1.0, held_days=1, days_to_expiry=20,
        regime="trending_up", entry_regime="volatile",
    )

    assert long_volatility_exit_reason(config=lv, **kwargs) is None
    assert long_volatility_exit_reason(config={**lv, "close_on_regime_exit": True}, **kwargs) == "regime_left_volatile"
    assert long_volatility_exit_reason(config={}, **kwargs) is None


def test_llm_driven_section_replaces_event_driven_key(tmp_path):
    config = load_options_config(tmp_path / "missing.json")

    assert "event_driven" not in config
    assert config["llm_driven"]["min_llm_confidence"] == 0.70
    assert config["llm_driven"]["enabled"] is True


def test_llm_driven_strategy_identity():
    from strategies.options import LlmDrivenOptionsStrategy

    assert LlmDrivenOptionsStrategy.strategy_id == "options_llm_driven"


def test_earnings_blackout_is_removed_from_eligibility_rules():
    import json
    from pathlib import Path

    rules = json.loads(
        (Path(__file__).resolve().parents[2] / "options/research/eligibility.default.json").read_text(encoding="utf-8")
    )

    assert "earnings_blackout" not in rules
    assert not hasattr(__import__("options.research.universe_selection", fromlist=["x"]), "_earnings_reasons")
