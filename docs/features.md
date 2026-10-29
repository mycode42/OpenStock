# Features

Part of the [OpenStock README](../README.md).

## Dashboard

Alpaca USD quotes, alert levels, and Alpaca price-history charts.

<img width="800" alt="Screenshot 2026-10-28 at 13 48 03" src="https://github.com/user-attachments/assets/8a077760-4125-43cc-83d0-3141a5f4bd0c" />

## Monitoring

A one-shot monitor cycle, run by `npm run monitor` or a scheduler, checks BUY and SELL thresholds against Alpaca quotes. Alerts fire once per episode and use a durable outbox for Telegram delivery. See [Monitoring](monitoring.md).

<img width="800" alt="Screenshot 2026-10-28 at 15 15 38" src="https://github.com/user-attachments/assets/249c61da-0a47-489f-8650-b91414a8fc06" />

## Trading journal

Reads Alpaca account balances, positions, and fills in USD. OpenStock does not submit orders from the journal.

<img width="800" alt="Screenshot 2026-10-28 at 13 34 18" src="https://github.com/user-attachments/assets/9a7c47a9-cffd-4236-98e1-7b2860158c21" />

## Capital events

Initial capital, deposits, withdrawals and financing are tracked separately, so they never count as trading P/L.

<img width="800" alt="Screenshot 2026-10-28 at 13 38 43" src="https://github.com/user-attachments/assets/daeb17d4-47c6-4541-be2f-2d83fbcab2c8" />

## Analytics

P/L, risk, equity curve, drawdown, monthly and per-stock performance. Figures show `N/A` or `Insufficient data` until enough history exists. See [Performance data](performance-data.md).

<img width="800" alt="Screenshot 2026-10-28 at 13 35 05" src="https://github.com/user-attachments/assets/7c4db67e-85fc-40b0-8472-8a50cacef214" />

## Algo Trader pages

Performance research and universe views for the Algo Trader installation in [algo-trader/](../algo-trader/README.md), with its strategy settings editable from the UI.

<img width="800" alt="Screenshot 2026-10-28 at 13 20 51" src="https://github.com/user-attachments/assets/b2bdc6a4-3884-49e0-890b-bb00282a4274" />

Research view:

<img width="800" alt="Screenshot 2026-10-28 at 13 21 27" src="https://github.com/user-attachments/assets/ae0d0fc7-2303-4786-ab2f-91b396c69b73" />

Universe view:

<img width="800" alt="Screenshot 2026-10-28 at 13 45 58" src="https://github.com/user-attachments/assets/34114b8a-af13-4e7d-8a9a-67d89ca7b0ab" />
