# Algo backtest package

`equity_research.backtest` remains the public import and command path. The package
separates the deterministic replay by responsibility:

- `portfolio.py` builds replay state and composes the replay mixins.
- `execution.py` owns fills, positions, costs, and risk-state accounting.
- `signals.py` owns peer strategy, factor, and regime signal evaluation.
- `replay.py` owns the ordered bar event loop and report assembly.
- `reporting.py` owns benchmark, correlation, and portfolio summary methods.
- `data.py` loads, validates, and prepares stored bars and replay options.
- `config.py` owns replay constants and input validation; `models.py` owns state records.
- `cli.py` and `__main__.py` retain the fetch, run, and coverage commands.

The event loop remains the single behavior source for portfolio replay. Research
and live strategy code continue to share the same signal and risk components.
