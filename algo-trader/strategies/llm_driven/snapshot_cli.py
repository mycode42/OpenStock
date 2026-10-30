"""Print the newest AI signal snapshot as JSON: `python -m strategies.llm_driven.snapshot_cli`."""

from strategies.llm_driven.ai import main

if __name__ == "__main__":
    raise SystemExit(main())
