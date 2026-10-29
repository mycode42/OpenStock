# Changelog

## Unreleased

### Changed

- Algo extended-hours trading is re-enabled for plain limit DAY or GTC orders during premarket, after-hours, and overnight sessions. Extended-hours entries and exits never carry a broker bracket or OCO; software stops protect them, and broker protection is attached at the next regular open.
- Overnight Algo entries are enabled by default (`overnight_entries_enabled`).
- Extended-session quotes are fresh for 300 seconds instead of 90, so overnight and premarket entries and protective updates are not paused by indicative BOATS quotes. Regular-session quotes keep the 90-second window.

## [3.1.1] - 2026-10-27

Patch release: CI fixes and test cleanup. No runtime behavior changes.

### Fixed

- `poetry.lock` refreshed after the `requires-python` cap, so the CI
  lock check passes again. Only environment markers changed.
- Trade-guard tests no longer depend on Alpaca credentials or the live
  market calendar.

### Changed

- Removed a duplicate parametrization of the rule-sell deferral test (the
  `oco` case covered the same open-sell check as `bracket`).

## [3.1.0] - 2026-10-27

Release: software-managed protection replaces broker brackets, the
equity and options LLM sleeve is renamed `llm_driven`, and research
integrity gates are tightened.

### Added

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

### Changed

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

### Fixed

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

### Upgrade notes

- Stop `algo-trader` and `options-paper`, run the strategy rename migration
  with `--apply`, then the protection migration (dry run first), then start
  both services.
- Run `openstock` after updating its configuration keys to the `llm_driven`
  names.

## [3.0.0] - 2026-10-20

Major release: independent options paper trading and validation join the
coordinated Algo equity portfolio.

### Added

- A dedicated Alpaca Paper options route with VRP, llm-driven, and optional
  directional strategy sleeves, separate order/fill journals, and combined
  equity/options risk checks.
- Options-specific rolling walk-forward research with realistic execution
  costs, chain-quality filters, Greek exposure tracking, assignment checks,
  stress tests, and an untouched holdout.
- Parallel nightly options research alongside equity AutoResearch, with its
  own report section and artifacts.
- Broker fill-based FIFO realized P&L reporting for long and short positions.

### Changed

- Refreshed the project guide, command examples, configuration map, and release
  metadata for the current coordinator, research, reporting, and paper-trading
  workflows.
- Consolidated the runtime into domain packages and kept the Algo strategy
  coordinator as the only route for Algo equity orders.

## [2.0.0] - 2026-10-09

V2.0.0 makes the Algo portfolio a coordinated, multi-horizon long/short system and adds a strict research promotion gate. Swing, Day, and Algo continue to use separate fixed universes and paper-account routes.

### Added

- A portfolio coordinator before the risk manager with per-name, sector, gross/net exposure, shared entry-budget, session, and regime-throttle controls. It can reject or reduce proposals; the risk manager remains the final approval layer.
- Daily swing trend and momentum strategies plus a separate cyclical mean-reversion strategy. The five-minute trend and momentum variants remain disabled; open-dip and opening-range breakout remain the intraday strategies.
- Directional long/short support for the core strategies and the lower-budget multi-factor and scheduled-event satellites.
- Daily-only multi-factor ranking and scheduled-event reaction/follow-through exits.
- Portfolio replay diagnostics for after-cost strategy returns, buy-and-hold excess returns, pairwise strategy-P&L correlation, and same-name directional overlap.
- A chronological walk-forward promotion gate with at least three folds, held-out evaluation, mandatory trading costs, minimum trade counts, parameter-neighbor stability, and fixed test windows.

### Changed

- Algo strategy lifecycle and coordinator state are persisted in SQLite; execution continues through the shared risk manager and Alpaca order path.
- The coordinator schedules intraday positions to flatten by default and reserves only a slice of the daily entry budget for intraday strategies.
- Removed the legacy web flow-editor application from the package; the maintained trading, research, and operations interfaces are the command-line tools and OpenClaw jobs documented here.
- Stopped tracking generated AutoResearch experiment logs; `.gitignore` keeps deployment-specific runs local.
- Package metadata and release documentation now identify V2.0.0.

### Validation

- `poetry check --lock`
- `poetry build` (source distribution and wheel)
- `poetry run python -m pytest -q` (250 passed)
- GitHub Actions runs the same validation gate for the release commit and tag.
- Paper-trading software only. Research candidates do not change live parameters, and this release does not certify strategy profitability.

## [1.6.0] - 2026-10-08

V1.6 adds research-only cross-sectional and scheduled-event strategies to the Algo portfolio, consolidates live and replay rule evaluation, and fixes Algo on a curated universe while keeping Swing and Day universes separate.

### Added

- Cross-sectional multi-factor signals with point-in-time feature calculation, ranking diagnostics, and purged factor-weight research.
- Scheduled earnings and macro-event signals with a point-in-time calendar, freshness checks, and conservative reaction windows.
- Daily Algo AutoResearch artifacts and candidate reports; research jobs do not modify live parameters or submit orders.
- Strategy-specific entry cooldowns and tactical position-count limits.
- Fixed 22-symbol Algo universe and documented, separate Swing and Day universes.

### Changed

- Live Algo scans and portfolio replay share the same pure rule-signal pipeline.
- Algo walk-forward replay aggregates daily bars once and uses indexed prior closes to reduce repeated work.
- Event-calendar cron now reports failures and skipped runs through the configured Telegram alert.
- Swing, Day, and Algo bucket caps are documented as independent exposure ceilings, not target allocations.

### Validation

- GitHub Actions test command: `poetry run python -m pytest -q` (215 passed).
- Paper trading only; AutoResearch candidates require explicit review before live configuration changes.

## [1.5.0] - 2026-10-08

V1.5 consolidates the separate paper-trading routes and adds a continuously running Algo strategy portfolio with persisted coordinator state and research tooling.

### Added

- Dedicated Swing, Day, and Algo Alpaca account routing with broker-side account ID verification.
- Persisted Algo coordinator and per-strategy lifecycle state, startup reconciliation, health reporting, and execution safeguards.
- Feed-aware market data for Basic and Algo Trader Plus, including BOATS routing for eligible overnight scans.
- Local Algo portfolio replay, bounded signal-parameter profiles, chronological walk-forward evaluation, and slippage sensitivity reports.
- Focused GitHub Actions test workflow.

### Changed

- OpenClaw prompts and operating notes document separate modes, explicit feed handling, and service health checks.
- Algo research replay accepts regular-session IEX or SIP only; BOATS requires a separate overnight replay and fill model.
- Package metadata uses the V1.5 version and PEP 621 project fields.

### Configuration notes

- Copy `.env.example` and configure separate paper-account credentials and account IDs for each enabled trading route.
- The Algo service uses the fixed Alpaca paper endpoint. Live-account execution is not supported by that service.
- Set `ALPACA_DATA_TIER=basic` until the Plus subscription is active; regular-session history then uses IEX and overnight bars use BOATS.
- This release is paper-trading software. Simulator reports and equity_research candidates do not update live strategy settings.
