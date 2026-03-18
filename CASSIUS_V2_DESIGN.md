# Cassius V2 — Trading System Redesign

## What Failed in V1

1. **No hard risk management.** Cassius could hold losers indefinitely. No stop losses, no position limits enforced at execution time.
2. **Leveraged ETF overnight holds.** TQQQ/SOXL/UPRO are 3x leveraged — designed for intraday, not multi-day. They decay and amplify drawdowns.
3. **Sector concentration.** 53% in AI infra with a 50% "cap" that was advisory, not enforced.
4. **LLM discretion without guardrails.** Cassius made buy/sell decisions with no systematic framework — just vibes from an LLM reading financial data.
5. **No benchmark awareness.** Never compared performance to SPY/QQQ. Lost 22% in a flat market.
6. **Overtrading.** 135 orders, 31 tickers, 2+ trades/day with no edge.

## V2 Architecture: Rules First, LLM Second

### Principle: The system enforces rules. Cassius proposes within them.

```
Hard Rules (code-enforced, Cassius CANNOT override)
  ↓
Cassius Proposes Trades (LLM reasoning within constraints)  
  ↓
Validator (rejects anything violating rules)
  ↓
Executor (places orders with mandatory stops)
  ↓
Monitor (tracks performance, kills underperformers)
```

### Hard Rules (Non-Negotiable, Code-Enforced)

| Rule | Value | Rationale |
|------|-------|-----------|
| Max position size | 8% of equity | No single stock can sink the portfolio |
| Max sector allocation | 30% | Diversification > conviction |
| Mandatory stop loss | -7% from entry | Cut losers fast |
| Mandatory trailing stop | 15% from peak | Lock in gains |
| Max daily loss (circuit breaker) | -2% | Stop trading for the day |
| Max weekly loss (circuit breaker) | -5% | Reduce position sizes by 50% next week |
| No leveraged ETFs | Period | They killed V1 |
| No moonshots > 3% total | 3% cap | Lottery tickets stay small |
| Min cash reserve | 20% | Always have dry powder |
| Max trades per day | 4 | Prevent overtrading |
| Max open positions | 12 | Focus > scatter |
| No new buys if portfolio > -3% for the day | Hard stop | Live to fight tomorrow |

### Universe V2

```python
UNIVERSE_V2 = {
    "core_tech": {
        "label": "Core Technology (high quality, profitable)",
        "tickers": ["NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN"],
        "max_sector_pct": 0.30,
        "max_per_stock_pct": 0.08,
    },
    "growth": {
        "label": "Growth (strong revenue, reasonable valuation)",
        "tickers": ["PLTR", "AMD", "CRM", "SNOW", "NET", "PANW"],
        "max_sector_pct": 0.25,
        "max_per_stock_pct": 0.08,
    },
    "value_dividend": {
        "label": "Value & Dividend (stability, income)",
        "tickers": ["JPM", "V", "UNH", "JNJ", "PG", "KO"],
        "max_sector_pct": 0.25,
        "max_per_stock_pct": 0.08,
    },
    "tactical": {
        "label": "Tactical (event-driven, momentum)",
        "tickers": ["COIN", "MSTR", "RKLB", "SMCI"],
        "max_sector_pct": 0.15,
        "max_per_stock_pct": 0.05,
    },
    "hedge": {
        "label": "Hedge / Direction",
        "tickers": ["SPY", "QQQ", "GLD", "TLT"],
        "max_sector_pct": 0.15,
        "max_per_stock_pct": 0.10,
    },
}
```

**Key changes:**
- Removed leveraged ETFs entirely (TQQQ, SOXL, UPRO)
- Removed moonshots (IONQ, RGTI, SOUN, LUNR)
- Added value/dividend for stability
- Added hedge instruments (GLD, TLT) for downside protection
- Hard per-stock and per-sector caps
- Diversified across growth stages

### Cassius's New Role

Cassius doesn't decide WHETHER to follow rules — he decides WHAT to buy/sell WITHIN them.

His LLM analysis now focuses on:
1. **Relative strength ranking** — which stocks in the universe look best right now?
2. **Entry timing** — is this a good price relative to recent action?
3. **Position sizing** — how much conviction (within the 8% max)?
4. **Exit reasoning** — beyond the mandatory stops, are there fundamental reasons to exit early?

### New Execution Flow

```
1. MORNING (9:30 AM) — Portfolio Health Check
   - Check all positions against stop losses
   - Auto-sell anything hitting -7% stop
   - Auto-adjust trailing stops
   - Report current state
   
2. MIDDAY (12:00 PM) — Analysis & Proposals  
   - Cassius analyzes universe with fresh data
   - Proposes up to 4 trades
   - Validator checks all rules
   - Execute approved trades with mandatory brackets

3. CLOSE (3:30 PM) — End of Day Review
   - Performance report
   - Compare to SPY/QQQ
   - Log all trades with reasoning
   - Flag any positions approaching stops
```

### Performance Tracking (New)

- Daily: compare to SPY, track cumulative alpha
- Weekly: performance report with Sharpe ratio estimate
- Monthly: full review, adjust strategy if underperforming
- Every trade logged with entry price, stop, target, reasoning, outcome

### Implementation Priority

1. `risk_manager.py` — Hard rules enforcement (validate every trade)
2. Update `execute_trades.py` — Mandatory bracket orders (stop + trailing stop on every buy)
3. Update `src/config.py` — New universe, new limits
4. `portfolio_monitor.py` — Auto-stop enforcement, daily health check
5. Update Cassius cron prompts — New constraints in system prompt
6. `performance_tracker_v2.py` — SPY-relative tracking, Sharpe, trade journal
