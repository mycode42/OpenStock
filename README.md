# Swarm Trader V1.5

Multi-agent AI trading system with separate Swing, Day, and Algo paper accounts. Analyst agents analyze stocks in Swing and Day. One continuous Algo coordinator selects strategy modules with a deterministic SPY regime check and manages their persisted lifecycles. All three routes use the shared **code-enforced risk management layer**.

**Multi-provider LLM support** — 13 providers: OpenAI, Anthropic, Google, DeepSeek, Groq, Ollama, xAI, OpenRouter, Azure OpenAI, GigaChat, Alibaba, Meta, Mistral.

**Zero paid data APIs required.** Hybrid data layer uses SEC EDGAR + yfinance. Works fully free out of the box.

> Built on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), extended with separate trading routes, code-enforced risk management, free data sources, Alpaca execution, custom agents, and strategy research.

---

## What's Different

| Feature | Upstream | Swarm Trader |
|---|---|---|
| LLM providers | Single provider | **13 providers (Ollama, OpenAI, Anthropic, Google, etc.)** |
| Financial data | financialdatasets.ai ($200/mo) | **Hybrid: SEC EDGAR + yfinance (free)** |
| Trade execution | Simulated only | **Alpaca paper trading — market, limit, bracket, OCO, trailing stop** |
| Trading routes | Single mode | **Swing + Day agent modes and a dedicated Algo strategy portfolio** |
| Risk management | Basic | **Code-enforced risk manager — hard stops, circuit breakers, position limits** |
| Mode steering | N/A | **`trading_mode.json` steers Swing/Day; Algo has its own coordinator** |
| Performance tracking | None | **Daily snapshots, SPY/QQQ comparison, Sharpe ratio** |
| Short positions | Not supported | **Existing short positions can be covered; new short entries are disabled** |
| Analyst agents | 12 built-in | **20 agents (12 + apex + market regime + 6 data/quant)** |
| Custom agents | Not supported | **Create your own analyst agents** |
| Automation | Manual runs | **Scheduled OpenClaw agent pipeline** |
| Strategy research | None | **Separate Day/Swing AutoResearch and research-only Algo walk-forward plus factor-weight candidates** |

---

## Three Trading Routes

Swing and Day are agent-driven routes. Algo runs as a separate continuous service. Account-aware scripts accept `--mode swing|day|algo` where supported and use the matching credentials. Swing and Day positions are not tagged by strategy inside Alpaca. Keep their accounts separate: Day's daily flatten closes every position and order in its selected account.

### Mode Selection

The Swing/Day route can be set by a CLI flag or the steering file. The Algo coordinator does not use this resolver:

```
Priority order:
  1. --mode CLI flag              (cron or manual override)
  2. override in trading_mode.json (human steering with optional expiry)
  3. mode field in trading_mode.json (auto / swing / day)
  4. TRADING_MODE env var          (fallback)
  5. Default: swing
```

**`trading_mode.json`** — the steering file:

```json
{
  "mode": "auto",
  "override": null,
  "override_until": null,
  "auto_rules": {
    "prefer_day_when": ["VIX > 25", "Major earnings/FOMC day", "Morning gap >1%"],
    "prefer_swing_when": ["VIX < 20", "Strong trend in core positions", "Default"]
  }
}
```

The `auto_rules` text in this file is guidance, not an implemented VIX/gap decision engine. In `run_hedge_fund.py` and `run_analysis.py`, unresolved `auto` currently falls back to Swing. OpenClaw jobs pass explicit Swing or Day modes. A human can override the steering file:

```python
# Human override: day mode for 4 hours
from src.config import set_mode
set_mode("day", reason="FOMC day", updated_by="human", override=True, override_hours=4)

# Or just edit trading_mode.json directly
```

### Multi-Account Routing

Swing, Day, and Algo operate on their configured Alpaca accounts. Algo has one coordinator and persisted lifecycle records for each strategy and ticker. Its coordinator submits orders through `execute_trades.py --mode algo` and the code-enforced 11-rule `risk_manager.py`; direct Algo execution without the active coordinator lease is rejected.

| Mode | Account | Purpose |
|---|---|---|
| `swing` | Primary account | Multi-day holds, conservative risk |
| `day` | Day trading account | Intraday only, flattens EOD |
| `algo` | Dedicated Algo account | Continuous six-strategy portfolio: four core rules with Multi Factor and Event Driven as confirmation satellites; pairs remain analysis-only |

Credentials are configured in `.env`:

```bash
# Primary / Swing account (required)
ALPACA_API_KEY=your_swing_key
ALPACA_API_SECRET=your_swing_secret
ALPACA_ACCOUNT_ID=your_swing_paper_account_id

# Day trading account (required for Day mode; must be a separate account)
ALPACA_DAY_API_KEY=your_day_key
ALPACA_DAY_API_SECRET=your_day_secret
ALPACA_DAY_ACCOUNT_ID=your_day_paper_account_id

# Dedicated Algo paper account (required for the Algo service)
ALPACA_ALGO_API_KEY=your_algo_paper_key
ALPACA_ALGO_API_SECRET=your_algo_paper_secret
ALPACA_ALGO_ACCOUNT_ID=your_algo_paper_account_id
```

Account routing is handled by `src/accounts.py`. Account-aware scripts use the credentials selected by `--mode`; missing credentials or a broker account ID mismatch stop live operations instead of falling back to another account. Configure separate Swing, Day, and Algo Alpaca accounts before enabling their jobs or service. Day's daily flatten closes every position in its selected account.

```python
from src.accounts import get_account_for_mode

acct = get_account_for_mode("swing")  # returns AlpacaAccount with correct keys
acct.headers  # ready-to-use API headers
```

### Autonomous Algo Portfolio

Mean reversion, trend following, momentum, and opening-range breakout are the four core entry rules. The deterministic SPY regime filter chooses which core rules may propose entries. Multi Factor and Event Driven run as satellites: they can confirm an aligned core entry, raise its priority, and modestly increase its risk budget, but cannot open a position or become its owner. Every resulting proposal still passes the regular Algo risk validation and position caps. Pairs remain analysis-only because the shared executor does not submit atomic two-leg orders. `data/algo_state.sqlite3` is the authoritative store for the coordinator, per-strategy ticker states, ownership, entry provenance, and its execution lease. `data/algo_state.json` is an atomic, derived compatibility snapshot for existing status consumers; do not edit it to change ownership.

