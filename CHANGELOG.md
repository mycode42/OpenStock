# Changelog

All notable changes to OpenStock, including the Algo Trader. Each release lists its dashboard changes and its Algo Trader changes.

## Unreleased

### Changed

- AGENTS.md states the money rule without currency: the Alpaca accounts are USD.

### Fixed

- Migration backups are written only for existing databases, and `*.pre-*.backup` files are git-ignored wherever they are written.
- The demo's one-week chart ends on the live quote, so it matches the card price.
- Alerts are not sent or logged per cycle while Telegram is not configured; they stay pending in the outbox. A failing alert is retried up to 10 times, then left in the outbox with one error line.
- Destructive migrations copy the database file first (`*.pre-*.backup`, git-ignored). Migration 032 is marked destructive.
- The demo keeps the market open every day: intraday bars, the one-week chart and fills follow the quotes on weekends too.
- The popup title on the Algo Universe and dashboard charts uses the theme text colour, so it is readable in light mode.
- Job logs on the research page show dates in the app's format, and the demo shows sample job logs and a 14-run research history, so the pager is visible.
- Without a Telegram recipient, the monitor logs the warning once per process. The monitor logs in `memory/` are trimmed once they pass 5 MB.
- The README and the 3.2.0 entry point to the current version, and the 3.2.0 entry is fixed.

- The live parameter tables on the Research page use the same two columns for every strategy, so the values line up between the swing and intraday tabs.

### Removed

- Legacy EUR data (migration `032_remove_legacy_eur_data`): the 58 EUR journal trades from the old Trade Republic import and their merge history, and the EUR stocks the Alpaca watchlist does not use, with their alert levels and price history. Six EUR stocks stay, because the Alpaca watchlist still points at them (`source_stock_id` is NOT NULL). USD rows and the Alpaca watchlist are unchanged.

## 3.2.1 - 2026-11-07

### Changed

- Theta opening backfill: a day that still fails in three separate runs is a known provider gap. It no longer keeps the archive incomplete, so options research stops reporting partial data for it. Known gaps are retried weekly, and the manifest reports them as `known_provider_gaps`.
- The daily report services (equity research, options research and the LLM digest) start their job log fresh on each run, so the dashboard shows the latest run rather than an old failure.

### Fixed

- The job list on the research page no longer treats a "(0 errors)" summary as an error.
- The loading arrow on the Algo pages uses the theme accent colour in every theme, with no box behind it.
- The Algo Universe hover charts use the dark background of the dashboard charts in dark themes.
- On phones, the expanded CPU and RAM gauge opens under the small gauge, so the small gauge stays visible and clickable.

## 3.2.0 - 2026-11-07

Major release. The Algo jobs no longer depend on OpenClaw or cron, and the dashboard runs its own price monitor.

### Highlights

- **Scheduling moved to systemd user timers.** Every Algo job is a timer in `algo-trader/ops/systemd/` with `OnCalendar=... America/New_York`, so jobs fire at Eastern time on any host clock. The crontab and the OpenClaw Algo automations are gone.
- **One dashboard service.** The price monitor runs inside `openstock.service` every 30 seconds. The separate monitor timer and unit are removed.
- **Event calendar refresh removed**, with its module, test, docs, and `data/event_calendar.csv`.
- **Research page.** Job logs show each job's last run and turn red on error. Research history is paginated, newest first, 10 per page.

### Added

- `algo-trader/scripts/market/export_demo_sessions.py` exports real intraday sessions from the market history for the demo's 1-day charts. The demo uses them when `server/demo-sessions.json` exists, and falls back to the simulated bars when it does not.

### Changed

