#!/usr/bin/env python3
"""
7-Analyst Short Signal Analysis Framework
Analyzes META and JPM for short opportunities using 7 distinct analyst perspectives:
1. Technical Analyst - Charts, patterns, momentum, RSI, MACD, moving averages
2. Fundamental Analyst - Valuation, financial health, growth trajectory
3. Apex Analyst - Aggressive contrarian, finds overvalued momentum traps
4. Cathie Wood Analyst - Innovation disruption, long-term growth sustainability
5. Warren Buffett Analyst - Value investing, moat, management quality
6. Michael Burry Analyst - Deep value, accounting red flags, bubble detection
7. Risk Management - Position sizing, stop losses, risk/reward

Plus Portfolio Manager synthesis.
"""

import json
import math
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import statistics

# Load the fetched data
with open('/home/zohair/.openclaw/workspace/swarm-trader/short_analysis_data.json', 'r') as f:
    DATA = json.load(f)

class TechnicalAnalyst:
    """Technical analysis focused on price action, momentum, and chart patterns."""
    
    def analyze(self, ticker: str, data: Dict) -> Dict:
        prices = data.get('prices', [])
        if not prices:
            return {"signal": "NEUTRAL", "conviction": 0, "reasoning": "No price data available"}
        
        # Convert to easier format
        closes = [p['close'] for p in prices]
        highs = [p['high'] for p in prices]
        lows = [p['low'] for p in prices]
        volumes = [p['volume'] for p in prices]
        dates = [p['time'] for p in prices]
        
        current_price = closes[-1]
        
        # Calculate indicators
        # 20-day and 50-day SMA
        sma_20 = statistics.mean(closes[-20:]) if len(closes) >= 20 else None
        sma_50 = statistics.mean(closes[-50:]) if len(closes) >= 50 else None
        sma_200 = statistics.mean(closes[-200:]) if len(closes) >= 200 else None
        
        # 52-week high/low
        high_52w = max(highs)
        low_52w = min(lows)
        pct_from_high = (current_price - high_52w) / high_52w * 100
        pct_from_low = (current_price - low_52w) / low_52w * 100
        
        # RSI (14-day)
        rsi = self.calculate_rsi(closes, 14)
        
        # MACD (12, 26, 9)
        macd_line, signal_line, macd_hist = self.calculate_macd(closes)
        
        # Volume trend
        avg_vol_20 = statistics.mean(volumes[-20:]) if len(volumes) >= 20 else statistics.mean(volumes)
        recent_vol = statistics.mean(volumes[-5:])
        vol_trend = "INCREASING" if recent_vol > avg_vol_20 * 1.2 else "DECREASING" if recent_vol < avg_vol_20 * 0.8 else "NORMAL"
        
        # Price momentum
        momentum_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
        momentum_20d = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
        
        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = self.calculate_bollinger(closes, 20)
        bb_position = (current_price - bb_lower) / (bb_upper - bb_lower) * 100 if bb_upper != bb_lower else 50
        
        # Analysis
        bearish_signals = []
        bullish_signals = []
        
        if sma_20 and sma_50:
            if current_price < sma_20 < sma_50:
                bearish_signals.append("Price below both 20-day and 50-day SMA (bearish alignment)")
            elif current_price > sma_20 > sma_50:
                bullish_signals.append("Price above both 20-day and 50-day SMA (bullish alignment)")
        
        if sma_50 and sma_200:
            if sma_50 < sma_200:
                bearish_signals.append("50-day SMA below 200-day SMA (death cross pattern)")
            elif sma_50 > sma_200:
                bullish_signals.append("50-day SMA above 200-day SMA (golden cross pattern)")
        
        if rsi:
            if rsi > 70:
                bearish_signals.append(f"RSI at {rsi:.1f} - Overbought territory")
            elif rsi < 30:
                bullish_signals.append(f"RSI at {rsi:.1f} - Oversold territory")
        
        if macd_line is not None and signal_line is not None:
            if macd_line < signal_line and macd_hist < 0:
                bearish_signals.append("MACD below signal line with negative histogram")
            elif macd_line > signal_line and macd_hist > 0:
                bullish_signals.append("MACD above signal line with positive histogram")
        
        if pct_from_high < -15:
            bearish_signals.append(f"Trading {pct_from_high:.1f}% below 52-week high")
        elif pct_from_high > -5:
            bullish_signals.append(f"Near 52-week highs ({pct_from_high:.1f}%)")
        
        if bb_position > 80:
            bearish_signals.append(f"Price in upper Bollinger Band ({bb_position:.0f}%) - overextended")
        elif bb_position < 20:
            bullish_signals.append(f"Price in lower Bollinger Band ({bb_position:.0f}%) - oversold")
        
        if momentum_20d < -10:
            bearish_signals.append(f"Negative 20-day momentum ({momentum_20d:.1f}%)")
        elif momentum_20d > 10:
            bullish_signals.append(f"Strong positive 20-day momentum ({momentum_20d:.1f}%)")
        
        # Calculate conviction
        conviction = (len(bearish_signals) - len(bullish_signals)) / max(len(bearish_signals) + len(bullish_signals), 1) * 100
        conviction = max(-100, min(100, conviction))
        
        if conviction > 30:
            signal = "BEARISH"
        elif conviction < -30:
            signal = "BULLISH"
        else:
            signal = "NEUTRAL"
        
        return {
            "analyst": "Technical Analyst",
            "signal": signal,
            "conviction": abs(conviction),
            "reasoning": {
                "current_price": current_price,
                "sma_20": round(sma_20, 2) if sma_20 else None,
                "sma_50": round(sma_50, 2) if sma_50 else None,
                "sma_200": round(sma_200, 2) if sma_200 else None,
                "rsi_14": round(rsi, 1) if rsi else None,
                "macd_signal": "Bearish" if macd_line and macd_line < signal_line else "Bullish" if macd_line else None,
                "52w_high": round(high_52w, 2),
                "52w_low": round(low_52w, 2),
                "pct_from_52w_high": round(pct_from_high, 1),
                "bollinger_position": round(bb_position, 0),
                "momentum_20d": round(momentum_20d, 1),
                "volume_trend": vol_trend,
                "bearish_signals": bearish_signals,
                "bullish_signals": bullish_signals,
            },
            "short_thesis": " - ".join(bearish_signals[:3]) if bearish_signals else "No clear technical setup for short",
            "risk_factors": bullish_signals[:3] if bullish_signals else [],
        }
    
    def calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        
        gains = []
        losses = []
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            gains.append(max(0, change))
            losses.append(max(0, -change))
        
        avg_gain = statistics.mean(gains[-period:])
        avg_loss = statistics.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def calculate_macd(self, prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
        if len(prices) < slow + signal:
            return None, None, None
        
        def ema(data, period):
            multiplier = 2 / (period + 1)
            ema_val = statistics.mean(data[:period])
            for price in data[period:]:
                ema_val = (price - ema_val) * multiplier + ema_val
            return ema_val
        
        fast_ema = ema(prices, fast)
        slow_ema = ema(prices, slow)
        macd_line = fast_ema - slow_ema
        
        # Simplified signal line
        recent_macds = []
        for i in range(signal):
            idx = len(prices) - signal + i
            if idx >= slow:
                fast_e = ema(prices[:idx+1], fast)
                slow_e = ema(prices[:idx+1], slow)
                recent_macds.append(fast_e - slow_e)
        
        signal_line = statistics.mean(recent_macds) if recent_macds else None
        macd_hist = macd_line - signal_line if signal_line else None
        
        return macd_line, signal_line, macd_hist
    
    def calculate_bollinger(self, prices: List[float], period: int = 20, std_dev: int = 2):
        if len(prices) < period:
            return None, None, None
        
        recent = prices[-period:]
        middle = statistics.mean(recent)
        std = statistics.stdev(recent)
        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)
        
        return upper, middle, lower


