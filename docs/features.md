# Features

Part of the [OpenStock README](../README.md). Each screen is shown in the demo, which runs with `npm run demo`; see [Demo mode](demo-mode.md).


- **Dashboard:** Alpaca USD quotes, alert levels, and Alpaca price-history charts.
  
  <img width="800" alt="Dashboard with a stock price-history chart open" src="screenshots/dashboard-chart.png" />

- **Monitoring:** a one-shot monitor cycle (run by `npm run monitor` or a scheduler) that checks BUY and SELL thresholds against Alpaca quotes. Alerts fire once per episode and use a durable outbox for Telegram delivery.

  <img width="800" alt="Configure: alert levels with the real-order switch and budget" src="screenshots/configure-alerts.png" />

- **Trading journal:** reads Alpaca account balances, positions, and fills in USD.
  
  <img width="800" alt="Trading journal" src="screenshots/journal.png" />

- **Capital events:** initial capital, deposits, withdrawals and financing are tracked separately, so they never count as trading P/L.

  <img width="800" alt="Journal: performance details, capital movements and trades" src="screenshots/journal-details.png" />

- **Analytics:** P/L, risk, equity curve, drawdown, monthly and per-stock performance. Figures show `N/A` or `Insufficient data` until enough history exists.
  
  <img width="800" alt="Journal analytics" src="screenshots/journal-details.png" />

- **Configure:** alert levels per stock, real-order switches with a budget for each level, and manual limit orders from an alert. The demo runs the same page on simulated data.

  <img width="800" alt="Configure with the alert editor open" src="screenshots/configure.png" />

- **Themes:** twelve colour themes, each in light, dark or system mode. The choice is saved in the browser.

  <img width="800" alt="Settings: theme and colour mode" src="screenshots/settings.png" />

- **Algo Trader pages:** performance, research and universe views for the Algo Trader installation, with its strategy settings editable from the UI.

  <img width="800" alt="Algo Trader: equity account" src="screenshots/algo-equity.png" />

  <img width="800" alt="Algo Trader: options account" src="screenshots/algo-options.png" />

  <img width="800" alt="Algo Trader research" src="screenshots/research.png" />


  <img width="800" alt="Algo Universe" src="screenshots/algo-universe.png" />

## Features at a glance

- **Alerts that fire once.** Each level queues one alert in a durable outbox. The outbox retries delivery and never sends twice.
- **A journal that never invents P/L.** Deposits and withdrawals are tracked separately from trading results.
- **Figures that admit missing data.** Metrics show `N/A` or `Insufficient data` until enough history exists.
