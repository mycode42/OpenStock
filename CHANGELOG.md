# Changelog

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
- This release is paper-trading software. Simulator reports and autoresearch candidates do not update live strategy settings.
