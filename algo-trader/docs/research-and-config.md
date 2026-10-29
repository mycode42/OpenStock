# Research, promotion, and configuration

Part of the [Algo Trader README](../README.md).

## Research and promotion

Research runs nightly. Parameter profiles that pass rolling walk-forward and forward-simulation gates go live automatically, without human confirmation; a detected drift warning pauses promotion. Every promotion is recorded in `data/research/algo-auto/promoted-profile.json`.

## Configuration files

| Setting | File |
| --- | --- |
| Equity limits and strategy switches | `portfolio/config.py`, `data/algo-strategy-config.json` |
| Candidate pool | `options/research/universe.json` |
| Selector rules and live gates | `options/research/eligibility.default.json` |
| Options paper settings | `options/config.default.json` (copy to `data/options/config.json`) |
| Options research windows, costs, and grids | `options/research/config.default.json` |

Equity strategy settings have two layers. `portfolio/algo-strategy-config.default.json` holds the shipped defaults. `data/algo-strategy-config.json` holds local overrides, merged on top by `portfolio/config.py`; the file is git-ignored, so each machine keeps its own. Per-strategy `max_entries_per_day` and `max_positions` are portfolio limits, read by the portfolio layer and not by signal logic. `cooldown_days` is retired and has no effect; the cooldown that applies is `cooldown_minutes`.

### One rule set

Both layers are checked against one rule set, `portfolio/algo-strategy-rules.json`. The live engine applies it when it loads the settings (`portfolio/settings_rules.py`), and OpenStock's settings editor applies the same file (`server/algo-trader/settings-rules.js`). Settings that break a rule are rejected, whether they come from the editor or a hand edit. Shared test cases in `tests/portfolio/algo-strategy-rule-cases.json` run in both suites. Add a rule to the JSON file, not to either evaluator.

## Further reading

- [Options research](options_research.md) and [options trading](options_trading.md)
- [Equity research](adaptive_research.md)
- [LLM-driven strategy](llm_driven_strategy.md)
- [Options long volatility](options-long-volatility.md)
