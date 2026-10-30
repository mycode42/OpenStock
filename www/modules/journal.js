import { escapeHtml } from './escape.js';
import { parseOpenStockDate, formatLocalDate } from './formatting.js';

export function createJournal(context) {
    let journalChartMode = 'equity';
    let journalPerformanceChartType = 'line';
    const openJournalSections = new Set();
    const { charts } = context;
    let journalData = null;
    let journalTradePage = 1;
    const TRADES_PER_PAGE = 15;
    const tradingAnalytics = () => journalData?.analytics || {};
    const capitalEvents = () => tradingAnalytics().capital_events || [];
    function pageTrades() {
        const list = tradingAnalytics().trade_list || [];
        const pages = Math.max(1, Math.ceil(list.length / TRADES_PER_PAGE));
        journalTradePage = Math.min(Math.max(1, journalTradePage), pages);
        return list.slice((journalTradePage - 1) * TRADES_PER_PAGE, journalTradePage * TRADES_PER_PAGE);
    }
    function tradePagination() {
        const total = (tradingAnalytics().trade_list || []).length;
        return { page: journalTradePage, page_size: TRADES_PER_PAGE, total, total_pages: Math.max(1, Math.ceil(total / TRADES_PER_PAGE)) };
    }
    const attachInteractiveLineCharts = charts.attachInteractiveLineCharts;
    const interactiveChartSvg = charts.interactiveChartSvg;

function journalMoney(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value));
}

function journalPercent(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : `${Number(value).toFixed(2)}%`;
}

function journalSigned(value) {
    const number = Number(value);
    if (value === null || value === undefined || !Number.isFinite(number)) return 'N/A';
    return `${number > 0 ? '+' : number < 0 ? '-' : ''}${journalMoney(Math.abs(number))}`;
}

function journalClass(value) {
    return Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : '';
}

function journalInputDate(value) {
    const date = parseOpenStockDate(value);
    if (!date) return '';
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 19);
}

function journalTradeOpenedDate(value) {
    const date = parseOpenStockDate(value);
    return date ? date.toLocaleDateString('en-GB', { timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit' }) : '—';
}

function journalTradePrice(value, currency, assetType = 'stock') {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    const code = /^[A-Z]{3}$/.test(String(currency || '')) ? currency : 'EUR';
    try {
        return new Intl.NumberFormat('en-GB', { style: 'currency', currency: code, minimumFractionDigits: 2, maximumFractionDigits: assetType === 'crypto' ? 12 : 2 }).format(Number(value));
    } catch {
        return `${code} ${Number(value).toFixed(assetType === 'crypto' ? 12 : 2)}`;
    }
}

function journalKpi(label, displayValue, hint = '', cssClass = '', visual = '') {
    return `<div class="journal-kpi" title="${escapeHtml(hint)}"><div class="journal-kpi-head"><div>${escapeHtml(label)}</div><strong class="${cssClass}">${displayValue}</strong></div>${visual ? `<div class="journal-kpi-visual">${visual}</div>` : ''}</div>`;
}

function journalKpiSparkline(values, cssClass = '') {
    const clean = (values || []).map(Number).filter(Number.isFinite);
    if (clean.length < 2) return '<div class="journal-kpi-visual-empty">Trend appears as snapshots accumulate</div>';
    const minimum = Math.min(...clean), maximum = Math.max(...clean);
    const span = maximum - minimum || 1;
    const points = clean.map((value, index) => `${(4 + index / (clean.length - 1) * 112).toFixed(1)},${(28 - (value - minimum) / span * 22).toFixed(1)}`);
    const line = `M${points.join(' L')}`;
    const area = `${line} L116,31 L4,31 Z`;
    return `<svg class="journal-kpi-sparkline ${cssClass}" viewBox="0 0 120 32" preserveAspectRatio="none" aria-hidden="true"><path class="journal-kpi-sparkline-area" d="${area}"/><path class="journal-kpi-sparkline-line" d="${line}"/></svg>`;
}

function journalKpiBalance(value, capitalBase) {
    const amount = Number(value);
    const base = Math.max(Math.abs(Number(capitalBase) || 0), 1);
    if (!Number.isFinite(amount)) return '';
    const width = Math.min(48, Math.abs(amount) / base * 48);
    const left = amount < 0 ? 50 - width : 50;
    return `<div class="journal-kpi-balance ${journalClass(amount)}" role="img" aria-label="Net P/L ${journalSigned(amount)} relative to contributed capital"><span class="journal-kpi-zero"></span><i style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%"></i></div><small class="journal-kpi-visual-note">P/L vs contributed capital</small>`;
}

function journalKpiRing(value, scale, note) {
    if (value === null || value === undefined || value === '') return '<div class="journal-kpi-visual-empty">Not enough history</div>';
    const amount = Number(value);
    if (!Number.isFinite(amount)) return '<div class="journal-kpi-visual-empty">Not enough history</div>';
    const radius = 20;
    const circumference = 2 * Math.PI * radius;
    const progress = Math.max(0, Math.min(1, Math.abs(amount) / scale));
    const color = amount < 0 ? 'negative' : 'positive';
    return `<div class="journal-kpi-ring-wrap"><svg class="journal-kpi-ring ${color}" viewBox="0 0 52 52" aria-hidden="true"><circle class="journal-kpi-ring-track" cx="26" cy="26" r="${radius}"/><circle class="journal-kpi-ring-value" cx="26" cy="26" r="${radius}" stroke-dasharray="${circumference.toFixed(2)}" stroke-dashoffset="${(circumference * (1 - progress)).toFixed(2)}" transform="rotate(-90 26 26)"/></svg><span>${escapeHtml(note)}</span></div>`;
}

function equityAxisBounds(minimum, maximum, ticks = 5) {
    const range = Math.max(maximum - minimum, 1);
    const exponent = Math.floor(Math.log10(range / Math.max(1, ticks - 1)));
    const fraction = range / Math.max(1, ticks - 1) / (10 ** exponent);
    const step = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * (10 ** exponent);
    const low = Math.floor(minimum / step) * step;
    const high = Math.ceil(maximum / step) * step;
    const values = [];
    for (let value = low; value <= high + step / 100; value += step) values.push(Number(value.toFixed(8)));
    return { low, high, values };
}

function journalDayMonthLabel(value) {
    const date = parseOpenStockDate(value);
    if (!date) return '';
    return date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'Europe/Berlin' }).replace(/^(\d+)\s+/, '$1. ');
}

