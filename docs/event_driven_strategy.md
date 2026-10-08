# Scheduled event strategy

The Algo event strategy is a sparse, long-only satellite. Version 1 reacts to
confirmed post-event price action; it does not take pre-event positions and it
does not read headlines or earnings-surprise values.

## Module layout

- `src/strategies/event_driven/models.py`: validated event records.
- `calendar.py`: point-in-time CSV calendar reader.
- `filters.py`: universe, regime, cooldown, and weekly activity limits.
- `reactions.py`: earnings gap continuation and macro breakout rules.
- `signals.py`: event-to-`Signal` adapter plus counters.
- Algo integration: registry/regime selection, standard sizing, shared order
  route, and the local portfolio replay.

## Event record format

Each calendar row must contain `event_id,event_type,ticker,scheduled_at,known_at,
reaction_start_at,reaction_end_at,source`. Every timestamp must contain an
explicit UTC offset. Earnings rows name the company ticker. Macro rows target
`SPY`, which is included in the fixed Algo universe for macro trades.

`known_at` is the first time this exact event record was available. The loader
does not expose a record to a replay before `known_at`. For historical tests,
prefer archived calendar snapshots. If the original publication time is
unknown, setting `known_at` to the event release time is conservative: the
strategy cannot act before the release. Keep revisions as separate event IDs
and do not overwrite the original row in historical datasets.

The reaction window is explicit because an after-close earnings report should
map to the next actual trading session, including holidays. The daily calendar
refresh uses Alpaca's exchange calendar for that mapping. The Algo profile now
enables the event satellite; the standalone `EventStrategyConfig` remains off
by default. Macro events use SPY from the fixed Algo universe. Other strategies
stay limited to that same configured universe.

Run `poetry run python -m src.algo.event_calendar_refresh` to refresh
`data/event_calendar.csv`. The `algo-event-calendar` OpenClaw job runs this at
04:05 ET on weekdays. It reads the configured fixed Algo universe, obtains scheduled
earnings and pre/post-market classifications from Nasdaq's public earnings
calendar, and uses Alpaca's market calendar to map after-hours reports to the
next session. Earnings without a supplied timing classification are skipped.
The job writes atomically, archives each point-in-time refresh under
`data/event_calendar_snapshots/`, and retains the previous live calendar on
feed failure.
The encoded CPI and Employment Situation dates currently cover Q4 2026. The
Federal Reserve calendar includes its published tentative FOMC decision dates
through 2027. Extend the BLS dates as they are published; calendar diagnostics
warn when an event type has no upcoming date in the coverage window.

## V1 rules and limits

- Earnings: use the configured first three 5-minute bars in the reaction
  window as the opening range. Require a positive overnight gap between 2% and
  8%, a later close above that range, volume at least 1.5 times the preceding
  event-window bars, and a close above bar VWAP when supplied.
- Macro: only `SPY`; require the same range break and volume confirmation plus
  at least a 0.4% positive move from the event-window open.
- Entries stop 90 minutes after `reaction_start_at`; the supplied calendar
  window can end earlier. Signals are long-only and target at most 2.5% of
  equity. A 2% stop and 0.05% equity event-risk budget bound the proposed size.
- At most three event proposals per ISO week, a 24-hour portfolio cooldown,
  and a five-day per-ticker cooldown. Volatile-regime days are blocked by
  default. All of these settings are configurable.

The generated `Signal` carries `stop_pct`, `target_weight`, and `event_id`. Live
Algo processing turns it into the usual trade proposal through `_make_trade`;
`execute_trades.py` then runs the normal `risk_manager.validate_trade()` checks
before Alpaca submission. The local replay uses the same `_make_trade` sizing
and `validate_trade()` gate, and fills accepted signals no earlier than the
next 5-minute bar.

## Sources and limitations

Alpaca's calendar endpoint is the exchange trading calendar, not an earnings
calendar. [Nasdaq's earnings calendar](https://www.nasdaq.com/market-activity/earnings)
provides the forward earnings dates and pre/post-market classification used
for live ingestion. [BLS publishes its official release calendar and subscribable ICS](https://www.bls.gov/schedule/news_release/bls.ics);
the [Federal Reserve publishes official FOMC schedules](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm).
[SEC EDGAR has free submission APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
and can reconstruct observed earnings-release times from filings, but it is
not a forward earnings-schedule feed. The Apple example is supported by the
[SEC filing detail](https://www.sec.gov/Archives/edgar/data/320193/000032019325000071/0000320193-25-000071-index.htm)
and [Apple's release](https://www.apple.com/newsroom/2025/07/apple-reports-third-quarter-results/).
The calendar CSV remains the replay boundary. The live updater stores the
refresh time in `known_at` and archives each snapshot; historical replay should
use those point-in-time files rather than treating today's forward calendar as
historical truth.

Example historical earnings and macro rows are in
`examples/event_calendar_2025.csv`. They use the observed release time as
`known_at`, so the simulator cannot see the event earlier. The earnings example
uses Apple's July 31, 2025 Form 8-K (Item 2.02) acceptance time and the next
regular session as its reaction window. The macro examples use the official BLS
release dates and Federal Reserve FOMC schedule. For a replay, ingest split-
adjusted 5-minute SIP bars and run:

```bash
poetry run python -m src.algo.backtest fetch \
  --symbols AAPL,SPY --start 2025-07-31 --end 2025-09-17 \
  --strategies event_driven --adjustment split

poetry run python -m src.algo.backtest run \
  --symbols AAPL,SPY \
  --start 2025-07-31 --end 2025-09-17 \
  --strategies event_driven \
  --adjustment split \
  --profile examples/event_strategy_profile.json \
  --event-calendar examples/event_calendar_2025.csv \
  --output data/algo_research/event_calendar_example.json
```

The minimal profile is:

```json
{"event_driven": {"enabled": true}}
```

The example is an interface demonstration, not a performance claim. It needs
local SIP bars for the requested date range and at least 22 earlier SPY daily
bars for the deterministic regime filter.
