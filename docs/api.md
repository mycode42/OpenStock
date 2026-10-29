# API

Part of the [OpenStock README](../README.md).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check and version |
| `/api/demo-status` | GET | Whether demo mode is active and what setup is missing |
| `/api/dashboard` | GET | Stock dashboard data |
| `/api/live-prices` | GET | Display-only Alpaca quotes |
| `/api/alpaca/journal` | GET | Read-only Alpaca account, positions, and fills |
| `/api/alpaca/alert-levels/:id/rearm` | POST | Rearm a copied Alpaca alert level |
| `/api/alpaca/orders/config` | GET | Order settings and trading mode (`paper` or `live`) |
| `/api/alpaca/orders` | GET | Recent orders |
| `/api/alpaca/alert-levels/:id/orders` | POST | Place a limit order for a level. Body `{"preview": true}` returns the plan only; `{"confirm": true}` submits. |
| `/api/alpaca/orders/:id/cancel` | POST | Cancel a live order. |
| `/api/stocks/:id/history?broker=alpaca` | GET | Alpaca chart history |

Mutating requests must come from the same origin as the page.
