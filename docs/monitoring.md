# Monitoring

Part of the [OpenStock README](../README.md). Settings are in [Configuration](configuration.md).

## Monitor cycle

`scripts/monitor.js` runs one cycle per invocation:

1. Reads enabled tickers from the Alpaca watchlist.
2. Fetches executable ask and bid quotes from Alpaca.
3. Evaluates BUY against ask and SELL against bid; SELL alerts wait for an open Alpaca position.
4. Queues Telegram alerts in the durable alert outbox and retries delivery until Telegram confirms it.
5. Fetches chart history directly from Alpaca.

The cycle uses bounded concurrency, a deadline, per-stock failure isolation, validated responses, explicit exit codes, and a single-run lock.

```bash
npm run monitor            # run one cycle
npm run monitor -- --help  # print usage only
```

## Alert levels

- Each alert level is independently one-shot. It retires when its alert is queued, so later polls cannot queue it again.
- An ambiguous Telegram send stays frozen in the outbox for a person to resolve. It is never sent twice automatically.
- Rearm a retired level explicitly through the alert-level API or the UI.
- `trigger_state` and the stock-level threshold columns are legacy compatibility data. `alert_levels` and open journal trades are authoritative.

## Journal positions

Position summaries and SELL eligibility come from the authenticated Alpaca account. Without an open position, SELL levels stay configured but do not fire.

## Chart history

Alpaca chart ranges use Alpaca bars directly.
