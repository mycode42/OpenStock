#!/usr/bin/env python3
"""
Fetch comprehensive data for META and JPM from financialdatasets.ai
and run the 7-analyst short analysis framework.
"""

import os
import sys
import json
from datetime import datetime, timedelta

# Add the ai-hedge-fund src to path
sys.path.insert(0, '/home/zohair/.openclaw/workspace/ai-hedge-fund/src')

from tools.api_original import (
    get_prices,
    get_financial_metrics,
    search_line_items,
    get_insider_trades,
    get_company_news,
    get_company_facts,
    get_market_cap,
    prices_to_df,
)

# API key from .env
API_KEY = os.getenv("FINANCIAL_DATASETS_API_KEY", "")
if not API_KEY:
    raise EnvironmentError("FINANCIAL_DATASETS_API_KEY must be set in .env")

# Date ranges
END_DATE = datetime.now().strftime("%Y-%m-%d")
START_DATE = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

def fetch_stock_data(ticker: str):
    """Fetch all available data for a stock."""
    print(f"\n{'='*80}")
    print(f"FETCHING DATA FOR {ticker}")
    print(f"{'='*80}\n")
    
    data = {
        "ticker": ticker,
        "prices": None,
        "financial_metrics": None,
        "line_items": None,
        "insider_trades": None,
        "company_news": None,
        "company_facts": None,
        "market_cap": None,
    }
    
    # 1. Company Facts
    print(f"1. Fetching company facts for {ticker}...")
    try:
        facts = get_company_facts(ticker, api_key=API_KEY)
        data["company_facts"] = facts.company_facts.model_dump() if facts else None
        print(f"   Company: {facts.company_facts.name if facts else 'N/A'}")
        print(f"   Sector: {facts.company_facts.sector if facts else 'N/A'}")
        print(f"   Industry: {facts.company_facts.industry if facts else 'N/A'}")
        print(f"   Market Cap: ${facts.company_facts.market_cap:,.0f}B" if facts and facts.company_facts.market_cap else "   Market Cap: N/A")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # 2. Market Cap
    print(f"\n2. Fetching market cap...")
    try:
        mc = get_market_cap(ticker, END_DATE, api_key=API_KEY)
        data["market_cap"] = mc
        print(f"   Market Cap: ${mc:,.0f}M" if mc else "   N/A")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # 3. Price Data (last 252 trading days)
    print(f"\n3. Fetching price data ({START_DATE} to {END_DATE})...")
    try:
        prices = get_prices(ticker, START_DATE, END_DATE, api_key=API_KEY)
        data["prices"] = [p.model_dump() for p in prices] if prices else []
        if prices:
            df = prices_to_df(prices)
            print(f"   Data points: {len(prices)}")
            print(f"   Current Price: ${df['close'].iloc[-1]:.2f}")
            print(f"   52W High: ${df['high'].max():.2f}")
            print(f"   52W Low: ${df['low'].min():.2f}")
            print(f"   Avg Volume: {df['volume'].mean():,.0f}")
        else:
            print("   No price data available")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # 4. Financial Metrics (TTM, quarterly, annual)
    print(f"\n4. Fetching financial metrics...")
    try:
        # TTM
        metrics_ttm = get_financial_metrics(ticker, END_DATE, period="ttm", limit=5, api_key=API_KEY)
        data["financial_metrics_ttm"] = [m.model_dump() for m in metrics_ttm] if metrics_ttm else []
        
        # Quarterly
        metrics_quarterly = get_financial_metrics(ticker, END_DATE, period="quarterly", limit=8, api_key=API_KEY)
        data["financial_metrics_quarterly"] = [m.model_dump() for m in metrics_quarterly] if metrics_quarterly else []
        
        # Annual
        metrics_annual = get_financial_metrics(ticker, END_DATE, period="annual", limit=10, api_key=API_KEY)
        data["financial_metrics_annual"] = [m.model_dump() for m in metrics_annual] if metrics_annual else []
        
        if metrics_ttm:
            m = metrics_ttm[0]
            print(f"   TTM Metrics Available: {len(metrics_ttm)} periods")
            print(f"   P/E: {m.price_to_earnings_ratio:.2f}" if m.price_to_earnings_ratio else "   P/E: N/A")
            print(f"   P/B: {m.price_to_book_ratio:.2f}" if m.price_to_book_ratio else "   P/B: N/A")
            print(f"   P/S: {m.price_to_sales_ratio:.2f}" if m.price_to_sales_ratio else "   P/S: N/A")
            print(f"   EV/EBITDA: {m.enterprise_value_to_ebitda_ratio:.2f}" if m.enterprise_value_to_ebitda_ratio else "   EV/EBITDA: N/A")
            print(f"   Gross Margin: {m.gross_margin*100:.1f}%" if m.gross_margin else "   Gross Margin: N/A")
            print(f"   Operating Margin: {m.operating_margin*100:.1f}%" if m.operating_margin else "   Operating Margin: N/A")
            print(f"   Net Margin: {m.net_margin*100:.1f}%" if m.net_margin else "   Net Margin: N/A")
            print(f"   ROE: {m.return_on_equity*100:.1f}%" if m.return_on_equity else "   ROE: N/A")
            print(f"   ROA: {m.return_on_assets*100:.1f}%" if m.return_on_assets else "   ROA: N/A")
            print(f"   Revenue Growth: {m.revenue_growth*100:.1f}%" if m.revenue_growth else "   Revenue Growth: N/A")
            print(f"   Earnings Growth: {m.earnings_growth*100:.1f}%" if m.earnings_growth else "   Earnings Growth: N/A")
            print(f"   FCF Growth: {m.free_cash_flow_growth*100:.1f}%" if m.free_cash_flow_growth else "   FCF Growth: N/A")
            print(f"   Debt/Equity: {m.debt_to_equity:.2f}" if m.debt_to_equity else "   Debt/Equity: N/A")
            print(f"   Current Ratio: {m.current_ratio:.2f}" if m.current_ratio else "   Current Ratio: N/A")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # 5. Line Items (key financial statement items)
    print(f"\n5. Fetching line items...")
    try:
        line_items_list = [
            "revenue", "total_revenue", "cost_of_revenue", "gross_profit",
            "operating_income", "operating_expense", "net_income", "ebitda",
            "interest_expense", "research_and_development", "selling_general_and_administrative",
            "total_assets", "total_liabilities", "total_equity", "current_assets",
            "current_liabilities", "cash_and_equivalents", "long_term_debt", "total_debt",
            "operating_cash_flow", "capital_expenditure", "free_cash_flow",
            "weighted_average_shares", "earnings_per_share"
        ]
        line_items = search_line_items(ticker, line_items_list, END_DATE, period="quarterly", limit=8, api_key=API_KEY)
        data["line_items"] = [li.model_dump() for li in line_items] if line_items else []
        print(f"   Line items fetched: {len(line_items)} periods")
        if line_items:
            li = line_items[0]
            print(f"   Latest Revenue: ${li.revenue:,.0f}M" if hasattr(li, 'revenue') and li.revenue else "   Revenue: N/A")
            print(f"   Latest Net Income: ${li.net_income:,.0f}M" if hasattr(li, 'net_income') and li.net_income else "   Net Income: N/A")
            print(f"   Latest FCF: ${li.free_cash_flow:,.0f}M" if hasattr(li, 'free_cash_flow') and li.free_cash_flow else "   FCF: N/A")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # 6. Insider Trades
    print(f"\n6. Fetching insider trades (last 12 months)...")
    try:
        insider_trades = get_insider_trades(ticker, END_DATE, start_date=START_DATE, limit=100, api_key=API_KEY)
        data["insider_trades"] = [t.model_dump() for t in insider_trades] if insider_trades else []
        print(f"   Insider trades: {len(insider_trades)}")
        if insider_trades:
            buys = sum(1 for t in insider_trades if t.transaction_shares and t.transaction_shares > 0)
            sells = sum(1 for t in insider_trades if t.transaction_shares and t.transaction_shares < 0)
            print(f"   Buys: {buys}, Sells: {sells}")
            net_shares = sum(t.transaction_shares or 0 for t in insider_trades)
            print(f"   Net shares: {net_shares:,.0f}")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # 7. Company News
    print(f"\n7. Fetching company news (last 90 days)...")
    try:
        news_start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        news = get_company_news(ticker, END_DATE, start_date=news_start, limit=50, api_key=API_KEY)
        data["company_news"] = [n.model_dump() for n in news] if news else []
        print(f"   News articles: {len(news)}")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    return data


def main():
    """Main execution."""
    tickers = ["META", "JPM"]
    all_data = {}
    
    for ticker in tickers:
        data = fetch_stock_data(ticker)
        all_data[ticker] = data
    
    # Save to file
    output_file = "/home/zohair/.openclaw/workspace/ai-hedge-fund/short_analysis_data.json"
    with open(output_file, 'w') as f:
        json.dump(all_data, f, indent=2, default=str)
    
    print(f"\n{'='*80}")
    print(f"DATA SAVED TO: {output_file}")
    print(f"{'='*80}\n")
    
    return all_data


if __name__ == "__main__":
    main()
