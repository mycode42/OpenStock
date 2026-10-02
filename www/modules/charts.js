import { escapeHtml } from './escape.js';
import { money, parseOpenStockDate, formatLocalDateTime, formatChartDate, formatChartTime, chartRangeLabel } from './formatting.js';

export function createCharts({ request, historyCache }) {
function chartRangeControls(activeRange) {
    return `<div class="history-ranges" role="group" aria-label="Chart range">${['1d', '1w', '1mo', '1y', '5y'].map(range => `<button class="history-range ${range === activeRange ? 'active' : ''}" type="button" data-history-range="${range}">${range === '1mo' ? '1M' : range.toUpperCase()}</button>`).join('')}</div>`;
}

function smoothCoordinatePath(points) {
    const coordinates = String(points || '').trim().split(/\s+/).map(pair => pair.split(',').map(Number)).filter(pair => pair.length === 2 && pair.every(Number.isFinite));
    if (coordinates.length < 2) return '';
    let path = `M${coordinates[0][0]},${coordinates[0][1]}`;
    for (let index = 1; index < coordinates.length; index += 1) {
        const previous = coordinates[index - 1], current = coordinates[index];
        const midpoint = (previous[0] + current[0]) / 2;
        path += ` C${midpoint},${previous[1]} ${midpoint},${current[1]} ${current[0]},${current[1]}`;
    }
    return path;
}

function interactiveChartSvg({ className, width, height, grid, lineClass = '', linePoints, lineSegments = null, smooth = false, hoverPoints, ariaLabel }) {
    const encodedPoints = escapeHtml(JSON.stringify(hoverPoints));
    const segments = Array.isArray(lineSegments) ? lineSegments : [linePoints];
    const lines = segments.filter(Boolean).map(points => smooth
        ? `<path class="${lineClass}" d="${smoothCoordinatePath(points)}"/>`
        : `<polyline class="${lineClass}" points="${points}"/>`).join('');
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
        const changeTarget = chart.closest('.history-popover')?.querySelector('[data-history-change]');
        let activeIndex = points.length - 1;
        let pinned = false;

        const updateChange = point => {
            if (!changeTarget || !Number.isFinite(Number(point?.change))) return;
            const change = Number(point.change);
            changeTarget.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
            changeTarget.classList.toggle('up', change >= 0);
            changeTarget.classList.toggle('down', change < 0);
        };
        const resetChange = () => {
            if (!changeTarget) return;
            changeTarget.textContent = changeTarget.dataset.defaultChange || '';
            changeTarget.classList.toggle('up', changeTarget.dataset.defaultChangeClass === 'up');
            changeTarget.classList.toggle('down', changeTarget.dataset.defaultChangeClass === 'down');
        };

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
            updateChange(point);
            hover.removeAttribute('hidden');
            chart.setAttribute('aria-label', `${chart.dataset.chartLabel}. ${point.primary}. ${point.secondary}${point.tertiary ? `. ${point.tertiary}` : ''}`);
        };
        const hide = () => { if (!pinned) { hover.setAttribute('hidden', ''); resetChange(); chart.setAttribute('aria-label', chart.dataset.chartLabel); } };
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

function renderHistoryChart(points, range, { start_at: startAt, end_at: endAt } = {}) {
    const clean = points
        .filter(point => Number.isFinite(Number(point.price_eur)) && parseOpenStockDate(point.recorded_at))
        .sort((left, right) => parseOpenStockDate(left.recorded_at).getTime() - parseOpenStockDate(right.recorded_at).getTime());
    if (clean.length < 2) return '<div class="history-empty">Collecting native EUR price history…</div>';
    const values = clean.map(point => Number(point.price_eur));
    const min = Math.min(...values), max = Math.max(...values);
    const padding = Math.max((max - min) * 0.08, max * 0.002, 0.01);
    const low = min - padding, high = max + padding, width = 264, height = 82;
    const sessionStart = range === '1d' ? parseOpenStockDate(startAt) : null;
    const sessionEnd = range === '1d' ? parseOpenStockDate(endAt) : null;
    const sessionDuration = sessionEnd?.getTime() - sessionStart?.getTime();
    const hasFixedDayScale = Number.isFinite(sessionDuration) && sessionDuration > 0;
    const coordinates = clean.map((point, index) => {
        const timestamp = parseOpenStockDate(point.recorded_at)?.getTime();
        const sessionX = hasFixedDayScale && Number.isFinite(timestamp)
            ? Math.max(0, Math.min(width, (timestamp - sessionStart.getTime()) / sessionDuration * width))
            : index / (clean.length - 1) * width;
        return { point, x: sessionX, y: height - ((Number(point.price_eur) - low) / (high - low || 1) * height) };
    });
    const linePoints = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const hoverPoints = coordinates.map(({ point, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: `€${money(point.price_eur)}`, secondary: formatLocalDateTime(point.recorded_at), change: (Number(point.price_eur) / Number(clean[0].price_eur) - 1) * 100 }));
    const first = clean[0], last = clean[clean.length - 1];
    const change = (Number(last.price_eur) / Number(first.price_eur) - 1) * 100;
    const changeClass = change >= 0 ? 'up' : 'down';
    const ariaLabel = `${chartRangeLabel(range)} native EUR price chart from ${formatChartDate(first.recorded_at)} to ${formatChartDate(last.recorded_at)}`;
    const chart = interactiveChartSvg({ className: 'history-chart', width, height, grid: `<path class="history-grid" d="M0 20H${width} M0 41H${width} M0 62H${width}"/>`, lineClass: `history-line ${changeClass}`, linePoints, smooth: true, hoverPoints, ariaLabel });
    const changeText = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
    const axisStart = hasFixedDayScale ? formatChartTime(startAt) : formatChartDate(first.recorded_at);
    const axisEnd = hasFixedDayScale ? formatChartTime(endAt) : formatChartDate(last.recorded_at);
    return `<div class="history-meta"><span>€${money(min)} – €${money(max)}</span><span class="history-change ${changeClass}" data-history-change data-default-change="${changeText}" data-default-change-class="${changeClass}">${changeText}</span></div>${chart}<div class="history-dates"><span>${axisStart}</span><span>${axisEnd}</span></div>`;
}

function renderHistoryPanel(panel, history, range) {
    panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · EUR <span>${history.points.length} native samples</span></div>${chartRangeControls(range)}</div>${renderHistoryChart(history.points, range, history)}`;
    attachInteractiveLineCharts(panel);
}

async function loadCardHistory(card, range = '1d', { force = false, showLoading = true } = {}) {
    const stockId = Number(card.dataset.stockId);
    const panel = card.querySelector('.history-popover');
    if (!Number.isInteger(stockId) || !panel) return;
    const key = `${stockId}:${range}`;
    panel.dataset.requestedRange = range;
    if (showLoading) panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · EUR</div>${chartRangeControls(range)}</div><div class="history-loading">Loading native EUR history…</div>`;
    let history = force ? null : historyCache.get(key);
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

async function refreshOpenHistoryPanels(container = document) {
    const openPanels = [...container.querySelectorAll('.stock-card[data-stock-id] .history-popover.is-open')];
    await Promise.all(openPanels.map(panel => {
        const card = panel.closest('.stock-card[data-stock-id]');
        return card ? loadCardHistory(card, panel.dataset.requestedRange || '1d', { force: true, showLoading: false }) : null;
    }));
}

function attachChartHovers(container) {
    const usesMobileChartInteraction = () => window.matchMedia?.('(hover: none), (pointer: coarse)').matches ?? false;
    const closeMobilePopovers = except => container.querySelectorAll('.history-popover.is-open').forEach(panel => {
        if (panel !== except) panel.classList.remove('is-open');
    });
    if (container.dataset.mobileChartDismissBound !== '1') {
        container.dataset.mobileChartDismissBound = '1';
        document.addEventListener('click', event => {
            if (!usesMobileChartInteraction() || event.target.closest?.('.stock-card[data-stock-id]')) return;
            closeMobilePopovers();
        });
    }
    container.querySelectorAll('.stock-card[data-stock-id]').forEach(card => {
        const panel = card.querySelector('.history-popover');
        let closeTimer;
        const cancelPopoverClose = () => {
            clearTimeout(closeTimer);
            closeTimer = undefined;
        };
        const delayPopoverClose = () => {
            cancelPopoverClose();
            panel.classList.add('is-open');
            closeTimer = setTimeout(() => panel.classList.remove('is-open'), 300);
        };

        card.addEventListener('mouseenter', () => { if (!usesMobileChartInteraction()) loadCardHistory(card, '1d'); }, { once: true });
        card.addEventListener('pointerenter', () => { if (!usesMobileChartInteraction()) cancelPopoverClose(); });
        card.addEventListener('pointerleave', () => { if (!usesMobileChartInteraction()) delayPopoverClose(); });
        panel.addEventListener('pointerenter', () => { if (!usesMobileChartInteraction()) cancelPopoverClose(); });
        panel.addEventListener('pointerleave', () => { if (!usesMobileChartInteraction()) delayPopoverClose(); });
        card.addEventListener('focusin', () => { if (!usesMobileChartInteraction()) loadCardHistory(card, '1d'); }, { once: true });
        card.addEventListener('click', event => {
            if (!usesMobileChartInteraction() || event.target.closest('.history-popover')) return;
            closeMobilePopovers(panel);
            panel.classList.add('is-open');
            loadCardHistory(card, panel.dataset.requestedRange || '1d');
        });
        panel.addEventListener('click', event => {
            const button = event.target.closest('[data-history-range]');
            if (!button) return;
            event.preventDefault();
            event.stopPropagation();
            loadCardHistory(card, button.dataset.historyRange);
        });
    });
}

    return { attachInteractiveLineCharts, interactiveChartSvg, renderHistoryChart, renderHistoryPanel, loadCardHistory, refreshOpenHistoryPanels, attachChartHovers };
}
