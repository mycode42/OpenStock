"""Shared equity execution-cost assumptions for research backtests and pipelines."""

from __future__ import annotations

EQUITY_SLIPPAGE_BPS = 5.0
EQUITY_SPREAD_BPS = 3.0
EQUITY_COMMISSION_PER_SHARE = 0.005

__all__ = ["EQUITY_COMMISSION_PER_SHARE", "EQUITY_SLIPPAGE_BPS", "EQUITY_SPREAD_BPS"]
