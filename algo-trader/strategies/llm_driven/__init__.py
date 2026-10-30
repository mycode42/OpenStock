"""LLM-signal strategy: scheduled AI recommendations and owner exit management.

Calendar data is an input for equity_research replay and options research. The
live scanner uses only LLMSignalBook and LLMSignalGenerator's AI and
owner-protection paths.
"""

from strategies.llm_driven.ai import LLMSignalBook, DEFAULT_AI_SIGNAL_DB
from strategies.llm_driven.calendar import EventCalendar
from strategies.llm_driven.config import LLMStrategyConfig
from strategies.llm_driven.models import ScheduledEvent
from strategies.llm_driven.signals import LLMSignalGenerator

__all__ = [
    "LLMSignalBook", "DEFAULT_AI_SIGNAL_DB", "EventCalendar",
    "LLMSignalGenerator", "LLMStrategyConfig", "ScheduledEvent",
]
