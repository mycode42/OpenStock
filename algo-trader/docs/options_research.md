# Options research and validation

Options sleeves use a separate research loop from the equity equity_research
pipeline. Each run evaluates one expression module: `vrp`, `llm_driven`, or
`directional_overlay`. The LLM-driven run consumes frozen point-in-time event
recommendations; it never searches or invokes the LLM analyst itself.

## Run one sleeve

Provide chronological option-chain snapshots, then run one sleeve. SPY is the
default underlying; use `--underlying` to select another ticker:

```sh
poetry run python -m options.research --strategy vrp \
  --underlying NVDA \
  --data data/options/research/options_history.sqlite3
```

Use `--config` for a JSON override and `--output-dir` to change the artifact
location. The defaults and search ranges are in
`options/research/config.default.json`. Research does not read `.env`, contact
the broker, or submit orders. Run-specific artifacts are written beneath
`data/options/research/runs/`; each run gets a reproducibility record, JSON and
Markdown reports, an OOS equity/Greek CSV, and a live-format candidate config.
Candidate-specific fold metrics and selected OOS summaries are also written to
`candidate_reports.json`; closed OOS trades are exported as CSV.
Compressed copies of reports, run artifacts, and candidates are stored in
`data/options/research/research.sqlite3`; the files remain available as exports.
An `approved-options-config.json` is emitted only if the configured validation
gates pass. Otherwise the export is named `candidate-options-config.json` and
the researched strategy section is disabled so copying it cannot enable an
unvalidated sleeve by accident.

Every standard run also evaluates a fixed-default baseline over the same rolling
OOS folds as the tuned walk-forward strategy, using the same execution-cost
assumptions. The per-run and nightly reports show tuned
minus baseline return and risk-adjusted-score deltas; baseline curves and trades
are exported alongside the tuned artifacts. This comparison is informational:
the approval gates still assess absolute validation results. Promotion over the
live champion is decided separately (see below).

Promotion. A candidate is approved only if the OOS gates pass. The live champion
is replayed on the same OOS folds, and the candidate replaces it only if it ranks
above it: lower-quartile per-fold fitness, then median fitness, then fold
consistency (`runtime/fold_ranking.py`, the same rule equity uses). The first
approved candidate becomes champion. Decisions are stored in
`options_champion_decision`; the champion in `options_champion`.

## Nightly automation

The OpenClaw Gateway schedules options and equity research as separate jobs:
`options-research` runs `python -m research.options` at 17:00 ET, and
`equity-research` runs `python -m research.equity` at 20:00 ET. The options job
currently runs VRP and long-volatility. VRP screens the 117-symbol history
candidate pool one ticker at a time; long-volatility keeps its explicit
9-symbol list. LLM-driven options research remains outside the nightly preset
until frozen point-in-time recommendations are available. Each ticker reads its effective ThetaData and
Alpaca history from `data/options/research/options_history.sqlite3`, selected
through `automation.data_path_template`;
missing or not-yet-long-enough datasets are reported per ticker while available
datasets continue through research. Edit `automation.strategies` in
`config.portfolio.json` to select a different subset, including
`directional_overlay`. The options job uses the worker budget in its portfolio
config (8 by default), capped by the CPUs available to the process. Options
research runs at lower process priority and writes a separate dated
JSON/Markdown report plus `latest.json` and `latest.md` under
`data/options/research/automation/`. The report records the requested and used
worker counts. Candidates remain research-only and are never applied
automatically.

The manual aggregate command `python -m research` runs options first, then
equity; it does not run the stages concurrently. Its combined report includes
an `options_research` summary. The scheduled jobs call their separate entry
points and produce separate reports.

If the configured chain dataset does not exist, the options report uses
`waiting_for_data` and records the missing path. The aggregate command still
continues to equity research, and the scheduled equity job runs independently.
Supply historical option-chain snapshots (or explicitly marked point-in-time
IV-surface rows) before expecting options validation runs.

## Eligible universe snapshots and portfolio exposure checks

The optional research selector starts with the 117-symbol history universe and
reads dated `daily_close` rows from the normalized history store. It applies a rolling 20-session
options activity gate (average contract volume or total open interest), equity
share volume and dollar volume gates, and a median option spread gate around the
money. Thresholds, liquidity coverage requirements, sector proxy mappings,
regime rules, and exposure caps are versioned in
[`eligibility.default.json`](../options/research/eligibility.default.json).
The initial values are screening candidates, not fitted thresholds.

The selector ranks configured sector ETF proxies by 20-session relative
strength, uses SPY's 200-session trend and stored volatility label for the
low-cardinality regime, then stores a shared eligible list with per-ticker
metrics and exclusion reasons. A small core can pass the soft sector overlay
only; it still must pass every hard liquidity gate. It also creates an equal
notional exposure preview with sector and rolling-correlation cluster limits.
The preview is for research portfolio simulations and does not change live
position sizing.

