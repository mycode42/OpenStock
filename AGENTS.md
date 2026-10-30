# AGENTS.md

OpenStock. Node 22, Express, better-sqlite3. Private monitor, journal, and dashboard, still in development. Read this, then do the task. Do not restate it.

## Map

- `server.js` — HTTP app setup, loopback only. Route handlers live in `server/routes/`.
- `www/` — dashboard, journal, config, charts, trading UI. `styles.css` for presentation. `www/modules/` for page behavior. `www/app.js` boots them.
- `lib/brokers/alpaca.js` — Alpaca client: quotes (SIP, falling back to IEX), bars, account, assets, orders.
- `lib/alpaca-monitor.js`, `scripts/monitor.js` — poll, thresholds, Telegram, alert outbox. `lib/monitor.js` holds the shared Telegram send and one-shot lock. Bounded concurrency.
- `database/db.js` exports the composed database class; behavior lives in `database/domains/`. `database/migrations/` is append-only after application.
- `lib/analytics.js` — performance and risk KPIs.
- `ops/systemd/` — unit files. Do not invent a second deploy path.
- `test/` — node:test. Name matches the module.

## Commands

- `npm install`
- `npm test` — full suite.
- `npm run check` — syntax plus PWA assets.
- `npm start` / `npm run monitor` — only when the task is runtime. Do not boot them to look busy.
- `npm run demo` — demo mode on simulated data, separate DB under `data/demo/`.

## Working agreement

The repo is in development. Features, fixes, and refactors are all normal. Scope still follows the task, not the whole tree.

- Touch the owning files and the matching `test/*.test.js`. Do not open sibling areas once the call site is known.
- A UI task stays in `www/`. A quote or alert task stays in `lib/` and `scripts/`. A schema task adds a migration. Do not cross those lines unless the task does.
- No plan recital, no file-tree dump, no second search for naming nits. One line if a choice is reversible.
- Search once. Reuse what you already read.
- No drive-by rename, format, dependency bump, or comment refresh outside the task.
- Verify with the narrowest check that can fail the change: matching test file, then `npm run check`. Full `npm test` when the change crosses monitor, quotes, orders, schema, or alert state.
- Before every `git push` to GitHub, run the full CI verification: `npm run check`, `npm test`, and `npm run audit:prod`. Fix failures before pushing. Commits may be made without running tests; these checks must pass before a push.
- Reply with what changed and how to verify. Skip the essay.

Review and optimization run only when asked. Stay inside the modules named in the task.

## Invariants

- EUR money uses JavaScript `Number` and SQLite `REAL`; preserve available precision and format only for display.
- Quote source is Alpaca: SIP first, IEX when the account cannot access SIP. Monitor BUY/SELL semantics use executable ask/bid.
- An alert fires once per episode. Outbox plus retired level. A poll must not spam Telegram.
- `/api/live-prices` is display-only. It must not write stock state, history, alert state, or equity snapshots.
- Orders are placed only from `lib/alpaca-orders.js` and the Algo order builders. Paper manual orders are confirmed in the UI. Any non-paper endpoint is live trading: it needs `ALPACA_ALLOW_LIVE=1` (the Python endpoint module refuses to load otherwise), and live manual orders and turning real orders on also need the order token. Every order is capped at $100,000 of notional value on paper and live. The trading URLs default to paper.
- One live order per alert level, enforced by a partial unique index in migration 025. Never weaken it.
- Provider failure is data. One symbol does not kill the cycle.
- There is no cap on monitored names.

- The trading gate set is frozen. Only approved changes move it: see `docs/policy-frozen.md`. `algo-trader/tests/policy/test_gate_freeze.py` fails on any unapproved gate.
- Orders are limit orders only. A market order is refused before any broker call.

## Never

- Print or commit tokens, session files, `.env`, or `data/auth.json`.
- Commit `data/*.log`, pid files, or the live SQLite database.
- Hand-edit applied migrations or `package-lock.json` unless a dependency was requested.
- Restart systemd from an edit task.
