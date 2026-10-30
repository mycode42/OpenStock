Options research only. Equity AutoResearch runs in its own job.

1. Run exactly: `cd {ROOT} && /usr/bin/nice -n 2 poetry run python -m research.options`. Set the exec timeoutSeconds to 28800 (8 hours) and do not use a shorter timeout. Wait for completion. Do not loop or start another run.

2. Read {ROOT}/data/options/research/automation/latest.md. Report per sleeve: status, data availability, and sleeves that did not run. If a sleeve is waiting_for_data, give its configured dataset path and do not describe it as a strategy failure. For each completed sleeve include the rolling OOS comparison table, including the "Return improvement" column exactly as written in the file, and the champion decision (promoted or not, and why).

Options research makes no order changes and places no orders. Do not touch equity.
