from __future__ import annotations

from datetime import date, timedelta

from src.algo.regime_macro import parse_fred_csv
from src.algo.market_store import SQLiteBarStore
from src.algo.strategies.regime import _raw_rule_states, _strategy_weights
from src.algo.strategies.regime_features import compute_regime_feature_history
from src.algo.strategies.regime_models import _regime_for_state, infer_regime_model


def _daily_bars(count: int, *, base: float = 100.0, drift: float = 0.2, phase: float = 0.0) -> list[dict]:
    start = date(2020, 1, 1)
    rows = []
    price = base
    for index in range(count):
        move = drift + ((index % 7) - 3) * 0.08 + phase
        opening = price
        price = max(1.0, price + move)
        rows.append({
            "t": (start + timedelta(days=index)).isoformat(),
            "o": opening, "h": max(opening, price) + 0.5,
            "l": min(opening, price) - 0.5, "c": price, "v": 100_000 + index,
        })
    return rows


def test_features_are_causal_and_adx_seed_does_not_use_next_bar():
    spy = _daily_bars(90)
    context = {"SPY": spy, "QQQ": _daily_bars(90, base=200, drift=0.35)}
    macro = {
        "vix": [{"date": row["t"], "value": 16 + (index % 5)} for index, row in enumerate(spy)],
        "vix3m": [{"date": row["t"], "value": 19} for row in spy],
    }
    full = compute_regime_feature_history(spy, context_bars=context, macro_series=macro)
    prefix = compute_regime_feature_history(
        spy[:45], context_bars={name: rows[:45] for name, rows in context.items()},
        macro_series={name: rows[:45] for name, rows in macro.items()},
    )
    assert full[:45] == prefix
    assert full[26]["adx_14"] is not None
    assert full[13]["atr_14"] is not None


def test_rule_hysteresis_and_high_volatility_mean_reversion_mapping():
    rows = [
        {"realized_vol_percentile_252d": 0.60, "efficiency_ratio_20d": 0.15, "adx_14": 10, "return_20d": 0.0},
        {"realized_vol_percentile_252d": 0.96, "efficiency_ratio_20d": 0.20, "adx_14": 12, "return_20d": -0.02},
        {"realized_vol_percentile_252d": 0.84, "efficiency_ratio_20d": 0.15, "adx_14": 10, "return_20d": 0.0},
    ]
    states, volatility = _raw_rule_states(rows, "technical")
    assert states == ["range_bound", "volatile", "volatile"]
    assert volatility == ["calm", "extreme", "extreme"]
    assert set(_strategy_weights(
        "volatile", strategy_policy="high_vol_mean_reversion",
        event_enabled=False, event_disable_in_volatile=True,
    )) == {"mean_reversion", "swing_mean_reversion"}
    assert _strategy_weights(
        "range_bound", strategy_policy="high_vol_mean_reversion",
        event_enabled=False, event_disable_in_volatile=True,
    ) == {}


def test_hmm_and_gmm_return_probabilistic_regimes_with_pit_training():
    rows = []
    start = date(2018, 1, 1)
    for index in range(360):
        phase = index // 90
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "return_20d": (-0.04, -0.01, 0.02, 0.05)[phase],
            "realized_vol_percentile_252d": (0.9, 0.4, 0.2, 0.6)[phase],
            "efficiency_ratio_20d": (0.2, 0.15, 0.75, 0.65)[phase],
        })
    result = infer_regime_model(
        rows, engine="gmm", feature_names=(
            "return_20d", "realized_vol_percentile_252d", "efficiency_ratio_20d",
        ), training_window=300,
    )
    assert result["status"] == "ok"
    assert result["as_of"] == rows[-1]["date"]
    assert result["fit_samples"] < len(rows)
    assert abs(sum(result["probabilities"].values()) - 1.0) < 1e-5


def test_model_state_means_can_identify_a_sustained_trend():
    assert _regime_for_state({
        "return_20d": 0.03, "efficiency_ratio_20d": 0.32,
        "adx_14": 27.0, "realized_vol_percentile_252d": 0.4,
    }) == "trending_up"


def test_fred_parser_drops_missing_and_future_values():
    csv = (
        "observation_date,VIXCLS,VXVCLS,BAMLH0A0HYM2\n"
        "2024-01-02,13.2,15.4,3.5\n"
        "2024-01-03,.,16.0,3.6\n"
        "2024-01-04,14,17,3.7\n"
    )
    parsed = parse_fred_csv(csv, today=date(2024, 1, 3))
    assert parsed["VIXCLS"] == {"2024-01-02": 13.2}
    assert parsed["VXVCLS"] == {"2024-01-02": 15.4, "2024-01-03": 16.0}


def test_macro_cache_schema_migration_is_additive_and_idempotent(tmp_path):
    database = tmp_path / "market.sqlite3"
    store = SQLiteBarStore(database)
    written = store.write_macro_observations(
        {"VIXCLS": {"2024-01-02": 13.2}}, provider="fred",
    )
    assert written == 1
    SQLiteBarStore(database)
    assert store.macro_series(["VIXCLS"], provider="fred") == {
        "VIXCLS": [{"date": "2024-01-02", "value": 13.2}],
    }
