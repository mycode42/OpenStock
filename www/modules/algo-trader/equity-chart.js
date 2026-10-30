import { escapeHtml } from '../escape.js';
import { numberOrNull, money, dateTime } from './format.js';
import { sessionWindow } from '../session-window.js';

export const RANGES = ['1D', '1W', '1M', '3M', 'ALL'];

const DAY_MS = 86400000;
const HOUR_MS = 3600000;
const RANGE_SPAN_MS = { '1W': 7 * DAY_MS, '1M': 31 * DAY_MS, '3M': 92 * DAY_MS };
const WIDTH = 800;
const HEIGHT = 270;
const LEFT = 74;
const RIGHT = 784;
const TOP = 20;
const BOTTOM = 220;
const localDay = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const localHour = new Intl.DateTimeFormat(undefined, { hour: 'numeric' });
const marketDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' });

// The viewer's time zone, shown with the chart so the axis scale is readable.
export function localTimeZone() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time';
}

function snapshotTime(item) {
    return new Date(item.timestamp || item.date).getTime();
}

function validPoints(history) {
    return Array.isArray(history) ? history.filter(item => numberOrNull(item.equity) !== null && Number.isFinite(snapshotTime(item))) : [];
}

// The week and longer views chart only the 04:00–20:00 New York session: Alpaca has no reliable overnight marks,
// and equity and options share this cut so both histories cover the same hours.
function sessionPoints(history) {
    return validPoints(history).filter(item => {
        const time = snapshotTime(item);
        const session = sessionWindow(new Date(time));
        return time >= session.start.getTime() && time <= session.end.getTime();
    });
}

function fraction(time, start, span) {
    return Math.min(1, Math.max(0, (time - start) / span));
}

// Hour labels such as "8 AM", stepped back from the right edge in equal two-hour intervals, so every label is the same distance apart.
// The edge carries the hour of the most recent snapshot.
function dayTicks(start, end) {
    const span = end - start;
    const ticks = [{ fraction: 1, label: localHour.format(end) }];
    for (let time = end - 2 * HOUR_MS; time > start; time -= 2 * HOUR_MS) {
        ticks.push({ fraction: (time - start) / span, label: localHour.format(time) });
    }
    return ticks;
}

// 1D: the last 24 hours, ending at the most recent snapshot on the right edge.
function dayLayout(points) {
    const end = snapshotTime(points[points.length - 1]);
    const start = end - DAY_MS;
    const items = points
        .filter(item => { const time = snapshotTime(item); return time >= start && time <= end; })
        .map(point => ({ point, time: snapshotTime(point), fraction: fraction(snapshotTime(point), start, DAY_MS) }));
    return { items, ticks: dayTicks(start, end) };
}

// One block per trading day. Each block spans that day's first to last snapshot, so the blocks join with no empty time between them.
function weekLayout(points, end) {
    const visible = points.filter(item => snapshotTime(item) >= end - RANGE_SPAN_MS['1W']);
    const bounds = new Map();
    for (const point of visible) {
        const time = snapshotTime(point);
        const key = marketDay.format(new Date(time));
        const current = bounds.get(key);
        bounds.set(key, current
            ? { first: Math.min(current.first, time), last: Math.max(current.last, time) }
            : { first: time, last: time });
    }
    const days = [...bounds.keys()].sort();
    const items = visible.map(point => {
        const time = snapshotTime(point);
        const key = marketDay.format(new Date(time));
        const { first, last } = bounds.get(key);
        const share = last > first ? (time - first) / (last - first) : 0.5;
        return { point, time, fraction: (days.indexOf(key) + share) / days.length };
    });
    const ticks = days.map((key, index) => ({ fraction: (index + 0.5) / days.length, label: localDay.format(bounds.get(key).first) }));
    return { items, ticks };
}