Option daily volume and underlying daily share volume are now retained in the
normalized history store. The Theta EOD collector reads the resumable monthly
archives and writes the refreshed snapshots and liquidity summaries directly
to SQLite. Until summary coverage exists, the selector fails closed and returns
no eligible tickers. Underlying historical NBBO spread
is not stored, so the hard spread gate currently uses option spreads.

For live selection, if SPY's daily-close date is missing or too stale, the
selector fills the missing SPY market bars from Alpaca (SIP first, IEX fallback)
and retries. These fallback rows contain completed SPY close and share-volume
data only; they do not create option contracts or liquidity summaries. ThetaData
remains a historical chain source, not the live market-date fallback.

After the history import is current, store a dated selector snapshot and run the
separate selection walk-forward report:

```sh
poetry run python -m options.research.universe_selection cache   # writes a snapshot to options_history.sqlite3
poetry run python -m options.research.universe_selection simulate
poetry run python -m options.research.universe_selection simulate --universe-scope history_117
```

The simulation defaults to the original 24 tickers, using a chronological
60/20/20 train, validation, and untouched holdout split. It reports eligible
set size and turnover, sector exposure and cap binding, correlation exposure,
and how often the regime filter changes the set versus a sector-momentum-only
counterfactual. Every period also reports the selected basket's return minus
the equal-weight average of the scope universe and minus SPY over the same
hold, with a t-statistic (holds do not overlap). Transaction costs are not
modeled.

The first `history_117` run (2026-10-27, 1,713 sessions, 17 configurations)
shows the selected basket trailing the equal-weight universe by about 0.9% per
five-session hold (t ≈ -3.1 over all sessions, -0.3% on the holdout) and
roughly matching SPY. Part of that gap may be style: the risk-off rules favor
defensive buckets, which can lag in rising markets. Either way the selection adds
no excess return over the universe, so the selector must not be applied to
offline research. The liquidity thresholds barely change the output on this history. After reviewing that result, run the broader candidate pool with
`--universe-scope history_117`. Its parameter grid changes
one rule at a time and reports all results; it does not pick or promote a
winner. It evaluates selection composition rather than any strategy's option
entries or exits.

Nightly automation stores the shared snapshot after the normalized SQLite store is
updated. Applying it to the research ticker jobs is a separate
`automation.eligibility.apply_to_research` switch, which stays `false` while the
volume fields are being repopulated and the selection walk-forward remains
unvalidated. Once validated, set it to `true`; every configured options sleeve
will then use the same snapshot list, intersected with its own strategy-specific
universe. This switch affects only offline research. The live equity universe
is the same selector's eligible subset of the 117-symbol pool, applied by the
Algo daemon each session; see the README.

## ThetaData historical backfill

ThetaData EOD bid/ask chains and historical open interest feed the same snapshot
loader used by the research runner. The collector writes directly to
`options_history.sqlite3`. It retains compressed monthly Theta responses as a
resumable download cache, but no longer writes per-symbol or combined chain
JSONL files. Its default is the 117-symbol research
history universe in [`universe.json`](../options/research/universe.json), which
is separate from live Algo portfolio membership. It starts at 2020-01-01, with
later listing dates configured per symbol. Symbols with fewer than
1,013 usable sessions remain marked `collecting` in their per-symbol manifest.

Install the optional client on Python 3.12 or newer and run the collector:

```sh
poetry install --with theta,research
poetry run python scripts/options/theta_historical_backfill.py
```

The collector reads `THETA_DATA_API_KEY` from `.env` (it also accepts ThetaData's
official `THETADATA_API_KEY` name). To backfill a smaller set, repeat
`--underlying`, for example `--underlying SPY --underlying NVDA`. The output is
written under `data/options/research/theta/<SYMBOL>/`.

SQLite is the canonical research history. Per-symbol manifests distinguish
`research_ready` (at least 1,013 usable sessions) from `archive_complete` (all
requested monthly EOD and open-interest files through the requested end date
were fetched). `research_ready` alone does not mean the full backfill is done.
If an older collector left chain JSONL files behind, migrate and remove only
database-verified copies with
`poetry run python scripts/options/migrate_chain_jsonl_to_sqlite.py --delete-verified`.
That command refuses to run while a Theta collector holds the backfill lock.

Historical opening snapshots for the same research history universe are
collected separately with `poetry run python -m scripts.options.theta_opening_backfill`. The collector
requests the last NBBO at 09:35 ET, applies the same 60-DTE and 20-strikes-per-
side filters, and writes opening snapshots directly to SQLite while retaining
compressed monthly source responses for resumability. Opening snapshots use the
underlying's daily opening price, prior-session close, and prior-session open
interest to avoid same-day lookahead; they remain separate from the
one-row-per-session close dataset. If local EOD open-interest archives arrive
after the opening quote backfill, rebuild those derived rows with
`--compile-only` after the EOD backfill finishes.

