# Changelog

All notable changes to OpenStock. The Algo Trader subtree keeps its own log in [algo-trader/CHANGELOG.md](algo-trader/CHANGELOG.md).

## Unreleased

### Changed

- The ThetaData history backfill is one unattended pipeline (`scripts/options/theta_pipeline.py`): the last 20 sessions, then EOD, then opening archives. It resumes daily from cron and, once complete, sends a Telegram message and a research-page notice to cancel the subscription and remove the key. Its status is shown on the Algo research page.
- Scheduled jobs write to one log file each under `algo-trader/data/algo/logs/`, capped daily by `scripts/rotate_logs.py`. The research page lists them with their last error.
- Fixed the opening backfill, which imported a helper that did not exist.
- The Journal equity curve for the manual Alpaca account is continuous. The server takes a snapshot every five minutes into `alpaca_equity_snapshots` (migration 031), and the curve uses those points from the first snapshot on. Days before that still come from Alpaca's daily history.
- Demo: the simulated broker's positions, cash, and open orders survive a restart. Orders the demo database tracks are rebuilt on start, so cancelling after a restart works.
- Demo: one simulated price per stock. The Dashboard, Algo Universe, and quotes read the same price.
- Demo: the Journal's return, equity curve, YTD P/L, and win rate agree. The demo has sample closed trades, so the win rate and YTD P/L are not empty. The Algo 1D equity chart is a seeded random walk, not a sine wave.
- Demo: the banner is readable in light mode.
- Demo: the demo database no longer seeds the legacy EUR stock rows, so `/api/stocks` shows only the demo watchlist.

### Removed

- `/api/analytics/dashboard`. It reported legacy EUR trade data, and the web app did not use it. The Journal reads `/api/alpaca/journal`.

## 3.1.3 - 2026-10-30

### Added

- Demo mode runs the real Alpaca code paths against a simulated paper account. The demo has a USD watchlist with alert levels and held positions. Orders can be placed, cancelled and filled, and they are accepted only in the demo database. The dry-run preview (`preview: true`) is available through the API only, not in the web app. Nothing is sent to a broker.
- The Research Lab shows in demo mode, read-only. Starting a run is refused there.
- The README uses new screenshots from the demo, and `docs/features.md` mirrors its features and screenshots.

### Changed

- Demo stock history follows the 04:00–20:00 New York session, and the Algo demo equity history uses the snapshot format.
- The old EUR demo data and the old demo journal were removed.

### Fixed

- Trade pagination, the research archive filters, the research lab gauge and strategy chips, and the Algo Universe hover chart use the theme colours. Light themes no longer show black or fixed dark surfaces.

## 3.1.2 - 2026-10-30

### Added

- The options paper account records intraday equity snapshots every five minutes, around the clock, and a close after each regular session. The Algo account and the options account now have the same snapshot series.
- Account snapshots are stored in the `account_snapshots` table of `algo_state.sqlite3`. `algo-trader/scripts/migrate_snapshot_files.py` moved the earlier JSON and JSONL files into it. It checked every record first and then removed the files.
- Algo equity and options history: the 1D view is the latest 24 hours, with the most recent snapshot on the right edge. The week and longer views use the 04:00–20:00 New York session. Charts show the viewer's time zone and an even two-hour axis.
- Dashboard alert cards link retired and triggered alerts to Configure, where they are rearmed.
- The README starts with a demo quickstart, then covers local setup, the Algo Trader and Docker or Podman.

### Changed

- Stock chart times are shown in New York time (ET).
- Configure and other notifications appear in a fixed layer, so they no longer move the page.

### Fixed

- The Algo equity history read only the last 50 intraday rows per day, which cut most of each session. Full sessions now load.
- Options history uses its own snapshots once they exist. The risk journal fills only the days before the first snapshot.

## 3.1.0 - 2026-10-29

### Breaking

- **Python 3.11 is no longer supported.** The Algo Trader requires Python 3.12 (`requires-python >=3.12,<3.13`). If you run the Algo Trader on 3.11, move to 3.12 and run `poetry install` again. The lock file dropped two packages that were pinned only for 3.11.
- **`/api/stocks` uses USD field names.** Request and response fields `*_eur` are now `*_usd`, for example `buy_threshold_usd`, `sell_threshold_usd` and `price_usd`. API clients must update. Stored database column names are unchanged.
- **Live trading is an explicit opt-in.** A broker URL other than the paper endpoint needs `ALPACA_ALLOW_LIVE=1`. Without it, the Python endpoint module refuses to load. Live manual orders and turning real orders on also need `OPENSTOCK_ORDER_TOKEN`. Paper needs neither.
- **Origin required for API writes.** Scripts and curl calls to `POST`, `PUT`, `PATCH` and `DELETE` under `/api` must send an `Origin` header matching the server's origin. Requests without one get 403. See the [API doc](docs/api.md#origin-requirement).

### Added

- **Per-order cap.** No single order may exceed $100,000 of notional value, on paper and live. A budget above the cap is refused when it is saved.
- **Live daily cap.** Live orders may total at most $300,000 of notional value per UTC day. Paper orders are not counted.
- The Research Lab shows demo strategies in demo mode instead of an error.
- `Dockerfile` and `.dockerignore`. Secrets, databases and the Algo Trader tree are kept out of the image.
- A Python CI job in the root `verify.yml` workflow. Issue templates moved to the repository root.
- `.mailmap` merges one contributor's identities, and the history figures in `docs/project-history.md` are current.

### Fixed

- A rearmed alert level notifies again on its next firing. Each firing gets its own outbox row.
- The options research checkpoint tolerates the coarse file clock when checking that a report belongs to the current run.
- Demo mode blanks the equity and options Alpaca keys as well as the manual ones.
- The Algo Trader's Poetry is resolved from `PATH`, and the research prompt uses the `{ROOT}` placeholder.
- The Alpaca base-URL aliases are named for the endpoint they point at (`ALGO_TRADING_BASE`, `OPTIONS_TRADING_BASE`).
- Pull-to-refresh and the Configure page follow the active theme. Configure buttons share one size.
- The demo blanks every broker URL, the live switch and the order token, so it can never show live mode or trade.

### Security

- **Origin enforcement.** Every write under `/api` must come from this site. A request with no Origin header is refused with 403, and so is one from another site. Paper orders and Research Lab writes are covered.
- **Forwarded headers need a proxy.** `X-Forwarded-Host` and `X-Forwarded-Proto` are honoured only when `OPENSTOCK_TRUST_PROXY=1` is set. Before, a script could send a foreign Origin with a matching `X-Forwarded-Host` and pass the check.
- **Python order guard.** An order that cannot be valued (no limit price and no notional) is refused, rather than passed through.

### Documentation

- Removed claims the code does not keep: order previews in the web app, and live URLs being refused. The docs now describe the live opt-in and the cap.

## 3.0.2 - 2026-10-29

- Docs describe the paper default accurately. Broker base URLs default to paper; a different URL is an explicit override.
- Demo mode serves Research Lab demo data.
- Release tag `v3.0.2` was moved to this release commit.

## 3.0.0

- Alpaca-only release: the Trade Republic integration is removed.
- Manual limit orders from alert levels, with a paper default.
- Frozen trading-gate policy with a freeze test.
