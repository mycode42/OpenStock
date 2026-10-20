"""Intraday and daily trend-following strategies."""

from . import intraday, swing
from .intraday import signal
from .swing import SwingTrendConfig

__all__ = ["SwingTrendConfig", "intraday", "signal", "swing"]
