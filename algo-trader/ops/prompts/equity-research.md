Pipeline steps
1. Run exactly one equity AutoResearch cycle (options research is a separate job):
cd {ROOT} && /usr/bin/nice -n 2 poetry run python -m research.equity
Set the OpenClaw `exec` tool call's `timeoutSeconds` to `43200` (12 hours) for this command. Do not use a shorter command timeout.
The command runs the equity AutoResearch pipeline only. Wait for completion. Do not loop or launch another cycle. If it exits nonzero, report the equity failure clearly and do not present an older equity latest.json as this run.
2. Read {ROOT}/data/research/algo-auto/latest.json. Report the equity sections only.

Keep the Telegram report under 2,000 characters. Use the equity report's fitness_timeline first; read referenced verification artifacts only when needed to explain dates or diagnostics. Never scan the artifact directory or report equity strategies absent from orchestration.strategies.

Equity section:
- Include run id/date/status, cache reuse, Plus SIP/feed, stored coverage, latest session age, refresh result, and the 2024-onward scored range. Distinguish indicator warm-up from scored OOS dates.
- For current strategies, summarize discovery window/attempts and candidate fitness delta versus its training baseline, then verification folds/date range, stitched OOS fitness, return, Sharpe, decay, stability, gate, and errors. Training improvement is not an OOS pass.
- Compare with fitness_timeline.comparison_to_previous. If not comparable or no prior score exists, explain why. Treat score changes across the expanding OOS range as trend changes, not a controlled A/B result. Use recent_runs for the timeline.
- Explain what improved or failed using these metrics. Describe the next search from fitness_timeline.next_run_plan; say whether an eligible recent candidate can seed research and that it must pass rolling OOS verification. Keep the baseline when no candidate beats it after costs.
- Say portfolio replay combines the same verified OOS folds using each fold's selected profile. Include compounded fold return and fold counts only when present; otherwise say incomplete verification skipped replay.
