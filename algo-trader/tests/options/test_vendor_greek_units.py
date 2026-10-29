import json
from datetime import date
from pathlib import Path

import pytest

from options import data as options_data
from options.pricing import model_greeks

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "options" / "alpaca_spy_snapshot_2026-10-27.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_vendor_scale_constants_match_recorded_ratios():
    assert options_data.VENDOR_VEGA_PER_VOL_POINT_SCALE == 100.0
    assert options_data.VENDOR_RHO_PER_RATE_POINT_SCALE == 100.0
    assert options_data.VENDOR_THETA_PER_DAY_SCALE == 1.0


@pytest.mark.parametrize("contract", _fixture()["contracts"], ids=lambda c: c["symbol"])
def test_converted_vendor_greeks_match_model_values(contract):
    fixture = _fixture()
    as_of = date.fromisoformat(fixture["captured_on"])
    expiration = date.fromisoformat(contract["expiration"])
    years = (expiration - as_of).days / 365.0
    model = model_greeks(
        fixture["spot"], contract["strike"], years, fixture["rate"], contract["implied_volatility"],
        contract["right"], dividend_yield=fixture["dividend_yield"], american=False,
    )
    vendor = contract["greeks"]

    # Vendor vega and rho are per 1% move; converted values must match the per-unit model.
    assert vendor["vega"] * options_data.VENDOR_VEGA_PER_VOL_POINT_SCALE == pytest.approx(model.vega, rel=0.02)
    assert vendor["rho"] * options_data.VENDOR_RHO_PER_RATE_POINT_SCALE == pytest.approx(model.rho, rel=0.02)
    # Vendor theta is already per calendar day.
    assert vendor["theta"] * options_data.VENDOR_THETA_PER_DAY_SCALE == pytest.approx(model.theta, rel=0.03, abs=0.002)
    assert vendor["delta"] == pytest.approx(model.delta, abs=0.01)
