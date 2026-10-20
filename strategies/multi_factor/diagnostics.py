"""Small, dependency-free cross-sectional diagnostics."""

from __future__ import annotations

import math


def _average_ranks(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    ranks = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2
        for symbol, _ in ordered[index:end]:
            ranks[symbol] = rank
        index = end
    return ranks


def information_coefficient(scores: dict[str, float], forward_returns: dict[str, float]) -> float | None:
    """Spearman rank IC for one forward horizon; caller must provide a PIT-safe label."""
    shared = sorted(set(scores) & set(forward_returns))
    if len(shared) < 3:
        return None
    left = _average_ranks({symbol: scores[symbol] for symbol in shared})
    right = _average_ranks({symbol: forward_returns[symbol] for symbol in shared})
    left_mean = sum(left.values()) / len(shared)
    right_mean = sum(right.values()) / len(shared)
    covariance = sum((left[s] - left_mean) * (right[s] - right_mean) for s in shared)
    left_var = sum((left[s] - left_mean) ** 2 for s in shared)
    right_var = sum((right[s] - right_mean) ** 2 for s in shared)
    denominator = math.sqrt(left_var * right_var)
    return covariance / denominator if denominator else None


def turnover(previous_targets: set[str], current_targets: set[str]) -> float:
    """Target-set turnover as one minus overlap divided by the larger book."""
    if not previous_targets and not current_targets:
        return 0.0
    denominator = max(len(previous_targets), len(current_targets))
    return 1 - len(previous_targets & current_targets) / denominator
