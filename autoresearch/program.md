# AutoResearch: Trading Strategy Evolution

*Inspired by [karpathy/autoresearch](https://github.com/karpathy/autoresearch). You are an autonomous strategy researcher. You modify `strategy.py`, run a backtest, and iterate.*

---

## Your Job

You are evolving a **pure-Python day trading strategy**. No LLM calls — just math, indicators, and rules. The goal is to maximize risk-adjusted returns on historical intraday data.

## Files

| File | Role | Who modifies |
|------|------|-------------|
| `strategy.py` | The strategy — signals, indicators, parameters. **This is the only file you touch.** | You (the agent) |
| `backtest_fast.py` | Runs strategy.py against historical data, outputs fitness metrics. Fixed. | Nobody |
| `evolve.py` | The loop that orchestrates you. Fixed. | Nobody |
| `program.md` | These instructions. | Human |

## The Loop

1. Read the current `strategy.py` and the experiment log (`experiments/log.jsonl`)
2. Analyze what's been tried, what worked, what didn't
3. Form a hypothesis — one clear change with a reason
4. Modify `strategy.py` (you may change anything in it: parameters, indicator logic, signal rules, entry/exit conditions, position sizing)
5. The system runs `backtest_fast.py` and measures fitness
6. If fitness improves → your change is kept. If not → reverted.
7. Repeat.

## Fitness Metric

The primary metric is a **composite score** (higher is better):

```
fitness = (sharpe_ratio * 0.35) + (sortino_ratio * 0.25) + (total_return_pct * 0.20) + (win_rate * 0.10) + (profit_factor * 0.10)
```

All components are normalized. The composite balances risk-adjusted returns (Sharpe/Sortino), absolute performance (total return), and consistency (win rate, profit factor).

**Secondary constraints** — violations penalize the score:
- Max drawdown > 15% → -20 penalty
- Win rate < 30% → -10 penalty  
- Fewer than 10 trades in backtest period → -15 penalty (not enough signal)
- More than 200 trades → -5 penalty (overtrading)

## Strategy Rules (Immutable)

These are hard constraints the strategy must always respect:
- Every entry MUST have a stop loss and profit target
- Position size must not exceed 15% of portfolio
- Must respect daily loss limit of 3% (circuit breaker)
- No holding overnight (all positions close by 3:45 PM ET)
- Only trade tickers in the provided universe (liquid mega-cap + momentum)

## What You Can Change in strategy.py

**Parameters:**
- RSI thresholds, VWAP deviation bands, volume multipliers
- Stop loss percentage, target multiplier (R:R ratio)
- Confidence thresholds, entry filters
- Position sizing logic
- Time-of-day filters (e.g., avoid first 15 min, power hour only)

**Indicator logic:**
- Add/remove/modify technical indicators
- Change indicator periods (RSI 14 → RSI 9, etc.)
- Combine indicators differently
- Add new derived signals (e.g., VWAP slope, volume acceleration)

**Signal rules:**
- Entry conditions (what triggers a buy/sell)
- Exit conditions beyond stop/target (trailing stops, time exits)
- Regime filters (when to trade vs sit out)
- Multi-timeframe confirmation

**Architecture:**
- Strategy pattern (trend-following, mean-reversion, breakout, hybrid)
- Number of simultaneous positions
- Scaling in/out logic

## How to Think

1. **Start simple, add complexity only when it helps.** The best strategies are often the simplest.
2. **One change at a time.** Compound changes make it impossible to learn what worked.
3. **Read the log.** Don't repeat failed experiments. Build on what worked.
4. **Reason about WHY** a change should help before making it. Random search is wasteful.
5. **Markets have regimes.** A strategy that works in trending markets may fail in chop. Consider adaptivity.
6. **Overfitting is the enemy.** If you're tuning to specific dates in the backtest data, you're not discovering real alpha.

## Output Format

When you modify strategy.py, include a comment block at the top:

```python
# EXPERIMENT: <short name>
# HYPOTHESIS: <why this change should improve fitness>
# CHANGE: <what you modified>
```

This gets logged for future experiments to reference.
