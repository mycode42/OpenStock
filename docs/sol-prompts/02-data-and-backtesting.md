# Task 2 — Data contracts and reproducible backtesting

Use the system contract and complete Task 1 first. Implement the smallest
vertical slice for **equity end-of-day or approved intraday data only**. No
execution and no automated target changes.

Build provider interfaces and validated, versioned schemas for: instruments,
quotes/bars, splits, dividends, fundamentals, and news metadata. Persist raw
payload hashes and normalized observations separately. Add freshness rules,
deduplication, provenance, UTC timestamps, failure states, and a provider
health view. Existing Yahoo Finance data may be used for experimentation only
after its terms and data limitations are documented; do not describe it as
licensed production execution data. Do not select a paid provider.

Create a reproducible backtest runner, preferably as a LEAN evaluation spike
behind an adapter. It must require a fixed data snapshot and strategy version,
reject future timestamps, and output an immutable run manifest. Model trading
calendar, splits, dividends, fees, bid/ask spread, conservative slippage,
latency, position/cash constraints, and delisted symbols where data permits.

Add a small fixture dataset plus tests that deliberately catch: look-ahead
bias, split/dividend adjustment error, duplicate input, stale input, trading
outside market hours, and non-deterministic reruns. Produce one baseline
strategy with no claim of profitability and a report containing return,
volatility, maximum drawdown, turnover, exposure, tail loss, sample count,
assumptions, and data gaps.