- Algo scheduled jobs are systemd user timers (`algo-trader/ops/systemd/*.timer`), installed by `algo-trader/scripts/install_units.py`. Each timer's schedule is in America/New_York. The crontab, `scripts/cron-env.sh`, and the OpenClaw Algo automations are removed. OpenClaw runs none of the Algo jobs.
- The Alpaca opening and history captures are timers (`options-opening-capture`, `options-history-capture`). Each run appends a timestamped line to its log, including successful runs.
- The research page's scheduled-job log list shows each job's name and last-written time. A job whose last run had an error shows its name in red. Tap a job for its size, last error, and last line.
- The research history is paginated, newest first, 10 runs per page, with Previous and Next. Paging keeps the scroll position.
- The price monitor runs inside `openstock.service` every 30 seconds (`lib/monitor-cycle.js`). The separate `openstock-monitor` timer and service were removed, so restarting the dashboard restarts the monitor too. Cycles never overlap, and demo mode runs none. `npm run monitor` still runs one cycle by hand.
- Frontend asset version markers and the module import versions are 3.2.0, so returning visitors fetch the new files.
- The price monitor runs inside `openstock.service` every 30 seconds (`lib/monitor-cycle.js`). The separate `openstock-monitor` timer and service were removed, so restarting the dashboard restarts the monitor too. Cycles never overlap, and demo mode runs none. `npm run monitor` still runs one cycle by hand.
- The entry caps went up: daily 10 to 15, swing group 6 to 11, and the per-strategy caps listed in the defaults file. Once the trading-day count was fixed, the old caps would still have throttled the strategies on a day with many valid signals. The hard dollar guards are unchanged: $100,000 per order and the daily loss limits.
- Daily entry limits count against the trading day. Daily-bar signals reference the previous session's bar, so counting by that date charged yesterday's entries to today.
- Entry caps: overall daily 15, swing group 11, intraday group 4. Per strategy: swing trend 3, swing multi-factor 3, swing momentum 3, swing mean reversion 4, swing LLM-driven 2, intraday mean reversion 2, intraday trend following 2, intraday momentum 2.
- The regime's volatility stress counts the high-yield credit spread only alongside an elevated volatility input. On its own, a wide credit spread no longer cuts risk.
- The options cycle logs one line per rejected candidate with its reason.
- The research page repaints only when its own data changes, and keeps the scroll position.

### Fixed

- The Journal chart labels on the y-axis show distinct values when neighbouring ticks are close together.
- The dashboard price history popup uses the same grid colour as the Algo Universe, so it is no longer heavy in light mode.
- The Journal equity curve and its KPI sparkline use the theme accent, so they show in light themes.
- The dashboard's price history charts have price labels on the y-axis.
- The research status panel keeps its last status on refresh instead of flashing "Loading".

### Removed

- The Algo event calendar refresh (`equity_research/event_calendar_refresh.py`), its test, its docs, and `data/event_calendar.csv`.
- The Algo crontab (`algo-trader/ops/crontab`) and `algo-trader/scripts/cron-env.sh`.
- `algo-trader/ops/automations.json` and `algo-trader/ops/openclaw-crons.md`, the OpenClaw job mirrors.
- The `openstock-monitor.service` and `openstock-monitor.timer` units.
- `www/sw.js`, the offline service worker. It was never registered, so it did nothing, and the checks that read it are removed.

### Algo Trader

- Algo LLM calls no longer depend on OpenClaw. The silent run reads `LLM_PROVIDER` and `LLM_MODEL` from `.env` and fails if they are unset; it no longer falls back to the OpenClaw model config.
- New `scripts/llm_report.py` runs a prompt file through the configured provider (hosted or local Ollama or OpenAI-compatible) and can post the reply to Telegram. It is the cron replacement for the agent-turn briefing and research jobs. A stale snapshot produces a notice without a model call.
- LLM-driven signal snapshots are stored in SQLite (`data/algo/llm_driven/signals.sqlite3`, table `llm_signal_snapshot`, last 200 runs kept) instead of `data/algo/llm_driven/signals.json`. `python -m strategies.llm_driven.snapshot_cli` prints the newest snapshot with its freshness. The briefing and silent-run prompts use it, so they no longer depend on the working directory. The JSON file is no longer written or read.
- Algo extended-hours trading is re-enabled for plain limit DAY or GTC orders during premarket, after-hours, and overnight sessions. Extended-hours entries and exits never carry a broker bracket or OCO; software stops protect them, and broker protection is attached at the next regular open.
- Overnight Algo entries are enabled by default (`overnight_entries_enabled`).
- Extended-session quotes are fresh for 300 seconds instead of 90, so overnight and premarket entries and protective updates are not paused by indicative BOATS quotes. Regular-session quotes keep the 90-second window.

