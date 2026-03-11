"""Shared configuration for the AI Hedge Fund / Swarm Trader platform."""

# Target investment universe — single source of truth.
# Used by: check_portfolio.py, gather_data.py, src/agents/apex.py, rebalance.py
UNIVERSE = {
    "ai_infra": {
        "label": "AI Infrastructure",
        "tickers": ["NVDA", "AVGO", "SMCI", "TSM"],
        "target_pct": 0.40,
    },
    "leveraged_etfs": {
        "label": "Leveraged ETFs",
        "tickers": ["TQQQ", "SOXL", "UPRO"],
        "target_pct": 0.25,
    },
    "momentum": {
        "label": "Momentum Plays",
        "tickers": ["PLTR", "MSTR", "COIN", "RKLB"],
        "target_pct": 0.20,
    },
    "moonshots": {
        "label": "Moonshots",
        "tickers": ["IONQ", "RGTI", "SOUN", "LUNR"],
        "target_pct": 0.15,
    },
}

# Flat list of all universe tickers
ALL_UNIVERSE_TICKERS = [t for cat in UNIVERSE.values() for t in cat["tickers"]]

# Simple category->tickers mapping (for scripts that don't need target_pct)
UNIVERSE_SIMPLE = {key: cat["tickers"] for key, cat in UNIVERSE.items()}
