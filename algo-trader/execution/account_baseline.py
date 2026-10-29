"""One-time broker-verified weekly loss baseline for a new paper account."""

import argparse
import json

from execution.risk import capture_account_inception_baseline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("algo",), default="algo")
    args = parser.parse_args()
    baseline = capture_account_inception_baseline(args.mode)
    print(json.dumps({
        "mode": args.mode,
        "week_start": baseline["week_start"],
        "opening_equity": baseline["equity"],
        "status": "verified",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
