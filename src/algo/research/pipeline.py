"""Public module for the scheduled, layered Algo research orchestrator.

The actual implementation lives in :mod:`src.algo.research.nightly`; this
module remains the stable import location used by the scheduled facade.
"""

from .nightly import build_parser, main, run_daily_research

__all__ = ["build_parser", "main", "run_daily_research"]


if __name__ == "__main__":
    raise SystemExit(main())