class FundamentalAnalyst:
    """Fundamental analysis focused on valuation, financial health, and growth."""
    
    def analyze(self, ticker: str, data: Dict) -> Dict:
        metrics_ttm = data.get('financial_metrics_ttm', [])
        metrics_annual = data.get('financial_metrics_annual', [])
        line_items = data.get('line_items', [])
        facts = data.get('company_facts', {})
        
        if not metrics_ttm:
            return {"signal": "NEUTRAL", "conviction": 0, "reasoning": "No financial metrics available"}
        
        m = metrics_ttm[0]
        
        # Valuation metrics
        pe = m.get('price_to_earnings_ratio')
        pb = m.get('price_to_book_ratio')
        ps = m.get('price_to_sales_ratio')
        ev_ebitda = m.get('enterprise_value_to_ebitda_ratio')
        peg = m.get('peg_ratio')
        fcf_yield = m.get('free_cash_flow_yield')
        
        # Profitability
        gross_margin = m.get('gross_margin', 0) or 0
        operating_margin = m.get('operating_margin', 0) or 0
        net_margin = m.get('net_margin', 0) or 0
        roe = m.get('return_on_equity', 0) or 0
        roa = m.get('return_on_assets', 0) or 0
        roic = m.get('return_on_invested_capital', 0) or 0
        
        # Growth
        revenue_growth = m.get('revenue_growth', 0) or 0
        earnings_growth = m.get('earnings_growth', 0) or 0
        fcf_growth = m.get('free_cash_flow_growth', 0) or 0
        
        # Financial health
        debt_equity = m.get('debt_to_equity')
        current_ratio = m.get('current_ratio')
        interest_coverage = m.get('interest_coverage')
        
        bearish_signals = []
        bullish_signals = []
        
        # Valuation analysis
        if pe and pe > 30:
            bearish_signals.append(f"High P/E ratio of {pe:.1f} - expensive relative to earnings")
        elif pe and pe < 15:
            bullish_signals.append(f"Low P/E ratio of {pe:.1f} - potentially undervalued")
        
        if pb and pb > 10:
            bearish_signals.append(f"High P/B ratio of {pb:.1f} - rich valuation")
        elif pb and pb < 2:
            bullish_signals.append(f"Low P/B ratio of {pb:.1f} - potential value")
        
        if ps and ps > 10:
            bearish_signals.append(f"High P/S ratio of {ps:.1f} - paying premium for sales")
        
        if ev_ebitda and ev_ebitda > 20:
            bearish_signals.append(f"High EV/EBITDA of {ev_ebitda:.1f}x")
        
        if peg and peg > 2:
            bearish_signals.append(f"PEG ratio of {peg:.1f} suggests overvaluation relative to growth")
        elif peg and peg < 1:
            bullish_signals.append(f"PEG ratio of {peg:.1f} suggests good value relative to growth")
        
        # Growth analysis
        if revenue_growth < 0:
            bearish_signals.append(f"Negative revenue growth ({revenue_growth*100:.1f}%)")
        elif revenue_growth < 5:
            bearish_signals.append(f"Slow revenue growth ({revenue_growth*100:.1f}%)")
        elif revenue_growth > 20:
            bullish_signals.append(f"Strong revenue growth ({revenue_growth*100:.1f}%)")
        
        if earnings_growth < 0:
            bearish_signals.append(f"Negative earnings growth ({earnings_growth*100:.1f}%)")
        elif earnings_growth > 20:
            bullish_signals.append(f"Strong earnings growth ({earnings_growth*100:.1f}%)")
        
        if fcf_growth and fcf_growth < 0:
            bearish_signals.append(f"Declining free cash flow ({fcf_growth*100:.1f}%)")
        
        # Profitability analysis
        if net_margin and net_margin < 5:
            bearish_signals.append(f"Low net margin ({net_margin*100:.1f}%)")
        elif net_margin and net_margin > 20:
            bullish_signals.append(f"Excellent net margin ({net_margin*100:.1f}%)")
        
        if roe and roe < 10:
            bearish_signals.append(f"Low ROE ({roe*100:.1f}%) - inefficient capital use")
        elif roe and roe > 20:
            bullish_signals.append(f"Strong ROE ({roe*100:.1f}%)")
        
        # Financial health
        if debt_equity and debt_equity > 2:
            bearish_signals.append(f"High debt-to-equity ({debt_equity:.2f})")
        elif debt_equity and debt_equity < 0.5:
            bullish_signals.append(f"Conservative debt-to-equity ({debt_equity:.2f})")
        
        if current_ratio and current_ratio < 1:
            bearish_signals.append(f"Current ratio below 1 ({current_ratio:.2f}) - liquidity concern")
        
        if interest_coverage and interest_coverage < 3:
            bearish_signals.append(f"Weak interest coverage ({interest_coverage:.1f}x)")
        
        # Calculate conviction
        conviction = (len(bearish_signals) - len(bullish_signals)) / max(len(bearish_signals) + len(bullish_signals), 1) * 100
        conviction = max(-100, min(100, conviction))
        
        if conviction > 30:
            signal = "BEARISH"
        elif conviction < -30:
            signal = "BULLISH"
        else:
            signal = "NEUTRAL"
        
        return {
            "analyst": "Fundamental Analyst",
            "signal": signal,
            "conviction": abs(conviction),
            "reasoning": {
                "valuation": {
                    "pe_ratio": round(pe, 2) if pe else None,
                    "pb_ratio": round(pb, 2) if pb else None,
                    "ps_ratio": round(ps, 2) if ps else None,
                    "ev_ebitda": round(ev_ebitda, 2) if ev_ebitda else None,
                    "peg_ratio": round(peg, 2) if peg else None,
                    "fcf_yield": round(fcf_yield*100, 2) if fcf_yield else None,
                },
                "profitability": {
                    "gross_margin": round(gross_margin*100, 1) if gross_margin else None,
                    "operating_margin": round(operating_margin*100, 1) if operating_margin else None,
                    "net_margin": round(net_margin*100, 1) if net_margin else None,
                    "roe": round(roe*100, 1) if roe else None,
                    "roa": round(roa*100, 1) if roa else None,
                },
                "growth": {
                    "revenue_growth": round(revenue_growth*100, 1),
                    "earnings_growth": round(earnings_growth*100, 1),
                    "fcf_growth": round(fcf_growth*100, 1) if fcf_growth else None,
                },
                "financial_health": {
                    "debt_to_equity": round(debt_equity, 2) if debt_equity else None,
                    "current_ratio": round(current_ratio, 2) if current_ratio else None,
                    "interest_coverage": round(interest_coverage, 1) if interest_coverage else None,
                },
                "bearish_signals": bearish_signals,
                "bullish_signals": bullish_signals,
            },
            "short_thesis": " - ".join(bearish_signals[:3]) if bearish_signals else "No fundamental red flags",
            "risk_factors": bullish_signals[:3] if bullish_signals else [],
        }


