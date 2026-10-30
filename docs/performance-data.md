# Performance data model

Part of the [OpenStock README](../README.md).

- `trades` records one position lot. An open trade has no close date; a completed trade keeps entry and exit prices, fees, currency and EUR conversion rates.
- `capital_events` never count as P/L.
- `alpaca_equity_snapshots` holds the manual Alpaca account every five minutes, like the Algo and options accounts. The Journal equity curve uses these points from the first snapshot on, and Alpaca's daily history for the days before.
- `equity_snapshots` are captured by the monitor and by state-changing journal operations, so equity curves and drawdown do not depend on today's prices.
- Analytics GET requests are read-only.
- Annualized return and estimated yearly profit need at least 30 calendar days since initial capital. Volatility and Sharpe need 30 equity observations.