## 3.1.4 - 2026-10-30

### Added

- The ThetaData history backfill is one unattended pipeline (`scripts/options/theta_pipeline.py`): the last 20 sessions, then EOD, then opening archives. It resumes daily from cron and, once complete, sends a Telegram message and a research-page notice to cancel the subscription and remove the key. Its status is shown on the Algo research page.
- Scheduled jobs write to one log file each under `algo-trader/data/algo/logs/`, capped daily by `scripts/rotate_logs.py`. The research page lists them with their last error.
- The Journal equity curve for the manual Alpaca account is continuous. The server takes a snapshot every five minutes into `alpaca_equity_snapshots` (migration 031), and the curve uses those points from the first snapshot on. Days before that still come from Alpaca's daily history.

### Changed

- Release 3.1.4 bumps the asset version in every page URL (`?v=3.1.4`), so returning visitors fetch the new files.
- The demo blanks `OPENSTOCK_TRUST_PROXY`, like the other broker settings.
- `docs/api.md` lists `/api/algo-trader/research/data-status`.
- The crontab's only absolute path is the `ALGO` line, and the health job reads the user's uid at run time (`scripts/cron-env.sh`).

### Fixed

- Fixed the opening backfill, which imported a helper that did not exist.
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

### Algo Trader 3.1.1 - 2026-10-27

Patch release: CI fixes and test cleanup. No runtime behavior changes.

**Fixed**

- `poetry.lock` refreshed after the `requires-python` cap, so the CI
  lock check passes again. Only environment markers changed.
- Trade-guard tests no longer depend on Alpaca credentials or the live
  market calendar.

**Changed**

- Removed a duplicate parametrization of the rule-sell deferral test (the
  `oco` case covered the same open-sell check as `bracket`).

### Algo Trader 3.1.0 - 2026-10-27

Release: software-managed protection replaces broker brackets, the
equity and options LLM sleeve is renamed `llm_driven`, and research
integrity gates are tightened.

**Added**

- Software-managed protection: stops, targets and trailing state live in the
  `position_protection` table and are evaluated every scan in every session.
  A daemon heartbeat raises an alarm when positions are open and the daemon
  has stopped scanning.
- Protection migration (`portfolio.protection.migration`): dry run by default;
  writes protection rows before cancelling any broker exit legs.
- Versioned profile store (`equity_research.profile_versions`): every promotion
  and revert is recorded with parameters, fitness and lineage, plus a
  `revert` command.
- Final holdout registry with `initialize`, `status` and `supersede`
  commands. The nightly run only reads holdout status.
- Deflated Sharpe Ratio promotion gate, with a rolling 28-day trial count and
  a nightly discovery cap of 60 evaluations per strategy.
- Live drift feed written by the daemon after each regular close. The nightly
  run reads it and can revert flagged profiles (dry run by default).
- Options: Alpaca trailing-12-month dividend yields with a dated research
  snapshot; FRED DGS3MO risk-free rate cache; spread-aware option slippage
  shared by live pricing and research fills.
- Strategy rename migration (`strategies.migrations.rename_llm_driven`):
  dry run by default, backs up before writing.

**Changed**

- Broker bracket and OCO orders are no longer submitted. Extended-hours,
  overnight and regular-session exits use software triggers.
