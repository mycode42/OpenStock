

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from market.models import CompanyNews
import pandas as pd
import numpy as np
import json

from .state import AgentState, show_agent_reasoning
from market.research_data import get_company_news
from .api_key import get_api_key_from_state
from .llm import call_llm
from .progress import progress
from typing_extensions import Literal


class HeadlineClassification(BaseModel):
    article_id: str
    sentiment: Literal["positive", "negative", "neutral"]
    confidence: int = Field(ge=0, le=100)


class HeadlineClassificationBatch(BaseModel):
    classifications: list[HeadlineClassification] = Field(default_factory=list)


def news_sentiment_agent(state: AgentState, agent_id: str = "news_sentiment_agent"):
    """Analyze recent ticker news with one bounded classification request."""
    data = state.get("data", {})
    end_date = data.get("end_date")
    tickers = data.get("tickers") or []
    api_key = get_api_key_from_state(state, "FINANCIAL_DATASETS_API_KEY")
    news_by_ticker: dict[str, list[CompanyNews]] = {}
    pending_headlines: dict[str, tuple[str, CompanyNews]] = {}

    # The previous per-headline LLM loop could make up to 120 sequential calls
    # for a 24-ticker universe. Gather the same maximum five headlines per
    # ticker, then classify them together in one request.
    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Fetching company news")
        company_news = get_company_news(
            ticker=ticker,
            end_date=end_date,
            limit=100,
            api_key=api_key,
        ) or []
        news_by_ticker[ticker] = company_news

        missing_count = 0
        for article_index, news in enumerate(company_news[:10]):
            if news.sentiment is None:
                article_id = f"{ticker}:{article_index}"
                pending_headlines[article_id] = (ticker, news)
                missing_count += 1
                if missing_count >= 5:
                    break

    sentiment_confidences: dict[int, int] = {}
    classified_by_ticker = {ticker: 0 for ticker in tickers}
    if pending_headlines:
        progress.update_status(
            agent_id,
            None,
            f"Classifying {len(pending_headlines)} headlines in one model request",
        )
        headline_payload = [
            {"article_id": article_id, "ticker": ticker, "headline": news.title}
            for article_id, (ticker, news) in pending_headlines.items()
        ]
        prompt = (
            "Classify each stock-news headline for its stated ticker. Return exactly one "
            "classification per article_id, with sentiment positive, negative, or neutral "
            "and confidence from 0 to 100. Do not infer missing catalysts.\n\n"
            f"Headlines: {json.dumps(headline_payload, ensure_ascii=False)}"
        )
        response = call_llm(
            prompt,
            HeadlineClassificationBatch,
            agent_name=agent_id,
            state=state,
            max_retries=1,
            default_factory=HeadlineClassificationBatch,
        )
        seen_ids: set[str] = set()
        for classification in getattr(response, "classifications", []) or []:
            article_id = str(getattr(classification, "article_id", ""))
            target = pending_headlines.get(article_id)
            if target is None or article_id in seen_ids:
                continue
            ticker, news = target
            news.sentiment = str(classification.sentiment).lower()
            sentiment_confidences[id(news)] = int(classification.confidence)
            classified_by_ticker[ticker] += 1
            seen_ids.add(article_id)

        # Treat missing or malformed rows as neutral rather than failing the
        # complete market briefing.
        for article_id, (_, news) in pending_headlines.items():
            if article_id not in seen_ids:
                news.sentiment = "neutral"
                sentiment_confidences[id(news)] = 0

    sentiment_analysis = {}
    for ticker, company_news in news_by_ticker.items():
        progress.update_status(agent_id, ticker, "Aggregating signals")
        if company_news:
            sentiment = pd.Series([news.sentiment for news in company_news]).dropna()
            news_signals = np.where(
                sentiment == "negative",
                "bearish",
                np.where(sentiment == "positive", "bullish", "neutral"),
            ).tolist()
        else:
            news_signals = []

        bullish_signals = news_signals.count("bullish")
        bearish_signals = news_signals.count("bearish")
        neutral_signals = news_signals.count("neutral")
        if bullish_signals > bearish_signals:
            overall_signal = "bullish"
        elif bearish_signals > bullish_signals:
            overall_signal = "bearish"
        else:
            overall_signal = "neutral"

        total_signals = len(news_signals)
        confidence = _calculate_confidence_score(
            sentiment_confidences=sentiment_confidences,
            company_news=company_news,
            overall_signal=overall_signal,
            bullish_signals=bullish_signals,
            bearish_signals=bearish_signals,
            total_signals=total_signals,
        )
        reasoning = {
            "news_sentiment": {
                "signal": overall_signal,
                "confidence": confidence,
                "metrics": {
                    "total_articles": total_signals,
                    "bullish_articles": bullish_signals,
                    "bearish_articles": bearish_signals,
                    "neutral_articles": neutral_signals,
                    "articles_classified_by_llm": classified_by_ticker[ticker],
                },
            }
        }
        sentiment_analysis[ticker] = {
            "signal": overall_signal,
            "confidence": confidence,
            "reasoning": reasoning,
        }
        progress.update_status(agent_id, ticker, "Done", analysis=json.dumps(reasoning, indent=4))

    message = HumanMessage(
        content=json.dumps(sentiment_analysis),
        name=agent_id,
    )
    if state.get("metadata", {}).get("show_reasoning"):
        show_agent_reasoning(sentiment_analysis, "News Sentiment Analysis Agent")
    if "analyst_signals" not in state["data"]:
        state["data"]["analyst_signals"] = {}
    state["data"]["analyst_signals"][agent_id] = sentiment_analysis
    progress.update_status(agent_id, None, "Done")
    return {"messages": [message], "data": state["data"]}

def _calculate_confidence_score(
    sentiment_confidences: dict,
    company_news: list,
    overall_signal: str,
    bullish_signals: int,
    bearish_signals: int,
    total_signals: int
) -> float:
    """
    Calculate confidence score for a sentiment signal.
    
    Uses a weighted approach combining LLM confidence scores (70%) with 
    signal proportion (30%) when LLM classifications are available.
    
    Args:
        sentiment_confidences: Dictionary mapping news article IDs to confidence scores.
        company_news: List of CompanyNews objects.
        overall_signal: The overall sentiment signal ("bullish", "bearish", or "neutral").
        bullish_signals: Count of bullish signals.
        bearish_signals: Count of bearish signals.
        total_signals: Total number of signals.
        
    Returns:
        Confidence score as a float between 0 and 100.
    """
    if total_signals == 0:
        return 0.0
    
    # Calculate weighted confidence using LLM confidence scores when available
    if sentiment_confidences:
        # Get articles that match the overall signal
        matching_articles = [
            news for news in company_news 
            if news.sentiment and (
                (overall_signal == "bullish" and news.sentiment == "positive") or
                (overall_signal == "bearish" and news.sentiment == "negative") or
                (overall_signal == "neutral" and news.sentiment == "neutral")
            )
        ]
        
        # Calculate average confidence from LLM-classified articles that match the signal
        llm_confidences = [
            sentiment_confidences[id(news)] 
            for news in matching_articles 
            if id(news) in sentiment_confidences
        ]
        
        if llm_confidences:
            # Weight: 70% from LLM confidence scores, 30% from signal proportion
            avg_llm_confidence = sum(llm_confidences) / len(llm_confidences)
            signal_proportion = (max(bullish_signals, bearish_signals) / total_signals) * 100
            return round(0.7 * avg_llm_confidence + 0.3 * signal_proportion, 2)
    
    # Fallback to proportion-based confidence
    return round((max(bullish_signals, bearish_signals) / total_signals) * 100, 2)