function journalMonthLabel(value) {
    const date = parseOpenStockDate(value);
    return date ? date.toLocaleDateString('en-GB', { month: 'short', timeZone: 'Europe/Berlin' }) : '';
}

function journalChartMeta(value, change, detail) {
    const changeText = Number.isFinite(change) ? `${change > 0 ? '+' : ''}${change.toFixed(2)}%` : 'N/A';
    const changeClass = Number.isFinite(change) && change < 0 ? 'down' : 'up';
    const detailMarkup = detail ? `<span class="journal-chart-detail" data-chart-detail data-default-chart-detail="${escapeHtml(detail)}">${escapeHtml(detail)}</span>` : '';
    return `<div class="journal-chart-meta"><div class="journal-chart-readout">${detailMarkup}<span class="journal-chart-value" data-chart-value data-default-chart-value="${escapeHtml(value)}">${escapeHtml(value)}</span></div><span class="history-change ${changeClass}" data-chart-change data-default-chart-change="${changeText}" data-default-chart-change-class="${changeClass}">${changeText}</span></div>`;
}

function journalAxisMoney(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) return '—';
    const sign = amount < 0 ? '-' : '';
    if (Math.abs(amount) >= 1000) return `${sign}$${Math.round(Math.abs(amount) / 1000)}k`;
    return `${sign}$${Math.round(Math.abs(amount)).toLocaleString('en-US')}`;
}

