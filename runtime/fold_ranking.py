"""Shared fold-level ranking for equity and options promotion.

A candidate ranks above an incumbent when, on the same out-of-sample folds, it has
a higher lower-quartile fitness, then a higher median fitness, then more positive
folds. Missing or unfinished evidence never wins.
"""

from __future__ import annotations

import statistics

RANK_KEYS = ("lower_quartile_fitness", "median_fitness", "positive_fold_fraction")


def fold_summary(scores: list[float]) -> dict[str, float | int | None]:
    """Summarize one candidate's per-fold fitness on the shared folds."""
    ordered = sorted(float(score) for score in scores)
    if not ordered:
        return {"fold_count": 0, "lower_quartile_fitness": None, "median_fitness": None, "positive_fold_fraction": None}
    return {
        "fold_count": len(ordered),
        "lower_quartile_fitness": ordered[int((len(ordered) - 1) * 0.25)],
        "median_fitness": statistics.median(ordered),
        "positive_fold_fraction": sum(score > 0 for score in ordered) / len(ordered),
    }


def beats(candidate: dict | None, incumbent: dict | None) -> tuple[bool, str]:
    """Return whether the candidate ranks above the incumbent, with the reason."""
    if not incumbent or incumbent.get("status", "passed") != "passed":
        return False, "incumbent has no passed evidence on these folds"
    if not candidate:
        return False, "candidate has no evidence on these folds"
    cand_rank = tuple(candidate.get(key) for key in RANK_KEYS)
    inc_rank = tuple(incumbent.get(key) for key in RANK_KEYS)
    if any(value is None for value in (*cand_rank, *inc_rank)):
        return False, "missing fitness evidence"
    if cand_rank > inc_rank:
        return True, "ranks above the incumbent on the same folds"
    return False, "does not rank above the incumbent on the same folds"