- Regime changes block new entries only; held positions exit through their
  own stop, trailing, target or time rules.
- Renamed `event_driven` to `llm_driven` for the equity strategy
  (`swing_llm_driven`) and the options sleeve (`options_llm_driven`).
  Calendar-reaction entries were removed; LLM signals are the only entry path.
- Leveraged ETFs are tradable (the `allow_leveraged_etfs` gate is removed).
- Earnings blackout removed from options universe selection.
- Equity: `risk_per_trade_pct` 2%, per-strategy `max_position_pct` 7%,
  tactical sector cap 10%.
- Options: per-trade risk 7% for the VRP, long-volatility and directional
  sleeves; `min_llm_confidence` 0.80; `close_on_regime_exit` false by default.
- Momentum exits use per-name rank hysteresis (exit outside 2 x top_n).
- Route weights and risk scales are separate (`risk_scale` per strategy).
- Equity cost constants are shared in `equity_research/costs.py`.

**Fixed**

- Pending buy reservations no longer count cover legs of shorts.
- Retryable exit refusals no longer pause entries.
- `--status` NameError in the portfolio daemon.
- Covered-call sizing no longer double-counts shares committed to open calls.
- Option theta is per calendar day for the final two days before expiry.
- Research gamma uses closed-form European values instead of noisy tree
  finite differences.
- Vendor option vega, rho and theta scales verified against Alpaca snapshots.
- Options paper sleeve error path NameError (`LOG`).
- Monitor report crash when the state store changed.

**Upgrade notes**

- Stop `algo-trader` and `options-paper`, run the strategy rename migration
  with `--apply`, then the protection migration (dry run first), then start
  both services.
- Run `openstock` after updating its configuration keys to the `llm_driven`
  names.

## 3.0.2 - 2026-10-29

- Docs describe the paper default accurately. Broker base URLs default to paper; a different URL is an explicit override.
- Demo mode serves Research Lab demo data.
- Release tag `v3.0.2` was moved to this release commit.

## 3.0.0

- Alpaca-only release: the Trade Republic integration is removed.
- Manual limit orders from alert levels, with a paper default.
- Frozen trading-gate policy with a freeze test.

### Algo Trader 3.0.0 - 2026-10-20

Major release: independent options paper trading and validation join the
coordinated Algo equity portfolio.

**Added**

- A dedicated Alpaca Paper options route with VRP, llm-driven, and optional
  directional strategy sleeves, separate order/fill journals, and combined
  equity/options risk checks.
- Options-specific rolling walk-forward research with realistic execution
  costs, chain-quality filters, Greek exposure tracking, assignment checks,
  stress tests, and an untouched holdout.
- Parallel nightly options research alongside equity AutoResearch, with its
  own report section and artifacts.
- Broker fill-based FIFO realized P&L reporting for long and short positions.

**Changed**

- Refreshed the project guide, command examples, configuration map, and release
  metadata for the current coordinator, research, reporting, and paper-trading
  workflows.
- Consolidated the runtime into domain packages and kept the Algo strategy
  coordinator as the only route for Algo equity orders.

### Algo Trader 2.0.0 - 2026-10-09

V2.0.0 makes the Algo portfolio a coordinated, multi-horizon long/short system and adds a strict research promotion gate. Swing, Day, and Algo continue to use separate fixed universes and paper-account routes.

**Added**

- A portfolio coordinator before the risk manager with per-name, sector, gross/net exposure, shared entry-budget, session, and regime-throttle controls. It can reject or reduce proposals; the risk manager remains the final approval layer.
- Daily swing trend and momentum strategies plus a separate cyclical mean-reversion strategy. The five-minute trend and momentum variants remain disabled; open-dip and opening-range breakout remain the intraday strategies.
- Directional long/short support for the core strategies and the lower-budget multi-factor and scheduled-event satellites.
- Daily-only multi-factor ranking and scheduled-event reaction/follow-through exits.
- Portfolio replay diagnostics for after-cost strategy returns, buy-and-hold excess returns, pairwise strategy-P&L correlation, and same-name directional overlap.
- A chronological walk-forward promotion gate with at least three folds, held-out evaluation, mandatory trading costs, minimum trade counts, parameter-neighbor stability, and fixed test windows.