All three modes use the Algo Trader Plus market-data plan through the shared `ALPACA_DATA_TIER=algo_trader_plus` setting. Regular and extended sessions use SIP. Algo's overnight intraday signal bars and quotes use real-time `feed=boats`, with entries enabled only for assets Alpaca marks `overnight_tradable`; daily regime and factor history remains on SIP. The service follows Alpaca's 24/5 sessions using New York time and the broker calendar. Overnight orders remain simple limit DAY orders with `extended_hours=true`; regular-session orders continue through the shared executor and risk manager. Algo's 40-entry quota follows its 20:00–20:00 ET trading session and it allows 20 open positions, subject to configured position, sector, cash, and drawdown limits. Execution remains paper-only. See [Alpaca's 24/5 data-feed guide](https://docs.alpaca.markets/us/docs/245-trading-for-trading-api) for session and feed details.

Extended-hours entries have software-managed stops and targets. The coordinator attaches a broker-verified full-size OCO during the next regular session once no symbol order remains open. For positions with broker bracket or OCO protection, a competing software exit signal is deferred while the protective order remains open: Alpaca can leave a cancellation in `pending_cancel`, which may neither release the shares nor preserve the stop. The service reports the suppressed exit and reevaluates it after broker reconciliation. A stale or failed historical bar request pauses new entries but does not disable quote-driven protective checks on owned positions.

#### Algo portfolio replay

`src.algo.backtest` replays the deterministic Algo regime selector, strategy rules, shared sizing, and hard risk validator over stored regular-session 5-minute bars. It fills signals no earlier than the next bar open, models adverse slippage, and reports its assumptions with every run. It is research-only and cannot submit orders.

`src.algo.auto_research` is a separate weekday research job for Algo. It uses the fixed 22-symbol Algo universe, refreshes Plus SIP split-adjusted bars, fits non-negative Ridge factor weights from purged 21-session forward labels, and runs chronological walk-forward profiles for the four core rules plus Multi Factor and Event Driven confirmation overlays. The profile set compares a core-only baseline, bounded core-rule variants, each satellite separately, and both satellites together. A training profile with an enabled satellite must produce at least three confirmed entries per training fold to pass the sample gate. The job also compares a four-rule portfolio against the six-strategy portfolio on 2024–2025 and the available 2026 forward period. The job never edits live settings, reloads the service, or submits orders. Its Telegram report is a review request: a candidate requires your explicit approval before any live-system change.

Historical scheduled-event inputs are rebuilt into `data/research/algo-auto/event-calendar-historical.csv`: issuer earnings dates use SEC EDGAR 8-K Item 2.02 filing acceptance times (ETFs are excluded), while CPI/NFP and FOMC dates use official BLS and Federal Reserve archives. Macro reactions become visible no earlier than their scheduled release, and all reaction windows align to stored regular-session bars. This is a conservative historical reaction ledger, not an intraday news feed; event profiles are skipped and reported if a valid full-range calendar cannot be built or reused. Historical fixed-universe membership remains a survivorship limitation.

Rebuild the event history against the locally stored SIP bars with:

```bash
poetry run python -m src.algo.historical_events --start 2016-01-01
```

```bash
# Small local smoke run; scheduled execution also refreshes recent Plus SIP bars.
poetry run python -m src.algo.auto_research --no-refresh --factor-only
```

The local SQLite market store defaults to `data/algo_market.sqlite3`. Feed, adjustment, symbol, interval, and timestamp are separate keys. Historical IEX rows remain segregated from the SIP dataset; Plus research and live defaults use SIP, and fetching the same feed again updates overlapping bars. When multi-factor is included, fetch, replay, slippage, and walk-forward commands default to split-adjusted bars; an explicit `--adjustment` overrides this, and fetch/run must use the same adjustment key. The fetch command stores 180 days of SPY daily bars for regime warmup and requests 260 calendar days of symbol-bar warmup for the default factor horizons. `--profile` increases that span when the selected factor profile needs more history:

```bash
poetry run python -m src.algo.backtest fetch --symbols AAPL,MSFT,NVDA --start 2026-07-01 --end 2026-10-01
poetry run python -m src.algo.backtest coverage
poetry run python -m src.algo.backtest run --symbols AAPL,MSFT,NVDA --start 2026-07-01 --end 2026-10-01 --output reports/algo-replay.json
poetry run python -m src.algo.slippage_sensitivity --symbols AAPL,MSFT,NVDA --start 2026-07-01 --end 2026-10-01 --slippage-grid 0,2,5,10,20
poetry run python -m src.algo.walk_forward --symbols AAPL,MSFT,NVDA --start 2026-07-01 --end 2026-10-01 --profiles autoresearch/algo-profiles.example.json --train-days 30 --test-days 10
```

Replay currently covers regular hours only. It uses a fixed supplied symbol list, does not reconstruct historical universe membership, omits LLM advice, assumes the stop executes first when both bracket legs are touched within one bar, and does not model commissions or regulatory fees. Historical bars do not contain the live quote stream, so slippage is configurable rather than inferred from quotes. These reports evaluate the model and its stability; they are not performance forecasts.

#### Multi Factor confirmation satellite

The Algo-only `src.strategies.multi_factor` package ranks the fixed Algo universe as a cross-section. Its default weekly score equally weights 1-, 3-, and 6-month price momentum with low volatility; it winsorizes each factor and converts it to a cross-sectional z-score before combining. It selects up to five long targets for research diagnostics. In the live six-strategy portfolio, a current-period long target confirms a matching core-rule entry; it cannot open or own a position. The current risk manager is long-only, so the strategy does not create shorts. Factor-only backtest runs report ranking and IC diagnostics without creating standalone positions.

Features use completed daily bars through the prior session. Live settings are centralized in `ALGO_DEFAULT_PARAMETERS["multi_factor"]`; research profiles can override the validated fields without changing live settings. The simulator aggregates stored regular-session 5-minute bars and fetches 260 calendar days of warmup by default, extending it for longer configured horizons. The fixed universe supplied to a backtest must contain enough names with the required history to meet `min_universe` (20 by default). Value is disabled by default; if enabled, it requires earnings-yield observations with an `available_at` timestamp, and observations published after the rebalance cutoff are ignored. No price-only value proxy is substituted.

For example, fetch data with warmup and inspect factor ranking/IC diagnostics:

```bash
poetry run python -m src.algo.backtest fetch --symbols AAPL,MSFT,NVDA,AMZN,GOOGL,META,AVGO,TSM,AMD,PLTR,CRM,ORCL,JPM,V,UNH,JNJ,PG,KO,SPY,QQQ --start 2021-01-01 --end 2025-12-31
poetry run python -m src.algo.backtest run --symbols AAPL,MSFT,NVDA,AMZN,GOOGL,META,AVGO,TSM,AMD,PLTR,CRM,ORCL,JPM,V,UNH,JNJ,PG,KO,SPY,QQQ --start 2021-01-01 --end 2025-12-31 --strategies multi_factor --output reports/algo-multi-factor.json
poetry run python -m src.algo.walk_forward --symbols AAPL,MSFT,NVDA,AMZN,GOOGL,META,AVGO,TSM,AMD,PLTR,CRM,ORCL,JPM,V,UNH,JNJ,PG,KO,SPY,QQQ --start 2021-01-01 --end 2025-12-31 --profiles autoresearch/multi-factor-profiles.example.json --train-days 252 --test-days 63 --step-days 63
```

Each target is a typed `MultiFactorSignal` with `score`, `rank`, `direction`, and `target_weight`. The live coordinator attaches aligned factor evidence to a core rule proposal, then uses the existing sizing and risk path:

```python
confirmed_signal = apply_satellite_confirmations(
    core_rule_signal,
    factor_signal=factor_signal.to_dict(),
    event_signal=event_signal.to_dict() if event_signal else None,
)
proposal = _make_trade(
    confirmed_signal, reference_price, account_equity, {},
    get_mode_config("algo")["risk"],
)
if proposal is not None:
    decision = validate_trade(
        ticker=proposal["ticker"], action=proposal["action"], qty=proposal["qty"],
        entry_price=proposal["entry_price"], portfolio_state=current_risk_state,
        mode="algo",
    )
    if decision.approved:
        execute_trades([proposal], mode="algo")
```

`_make_trade` applies the existing Algo risk sizing and the signal's target weight as an additional cap; the executor calls `risk_manager.validate_trade()` again before submission. The backtester applies the same validator to buys and sells and respects the deterministic SPY regime's permission to open factor positions. Backtest diagnostics include target turnover and weekly 21-session Spearman IC; those labels overlap and are descriptive. AutoResearch separately uses monthly-spaced 21-session labels with a purge for its Ridge fit and expanding prequential IC. The fixed 22-symbol universe is replayed across all historical years because point-in-time membership is not available; this creates survivorship and listing-availability bias, which limits historical conclusions.

The slippage sensitivity runner holds the strategy profile, symbols, dates, feed, and risk limits constant while varying adverse basis-point assumptions. The walk-forward runner chooses among a small supplied candidate list on each preceding training window and evaluates the selected profile on the next non-overlapping window. Candidate profiles can change signal rules only; portfolio risk settings and live settings are never modified. Each holdout fold starts with a flat portfolio and fresh capital, so fold results measure stability rather than one continuous deployable equity curve. The fixed 22-symbol replay uses the same current membership across the full period; point-in-time membership is unavailable, so survivorship and listing-availability bias remain. BOATS cannot be passed to these regular-session replay commands: overnight bars need a separate session calendar, entry cutoff, and overnight order/fill model first.

Research controls can apply a ticker cooldown per strategy with `cooldown_minutes_by_strategy`; the legacy `cooldown_minutes` control remains global. Reports separate repeated five-minute signal observations from candidate counts deduplicated to one ticker/strategy/trading-day.

An optional parameter profile can change strategy signal inputs while risk settings remain fixed. The checked-in example is `autoresearch/algo-profile.example.json`; profile values are range-checked and each report includes a hash for reproducibility. The existing research loop should use this replay and chronological walk-forward splits before any candidate is considered for paper shadowing. It must not write tuned values into live settings automatically.

The Algo quote adapter uses Alpaca for both data and execution, reads `ALPACA_ALGO_*` credentials, and verifies the returned paper account ID before starting. The account route is paper-only in this service. The OpenAI regime advisor uses `LLM_PROVIDER`, `LLM_MODEL`, and `OPENAI_API_KEY`; it can only reorder strategies already allowed by deterministic checks and has no broker access or ticker-signal authority.

#### Algo paper account setup

Put the Algo paper API credentials and exact account ID in `.env`:

```bash
ALPACA_ALGO_API_KEY=your_algo_paper_key
ALPACA_ALGO_API_SECRET=your_algo_paper_secret
ALPACA_ALGO_ACCOUNT_ID=your_algo_paper_account_id
```

The daemon requires all three values and fails closed if the API reports a different account. `ALPACA_ALGO_ACCOUNT_ID` may match the API response's UUID `id` or its `account_number`. A mismatch stops the service cleanly; correct the identifier or use the keys belonging to the intended paper account, then restart. Reserve this account for the Algo portfolio: existing positions in subscribed symbols can be managed by their owning strategy. Keep `.env` private.

After creating a new Algo paper account, stop the service, configure that account's new keys and ID, and verify it is active with zero positions and order history. Archive both `data/algo_state.sqlite3` and `data/algo_state.json` before restarting; the JSON file would otherwise migrate old ownership into a new database. Keep the old journal and snapshots as audit history. Capture the new account's opening weekly loss baseline once while it is still unused:

```bash
poetry run python -m src.account_baseline --mode algo
```

The command checks the broker account, age, equity, cash, positions, and order history and will not move an existing baseline. The coordinator binds its state to the broker account ID on first run and refuses a later mismatch. Current-account performance reports exclude snapshots and journal entries from previous paper accounts.

#### Service installation and operation

The service unit is [deploy/swarm-trader-algo.service](deploy/swarm-trader-algo.service). It runs as `openclaw`, loads the project `.env`, starts after networking, restarts after failures, and writes logs to journald. It does not alter or replace Swing/Day cron jobs.

```bash
sudo install -m 0644 deploy/swarm-trader-algo.service /etc/systemd/system/swarm-trader-algo.service
sudo systemctl daemon-reload
# After validating Algo paper credentials:
sudo systemctl enable --now swarm-trader-algo.service
sudo systemctl status swarm-trader-algo.service
sudo systemctl stop swarm-trader-algo.service
sudo systemctl start swarm-trader-algo.service
sudo systemctl restart swarm-trader-algo.service
sudo journalctl -u swarm-trader-algo.service -f
```

Inspect the persisted state with `poetry run python -m src.algo.daemon --status`. The daemon uses an exclusive process lock and a SQLite coordinator lease, commits state transactionally, then writes the JSON compatibility snapshot atomically. On startup it reconciles broker positions, nested orders, exact Algo journal entries, and persisted entry IDs before assigning an owner. Ambiguous ownership, missing verified stops, and inconsistent state pause new entries for review. Order fills, including partial fills and terminal rejections/cancellations, drive lifecycle transitions; submission is never treated as an immediate fill. Extended-hours positions are software-managed by the continuously running service because Alpaca does not support bracket orders in those sessions.

### Recommended: Separate Trading Agents

For best results, run a **dedicated agent per account** rather than one agent switching modes. Each agent develops its own context, memory, and trading style:

| Agent | Role | Account | Schedule |
|---|---|---|---|
| Swing agent | Multi-day positions, sector rotation | Primary | 3 weekday jobs: 9:30 AM, 12 PM, 3 PM ET |
| Day agent | Intraday scalps, flat by close | Day trading | 5 weekday reports: 9:35 AM, 10 AM, 12 PM, 2:30 PM, 3:45 PM ET |

This provides account and P&L isolation. Each entry still uses the shared code-enforced risk manager, with its configured limits. The Day opening cleanup, deadline guard, Algo health check, and close snapshots are separate command jobs. See [Automation with OpenClaw](#automation-with-openclaw).

### Algo Strategy Runner

The autonomous Algo portfolio runs continuously on the dedicated Alpaca account through `poetry run python -m src.algo.daemon --mode algo`, normally under systemd. It loads its fixed 22-symbol universe and refreshes Alpaca's overnight-eligibility metadata during the session. Deterministic SPY regime ranking controls new entries; every open position remains owned and managed by its persisted strategy lifecycle, even when that strategy is inactive for new entries.

Each rule family is an independent module with its own entry and exit rules. The coordinator tracks each strategy and ticker through `flat → entry_pending → open → exit_pending → cooldown` (or `quarantined` when attribution is unsafe). Its SQLite state includes stops, targets, pending broker IDs, and owner provenance. A software exit is suppressed while broker protection is active; missing broker protection is repaired through exact order and position checks during regular hours. Algo one-shot execution is removed; direct `execute_trades.py --mode algo` needs the coordinator lease.

Algo membership is configured independently from Swing and Day. `max_sector_pct` is an exposure ceiling for a configured universe bucket, measured against account equity: the risk manager blocks a buy if the bucket's current market value plus the proposed order would cross that ceiling. The code calls these buckets “sectors,” but they are the configured groups below, not standard industry sectors. These are independent limits, not target weights, so they do not need to add to 100%.

| Group | Fixed symbols | Bucket cap |
| --- | --- | --- |
| Mega-liquid Tech / Growth | NVDA, AVGO, TSM, MSFT, AAPL, GOOGL, META, AMZN, AMD | 40% |
| Non-Tech Diversifiers | JPM, V, UNH, JNJ, PG, KO | 30% |
| Broad Market / Hedge ETFs | SPY, QQQ, GLD, TLT | 20% |
| Tactical Satellites | PLTR, CRM, PANW | 4% |

Algo's separate `max_tactical_pct` cap is also 4%, with at most 2 tactical positions and 2% per position. Cash reserve, position count, and per-stock limits can constrain total exposure further. Alpaca's active asset metadata is used only to determine BOATS overnight eligibility; it does not add or remove universe members. Existing strategy-owned positions remain subscribed for exits even when they are outside the configured entry universe.

Swing and Day scans use the fixed, mode-specific lists shown below. They remain available for diagnostics and explicit execution and use the same deterministic strategy modules, `execute_trades.py`, and `risk_manager.py`; `--execute` submits orders from the configured list.

```bash
# One-shot Swing/Day diagnostics
poetry run python -m src.strategies.runner --mode swing
poetry run python -m src.strategies.runner --mode day

# Preview one-shot trades through the existing risk validation path
poetry run python -m src.strategies.runner --mode swing --execute --dry-run

# The continuous Algo-account portfolio mode is launched by systemd:
poetry run python -m src.algo.daemon --mode algo

# Submit eligible one-shot Day orders through the shared executor
poetry run python -m src.strategies.runner --mode day --execute
```

The deterministic SPY filter activates trend following, momentum, mean reversion, and opening-range breakout according to regime. Strategy modules return one common signal shape and send compatible orders through the shared executor with risk-based sizing and stop/target prices. Strategy ownership and protection checks remain active regardless of which strategies the current regime selects for new entries.

The optional regime advisor uses the OpenAI model selected by `LLM_PROVIDER` and `LLM_MODEL` with `OPENAI_API_KEY`. Its output only reorders strategies already activated by deterministic checks; failures keep the deterministic ordering, and it never receives broker access or emits ticker-level signals. Set `STRATEGY_REGIME_LLM_ENABLED=false` to disable it.

Pairs analysis uses 60 daily bars, correlation, and an OLS spread z-score. It reports candidate spreads only: the existing executor cannot submit two legs atomically, so the portfolio never opens an unhedged pair. Trade journal rows include strategy attribution, and `performance_tracker_v2.py --json --mode algo` reports activity for the Algo account.

### Swing Mode

Position trading over days to weeks. Diversified universe with conservative risk limits.

| Parameter | Value |
|---|---|
| Universe | Fixed list of 28 configured tickers; no dynamic ranking |
| Max position | 8% of equity |
| Mode-wide max-sector fallback | 30% |
| Stop loss | -7% hard stop |
| Trailing stop | 15% from peak |
| Max trades/day | 4 |
| Max positions | 12 |
| Cash reserve | Min 20% |
| Leveraged ETFs | ❌ Banned |
| Overnight holds | ✅ Yes |
| Circuit breaker | -2% daily, -5% weekly |

**Swing universe:**

| Configured group | Tickers | Bucket cap |
|---|---|---|
| Core Technology | NVDA, AVGO, TSM, MSFT, AAPL, GOOGL, META, AMZN | 30% |
| Growth | PLTR, AMD, CRM, SNOW, NET, PANW | 25% |
| Value & Dividend | JPM, V, UNH, JNJ, PG, KO | 25% |
| Tactical | COIN, MSTR, RKLB, SMCI | 3% |
| Hedge | SPY, QQQ, GLD, TLT | 15% |

Swing's Tactical bucket cap matches its 3% `max_tactical_pct` limit.

### Day Trading Mode

Intraday trading with tighter stops, larger positions, and mandatory daily flatten. Flatten is needed because this route promises intraday exposure and an account-wide clean start each trading day.

| Parameter | Value |
|---|---|
| Universe | Fixed list of 14 configured tickers; no dynamic ranking |
| Max position | 15% of equity |
| Mode-wide max-sector fallback | 50% |
| Stop loss | -1.2% tight stop |
| Trailing stop | 3% from intraday high |
| Max trades/day | 20 |
| Max positions | 8 |
| Cash reserve | Min 10% |
| Leveraged ETFs | ✅ Allowed (intraday only) |
| Overnight holds | ❌ Flattens by 3:45 PM ET on a regular session; earlier on early closes |
| Circuit breaker | -3% daily, -8% weekly |

**Day universe:**

| Configured group | Tickers | Bucket cap |
|---|---|---|
| Mega-Cap Liquid | NVDA, AVGO, TSM, AMD, MSFT, AAPL, META, GOOGL, AMZN | 50% |
| Momentum | PLTR, TQQQ, COIN | 35% |
| ETF Direction | SPY, QQQ | 30% |

**Day trading features:**

- **5-min intraday bars** from Alpaca market data API
- **VWAP** — volume-weighted average price
- **RSI(14)** on 5-min timeframe
- **Volume ratio** — today's volume vs 20-day average
- **Fixed per-mode universes** — live Swing, Day, and Algo scans use their separate configured lists.
- **Market regime detection** — trending, range-bound, or volatile
- **Verified opening cleanup** — repeated `execute_trades.py --mode day --flatten` runs before the first Day agent report. A successful check verifies zero broker positions and open orders and writes an account-bound `data/day_recovery.json` receipt for that New York date. New Day buys require today's receipt.
- **Calendar-aware flatten** — `execute_trades.py --mode day --flatten-if-due` uses Alpaca's exchange calendar. The deadline is the earlier of 3:45 PM ET or 15 minutes before an early close; new entries stop 15 minutes before that deadline. The command cancels orders, closes residual positions, and succeeds only after verifying zero positions and open orders.
- **Close snapshot** — after the broker's actual close, `performance_tracker_v2.py --mode day --snapshot --force --snapshot-kind close` records a marked baseline for later drawdown checks.

### Algo Portfolio Risk Mode

Algo has its own limits; Swing and Day retain the values above and below. The shared risk manager enforces these limits for every strategy order.

| Parameter | Algo limit |
|---|---|
| Max open positions | 20 |
| New buy entries/day | 40; counts buys that open a flat ticker, not scale-ins or exits |
| Max position | 5% of equity |
| Sector/theme allocation | 32% per configured bucket; tactical holdings have a separate 15% aggregate cap |
| Daily circuit breaker | -2% |
| Weekly circuit breaker | -5% |
| No-buy threshold | -2% daily P&L |
| Minimum cash reserve | 15% |
| Leveraged ETFs | Banned |

Position count and entry throughput rise together, while individual size stays at 5%, the drawdown breakers remain at -2%/-5%, and tactical exposure stays capped. Algo uses the existing strategy-provided stops and the configured 1% hard-stop / 3% trailing defaults.

---

## Architecture

```
Swing/Day agents + explicit mode       Algo daemon + deterministic SPY regime
        │                                    │  optional local ranking advice
        │                                    ├── per-strategy SQLite lifecycle
        │                                    └── broker/journal reconciliation
        └──────────────────┬─────────────────┘
                           ↓
             Account verification + risk_manager.py
                           ↓
            execute_trades.py → separate Alpaca paper accounts
                           ↓
     Broker order/fill reconciliation + close snapshots + reports
```

---

## Risk Management (V2)

The risk manager (`risk_manager.py`) enforces **11 hard rules** that no agent can bypass. Every trade proposal passes through `validate_trade()` before execution. Violations are rejected with a clear reason.

### Rules (Code-Enforced)

| # | Rule | Swing | Day | Algo |
|---|---|---|---|---|
| 1 | No leveraged ETFs | ❌ Hard block | ✅ Allowed intraday | ❌ Hard block |
| 2 | No moonshots (IONQ, RGTI, etc.) | ❌ Hard block | ❌ Hard block | ❌ Hard block |
| 3 | Daily loss circuit breaker | -2% | -3% | -2% |
| 4 | No new buys if down for day | -3% | -2% | -2% |
| 5 | Weekly loss circuit breaker | -5% | -8% | -5% |
| 6 | Daily entries | 4 trades | 20 trades | 40 new-position buys |
| 7 | Max open positions | 12 | 8 | 20 |
| 8 | Min cash reserve | 20% | 10% | 15% |
| 9 | Max position size | 8% | 15% | 5% |
| 10 | Max sector allocation | 30% | 50% | 32% per configured bucket |
| 11 | Tactical/high-risk cap | 3% total | No cap | 15% total |

### Stop Losses

The configured defaults are:

- **Swing:** -7% hard stop from entry + 15% trailing stop from peak.
- **Day:** -1.2% hard stop + 3% trailing stop from intraday high.
- **Algo:** strategy stop/target where supplied, with 1% hard-stop and 3% trailing defaults.

Regular-session entries with stop and target levels use broker brackets. Extended-hours Algo orders cannot use those brackets, so the coordinator monitors persisted software levels using fresh quotes. `portfolio_monitor.py` checks Swing and Day positions when invoked by their jobs; do not assume it runs continuously.

### Status Check

```bash
# See current risk status for either mode
poetry run python risk_manager.py --status --mode swing
poetry run python risk_manager.py --status --mode day
```

---

## Performance Tracking

`performance_tracker_v2.py` tracks each account's equity against SPY and QQQ benchmarks. Use an explicit `--mode` so the report reads the intended paper account.

```bash
# Record a close baseline after Alpaca's actual market close
poetry run python performance_tracker_v2.py --mode swing --snapshot --force --snapshot-kind close

# View performance report
poetry run python performance_tracker_v2.py --mode swing --report

# JSON output for automation
poetry run python performance_tracker_v2.py --mode algo --json

# Optional read-only gross realized P&L from broker FILL activities
poetry run python performance_tracker_v2.py --mode algo --json --fill-report
```

Tracks:

- Mode-specific daily equity snapshots: Swing in `snapshots/`, Day in `snapshots/day/`, and Algo in `data/snapshots/algo/`.
- Broker-calendar-verified `snapshot_kind=close` baselines, SPY and QQQ comparison, and rolling 21-day Sharpe ratio.
- Journal submissions separately from broker-confirmed fills; a submitted order is not counted as an execution.
- Optional FIFO gross realized P&L from all available broker FILL activity pages. Attribution requires exact mode-tagged journal order IDs or verified bracket child links. Missing buy basis, mismatched broker inventory, or uncertain attribution is reported rather than guessed; incomplete basis makes the command exit nonzero. Fees, dividends, transfers, splits, and tax-lot accounting are outside this report.

---

## AutoResearch — Strategy Evolution Lab

> Adapted from [karpathy/autoresearch](https://github.com/karpathy/autoresearch) for paper trading research.

The legacy `autoresearch-run` job still runs five LLM-driven experiments against `autoresearch/strategy.py` for Swing/Day after market close. Algo has a separate deterministic AutoResearch pipeline described below; it does not edit Python source or share the legacy experiment log.

| | Karpathy's autoresearch | Swarm Trader AutoResearch |
|---|---|---|
| What evolves | `train.py` (PyTorch training) | `autoresearch/strategy.py` (currently Day-oriented rules) |
| Metric | `val_bpb` (validation loss) | Fitness = Sharpe×0.35 + Sortino×0.25 + Return%×0.20 + WinRate×0.10 + ProfitFactor×0.10 |
| Budget | 5-min GPU training run | Fast backtest on cached 5-min bars (~30s) |
| Agent | Claude Code | Claude Code (pinned to Sonnet) |

### Quick Start

```bash
# Cache historical data (one-time)
poetry run python autoresearch/backtest_fast.py --days 10

# Run baseline backtest
poetry run python autoresearch/backtest_fast.py

# Run a small manual Day research batch
poetry run python autoresearch/evolve.py --mode day --iterations 5

# Review results
poetry run python autoresearch/analyze.py
```

### Files

| File | Role | Modified by |
|------|------|-------------|
| `autoresearch/strategy.py` | Pure-Python strategy (tunable params) | Agent |
| `autoresearch/backtest_fast.py` | Deterministic backtester + fitness scorer | Nobody |
| `autoresearch/evolve.py` | Evolution loop orchestrator | Nobody |
| `autoresearch/analyze.py` | Cross-run analytics | Nobody |
| `autoresearch/program.md` | Agent instructions | Human |
| `autoresearch/experiments/log.jsonl` | Full experiment history | System |

### From Research to Production

`src/agents/autoresearch_agent.py` is a deterministic analyst bridge that imports the evolved strategy into the Swing/Day committee when selected. **`run_hedge_fund.py` selects it by default, even with `--mode swing`**, so Day-oriented evolved signals can influence Swing decisions unless `--analysts` excludes it. `run_analysis.py` excludes it from its default analyst list. The current OpenClaw trading prompts do not explicitly invoke either CLI. The bridge hardcodes Day context, approximates Eastern time as UTC-4, and caches its import for the process lifetime; an already running process does not hot reload a new experiment. It is not part of the Algo scanner. Research results should be reviewed before using this analyst for paper decisions; the current backtest is a short research screen, not a validated promotion gate.

The legacy research loop is not suitable for Algo parameter adaptation: it forces positions flat daily and evaluates a different single-strategy signal. Algo AutoResearch instead uses the Algo portfolio simulator for rule candidates and a purged expanding Ridge evaluation for factor weights. These jobs use Plus SIP regular-session data; BOATS sessions and overnight fills remain outside this research. Candidate profiles are reports only and require your confirmation before a live configuration change.

---

## Data Sources

| Data Type | Source | Mode | Cache TTL |
|---|---|---|---|
| Prices (OHLCV) | yfinance | swing | 15 min |
| 5-min intraday bars | Alpaca market data API | day | none (live) |
| Overnight completed 5-min bars and quotes | Alpaca `feed=boats` | algo | Plus real-time feed; freshness gate: 90 seconds |
| Regular/extended session bars and quotes | Alpaca SIP | swing, day, algo | Scan-dependent |
| VWAP / RSI / volume | Calculated from bars | day | none |
| Financial metrics | yfinance + SEC EDGAR XBRL | swing | 24 hrs |
| Insider trades | SEC EDGAR Form 4 | swing | 7 days |
| Company news | yfinance news feed | both | 15 min |
| Broker fills | Alpaca account activities | reporting | Paginated when requested |

---

## Quick Start

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/)
- [Alpaca](https://app.alpaca.markets) paper trading account (free)
- An LLM provider for Swing/Day analyst runs; the deterministic Algo service does not require one

### Setup

```bash
git clone https://github.com/zhound420/swarm-trader.git
cd swarm-trader
poetry install

cp .env.example .env
```

Edit `.env`:

```bash
# Required — Alpaca paper trading (primary / swing account)
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_ACCOUNT_ID=your_swing_paper_account_id

# Required for Day mode — a separate paper account
ALPACA_DAY_API_KEY=your_day_key_here
ALPACA_DAY_API_SECRET=your_day_secret_here
ALPACA_DAY_ACCOUNT_ID=your_day_paper_account_id

# Required for the continuous Algo service — a third paper account
ALPACA_ALGO_API_KEY=your_algo_key_here
ALPACA_ALGO_API_SECRET=your_algo_secret_here
ALPACA_ALGO_ACCOUNT_ID=your_algo_paper_account_id

# LLM provider (at least one)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
GROQ_API_KEY=
DEEPSEEK_API_KEY=

# Optional Swing/Day fallback if trading_mode.json is absent
TRADING_MODE=swing
```

### Verify

```bash
# Test data layer
python test_data.py --ticker NVDA

# Check risk manager
poetry run python risk_manager.py --status --mode swing
```

### Run (Swing Mode)

```bash
# Analyze holdings
poetry run python run_hedge_fund.py --mode swing

# Execute trades (risk manager validates each one)
poetry run python run_hedge_fund.py --mode swing --execute

# Check portfolio
poetry run python check_portfolio.py --mode swing

# Preview stop monitoring without placing sells
poetry run python portfolio_monitor.py --mode swing --dry-run

# Preview sales for positions outside the swing universe
poetry run python rebalance.py --mode swing
```

### Run (Day Trading Mode)

```bash
# Gather intraday data for holdings and the fixed Day universe
poetry run python gather_data.py --mode day --include-universe --output /tmp/data.json

# Verify and clear residual Day exposure before new Day buys
poetry run python execute_trades.py --mode day --flatten

# Execute trades after a verified opening cleanup
echo '{"trades":[{"ticker":"NVDA","action":"buy","qty":10}]}' | \
  poetry run python execute_trades.py --mode day

# Calendar-aware guard: no-op before the deadline, verified flatten when due
poetry run python execute_trades.py --mode day --flatten-if-due

# Market pulse check
poetry run python check_moves.py --mode day
```

---

## Order Types

![Execution Methods & Safety Rails](docs/execution-methods.png)

| Type | When to use | Key fields |
|---|---|---|
| `market` | Broker-submitted market order; fill is confirmed separately | — |
| `limit` | Enter/exit at a specific price | `limit_price` |
| `bracket` | Entry with automatic stop + target | `stop_price`, `take_profit` |
| `stop` | Standalone stop-loss on existing position | `stop_price` |
| `oco` | Exit-only: stop + take-profit on existing | `stop_price`, `take_profit` |
| `trailing_stop` | Stop that rises with price | `trail_percent` |

Regular-hours buys with supplied stop and target levels use broker brackets. Algo extended-hours orders are simple limit orders with software-managed levels, and protected regular-hours positions defer outside-hours replacement exits. A submission response does not mean the order filled.

---

## Analyst Agents

### LLM Agents (13)

| Agent | Philosophy |
|---|---|
| `warren_buffett` | Value investing, moats, margin of safety |
| `charlie_munger` | Quality at fair prices |
| `michael_burry` | Contrarian deep value, FCF |
| `cathie_wood` | Disruptive innovation |
| `peter_lynch` | Growth at reasonable price |
| `bill_ackman` | Activist, concentrated positions |
| `stanley_druckenmiller` | Macro, asymmetric bets |
| `ben_graham` | Deep value, net-nets |
| `phil_fisher` | Qualitative growth |
| `aswath_damodaran` | DCF valuation |
| `rakesh_jhunjhunwala` | Emerging market growth |
| `mohnish_pabrai` | Dhandho framework |
| `mordecai` | **Mode-aware contrarian** — disciplined, rules-based, pulls universe from active mode |

### Data/Quant Agents (7)

| Agent | What it does |
|---|---|
| `fundamentals_analyst` | ROE, margins, P/E, P/B |
| `technical_analyst` | Trend, momentum, volatility |
| `sentiment_analyst` | Market sentiment |
| `growth_analyst` | Revenue acceleration, R&D |
| `valuation_analyst` | Fair value models |
| `news_sentiment_analyst` | News-driven sentiment signals |
| `market_regime` | Classifies conditions: trending, range-bound, volatile |

### Custom Agents

Create your own — see `src/agents/mordecai.py` as a template. Register in `src/utils/analysts.py`. Full guide in [PLAYBOOK.md](./PLAYBOOK.md).

---

## Automation with OpenClaw

Swing and Day use dedicated OpenClaw agents for scheduled analysis and reports. The Algo daemon trades independently; its OpenClaw agent reports service health, persisted strategy ownership, scan freshness, regime, account risk, and broker-confirmed order events. The report agent does not place trades. All trader jobs use `America/New_York` so market-time schedules follow daylight saving changes.

Deterministic Gateway command jobs provide the operational guards:

- `day-open-recovery` repeats the verified Day account cleanup from 9:30 through 9:34 AM ET. The first Day agent report starts at 9:35 AM, and new Day buys require that day's successful recovery receipt.
- `day-flatten-guard` checks the broker-calendar deadline every five minutes from noon through 4 PM ET on weekdays. The existing 3:45 PM Day report also checks the flatten result. Early closes move the deadline earlier.
- Swing, Day, and Algo close-snapshot jobs record account-specific `snapshot_kind=close` baselines after the actual exchange close. Holidays are successful skips.
- `algo-health` runs every ten minutes, including Sunday overnight, and exits nonzero on stale scans, state problems, broker protection gaps, or an inactive service.

The four Algo report jobs cover premarket, morning, midday, and close. Report jobs announce their summaries; command jobs alert on failures without delivering normal stdout. Existing trader and AutoResearch jobs also have explicit failure alerts and bounded timeouts. See [the maintained OpenClaw schedule and alert reference](ops/openclaw-crons.md) for the current job names, cron expressions, and operating details. Inspect the live declarations with:

```bash
openclaw cron list --all --json
```

The legacy `autoresearch-run` loop remains Day-only and uses five LLM-driven iterations. Algo has a separate fixed-candidate research pipeline and report; neither job changes live settings automatically.

---

## CLI Reference

Account-aware scripts should be run with an explicit `--mode`; each supported mode routes to its configured Alpaca account. Algo trade execution is reserved for the leased coordinator.

### Core Scripts

| Script | Purpose | Key Flags |
|---|---|---|
| `run_hedge_fund.py` | Multi-agent analysis + execution | `--execute`, `--tickers`, `--mode`, `--show-reasoning` |
| `gather_data.py` | Market data gathering | `--mode`, `--tickers`, `--top N`, `--output` |
| `execute_trades.py` | Trade execution with risk validation | `--mode`, `--file`, `--flatten`, `--flatten-if-due` (Day), `--dry-run` |
| `risk_manager.py` | Risk status check | `--status`, `--mode` |
| `portfolio_monitor.py` | Stop loss monitoring + auto-sell | `--mode`, `--dry-run` / `--no-sell` |
| `performance_tracker_v2.py` | Account-specific snapshots, benchmarking, optional broker fill P&L | `--mode`, `--snapshot`, `--snapshot-kind close`, `--report`, `--json`, `--fill-report`, `--days N` |
| `python -m src.account_baseline` | One-time weekly loss baseline for a verified unused paper account | `--mode` |
| `check_portfolio.py` | Quick portfolio overview | `--mode`, `--telegram`, `--json` |
| `check_moves.py` | Universe pulse check | `--mode` |
| `rebalance.py` | Sell non-universe positions | `--mode`, `--execute` |
| `trade_alerts.py` | Anomaly detection | `--check`, `--mode`, `--telegram` |
| `trade_journal.py` | Trade history + stats | `--show`, `--stats`, `--ticker` |
| `intel_exchange.py` | Peer signal sharing via A2A | `--mode`, `--dry-run`, `--json` |

---

## Project Structure

```
swarm-trader/
├── run_hedge_fund.py          # Main runner — analysis + execution
├── run_analysis.py            # Wrapper for run_hedge_fund.py
├── risk_manager.py            # V2 risk manager (11 hard rules, code-enforced)
├── portfolio_monitor.py       # Stop loss monitor + auto-sell + SPY comparison
├── performance_tracker_v2.py  # Daily snapshots, Sharpe ratio, benchmarking
├── execute_trades.py          # Trade executor with risk validation
├── gather_data.py             # Market data gatherer (swing + day modes)
├── check_portfolio.py         # Quick Alpaca portfolio check
├── check_moves.py             # Universe pulse check
├── rebalance.py               # Sell positions outside active universe
├── trade_alerts.py            # Anomaly detection (mode-aware thresholds)
├── trade_journal.py           # Persistent trade log
├── intel_exchange.py          # Peer signal sharing via A2A
├── trading_mode.json          # Mode steering file (auto/swing/day + overrides)
├── PLAYBOOK.md                # Complete operations guide
├── DESIGN.md                  # V2 design document (architecture + rules)
├── .env.example               # Secrets template
├── src/
│   ├── config.py              # MODES dict — single source of truth per mode
│   ├── accounts.py            # Three paper account routes + broker verification
│   ├── day_recovery.py        # Daily, account-bound verified opening receipt
│   ├── market_calendar.py     # Broker-calendar Day deadlines and early closes
│   ├── fill_analytics.py      # Read-only broker FILL FIFO realized P&L
│   ├── algo/                 # One coordinator, SQLite state, reconciliation, health
│   │   ├── daemon.py          # Continuous coordinator and execution lease
│   │   ├── state_store.py     # Authoritative SQLite strategy/owner state
│   │   ├── reconciliation.py  # Exact broker and journal ownership checks
│   │   ├── healthcheck.py     # Read-only daemon, state, broker protection check
│   │   └── sessions.py       # ET broker sessions and BOATS/overnight feeds
│   ├── strategies/             # Pure rules, per-strategy state, regime, universe
│   │   ├── mean_reversion.py   # Bollinger mean reversion + Open Dip adapter
│   │   ├── trend_following.py  # Moving-average crossover
│   │   ├── momentum.py         # Price and volume confirmed momentum
│   │   ├── opening_range_breakout.py # 30-minute ORB rule
│   │   ├── pairs.py            # Signal-only statistical pair analysis
│   │   ├── regime.py           # Deterministic SPY strategy ranking
│   │   ├── universe.py         # Risk-catalog constrained liquid universe
│   │   ├── registry.py         # Equal strategy-family evaluation interface
│   │   ├── state_machine.py    # Per-strategy ticker entry/position/exit lifecycle
│   │   ├── regime_advisor.py   # Optional bounded local Ollama ranking advice
│   │   └── runner.py           # Alpaca scan + shared executor handoff
│   ├── tools/
│   │   ├── api.py             # Hybrid dispatcher (paid → free fallback)
│   │   ├── api_free.py        # Free data layer (SEC EDGAR + yfinance)
│   │   └── api_original.py    # Original financialdatasets.ai client
│   ├── agents/
│   │   ├── mordecai.py        # Mode-aware contrarian agent
│   │   ├── apex.py            # Intraday day trading agent
│   │   ├── market_regime.py   # Market regime classifier
│   │   ├── portfolio_manager.py
│   │   └── ...                # 12 LLM + 6 data/quant agents
│   ├── alpaca_integration.py  # Read-only Alpaca helpers + legacy decision previews
│   └── llm/
│       └── models.py          # 13 LLM provider definitions
├── autoresearch/
│   ├── strategy.py            # Evolved strategy (agent-modified)
│   ├── evolve.py              # Evolution loop orchestrator
│   ├── backtest_fast.py       # Fast backtester + fitness scorer
│   ├── analyze.py             # Cross-run analytics
│   └── experiments/           # Experiment logs
├── snapshots/                 # Daily performance snapshots
├── data/                      # SQLite Algo state, JSON status, Day receipt, journal
├── ops/                       # Maintained OpenClaw job and prompt references
└── docs/
    ├── architecture.png       # System architecture diagram
    └── execution-methods.png  # Order types + safety rails reference
```

---

## Configuration

### Mode Config (`src/config.py`)

All configuration lives in the `MODES` dict — one complete, self-contained config per mode:

```python
from src.config import get_mode_config

config = get_mode_config("swing")  # or "day"
universe = config["universe"]      # sector → tickers + caps
risk = config["risk"]              # all risk parameters
```

### Mode Steering (`trading_mode.json`)

```python
from src.config import resolve_mode, set_mode

# Check current mode
mode = resolve_mode()  # "auto", "swing", or "day"

# Agent sets mode
set_mode("swing", reason="VIX 18, low vol", updated_by="agent")

# Human override for 4 hours
set_mode("day", reason="FOMC day", updated_by="human", override=True, override_hours=4)
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `BLOCKED: Daily circuit breaker` | Down past limit today | Normal — resets next trading day |
| `BLOCKED: Max 12 open positions` | Too many positions | Close something first or switch to day mode (8 max) |
| `BLOCKED: leveraged ETF` | Trying to buy TQQQ in swing mode | Switch to day mode or remove the trade |
| `ALPACA_API_KEY not set` | Missing `.env` | Copy `.env.example` to `.env` |
| `No Alpaca account configured for mode 'day'` | Day credentials are missing | Add `ALPACA_DAY_API_KEY` and `ALPACA_DAY_API_SECRET` to `.env` |
| `insufficient qty` on sell | Shares can be reserved by open broker orders | Inspect the account's open orders and retry after broker cancellation completes |
| Day buy rejected for missing opening cleanup | Today's account-bound receipt is missing or stale | Run and verify `execute_trades.py --mode day --flatten` before a new entry |
| Algo entries paused after restart | Ownership, stop, state, or data check failed | Inspect `poetry run python -m src.algo.daemon --status` and the `algo-health` report; resolve the broker/state mismatch before new entries |
| Algo exit deferred outside regular hours | A protective bracket/OCO child is still open or `pending_cancel` | Review the broker order state; the coordinator reevaluates the exit during regular hours |
| Close snapshot is not due | The broker's actual exchange close has not passed | Run `--snapshot-kind close` after that day's Alpaca calendar close |
| Scanner returns only core tickers | Market closed | Scanner works during market hours |
| Risk manager rejects everything | Multiple rules violated | Run `risk_manager.py --status` to see what's wrong |
| `flatten` sells nothing | No positions or market closed | Expected behavior |

---

## Credits

Built on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund). Extended with dual-mode trading, code-enforced risk management, free data sources, Alpaca execution, and autonomous strategy evolution.

AutoResearch loop adapted from [karpathy/autoresearch](https://github.com/karpathy/autoresearch). Core idea and architecture are his — we ported it to trading strategy evolution.

## Disclaimer

Educational and research purposes only. Not investment advice. Paper trading only.

## License

MIT
