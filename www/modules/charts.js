import { escapeHtml } from './escape.js';
import { money, parseOpenStockDate, formatLocalDateTime, formatChartDate, chartRangeLabel } from './formatting.js';

export function createCharts({ request, historyCache }) {
function chartRangeControls(activeRange) {
    return `<div class="history-ranges" role="group" aria-label="Chart range">${['1d', '1mo', '1y', '5y'].map(range => `<button class="history-range ${range === activeRange ? 'active' : ''}" type="button" data-history-range="${range}">${range === '1mo' ? '1M' : range.toUpperCase()}</button>`).join('')}</div>`;
}

function interactiveChartSvg({ className, width, height, grid, lineClass = '', linePoints, lineSegments = null, hoverPoints, ariaLabel }) {
    const encodedPoints = escapeHtml(JSON.stringify(hoverPoints));
    const segments = Array.isArray(lineSegments) ? lineSegments : [linePoints];
    const lines = segments.filter(Boolean).map(points => `<polyline class="${lineClass}" points="${points}"/>`).join('');
    return `<svg class="${className} interactive-line-chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" tabindex="0" role="img" aria-label="${escapeHtml(ariaLabel)}" data-chart-label="${escapeHtml(ariaLabel)}" data-chart-points="${encodedPoints}">${grid}${lines}<g class="chart-hover" hidden><line class="chart-hover-guide" y1="0" y2="${height}"/><circle class="chart-hover-dot" r="4"/><text class="chart-hover-text"><tspan class="chart-hover-primary"/><tspan class="chart-hover-secondary"/><tspan class="chart-hover-tertiary"/></text></g><rect class="chart-hit-area" x="0" y="0" width="${width}" height="${height}"/></svg>`;
}

function attachInteractiveLineCharts(root) {
    root.querySelectorAll('.interactive-line-chart').forEach(chart => {
        if (chart.dataset.chartBound === '1') return;
        chart.dataset.chartBound = '1';
        let points;
        try { points = JSON.parse(chart.dataset.chartPoints || '[]'); } catch { points = []; }
        if (!points.length) return;
        const hover = chart.querySelector('.chart-hover');
        const guide = chart.querySelector('.chart-hover-guide');
        const dot = chart.querySelector('.chart-hover-dot');
        const text = chart.querySelector('.chart-hover-text');
        const primary = chart.querySelector('.chart-hover-primary');
        const secondary = chart.querySelector('.chart-hover-secondary');
        const tertiary = chart.querySelector('.chart-hover-tertiary');
        const hitArea = chart.querySelector('.chart-hit-area');
        let activeIndex = points.length - 1;
        let pinned = false;

        const show = index => {
            activeIndex = Math.max(0, Math.min(points.length - 1, index));
            const point = points[activeIndex];
            const width = chart.viewBox.baseVal.width;
            const height = chart.viewBox.baseVal.height;
            const bounds = chart.getBoundingClientRect();
            const inverseScaleX = bounds.width ? width / bounds.width : 1;
            const inverseScaleY = bounds.height ? height / bounds.height : 1;
            const compensateStretch = (x, y) => `translate(${x} ${y}) scale(${inverseScaleX} ${inverseScaleY}) translate(${-x} ${-y})`;
            guide.setAttribute('x1', point.x); guide.setAttribute('x2', point.x);
            dot.setAttribute('cx', point.x); dot.setAttribute('cy', point.y);
            dot.setAttribute('transform', compensateStretch(point.x, point.y));
            const anchor = point.x < width * 0.22 ? 'start' : point.x > width * 0.78 ? 'end' : 'middle';
            const lineGap = point.lineGap || 9;
            const lineCount = point.tertiary ? 3 : 2;
            const blockHeight = (lineCount - 1) * lineGap;
            const labelY = point.y > blockHeight + 18 ? point.y - blockHeight - 10 : point.y + 16;
            text.setAttribute('x', point.x); text.setAttribute('y', labelY); text.setAttribute('text-anchor', anchor);
            text.setAttribute('transform', compensateStretch(point.x, labelY));
            primary.setAttribute('x', point.x);
            secondary.setAttribute('x', point.x); secondary.setAttribute('dy', lineGap);
            tertiary.setAttribute('x', point.x); tertiary.setAttribute('dy', lineGap);
            primary.textContent = point.primary;
            secondary.textContent = point.secondary;
            tertiary.textContent = point.tertiary || '';
            tertiary.toggleAttribute('hidden', !point.tertiary);
            hover.removeAttribute('hidden');
            chart.setAttribute('aria-label', `${chart.dataset.chartLabel}. ${point.primary}. ${point.secondary}${point.tertiary ? `. ${point.tertiary}` : ''}`);
        };
        const hide = () => { if (!pinned) { hover.setAttribute('hidden', ''); chart.setAttribute('aria-label', chart.dataset.chartLabel); } };
        const nearest = clientX => {
            const bounds = chart.getBoundingClientRect();
            const x = (clientX - bounds.left) / bounds.width * chart.viewBox.baseVal.width;
            let best = 0;
            for (let index = 1; index < points.length; index += 1) if (Math.abs(points[index].x - x) < Math.abs(points[best].x - x)) best = index;
            return best;
        };

        hitArea.addEventListener('pointermove', event => show(nearest(event.clientX)));
        hitArea.addEventListener('pointerleave', hide);
        hitArea.addEventListener('pointerdown', event => { pinned = true; chart.focus(); show(nearest(event.clientX)); });
        chart.addEventListener('focus', () => show(activeIndex));
        chart.addEventListener('blur', () => { pinned = false; hide(); });
        chart.addEventListener('keydown', event => {
            if (event.key === 'ArrowLeft') activeIndex -= 1;
            else if (event.key === 'ArrowRight') activeIndex += 1;
            else if (event.key === 'Home') activeIndex = 0;
            else if (event.key === 'End') activeIndex = points.length - 1;
            else if (event.key === 'Escape') { pinned = false; hide(); return; }
            else return;
            event.preventDefault(); pinned = true; show(activeIndex);
        });
    });
}

function renderHistoryChart(points, range, maxLiveGapSeconds = 0) {
    const clean = points.filter(point => Number.isFinite(Number(point.price_eur)) && parseOpenStockDate(point.recorded_at));
    if (clean.length < 2) return '<div class="history-empty">Collecting native EUR price history…</div>';
    const values = clean.map(point => Number(point.price_eur));
    const min = Math.min(...values), max = Math.max(...values);
    const padding = Math.max((max - min) * 0.08, max * 0.002, 0.01);
    const low = min - padding, high = max + padding, width = 264, height = 82;
    const times = clean.map(point => parseOpenStockDate(point.recorded_at).getTime());
    const firstTime = times[0], timeRange = times[times.length - 1] - firstTime || 1;
    const coordinates = clean.map((point, index) => ({ point, x: (times[index] - firstTime) / timeRange * width, y: height - ((Number(point.price_eur) - low) / (high - low || 1) * height) }));
    const liveSources = new Set(['trade_republic_order_price', 'manual_refresh_trade_republic_order_price']);
    const maxLiveGapMs = Math.max(0, Number(maxLiveGapSeconds) || 0) * 1000;
    const coordinateSegments = [[]];
    coordinates.forEach((coordinate, index) => {
        if (index > 0 && maxLiveGapMs && times[index] - times[index - 1] > maxLiveGapMs && (liveSources.has(clean[index - 1].source) || liveSources.has(clean[index].source))) coordinateSegments.push([]);
        coordinateSegments[coordinateSegments.length - 1].push(coordinate);
    });
    const lineSegments = coordinateSegments.filter(segment => segment.length > 1).map(segment => segment.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' '));
    const hoverPoints = coordinates.map(({ point, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: `€${money(point.price_eur)}`, secondary: formatLocalDateTime(point.recorded_at) }));
    const first = clean[0], last = clean[clean.length - 1];
    const change = (Number(last.price_eur) / Number(first.price_eur) - 1) * 100;
    const changeClass = change >= 0 ? 'up' : 'down';
    const ariaLabel = `${chartRangeLabel(range)} native EUR price chart from ${formatChartDate(first.recorded_at)} to ${formatChartDate(last.recorded_at)}`;
    const chart = interactiveChartSvg({ className: 'history-chart', width, height, grid: `<path class="history-grid" d="M0 20H${width} M0 41H${width} M0 62H${width}"/>`, lineClass: `history-line ${changeClass}`, lineSegments, hoverPoints, ariaLabel });
    return `<div class="history-meta"><span>€${money(min)} – €${money(max)}</span><span class="history-change ${changeClass}">${change >= 0 ? '+' : ''}${change.toFixed(2)}%</span></div>${chart}<div class="history-dates"><span>${formatChartDate(first.recorded_at)}</span><span>${formatChartDate(last.recorded_at)}</span></div>`;
}

