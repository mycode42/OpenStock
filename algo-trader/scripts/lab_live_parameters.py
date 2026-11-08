"""Read the live (effective) values of the strategy parameters the research lab can profile.

Takes one JSON argument, {strategy: [parameter, ...]}, and prints {strategy: {parameter: value}}.
Read-only: it reports what the registry runs with now, including any promoted profile, and writes nothing.
The research lab uses it for the "Live now" column of strategies whose live values are not in the settings file.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategies.registry import ALGO_DEFAULT_PARAMETERS  # noqa: E402


def main() -> None:
    requested = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    values = {}
    for strategy, keys in requested.items():
        effective = ALGO_DEFAULT_PARAMETERS.get(strategy)
        if effective is None:
            continue
        values[strategy] = {key: effective[key] for key in keys if key in effective}
    print(json.dumps(values))


if __name__ == "__main__":
    main()
