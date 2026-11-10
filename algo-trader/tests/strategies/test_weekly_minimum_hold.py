from strategies.rule_pipeline import _held_too_short_to_exit
from strategies.signals import Signal


def _signal(action):
    return Signal("AAPL", action, "weekly_momentum", "momentum or 20-bar trend turned negative", 0.65)


def _bars(now):
    return [{"t": now, "c": 100.0}]


def test_exit_is_ignored_inside_the_minimum_hold_and_allowed_after_it():
    entry = "2026-11-02T14:30:00+00:00"  # Monday
    params = {"minimum_hold_sessions": 3}
    # Wednesday is two sessions after entry: still inside a three-session minimum.
    assert _held_too_short_to_exit(_signal("sell"), params, 10, entry, _bars("2026-11-04T15:00:00+00:00")) is True
    # Thursday is three sessions after entry: the exit may act.
    assert _held_too_short_to_exit(_signal("sell"), params, 10, entry, _bars("2026-11-05T15:00:00+00:00")) is False


def test_default_and_entries_are_unaffected():
    entry = "2026-11-02T14:30:00+00:00"
    now = _bars("2026-11-03T15:00:00+00:00")
    assert _held_too_short_to_exit(_signal("sell"), {}, 10, entry, now) is False
    assert _held_too_short_to_exit(_signal("buy"), {"minimum_hold_sessions": 3}, 0, entry, now) is False
    assert _held_too_short_to_exit(None, {"minimum_hold_sessions": 3}, 10, entry, now) is False


def test_short_positions_are_covered_by_the_same_rule():
    entry = "2026-11-02T14:30:00+00:00"
    assert _held_too_short_to_exit(_signal("cover"), {"minimum_hold_sessions": 2}, -10, entry, _bars("2026-11-03T15:00:00+00:00")) is True
