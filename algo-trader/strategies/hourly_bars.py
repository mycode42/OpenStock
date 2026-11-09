"""Hourly bars derived from regular-session five-minute bars.

Buckets are anchored at the regular-session open (09:30 ET), so a session has
seven hourly bars: 09:30, 10:30, ..., 14:30 and 15:30 (the last one closes at
16:00). The aggregator is causal: a bucket is emitted as soon as its last
five-minute bar arrives, or when the next bucket starts. Live scans and the
replay use the same code, so the weekly strategies see the same bars in both.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
HOUR = timedelta(hours=1)
FIVE_MINUTES = timedelta(minutes=5)
HISTORY_LIMIT = 400


def parse_bar_time(value) -> datetime:
    """Timezone-aware timestamp of a bar's `t` field."""
    return _parse(value)


def _parse(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def bucket_bounds(stamp: datetime) -> tuple[datetime, datetime] | None:
    """Start and end of the hourly bucket holding `stamp`, or None outside the regular session."""
    local = stamp.astimezone(ET)
    open_at = datetime.combine(local.date(), SESSION_OPEN, tzinfo=ET)
    close_at = datetime.combine(local.date(), SESSION_CLOSE, tzinfo=ET)
    if not open_at <= local < close_at:
        return None
    start = open_at + ((local - open_at) // HOUR) * HOUR
    return start, min(start + HOUR, close_at)


def _summarize(bars: list[dict], start: datetime) -> dict:
    return {
        "t": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": float(bars[0]["o"]),
        "h": max(float(bar["h"]) for bar in bars),
        "l": min(float(bar["l"]) for bar in bars),
        "c": float(bars[-1]["c"]),
        "v": sum(float(bar.get("v", 0) or 0) for bar in bars),
        "n": len(bars),
    }


class HourlyAggregator:
    """Streams five-minute bars for one symbol and keeps the completed hourly bars."""

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self.limit = limit
        self.completed: list[dict] = []
        self._start: datetime | None = None
        self._end: datetime | None = None
        self._buffer: list[dict] = []

    def add(self, bar: dict) -> dict | None:
        """Consume one five-minute bar; return the hourly bar it completed, if any."""
        stamp = _parse(bar["t"])
        bounds = bucket_bounds(stamp)
        if bounds is None:
            return None
        start, end = bounds
        finished = None
        if self._start is not None and start != self._start:
            finished = self._finish()
        if self._start is None:
            self._start, self._end = start, end
        self._buffer.append(bar)
        if stamp + FIVE_MINUTES >= end:
            finished = self._finish() or finished
        return finished

    def _finish(self) -> dict | None:
        if not self._buffer:
            self._start = self._end = None
            return None
        bar = _summarize(self._buffer, self._start)
        self.completed.append(bar)
        if len(self.completed) > 2 * self.limit:
            del self.completed[:-self.limit]
        self._buffer = []
        self._start = self._end = None
        return bar

    def recent(self, count: int) -> list[dict]:
        """The last `count` completed hourly bars, oldest first."""
        return self.completed[-count:] if count > 0 else []

    def history(self) -> list[dict]:
        return list(self.completed[-self.limit:])


def hourly_bars(bars_5min: list[dict]) -> list[dict]:
    """All completed hourly bars for a chronological list of five-minute bars."""
    aggregator = HourlyAggregator(limit=max(len(bars_5min), 1))
    for bar in sorted(bars_5min, key=lambda item: _parse(item["t"])):
        aggregator.add(bar)
    return aggregator.history()
