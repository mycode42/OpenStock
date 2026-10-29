from datetime import date, datetime, timezone

import pytest

from options.costs import OptionsCostModel, adverse_slippage_per_share
from options.data import ChainQuote
from options.models import GreeksSnapshot, OptionContract, OptionLeg
from options.research.execution import ExecutionAssumptions
from options.structures import structure_limit_price, vertical_spread

FLOOR = 5.0
CAP = 25.0


def _leg(symbol: str, right: str, strike: float, side: str, bid: float, ask: float) -> OptionLeg:
    contract = OptionContract(
        symbol=symbol, underlying="SPY", expiration=date(2027, 1, 15),
        right=right, strike=strike, exercise_style="american",
    )
    return OptionLeg(contract, side, bid=bid, ask=ask, greeks=GreeksSnapshot(delta=0.5))


def _quote(leg: OptionLeg) -> ChainQuote:
    return ChainQuote(
        contract=leg.contract, bid=leg.bid, ask=leg.ask, greeks=leg.greeks,
        observed_at=datetime(2027, 1, 1, 14, tzinfo=timezone.utc), feed="opra",
    )


def test_tight_leg_is_floored_at_flat_slippage():
    # 0.02 spread x 0.5 x 100 = $1 per contract, floored to $5 => $0.05 per share.
    assert adverse_slippage_per_share(1.00, 1.02, fraction=0.5, floor_per_contract_usd=FLOOR,
                                      cap_per_contract_usd=CAP) == pytest.approx(0.05)


def test_mid_leg_scales_with_spread_between_floor_and_cap():
    # 0.20 spread x 0.5 x 100 = $10 per contract => $0.10 per share.
    assert adverse_slippage_per_share(1.00, 1.20, fraction=0.5, floor_per_contract_usd=FLOOR,
                                      cap_per_contract_usd=CAP) == pytest.approx(0.10)


def test_wide_leg_is_capped_at_configured_maximum():
    # 1.00 spread x 0.5 x 100 = $50 per contract, capped to $25 => $0.25 per share.
    assert adverse_slippage_per_share(1.00, 2.00, fraction=0.5, floor_per_contract_usd=FLOOR,
                                      cap_per_contract_usd=CAP) == pytest.approx(0.25)


def test_live_structure_limit_and_risk_estimate_use_per_leg_spreads():
    costs = OptionsCostModel(slippage_per_contract_usd=FLOOR, max_slippage_per_contract_usd=CAP)
    tight = _leg("SPY270115C00100000", "call", 100, "buy", 4.00, 4.02)
    wide = _leg("SPY270115C00105000", "call", 105, "sell", 1.00, 2.00)
    structure = vertical_spread(tight, wide, name="test")

    # Tight buy leg adds $0.05; wide sell leg adds $0.25 (capped), so the limit rises by $0.30.
    assert structure_limit_price(structure, costs=costs) == round(structure.net_debit_per_share + 0.30, 2)
    # Estimate: commission 0.65 each leg, plus $5 floor on the tight leg and $25 cap on the wide leg.
    assert costs.estimate(structure, 1) == pytest.approx(2 * 0.65 + 5.0 + 25.0)


def test_research_fills_and_live_limits_call_the_same_slippage_function():
    costs = OptionsCostModel(slippage_per_contract_usd=FLOOR, max_slippage_per_contract_usd=CAP)
    execution = ExecutionAssumptions.from_dict({
        "spread_fraction_per_leg": 0.5, "slippage_per_contract_usd": FLOOR,
        "max_slippage_per_contract_usd": CAP,
    })
    for leg in (_leg("SPY270115C00100000", "call", 100, "buy", 4.00, 4.02),
                _leg("SPY270115C00105000", "call", 105, "buy", 1.00, 2.00)):
        quote = _quote(leg)
        midpoint = quote.midpoint
        live = costs.leg_slippage_per_share(leg.bid, leg.ask, multiplier=leg.contract.multiplier)
        assert execution.entry_fill(quote, "buy") == pytest.approx(midpoint + live)
        # A short leg buys to close, so the adverse move is above the midpoint.
        assert execution.exit_fill(quote, -1) == pytest.approx(midpoint + live)
        assert execution.exit_fill(quote, 1) == pytest.approx(midpoint - live)


def test_invalid_slippage_inputs_are_rejected():
    with pytest.raises(ValueError):
        adverse_slippage_per_share(2.0, 1.0, fraction=0.5, floor_per_contract_usd=FLOOR, cap_per_contract_usd=CAP)
    with pytest.raises(ValueError):
        adverse_slippage_per_share(1.0, 1.1, fraction=0.5, floor_per_contract_usd=30.0, cap_per_contract_usd=CAP)