function renderEquityCurve(points, resolution = 'snapshot', initialCapital) {
    const clean = (points || []).filter(point => Number.isFinite(Number(point.equity_usd)) && parseOpenStockDate(point.at)).sort((left, right) => parseOpenStockDate(left.at).getTime() - parseOpenStockDate(right.at).getTime());
    if (clean.length < 2) return '<div class="journal-empty">Insufficient data — equity snapshots begin after initial capital is recorded.</div>';
    const values = clean.map(point => Number(point.equity_usd));
    const min = Math.min(...values), max = Math.max(...values), padding = Math.max((max - min) * 0.08, 1);
    const width = 800, height = 260, left = 40, right = 12, top = 10, bottom = 32, plotWidth = width - left - right, plotHeight = height - top - bottom;
    const { low, high, values: axisValues } = equityAxisBounds(min - padding, max + padding);
    const times = clean.map(point => parseOpenStockDate(point.at).getTime());
    const firstTime = times[0], timeRange = times[times.length - 1] - firstTime || 1;
    const coordinates = clean.map((point, index) => ({ point, value: Number(point.equity_usd), x: left + (times[index] - firstTime) / timeRange * plotWidth, y: top + plotHeight - ((Number(point.equity_usd) - low) / (high - low || 1) * plotHeight) }));
    const line = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const capitalBase = Number(initialCapital);
    const hoverPoints = coordinates.map(({ point, value, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: journalDayMonthLabel(point.at), secondary: '', tertiary: '', price: journalMoney(value), change: capitalBase > 0 ? (value / capitalBase - 1) * 100 : null }));
    const ariaLabel = `Equity curve using ${resolution} closing samples from ${formatLocalDate(clean[0].at)} to ${formatLocalDate(clean[clean.length - 1].at)}`;
    const yTicks = axisValues.map(value => ({ value, y: top + plotHeight - ((value - low) / (high - low || 1) * plotHeight) }));
    const xTicks = Array.from({ length: 12 }, (_, index) => {
        const ratio = index / 11;
        return { ratio, x: left + plotWidth * ratio, label: journalDayMonthLabel(new Date(firstTime + timeRange * ratio).toISOString()) };
    });
    const yAxis = yTicks.map(({ y }) => `<line class="equity-grid-line" x1="${left}" x2="${width - right}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`).join('');
    const xAxis = xTicks.map(({ x }) => `<line class="equity-axis-tick" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${top + plotHeight}" y2="${top + plotHeight + 5}"/>`).join('');
    const chart = interactiveChartSvg({ className: 'journal-equity-curve', width, height, grid: `<g class="equity-axis">${yAxis}${xAxis}<line class="equity-axis-baseline" x1="${left}" x2="${width - right}" y1="${top + plotHeight}" y2="${top + plotHeight}"/></g>`, lineClass: 'equity-line', linePoints: line, hoverPoints, ariaLabel });
    const yLabels = yTicks.map(({ value, y }) => `<span style="top:${(y / height * 100).toFixed(3)}%">${journalAxisMoney(value)}</span>`).join('');
    const xLabels = xTicks.map(({ ratio, label }) => `<span style="left:${(left / width * 100 + ratio * plotWidth / width * 100).toFixed(3)}%">${escapeHtml(label)}</span>`).join('');
    const lastEquity = values[values.length - 1];
    const equityChange = capitalBase > 0 ? (lastEquity / capitalBase - 1) * 100 : undefined;
    return `<div class="journal-chart-container" data-chart-readout>${journalChartMeta(journalMoney(lastEquity), equityChange, '')}<div class="journal-equity-chart"><div class="journal-equity-y-labels" aria-hidden="true">${yLabels}</div>${chart}<div class="journal-equity-x-labels equity-x-labels" aria-hidden="true">${xLabels}</div></div></div>`;
}