class ApexAnalyst:
    """Aggressive contrarian - hunts overvalued momentum traps and hype-driven stocks."""
    
    def analyze(self, ticker: str, data: Dict) -> Dict:
        metrics_ttm = data.get('financial_metrics_ttm', [])
        prices = data.get('prices', [])
        insider_trades = data.get('insider_trades', [])
        
        if not metrics_ttm or not prices:
            return {"signal": "NEUTRAL", "conviction": 0, "reasoning": "Insufficient data"}
        
        m = metrics_ttm[0]
        current_price = prices[-1]['close']
        
        # Apex looks for: extreme valuations, insider selling, parabolic moves, hype
        bearish_signals = []
        bullish_signals = []
        
        # Check for parabolic price action
        if len(prices) >= 90:
            price_90d_ago = prices[-90]['close']
            price_change_90d = (current_price - price_90d_ago) / price_90d_ago * 100
            
            if price_change_90d > 50:
                bearish_signals.append(f"PARABOLIC: +{price_change_90d:.0f}% in 90 days - unsustainable momentum")
            elif price_change_90d > 30:
                bearish_signals.append(f"Strong momentum (+{price_change_90d:.0f}% in 90d) - potential exhaustion")
            elif price_change_90d < -30:
                bullish_signals.append(f"Already corrected {price_change_90d:.0f}% - may be oversold")
        
        # Valuation extremes
        pe = m.get('price_to_earnings_ratio')
        ps = m.get('price_to_sales_ratio')
        
        if pe and pe > 40:
            bearish_signals.append(f"EXTREME VALUATION: P/E of {pe:.1f} - pricing in perfection")
        elif pe and pe > 25:
            bearish_signals.append(f"Rich valuation: P/E of {pe:.1f}")
        
        if ps and ps > 15:
            bearish_signals.append(f"EXTREME: P/S of {ps:.1f}x - paying huge premium")
        
        # Insider selling (major red flag for Apex)
        if insider_trades:
            recent_trades = insider_trades[:20]  # Last 20 trades
            sells = sum(1 for t in recent_trades if t.get('transaction_shares', 0) < 0)
            buys = sum(1 for t in recent_trades if t.get('transaction_shares', 0) > 0)
            net_shares = sum(t.get('transaction_shares', 0) or 0 for t in recent_trades)
            
            if sells > buys * 3:
                bearish_signals.append(f"INSIDER SELLING: {sells} sells vs {buys} buys - insiders exiting")
            elif sells > buys:
                bearish_signals.append(f"Net insider selling: {sells} sells, {buys} buys")
            
            if net_shares < -10000:
                bearish_signals.append(f"Insiders net sold {abs(net_shares):,.0f} shares")
        
        # Check if growth justifies valuation
        revenue_growth = m.get('revenue_growth', 0) or 0
        earnings_growth = m.get('earnings_growth', 0) or 0
        
        if pe and pe > 30 and revenue_growth < 20:
            bearish_signals.append(f"Growth doesn't justify valuation: {pe:.0f} P/E with only {revenue_growth*100:.0f}% revenue growth")
        
        if pe and pe > 50 and earnings_growth and earnings_growth < pe:
            bearish_signals.append(f"PEG disaster: P/E {pe:.0f} with earnings growth {earnings_growth*100:.0f}%")
        
        # Market cap vs fundamentals
        market_cap = m.get('market_cap')
        if market_cap and market_cap > 500e9:  # >$500B
            bearish_signals.append(f"Mega-cap ({market_cap/1e9:.0f}B) - harder to move, more scrutiny")
        
        # Sentiment check (if news available)
        news = data.get('company_news', [])
        if news:
            recent_news = news[:10]
            # Simple heuristic: lots of news can mean peak attention
            if len(recent_news) > 5:
                bearish_signals.append("High media attention - potential top signal")
        
        # Conviction calculation - Apex is more aggressive
        conviction = (len(bearish_signals) - len(bullish_signals)) / max(len(bearish_signals) + len(bullish_signals), 1) * 100
        conviction = max(-100, min(100, conviction))
        
        # Apex requires strong signals
        if conviction > 40:
            signal = "BEARISH"
        elif conviction < -40:
            signal = "BULLISH"
        else:
            signal = "NEUTRAL"
        
        return {
            "analyst": "Apex Analyst (Contrarian)",
            "signal": signal,
            "conviction": abs(conviction),
            "reasoning": {
                "valuation_extremes": {
                    "pe_ratio": round(pe, 1) if pe else None,
                    "ps_ratio": round(ps, 1) if ps else None,
                },
                "momentum": {
                    "price_90d_change": round(price_change_90d, 1) if len(prices) >= 90 else None,
                },
                "insider_activity": {
                    "sells": sells if insider_trades else None,
                    "buys": buys if insider_trades else None,
                    "net_shares": net_shares if insider_trades else None,
                },
                "bearish_signals": bearish_signals,
                "bullish_signals": bullish_signals,
            },
            "short_thesis": " - ".join(bearish_signals[:3]) if bearish_signals else "No Apex-style setup",
            "risk_factors": bullish_signals[:3] if bullish_signals else [],
        }


