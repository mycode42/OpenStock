"""``python -m research.options``: options research only.

Defaults live here so the command needs no flags: the portfolio config, eight
workers from that config, and one run ID per day so an interrupted run resumes.
Console output is also written to data/research/logs/options.log.
"""

import sys
from datetime import datetime

from equity_research.settings import OPTIONS_LOG_PATH
from options.research.automation import main as options_main
from research.logs import tee_to
from runtime.paths import ROOT

DEFAULT_CONFIG = ROOT / "options/research/config.portfolio.json"


def _with_defaults(argv: list[str]) -> list[str]:
    args = list(argv)
    if "--config" not in args:
        args += ["--config", str(DEFAULT_CONFIG)]
    if "--run-id" not in args:
        args += ["--run-id", datetime.now().strftime("options-%Y%m%d")]
    return args


def main() -> int:
    sys.argv = [sys.argv[0], *_with_defaults(sys.argv[1:])]
    with tee_to(OPTIONS_LOG_PATH):
        return options_main()


if __name__ == "__main__":
    raise SystemExit(main())
