import { escapeHtml } from './escape.js';
import { money, parseOpenStockDate, formatLocalDate, formatLocalDateTime } from './formatting.js';

export function createJournal(context) {
    let editingTradeId = null;
    let editingIntent = 'create';
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

function renderEquityCurve(points, resolution = 'snapshot') {
    if (!points || points.length < 2) return '<div class="journal-empty">Insufficient data — equity snapshots begin after initial capital is recorded.</div>';
    const values = points.map(point => Number(point.equity_eur));
    const min = Math.min(...values), max = Math.max(...values), padding = Math.max((max - min) * 0.08, 1);
    const width = 800, height = 220, low = min - padding, high = max + padding;
    const times = points.map(point => parseOpenStockDate(point.at).getTime());
    const firstTime = times[0], timeRange = times[times.length - 1] - firstTime || 1;
    const coordinates = points.map((point, index) => ({ point, value: Number(point.equity_eur), x: (times[index] - firstTime) / timeRange * width, y: height - ((Number(point.equity_eur) - low) / (high - low || 1) * height) }));
    const line = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const hoverPoints = coordinates.map(({ point, value, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: `Equity ${journalMoney(value)}`, secondary: formatLocalDateTime(point.at), tertiary: `Cash ${journalMoney(point.cash_eur)} · Positions ${journalMoney(point.position_value_eur)}`, lineGap: 13 }));
    const ariaLabel = `Equity curve using ${resolution} closing samples from ${formatLocalDate(points[0].at)} to ${formatLocalDate(points[points.length - 1].at)}`;
    const chart = interactiveChartSvg({ className: 'journal-equity-curve', width, height, grid: `<path d="M0 55H${width} M0 110H${width} M0 165H${width}"/>`, lineClass: 'equity-line', linePoints: line, hoverPoints, ariaLabel });
    return `<div class="journal-chart-scale"><span>${points.length} ${resolution} samples</span><span>Hover or focus the line for exact values</span></div><div class="journal-chart-labels"><span>€${high.toFixed(0)}</span><span>€${low.toFixed(0)}</span></div>${chart}<div class="journal-chart-labels"><span>${formatLocalDate(points[0].at)}</span><span>Peak ${journalMoney(max)}</span><span>${formatLocalDate(points[points.length - 1].at)}</span></div>`;
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
    return `<div class="journal-pagination" aria-label="Trade history pages"><span>${total} ${total === 1 ? 'trade' : 'trades'} · Page ${page} of ${totalPages}</span><div class="btn-group"><button type="button" class="btn btn-sm btn-secondary" data-trade-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>Previous</button><button type="button" class="btn btn-sm btn-secondary" data-trade-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>Next</button></div></div>`;
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
    const editorContext = edit ? `Trade ${escapeHtml(edit.trade_id)} · ${edit.is_closed ? 'CLOSED' : 'OPEN'} · ID ${escapeHtml(edit.id)}` : 'Create a trade. New purchases merge into an existing open position for the same stock using a weighted average.';
    const selectableStocks = getStocks().filter(stock => (Number(stock.archived) !== 1 && Number(stock.enabled) === 1) || String(edit?.stock_id) === String(stock.id));
    const options = selectableStocks.map(stock => `<option value="${stock.id}" ${String(edit?.stock_id) === String(stock.id) ? 'selected' : ''}>${escapeHtml(stock.symbol)} — ${escapeHtml(stock.name || '')}${stock.archived ? ' (archived history)' : ''}</option>`).join('');
    const currentPnl = (Number(realized.net_eur) || 0) + (Number(capital.unrealized_pnl_eur) || 0);
    const initialCapital = getCapitalEvents().find(item => item.event_type === 'INITIAL');
    const initialCapitalExists = Boolean(initialCapital);
    const capitalHelp = initialCapitalExists
        ? `Initial capital was set on ${formatLocalDate(initialCapital.occurred_at)}. To align with your broker cash balance, add the difference as a Deposit (or Withdrawal); it does not change trading P/L.`
        : 'Set Initial Capital once to establish the starting balance. Later cash changes should be recorded as Deposits or Withdrawals.';
    container.innerHTML = `<header><h1>📓 Trading Journal</h1><p class="subtitle">Trades, performance and risk — separate from the stock-monitoring overview.</p></header><div id="ac"></div>${analytics?.equity_available ? '' : '<div class="alert-box info">Add an <strong>Initial capital</strong> event below to calculate current capital, return, exposure and an equity curve.</div>'}
      <div class="journal-kpi-grid">${journalKpi('Current Capital', journalMoney(capital.current_eur), 'Cash plus priced open positions.')}${journalKpi('Net P/L', journalSigned(currentPnl), 'Realized plus unrealized P/L; capital events excluded.', journalClass(currentPnl))}${journalKpi('Return', journalPercent(capital.current_return_pct), 'Current capital less net owner contributions divided by net owner contributions.', journalClass(capital.current_return_pct))}${journalKpi('YTD P/L', journalSigned(performance.ytd_pnl_eur), 'Net P/L from completed trades this calendar year.', journalClass(performance.ytd_pnl_eur))}${journalKpi('Win Rate', journalPercent(tradeStats.win_rate_pct), 'Winning completed trades divided by completed trades.')}${journalKpi('Max Drawdown', journalPercent(risk.max_drawdown_pct), 'Largest peak-to-trough decline in equity snapshots.', journalClass(risk.max_drawdown_pct))}${journalKpi('Available Cash', journalMoney(capital.available_cash_eur), 'Capital events less purchases/fees plus sales.')}${journalKpi('Open Positions', String(tradeStats.open ?? 0), 'Lots not yet closed.')}</div>
      <div class="card"><div class="section-header"><div><div class="section-title">Equity Curve</div><p class="subtitle">Current capital and peak equity over time.</p></div><div class="journal-small">Estimated yearly profit: ${journalMoney(performance.estimated_yearly_profit_eur)}<br>Annualized return: ${journalPercent(performance.annualized_return_pct)}</div></div>${renderEquityCurve(analytics?.equity_curve, analytics?.equity_curve_resolution)}</div>
      <div class="journal-columns"><div class="card"><div class="section-title">Trade Statistics</div><dl class="journal-metrics"><dt>Completed / winning / losing</dt><dd>${tradeStats.completed ?? 0} / ${tradeStats.winning ?? 0} / ${tradeStats.losing ?? 0}</dd><dt>Realized gains / losses</dt><dd><span class="positive">${journalSigned(realized.gains_eur)}</span> / <span class="negative">${journalSigned(realized.losses_eur)}</span></dd><dt>Net realized P/L</dt><dd class="${journalClass(realized.net_eur)}">${journalSigned(realized.net_eur)}</dd><dt>Average trade / profit factor</dt><dd>${journalSigned(tradeStats.expectancy_eur)} / ${tradeStats.profit_factor == null ? 'N/A' : Number(tradeStats.profit_factor).toFixed(2)}</dd><dt>Average holding / trades month</dt><dd>${tradeStats.average_holding_days == null ? 'N/A' : `${Number(tradeStats.average_holding_days).toFixed(1)} days`} / ${tradeStats.trades_per_month == null ? 'N/A' : Number(tradeStats.trades_per_month).toFixed(2)}</dd><dt>Volatility / Sharpe</dt><dd>${journalPercent(risk.volatility_pct)} / ${risk.sharpe_ratio == null ? 'Insufficient data' : Number(risk.sharpe_ratio).toFixed(2)}</dd></dl></div><div class="card"><div class="section-title">Monthly Performance</div>${analytics?.monthly_performance?.length ? `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Month</th><th>P/L</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody>${analytics.monthly_performance.slice(-12).reverse().map(row => `<tr><td>${escapeHtml(row.month)}</td><td class="${journalClass(row.pnl_eur)}">${journalSigned(row.pnl_eur)}</td><td>${row.trades}</td><td>${journalPercent(row.wins / row.trades * 100)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="journal-empty">No completed trades yet.</p>'}</div></div>
      <div class="card journal-trade-editor ${isClosing ? 'is-closing' : edit ? 'is-editing' : ''}" id="journal-trade-editor" tabindex="-1"><div class="section-header"><div><div class="section-title">${editorTitle}</div><p class="subtitle" aria-live="polite">${editorContext}</p></div>${edit ? '<button type="button" class="btn btn-sm btn-secondary" data-trade-action="cancel">Cancel</button>' : ''}</div><form id="trade-form"><div class="form-row"><div class="form-group"><label for="trade-stock">Stock</label><select id="trade-stock" name="stock_id" required>${options}</select></div><div class="form-group"><label for="trade-opened">Opened</label><input id="trade-opened" name="opened_at" type="datetime-local" step="1" required value="${journalInputDate(edit?.opened_at) || journalInputDate(new Date().toISOString())}"></div><div class="form-group"><label for="trade-quantity">Quantity</label><input id="trade-quantity" name="quantity" type="number" min=".000001" step=".000001" required value="${escapeHtml(edit?.quantity ?? '')}"></div></div><div class="form-row"><div class="form-group"><label for="trade-currency">Currency</label><input id="trade-currency" name="currency" maxlength="3" required value="${escapeHtml(edit?.currency || 'EUR')}"></div><div class="form-group"><label for="trade-entry-price">Buy Price</label><input id="trade-entry-price" name="entry_price" type="number" min=".0001" step=".0001" required value="${escapeHtml(journalInputNumber(edit?.entry_price))}"></div><div class="form-group"><label for="trade-entry-fx">EUR Rate at Buy</label><input id="trade-entry-fx" name="entry_fx_rate" type="number" min=".000001" step=".000001" required value="${escapeHtml(edit?.entry_fx_rate ?? 1)}"></div><div class="form-group"><label for="trade-entry-fee">Buy Fees (€)</label><input id="trade-entry-fee" name="entry_fee_eur" type="number" min="0" step=".01" value="${escapeHtml(edit?.entry_fee_eur ?? 0)}"></div></div><details ${edit?.closed_at || isClosing ? 'open' : ''}><summary>${edit?.closed_at ? 'Closed trade details' : 'Close trade (optional)'}</summary><div class="form-row" style="margin-top:.75rem"><div class="form-group"><label for="trade-closed">Closed</label><input id="trade-closed" name="closed_at" type="datetime-local" step="1" ${isClosing ? 'required' : ''} value="${journalInputDate(edit?.closed_at || (isClosing ? new Date().toISOString() : ''))}"></div><div class="form-group"><label for="trade-exit-price">Sell Price</label><input id="trade-exit-price" name="exit_price" type="number" min=".0001" step=".0001" ${isClosing ? 'required' : ''} value="${escapeHtml(journalInputNumber(edit?.exit_price))}"></div><div class="form-group"><label for="trade-exit-fx">EUR Rate at Sell</label><input id="trade-exit-fx" name="exit_fx_rate" type="number" min=".000001" step=".000001" ${isClosing ? 'required' : ''} value="${escapeHtml(edit?.exit_fx_rate ?? 1)}"></div><div class="form-group"><label for="trade-exit-fee">Sell Fees (€)</label><input id="trade-exit-fee" name="exit_fee_eur" type="number" min="0" step=".01" value="${escapeHtml(edit?.exit_fee_eur ?? 0)}"></div></div></details><div class="form-row" style="margin-top:.75rem"><div class="form-group"><label for="trade-strategy">Strategy / Reason</label><input id="trade-strategy" name="strategy" value="${escapeHtml(edit?.strategy || '')}"></div><div class="form-group"><label for="trade-notes">Notes</label><input id="trade-notes" name="notes" value="${escapeHtml(edit?.notes || '')}"></div></div><div class="btn-group"><button type="submit" class="btn btn-primary">${isClosing ? 'Close Trade' : edit ? 'Save Changes' : 'Record Trade'}</button>${edit ? '<button type="button" class="btn btn-secondary" data-trade-action="cancel">Cancel</button>' : ''}</div></form></div>
      <div class="card"><div class="section-title">Capital Events</div><p class="subtitle">Capital events change cash/equity, never trading P/L.</p><form class="journal-capital-form" id="capital-event-form"><label class="sr-only" for="capital-type">Event type</label><select id="capital-type" name="event_type"><option value="INITIAL" ${initialCapitalExists ? 'disabled' : ''}>Initial Capital${initialCapitalExists ? ' (already recorded)' : ''}</option><option value="DEPOSIT" ${initialCapitalExists ? 'selected' : ''}>Deposit</option><option value="WITHDRAWAL">Withdrawal</option><option value="FINANCING">Financing / Debt</option></select><label class="sr-only" for="capital-amount">Amount</label><input id="capital-amount" name="amount_eur" type="number" min=".01" step=".01" placeholder="Amount €" required><label class="sr-only" for="capital-date">Date and time</label><input id="capital-date" name="occurred_at" type="datetime-local" value="${journalInputDate(new Date().toISOString())}" required><label class="sr-only" for="capital-notes">Note</label><input id="capital-notes" name="notes" placeholder="Optional note"><button type="submit" class="btn btn-primary">Add Event</button></form><p class="journal-help" id="capital-event-help" aria-live="polite">${capitalHelp}</p>${getCapitalEvents().length ? `<div class="journal-events">${getCapitalEvents().map(event => `<span><strong>${escapeHtml(event.event_type)}</strong> ${journalMoney(event.amount_eur)} · ${formatLocalDate(event.occurred_at)}</span>`).join('')}</div>` : '<p class="journal-empty">No capital events recorded.</p>'}</div>
      <div class="card"><div class="section-header"><div><div class="section-title">Trades</div><p class="subtitle">Showing up to 20 entries per page.</p></div></div><div class="journal-table-wrap"><table class="table"><thead><tr><th>Trade ID</th><th>Stock</th><th>Status</th><th>Opened</th><th>Net P/L</th><th>Return</th><th></th></tr></thead><tbody>${renderTradeRows(getTrades())}</tbody></table></div>${renderTradePagination(getTradePagination())}</div>
      <div class="card"><div class="section-title">Per-Stock Analytics</div>${analytics?.stock_performance?.length ? `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Stock</th><th>P/L</th><th>Trades</th><th>Win Rate</th><th>Average Return</th><th>Average Hold</th></tr></thead><tbody>${analytics.stock_performance.map(row => `<tr><td><strong>${escapeHtml(row.symbol)}</strong></td><td class="${journalClass(row.pnl_eur)}">${journalSigned(row.pnl_eur)}</td><td>${row.trades}</td><td>${journalPercent(row.win_rate_pct)}</td><td>${journalPercent(row.average_return_pct)}</td><td>${Number(row.average_holding_days).toFixed(1)} days</td></tr>`).join('')}</tbody></table></div>` : '<p class="journal-empty">Per-stock analytics appear after completed trades.</p>'}</div><div class="card"><p class="journal-help" id="journal-definitions">Definitions: Net P/L = sell EUR value − buy EUR value − entry fees − exit fees. Return = net P/L ÷ (buy EUR value + entry fees). Realized metrics use closed trades only; unrealized P/L uses current monitored EUR prices for open positions. Annualized estimates need at least 30 calendar days from initial capital; volatility and Sharpe require at least 30 equity observations.</p></div>`;
    container.querySelectorAll('[data-trade-action]').forEach(button => button.addEventListener('click', () => ({ edit: editTrade, close: closeTrade, cancel: cancelTradeEdit }[button.dataset.tradeAction])?.(button.dataset.tradeId)));
    container.querySelectorAll('[data-trade-page]').forEach(button => button.addEventListener('click', () => {
        const page = Number(button.dataset.tradePage);
        if (Number.isInteger(page) && page > 0) { setJournalPage(page); reloadJournal(page); }
    }));
    container.querySelector('#trade-form')?.addEventListener('submit', saveTrade);
    container.querySelector('#capital-event-form')?.addEventListener('submit', saveCapitalEvent);
    if (edit) focusTradeEditor(isClosing);
    attachInteractiveLineCharts(container);
}

function queueJournalDividendEnhancements(container) {
    queueMicrotask(() => {
        const closeDetails = container.querySelector('details');
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
