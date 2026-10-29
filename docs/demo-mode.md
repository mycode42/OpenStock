# Demo mode

Part of the [OpenStock README](../README.md).

`npm run demo` starts OpenStock with simulated quotes and charts. It:

- ignores any local `.env` file and every broker credential variable, so it never contacts a broker;
- stores its data in `data/demo/openstock.db`, separate from any real database;
- fills the trading journal with sample lots, deposits and a 150-day equity curve, so Journal and Analytics have data to show. Delete `data/demo/` to start over;
- shows a banner in the interface listing the setup still missing.

Normal startup uses Alpaca when its API key and secret are configured. Without Alpaca credentials, OpenStock starts in demo mode. Algo Trader is optional and does not control the stock dashboard's demo state. The application reports missing setup through `GET /api/demo-status`.