class CathieWoodAnalyst:
    """Innovation-focused - analyzes long-term disruption potential and growth sustainability."""
    
    def analyze(self, ticker: str, data: Dict) -> Dict:
        facts = data.get('company_facts', {})
        metrics_ttm = data.get('financial_metrics_ttm', [])
        line_items = data.get('line_items', [])
        
        sector = facts.get('sector', '')
        industry = facts.get('industry', '')
        
        if not metrics_ttm:
            return {"signal": "NEUTRAL", "conviction": 0, "reasoning": "No data"}
        
        m = metrics_ttm[0]
        
        bearish_signals = []
        bullish_signals = []
        
        # Sector analysis - CW loves disruptive innovation
        disruptive_sectors = ['Technology', 'Communication Services', 'Healthcare', 'Consumer Cyclical']
        traditional_sectors = ['Financial Services', 'Energy', 'Utilities', 'Industrials']
        
        if sector in disruptive_sectors:
            bullish_signals.append(f"Operates in disruptive sector: {sector}")
        elif sector in traditional_sectors:
            bearish_signals.append(f"Traditional sector ({sector}) - less disruption potential")
        
        # R&D investment (innovation indicator)
        revenue = None
        rd_expense = None
        if line_items:
            latest = line_items[0]
            revenue = latest.get('revenue') or latest.get('total_revenue')
            rd_expense = latest.get('research_and_development')
        
        if revenue and rd_expense:
            rd_ratio = rd_expense / revenue
            if rd_ratio > 15:
                bullish_signals.append(f"Strong R&D investment ({rd_ratio:.1f}% of revenue)")
            elif rd_ratio < 5:
                bearish_signals.append(f"Low R&D spend ({rd_ratio:.1f}% of revenue) - may lack innovation")
        
        # Growth sustainability
        revenue_growth = m.get('revenue_growth', 0) or 0
        earnings_growth = m.get('earnings_growth', 0) or 0
        
        if revenue_growth > 25:
            bullish_signals.append(f"Exceptional revenue growth ({revenue_growth*100:.0f}%) - disruptive potential")
        elif revenue_growth > 15:
            bullish_signals.append(f"Strong revenue growth ({revenue_growth*100:.0f}%)")
        elif revenue_growth < 5:
            bearish_signals.append(f"Slow growth ({revenue_growth*100:.0f}%) - not disruptive enough")
        
        # Margin expansion potential
        gross_margin = m.get('gross_margin', 0) or 0
        operating_margin = m.get('operating_margin', 0) or 0
        
        if gross_margin > 60:
            bullish_signals.append(f"High gross margin ({gross_margin*100:.0f}%) - scalable business model")
        elif gross_margin < 30:
            bearish_signals.append(f"Low gross margin ({gross_margin*100:.0f}%) - limited pricing power")
        
        # Market cap vs TAM (simplified)
        market_cap = m.get('market_cap')
        if market_cap:
            if market_cap > 1e12:  # >$1T
                bearish_signals.append(f"Mega-cap (${market_cap/1e9:.0f}B) - limited upside remaining")
            elif market_cap < 50e9:  # <$50B
                bullish_signals.append(f"Mid-cap (${market_cap/1e9:.0f}B) - room to grow")
        
        # FCF for reinvestment
        fcf_growth = m.get('free_cash_flow_growth')
        if fcf_growth and fcf_growth > 20:
            bullish_signals.append(f"Strong FCF growth ({fcf_growth*100:.0f}%) - capital for innovation")
        elif fcf_growth and fcf_growth < 0:
            bearish_signals.append(f"Declining FCF ({fcf_growth*100:.0f}%) - limits investment ability")
        
        # Conviction
        conviction = (len(bearish_signals) - len(bullish_signals)) / max(len(bearish_signals) + len(bullish_signals), 1) * 100
        conviction = max(-100, min(100, conviction))
        
        if conviction > 40:
            signal = "BEARISH"
        elif conviction < -40:
            signal = "BULLISH"
        else:
            signal = "NEUTRAL"
        
        return {
            "analyst": "Cathie Wood Analyst (Innovation)",
            "signal": signal,
            "conviction": abs(conviction),
            "reasoning": {
                "sector": sector,
                "industry": industry,
                "innovation_metrics": {
                    "rd_ratio": round(rd_expense/revenue*100, 1) if revenue and rd_expense else None,
                    "revenue_growth": round(revenue_growth*100, 1),
                    "gross_margin": round(gross_margin*100, 1),
                    "fcf_growth": round(fcf_growth*100, 1) if fcf_growth else None,
                },
                "market_cap": round(market_cap/1e9, 1) if market_cap else None,
                "bearish_signals": bearish_signals,
                "bullish_signals": bullish_signals,
            },
            "short_thesis": " - ".join(bearish_signals[:3]) if bearish_signals else "Innovation story intact",
            "risk_factors": bullish_signals[:3] if bullish_signals else [],
        }


