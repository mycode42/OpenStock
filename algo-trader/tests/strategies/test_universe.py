from strategies import universe


def test_load_algo_universe_exposes_overnight_asset_eligibility(monkeypatch):
    monkeypatch.setattr(universe, "selected_universe", lambda: {"tickers": ["AAPL", "MSFT", "XYZ"]})
    monkeypatch.setattr(universe, "_fetch_active_assets", lambda mode: {
        "AAPL": {"attributes": ["overnight_tradable"]},
        "MSFT": {"overnight_tradable": True, "overnight_halted": True},
        "XYZ": {},
    })

    result = universe.load_algo_universe()

    assert result["overnight_metadata_status"] == "ok"
    assert result["overnight_tradable_tickers"] == ["AAPL", "MSFT"]
    assert result["overnight_halted_tickers"] == ["MSFT"]