function renderPerformanceCurve(rows, period, capitalBase, chartType = 'line') {
    const clean = (rows || []).filter(row => Number.isFinite(Number(row.pnl_usd)) && row.day).map(row => ({ ...row, at: `${row.day}T12:00:00Z`, value: Number(row.pnl_usd) })).filter(row => parseOpenStockDate(row.at)).sort((left, right) => parseOpenStockDate(left.at).getTime() - parseOpenStockDate(right.at).getTime());
    const daily = period === 'daily';
    const periodName = daily ? 'Daily' : 'Monthly';
    const periodCount = daily ? 14 : 12;
    const currentMonth = new Date();
    currentMonth.setDate(1);
    currentMonth.setHours(12, 0, 0, 0);
    const monthKey = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
    const firstVisibleMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 11, 1, 12);
    const visibleRows = daily
        ? clean.slice(-periodCount)
        : clean.filter(row => row.day.slice(0, 7) >= monthKey(firstVisibleMonth) && row.day.slice(0, 7) <= monthKey(currentMonth));
    const values = visibleRows.map(point => point.value);
    const min = Math.min(0, ...values), max = Math.max(0, ...values), padding = Math.max((max - min) * .1, 1);
    const width = 800, height = 260, left = 40, right = 12, top = 10, bottom = 32, plotWidth = width - left - right, plotHeight = height - top - bottom;
    const { low, high, values: axisValues } = equityAxisBounds(min - padding, max + padding);
    const periodSlotWidth = plotWidth / periodCount;
    const fixedSlots = daily
        ? Array.from({ length: periodCount }, (_, index) => {
            const point = visibleRows[index] || null;
            const label = point ? journalDayMonthLabel(point.at) : '';
            return { point, label, x: left + periodSlotWidth * (index + .5), left: left + periodSlotWidth * index, width: periodSlotWidth };
        })
        : Array.from({ length: periodCount }, (_, index) => {
            const date = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 11 + index, 1, 12);
            const point = visibleRows.find(row => row.day.slice(0, 7) === monthKey(date)) || null;
            const label = date.toLocaleDateString('en-GB', { month: 'short' });
            return { point, label, x: left + periodSlotWidth * (index + .5), left: left + periodSlotWidth * index, width: periodSlotWidth };
        });
    const coordinates = fixedSlots.flatMap(slot => slot.point ? [{
        point: slot.point,
        value: slot.point.value,
        x: slot.x,
        y: top + plotHeight - ((slot.point.value - low) / (high - low || 1) * plotHeight)
    }] : []);
    const performanceDetail = point => `${point.trades} ${point.trades === 1 ? 'trade' : 'trades'}`;
    const performanceHoverPoint = (point, value, x, y) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: daily ? journalDayMonthLabel(point.at) : journalMonthLabel(point.at), secondary: '', tertiary: '', price: journalSigned(value), detail: performanceDetail(point), change: capitalBase > 0 ? value / capitalBase * 100 : null });
    const hoverPoints = fixedSlots.map(({ point, label, x }) => point
        ? performanceHoverPoint(point, point.value, x, top + plotHeight - ((point.value - low) / (high - low || 1) * plotHeight))
        : { x: Number(x.toFixed(1)), y: Number((top + plotHeight - ((0 - low) / (high - low || 1) * plotHeight)).toFixed(1)), primary: label || 'No data', secondary: '', tertiary: '', price: 'N/A', detail: '', change: null });
    const yTicks = axisValues.map(value => ({ value, y: top + plotHeight - ((value - low) / (high - low || 1) * plotHeight) }));
    const yAxis = yTicks.map(({ y }) => `<line class="equity-grid-line" x1="${left}" x2="${width - right}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`).join('');
    const xAxis = fixedSlots.map(({ x }) => `<line class="equity-axis-tick" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${top + plotHeight}" y2="${top + plotHeight + 5}"/>`).join('');
    const zeroY = top + plotHeight - ((0 - low) / (high - low || 1) * plotHeight);
    const bars = chartType === 'bar' ? fixedSlots.map(({ point, left: barLeft, width: barWidth }) => {
        if (!point) return '';
        const valueY = top + plotHeight - ((point.value - low) / (high - low || 1) * plotHeight);
        const barY = Math.min(valueY, zeroY);
        const barHeight = Math.max(1, Math.abs(valueY - zeroY));
        const color = point.value < 0 ? 'negative' : 'positive';
        return `<rect class="performance-bar ${color}" x="${barLeft}" y="${barY.toFixed(1)}" width="${barWidth}" height="${barHeight.toFixed(1)}"/>`;
    }).join('') : '';
    const lineRuns = [];
    let lineRun = [];
    for (const { point, x } of fixedSlots) {
        if (!point) {
            if (lineRun.length) lineRuns.push(lineRun);
            lineRun = [];
            continue;
        }
        const y = top + plotHeight - ((point.value - low) / (high - low || 1) * plotHeight);
        lineRun.push(`${x.toFixed(1)},${y.toFixed(1)}`);
    }
    if (lineRun.length) lineRuns.push(lineRun);
    const lineSegments = chartType === 'line' ? lineRuns.filter(run => run.length > 1).map(run => run.join(' ')) : [];
    const linePoints = chartType === 'line' ? lineRuns.flatMap(run => run).join(' ') : '';
    const markers = chartType === 'line' ? coordinates.map(({ x, y, value }) => `<span class="performance-point ${value < 0 ? 'negative' : 'positive'}" style="left:${(x / width * 100).toFixed(3)}%;top:${(y / height * 100).toFixed(3)}%"></span>`).join('') : '';
    const zeroLine = `<line class="equity-axis-baseline performance-zero-line" x1="${left}" x2="${width - right}" y1="${zeroY}" y2="${zeroY}"/>`;
    const chart = interactiveChartSvg({ className: 'journal-equity-curve', width, height, grid: `<g class="equity-axis">${yAxis}${xAxis}${zeroLine}</g>${bars}`, lineClass: `performance-line ${period}`, linePoints, lineSegments, hoverPoints, ariaLabel: `${periodName} realized P/L ${chartType} chart in EUR` });
    const yLabels = yTicks.map(({ value, y }) => `<span style="top:${(y / height * 100).toFixed(3)}%">${value > 0 ? '+' : ''}${journalAxisMoney(value)}</span>`).join('');
    const xLabels = fixedSlots.map(({ x, label }) => `<span style="left:${(x / width * 100).toFixed(3)}%">${escapeHtml(label)}</span>`).join('');
    const xLabelClass = daily ? 'journal-equity-x-labels daily-performance-labels' : 'journal-equity-x-labels';
    const lastPoint = [...fixedSlots].reverse().find(slot => slot.point)?.point;
    const latestChange = lastPoint && capitalBase > 0 ? lastPoint.value / capitalBase * 100 : undefined;
    return `<div class="journal-chart-container" data-chart-readout>${journalChartMeta(lastPoint ? journalSigned(lastPoint.value) : 'N/A', latestChange, lastPoint ? performanceDetail(lastPoint) : '')}<div class="journal-equity-chart"><div class="journal-equity-y-labels" aria-hidden="true">${yLabels}</div>${chart}${markers ? `<div class="performance-point-layer" aria-hidden="true">${markers}</div>` : ''}<div class="${xLabelClass}" aria-hidden="true">${xLabels}</div></div></div>`;
}

