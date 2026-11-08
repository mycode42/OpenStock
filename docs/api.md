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

## Research lab and system load

These routes are read by the Research page. Writes need a same-origin request, and in the public demo they are refused with HTTP 409.

- `GET /api/algo-trader/research/lab?account=equity|options`: strategies, their live parameters, profiles, and entry and exit rules. The overview also says whether the lab is read-only (`read_only`).
- `GET /api/algo-trader/research/plan?account=` and `PUT /api/algo-trader/research/plan`: the profile chosen for each strategy, kept between visits.
- `GET /api/algo-trader/research/plan/preview?account=`: the settings changes a plan would make. Empty in the demo.
- `POST /api/algo-trader/research/plan/apply` and `POST /api/algo-trader/research/plan/rollback`: write or restore the settings. Rollback refuses when the settings changed after the apply. Send `{"force": true}` to roll back anyway.
- `POST /api/algo-trader/research/profiles`: save a custom profile. Numbers, or one of the listed words for `slope_mode` and `regime_filter`.
- `POST /api/algo-trader/research/jobs` and `POST /api/algo-trader/research/jobs/:id/cancel`: start or cancel a lab run. `GET /research/jobs/:id` returns its stage progress.
- `GET /api/algo-trader/research/control` and the nightly and kill routes: nightly timers and running research. Without a systemd user session the state reads `available: false`.
- `GET /api/system/load`: CPU, memory, load and process figures for this host. The public demo returns sample figures instead.