// The last point of each trading day stands for that day: its close snapshot when one exists, otherwise its latest point.
function closeLayout(points, range, end) {
    const start = range === 'ALL' ? snapshotTime(points[0]) : end - RANGE_SPAN_MS[range];
    const byDay = new Map();
    for (const point of points) {
        const time = snapshotTime(point);
        if (time < start) continue;
        const key = marketDay.format(new Date(time));
        const current = byDay.get(key);
        const pointIsClose = point.snapshot_kind === 'close';
        const currentIsClose = current?.snapshot_kind === 'close';
        const replace = !current || (pointIsClose !== currentIsClose ? pointIsClose : time > snapshotTime(current));
        if (replace) byDay.set(key, point);
    }
    const days = [...byDay.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([, point]) => point);
    const count = days.length;
    const items = days.map((point, index) => ({ point, time: snapshotTime(point), fraction: count > 1 ? index / (count - 1) : 0 }));
    const ticks = [0, 0.25, 0.5, 0.75, 1].map(share => {
        const index = Math.round((count - 1) * share);
        return { fraction: share, label: count ? localDay.format(new Date(snapshotTime(days[index]))) : '' };
    });
    return { items, ticks };
}

function layoutFor(range, points) {
    const end = snapshotTime(points[points.length - 1]);
    if (range === '1D') return dayLayout(points);
    if (range === '1W') return weekLayout(points, end);
    return closeLayout(points, range, end);
}

export function renderChart(history, range, charts) {
    const points = range === '1D' ? validPoints(history) : sessionPoints(history);
    const empty = '<div class="algo-chart-empty">Equity history will appear after more account snapshots are recorded.</div>';
    if (points.length < 2) return empty;
    const { items, ticks } = layoutFor(range, points);
    if (items.length < 2) return empty;
    const values = items.map(item => Number(item.point.equity));
    const low = Math.min(...values);
    const high = Math.max(...values);
    const padding = Math.max((high - low) * 0.16, Math.max(Math.abs(high), 1) * 0.0005);
    const min = low - padding;
    const max = high + padding;
    const coords = items.map((item, index) => ({
        x: LEFT + (RIGHT - LEFT) * item.fraction,
        y: BOTTOM - ((values[index] - min) / (max - min || 1)) * (BOTTOM - TOP),
        value: values[index],
        time: item.time,
        point: item.point,
    }));
    // One continuous line across the whole range; the fill sits under it.
    const line = coords.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
    const area = `${line} L ${coords[coords.length - 1].x.toFixed(1)} ${BOTTOM} L ${coords[0].x.toFixed(1)} ${BOTTOM} Z`;
    const linePoints = coords.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
    const grid = [0, 1, 2, 3].map(index => {
        const y = TOP + (BOTTOM - TOP) * (index / 3);
        const value = max - (max - min) * (index / 3);
        return `<line class="algo-chart-grid" x1="${LEFT}" y1="${y}" x2="${RIGHT}" y2="${y}"/><text class="algo-chart-y-label" x="${LEFT - 12}" y="${y + 4}" text-anchor="end">${escapeHtml(money(value, 'USD', 0))}</text>`;
    }).join('');
    const labels = ticks.map(({ fraction: share, label }) => {
        const anchor = share === 0 ? 'start' : share === 1 ? 'end' : 'middle';
        return `<text class="algo-chart-x-label" x="${(LEFT + (RIGHT - LEFT) * share).toFixed(1)}" y="252" text-anchor="${anchor}">${escapeHtml(label)}</text>`;
    }).join('');
    const hoverPoints = coords.map(point => ({
        x: point.x,
        y: point.y,
        primary: point.point.timestamp ? dateTime(point.point.timestamp) : dateTime(point.point.date),
        secondary: '',
        price: money(point.value),
    }));
    const chartGrid = `<defs><linearGradient id="algo-chart-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="var(--os-accent, #c8ff00)" stop-opacity=".08"/><stop offset="100%" stop-color="var(--os-accent, #c8ff00)" stop-opacity="0"/></linearGradient></defs><path class="algo-chart-area" d="${area}"/>${grid}${labels}`;
    return charts.interactiveChartSvg({
        className: 'algo-performance-chart',
        width: WIDTH,
        height: HEIGHT,
        grid: chartGrid,
        lineClass: 'algo-chart-line',
        lineSegments: [linePoints],
        smooth: range === '1W',
        hoverPoints,
        ariaLabel: `Algo paper account equity history for ${range}`,
    });
}

export function sparklinePath(history) {
    const values = (history || []).slice(-14).map(point => Number(point.equity)).filter(Number.isFinite);
    if (values.length < 2) return 'M1 27 L119 27';
    const min = Math.min(...values);
    const max = Math.max(...values);
    return values.map((value, index) => {
        const x = 1 + 118 * index / (values.length - 1);
        const y = 28 - ((value - min) / (max - min || 1)) * 24;
        return `${index ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(' ');
}