function journalChartControls(active, chartType) {
    const metrics = `<div class="history-ranges journal-chart-controls" role="group" aria-label="Journal chart metric">${[['equity', 'Equity'], ['daily', 'Daily'], ['monthly', 'Monthly']].map(([mode, label]) => `<button type="button" class="history-range ${mode === active ? 'active' : ''}" data-journal-chart-mode="${mode}">${label}</button>`).join('')}</div>`;
    const styles = active === 'equity' ? '' : `<div class="history-ranges journal-chart-controls" role="group" aria-label="Performance chart style">${[['line', 'Line'], ['bar', 'Bar']].map(([type, label]) => `<button type="button" class="history-range ${type === chartType ? 'active' : ''}" data-journal-chart-type="${type}">${label}</button>`).join('')}</div>`;
    return `<div class="journal-chart-control-groups">${metrics}${styles}</div>`;
}

function renderJournalChart(analytics, mode, chartType, initialCapital) {
    if (mode === 'daily') return renderPerformanceCurve(analytics?.daily_performance, 'daily', Number(analytics?.capital?.net_contributions_usd), chartType);
    if (mode === 'monthly') return renderPerformanceCurve(analytics?.monthly_performance?.map(row => ({ ...row, day: `${row.month}-15` })), 'monthly', Number(analytics?.capital?.net_contributions_usd), chartType);
    return renderEquityCurve(analytics?.equity_curve, analytics?.equity_curve_resolution, initialCapital);
}

function tradeGroupLegs(trade) {
    const group = trade.group_details || {};
    const merged = (group.merged_entries || []).map(entry => ({
        type: entry.audit_type === 'duplicate_manual_record' ? 'Removed duplicate entry' : entry.kind === 'broker_execution' ? `${entry.side || 'buy'} fill` : 'Rebuy',
        at: entry.executed_at || entry.opened_at,
        quantity: entry.quantity,
        price: entry.execution_price_usd ?? entry.entry_price_usd,
        fee: entry.fees_usd ?? entry.entry_fee_usd,
        currency: entry.currency || trade.currency,
        asset_type: trade.asset_type
    }));
    const partialSales = (group.partial_sales || []).map(sale => ({
        type: 'Partial sell', at: sale.closed_at, quantity: sale.quantity, price: sale.exit_price_usd,
        fee: sale.exit_fee_usd, currency: sale.currency || trade.currency, asset_type: sale.asset_type || trade.asset_type
    }));
    return [...merged, ...partialSales];
}

function renderTradeRows(items) {
    if (!items.length) return '<tr><td colspan="9" style="color:#64748b">No trades recorded.</td></tr>';
    const quantity = value => Number.isFinite(Number(value)) ? new Intl.NumberFormat('en-GB', { maximumFractionDigits: 12 }).format(Number(value)) : '—';
    return items.map(trade => {
        const legs = tradeGroupLegs(trade);
        const details = legs.length ? `<details class="journal-trade-details"><summary>View ${legs.length} fill${legs.length === 1 ? '' : 's'}</summary><div class="journal-trade-fill-list">${legs.map(leg => `<div><strong>${escapeHtml(leg.type)}</strong> · ${escapeHtml(journalTradeOpenedDate(leg.at))} · ${quantity(leg.quantity)} @ ${escapeHtml(journalTradePrice(leg.price, leg.currency, leg.asset_type))}${Number(leg.fee) > 0 ? ` · fee ${escapeHtml(journalMoney(leg.fee))}` : ''}</div>`).join('')}</div></details>` : '';
        return `<tr><td>${Number(trade.trade_number)}</td><td>${journalTradeOpenedDate(trade.opened_at)}</td><td><span class="badge ${trade.is_closed ? 'sold' : 'owned'}">${trade.is_closed ? 'CLOSED' : 'OPEN'}</span></td><td><strong>${escapeHtml(trade.symbol)}</strong>${details}</td><td>${quantity(trade.quantity)}</td><td>${journalTradePrice(trade.entry_price_usd, trade.currency, trade.asset_type)}</td><td>${journalTradePrice(trade.exit_price_usd, trade.currency, trade.asset_type)}</td><td class="${journalClass(trade.net_pnl_usd)}">${journalSigned(trade.net_pnl_usd)}</td><td class="${journalClass(trade.return_pct)}">${journalPercent(trade.return_pct)}</td></tr>`;
    }).join('');
}

