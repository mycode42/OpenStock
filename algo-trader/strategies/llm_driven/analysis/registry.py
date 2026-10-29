"""Analyst nodes used by the live LLM-driven signal workflow."""

from .fundamentals import fundamentals_analyst_agent
from .growth_agent import growth_analyst_agent
from .market_regime import market_regime_agent
from .news_sentiment import news_sentiment_agent
from .sentiment import sentiment_analyst_agent
from .technicals import technical_analyst_agent


ANALYSTS = {
    "market_regime": market_regime_agent,
    "news_sentiment_analyst": news_sentiment_agent,
    "sentiment_analyst": sentiment_analyst_agent,
    "technical_analyst": technical_analyst_agent,
    "fundamentals_analyst": fundamentals_analyst_agent,
    "growth_analyst": growth_analyst_agent,
}


def get_analyst_nodes():
    """Return the graph node name and function for each supported analyst."""
    return {key: (f"{key}_agent", function) for key, function in ANALYSTS.items()}
