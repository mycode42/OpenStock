# Operations

Part of the [Algo Trader README](../README.md).

## Common commands

```sh
poetry run python -m portfolio.healthcheck                 # combined equity and options health
poetry run python -m portfolio.daemon --mode algo          # run the Algo coordinator
poetry run python -m equity_research.auto_research --workers 8
poetry run python -m options.paper                         # options dry run
poetry run python -m options.paper --submit                # submit to options paper
poetry run python -m options.research.universe_selection simulate --universe-scope history_117
```

Production runs as two user units, `equity` and `options`, rendered from `ops/systemd/` by `scripts/install_units.py` (`scripts/setup.sh` runs it). They need no root:

```sh
systemctl --user enable --now equity options   # start, and at login or boot
systemctl --user restart equity                         # apply config or code changes
journalctl --user -u equity -n 50                       # logs
```

Lingering keeps user units running after logout and across reboots (`loginctl enable-linger <user>`). Scheduled research and reports are described in [Scheduled automations](automations.md) and [ops/openclaw-crons.md](../ops/openclaw-crons.md).

Changes to `portfolio/config.py` or the daemon take effect only after a restart.

## Live universe

Each session the daemon runs the eligible-universe selector over the 117-name pool in `options/research/universe.json`. The result is stored as a dated snapshot in `data/options/research/options_history.sqlite3`. Entries pause if the snapshot is more than one session old, or if fewer than six names are eligible. If the SPY market-date anchor is missing or stale, the live selector retries from completed Alpaca daily bars, preferring SIP and falling back to IEX. Those recovery rows contain SPY price and volume only; they do not supply option-chain liquidity metrics. Positions that leave the universe are still managed until they exit. Regime context stays on the static 24-name set in `portfolio/config.py`.

## State and backups

Coordinator state, the broker order ledger, and the research checkpoints all live in SQLite. The state database at `data/algo_state.sqlite3` is backed up once per trading day to `data/backups/algo_state/`; the newest 30 are kept. To restore, stop the daemon and copy a backup over the database.
