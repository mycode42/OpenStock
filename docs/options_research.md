# Options research and validation

Options sleeves use a separate research loop from the equity autoresearch
pipeline. Each run evaluates one expression module: `vrp`, `event_driven`, or
`directional_overlay`. The event-driven run consumes frozen point-in-time event
recommendations; it never searches or invokes the LLM analyst itself.

## Run one sleeve

Provide chronological SPY (default) or SPX option-chain snapshots, then run:

```sh
poetry run python -m options.research --strategy vrp \
  --data data/options/research/chains.jsonl
```

Use `--config` for a JSON override and `--output-dir` to change the artifact
location. The defaults and search ranges are in
`options/research/config.default.json`. Research does not read `.env`, contact
the broker, or submit orders. Run-specific artifacts are written beneath
`data/options/research/runs/`; each run gets a reproducibility record, JSON and
Markdown reports, an OOS equity/Greek CSV, and a live-format candidate config.
Candidate-specific fold metrics and selected OOS summaries are also written to
`candidate_reports.json`; closed OOS and holdout trades have separate CSVs.
An `approved-options-config.json` is emitted only if the configured validation
gates pass. Otherwise the export is named `candidate-options-config.json` and
the researched strategy section is disabled so copying it cannot enable an
unvalidated sleeve by accident.

## Nightly automation

The scheduled AutoResearch automation runs `autoresearch.nightly`, which starts
the existing equity research command and an independent options job concurrently.
The default options batch runs `vrp` and `event_driven`; set
`automation.strategies` in this file's JSON configuration to select a different
subset, including `directional_overlay`. Options research runs at lower process
priority and writes a separate dated JSON/Markdown report plus `latest.json` and
`latest.md` under `data/options/research/automation/`. The equity report also
contains a top-level `options_research` summary section. Candidates remain
research-only and are never applied automatically.

If the configured chain dataset does not exist, the options report uses
`waiting_for_data` and records the missing path. The equity research cycle still
completes independently. Supply historical option-chain snapshots (or explicitly
marked point-in-time IV-surface rows) before expecting options validation runs.

## Snapshot format

The input can be a JSONL stream (one snapshot per line), a JSON array, or an
object with a `snapshots` array. A snapshot has this shape:

```json
{
  "as_of": "2024-01-03T21:00:00Z",
  "underlying": "SPY",
  "spot": 470.0,
  "underlying_close": 470.0,
  "regime": "range_bound",
  "volatility_state": "normal",
  "feed": "historical",
  "contracts": [
    {
      "symbol": "SPY240216P00450000",
      "expiration": "2024-02-16",
      "right": "put",
      "strike": 450.0,
      "bid": 2.1,
      "ask": 2.2,
      "quote_at": "2024-01-03T20:59:59Z",
      "iv": 0.22,
      "open_interest": 1200,
      "multiplier": 100,
      "exercise_style": "american"
    }
  ],
  "events": [],
  "event_recommendations": [],
  "equity_signals": []
}
```

Contract rows retain the vendor's original symbol, bid/ask, IV, open interest,
multiplier, and exercise style. `quote_at` must be timezone-aware. Each
observation should include a full enough chain for the strategy's DTE and delta
selection. If `underlying_close` is omitted on every row, spot is used as a
close proxy and the report marks that weaker data mode. To supply a separate
historical close series, a JSON object may include `underlying_history`, keyed
by symbol, with `[date, close]` rows.

For `event_driven`, include the frozen output from the existing event-driven
LLM briefing in `event_recommendations`, keyed by ticker or as an array. Each
signal should include `action`, `confidence`, `known_at`, `run_id`, and
`reason`. The options search only varies the expression layer (confidence
threshold and target DTE); it never reruns or searches the LLM analyst. The
separate scheduled-event calendar is not required for this sleeve. Equity
signals can also be supplied as frozen inputs; include `known_at` so future
signals are excluded.

Regime and volatility labels should come from the same point-in-time detector
used by the equity strategies. Labels must be present on each snapshot; the
runner does not backfill regime classifications with future data.

For a pricing-model replay, set `data_mode` to `model_surface` and provide
`expirations` plus point-in-time `iv_surface` points on each observation. Each
point has `expiration`, `strike`, `right`, and decimal `iv` (or IV as a
percentage). The provider interpolates from the nearest four same-right points
in expiry/moneyness space, then generates a strike grid and synthetic quotes.
Model rows do not contain observed open interest. Set
`market_data.require_open_interest` to `false` only when deliberately accepting
that missing liquidity field; the report will identify the source as modeled.
The `model_surface` config controls strike-grid spacing, synthetic spread, and
exercise style (defaults to American for SPY and European for SPX).

## Validation protocol

- Rolling training windows tune a small, configuration-defined grid or an
  interpretable inverse-distance surrogate. Search candidates are ranked on
  training data with a neighbor/plateau score.
- OOS windows do not overlap. The gap before each OOS window is the larger of
  the strategy's longest lookback and the maximum structure DTE, plus the
  configured embargo. Several OOS windows are required before the final holdout.
- The untouched tail holdout must cover the configured number of distinct
  market regimes and several high-volatility sessions. It is evaluated only
  after the fold selections and final parameter consensus are fixed.
- Per-leg fills cross the configured fraction of the observed spread and pay
  commissions on entry and exit. Sensitivity runs widen displayed spreads and
  increase spread-crossing slippage; contracts that then fail spread/liquidity
  filters are skipped. Multi-leg structures are modeled as atomic package
  orders with a separate, lower completion probability; partial leg fills are
  not simulated because the paper route submits complex orders as MLegs.
- Missing chains, stale quotes, wide spreads, missing/low open interest, and
  fill failures are counted. The report includes Sharpe, drawdown, return,
  walk-forward efficiency, candidate stability, OOS regime and volatility
  breakdowns, Greek paths, and attribution summaries.
- Assignment sensitivity uses a basic American exercise heuristic plus
  deterministic pin-risk simulation. European contracts are cash-settled when
  their exercise style is correctly marked. A seeded permutation of realized
  OOS trade P&L checks sequence sensitivity; it is not a market-path simulator.

## Configuration and limits

Tune windows, minimum trade counts, embargo, regime coverage, candidate grids,
spread/OI filters, costs, assignment thresholds, Greek limits, cost sweeps,
and random seeds in the JSON config. Search grids are intentionally bounded to
seven values per parameter. The final export modifies one existing strategy
section in the same full JSON format used by the paper system; it does not
write the live options config file.

The baseline simulator holds one defined-risk structure at a time, uses one
contract by default, and closes at expiry or the validation-window boundary.
Greeks are calculated from historical IV and a pricing model; the input may
contain vendor Greeks, but the current loader recalculates them for consistent
units. Delta/gamma/vega/theta P&L attribution uses a local Taylor
approximation and can be inaccurate across large moves or stale quote gaps.
Synthetic or model-generated chains must be explicitly labeled with
`feed: "model"` or `feed: "synthetic"`; they are reported separately and do
not establish empirical fill quality.

This research loop enforces sleeve-level premium and Greek limits. It does not
replay a contemporaneous stock portfolio, so it cannot measure cross-account
hedges or net Greeks; the live coordinator and risk manager remain responsible
for combined equity/options exposure. Before trusting results for live sizing,
use point-in-time historical chains, official dividend and corporate-action
records, accurate exercise/settlement metadata, and observed multi-leg fills.

The options sleeve architecture and paper route are described in
`docs/options_trading.md`.
