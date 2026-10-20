# Options sleeves

The options implementation is an additional sleeve. Existing stock strategies
remain under the Algo coordinator; options orders use the dedicated
`ALPACA_OPTIONS_*` paper credentials and fixed Alpaca paper API URL.

## Modules and data flow

| Module | Responsibility |
| --- | --- |
| `options/models.py` | Contracts, legs, structures, signals, positions, orders, fills, Greeks, and portfolio risk summaries |
| `options/structures.py` | Vertical spreads, iron condors, long straddles/strangles, protective puts, collars, and signed package pricing |
| `options/pricing.py` | Black-Scholes, CRR American pricing, finite-difference Greeks, and IV solving |
| `options/data.py` | Provider protocol plus Alpaca, historical chain snapshot, and model IV-surface implementations |
| `options/risk.py` | Cross-account delta, gamma, vega, theta, defined-loss, buying-power, assignment, and stress checks |
| `options/assignment.py` | Basic American early-exercise/assignment heuristic for replay |
| `broker/options.py` | Options-only Alpaca adapter; all order URLs are pinned to paper trading |
| `options/paper.py` | Independent paper scan, order intent, reconciliation, and per-account journal |
| `options/daemon.py` | Market-hours scans using the same account lock and paper risk path |
| `strategies/options/vrp.py` | Rule-based SPY IV-versus-realized-volatility iron condor |
| `strategies/options/event_driven.py` | Event-driven LLM briefing signals expressed as defined-risk debit spreads |
| `strategies/options/directional_overlay.py` | Frozen equity signal expressed as a defined-risk debit vertical |
| `options/research/` | Separate options search, purged walk-forward validation, costs, assignment, and reports |

`OptionSignal.desired_positions` gives each leg's signed target contracts and
delta, gamma, vega, theta, and multiplier. The equity coordinator reads the
published options delta-notional snapshot into its gross/net exposure book.
Before an options order, `PortfolioRiskManager` rebuilds exposure from the Algo
equity account and the options account, then checks Greek, stress, assignment,
margin, and defined-loss limits together. Configurable regime weights scale the
per-trade risk budget; delta, gamma, vega, and negative theta each have separate
portfolio limits.

## Run the paper route

The `.env` file should contain `ALPACA_OPTIONS_API_KEY`,
`ALPACA_OPTIONS_API_SECRET`, and `ALPACA_OPTIONS_ACCOUNT_ID`. The route verifies
that the configured ID resolves to an active paper account and differs from the
Algo account. It does not support live URLs.

```sh
# Read account data and produce/log a dry-run decision; no order is submitted
poetry run python -m options.paper

# Submit only approved orders to Alpaca's paper endpoint
poetry run python -m options.paper --submit

# Run a market-hours loop; omit --submit for a dry-run daemon
poetry run python -m options.daemon --submit
```

The packaged defaults are in `options/config.default.json`. Copy that file to
`data/options/config.json` to adjust DTE windows, IV-RV thresholds, contract
limits, quote filters, risk limits, and cost assumptions without editing code.
Regime risk multipliers can disable a sleeve with `0`, reduce or increase its
trade risk with values below or above `1`, and default to disabled for an
unknown regime. The paper runner also stops new entries after the configured
combined-account daily loss limit is reached.
`ALPACA_OPTIONS_FEED` can select `opra` or `indicative`; the default is OPRA.
The runner rejects stale or one-sided option quotes. Alpaca documents OPRA as
the consolidated feed and indicative data as a delayed, modified derivative
([market-data details](https://docs.alpaca.markets/us/docs/historical-option-data),
[option snapshots](https://docs.alpaca.markets/us/reference/optionchain)).
The systemd unit scans every 15 minutes during regular US hours, starts 10
minutes after the open, and stops opening new trades 15 minutes before close.
It submits orders only to the separate Alpaca paper endpoint.

The dedicated options research runner is separate from equity autoresearch:

```sh
poetry run python -m options.research --strategy vrp --data data/options/research/chains.jsonl
```

See `docs/options_research.md` for the point-in-time chain format, purged
walk-forward protocol, execution assumptions, and report artifacts.

Each account gets private JSONL streams for order intents/responses, fills,
position Greeks, assignment activities, decisions, and daily risk summaries in
`data/options/accounts/<account-hash>/`. The coordinator's small shared exposure
read model is `data/options/risk_snapshot.json`.

## Initial strategy rules

- VRP uses SPY options with 25–45 DTE by default. It compares ATM IV against
  annualized realized volatility, requires an approved regime, then selects a
  1:1 defined-risk iron condor from short-delta and wing-delta bands.
- Event options consume the fresh point-in-time briefing produced by the
  existing event-driven LLM strategy. A `buy` recommendation becomes a call
  debit spread and a `sell` recommendation becomes a put debit spread. The
  options expression does not require or generate the legacy scheduled-event
  calendar, and it does not invoke or optimize the LLM core.
- Both sleeves produce one contract before risk sizing. Sizing then uses the
  options account equity, maximum loss, estimated costs, current positions,
  combined stock/options delta, Greek limits, stress scenarios, assignment
  shares, and broker buying power.

## Assumptions and limits

- Current live-chain Greeks are calculated from quoted IV with a fast
  Black-Scholes approximation. The CRR lattice supplies American prices/Greeks
  for model work. Historical/modeled runs can inject an IV surface and closes;
  the real underlying-specific surface, borrow curve, discrete dividends, and
  exchange-specific early-exercise probabilities still need vendor data.
- Commissions default to $0.65 and additional slippage to $5 per option leg and
  contract. These are conservative editable estimates, not a statement of
  Alpaca's current fee schedule or a guarantee of paper fills.
- Early assignment simulation flags short American options near expiry or
  short calls whose extrinsic value is below a configured dividend. It is a
  deterministic risk warning; the broker's clearing process remains authoritative.
- Vertical spreads, iron condors, straddles, and strangles route as a single
  MLeg limit order. Alpaca requires options-level approval for spreads and
  supports MLeg on paper ([options trading](https://docs.alpaca.markets/us/docs/options-trading),
  [MLeg orders](https://docs.alpaca.markets/us/docs/options-level-3-trading)).
- Protective puts and collars route as option orders backed by shares already
  held in the options account. The stock is not part of the option order, so
  manual stock changes after entry can alter coverage and need reconciliation.
- Automatic stop/roll policy for an open options structure is not part of this
  first paper runner. It reconciles existing positions, blocks duplicate/open
  orders, logs marks and assignment activity, and reserves risk through expiry.
  Alpaca may automatically exercise in-the-money contracts at expiry, so review
  the expiry policy before scheduling unattended submissions
  ([exercise and assignment](https://docs.alpaca.markets/us/docs/options-trading)).
