# Performance data model

Part of the [OpenStock README](../README.md).

- `trades` records one position lot. An open trade has no close date; a completed trade keeps entry and exit prices, fees, currency and EUR conversion rates.
- `capital_events` never count as P/L.
- `equity_snapshots` are captured by the monitor and by state-changing journal operations, so equity curves and drawdown do not depend on today's prices.
- Analytics GET requests are read-only.
- Annualized return and estimated yearly profit need at least 30 calendar days since initial capital. Volatility and Sharpe need 30 equity observations.
