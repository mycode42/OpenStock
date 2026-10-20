"""Analyst nodes used by the Algo Event Driven research snapshot."""

from src.agents.fundamentals import fundamentals_analyst_agent
from src.agents.growth_agent import growth_analyst_agent
from src.agents.market_regime import market_regime_agent
from src.agents.news_sentiment import news_sentiment_agent
from src.agents.sentiment import sentiment_analyst_agent
from src.agents.technicals import technical_analyst_agent


ANALYST_CONFIG = {
    "market_regime": {
        "display_name": "Market Regime",
        "description": "Classifies current market conditions.",
        "investing_style": "Uses broad market data to label the regime.",
        "agent_func": market_regime_agent,
        "type": "analyst",
        "order": 0,
    },
    "news_sentiment_analyst": {
        "display_name": "News Sentiment Analyst",
        "description": "Reviews current company and market news.",
        "investing_style": "Summarizes news-driven evidence and sentiment.",
        "agent_func": news_sentiment_agent,
        "type": "analyst",
        "order": 1,
    },
    "sentiment_analyst": {
        "display_name": "Sentiment Analyst",
        "description": "Reviews market sentiment indicators.",
        "investing_style": "Uses sentiment and investor behavior as supporting evidence.",
        "agent_func": sentiment_analyst_agent,
        "type": "analyst",
        "order": 2,
    },
    "technical_analyst": {
        "display_name": "Technical Analyst",
        "description": "Reviews price action and technical indicators.",
        "investing_style": "Uses chart and trend evidence to assess setups.",
        "agent_func": technical_analyst_agent,
        "type": "analyst",
        "order": 3,
    },
    "fundamentals_analyst": {
        "display_name": "Fundamentals Analyst",
        "description": "Reviews company fundamentals.",
        "investing_style": "Uses financial statements and business metrics as supporting evidence.",
        "agent_func": fundamentals_analyst_agent,
        "type": "analyst",
        "order": 4,
    },
    "growth_analyst": {
        "display_name": "Growth Analyst",
        "description": "Reviews company growth trends.",
        "investing_style": "Uses revenue and operating growth as supporting evidence.",
        "agent_func": growth_analyst_agent,
        "type": "analyst",
        "order": 5,
    },
}

ANALYST_ORDER = [(item["display_name"], key) for key, item in ANALYST_CONFIG.items()]


def get_analyst_nodes():
    """Return the graph node name and function for each supported analyst."""
    return {key: (f"{key}_agent", item["agent_func"]) for key, item in ANALYST_CONFIG.items()}


def get_agents_list():
    """Return serializable descriptions of the supported analyst nodes."""
    return [
        {
            "key": key,
            "display_name": item["display_name"],
            "description": item["description"],
            "investing_style": item["investing_style"],
            "order": item["order"],
        }
        for key, item in ANALYST_CONFIG.items()
    ]
