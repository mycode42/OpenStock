"""Merge historical vendor chains with daily live Alpaca snapshots."""

from __future__ import annotations

from datetime import date, datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from runtime.paths import DATA_DIR


ET = ZoneInfo("America/New_York")
RESEARCH_ROOT = DATA_DIR / "options" / "research"


def _group_jsonl(path: Path):
    """Stream one highest-priority row per sorted New York session date."""
    if not path.is_file():
        return
    current_day = None
    current_row = None
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            day = _row_date(row)
            if current_day is None or day == current_day:
                if current_row is None or _row_priority(row) > _row_priority(current_row):
                    current_row = row
                current_day = day
                continue
            if day < current_day:
                raise ValueError(f"{path} is not ordered by New York session date")
            yield current_day, current_row
            current_day, current_row = day, row
    if current_row is not None:
        yield current_day, current_row


def _last_jsonl_row(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell() - 1
        while position >= 0:
            stream.seek(position)
            if stream.read(1) != b"\n":
                break
            position -= 1
        end = position + 1
        while position >= 0:
            stream.seek(position)
            if stream.read(1) == b"\n":
                break
            position -= 1
        stream.seek(position + 1)
        row = json.loads(stream.read(end - position - 1))
    if not isinstance(row, dict):
        raise ValueError(f"{path} last row is not a JSON object")
    return row


def _row_date(row: dict) -> date:
    stamp = datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("research snapshot as_of must include a timezone")
    return stamp.astimezone(ET).date()


def _row_priority(row: dict) -> tuple[int, str]:
    feed = str(row.get("feed", "")).lower()
    source_rank = 2 if feed.startswith("alpaca_") else 1 if feed.startswith("thetadata_") else 0
    stamp = datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00"))
    return source_rank, stamp.astimezone(timezone.utc).isoformat()


def merge_symbol_dataset(symbol: str) -> dict:
    """Deprecated JSONL merger; history collectors now write to SQLite directly."""
    normalized = symbol.strip().upper()
    if not normalized or not normalized.replace(".", "").isalnum():
        raise ValueError("underlying must be a ticker symbol")
    from options.research.history_store import DEFAULT_DATABASE

    return {
        "underlying": normalized,
        "dataset_path": str(DEFAULT_DATABASE),
        "skipped": True,
        "reason": "SQLite is the canonical options history store; JSONL merging is disabled",
    }


def session_labels(closes: dict[date, float]) -> dict[date, tuple[str, str]]:
    """Apply the same trailing trend/volatility labels used in Theta compiles."""
    ordered = sorted(
        (day, float(close)) for day, close in closes.items()
        if math.isfinite(float(close)) and float(close) > 0
    )
    log_returns: list[float] = []
    realized: list[float | None] = []
    for index, (_, close) in enumerate(ordered):
        if index:
            previous = ordered[index - 1][1]
            log_returns.append(math.log(close / previous))
        else:
            log_returns.append(0.0)
        sample = log_returns[max(1, index - 19):index + 1]
        if len(sample) >= 10:
            mean = sum(sample) / len(sample)
            variance = sum((value - mean) ** 2 for value in sample) / max(1, len(sample) - 1)
            realized.append(math.sqrt(max(0.0, variance) * 252.0))
        else:
            realized.append(None)

    labels = {}
    for index, (day, close) in enumerate(ordered):
        values = [value for _, value in ordered[max(0, index - 49):index + 1]]
        mean_50 = sum(values) / len(values)
        long_values = [value for _, value in ordered[max(0, index - 199):index + 1]]
        mean_200 = sum(long_values) / len(long_values)
        if len(long_values) >= 200 and close > mean_50 > mean_200:
            regime = "trending_up"
        elif len(long_values) >= 200 and close < mean_50 < mean_200:
            regime = "trending_down"
        else:
            regime = "range_bound"

        current_vol = realized[index]
        prior_vols = [value for value in realized[max(0, index - 251):index + 1] if value is not None]
        if current_vol is not None and len(prior_vols) >= 20:
            threshold = sorted(prior_vols)[max(0, math.ceil(len(prior_vols) * 0.75) - 1)]
            vol_state = "volatile" if current_vol >= threshold else "normal"
        else:
            vol_state = "normal"
        labels[day] = (regime, vol_state)
    return labels


def main() -> int:
    """Rebuild combined JSONL files after deployment or a historical compile."""
    import argparse
    import json
    from options.research.universe import history_underlyings

    parser = argparse.ArgumentParser(description="Merge Theta history and Alpaca daily option archives")
    parser.add_argument("--underlying", action="append", help="ticker to merge; repeatable")
    args = parser.parse_args()
    symbols = args.underlying or history_underlyings()
    print(json.dumps([merge_symbol_dataset(symbol) for symbol in symbols], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
