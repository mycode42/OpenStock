"""Cross-sectional multi-factor strategy components."""

from strategies.multi_factor.config import DEFAULT_CONFIG, MultiFactorConfig
from strategies.multi_factor.features import FactorCalculator
from strategies.multi_factor.ranking import Combiner, FactorRanker
from strategies.multi_factor.signals import MultiFactorSignal, SignalGenerator

__all__ = [
    "Combiner", "DEFAULT_CONFIG", "FactorCalculator", "FactorRanker", "MultiFactorConfig",
    "MultiFactorSignal", "SignalGenerator",
]