class WarrenBuffettAnalyst:
    """Value investing - moat, management quality, predictable earnings."""
    
    def analyze(self, ticker: str, data: Dict) -> Dict:
        metrics_ttm = data.get('financial_metrics_ttm', [])
        metrics_annual = data.get('financial_metrics_annual', [])
        facts = data.get('company_facts', {})
        
        if not metrics_ttm:
            return {"signal": "NEUTRAL", "conviction": 0, "reasoning": "No data"}
        
        m = metrics_ttm[0]
        
        bearish_signals = []
        bullish_signals = []
        
        # Moat indicators - consistent ROE
        roe = m.get('return_on_equity', 0) or 0
        roic = m.get('return_on_invested_capital', 0) or 0
        
        if roe > 20:
            bullish_signals.append(f"Excellent ROE ({roe*100:.0f}%) - wide moat indicator")
        elif roe > 15:
            bullish_signals.append(f"Good ROE ({roe*100:.0f}%) - decent moat")
        elif roe < 10:
            bearish_signals.append(f"Poor ROE ({roe*100:.0f}%) - no moat")
        
        if roic and roic > 15:
            bullish_signals.append(f"Strong ROIC ({roic*100:.0f}%) - efficient capital allocation")
        elif roic and roic < 8:
            bearish_signals.append(f"Weak ROIC ({roic*100:.0f}%) - poor capital efficiency")
        
        # Margin consistency (simplified - just current level)
        net_margin = m.get('net_margin', 0) or 0
        operating_margin = m.get('operating_margin', 0) or 0
        
        if net_margin > 20:
            bullish_signals.append(f"Excellent net margin ({net_margin*100:.0f}%) - pricing power")
        elif net_margin > 10:
            bullish_signals.append(f"Good net margin ({net_margin*100:.0f}%)")
        elif net_margin < 5:
            bearish_signals.append(f"Thin net margin ({net_margin*100:.0f}%) - vulnerable business")
        
        # Debt levels - Buffett hates excessive debt
        debt_equity = m.get('debt_to_equity')
        if debt_equity and debt_equity < 0.5:
            bullish_signals.append(f"Conservative balance sheet (D/E: {debt_equity:.2f})")
        elif debt_equity and debt_equity > 1.5:
            bearish_signals.append(f"High debt (D/E: {debt_equity:.2f}) - risky")
        
        # Interest coverage
        interest_coverage = m.get('interest_coverage')
        if interest_coverage and interest_coverage > 10:
            bullish_signals.append(f"Strong interest coverage ({interest_coverage:.1f}x)")
        elif interest_coverage and interest_coverage < 5:
            bearish_signals.append(f"Weak interest coverage ({interest_coverage:.1f}x)")
        
        # Valuation - intrinsic value check
        pe = m.get('price_to_earnings_ratio')
        pb = m.get('price_to_book_ratio')
        
        # Buffett buys wonderful companies at fair prices
        if pe and pe < 20 and roe > 15:
            bullish_signals.append(f"Good value: P/E {pe:.1f} with {roe*100:.0f}% ROE")
        elif pe and pe > 30:
            bearish_signals.append(f"Too expensive: P/E of {pe:.1f} - not a buy")
        
        if pb and pb < 3:
            bullish_signals.append(f"Reasonable P/B ({pb:.1f})")
        elif pb and pb > 8:
            bearish_signals.append(f"Rich P/B ({pb:.1f})")
        
        # Earnings predictability (simplified - check if earnings positive)
        eps = m.get('earnings_per_share')
        if eps and eps > 0:
            bullish_signals.append("Profitable operations")
        else:
            bearish_signals.append("Not profitable - unpredictable")
        
        # FCF generation
        fcf_yield = m.get('free_cash_flow_yield')
        if fcf_yield and fcf_yield > 5:
            bullish_signals.append(f"Strong FCF yield ({fcf_yield*100:.1f}%)")
        elif fcf_yield and fcf_yield < 2:
            bearish_signals.append(f"Weak FCF yield ({fcf_yield*100:.1f}%)")
        
        # Conviction
        conviction = (len(bearish_signals) - len(bullish_signals)) / max(len(bearish_signals) + len(bullish_signals), 1) * 100
        conviction = max(-100, min(100, conviction))
        
        if conviction > 40:
            signal = "BEARISH"
        elif conviction < -40:
            signal = "BULLISH"
        else:
            signal = "NEUTRAL"
        
        return {
            "analyst": "Warren Buffett Analyst (Value)",
            "signal": signal,
            "conviction": abs(conviction),
            "reasoning": {
                "moat_indicators": {
                    "roe": round(roe*100, 1),
                    "roic": round(roic*100, 1) if roic else None,
                    "net_margin": round(net_margin*100, 1),
                },
                "financial_strength": {
                    "debt_to_equity": round(debt_equity, 2) if debt_equity else None,
                    "interest_coverage": round(interest_coverage, 1) if interest_coverage else None,
                },
                "valuation": {
                    "pe_ratio": round(pe, 1) if pe else None,
                    "pb_ratio": round(pb, 1) if pb else None,
                    "fcf_yield": round(fcf_yield*100, 2) if fcf_yield else None,
                },
                "bearish_signals": bearish_signals,
                "bullish_signals": bullish_signals,
            },
            "short_thesis": " - ".join(bearish_signals[:3]) if bearish_signals else "Quality business, not a short",
            "risk_factors": bullish_signals[:3] if bullish_signals else [],
        }


