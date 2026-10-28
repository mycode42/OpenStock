import { escapeHtml } from './escape.js';
import { money, parseOpenStockDate, formatChartHoverTime, formatChartDate, formatChartTime, chartRangeLabel } from './formatting.js';

export function createCharts({ request, historyCache }) {
const historyRequests = new Map();
const usdChartNumber = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
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

function isMarketClosedWeekend(date) {
    const weekday = new Intl.DateTimeFormat('en-US', { timeZone: 'Europe/Berlin', weekday: 'short' }).format(date);
    return weekday === 'Sat' || weekday === 'Sun';
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
        const readout = chart.closest('[data-chart-readout]');
        const changeTarget = readout?.querySelector('[data-chart-change]');
        const valueTarget = readout?.querySelector('[data-chart-value]');
        const detailTarget = readout?.querySelector('[data-chart-detail]');
        let activeIndex = points.length - 1;
        let pinned = false;

        const updateChange = point => {
            if (!changeTarget || !point || !Object.hasOwn(point, 'change')) return;
            if (point.change === null || !Number.isFinite(Number(point.change))) {
                changeTarget.textContent = 'N/A';
                changeTarget.classList.remove('up', 'down');
                return;
            }
            const change = Number(point.change);
            changeTarget.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
            changeTarget.classList.toggle('up', change >= 0);
            changeTarget.classList.toggle('down', change < 0);
        };
        const resetChange = () => {
            if (!changeTarget) return;
            changeTarget.textContent = changeTarget.dataset.defaultChartChange || '';
            const defaultClass = changeTarget.dataset.defaultChartChangeClass;
            changeTarget.classList.toggle('up', defaultClass === 'up');
            changeTarget.classList.toggle('down', defaultClass === 'down');
        };
        const updateValue = point => {
            if (valueTarget && point?.price) valueTarget.textContent = point.price;
        };
        const resetValue = () => {
            if (valueTarget) valueTarget.textContent = valueTarget.dataset.defaultChartValue || '';
        };
        const updateDetail = point => {
            if (detailTarget && point && Object.hasOwn(point, 'detail')) detailTarget.textContent = point.detail;
        };
        const resetDetail = () => {
            if (detailTarget) detailTarget.textContent = detailTarget.dataset.defaultChartDetail || '';
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
            const lineCount = 1 + Number(Boolean(point.secondary)) + Number(Boolean(point.tertiary));
            const blockHeight = (lineCount - 1) * lineGap;
            const labelY = point.y > blockHeight + 18 ? point.y - blockHeight - 10 : point.y + 16;
            text.setAttribute('x', point.x); text.setAttribute('y', labelY); text.setAttribute('text-anchor', anchor);
            text.setAttribute('transform', compensateStretch(point.x, labelY));
            primary.setAttribute('x', point.x);
            secondary.setAttribute('x', point.x); secondary.setAttribute('dy', lineGap);
            tertiary.setAttribute('x', point.x); tertiary.setAttribute('dy', lineGap);
            primary.textContent = point.primary;
            secondary.textContent = point.secondary;
            secondary.toggleAttribute('hidden', !point.secondary);
            tertiary.textContent = point.tertiary || '';
            tertiary.toggleAttribute('hidden', !point.tertiary);
            updateChange(point);
            updateValue(point);
            updateDetail(point);
            hover.removeAttribute('hidden');
            chart.setAttribute('aria-label', `${chart.dataset.chartLabel}. ${point.primary}. ${point.secondary}${point.tertiary ? `. ${point.tertiary}` : ''}`);
        };
        const hide = () => { if (!pinned) { hover.setAttribute('hidden', ''); resetChange(); resetValue(); resetDetail(); chart.setAttribute('aria-label', chart.dataset.chartLabel); } };
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

function chartPrice(value, currency) {
    if (currency === 'USD') return usdChartNumber.format(value);
    return `€${money(value)}`;
}

function renderHistoryChart(points, range, { start_at: startAt, end_at: endAt, now = new Date(), priceField = 'price_eur', currency = 'EUR' } = {}) {
    const clean = points
        .map(point => ({ ...point, _chartPrice: Number(point[priceField]), _chartTime: point.recorded_at || point.timestamp }))
        .filter(point => Number.isFinite(point._chartPrice) && parseOpenStockDate(point._chartTime))
        .sort((left, right) => parseOpenStockDate(left._chartTime).getTime() - parseOpenStockDate(right._chartTime).getTime());
    if (clean.length < 2) {
        const message = range === '1d' && isMarketClosedWeekend(now) ? 'market closed' : 'collecting price history';
        return `<div class="history-empty">${message}</div>`;
    }
    const values = clean.map(point => point._chartPrice);
    const min = Math.min(...values), max = Math.max(...values);
    const padding = Math.max((max - min) * 0.08, max * 0.002, 0.01);
    const low = min - padding, high = max + padding, width = 264, height = 82;
    const sessionStart = range === '1d' ? parseOpenStockDate(startAt) : null;
    const sessionEnd = range === '1d' ? parseOpenStockDate(endAt) : null;
    const sessionDuration = sessionEnd?.getTime() - sessionStart?.getTime();
    const hasFixedDayScale = Number.isFinite(sessionDuration) && sessionDuration > 0;
    const coordinates = clean.map((point, index) => {
        const timestamp = parseOpenStockDate(point._chartTime)?.getTime();
        const sessionX = hasFixedDayScale && Number.isFinite(timestamp)
            ? Math.max(0, Math.min(width, (timestamp - sessionStart.getTime()) / sessionDuration * width))
            : index / (clean.length - 1) * width;
        return { point, x: sessionX, y: height - ((point._chartPrice - low) / (high - low || 1) * height) };
    });
    const linePoints = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const hoverPoints = coordinates.map(({ point, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: ['1y', '5y'].includes(range) ? formatChartDate(point._chartTime) : formatChartHoverTime(point._chartTime), secondary: '', price: chartPrice(point._chartPrice, currency), change: (point._chartPrice / clean[0]._chartPrice - 1) * 100 }));
    const first = clean[0], last = clean[clean.length - 1];
    const change = (last._chartPrice / first._chartPrice - 1) * 100;
    const changeClass = change >= 0 ? 'up' : 'down';
    const ariaLabel = `${chartRangeLabel(range)} ${currency} price chart from ${formatChartDate(first._chartTime)} to ${formatChartDate(last._chartTime)}`;
    const chart = interactiveChartSvg({ className: 'history-chart', width, height, grid: `<path class="history-grid" d="M0 20H${width} M0 41H${width} M0 62H${width}"/>`, lineClass: `history-line ${changeClass}`, linePoints, smooth: true, hoverPoints, ariaLabel });
    const changeText = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
    const axisStart = hasFixedDayScale ? formatChartTime(startAt) : formatChartDate(first._chartTime);
    const axisEnd = hasFixedDayScale ? formatChartTime(endAt) : formatChartDate(last._chartTime);
    const axisTicks = [0, 0.25, 0.5, 0.75, 1].map(fraction => {
        if (fraction === 0) return { fraction, label: axisStart };
        if (fraction === 1) return { fraction, label: axisEnd };
        if (hasFixedDayScale) return { fraction, label: formatChartTime(new Date(sessionStart.getTime() + sessionDuration * fraction).toISOString()) };
        const point = clean[Math.round((clean.length - 1) * fraction)];
        return { fraction, label: formatChartDate(point._chartTime) };
    });
    const axisLabels = axisTicks.map(({ fraction, label }) => `<span style="left:${(fraction * 100).toFixed(0)}%">${label}</span>`).join('');
    const priceRange = `${chartPrice(min, currency)} – ${chartPrice(max, currency)}`;
    return `<div class="history-chart-readout" data-chart-readout><div class="history-meta"><span class="history-price" data-chart-value data-default-chart-value="${priceRange}">${priceRange}</span><span class="history-change ${changeClass}" data-chart-change data-default-chart-change="${changeText}" data-default-chart-change-class="${changeClass}">${changeText}</span></div>${chart}<div class="history-dates" aria-label="Chart time axis">${axisLabels}</div></div>`;
}

function renderHistoryPanel(panel, history, range, { currency = 'EUR', priceField = 'price_eur', sampleLabel = 'native samples' } = {}) {
    const chartOptions = { ...history, currency, priceField };
    panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · ${currency} <span>${history.points.length} ${sampleLabel}${history.stale ? ' · cached' : ''}</span></div>${chartRangeControls(range)}</div>${renderHistoryChart(history.points, range, chartOptions)}`;
    attachInteractiveLineCharts(panel);
}

async function loadCardHistory(card, range = '1d', { force = false, showLoading = true } = {}) {
    const stockId = Number(card.dataset.stockId);
    const panel = card.querySelector('.history-popover');
    if (!Number.isInteger(stockId) || !panel) return;
    const key = `${stockId}:${range}`;
    panel.dataset.requestedRange = range;
    if (showLoading) panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · EUR</div>${chartRangeControls(range)}</div><div class="history-loading">loading price history</div>`;
    let history = force ? null : historyCache.get(key);
    if (!history) {
        let pending = historyRequests.get(key);
        if (!pending) {
            pending = request(`/stocks/${stockId}/history?range=${range}`);
            historyRequests.set(key, pending);
        }
        try {
            history = await pending;
            if (history) historyCache.set(key, history);
        } finally {
            if (historyRequests.get(key) === pending) historyRequests.delete(key);
        }
    }
    if (panel.dataset.requestedRange !== range) return;
    if (!history) {
        panel.innerHTML = '<div class="history-empty">History is unavailable.</div>';
        return;
    }
    renderHistoryPanel(panel, history, range);
}

async function loadQuoteCardHistory(card, range = '1d', { force = false, showLoading = true } = {}) {
    const symbol = String(card.dataset.quoteSymbol || '').toUpperCase();
    const panel = card.querySelector('.history-popover');
    if (!/^[A-Z0-9.-]{1,16}$/.test(symbol) || !panel) return;
    const key = `algo-universe:${symbol}:${range}`;
    panel.dataset.requestedRange = range;
    panel.dataset.requestedSymbol = symbol;
    if (showLoading) panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · USD</div>${chartRangeControls(range)}</div><div class="history-loading">loading price history</div>`;
    let history = force ? null : historyCache.get(key);
    if (!history) {
        let pending = historyRequests.get(key);
        if (!pending) {
            pending = request(`/algo-trader/universe/${encodeURIComponent(symbol)}/history?range=${encodeURIComponent(range)}`, {}, () => {});
            historyRequests.set(key, pending);
        }
        try {
            history = await pending;
            if (history) historyCache.set(key, history);
        } finally {
            if (historyRequests.get(key) === pending) historyRequests.delete(key);
        }
    }
    if (panel.dataset.requestedRange !== range || panel.dataset.requestedSymbol !== symbol) return;
    if (!history || !Array.isArray(history.points)) {
        panel.innerHTML = '<div class="history-empty">Price history is unavailable.</div>';
        return;
    }
    renderHistoryPanel(panel, history, range, { currency: 'USD', priceField: 'price', sampleLabel: history.demo_mode ? 'demo samples' : 'Alpaca bars' });
}

async function refreshOpenHistoryPanels(container = document, { includePreviouslyOpened = false } = {}) {
    const openPanels = [...container.querySelectorAll('.stock-card[data-stock-id] .history-popover')]
        .filter(panel => panel.classList.contains('is-open')
            || panel.closest('.stock-card')?.matches(':hover, :focus-within')
            || (includePreviouslyOpened && panel.dataset.requestedRange));
    await Promise.all(openPanels.map(panel => {
        const card = panel.closest('.stock-card[data-stock-id]');
        return card ? loadCardHistory(card, panel.dataset.requestedRange || '1d', { force: true, showLoading: false }) : null;
    }));
}

async function refreshOpenQuoteHistoryPanels(container = document, { includePreviouslyOpened = false } = {}) {
    const openPanels = [...container.querySelectorAll('.stock-card[data-quote-symbol] .history-popover')]
        .filter(panel => panel.classList.contains('is-open')
            || panel.closest('.stock-card')?.matches(':hover, :focus-within')
            || (includePreviouslyOpened && panel.dataset.requestedRange));
    await Promise.all(openPanels.map(panel => {
        const card = panel.closest('.stock-card[data-quote-symbol]');
        return card ? loadQuoteCardHistory(card, panel.dataset.requestedRange || '1d', { force: true, showLoading: false }) : null;
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
        const refreshCardHistory = () => loadCardHistory(card, panel.dataset.requestedRange || '1d', { force: true, showLoading: false });
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

        card.addEventListener('mouseenter', () => { if (!usesMobileChartInteraction()) refreshCardHistory(); });
        card.addEventListener('pointerenter', () => { if (!usesMobileChartInteraction()) cancelPopoverClose(); });
        card.addEventListener('pointerleave', () => { if (!usesMobileChartInteraction()) delayPopoverClose(); });
        panel.addEventListener('pointerenter', () => { if (!usesMobileChartInteraction()) cancelPopoverClose(); });
        panel.addEventListener('pointerleave', () => { if (!usesMobileChartInteraction()) delayPopoverClose(); });
        card.addEventListener('focusin', () => { if (!usesMobileChartInteraction()) refreshCardHistory(); });
        card.addEventListener('click', event => {
            if (!usesMobileChartInteraction() || event.target.closest('.history-popover')) return;
            closeMobilePopovers(panel);
            panel.classList.add('is-open');
            refreshCardHistory();
        });
        panel.addEventListener('click', event => {
            const button = event.target.closest('[data-history-range]');
            if (!button) return;
            event.preventDefault();
            event.stopPropagation();
            loadCardHistory(card, button.dataset.historyRange, { force: true });
        });
    });
}

function attachQuoteChartHovers(container) {
    const usesMobileChartInteraction = () => window.matchMedia?.('(hover: none), (pointer: coarse)').matches ?? false;
    const closeMobilePopovers = except => container.querySelectorAll('.history-popover.is-open').forEach(panel => {
        if (panel !== except) panel.classList.remove('is-open');
    });
    if (container.dataset.mobileQuoteChartDismissBound !== '1') {
        container.dataset.mobileQuoteChartDismissBound = '1';
        document.addEventListener('click', event => {
            if (!usesMobileChartInteraction() || event.target.closest?.('.stock-card[data-quote-symbol]')) return;
            closeMobilePopovers();
        });
    }
    container.querySelectorAll('.stock-card[data-quote-symbol]').forEach(card => {
        const panel = card.querySelector('.history-popover');
        if (!panel) return;
        const refreshHistory = () => loadQuoteCardHistory(card, panel.dataset.requestedRange || '1d', { force: true, showLoading: false });
        card.addEventListener('mouseenter', () => { if (!usesMobileChartInteraction()) refreshHistory(); });
        card.addEventListener('focusin', () => { if (!usesMobileChartInteraction()) refreshHistory(); });
        card.addEventListener('click', event => {
            if (!usesMobileChartInteraction() || event.target.closest('.history-popover')) return;
            closeMobilePopovers(panel);
            panel.classList.add('is-open');
            refreshHistory();
        });
        panel.addEventListener('click', event => {
            const button = event.target.closest('[data-history-range]');
            if (!button) return;
            event.preventDefault();
            event.stopPropagation();
            loadQuoteCardHistory(card, button.dataset.historyRange, { force: true });
        });
    });
}

    return { attachInteractiveLineCharts, interactiveChartSvg, renderHistoryChart, renderHistoryPanel, loadCardHistory, loadQuoteCardHistory, refreshOpenHistoryPanels, refreshOpenQuoteHistoryPanels, attachChartHovers, attachQuoteChartHovers };
}
