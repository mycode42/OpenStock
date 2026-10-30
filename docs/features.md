# Features

Part of the [OpenStock README](../README.md).

## Dashboard

Alpaca USD quotes, alert levels, and Alpaca price-history charts.

<img width="800" alt="Dashboard with a stock price-history chart open" src="docs/screenshots/dashboard-chart.png" />

## Monitoring

A one-shot monitor cycle, run by `npm run monitor` or a scheduler, checks BUY and SELL thresholds against Alpaca quotes. Alerts fire once per episode and use a durable outbox for Telegram delivery. See [Monitoring](monitoring.md).

<img width="800" alt="Configure: alert levels with the real-order switch and budget" src="docs/screenshots/configure-alerts.png" />

## Trading journal

Reads Alpaca account balances, positions, and fills in USD. OpenStock does not submit orders from the journal.

<img width="800" alt="Trading journal" src="docs/screenshots/journal.png" />

## Capital events

Initial capital, deposits, withdrawals and financing are tracked separately, so they never count as trading P/L.

<img width="800" alt="Journal: performance details, capital movements and trades" src="docs/screenshots/journal-details.png" />

## Analytics

P/L, risk, equity curve, drawdown, monthly and per-stock performance. Figures show `N/A` or `Insufficient data` until enough history exists. See [Performance data](performance-data.md).

<img width="800" alt="Journal analytics" src="docs/screenshots/journal-details.png" />

## Algo Trader pages

Performance research and universe views for the Algo Trader installation in [algo-trader/](../algo-trader/README.md), with its strategy settings editable from the UI.

<img width="800" alt="Algo Trader: equity account" src="docs/screenshots/algo-equity.png" />

Research view:

<img width="800" alt="Algo Trader: options account" src="docs/screenshots/algo-options.png" />

Universe view:

<img width="800" alt="Algo Trader research" src="docs/screenshots/research.png" />