function renderTradePagination(pagination) {
    const total = Number(pagination?.total) || 0;
    const page = Number(pagination?.page) || 1;
    const totalPages = Number(pagination?.total_pages) || 1;
    if (!total) return '';
    const visiblePages = totalPages <= 7
        ? Array.from({ length: totalPages }, (_, index) => index + 1)
        : [...new Set([1, page - 1, page, page + 1, totalPages].filter(number => number >= 1 && number <= totalPages))].sort((left, right) => left - right);
    const numberedPages = visiblePages.map((number, index) => {
        const gap = index > 0 && number - visiblePages[index - 1] > 1 ? '<span class="journal-page-ellipsis" aria-hidden="true">…</span>' : '';
        const current = number === page;
        return `${gap}<button type="button" class="btn btn-sm btn-secondary" data-trade-page="${number}" aria-label="Page ${number}"${current ? ' aria-current="page"' : ''}>${number}</button>`;
    }).join('');
    return `<div class="journal-pagination" aria-label="Trade history pages"><span>${total} ${total === 1 ? 'trade' : 'trades'} · ${pagination.page_size} per page · Page ${page} of ${totalPages}</span><div class="journal-pagination-actions"><button type="button" class="btn btn-sm btn-secondary" data-trade-page="${page - 1}" aria-label="Previous page" ${page <= 1 ? 'disabled' : ''}>Previous</button><div class="journal-pagination-pages" aria-label="Choose trade page">${numberedPages}</div><button type="button" class="btn btn-sm btn-secondary" data-trade-page="${page + 1}" aria-label="Next page" ${page >= totalPages ? 'disabled' : ''}>Next</button></div></div>`;
}

function renderDailyPerformance(rows = [], capitalBase = 0) {
    if (!rows.length) return '<p class="journal-empty">Daily results appear after completed trades.</p>';
    return `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Day</th><th>P/L</th><th>Return</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody>${rows.slice(-14).reverse().map(row => `<tr><td>${escapeHtml(row.day)}</td><td class="${journalClass(row.pnl_usd)}">${journalSigned(row.pnl_usd)}</td><td class="${journalClass(row.pnl_usd)}">${journalPercent(capitalBase > 0 ? Number(row.pnl_usd) / capitalBase * 100 : null)}</td><td>${row.trades}</td><td>${journalPercent(row.wins / row.trades * 100)}</td></tr>`).join('')}</tbody></table></div>`;
}

function renderMonthlyPerformance(rows = [], capitalBase = 0) {
    if (!rows.length) return '<p class="journal-empty">Monthly results appear after completed trades.</p>';
    return `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Month</th><th>P/L</th><th>Return</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody>${rows.slice(-12).reverse().map(row => `<tr><td>${escapeHtml(row.month)}</td><td class="${journalClass(row.pnl_usd)}">${journalSigned(row.pnl_usd)}</td><td class="${journalClass(row.pnl_usd)}">${journalPercent(capitalBase > 0 ? Number(row.pnl_usd) / capitalBase * 100 : null)}</td><td>${row.trades}</td><td>${journalPercent(row.wins / row.trades * 100)}</td></tr>`).join('')}</tbody></table></div>`;
}

function renderPerformanceDetails(analytics) {
    const capital = analytics?.capital || {};
    const capitalBase = Number(capital.net_contributions_usd);
    return `<details class="journal-collapsible journal-performance-details"><summary><span>Performance details</span></summary><div class="journal-collapsible-content"><div class="journal-performance-section"><div class="journal-performance-heading">Daily performance <span>Latest 14 trading days · return on contributed capital</span></div>${renderDailyPerformance(analytics?.daily_performance, capitalBase)}</div><div class="journal-performance-section"><div class="journal-performance-heading">Monthly performance <span>Latest 12 completed months · return on contributed capital</span></div>${renderMonthlyPerformance(analytics?.monthly_performance, capitalBase)}</div></div></details>`;
}

