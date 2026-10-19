#!/usr/bin/env python3
"""Compatibility entry point for the modular execution package."""

import sys

from src.execution import runner


if __name__ == "__main__":
    raise SystemExit(runner.main())

# Keep imports such as ``from execute_trades import place_order`` compatible.
# Returning the implementation module also preserves existing monkeypatch
# points used by integrations and the legacy executor tests.
sys.modules[__name__] = runner