class MichaelBurryAnalyst:
    """Deep value / contrarian - hunts accounting red flags, bubbles, and hidden problems."""
    
    def analyze(self, ticker: str, data: Dict) -> Dict:
        metrics_ttm = data.get('financial_metrics_ttm', [])
        metrics_annual = data.get('financial_metrics_annual', [])
        line_items = data.get('line_items', [])
        insider_trades = data.get('insider_trades', [])
        
        if not metrics_ttm:
            return {"signal": "NEUTRAL", "conviction": 0, "reasoning": "No data"}
        
        m = metrics_ttm[0]
        
        bearish_signals = []
        bullish_signals = []
        
        # Look for accounting red flags
        
        # 1. Revenue vs Cash Flow divergence
        revenue_growth = m.get('revenue_growth', 0) or 0
        # Simplified - ideally compare to operating cash flow growth
        
        # 2. Deteriorating margins
        gross_margin = m.get('gross_margin', 0) or 0
        operating_margin = m.get('operating_margin', 0) or 0
        net_margin = m.get('net_margin', 0) or 0
        
        if len(metrics_annual) >= 2:
            prev_gross = metrics_annual[1].get('gross_margin', 0) or 0
            if gross_margin < prev_gross * 0.9:  # 10% decline
                bearish_signals.append(f"DECLINING MARGINS: Gross margin deteriorating")
        
        # 3. High debt with weak coverage
        debt_equity = m.get('debt_to_equity')
        interest_coverage = m.get('interest_coverage')
        
        if debt_equity and debt_equity > 2:
            if interest_coverage and interest_coverage < 4:
                bearish_signals.append(f"DANGER: High debt (D/E {debt_equity:.2f}) with weak coverage ({interest_coverage:.1f}x)")
            else:
                bearish_signals.append(f"High debt load (D/E {debt_equity:.2f})")
        
        # 4. Stock-based compensation (if available)
        # Would need line item data
        
        # 5. Insider selling (major Burry signal)
        if insider_trades:
            recent = insider_trades[:30]
            sells = sum(1 for t in recent if t.get('transaction_shares', 0) < 0)
            buys = sum(1 for t in recent if t.get('transaction_shares', 0) > 0)
            net_value = sum((t.get('transaction_shares', 0) or 0) * (t.get('transaction_price_per_share', 0) or 0) for t in recent)
            
            if sells > 0 and buys == 0:
                bearish_signals.append(f"RED FLAG: Only insider selling, zero buys ({sells} transactions)")
            elif sells > buys * 2:
                bearish_signals.append(f"Heavy insider selling: {sells} sells vs {buys} buys")
            
            if net_value < -1e6:  # >$1M net selling
                bearish_signals.append(f"Insiders net sold ${abs(net_value)/1e6:.1f}M")
        
        # 6. Valuation extremes
        pe = m.get('price_to_earnings_ratio')
        ps = m.get('price_to_sales_ratio')
        pb = m.get('price_to_book_ratio')
        
        if pe and pe > 50:
            bearish_signals.append(f"BUBBLE TERRITORY: P/E of {pe:.1f}")
        elif pe and pe > 35:
            bearish_signals.append(f"Extreme valuation: P/E {pe:.1f}")
        
        if ps and ps > 20:
            bearish_signals.append(f"Insane P/S of {ps:.1f}x - 2000 dot-com vibes")
        
        # 7. Growth slowing
        earnings_growth = m.get('earnings_growth', 0) or 0
        if earnings_growth < 0:
            bearish_signals.append(f"Earnings contraction ({earnings_growth*100:.0f}%)")
        elif earnings_growth < 5 and pe and pe > 25:
            bearish_signals.append(f"Growth slowing ({earnings_growth*100:.0f}%) but valuation still rich")
        
        # 8. Working capital issues
        current_ratio = m.get('current_ratio')
        if current_ratio and current_ratio < 1:
            bearish_signals.append(f"LIQUIDITY RISK: Current ratio {current_ratio:.2f} < 1")
        
        # 9. Negative FCF
        fcf_growth = m.get('free_cash_flow_growth')
        fcf_yield = m.get('free_cash_flow_yield')
        if fcf_yield and fcf_yield < 0:
            bearish_signals.append("NEGATIVE FREE CASH FLOW - burning cash")
        
        # 10. Compare price to book for asset bubble
        if pb and pb > 15:
            bearish_signals.append(f"Price far exceeds book value (P/B {pb:.1f}) - asset bubble")
        
        # Conviction - Burry needs strong evidence
        conviction = (len(bearish_signals) - len(bullish_signals)) / max(len(bearish_signals) + len(bullish_signals), 1) * 100
        conviction = max(-100, min(100, conviction))
        
        # Burry only shorts when he sees clear problems
        if conviction > 50:
            signal = "BEARISH"
        elif conviction < -40:
            signal = "BULLISH"
        else:
            signal = "NEUTRAL"
        
        return {
            "analyst": "Michael Burry Analyst (Deep Value)",
            "signal": signal,
            "conviction": abs(conviction),
            "reasoning": {
                "red_flags_detected": len(bearish_signals),
                "valuation": {
                    "pe_ratio": round(pe, 1) if pe else None,
                    "ps_ratio": round(ps, 1) if ps else None,
                    "pb_ratio": round(pb, 1) if pb else None,
                },
                "financial_health": {
                    "debt_to_equity": round(debt_equity, 2) if debt_equity else None,
                    "interest_coverage": round(interest_coverage, 1) if interest_coverage else None,
                    "current_ratio": round(current_ratio, 2) if current_ratio else None,
                },
                "insider_activity": {
                    "sells": sells if insider_trades else None,
                    "buys": buys if insider_trades else None,
                },
                "bearish_signals": bearish_signals,
                "bullish_signals": bullish_signals,
            },
            "short_thesis": " - ".join(bearish_signals[:4]) if bearish_signals else "No clear accounting/fundamental red flags",
            "risk_factors": bullish_signals[:3] if bullish_signals else [],
        }


