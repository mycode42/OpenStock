import { escapeHtml } from '../escape.js';
import { numberOrNull, money, dateTime, timeOnly, dateOnly } from './format.js';
import { sessionWindow, sessionFraction, sessionTicks, formatSessionTime } from '../session-window.js';

export const RANGES = ['1D', '1W', '1M', '3M', 'ALL'];

// 1D shows the session of the latest snapshot, so it has data before today's first snapshot arrives.
function latestSnapshotTime(history) {
    const points = Array.isArray(history) ? history.filter(item => numberOrNull(item.equity) !== null) : [];
    const last = points[points.length - 1];
    return last ? new Date(last.timestamp || last.date) : new Date();
}

function rangeHistory(history, range) {
    const points = Array.isArray(history) ? history.filter(item => numberOrNull(item.equity) !== null) : [];
    if (range === 'ALL' || points.length <= 1) return points;
    if (range === '1D') {
        const session = sessionWindow(latestSnapshotTime(history));
        return points.filter(item => { const time = new Date(item.timestamp || item.date).getTime(); return time >= session.start.getTime() && time <= session.end.getTime(); });
    }
    const lookback = range === '1D' ? 1 : range === '1W' ? 7 : range === '1M' ? 31 : 92;
    const latestDate = new Date(points[points.length - 1].timestamp || points[points.length - 1].date);
    if (Number.isNaN(latestDate.getTime())) return points;
    const cutoff = latestDate.getTime() - lookback * 86400000;
    const filtered = points.filter(item => new Date(item.timestamp || item.date).getTime() >= cutoff);
    return filtered.length > 1 || range === '1D' ? filtered : points;
}

export function renderChart(history, range, charts) {
    const points = rangeHistory(history, range);
    if (points.length < 2) return '<div class="algo-chart-empty">Equity history will appear after more account snapshots are recorded.</div>';
    const width = 800;
    const height = 270;
    const left = 74;
    const right = 784;
    const top = 20;
    const bottom = 220;
    const values = points.map(point => Number(point.equity));
    const low = Math.min(...values);
    const high = Math.max(...values);
    const padding = Math.max((high - low) * 0.16, Math.max(Math.abs(high), 1) * 0.0005);
    const min = low - padding;
    const max = high + padding;
    const session = range === '1D' ? sessionWindow(latestSnapshotTime(history)) : null;
    const xFor = (point, index) => left + (right - left) * (session ? sessionFraction(point.timestamp, session) : index / (values.length - 1));
    const coords = values.map((value, index) => ({
        x: xFor(points[index], index),
        y: bottom - ((value - min) / (max - min || 1)) * (bottom - top),
        value,
    }));
    const line = coords.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
    // The fill sits under the line only, so a partial session does not paint empty space.
    const area = `${line} L ${coords[coords.length - 1].x} ${bottom} L ${coords[0].x} ${bottom} Z`;
    const grid = [0, 1, 2, 3].map(index => {
        const y = top + (bottom - top) * (index / 3);
        const value = max - (max - min) * (index / 3);
        return `<line class="algo-chart-grid" x1="${left}" y1="${y}" x2="${right}" y2="${y}"/><text class="algo-chart-y-label" x="${left - 12}" y="${y + 4}" text-anchor="end">${escapeHtml(money(value, 'USD', 0))}</text>`;
    }).join('');
    const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
    const sessionLabels = session ? sessionTicks(session).map(({ fraction, time }) => {
        const anchor = fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle';
        return `<text class="algo-chart-x-label" x="${(left + (right - left) * fraction).toFixed(1)}" y="252" text-anchor="${anchor}">${escapeHtml(formatSessionTime(time.toISOString()))}</text>`;
    }).join('') : '';
    const labels = session ? sessionLabels : labelIndexes.map(index => {
        const anchor = index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle';
        const value = points[index].timestamp || points[index].date;
        const label = range === '1D' && points[index].timestamp ? timeOnly(value) : dateOnly(points[index].date || points[index].timestamp);
        return `<text class="algo-chart-x-label" x="${coords[index].x}" y="252" text-anchor="${anchor}">${escapeHtml(label)}</text>`;
    }).join('');
    const hoverPoints = coords.map((point, index) => ({
        x: point.x,
        y: point.y,
        primary: points[index].timestamp ? dateTime(points[index].timestamp) : dateOnly(points[index].date),
        secondary: '',
        price: money(point.value),
    }));
    const chartGrid = `<defs><linearGradient id="algo-chart-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="var(--os-accent, #c8ff00)" stop-opacity=".08"/><stop offset="100%" stop-color="var(--os-accent, #c8ff00)" stop-opacity="0"/></linearGradient></defs><path class="algo-chart-area" d="${area}"/>${grid}${labels}`;
    return charts.interactiveChartSvg({
        className: 'algo-performance-chart',
        width,
        height,
        grid: chartGrid,
        lineClass: 'algo-chart-line',
        linePoints: coords.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '),
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