function renderHistoryPanel(panel, history, range) {
    panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · EUR <span>${history.points.length} native samples</span></div>${chartRangeControls(range)}</div>${renderHistoryChart(history.points, range, history.max_live_gap_seconds)}`;
    attachInteractiveLineCharts(panel);
}

async function loadCardHistory(card, range = '1mo') {
    const stockId = Number(card.dataset.stockId);
    const panel = card.querySelector('.history-popover');
    if (!Number.isInteger(stockId) || !panel) return;
    const key = `${stockId}:${range}`;
    panel.dataset.requestedRange = range;
    panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · EUR</div>${chartRangeControls(range)}</div><div class="history-loading">Loading native EUR history…</div>`;
    let history = historyCache.get(key);
    if (!history) {
        history = await request(`/stocks/${stockId}/history?range=${range}`);
        if (history) historyCache.set(key, history);
    }
    if (panel.dataset.requestedRange !== range) return;
    if (!history) {
        panel.innerHTML = '<div class="history-empty">History is unavailable.</div>';
        return;
    }
    renderHistoryPanel(panel, history, range);
}

function attachChartHovers(container) {
    container.querySelectorAll('.stock-card[data-stock-id]').forEach(card => {
        card.addEventListener('mouseenter', () => loadCardHistory(card, '1mo'), { once: true });
        card.addEventListener('focusin', () => loadCardHistory(card, '1mo'), { once: true });
        card.addEventListener('touchstart', () => {
            const panel = card.querySelector('.history-popover');
            panel.classList.add('is-open');
            loadCardHistory(card, panel.dataset.requestedRange || '1mo');
        }, { passive: true, once: true });
        card.querySelector('.history-popover').addEventListener('click', event => {
            const button = event.target.closest('[data-history-range]');
            if (!button) return;
            event.preventDefault();
            event.stopPropagation();
            loadCardHistory(card, button.dataset.historyRange);
        });
    });
}

    return { attachInteractiveLineCharts, interactiveChartSvg, renderHistoryChart, renderHistoryPanel, loadCardHistory, attachChartHovers };
}