function collapseJournalCard(container, selector, label, description = '') {
    const card = typeof selector === 'string' ? container.querySelector(selector) : selector;
    if (!card || card.querySelector(':scope > details.journal-collapsible')) return;
    const details = document.createElement('details');
    details.className = 'journal-collapsible';
    details.dataset.journalSection = label;
    details.open = openJournalSections.has(label);
    details.addEventListener('toggle', () => {
        if (details.open) openJournalSections.add(label);
        else openJournalSections.delete(label);
    });
    const summary = document.createElement('summary');
    summary.innerHTML = `<span>${escapeHtml(label)}</span>${description ? `<small>${escapeHtml(description)}</small>` : ''}`;
    const content = document.createElement('div');
    content.className = 'journal-collapsible-content';
    [...card.childNodes].forEach(node => content.appendChild(node));
    details.append(summary, content);
    card.append(details);
}

function collapseJournalCardByTitle(container, title, label, description) {
    const card = [...container.querySelectorAll('.card')].find(item => item.querySelector('.section-title')?.textContent.trim() === title);
    if (card) collapseJournalCard(container, card, label, description);
}

function renderJournal() {
    const container = document.getElementById('page-journal');
    const analytics = tradingAnalytics();
    const capital = analytics?.capital || {};
    const realized = analytics?.realized || {};
    const tradeStats = analytics?.trades || {};
    const performance = analytics?.performance || {};
    const currentPnl = (Number(realized.net_usd) || 0) + (Number(capital.unrealized_pnl_usd) || 0);
    container.innerHTML = `<header><h1>Trading Journal</h1></header><div id="ac"></div>${analytics?.equity_available ? '' : '<div class="alert-box info">The equity curve appears once Alpaca reports portfolio history.</div>'}
      <div class="journal-kpi-grid journal-kpi-featured-grid">${journalKpi('Current Capital', journalMoney(capital.current_usd), 'Cash plus priced open positions.', '', journalKpiSparkline((analytics?.equity_curve || []).map(point => point.equity_usd)))}${journalKpi('Net P/L', journalSigned(currentPnl), 'Realized plus unrealized P/L; capital events excluded.', journalClass(currentPnl), journalKpiBalance(currentPnl, capital.net_contributions_usd))}${journalKpi('Return', journalPercent(capital.current_return_pct), 'Current capital less net owner contributions divided by net owner contributions.', journalClass(capital.current_return_pct), journalKpiRing(capital.current_return_pct, 25, 'Return magnitude · 25% scale'))}${journalKpi('Win Rate', journalPercent(tradeStats.win_rate_pct), 'Winning completed trades divided by completed trades.', '', journalKpiRing(tradeStats.win_rate_pct, 100, 'Winning closed trades'))}</div>
      <div class="journal-kpi-grid journal-kpi-secondary-grid">${journalKpi('Available Cash', journalMoney(capital.available_cash_usd), 'Capital events less purchases/fees plus sales.')}${journalKpi('Avg Daily', `${journalSigned(performance.average_daily_pnl_usd)} · ${journalPercent(performance.average_daily_return_pct)}`, 'Average realized P/L per completed trading day and its percentage of contributed capital.', journalClass(performance.average_daily_pnl_usd))}${journalKpi('Avg Monthly', `${journalSigned(performance.average_monthly_pnl_usd)} · ${journalPercent(performance.average_monthly_return_pct)}`, 'Average realized P/L per completed month and its percentage of contributed capital.', journalClass(performance.average_monthly_pnl_usd))}${journalKpi('YTD P/L', journalSigned(performance.ytd_pnl_usd), 'Net P/L from completed trades this calendar year.', journalClass(performance.ytd_pnl_usd))}${journalKpi('Est. Yearly Profit', journalSigned(performance.estimated_yearly_profit_usd), 'Observed-rate annual profit illustration.', journalClass(performance.estimated_yearly_profit_usd))}${journalKpi('Open Positions', String(tradeStats.open ?? 0), 'Lots not yet closed.')}</div>
      <div class="card"><div class="section-header"><div class="section-title">Equity Curve</div></div>${renderEquityCurve(analytics?.equity_curve, analytics?.equity_curve_resolution)}</div>
      <div class="card journal-performance-card"></div>
      <div class="card"><div class="section-title">Capital Events</div><p class="subtitle">Deposits and withdrawals reported by Alpaca. Capital events change cash/equity, never trading P/L.</p>${capitalEvents().length ? `<div class="journal-events">${capitalEvents().map(event => `<span><strong>${escapeHtml(event.event_type)}</strong> ${journalMoney(event.amount_usd)} · ${formatLocalDate(event.occurred_at)}</span>`).join('')}</div>` : '<p class="journal-empty">No deposits or withdrawals reported by Alpaca.</p>'}</div>
      <div class="card"><div class="section-header"><div><div class="section-title">Trades</div><p class="subtitle">Showing up to 15 entries per page. Trade records are read-only; expand a position to see grouped rebuys and partial sells.</p></div></div><div class="journal-table-wrap"><table class="table"><thead><tr><th>Trade No.</th><th>Opened</th><th>Status</th><th>Instrument</th><th>Quantity</th><th>Buy-in Price</th><th>Sell Price</th><th>Net P/L</th><th>Return</th></tr></thead><tbody>${renderTradeRows(pageTrades())}</tbody></table></div>${renderTradePagination(tradePagination())}</div>
      <div class="card"><div class="section-title">Per-Instrument Analytics</div>${analytics?.stock_performance?.length ? `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Instrument</th><th>P/L</th><th>Trades</th><th>Win Rate</th><th>Average Return</th><th>Average Hold</th></tr></thead><tbody>${analytics.stock_performance.map(row => `<tr><td><strong>${escapeHtml(row.symbol)}</strong></td><td class="${journalClass(row.pnl_usd)}">${journalSigned(row.pnl_usd)}</td><td>${row.trades}</td><td>${journalPercent(row.win_rate_pct)}</td><td>${journalPercent(row.average_return_pct)}</td><td>${Number(row.average_holding_days).toFixed(1)} days</td></tr>`).join('')}</tbody></table></div>` : '<p class="journal-empty">Per-instrument analytics appear after completed trades.</p>'}</div>`;
    const chartCard = [...container.querySelectorAll('.card')].find(card => card.querySelector('.section-title')?.textContent.trim() === 'Equity Curve');
    const chartHeader = chartCard?.querySelector('.section-header');
    if (chartCard && chartHeader) {
        chartHeader.classList.add('journal-chart-header');
        chartHeader.querySelector('.section-title').textContent = journalChartMode === 'equity' ? 'Equity Curve' : journalChartMode === 'daily' ? 'Daily Performance' : 'Monthly Performance';
        chartHeader.insertAdjacentHTML('beforeend', journalChartControls(journalChartMode, journalPerformanceChartType));
        [...chartCard.children].filter(child => child !== chartHeader).forEach(child => child.remove());
        chartHeader.insertAdjacentHTML('afterend', renderJournalChart(analytics, journalChartMode, journalPerformanceChartType, analytics?.capital?.net_contributions_usd));
    }
    const performanceCard = container.querySelector('.journal-performance-card');
    if (performanceCard && chartCard) {
        performanceCard.innerHTML = renderPerformanceDetails(analytics);
        chartCard.insertAdjacentElement('afterend', performanceCard);
    }
    collapseJournalCardByTitle(container, 'Capital Events', 'Capital & cash movements', 'Deposits, withdrawals, and financing');
    collapseJournalCardByTitle(container, 'Trades', 'Trades', 'Read-only trade history');
    collapseJournalCardByTitle(container, 'Per-Instrument Analytics', 'Per-instrument analytics', 'Completed-trade breakdown by instrument');
    container.querySelectorAll('[data-trade-page]').forEach(button => button.addEventListener('click', async () => {
        const page = Number(button.dataset.tradePage);
        if (!Number.isInteger(page) || page < 1) return;
        const tradesSection = button.closest('[data-journal-section="Trades"]');
        const tradesTop = tradesSection?.getBoundingClientRect().top;
        journalTradePage = page;
        renderJournal();
        const refreshedTradesSection = container.querySelector('[data-journal-section="Trades"]');
        if (refreshedTradesSection && Number.isFinite(tradesTop)) {
            const offset = refreshedTradesSection.getBoundingClientRect().top - tradesTop;
            if (Math.abs(offset) > 0.5) window.scrollBy(0, offset);
        }
    }));
    container.querySelectorAll('[data-journal-chart-mode]').forEach(button => button.addEventListener('click', () => {
        journalChartMode = button.dataset.journalChartMode;
        renderJournal();
    }));
    container.querySelectorAll('[data-journal-chart-type]').forEach(button => button.addEventListener('click', () => {
        journalPerformanceChartType = button.dataset.journalChartType;
        renderJournal();
    }));
    attachInteractiveLineCharts(container);
}

    // Entry point for the Alpaca journal: analytics come from /api/alpaca/journal.
    function renderAlpacaJournal(data) {
        journalData = { analytics: data.analytics || {} };
        renderJournal();
    }
    return { renderJournal, renderAlpacaJournal, renderTradePagination };
}
