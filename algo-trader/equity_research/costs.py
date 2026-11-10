"""Shared equity execution-cost assumptions for research backtests and pipelines."""

from __future__ import annotations

# Replay costs set to zero on 2026-11-10 (per Andreas): the replay assumes fills at the modelled price with no
# spread, slippage or commission. Borrow cost for shorts is separate and unchanged; shorts are off in live.
EQUITY_SLIPPAGE_BPS = 0.0
EQUITY_SPREAD_BPS = 0.0
EQUITY_COMMISSION_PER_SHARE = 0.0

__all__ = ["EQUITY_COMMISSION_PER_SHARE", "EQUITY_SLIPPAGE_BPS", "EQUITY_SPREAD_BPS"]
