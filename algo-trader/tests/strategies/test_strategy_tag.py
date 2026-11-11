import re

from strategies.catalog import strategy_tag


def test_tag_keeps_the_strategy_name_in_a_broker_safe_form():
    assert strategy_tag("intraday_trend_following") == "intradaytrendfollowing"
    assert strategy_tag("swing_mean_reversion") == "swingmeanreversion"
    assert re.fullmatch(r"[a-z0-9]{1,24}", strategy_tag("x" * 80))


def test_missing_strategy_is_marked_unknown():
    assert strategy_tag(None) == "unknown"
    assert strategy_tag("") == "unknown"
