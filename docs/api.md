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
| `/api/algo-trader/research/data-status` | GET | Read-only status of the ThetaData history pipeline and the scheduled job logs. It reports whether `THETA_DATA_API_KEY` is set, never its value. Demo mode returns an empty status. |

## Origin requirement

Every `POST`, `PUT`, `PATCH` and `DELETE` under `/api` must send an `Origin` header that matches the server's own origin, meaning scheme, host and port. A request with no `Origin` header, or one from another site, gets `403 {"error": "Same-origin mutation required"}`. `GET` requests need no `Origin`.

For a local install, use the address you run OpenStock on:

```sh
curl -X POST http://127.0.0.1:3000/api/stocks \
  -H "Origin: http://127.0.0.1:3000" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","name":"Apple"}'
```

Behind a reverse proxy, set `OPENSTOCK_TRUST_PROXY=1` so the server honours the proxy's `X-Forwarded-Host` and `X-Forwarded-Proto`, and send the public origin, for example `https://openstock.example.com`. When that setting is on, the proxy must overwrite any `X-Forwarded-Host` and `X-Forwarded-Proto` the client sends, so a client cannot supply its own. Without the setting, the forwarded headers are ignored, so a client cannot make a foreign origin match.

Live orders additionally need `ALPACA_ALLOW_LIVE=1` and the `X-OpenStock-Order-Token` header. See [Configuration](configuration.md).
