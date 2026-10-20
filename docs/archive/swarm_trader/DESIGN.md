# Trading System V2 — Design Document

## What Failed in V1

1. **No hard risk management.** The agent could hold losers indefinitely. No stop losses, no position limits enforced at execution time.
2. **Leveraged ETF overnight holds.** TQQQ/SOXL/UPRO are 3x leveraged — designed for intraday, not multi-day. They decay and amplify drawdowns.
3. **Sector concentration.** 53% in AI infra with a 50% "cap" that was advisory, not enforced.
4. **LLM discretion without guardrails.** The agent made buy/sell decisions with no systematic framework — just vibes from an LLM reading financial data.
5. **No benchmark awareness.** Never compared performance to SPY/QQQ. Lost 22% in a flat market.
6. **Overtrading.** 135 orders, 31 tickers, 2+ trades/day with no edge.

## V2 Architecture: Rules First, LLM Second

### Principle: The system enforces rules. The agent proposes within them.

```
Hard Rules (code-enforced, agent CANNOT override)
  ↓
Agent Proposes Trades (LLM reasoning within constraints)  
  ↓
Validator (rejects anything violating rules)
  ↓
Executor (places orders with mandatory stops)
  ↓
Monitor (tracks performance, kills underperformers)
```

### Hard Rules (Non-Negotiable, Code-Enforced)

| Rule | Swing | Day | Rationale |
|------|-------|-----|-----------|
| Max position size | 8% | 15% | No single stock sinks the portfolio |
| Max sector allocation | 30% | 50% | Diversification > conviction |
| Stop loss | -7% | -1.2% | Cut losers fast |
| Trailing stop | 15% from peak | 3% intraday | Lock in gains |
| Daily circuit breaker | -2% | -3% | Stop trading for the day |
| Weekly circuit breaker | -5% | -8% | Reduce size next week |
| Leveraged ETFs | ❌ Banned | ✅ Intraday OK | They killed V1 in swing mode |
| Moonshots | ❌ Blocked | ❌ Blocked | Not in any V2 universe |
| Min cash reserve | 20% | 10% | Always have dry powder |
| Max trades/day | 4 | 20 | Prevent overtrading |
| Max positions | 12 | 8 | Focus > scatter |
| No buys if down | -3% | -2% | Live to fight tomorrow |
| EOD flatten | No | Yes (3:45 PM ET) | Day trades don't hold overnight |

### Agent's Role in V2

The agent doesn't decide WHETHER to follow rules — it decides WHAT to buy/sell WITHIN them.

LLM analysis focuses on:
1. **Relative strength ranking** — which stocks in the universe look best right now?
2. **Entry timing** — is this a good price relative to recent action?
3. **Position sizing** — how much conviction (within the max)?
4. **Exit reasoning** — beyond the mandatory stops, are there fundamental reasons to exit early?

### Execution Flow

```
1. MORNING — Portfolio Health Check
   - Check all positions against stop losses
   - Auto-sell anything hitting stops
   - Auto-adjust trailing stops
   - Report current state
   
2. MIDDAY — Analysis & Proposals  
   - Agent analyzes universe with fresh data
   - Proposes trades (up to mode limit)
   - Risk manager validates all rules
   - Execute approved trades with mandatory brackets

3. CLOSE — End of Day Review
   - Performance report
   - Compare to SPY/QQQ
   - Log all trades with reasoning
   - Flag any positions approaching stops
   - Day mode: mandatory flatten
```

### Performance Tracking

- Daily: compare to SPY, track cumulative alpha
- Weekly: performance report with Sharpe ratio estimate
- Monthly: full review, adjust strategy if underperforming
- Every trade logged with entry price, stop, target, reasoning, outcome

### Implementation

1. `risk_manager.py` — Hard rules enforcement (validate every trade)
2. `execute_trades.py` — Mandatory bracket orders on every entry
3. `src/config.py` — Universe, limits, mode configuration
4. `src/accounts.py` — Multi-account routing (swing vs day)
5. `portfolio_monitor.py` — Auto-stop enforcement, daily health check
6. `performance_tracker_v2.py` — SPY-relative tracking, Sharpe, trade journal
