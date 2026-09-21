import { escapeHtml } from './escape.js';
import { money, parseOpenStockDate, formatLocalDate, formatLocalDateTime } from './formatting.js';

export function createJournal(context) {
    let editingTradeId = null;
    let editingIntent = 'create';
    let journalChartMode = 'equity';
    const { getStocks, getTrades, getTradePagination, getCapitalEvents, getAnalytics, request: af, showA, setJournalPage, reloadJournal, charts } = context;
    const stocks = getStocks;
    const trades = getTrades;
    const capitalEvents = getCapitalEvents;
    const tradingAnalytics = getAnalytics;
    const attachInteractiveLineCharts = charts.attachInteractiveLineCharts;
    const interactiveChartSvg = charts.interactiveChartSvg;

function journalMoney(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : `€${Number(value).toFixed(2)}`;
}

function journalPercent(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : `${Number(value).toFixed(2)}%`;
}

function journalSigned(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : `${Number(value) > 0 ? '+' : ''}${journalMoney(value)}`;
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

function journalInputNumber(value, decimals = 4) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? '' : Number(value).toFixed(decimals);
}

function journalKpi(label, displayValue, hint = '', cssClass = '') {
    return `<div class="journal-kpi" title="${escapeHtml(hint)}"><div>${escapeHtml(label)}</div><strong class="${cssClass}">${displayValue}</strong></div>`;
}

function performanceMetric(label, value, description = '', cssClass = '') {
    return `<div class="journal-performance-metric"><span>${escapeHtml(label)}</span><strong class="${cssClass}">${value}</strong>${description ? `<small>${escapeHtml(description)}</small>` : ''}</div>`;
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

function equityTimeLabel(value, range) {
    const date = parseOpenStockDate(value);
    if (!date) return '';
    if (range < 48 * 60 * 60 * 1000) return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Berlin' }).format(date);
    return new Intl.DateTimeFormat('en-GB', { day: '2-digit', month: 'short', timeZone: 'Europe/Berlin' }).format(date);
}

function renderEquityCurve(points, resolution = 'snapshot') {
    const clean = (points || []).filter(point => Number.isFinite(Number(point.equity_eur)) && parseOpenStockDate(point.at)).sort((left, right) => parseOpenStockDate(left.at).getTime() - parseOpenStockDate(right.at).getTime());
    if (clean.length < 2) return '<div class="journal-empty">Insufficient data — equity snapshots begin after initial capital is recorded.</div>';
    const values = clean.map(point => Number(point.equity_eur));
    const min = Math.min(...values), max = Math.max(...values), padding = Math.max((max - min) * 0.08, 1);
    const width = 800, height = 260, left = 68, right = 12, top = 10, bottom = 32, plotWidth = width - left - right, plotHeight = height - top - bottom;
    const { low, high, values: axisValues } = equityAxisBounds(min - padding, max + padding);
    const times = clean.map(point => parseOpenStockDate(point.at).getTime());
    const firstTime = times[0], timeRange = times[times.length - 1] - firstTime || 1;
    const coordinates = clean.map((point, index) => ({ point, value: Number(point.equity_eur), x: left + (times[index] - firstTime) / timeRange * plotWidth, y: top + plotHeight - ((Number(point.equity_eur) - low) / (high - low || 1) * plotHeight) }));
    const line = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const hoverPoints = coordinates.map(({ point, value, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: `Equity ${journalMoney(value)}`, secondary: formatLocalDateTime(point.at), tertiary: `Cash ${journalMoney(point.cash_eur)} · Positions ${journalMoney(point.position_value_eur)}`, lineGap: 13 }));
    const ariaLabel = `Equity curve using ${resolution} closing samples from ${formatLocalDate(clean[0].at)} to ${formatLocalDate(clean[clean.length - 1].at)}`;
    const yTicks = axisValues.map(value => ({ value, y: top + plotHeight - ((value - low) / (high - low || 1) * plotHeight) }));
    const xTicks = [0, .25, .5, .75, 1].map(ratio => ({ ratio, x: left + plotWidth * ratio, label: equityTimeLabel(new Date(firstTime + timeRange * ratio).toISOString(), timeRange) }));
    const yAxis = yTicks.map(({ y }) => `<line class="equity-grid-line" x1="${left}" x2="${width - right}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`).join('');
    const xAxis = xTicks.map(({ x }) => `<line class="equity-axis-tick" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${top + plotHeight}" y2="${top + plotHeight + 5}"/>`).join('');
    const chart = interactiveChartSvg({ className: 'journal-equity-curve', width, height, grid: `<g class="equity-axis">${yAxis}${xAxis}<line class="equity-axis-baseline" x1="${left}" x2="${width - right}" y1="${top + plotHeight}" y2="${top + plotHeight}"/></g>`, lineClass: 'equity-line', linePoints: line, hoverPoints, ariaLabel });
    const yLabels = yTicks.map(({ value, y }) => `<span style="top:${(y / height * 100).toFixed(3)}%">€${Math.round(value).toLocaleString('en-GB')}</span>`).join('');
    const xLabels = xTicks.map(({ ratio, label }) => `<span style="left:${(left / width * 100 + ratio * plotWidth / width * 100).toFixed(3)}%">${escapeHtml(label)}</span>`).join('');
    return `<div class="journal-chart-scale"><span>${clean.length} ${resolution} samples</span><span>Hover or focus for exact values</span></div><div class="journal-equity-chart"><div class="journal-equity-y-labels" aria-hidden="true">${yLabels}</div>${chart}<div class="journal-equity-x-labels" aria-hidden="true">${xLabels}</div></div>`;
}

function renderPerformanceCurve(rows, period, capitalBase) {
    const clean = (rows || []).filter(row => Number.isFinite(Number(row.pnl_eur)) && row.day).map(row => ({ ...row, at: `${row.day}T12:00:00Z`, value: Number(row.pnl_eur) })).filter(row => parseOpenStockDate(row.at)).sort((left, right) => parseOpenStockDate(left.at).getTime() - parseOpenStockDate(right.at).getTime());
    if (clean.length < 2) return '<div class="journal-empty">At least two completed periods are needed for this chart.</div>';
    const values = clean.map(point => point.value);
    const min = Math.min(0, ...values), max = Math.max(0, ...values), padding = Math.max((max - min) * .1, 1);
    const width = 800, height = 260, left = 68, right = 12, top = 10, bottom = 32, plotWidth = width - left - right, plotHeight = height - top - bottom;
    const { low, high, values: axisValues } = equityAxisBounds(min - padding, max + padding);
    const times = clean.map(point => parseOpenStockDate(point.at).getTime());
    const firstTime = times[0], timeRange = times[times.length - 1] - firstTime || 1;
    const coordinates = clean.map((point, index) => ({ point, value: point.value, x: left + (times[index] - firstTime) / timeRange * plotWidth, y: top + plotHeight - ((point.value - low) / (high - low || 1) * plotHeight) }));
    const line = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const periodName = period === 'daily' ? 'Daily' : 'Monthly';
    const hoverPoints = coordinates.map(({ point, value, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: `${periodName} P/L ${journalSigned(value)}`, secondary: formatLocalDate(point.at), tertiary: `${journalPercent(capitalBase > 0 ? value / capitalBase * 100 : null)} of capital · ${point.trades} ${point.trades === 1 ? 'trade' : 'trades'}`, lineGap: 13 }));
    const yTicks = axisValues.map(value => ({ value, y: top + plotHeight - ((value - low) / (high - low || 1) * plotHeight) }));
    const xTicks = [0, .25, .5, .75, 1].map(ratio => ({ ratio, x: left + plotWidth * ratio, label: equityTimeLabel(new Date(firstTime + timeRange * ratio).toISOString(), timeRange) }));
    const yAxis = yTicks.map(({ y }) => `<line class="equity-grid-line" x1="${left}" x2="${width - right}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`).join('');
    const xAxis = xTicks.map(({ x }) => `<line class="equity-axis-tick" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${top + plotHeight}" y2="${top + plotHeight + 5}"/>`).join('');
    const chart = interactiveChartSvg({ className: 'journal-equity-curve', width, height, grid: `<g class="equity-axis">${yAxis}${xAxis}<line class="equity-axis-baseline" x1="${left}" x2="${width - right}" y1="${top + plotHeight - ((0 - low) / (high - low || 1) * plotHeight)}" y2="${top + plotHeight - ((0 - low) / (high - low || 1) * plotHeight)}"/></g>`, lineClass: `performance-line ${period}`, linePoints: line, hoverPoints, ariaLabel: `${periodName} realized P/L chart in EUR` });
    const yLabels = yTicks.map(({ value, y }) => `<span style="top:${(y / height * 100).toFixed(3)}%">${value > 0 ? '+' : ''}€${Math.round(value).toLocaleString('en-GB')}</span>`).join('');
    const xLabels = xTicks.map(({ ratio, label }) => `<span style="left:${(left / width * 100 + ratio * plotWidth / width * 100).toFixed(3)}%">${escapeHtml(label)}</span>`).join('');
    return `<div class="journal-chart-scale"><span>${clean.length} completed ${period} periods</span><span>Hover or focus for € and % of capital</span></div><div class="journal-equity-chart"><div class="journal-equity-y-labels" aria-hidden="true">${yLabels}</div>${chart}<div class="journal-equity-x-labels" aria-hidden="true">${xLabels}</div></div>`;
}

function journalChartControls(active) {
    return `<div class="history-ranges journal-chart-controls" role="group" aria-label="Journal chart metric">${[['equity', 'Equity'], ['daily', 'Daily'], ['monthly', 'Monthly']].map(([mode, label]) => `<button type="button" class="history-range ${mode === active ? 'active' : ''}" data-journal-chart-mode="${mode}">${label}</button>`).join('')}</div>`;
}

function renderJournalChart(analytics, mode) {
    if (mode === 'daily') return renderPerformanceCurve(analytics?.daily_performance, 'daily', Number(analytics?.capital?.net_contributions_eur));
    if (mode === 'monthly') return renderPerformanceCurve(analytics?.monthly_performance?.map(row => ({ ...row, day: `${row.month}-15` })), 'monthly', Number(analytics?.capital?.net_contributions_eur));
    return renderEquityCurve(analytics?.equity_curve, analytics?.equity_curve_resolution);
}

function renderTradeRows(items) {
    if (!items.length) return '<tr><td colspan="8" style="color:#64748b">No trades recorded.</td></tr>';
    return items.map(trade => `<tr><td>${escapeHtml(trade.trade_id)}</td><td><strong>${escapeHtml(trade.symbol)}</strong></td><td><span class="badge ${trade.is_closed ? 'sold' : 'owned'}">${trade.is_closed ? 'CLOSED' : 'OPEN'}</span></td><td>${formatLocalDate(trade.opened_at)}</td><td>${trade.is_closed ? journalMoney(trade.dividend_eur) : '—'}</td><td class="${journalClass(trade.net_pnl_eur)}">${journalSigned(trade.net_pnl_eur)}</td><td class="${journalClass(trade.return_pct)}">${journalPercent(trade.return_pct)}</td><td><button class="btn btn-sm btn-secondary" data-trade-action="edit" data-trade-id="${escapeHtml(trade.id)}">View / Edit</button>${trade.is_closed ? '' : ` <button class="btn btn-sm btn-success" data-trade-action="close" data-trade-id="${escapeHtml(trade.id)}">Close</button>`}</td></tr>`).join('');
}

function renderTradePagination(pagination) {
    const total = Number(pagination?.total) || 0;
    const page = Number(pagination?.page) || 1;
    const totalPages = Number(pagination?.total_pages) || 1;
    if (!total) return '';
    return `<div class="journal-pagination" aria-label="Trade history pages"><span>${total} ${total === 1 ? 'trade' : 'trades'} · ${pagination.page_size} per page · Page ${page} of ${totalPages}</span><div class="btn-group"><button type="button" class="btn btn-sm btn-secondary" data-trade-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>Previous</button><button type="button" class="btn btn-sm btn-secondary" data-trade-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>Next</button></div></div>`;
}

function renderDailyPerformance(rows = [], capitalBase = 0) {
    if (!rows.length) return '<p class="journal-empty">Daily results appear after completed trades.</p>';
    return `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Day</th><th>P/L</th><th>Return</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody>${rows.slice(-14).reverse().map(row => `<tr><td>${escapeHtml(row.day)}</td><td class="${journalClass(row.pnl_eur)}">${journalSigned(row.pnl_eur)}</td><td class="${journalClass(row.pnl_eur)}">${journalPercent(capitalBase > 0 ? Number(row.pnl_eur) / capitalBase * 100 : null)}</td><td>${row.trades}</td><td>${journalPercent(row.wins / row.trades * 100)}</td></tr>`).join('')}</tbody></table></div>`;
}

function renderMonthlyPerformance(rows = [], capitalBase = 0) {
    if (!rows.length) return '<p class="journal-empty">Monthly results appear after completed trades.</p>';
    return `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Month</th><th>P/L</th><th>Return</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody>${rows.slice(-12).reverse().map(row => `<tr><td>${escapeHtml(row.month)}</td><td class="${journalClass(row.pnl_eur)}">${journalSigned(row.pnl_eur)}</td><td class="${journalClass(row.pnl_eur)}">${journalPercent(capitalBase > 0 ? Number(row.pnl_eur) / capitalBase * 100 : null)}</td><td>${row.trades}</td><td>${journalPercent(row.wins / row.trades * 100)}</td></tr>`).join('')}</tbody></table></div>`;
}

function renderPerformanceDetails(analytics, mode) {
    const capital = analytics?.capital || {};
    const performance = analytics?.performance || {};
    const tradeStats = analytics?.trades || {};
    const risk = analytics?.risk || {};
    const realized = analytics?.realized || {};
    const daily = mode === 'daily';
    const monthly = mode === 'monthly';
    const tableHeading = daily ? 'Daily performance table' : monthly ? 'Monthly performance table' : 'Performance tables';
    const capitalBase = Number(capital.net_contributions_eur);
    const table = daily ? renderDailyPerformance(analytics?.daily_performance, capitalBase) : monthly ? renderMonthlyPerformance(analytics?.monthly_performance, capitalBase) : `<div class="journal-performance-section"><div class="journal-performance-heading">Daily performance <span>Latest 14 trading days · return on contributed capital</span></div>${renderDailyPerformance(analytics?.daily_performance, capitalBase)}</div><div class="journal-performance-section"><div class="journal-performance-heading">Monthly performance <span>Completed months · return on contributed capital</span></div>${renderMonthlyPerformance(analytics?.monthly_performance, capitalBase)}</div>`;
    return `<details class="journal-collapsible journal-performance-details"><summary><span>Performance details</span><small>Returns, compounding and trade quality</small></summary><div class="journal-collapsible-content"><div class="journal-performance-overview"><div><p class="journal-eyebrow">Capital growth</p><strong class="${journalClass(performance.investment_profit_eur)}">${journalSigned(performance.investment_profit_eur)}</strong><span>investment profit on ${journalMoney(capital.net_contributions_eur)} contributed</span></div><div><p class="journal-eyebrow">Growth multiple</p><strong>${performance.growth_multiple == null ? 'N/A' : `${Number(performance.growth_multiple).toFixed(3)}×`}</strong><span>current capital relative to contributions</span></div></div><div class="journal-performance-metric-grid">${performanceMetric('Cumulative return', journalPercent(capital.current_return_pct), 'Profit on contributed capital', journalClass(capital.current_return_pct))}${performanceMetric('Annualized return', journalPercent(performance.annualized_return_pct), 'Observed return, compounded to one year', journalClass(performance.annualized_return_pct))}${performanceMetric('Monthly compound rate', journalPercent(performance.monthly_compounding_rate_pct), 'Equivalent monthly rate from annualized return', journalClass(performance.monthly_compounding_rate_pct))}${performanceMetric('1-year capital illustration', journalMoney(performance.projected_one_year_capital_eur), `Change ${journalSigned(performance.estimated_yearly_profit_eur)} at the observed annualized rate`, journalClass(performance.estimated_yearly_profit_eur))}</div><p class="journal-compounding-note">${escapeHtml(performance.estimate_methodology || 'Compounding metrics become available when sufficient capital history exists.')}</p><div class="journal-stat-strip">${performanceMetric('Net realized P/L', journalSigned(realized.net_eur), '', journalClass(realized.net_eur))}${performanceMetric('Avg. trade', journalSigned(tradeStats.expectancy_eur), '', journalClass(tradeStats.expectancy_eur))}${performanceMetric('Profit factor', tradeStats.profit_factor == null ? 'N/A' : Number(tradeStats.profit_factor).toFixed(2))}${performanceMetric('Max drawdown', journalPercent(risk.max_drawdown_pct), '', journalClass(risk.max_drawdown_pct))}</div><div class="journal-performance-table"><div class="journal-performance-heading">${tableHeading}<span>${daily ? 'Latest 14 trading days' : monthly ? 'Latest 12 completed months' : 'Completed-trade results'}</span></div>${table}</div></div></details>`;
}

function collapseJournalCard(container, selector, label, description = '') {
    const card = typeof selector === 'string' ? container.querySelector(selector) : selector;
    if (!card || card.querySelector(':scope > details.journal-collapsible')) return;
    const details = document.createElement('details');
    details.className = 'journal-collapsible';
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
    queueJournalDividendEnhancements(container);
    const analytics = tradingAnalytics();
    const capital = analytics?.capital || {};
    const realized = analytics?.realized || {};
    const tradeStats = analytics?.trades || {};
    const performance = analytics?.performance || {};
    const risk = analytics?.risk || {};
    const edit = editingTradeId ? getTrades().find(trade => String(trade.id) === String(editingTradeId)) : null;
    const isClosing = Boolean(edit && !edit.is_closed && editingIntent === 'close');
    const editorTitle = isClosing ? `Close ${escapeHtml(edit.trade_id)}` : edit ? `View / Edit ${escapeHtml(edit.trade_id)}` : 'Record Trade';
    const editorContext = edit ? `Trade ${escapeHtml(edit.trade_id)} · ${edit.is_closed ? 'CLOSED' : 'OPEN'} · ID ${escapeHtml(edit.id)}${isClosing ? ' · Enter the quantity to sell; the unsold balance remains open.' : ''}` : 'Create a trade. New purchases merge into an existing open position for the same stock using a weighted average.';
    const selectableStocks = getStocks().filter(stock => (Number(stock.archived) !== 1 && Number(stock.enabled) === 1) || String(edit?.stock_id) === String(stock.id));
    const options = selectableStocks.map(stock => `<option value="${stock.id}" ${String(edit?.stock_id) === String(stock.id) ? 'selected' : ''}>${escapeHtml(stock.symbol)} — ${escapeHtml(stock.name || '')}${stock.archived ? ' (archived history)' : ''}</option>`).join('');
    const currentPnl = (Number(realized.net_eur) || 0) + (Number(capital.unrealized_pnl_eur) || 0);
    const initialCapital = getCapitalEvents().find(item => item.event_type === 'INITIAL');
    const initialCapitalExists = Boolean(initialCapital);
    const capitalHelp = initialCapitalExists
        ? `Initial capital was set on ${formatLocalDate(initialCapital.occurred_at)}. To align with your broker cash balance, add the difference as a Deposit (or Withdrawal); it does not change trading P/L.`
        : 'Set Initial Capital once to establish the starting balance. Later cash changes should be recorded as Deposits or Withdrawals.';
    container.innerHTML = `<header><h1>📓 Trading Journal</h1></header><div id="ac"></div>${analytics?.equity_available ? '' : '<div class="alert-box info">Add an <strong>Initial capital</strong> event below to calculate current capital, return, exposure and an equity curve.</div>'}
      <div class="journal-kpi-grid">${journalKpi('Current Capital', journalMoney(capital.current_eur), 'Cash plus priced open positions.')}${journalKpi('Net P/L', journalSigned(currentPnl), 'Realized plus unrealized P/L; capital events excluded.', journalClass(currentPnl))}${journalKpi('Return', journalPercent(capital.current_return_pct), 'Current capital less net owner contributions divided by net owner contributions.', journalClass(capital.current_return_pct))}${journalKpi('YTD P/L', journalSigned(performance.ytd_pnl_eur), 'Net P/L from completed trades this calendar year.', journalClass(performance.ytd_pnl_eur))}${journalKpi('Est. Yearly Profit', journalSigned(performance.estimated_yearly_profit_eur), 'Observed-rate annual profit illustration.', journalClass(performance.estimated_yearly_profit_eur))}${journalKpi('Annualized Return', journalPercent(performance.annualized_return_pct), 'Observed return, compounded to one year.', journalClass(performance.annualized_return_pct))}${journalKpi('Win Rate', journalPercent(tradeStats.win_rate_pct), 'Winning completed trades divided by completed trades.')}${journalKpi('Max Drawdown', journalPercent(risk.max_drawdown_pct), 'Largest peak-to-trough decline in equity snapshots.', journalClass(risk.max_drawdown_pct))}${journalKpi('Available Cash', journalMoney(capital.available_cash_eur), 'Capital events less purchases/fees plus sales.')}${journalKpi('Open Positions', String(tradeStats.open ?? 0), 'Lots not yet closed.')}</div>
      <div class="card"><div class="section-header"><div class="section-title">Equity Curve</div></div>${renderEquityCurve(analytics?.equity_curve, analytics?.equity_curve_resolution)}</div>
      <div class="card journal-performance-card"></div>
      <div class="card journal-trade-editor ${isClosing ? 'is-closing' : edit ? 'is-editing' : ''}" id="journal-trade-editor" tabindex="-1"><div class="section-header"><div><div class="section-title">${editorTitle}</div><p class="subtitle" aria-live="polite">${editorContext}</p></div>${edit ? '<button type="button" class="btn btn-sm btn-secondary" data-trade-action="cancel">Cancel</button>' : ''}</div><form id="trade-form"><div class="form-row"><div class="form-group"><label for="trade-stock">Stock</label><select id="trade-stock" name="stock_id" required ${isClosing ? 'disabled' : ''}>${options}</select></div><div class="form-group"><label for="trade-opened">Opened</label><input id="trade-opened" name="opened_at" type="datetime-local" step="1" required ${isClosing ? 'readonly' : ''} value="${journalInputDate(edit?.opened_at) || journalInputDate(new Date().toISOString())}"></div><div class="form-group"><label for="trade-quantity">Open Quantity</label><input id="trade-quantity" name="quantity" type="number" min=".000001" step=".000001" required ${isClosing ? 'readonly' : ''} value="${escapeHtml(edit?.quantity ?? '')}"></div></div><div class="form-row"><div class="form-group"><label for="trade-currency">Currency</label><input id="trade-currency" name="currency" maxlength="3" required ${isClosing ? 'readonly' : ''} value="${escapeHtml(edit?.currency || 'EUR')}"></div><div class="form-group"><label for="trade-entry-price">Buy Price</label><input id="trade-entry-price" name="entry_price" type="number" min=".01" step=".01" required ${isClosing ? 'readonly' : ''} value="${escapeHtml(journalInputNumber(edit?.entry_price, 2))}"></div><div class="form-group"><label for="trade-entry-fx">EUR Rate at Buy</label><input id="trade-entry-fx" name="entry_fx_rate" type="number" min=".000001" step=".000001" required ${isClosing ? 'readonly' : ''} value="${escapeHtml(edit?.entry_fx_rate ?? 1)}"></div><div class="form-group"><label for="trade-entry-fee">Buy Fees (€)</label><input id="trade-entry-fee" name="entry_fee_eur" type="number" min="0" step=".01" ${isClosing ? 'readonly' : ''} value="${escapeHtml(edit?.entry_fee_eur ?? 1)}"></div></div><details ${edit?.closed_at || isClosing ? 'open' : ''}><summary>${edit?.closed_at ? 'Closed trade details' : 'Close trade (optional)'}</summary><div class="form-row" style="margin-top:.75rem">${isClosing ? `<div class="form-group"><label for="trade-close-quantity">Quantity to sell</label><input id="trade-close-quantity" name="close_quantity" type="number" min=".000001" max="${escapeHtml(edit.quantity)}" step=".000001" required value="${escapeHtml(edit.quantity)}"></div>` : ''}<div class="form-group"><label for="trade-closed">Closed</label><input id="trade-closed" name="closed_at" type="datetime-local" step="1" ${isClosing ? 'required' : ''} value="${journalInputDate(edit?.closed_at || (isClosing ? new Date().toISOString() : ''))}"></div><div class="form-group"><label for="trade-exit-price">Sell Price</label><input id="trade-exit-price" name="exit_price" type="number" min=".0001" step=".0001" ${isClosing ? 'required' : ''} value="${escapeHtml(journalInputNumber(edit?.exit_price))}"></div><div class="form-group"><label for="trade-exit-fx">EUR Rate at Sell</label><input id="trade-exit-fx" name="exit_fx_rate" type="number" min=".000001" step=".000001" ${isClosing ? 'required' : ''} value="${escapeHtml(edit?.exit_fx_rate ?? 1)}"></div><div class="form-group"><label for="trade-exit-fee">Sell Fees (€)</label><input id="trade-exit-fee" name="exit_fee_eur" type="number" min="0" step=".01" value="${escapeHtml(edit?.exit_fee_eur ?? 0)}"></div></div></details><div class="form-row" style="margin-top:.75rem"><div class="form-group"><label for="trade-strategy">Strategy / Reason</label><input id="trade-strategy" name="strategy" value="${escapeHtml(edit?.strategy || '')}"></div><div class="form-group"><label for="trade-notes">Notes</label><input id="trade-notes" name="notes" value="${escapeHtml(edit?.notes || '')}"></div></div><div class="btn-group"><button type="submit" class="btn btn-primary">${isClosing ? 'Record Sale' : edit ? 'Save Changes' : 'Record Trade'}</button>${edit ? '<button type="button" class="btn btn-secondary" data-trade-action="cancel">Cancel</button>' : ''}</div></form></div>
      <div class="card"><div class="section-title">Capital Events</div><p class="subtitle">Capital events change cash/equity, never trading P/L.</p><form class="journal-capital-form" id="capital-event-form"><label class="sr-only" for="capital-type">Event type</label><select id="capital-type" name="event_type"><option value="INITIAL" ${initialCapitalExists ? 'disabled' : ''}>Initial Capital${initialCapitalExists ? ' (already recorded)' : ''}</option><option value="DEPOSIT" ${initialCapitalExists ? 'selected' : ''}>Deposit</option><option value="WITHDRAWAL">Withdrawal</option><option value="FINANCING">Financing / Debt</option></select><label class="sr-only" for="capital-amount">Amount</label><input id="capital-amount" name="amount_eur" type="number" min=".01" step=".01" placeholder="Amount €" required><label class="sr-only" for="capital-date">Date and time</label><input id="capital-date" name="occurred_at" type="datetime-local" value="${journalInputDate(new Date().toISOString())}" required><label class="sr-only" for="capital-notes">Note</label><input id="capital-notes" name="notes" placeholder="Optional note"><button type="submit" class="btn btn-primary">Add Event</button></form><p class="journal-help" id="capital-event-help" aria-live="polite">${capitalHelp}</p>${getCapitalEvents().length ? `<div class="journal-events">${getCapitalEvents().map(event => `<span><strong>${escapeHtml(event.event_type)}</strong> ${journalMoney(event.amount_eur)} · ${formatLocalDate(event.occurred_at)}</span>`).join('')}</div>` : '<p class="journal-empty">No capital events recorded.</p>'}</div>
      <div class="card"><div class="section-header"><div><div class="section-title">Trades</div><p class="subtitle">Showing up to 15 entries per page.</p></div></div><div class="journal-table-wrap"><table class="table"><thead><tr><th>Trade ID</th><th>Stock</th><th>Status</th><th>Opened</th><th>Net P/L</th><th>Return</th><th></th></tr></thead><tbody>${renderTradeRows(getTrades())}</tbody></table></div>${renderTradePagination(getTradePagination())}</div>
      <div class="card"><div class="section-title">Per-Stock Analytics</div>${analytics?.stock_performance?.length ? `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Stock</th><th>P/L</th><th>Trades</th><th>Win Rate</th><th>Average Return</th><th>Average Hold</th></tr></thead><tbody>${analytics.stock_performance.map(row => `<tr><td><strong>${escapeHtml(row.symbol)}</strong></td><td class="${journalClass(row.pnl_eur)}">${journalSigned(row.pnl_eur)}</td><td>${row.trades}</td><td>${journalPercent(row.win_rate_pct)}</td><td>${journalPercent(row.average_return_pct)}</td><td>${Number(row.average_holding_days).toFixed(1)} days</td></tr>`).join('')}</tbody></table></div>` : '<p class="journal-empty">Per-stock analytics appear after completed trades.</p>'}</div><div class="card"><p class="journal-help" id="journal-definitions">Definitions: Net P/L = sell EUR value − buy EUR value − entry fees − exit fees. Return = net P/L ÷ (buy EUR value + entry fees). Realized metrics use closed trades only; unrealized P/L uses current monitored EUR prices for open positions. Annualized estimates need at least 30 calendar days from initial capital; volatility and Sharpe require at least 30 equity observations.</p></div>`;
    const kpiGrid = container.querySelector('.journal-kpi-grid');
    if (kpiGrid) kpiGrid.insertAdjacentHTML('beforeend', `${journalKpi('Avg Daily', `${journalSigned(performance.average_daily_pnl_eur)} · ${journalPercent(performance.average_daily_return_pct)}`, 'Average realized P/L per completed trading day and its percentage of contributed capital.', journalClass(performance.average_daily_pnl_eur))}${journalKpi('Avg Monthly', `${journalSigned(performance.average_monthly_pnl_eur)} · ${journalPercent(performance.average_monthly_return_pct)}`, 'Average realized P/L per completed month and its percentage of contributed capital.', journalClass(performance.average_monthly_pnl_eur))}`);
    const chartCard = [...container.querySelectorAll('.card')].find(card => card.querySelector('.section-title')?.textContent.trim() === 'Equity Curve');
    const chartHeader = chartCard?.querySelector('.section-header');
    if (chartCard && chartHeader) {
        chartHeader.classList.add('journal-chart-header');
        chartHeader.querySelector('.section-title').textContent = journalChartMode === 'equity' ? 'Equity Curve' : journalChartMode === 'daily' ? 'Daily Performance' : 'Monthly Performance';
        chartHeader.insertAdjacentHTML('beforeend', journalChartControls(journalChartMode));
        [...chartCard.children].filter(child => child !== chartHeader).forEach(child => child.remove());
        chartHeader.insertAdjacentHTML('afterend', renderJournalChart(analytics, journalChartMode));
    }
    const performanceCard = container.querySelector('.journal-performance-card');
    if (performanceCard && chartCard) {
        performanceCard.innerHTML = renderPerformanceDetails(analytics, journalChartMode);
        chartCard.insertAdjacentElement('afterend', performanceCard);
    }
    if (!edit) collapseJournalCard(container, '#journal-trade-editor', 'Add a trade', 'Record a purchase or an historical lot');
    collapseJournalCardByTitle(container, 'Capital Events', 'Capital & cash movements', 'Deposits, withdrawals, and financing');
    collapseJournalCardByTitle(container, 'Per-Stock Analytics', 'Per-stock analytics', 'Completed-trade breakdown by stock');
    container.querySelectorAll('[data-trade-action]').forEach(button => button.addEventListener('click', () => ({ edit: editTrade, close: closeTrade, cancel: cancelTradeEdit }[button.dataset.tradeAction])?.(button.dataset.tradeId)));
    container.querySelectorAll('[data-trade-page]').forEach(button => button.addEventListener('click', () => {
        const page = Number(button.dataset.tradePage);
        if (Number.isInteger(page) && page > 0) { setJournalPage(page); reloadJournal(page); }
    }));
    container.querySelectorAll('[data-journal-chart-mode]').forEach(button => button.addEventListener('click', () => {
        journalChartMode = button.dataset.journalChartMode;
        renderJournal();
    }));
    container.querySelector('#trade-form')?.addEventListener('submit', saveTrade);
    container.querySelector('#capital-event-form')?.addEventListener('submit', saveCapitalEvent);
    if (edit) focusTradeEditor(isClosing);
    attachInteractiveLineCharts(container);
}

function queueJournalDividendEnhancements(container) {
    queueMicrotask(() => {
        const closeDetails = container.querySelector('#journal-trade-editor details');
        const closeRow = closeDetails?.querySelector('.form-row');
        if (closeRow && !closeRow.querySelector('[name="dividend_eur"]')) {
            const edit = editingTradeId ? getTrades().find(trade => String(trade.id) === String(editingTradeId)) : null;
            const dividends = Number.isFinite(Number(edit?.dividend_eur)) ? Number(edit.dividend_eur) : 0;
            closeRow.insertAdjacentHTML('beforeend', `<div class="form-group"><label>Dividends received (€)</label><input name="dividend_eur" type="number" min="0" step=".01" value="${dividends}"></div>`);
        }

        const tradesCard = [...container.querySelectorAll('.card')].find(card => card.querySelector('.section-title')?.textContent.trim() === 'Trades');
        const tradeHeaders = tradesCard?.querySelector('thead tr');
        if (tradeHeaders && ![...tradeHeaders.children].some(header => header.textContent === 'Dividends')) {
            const dividendHeader = document.createElement('th');
            dividendHeader.textContent = 'Dividends';
            const netPnlHeader = [...tradeHeaders.children].find(header => header.textContent === 'Net P/L');
            tradeHeaders.insertBefore(dividendHeader, netPnlHeader || null);
        }

        const statsCard = [...container.querySelectorAll('.card')].find(card => card.querySelector('.section-title')?.textContent.trim() === 'Trade Statistics');
        const metrics = statsCard?.querySelector('.journal-metrics');
        if (metrics && ![...metrics.querySelectorAll('dt')].some(term => term.textContent === 'Dividends received')) {
            metrics.insertAdjacentHTML('beforeend', `<dt>Dividends received</dt><dd class="positive">${journalSigned(getAnalytics()?.realized?.dividends_eur)}</dd>`);
        }

        const help = container.querySelector('#journal-definitions');
        if (help) help.textContent = 'Definitions: Net P/L = sell EUR value + received dividends − buy EUR value − entry fees − exit fees. Return = net P/L ÷ (buy EUR value + entry fees). Realized metrics use closed trades only; unrealized P/L uses current monitored EUR prices for open positions. Annualized estimates need at least 30 calendar days from initial capital; volatility and Sharpe require at least 30 equity observations.';
    });
}

function formDataObject(form) {
    return Object.fromEntries(new FormData(form).entries());
}

async function saveTrade(event) {
    event.preventDefault();
    const body = formDataObject(event.currentTarget);
    if (!body.closed_at) {
        delete body.exit_price;
        delete body.exit_fx_rate;
        delete body.exit_fee_eur;
        delete body.dividend_eur;
    }
    const closing = editingIntent === 'close';
    const creating = !editingTradeId;
    const path = closing ? `/trades/${editingTradeId}/close` : editingTradeId ? `/trades/${editingTradeId}` : '/trades';
    const method = closing ? 'POST' : editingTradeId ? 'PUT' : 'POST';
    const result = await af(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!result) return;
    editingTradeId = null;
    editingIntent = 'create';
    if (creating) setJournalPage(1);
    await reloadJournal();
    showA(result.message, 'success');
}

function editTrade(id) {
    const trade = getTrades().find(item => String(item.id) === String(id));
    if (!trade) return;
    editingTradeId = String(trade.id);
    editingIntent = 'edit';
    renderJournal();
}

function cancelTradeEdit() {
    editingTradeId = null;
    editingIntent = 'create';
    renderJournal();
}

function closeTrade(id) {
    const trade = getTrades().find(item => String(item.id) === String(id));
    if (!trade || trade.is_closed) return;
    editingTradeId = String(trade.id);
    editingIntent = 'close';
    renderJournal();
}

function focusTradeEditor(focusCloseDetails = false) {
    const editor = document.getElementById('journal-trade-editor');
    if (!editor) return;
    requestAnimationFrame(() => {
        editor.scrollIntoView({ behavior: 'smooth', block: 'start' });
        editor.focus({ preventScroll: true });
        const target = focusCloseDetails ? editor.querySelector('#trade-closed') : editor.querySelector('input, select');
        target?.focus({ preventScroll: true });
    });
}

async function saveCapitalEvent(event) {
    event.preventDefault();
    const help = document.getElementById('capital-event-help');
    const result = await af('/capital-events', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formDataObject(event.currentTarget)) }, message => {
        if (help) help.textContent = message;
    });
    if (!result) return;
    await reloadJournal();
    showA('Capital event recorded', 'success');
}

    return { renderJournal, saveTrade, editTrade, cancelTradeEdit, closeTrade, saveCapitalEvent };
}
