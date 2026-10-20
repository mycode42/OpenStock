"""Mode-specific hard limits and cached ticker policy maps."""

from __future__ import annotations

# Leveraged ETFs blocked by the Algo portfolio policy.
LEVERAGED_ETFS = {
    "TQQQ", "SOXL", "UPRO", "SPXL", "TECL", "LABU", "SOXS", "TECS",
    "SPXS", "SQQQ", "SDOW", "UDOW", "NAIL", "CURE", "HIBL", "HIBS",
}

# Moonshots are blocked in every mode and excluded from all configured universes.
MOONSHOTS = {"RGTI", "SOUN", "LUNR"}


def build_universe_maps(mode: str, config_loader, cache: dict) -> tuple[dict, dict, set, dict]:
    """Build and cache ticker sector, position cap, tactical set and universe."""
    if mode not in cache:
        mode_config = config_loader(mode)
        universe = mode_config["universe"]
        default_max_pct = mode_config["risk"]["max_position_pct"]
        ticker_sector: dict[str, str] = {}
        ticker_max_pct: dict[str, float] = {}
        for sector_key, sector_data in universe.items():
            per_stock = sector_data.get("max_per_stock_pct", default_max_pct)
            for ticker in sector_data.get("tickers", []):
                ticker_sector[ticker] = sector_key
                ticker_max_pct[ticker] = per_stock
        tactical_tickers = set(universe.get("tactical", {}).get("tickers", []))
        cache[mode] = ticker_sector, ticker_max_pct, tactical_tickers, universe
    return cache[mode]


__all__ = ["LEVERAGED_ETFS", "MOONSHOTS", "build_universe_maps"]
