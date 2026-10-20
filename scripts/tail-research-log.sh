#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
tail -f data/research/algo-auto/logs/auto-research.log
