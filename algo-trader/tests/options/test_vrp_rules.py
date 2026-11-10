from strategies.options.vrp import iv_rank_allows, vrp_exit_reason


def test_iv_rank_gate_is_off_without_a_minimum_and_fails_closed_without_a_percentile():
    assert iv_rank_allows(None, None) is True
    assert iv_rank_allows(10.0, None) is True
    assert iv_rank_allows(None, 50) is False
    assert iv_rank_allows(49.9, 50) is False
    assert iv_rank_allows(50.0, 50) is True


def test_no_stop_or_holding_limit_unless_configured():
    assert vrp_exit_reason(entry_credit=100.0, pnl_if_closed=-500.0, held_days=90, config={}) is None


def test_credit_multiple_stop_closes_at_the_configured_loss():
    config = {"stop_loss_credit_multiple": 2.0}
    assert vrp_exit_reason(entry_credit=100.0, pnl_if_closed=-199.0, held_days=3, config=config) is None
    assert vrp_exit_reason(entry_credit=100.0, pnl_if_closed=-200.0, held_days=3, config=config) == "structure_stop_loss"


def test_holding_limit_and_missing_values():
    config = {"max_holding_days": 21}
    assert vrp_exit_reason(entry_credit=100.0, pnl_if_closed=10.0, held_days=21, config=config) == "maximum_holding_period"
    assert vrp_exit_reason(entry_credit=0.0, pnl_if_closed=-500.0, held_days=30, config=config) is None
    assert vrp_exit_reason(entry_credit=100.0, pnl_if_closed=None, held_days=30, config=config) is None