**Changed**

- Algo strategy lifecycle and coordinator state are persisted in SQLite; execution continues through the shared risk manager and Alpaca order path.
- The coordinator schedules intraday positions to flatten by default and reserves only a slice of the daily entry budget for intraday strategies.
- Removed the legacy web flow-editor application from the package; the maintained trading, research, and operations interfaces are the command-line tools and OpenClaw jobs documented here.
- Stopped tracking generated AutoResearch experiment logs; `.gitignore` keeps deployment-specific runs local.
- Package metadata and release documentation now identify V2.0.0.

**Validation**

- `poetry check --lock`
- `poetry build` (source distribution and wheel)
- `poetry run python -m pytest -q` (250 passed)
- GitHub Actions runs the same validation gate for the release commit and tag.
- Paper-trading software only. Research candidates do not change live parameters, and this release does not certify strategy profitability.

### Algo Trader 1.6.0 - 2026-10-08

V1.6 adds research-only cross-sectional and scheduled-event strategies to the Algo portfolio, consolidates live and replay rule evaluation, and fixes Algo on a curated universe while keeping Swing and Day universes separate.

**Added**

- Cross-sectional multi-factor signals with point-in-time feature calculation, ranking diagnostics, and purged factor-weight research.
- Scheduled earnings and macro-event signals with a point-in-time calendar, freshness checks, and conservative reaction windows.
- Daily Algo AutoResearch artifacts and candidate reports; research jobs do not modify live parameters or submit orders.
- Strategy-specific entry cooldowns and tactical position-count limits.
- Fixed 22-symbol Algo universe and documented, separate Swing and Day universes.

**Changed**

- Live Algo scans and portfolio replay share the same pure rule-signal pipeline.
- Algo walk-forward replay aggregates daily bars once and uses indexed prior closes to reduce repeated work.
- Event-calendar cron now reports failures and skipped runs through the configured Telegram alert.
- Swing, Day, and Algo bucket caps are documented as independent exposure ceilings, not target allocations.

**Validation**

- GitHub Actions test command: `poetry run python -m pytest -q` (215 passed).
- Paper trading only; AutoResearch candidates require explicit review before live configuration changes.

### Algo Trader 1.5.0 - 2026-10-08

V1.5 consolidates the separate paper-trading routes and adds a continuously running Algo strategy portfolio with persisted coordinator state and research tooling.

**Added**

- Dedicated Swing, Day, and Algo Alpaca account routing with broker-side account ID verification.
- Persisted Algo coordinator and per-strategy lifecycle state, startup reconciliation, health reporting, and execution safeguards.
- Feed-aware market data for Basic and Algo Trader Plus, including BOATS routing for eligible overnight scans.
- Local Algo portfolio replay, bounded signal-parameter profiles, chronological walk-forward evaluation, and slippage sensitivity reports.
- Focused GitHub Actions test workflow.

**Changed**

- OpenClaw prompts and operating notes document separate modes, explicit feed handling, and service health checks.
- Algo research replay accepts regular-session IEX or SIP only; BOATS requires a separate overnight replay and fill model.
- Package metadata uses the V1.5 version and PEP 621 project fields.

**Configuration notes**

- Copy `.env.example` and configure separate paper-account credentials and account IDs for each enabled trading route.
- The Algo service uses the fixed Alpaca paper endpoint. Live-account execution is not supported by that service.
- Set `ALPACA_DATA_TIER=basic` until the Plus subscription is active; regular-session history then uses IEX and overnight bars use BOATS.
- This release is paper-trading software. Simulator reports and equity_research candidates do not update live strategy settings.