## Daily Alpaca capture

The `options-history-capture` OpenClaw automation starts at 15:45
America/New_York on weekdays, in the final 15 minutes before the regular close.
It saves same-session OPRA quotes for all 117 options research history tickers,
for expirations within 60 days and up to 20 strikes on each side of spot per
expiry. Snapshots with fewer than 20 valid same-session quotes are rejected and
logged per ticker. The collector reads only the Alpaca options account
credentials; it does not read or call ThetaData. Its `as_of` is the actual
capture time capped at the session close. A daily stock bar fetched before
16:00 ET may still be forming, so this is a close-side snapshot rather than a
guarantee of the final stock closing print.

Alpaca rows are upserted directly into `options_history.sqlite3`; this job does
not rebuild legacy merged JSONL datasets. Research keeps
one effective row per New York session and prefers Alpaca when both providers
have that date. Thus Theta remains the historical backfill source, while daily
research advances from Alpaca. The capture can also be run manually with
`poetry run python -m scripts.options.alpaca_daily_capture`.

The `options-opening-capture` automation runs at 09:35 ET on weekdays for the
same 117-symbol history universe and stores a current-session opening snapshot
from Alpaca OPRA directly in SQLite. It uses a fresh
underlying quote for spot and records each contract's actual quote timestamp.
Opening rows are kept separate from the close archive and are not merged into
the daily research file.

After every ticker's Theta manifest reports `archive_complete: true` for the
same requested date range, and the Alpaca capture has successfully archived the
latest session for the configured universe, the Theta subscription is no longer
needed to extend the dataset. The Alpaca automation will continue adding current
sessions to SQLite.

The capture CLI defaults to the 24-symbol Algo portfolio for manual use. Pass
`--universe research` to collect the expanded history universe, as the two
scheduled Alpaca captures do. This research universe does not change live
trading eligibility; it expands the offline VRP candidate scan, while
long-volatility retains its explicit nine-symbol list. The candidate scan is a
research backtest and does not submit orders.

To narrow the nightly batch, set `automation.underlyings` in
`config.portfolio.json`. Every strategy runs on that shared list; there are no
per-strategy lists. The collectors write
directly to `data/options/research/options_history.sqlite3`; custom datasets can use
`automation.data_path_template` or per-symbol `automation.data_paths`. The
automation keeps one result per strategy and underlying. Symbols remain in the
report as `waiting_for_data` or `insufficient_history` until their own archive
is ready, then start research automatically on a later run.

The collector takes each option's EOD NBBO and prior-session open interest. It
keeps the unadjusted close as the option-pricing spot, while split-adjusting the
underlying close history for realized-volatility calculations using Yahoo
Finance split events. It labels trend from 50/200-session moving averages and
volatility from the trailing 252-session distribution of 20-session realized
volatility. The per-symbol manifest records these assumptions. Historical
option EOD marks are daily snapshots, not intraday replay data.

The added research tickers also have an independent Alpaca daily equity-bar
archive. Run `poetry run python -m scripts.options.alpaca_equity_history_backfill`
to collect split-adjusted OHLCV bars from each ticker's configured history start
through the latest completed session. The collector follows Alpaca page tokens
and writes a compressed per-symbol archive plus a checksum manifest under
`data/options/research/alpaca/<SYMBOL>/`.

## Snapshot format

The input can be a JSONL stream (one snapshot per line), a JSON array, or an
object with a `snapshots` array. A snapshot has this shape:

```json
{
  "as_of": "2024-01-03T21:00:00Z",
  "underlying": "SPY",
  "spot": 470.0,
  "underlying_close": 470.0,
  "equity_volume": 54230000,
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
      "volume": 418,
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
daily option volume, multiplier, and exercise style. Snapshot-level
`equity_volume` stores underlying daily shares traded when available.
`quote_at` must be timezone-aware. Each
observation should include a full enough chain for the strategy's DTE and delta
selection. If `underlying_close` is omitted on every row, spot is used as a
close proxy and the report marks that weaker data mode. To supply a separate
historical close series, a JSON object may include `underlying_history`, keyed
by symbol, with `[date, close]` rows.

For `llm_driven`, include the frozen output from the existing llm-driven
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
  configured embargo. Every session after warm-up is out-of-sample for some fold.
- There is no separate holdout. Champion decisions use the same folds as the
  approval gates, and the same rule applies to equity.
- Per-leg fills cross the configured fraction of the observed spread, floored at
  `slippage_per_contract_usd` and capped at `max_slippage_per_contract_usd`
  (the same function live limit prices use), and pay commissions on entry and exit. Sensitivity runs widen displayed spreads and
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
