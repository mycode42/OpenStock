Pipeline steps
1. Run exactly one equity simulation (options research is a separate job):
cd {ROOT} && /usr/bin/nice -n 2 poetry run python -m research.equity
The command replays the live settings over the full stored history. Wait for completion. Do not loop or launch another run. If it exits nonzero, report the failure clearly and do not present an older result as this run.
2. Use the compact equity simulation JSON supplied below (schema swarm_algo_equity_simulation_v1). It is generated from {ROOT}/data/research/simulation/latest.json; do not read other files.

Keep the Telegram report under 2,000 characters. Use the heading "Equity simulation" with the run date and the window.
- One line for the net return, the buy-and-hold benchmark return, the excess return and the maximum drawdown. Percentages are as given in the JSON.
- One line for the closed trades, entries and open positions, and the universe size.
- One short line per strategy with closed trades: its P/L in USD, win rate and profit factor. Only strategies listed in by_strategy.
- If live_comparison is present, one line comparing its net return and trade count with the chosen settings.

Copy every number exactly from the JSON. Do not compute new totals, infer causes, or mention discovery, promotions, verification or research-only disclaimers.