class RiskManagementAnalyst:
    """Risk management - position sizing, stop losses, risk/reward analysis."""
    
    def analyze(self, ticker: str, data: Dict, other_signals: List[Dict]) -> Dict:
        prices = data.get('prices', [])
        metrics_ttm = data.get('financial_metrics_ttm', [])
        
        if not prices:
            return {"signal": "NEUTRAL", "conviction": 0, "reasoning": "No price data"}
        
        current_price = prices[-1]['close']
        highs = [p['high'] for p in prices]
        lows = [p['low'] for p in prices]
        
        high_52w = max(highs)
        low_52w = min(lows)
        
        # Volatility analysis
        if len(prices) >= 20:
            recent_returns = [(prices[i]['close'] - prices[i-1]['close']) / prices[i-1]['close'] for i in range(1, len(prices))]
            daily_vol = statistics.stdev(recent_returns[-20:]) if len(recent_returns) >= 20 else 0.02
            annualized_vol = daily_vol * math.sqrt(252) * 100
        else:
            annualized_vol = 30  # Default
        
        # Aggregate other analyst signals
        bearish_count = sum(1 for s in other_signals if s.get('signal') == 'BEARISH')
        bullish_count = sum(1 for s in other_signals if s.get('signal') == 'BULLISH')
        neutral_count = sum(1 for s in other_signals if s.get('signal') == 'NEUTRAL')
        
        avg_conviction = statistics.mean([s.get('conviction', 0) for s in other_signals]) if other_signals else 0
        
        # Risk/Reward calculation
        # Potential reward: distance to support (52w low or key level)
        # Potential risk: distance to resistance (52w high)
        
        downside_target = low_52w * 0.9  # 10% below 52w low as extreme case
        upside_risk = high_52w * 1.1  # 10% above 52w high
        
        potential_reward = (current_price - downside_target) / current_price * 100
        potential_risk = (upside_risk - current_price) / current_price * 100
        
        risk_reward_ratio = potential_reward / potential_risk if potential_risk > 0 else 0
        
        # Position sizing recommendation
        # Base: 5% of portfolio
        # Adjust based on conviction and volatility
        
        base_position = 5.0  # percent
        
        # High conviction = larger position
        conviction_multiplier = min(2.0, max(0.5, avg_conviction / 50))
        
        # High volatility = smaller position
        vol_multiplier = min(1.5, max(0.5, 30 / annualized_vol)) if annualized_vol > 0 else 1.0
        
        # Analyst agreement = larger position
        agreement_multiplier = 1.0 + (bearish_count / len(other_signals) - 0.5) if other_signals else 1.0
        
        recommended_position = base_position * conviction_multiplier * vol_multiplier * agreement_multiplier
        recommended_position = max(1, min(10, recommended_position))  # Cap between 1-10%
        
        # Stop loss recommendation
        # Place above recent highs or key resistance
        recent_high = max([p['high'] for p in prices[-20:]]) if len(prices) >= 20 else current_price * 1.05
        stop_loss_price = recent_high * 1.03  # 3% above recent high
        stop_loss_pct = (stop_loss_price - current_price) / current_price * 100
        
        # Take profit levels
        tp1 = current_price * 0.90  # 10% down
        tp2 = current_price * 0.80  # 20% down
        tp3 = downside_target  # Extreme case
        
        # Risk assessment
        risk_level = "LOW"
        if annualized_vol > 50:
            risk_level = "HIGH"
        elif annualized_vol > 30:
            risk_level = "MEDIUM"
        
        # Short-term technical risk
        if current_price > highs[-5] * 0.98:  # Near recent highs
            risk_level = "HIGH" if risk_level != "HIGH" else risk_level
        
        bearish_signals = []
        if bearish_count >= 5:
            bearish_signals.append(f"Strong analyst agreement: {bearish_count}/{len(other_signals)} bearish")
        if risk_reward_ratio > 2:
            bearish_signals.append(f"Favorable risk/reward: {risk_reward_ratio:.2f}x")
        if annualized_vol < 25:
            bearish_signals.append(f"Low volatility ({annualized_vol:.0f}%) -可控 risk")
        
        bullish_signals = []
        if bearish_count <= 2:
            bullish_signals.append(f"Weak analyst agreement: only {bearish_count}/{len(other_signals)} bearish")
        if risk_reward_ratio < 1:
            bullish_signals.append(f"Poor risk/reward: {risk_reward_ratio:.2f}x")
        if annualized_vol > 40:
            bullish_signals.append(f"High volatility ({annualized_vol:.0f}%) - elevated risk")
        if current_price < low_52w * 1.1:
            bullish_signals.append("Near 52-week lows - may be oversold")
        
        # Final signal
        if bearish_count >= 4 and risk_reward_ratio > 1.5:
            signal = "BEARISH"
        elif bearish_count <= 2 or risk_reward_ratio < 0.8:
            signal = "BULLISH"
        else:
            signal = "NEUTRAL"
        
        return {
            "analyst": "Risk Management",
            "signal": signal,
            "conviction": avg_conviction,
            "reasoning": {
                "volatility": {
                    "annualized_vol": round(annualized_vol, 1),
                    "risk_level": risk_level,
                },
                "analyst_consensus": {
                    "bearish": bearish_count,
                    "bullish": bullish_count,
                    "neutral": neutral_count,
                    "avg_conviction": round(avg_conviction, 1),
                },
                "risk_reward": {
                    "ratio": round(risk_reward_ratio, 2),
                    "potential_reward_pct": round(potential_reward, 1),
                    "potential_risk_pct": round(potential_risk, 1),
                },
                "position_sizing": {
                    "recommended_pct": round(recommended_position, 1),
                    "base_pct": base_position,
                    "conviction_mult": round(conviction_multiplier, 2),
                    "vol_mult": round(vol_multiplier, 2),
                    "agreement_mult": round(agreement_multiplier, 2),
                },
                "trade_parameters": {
                    "entry_price": round(current_price, 2),
                    "stop_loss": round(stop_loss_price, 2),
                    "stop_loss_pct": round(stop_loss_pct, 1),
                    "take_profit_1": round(tp1, 2),
                    "take_profit_2": round(tp2, 2),
                    "take_profit_3": round(tp3, 2),
                },
                "bearish_signals": bearish_signals,
                "bullish_signals": bullish_signals,
            },
            "short_thesis": " - ".join(bearish_signals[:3]) if bearish_signals else "Risk/reward not compelling",
            "risk_factors": bullish_signals[:3] if bullish_signals else [],
        }


