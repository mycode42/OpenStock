import pytest

from src.strategies.satellite_overlay import SatelliteOverlayConfig, apply_satellite_confirmations


def test_satellites_amplify_a_core_entry_without_owning_it():
    signal = {
        "ticker": "AAPL", "action": "buy", "strategy": "momentum",
        "strength": 0.7, "rank_score": 0.9,
    }

    result = apply_satellite_confirmations(
        signal,
        factor_signal={"action": "buy", "direction": "long", "rank": 2, "score": 1.1},
        event_signal={"action": "buy", "event_id": "cpi-2026-10", "reason": "post-release continuation"},
    )

    assert result["strategy"] == "momentum"
    assert result["satellite_confirmations"] == ["multi_factor", "event_driven"]
    assert result["risk_multiplier"] == 1.2
    assert result["strength"] == pytest.approx(0.88)
    assert result["event_id"] == "cpi-2026-10"


def test_satellites_cannot_create_standalone_positions():
    signal = {"ticker": "AAPL", "action": "buy", "strategy": "multi_factor", "strength": 0.8}

    result = apply_satellite_confirmations(
        signal, event_signal={"action": "buy", "event_id": "fomc-2026"},
    )

    assert result == signal


def test_overlay_disable_flags_leave_core_sizing_unchanged():
    signal = {"ticker": "AAPL", "action": "buy", "strategy": "momentum", "strength": 0.7}
    config = SatelliteOverlayConfig.from_dict({
        "multi_factor_enabled": False, "event_driven_enabled": False,
    })

    result = apply_satellite_confirmations(
        signal,
        factor_signal={"action": "buy", "direction": "long"},
        event_signal={"action": "buy"},
        config=config,
    )

    assert result == signal
