# Algo Trader setup

Part of the [Algo Trader README](../README.md).

## Requirements

Python 3.11 or 3.12, Poetry, and a C compiler toolchain to build the optional Cython performance extensions (for example, `build-essential` on Debian/Ubuntu). Python 3.13 is not supported yet.

## One command

```sh
export ALGO_REPORT_TELEGRAM_TO=<chat id>   # report destination for the agent-turn automations
scripts/setup.sh                           # poetry install, then installs the scheduled automations
scripts/setup.sh --dry-run                 # preview the automation changes first
```

`scripts/setup.sh` installs the Python dependencies, renders the two systemd user units into `~/.config/systemd/user` (no root), and installs the scheduled automations. It does not start the services and does not create `.env`. Start them with `systemctl --user enable --now equity options`; see [Operations](operations.md). The automation installer is described in [Scheduled automations](automations.md).

## Environment

The live systemd units read the OpenStock root `.env` (the `EnvironmentFile` in the rendered units), so the Algo keys belong there, not in a second file. `ALGO_TRADER_ROOT=./algo-trader` in that file points OpenStock at this directory. The variable names are in `algo-trader/.env.example`.

- Equity uses `ALPACA_EQUITY_API_KEY`, `ALPACA_EQUITY_API_SECRET`, and `ALPACA_EQUITY_ACCOUNT_ID`.
- The options sleeve uses the `ALPACA_OPTIONS_*` equivalents.
- Keep the Alpaca URLs on paper. They default to paper; any other endpoint is an explicit override.

Keep `.env` out of source control.