class PortfolioManager:
    """Synthesizes all analyst views into final recommendation."""
    
    def synthesize(self, ticker: str, all_signals: List[Dict]) -> Dict:
        # Extract signals
        technical = next((s for s in all_signals if s['analyst'] == 'Technical Analyst'), {})
        fundamental = next((s for s in all_signals if s['analyst'] == 'Fundamental Analyst'), {})
        apex = next((s for s in all_signals if s['analyst'] == 'Apex Analyst (Contrarian)'), {})
        cathie = next((s for s in all_signals if s['analyst'] == 'Cathie Wood Analyst (Innovation)'), {})
        buffett = next((s for s in all_signals if s['analyst'] == 'Warren Buffett Analyst (Value)'), {})
        burry = next((s for s in all_signals if s['analyst'] == 'Michael Burry Analyst (Deep Value)'), {})
        risk = next((s for s in all_signals if s['analyst'] == 'Risk Management'), {})
        
        # Count signals
        bearish_count = sum(1 for s in all_signals if s.get('signal') == 'BEARISH')
        bullish_count = sum(1 for s in all_signals if s.get('signal') == 'BULLISH')
        neutral_count = sum(1 for s in all_signals if s.get('signal') == 'NEUTRAL')
        
        total = len(all_signals)
        
        # Weighted conviction
        # Give more weight to certain analysts for short signals
        weights = {
            'Technical Analyst': 1.0,
            'Fundamental Analyst': 1.2,
            'Apex Analyst (Contrarian)': 1.3,
            'Cathie Wood Analyst (Innovation)': 0.8,
            'Warren Buffett Analyst (Value)': 1.1,
            'Michael Burry Analyst (Deep Value)': 1.4,
            'Risk Management': 1.2,
        }
        
        weighted_bearish = 0
        weighted_total = 0
        
        for s in all_signals:
            weight = weights.get(s['analyst'], 1.0)
            weighted_total += weight
            if s.get('signal') == 'BEARISH':
                weighted_bearish += weight * (s.get('conviction', 50) / 100)
            elif s.get('signal') == 'NEUTRAL':
                weighted_bearish += weight * 0.3  # Neutral leans slightly bearish for shorts
        
        final_conviction = (weighted_bearish / weighted_total) * 100 if weighted_total > 0 else 0
        
        # Final decision
        if bearish_count >= 5 and final_conviction > 50:
            final_signal = "STRONG SHORT"
        elif bearish_count >= 4 and final_conviction > 40:
            final_signal = "SHORT"
        elif bearish_count >= 3 and final_conviction > 30:
            final_signal = "WEAK SHORT"
        elif bullish_count >= 4:
            final_signal = "NO SHORT / BULLISH"
        else:
            final_signal = "NEUTRAL / NO ACTION"
        
        # Position size recommendation
        if final_signal == "STRONG SHORT":
            position_size = "5-8% of portfolio"
        elif final_signal == "SHORT":
            position_size = "3-5% of portfolio"
        elif final_signal == "WEAK SHORT":
            position_size = "1-3% of portfolio (speculative)"
        else:
            position_size = "0% - No position"
        
        # Key risks summary
        key_risks = []
        for s in all_signals:
            key_risks.extend(s.get('risk_factors', [])[:2])
        key_risks = list(set(key_risks))[:5]  # Dedupe and limit
        
        # Key thesis points
        key_thesis = []
        for s in all_signals:
            if s.get('short_thesis') and s.get('signal') == 'BEARISH':
                key_thesis.append(f"{s['analyst']}: {s['short_thesis']}")
        
        return {
            "ticker": ticker,
            "final_signal": final_signal,
            "conviction": round(final_conviction, 1),
            "analyst_breakdown": {
                "bearish": bearish_count,
                "bullish": bullish_count,
                "neutral": neutral_count,
                "total": total,
            },
            "position_sizing": position_size,
            "key_thesis_points": key_thesis[:5],
            "key_risks": key_risks,
            "all_analyst_signals": all_signals,
        }


def main():
    """Run the 7-analyst framework on META and JPM."""
    
    tickers = ["META", "JPM"]
    results = {}
    
    for ticker in tickers:
        print(f"\n{'='*100}")
        print(f"ANALYZING {ticker}")
        print(f"{'='*100}\n")
        
        data = DATA.get(ticker, {})
        
        if not data:
            print(f"No data available for {ticker}")
            continue
        
        # Run all 7 analysts
        analysts = [
            TechnicalAnalyst(),
            FundamentalAnalyst(),
            ApexAnalyst(),
            CathieWoodAnalyst(),
            WarrenBuffettAnalyst(),
            MichaelBurryAnalyst(),
        ]
        
        all_signals = []
        for analyst in analysts:
            print(f"Running {analyst.__class__.__name__}...")
            signal = analyst.analyze(ticker, data)
            all_signals.append(signal)
            
            print(f"  Signal: {signal['signal']}")
            print(f"  Conviction: {signal['conviction']}%")
            print()
        
        # Run Risk Management (needs other signals)
        print("Running Risk Management...")
        risk_analyst = RiskManagementAnalyst()
        risk_signal = risk_analyst.analyze(ticker, data, all_signals)
        all_signals.append(risk_signal)
        print(f"  Signal: {risk_signal['signal']}")
        print(f"  Conviction: {risk_signal['conviction']}%")
        print()
        
        # Portfolio Manager synthesis
        print("Portfolio Manager synthesizing...")
        pm = PortfolioManager()
        final_recommendation = pm.synthesize(ticker, all_signals)
        
        results[ticker] = final_recommendation
        
        # Print summary
        print(f"\n{'='*100}")
        print(f"{ticker} FINAL RECOMMENDATION")
        print(f"{'='*100}")
        print(f"Signal: {final_recommendation['final_signal']}")
        print(f"Conviction: {final_recommendation['conviction']}%")
        print(f"Position Size: {final_recommendation['position_sizing']}")
        print(f"\nAnalyst Breakdown:")
        print(f"  Bearish: {final_recommendation['analyst_breakdown']['bearish']}/{final_recommendation['analyst_breakdown']['total']}")
        print(f"  Bullish: {final_recommendation['analyst_breakdown']['bullish']}/{final_recommendation['analyst_breakdown']['total']}")
        print(f"  Neutral: {final_recommendation['analyst_breakdown']['neutral']}/{final_recommendation['analyst_breakdown']['total']}")
        
        if final_recommendation['key_thesis_points']:
            print(f"\nKey Short Thesis Points:")
            for point in final_recommendation['key_thesis_points'][:3]:
                print(f"  • {point}")
        
        if final_recommendation['key_risks']:
            print(f"\nKey Risks:")
            for risk in final_recommendation['key_risks'][:3]:
                print(f"  ⚠ {risk}")
    
    # Save full results
    output_file = '/home/zohair/.openclaw/workspace/swarm-trader/short_analysis_results.json'
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n{'='*100}")
    print(f"Full results saved to: {output_file}")
    print(f"{'='*100}\n")
    
    return results


if __name__ == "__main__":
    main()
