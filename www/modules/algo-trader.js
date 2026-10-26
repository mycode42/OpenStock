import { escapeHtml } from './escape.js';

const RANGES = ['1D', '1W', '1M', '3M', 'ALL'];
const ACTIVITY_PAGE_SIZE = 10;
function numberOrNull(value) {
    const number = Number(value);
    return value !== null && value !== undefined && value !== '' && Number.isFinite(number) ? number : null;
}

function money(value, currency = 'USD', digits = 2) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return new Intl.NumberFormat(undefined, { style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(number);
}

function signedMoney(value, currency = 'USD') {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${money(number, currency)}`;
}

function signedPercent(value, digits = 2) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
}

function signedScore(value) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${number.toFixed(4)}`;
}

function dateTime(value, options = {}) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short', ...options }).format(date);
}

function timeOnly(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(date);
}

function dateOnly(value) {
    if (!value) return '';
    const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(date);
}

function classForReturn(value) {
    const number = numberOrNull(value);
    return number === null || number === 0 ? '' : number > 0 ? 'up' : 'down';
}

function rangeHistory(history, range) {
    const points = Array.isArray(history) ? history.filter(item => numberOrNull(item.equity) !== null) : [];
    if (range === 'ALL' || points.length <= 1) return points;
    const lookback = range === '1D' ? 1 : range === '1W' ? 7 : range === '1M' ? 31 : 92;
    const latestDate = new Date(points[points.length - 1].timestamp || points[points.length - 1].date);
    if (Number.isNaN(latestDate.getTime())) return points;
    const cutoff = latestDate.getTime() - lookback * 86400000;
    const filtered = points.filter(item => new Date(item.timestamp || item.date).getTime() >= cutoff);
    return filtered.length > 1 || range === '1D' ? filtered : points;
}

function renderChart(history, range, charts) {
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
    const coords = values.map((value, index) => ({
        x: left + (right - left) * (index / (values.length - 1)),
        y: bottom - ((value - min) / (max - min || 1)) * (bottom - top),
        value,
    }));
    const line = coords.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
    const area = `${line} L ${right} ${bottom} L ${left} ${bottom} Z`;
    const grid = [0, 1, 2, 3].map(index => {
        const y = top + (bottom - top) * (index / 3);
        const value = max - (max - min) * (index / 3);
        return `<line class="algo-chart-grid" x1="${left}" y1="${y}" x2="${right}" y2="${y}"/><text class="algo-chart-y-label" x="${left - 12}" y="${y + 4}" text-anchor="end">${escapeHtml(money(value, 'USD', 0))}</text>`;
    }).join('');
    const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
    const labels = labelIndexes.map(index => {
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

function statusClass(value) {
    const status = String(value || '').toLowerCase();
    if (['filled', 'complete', 'completed', 'ok', 'promoted', 'approved'].includes(status)) return 'promising';
    if (['failed', 'error', 'rejected', 'cancelled', 'canceled'].includes(status)) return 'rejected';
    if (['staged'].includes(status)) return 'staged';
    return '';
}

function renderOptimizerDetail(optimizer) {
    if (!optimizer) return '<div class="algo-empty">Select an optimizer run to inspect its results.</div>';
    const strategies = Array.isArray(optimizer.strategies) ? optimizer.strategies : [];
    const rows = strategies.map(strategy => {
        const eligibility = strategy.selected_eligible
            ? ['promising', 'Eligible']
            : strategy.selected_candidate === 'baseline' ? ['', 'Baseline retained'] : ['rejected', 'Not eligible'];
        return `<tr>
            <th scope="row"><strong>${escapeHtml(String(strategy.name || '').replaceAll('_', ' '))}</strong><small>Archive #${strategy.archive_sequence ?? '—'} · ${strategy.new_variants ?? 0} variants</small></th>
            <td title="${escapeHtml(strategy.top_tested_candidate || '')}">${escapeHtml(strategy.top_tested_candidate || 'Unavailable')}</td>
            <td class="${classForReturn(strategy.top_tested_fitness)}">${signedScore(strategy.top_tested_fitness)}</td>
            <td class="${classForReturn(strategy.top_tested_delta)}">${signedScore(strategy.top_tested_delta)}</td>
            <td title="${escapeHtml(strategy.selected_candidate || '')}">${escapeHtml(strategy.selected_candidate || 'Unavailable')}</td>
            <td><span class="algo-status-pill ${eligibility[0]}">${eligibility[1]}</span></td>
            <td>${strategy.selected_closed_trades ?? 0}</td>
            <td class="${classForReturn(strategy.selected_return_pct)}">${signedPercent(strategy.selected_return_pct, 4)}</td>
            <td>${strategy.top_tested_window_days ? `${strategy.top_tested_window_days} sessions` : '—'}</td>
        </tr>`;
    }).join('');
    return `<div class="algo-optimizer-detail">
        <div class="algo-section-heading"><div><span class="algo-eyebrow">OPTIMIZER CYCLE</span><h3>${escapeHtml(optimizer.date || 'Undated run')}</h3><p>${escapeHtml(optimizer.range_start || '—')} to ${escapeHtml(optimizer.range_end || '—')} · ${optimizer.created_at ? `completed ${escapeHtml(dateTime(optimizer.created_at))}` : 'completion time unavailable'}</p></div><span class="algo-status-pill ${statusClass(optimizer.status)}">${escapeHtml(optimizer.status || 'UNKNOWN')}</span></div>
        <div class="algo-optimizer-run-meta"><span title="${escapeHtml(optimizer.run_id || '')}">Run ${escapeHtml(optimizer.run_id || 'ID unavailable')}</span><span>${optimizer.duration_seconds === null || optimizer.duration_seconds === undefined ? 'Duration unavailable' : `${Math.round(optimizer.duration_seconds / 60)} min`}</span></div>
        <div class="algo-optimizer-summary"><div><span>Strategies</span><strong>${optimizer.strategy_count ?? 0}</strong></div><div><span>New variants</span><strong>${optimizer.new_variants ?? 0}</strong></div><div><span>Search iterations</span><strong>${optimizer.search_iterations ?? 0}</strong></div><div><span>Archive sequence</span><strong>#${optimizer.archive_sequence ?? '—'}</strong></div></div>
        ${rows ? `<div class="algo-optimizer-table-wrap" role="region" aria-label="Per-strategy optimizer results" tabindex="0"><table class="algo-optimizer-results"><thead><tr><th scope="col">Strategy</th><th scope="col">Top candidate</th><th scope="col">Fitness</th><th scope="col">Δ baseline</th><th scope="col">Archive choice</th><th scope="col">Eligibility</th><th scope="col">Trades</th><th scope="col">Return</th><th scope="col">Best window</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="algo-optimizer-table-empty">No per-strategy discovery results were recorded.</div>'}
        <p class="algo-optimizer-note">Discovery fitness is measured on the search window. Optimizer-only cycles do not verify or promote candidates.</p>
    </div>`;
}

function renderOptimizerPanel(optimizer, selectedCycleId) {
    const history = Array.isArray(optimizer?.history) ? optimizer.history : [];
    const selected = history.find(run => run.cycle_id === selectedCycleId) || history[0] || null;
    const runs = history.map(run => `<button type="button" class="algo-optimizer-run-row ${run.cycle_id === selected?.cycle_id ? 'selected' : ''}" data-optimizer-run="${escapeHtml(run.cycle_id || '')}" aria-pressed="${run.cycle_id === selected?.cycle_id}" title="${escapeHtml(run.run_id || '')}">
        <span class="algo-optimizer-run-date">${escapeHtml(run.date || 'Undated')}<small>${run.created_at ? escapeHtml(timeOnly(run.created_at)) : 'saved cycle'} · ${escapeHtml(run.run_id || 'Run ID unavailable')} · ${run.new_variants ?? 0} variants · ${run.search_iterations ?? 0} iterations</small></span>
        <span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span>
    </button>`).join('');
    return `<section class="card algo-panel algo-history-panel algo-optimizer-panel" id="algo-optimizer">
        <div class="algo-section-heading algo-history-heading"><div><span class="algo-eyebrow">OPTIMIZER ARCHIVE</span><h2>Optimizer history</h2><p>Browse saved discovery cycles and their per-strategy results.</p></div><span class="algo-status-pill">${history.length} ${history.length === 1 ? 'RUN' : 'RUNS'}</span></div>
        <div class="algo-optimizer-history-layout"><div class="algo-optimizer-run-list" role="list">${runs || '<div class="algo-empty">No optimizer cycles are available yet.</div>'}</div><aside class="algo-optimizer-detail-pane">${renderOptimizerDetail(selected)}</aside></div>
    </section>`;
}

const EQUITY_STRATEGY_ALIASES = Object.freeze({
    mean_reversion: 'intraday_mean_reversion',
    trend_following: 'swing_trend_following',
    momentum: 'swing_momentum',
    intraday_trend: 'intraday_trend_following',
    multi_factor: 'swing_multi_factor',
    event_driven: 'swing_event_driven',
});

const EQUITY_STRATEGY_LABELS = Object.freeze({
    intraday_mean_reversion: 'Intraday Mean Reversion',
    swing_trend_following: 'Swing Trend Following',
    swing_momentum: 'Swing Momentum',
    swing_mean_reversion: 'Swing Mean Reversion',
    intraday_trend_following: 'Intraday Trend Following',
    intraday_momentum: 'Intraday Momentum',
    swing_multi_factor: 'Swing Multi-Factor',
    swing_event_driven: 'Swing Event-Driven',
});
const OPTIONS_STRATEGY_ALIASES = Object.freeze({
    vrp: 'options_vrp',
    event_driven: 'options_event_driven',
    directional_overlay: 'options_directional_overlay',
    long_volatility: 'options_long_volatility_sleeve',
    options_long_volatility: 'options_long_volatility_sleeve',
});
const OPTIONS_STRATEGY_LABELS = Object.freeze({
    options_vrp: 'Volatility Risk Premium (VRP)',
    options_event_driven: 'Event-Driven',
    options_directional_overlay: 'Directional Overlay',
    options_long_volatility_sleeve: 'Long Volatility',
});

function equityStrategyId(value) {
    const strategy = String(value || '').trim();
    return EQUITY_STRATEGY_ALIASES[strategy] || strategy;
}

function optionsStrategyId(value) {
    const strategy = String(value || '').trim();
    return OPTIONS_STRATEGY_ALIASES[strategy] || strategy;
}

function strategyDisplayName(value, isOptions = false) {
    const strategy = String(value || 'Unknown strategy');
    if (isOptions && OPTIONS_STRATEGY_LABELS[strategy]) return OPTIONS_STRATEGY_LABELS[strategy];
    if (EQUITY_STRATEGY_LABELS[strategy]) return EQUITY_STRATEGY_LABELS[strategy];
    const displayStrategy = isOptions
        ? strategy.replace(/^options_/, '').replace(/_sleeve$/, '')
        : strategy;
    return displayStrategy.split('_').map(part => part ? part[0].toUpperCase() + part.slice(1) : '').join(' ');
}

function renderStrategyPerformance(performance, isOptions, settings = {}) {
    const scorecard = performance || {};
    const sourceStrategies = Array.isArray(scorecard.strategies) ? scorecard.strategies : [];
    const byName = new Map();
    const ensureStrategy = rawName => {
        const strategy = isOptions ? optionsStrategyId(rawName) : equityStrategyId(rawName);
        if (!strategy) return null;
        if (!byName.has(strategy)) byName.set(strategy, {
            strategy,
            latest_activity: null,
            latest_pnl_eur: null,
            latest_pnl_pct: null,
            cumulative_pnl_eur: null,
            cumulative_pnl_pct: null,
            complete: ['ready', 'empty'].includes(scorecard.status),
        });
        return byName.get(strategy);
    };
    const canShowKnownStrategies = !isOptions;
    if (canShowKnownStrategies && settings.strategies && typeof settings.strategies === 'object') {
        for (const strategy of Object.keys(settings.strategies)) ensureStrategy(strategy);
    }
    if (Array.isArray(scorecard.known_strategies)) {
        for (const strategy of scorecard.known_strategies) ensureStrategy(strategy);
    }
    for (const strategy of sourceStrategies) {
        const row = ensureStrategy(strategy.strategy);
        if (!row) continue;
        byName.set(row.strategy, { ...row, ...strategy, strategy: row.strategy });
    }
    const strategies = [...byName.values()];
    const hasActivity = strategies.some(strategy => strategy.latest_activity
        || numberOrNull(strategy.cumulative_pnl_eur) !== null);
    const normalizedScorecard = {
        ...scorecard,
        status: scorecard.status || (hasActivity ? 'ready' : 'empty'),
        strategies,
    };
    const ranked = normalizedScorecard.strategies.filter(strategy => strategy.complete === true
        && numberOrNull(strategy.cumulative_pnl_eur) !== null);
    const best = ranked.reduce((current, strategy) => (
        !current || strategy.cumulative_pnl_eur > current.cumulative_pnl_eur ? strategy : current
    ), null);
    const worst = ranked.reduce((current, strategy) => (
        !current || strategy.cumulative_pnl_eur < current.cumulative_pnl_eur ? strategy : current
    ), null);
    const rows = [...normalizedScorecard.strategies].sort((left, right) => {
        const a = numberOrNull(left.cumulative_pnl_eur);
        const b = numberOrNull(right.cumulative_pnl_eur);
        if (a === null) return b === null ? String(left.strategy).localeCompare(String(right.strategy)) : 1;
        if (b === null) return -1;
        return b - a;
    });
    const fxNote = scorecard.status === 'loading'
        ? 'Waiting for fill reconciliation'
        : scorecard.fx_rate
            ? `EUR converted at USD/EUR ${Number(scorecard.fx_rate).toFixed(4)}${scorecard.fx_date ? ` · ${scorecard.fx_date}` : ''}${scorecard.fx_source ? ` · ${scorecard.fx_source}` : ''}`
            : 'EUR conversion unavailable';
    const basisNote = isOptions ? 'percentages use summed entry leg premiums' : 'percentages use share cost basis';
    const emptyMessage = scorecard.status === 'unavailable'
        ? scorecard.message || 'Broker fill history could not be reconciled.'
        : scorecard.status === 'loading'
            ? scorecard.message || 'Reconciling broker fills and current positions.'
            : scorecard.status === 'empty'
                ? 'No confirmed fills with strategy attribution have been recorded yet.'
                : 'No strategy P/L records are available yet.';
    return `<section class="card algo-panel algo-strategy-pnl-panel" aria-labelledby="algo-strategy-pnl-title">
        <div class="algo-section-heading"><div><span class="algo-eyebrow">STRATEGY SCORECARD</span><h2 id="algo-strategy-pnl-title">Strategy P/L</h2><p>Latest confirmed activity and cumulative P/L by ${isOptions ? 'options' : 'equity'} strategy.</p></div><span class="algo-status-pill ${normalizedScorecard.status === 'ready' ? 'promising' : ''}">${escapeHtml(String(normalizedScorecard.status || 'unavailable').toUpperCase())}</span></div>
        <div class="algo-strategy-pnl-meta"><span>Amounts in EUR · ${escapeHtml(basisNote)}</span><span>${escapeHtml(fxNote)}</span></div>
        ${rows.length ? `<div class="algo-strategy-pnl-grid algo-strategy-pnl-header" role="row"><span>Strategy</span><span>Latest activity</span><span>Latest P/L</span><span>Cumulative P/L</span></div><div class="algo-strategy-pnl-rows">${rows.map(strategy => {
            const latest = strategy.latest_activity || {};
            const pnlClass = classForReturn(strategy.cumulative_pnl_eur);
            const badge = strategy === best && best?.cumulative_pnl_eur > 0
                ? '<span class="algo-strategy-pnl-badge best">Top performer</span>'
                : strategy === worst && worst?.cumulative_pnl_eur < 0
                    ? '<span class="algo-strategy-pnl-badge review">Review</span>' : '';
            const quantity = numberOrNull(latest.qty);
            const unit = isOptions ? (quantity === 1 ? 'contract' : 'contracts') : (quantity === 1 ? 'share' : 'shares');
            const activityTitle = [latest.action, latest.symbol].filter(Boolean).join(' · ')
                || (scorecard.status === 'loading' ? 'Loading fill activity'
                    : scorecard.status === 'unavailable' ? 'Activity unavailable' : 'No filled activity');
            const activityDetail = [latest.at ? dateTime(latest.at) : '', quantity === null ? '' : `${quantity} ${unit}`, latest.status || '']
                .filter(Boolean).join(' · ');
            const activityFallback = scorecard.status === 'loading'
                ? 'Reconciling broker fills'
                : scorecard.status === 'unavailable'
                    ? 'Broker fill history could not be reconciled'
                    : 'No activity in the fill journal';
            const displayName = isOptions
                ? strategyDisplayName(strategy.strategy, true)
                : strategyDisplayName(strategy.strategy);
            return `<div class="algo-strategy-pnl-grid algo-strategy-pnl-row" role="row"><div class="algo-strategy-pnl-name"><strong>${escapeHtml(displayName)}</strong>${badge}</div><div class="algo-strategy-pnl-activity"><strong>${escapeHtml(activityTitle)}</strong><small>${escapeHtml(activityDetail || activityFallback)}</small></div><div class="algo-strategy-pnl-value"><strong class="${classForReturn(strategy.latest_pnl_eur)}">${signedMoney(strategy.latest_pnl_eur, 'EUR')}</strong><small class="${classForReturn(strategy.latest_pnl_pct)}">${signedPercent(strategy.latest_pnl_pct)}</small></div><div class="algo-strategy-pnl-value"><strong class="${pnlClass}">${signedMoney(strategy.cumulative_pnl_eur, 'EUR')}</strong><small class="${classForReturn(strategy.cumulative_pnl_pct)}">${signedPercent(strategy.cumulative_pnl_pct)}</small></div></div>`;
        }).join('')}</div>` : `<div class="algo-empty algo-strategy-pnl-empty">${escapeHtml(emptyMessage)}</div>`}
        <p class="algo-strategy-pnl-note">${escapeHtml(normalizedScorecard.message || 'Only recorded, strategy-attributed activity is included.')}${normalizedScorecard.source ? ` · Source: ${escapeHtml(normalizedScorecard.source)}` : ''} · Gross P/L before fees.</p>
    </section>`;
}

function activityIcon(kind) {
    return ({ order: '⇄', scan: '◷', signal: '↗', research: '⌕' })[kind] || '·';
}

function setPath(target, path, value) {
    const keys = path.split('.');
    const finalKey = keys.pop();
    let current = target;
    for (const key of keys) current = current[key] ||= {};
    current[finalKey] = value;
}

function optionsSettingFields(settings, readonlyFields = []) {
    const fields = [];
    const visit = (value, path = '') => {
        if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length) {
            for (const [key, child] of Object.entries(value)) visit(child, path ? `${path}.${key}` : key);
            return;
        }
        fields.push({ path, value, json: Array.isArray(value) || (value && typeof value === 'object'), readonly: readonlyFields.includes(path) });
    };
    visit(settings);
    const groups = new Map();
    for (const field of fields) {
        const [section, ...rest] = field.path.split('.');
        const group = rest.length ? section : 'General';
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(field);
    }
    return [...groups.entries()].map(([group, rows]) => `<details class="algo-options-config-section" data-config-section="options:${escapeHtml(group)}"><summary>${escapeHtml(group.replaceAll('_', ' '))}</summary><div class="algo-options-config-fields">${rows.map(field => {
        const label = field.path.replaceAll('_', ' ').replaceAll('.', ' · ');
        const disabled = field.readonly ? 'disabled' : '';
        const input = typeof field.value === 'boolean'
            ? `<label class="algo-config-toggle"><input type="checkbox" data-setting-path="${escapeHtml(field.path)}" ${field.value ? 'checked' : ''} ${disabled}><span><strong>${escapeHtml(label)}</strong></span><i></i></label>`
            : field.json
                ? `<div class="algo-config-field"><label>${escapeHtml(label)}</label><textarea data-setting-path="${escapeHtml(field.path)}" rows="2" ${disabled}>${escapeHtml(JSON.stringify(field.value))}</textarea></div>`
                : field.path === 'market_data_feed'
                    ? `<div class="algo-config-field"><label>${escapeHtml(label)}</label><select data-setting-path="${escapeHtml(field.path)}" ${disabled}><option value="opra" ${field.value === 'opra' ? 'selected' : ''}>OPRA</option><option value="indicative" ${field.value === 'indicative' ? 'selected' : ''}>Indicative</option></select></div>`
                    : `<div class="algo-config-field"><label>${escapeHtml(label)}</label><input data-setting-path="${escapeHtml(field.path)}" type="${typeof field.value === 'number' || field.value === null ? 'number' : 'text'}" ${typeof field.value === 'number' || field.value === null ? 'step="any"' : ''} ${field.value === null ? 'data-nullable="true" placeholder="Optional — leave blank to disable"' : ''} value="${escapeHtml(field.value ?? '')}" ${disabled}></div>`;
        return input;
    }).join('')}</div></details>`).join('');
}

function equitySettingSections(settings) {
    const fractionPath = path => path.endsWith('_pct');
    const humanize = path => path.includes('.regime_weights.')
        ? path.split('.').at(-1).replaceAll('_', ' ') + ' route and risk weight'
        : path === 'strategies.swing_event_driven.minimum_confidence'
            ? 'minimum event confidence'
        : path.split('.').at(-1).replaceAll('_', ' ');
    const valueInput = (path, value) => {
        const label = humanize(path);
        const eventConfidence = path === 'strategies.swing_event_driven.minimum_confidence';
        if (typeof value === 'boolean') {
            return '<label class="algo-config-toggle"><input type="checkbox" data-setting-path="' + escapeHtml(path) + '"' + (value ? ' checked' : '') + '><span><strong>' + escapeHtml(label) + '</strong></span><i></i></label>';
        }
        if (Array.isArray(value) || (value && typeof value === 'object')) {
            return '<div class="algo-config-field"><label>' + escapeHtml(label) + '</label><textarea data-setting-path="' + escapeHtml(path) + '" rows="2">' + escapeHtml(JSON.stringify(value)) + '</textarea></div>';
        }
        if (typeof value === 'number') {
            const percentage = fractionPath(path);
            const display = percentage ? value * 100 : value;
            return '<div class="algo-config-field"><label>' + escapeHtml(label) + (percentage || eventConfidence ? ' · %' : '') + '</label><input data-setting-path="' + escapeHtml(path) + '" type="number" step="' + (eventConfidence ? '1' : 'any') + '"' + (percentage ? ' data-percent="true"' : '') + ' value="' + escapeHtml(display) + '"></div>';
        }
        const choices = path === 'general.regime_engine'
            ? ['committee', 'rule', 'hmm', 'gmm']
            : path === 'general.regime_feature_set'
                ? ['core', 'technical', 'macro', 'marketwide']
                : null;
        if (choices) {
            return '<div class="algo-config-field"><label>' + escapeHtml(label) + '</label><select data-setting-path="' + escapeHtml(path) + '">' + choices.map(choice => '<option value="' + choice + '"' + (choice === value ? ' selected' : '') + '>' + escapeHtml(choice === 'committee' ? 'committee · rule + HMM + GMM' : choice) + '</option>').join('') + '</select></div>';
        }
        const structural = path.endsWith('.horizon') || path.endsWith('.bar_interval');
        return '<div class="algo-config-field"><label>' + escapeHtml(label) + '</label><input data-setting-path="' + escapeHtml(path) + '" type="text" value="' + escapeHtml(value) + '"' + (structural ? ' disabled' : '') + '></div>';
    };
    const fields = (value, prefix) => Object.entries(value || {}).map(([key, child]) => {
        const path = prefix ? prefix + '.' + key : key;
        return child && typeof child === 'object' && !Array.isArray(child)
            ? fields(child, path)
            : valueInput(path, child);
    }).join('');
    const sections = [
        ['general', settings.general || {}],
        ['portfolio', settings.portfolio || {}],
    ].map(([name, value]) => '<details class="algo-options-config-section" data-config-section="equity:' + escapeHtml(name) + '"><summary>' + escapeHtml(name) + '</summary><div class="algo-options-config-fields">' + fields(value, name) + '</div></details>');
    const strategies = Object.entries(settings.strategies || {}).map(([name, value]) => '<details class="algo-options-config-section" data-config-section="equity:strategy:' + escapeHtml(name) + '"><summary>' + escapeHtml(name.replaceAll('_', ' ')) + '</summary><div class="algo-options-config-fields">' + fields(value, 'strategies.' + name) + '</div></details>').join('');
    sections.push('<details class="algo-options-config-section" data-config-section="equity:strategies"><summary>Strategies</summary><div class="algo-options-config">' + strategies + '</div></details>');
    return sections.join('');
}

export function createAlgoTrader({ request, charts }) {
    const host = document.getElementById('page-algo-trader');
    const snapshots = { equity: null, options: null };
    const errors = { equity: '', options: '' };
    const loadingAccounts = new Set();
    const requestInFlight = new Set();
    const draftSettingsByAccount = { equity: null, options: null };
    const dirtyAccounts = new Set();
    const openConfigSections = { equity: new Set(), options: new Set() };
    const notices = { equity: '', options: '' };
    let accountMode = 'equity';
    let saving = false;
    let range = '1M';
    let researchFilter = 'all';
    let selectedRunDate = null;
    let selectedOptimizerRunId = null;
    let activityPage = 0;
    let pollTimer = null;

    function visibleRuns() {
        const history = snapshots[accountMode]?.research?.history || [];
        if (researchFilter === 'promoted') return history.filter(run => run.promoted > 0);
        if (researchFilter === 'no-promotion') return history.filter(run => !run.promoted);
        return history;
    }

    function renderRunDetail(run) {
        if (!run) return '<div class="algo-empty">No autoresearch reports are available yet.</div>';
        if (Array.isArray(run.options_strategies)) {
            const approved = run.options_strategies.filter(strategy => strategy.approved).length;
            const completed = run.options_strategies.filter(strategy => strategy.status === 'completed').length;
            const errors = run.options_strategies.filter(strategy => strategy.status === 'error').length;
            const strategies = run.options_strategies.map(strategy => `${strategy.name.replaceAll('_', ' ')}: ${strategy.status}${strategy.approved ? ' · approved' : ''}`).join(' · ');
            return `<div class="algo-research-detail-head"><div><span class="algo-eyebrow">OPTIONS RESEARCH RUN</span><h3>${escapeHtml(run.date || 'Undated run')}</h3></div><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span></div>
                <p class="algo-research-note">${escapeHtml(run.result || 'Options research result unavailable')}</p>
                <div class="algo-run-metrics"><div><span>Strategies</span><strong>${run.active_trials ?? 0}</strong></div><div><span>Completed</span><strong>${completed}</strong></div><div><span>Approved</span><strong>${approved}</strong></div><div><span>Errors</span><strong>${errors}</strong></div></div>
                <p class="algo-research-note">${escapeHtml(strategies || 'No strategy results')}</p>
                <small class="algo-detail-time">${run.created_at ? `Completed ${escapeHtml(dateTime(run.created_at))}` : 'Completion time unavailable'} · research only</small>`;
        }
        const strategyFitness = Array.isArray(run.strategy_fitness) ? run.strategy_fitness : [];
        return `<div class="algo-research-detail-head"><div><span class="algo-eyebrow">RESEARCH RUN</span><h3>${escapeHtml(run.date || 'Undated run')}</h3></div><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span></div>
            <p class="algo-research-note">${escapeHtml(run.result || 'Research result unavailable')}${run.portfolio_status && run.portfolio_status !== 'unavailable' ? ` · portfolio replay ${escapeHtml(run.portfolio_status)}` : ''}</p>
            ${run.factor_model ? `<div class="algo-run-metrics"><div><span>Factor model</span><strong>${escapeHtml(run.factor_model.family)}</strong></div><div><span>Observations</span><strong>${run.factor_model.observations ?? '—'}</strong></div><div><span>Rolling evaluations</span><strong>${run.factor_model.rolling_evaluations ?? '—'}</strong></div><div><span>Forward horizon</span><strong>${run.factor_model.forward_horizon_sessions ?? '—'} sessions</strong></div></div><p class="algo-research-note">Discovery ${escapeHtml(run.discovery_status)}${run.discovery_reason ? ` · ${escapeHtml(run.discovery_reason)}` : ''}</p>` : `<div class="algo-run-metrics"><div><span>Active trials</span><strong>${run.active_trials ?? '—'}</strong></div><div><span>Promoted</span><strong>${run.promoted ?? 0}</strong></div><div><span>Staged</span><strong>${run.staged ?? 0}</strong></div><div><span>Portfolio return</span><strong class="${classForReturn(run.portfolio_return_pct)}">${signedPercent(run.portfolio_return_pct)}</strong></div></div>`}
            ${strategyFitness.length ? `<section class="algo-strategy-fitness"><div class="algo-strategy-fitness-title"><strong>Strategy fitness</strong><span>Out-of-sample score</span></div><div class="algo-strategy-fitness-grid algo-strategy-fitness-header"><span>Strategy</span><span>Current</span><span>Δ vs last run</span></div>${strategyFitness.map(strategy => `<div class="algo-strategy-fitness-grid"><span>${escapeHtml(String(strategy.name || '').replaceAll('_', ' '))}</span><strong class="${classForReturn(strategy.fitness)}">${signedScore(strategy.fitness)}</strong><strong class="${classForReturn(strategy.delta_vs_previous)}"${strategy.previous_run_date ? ` title="Compared with ${escapeHtml(strategy.previous_run_date)}"` : ''}>${signedScore(strategy.delta_vs_previous)}</strong></div>`).join('')}</section>` : ''}
            <small class="algo-detail-time">${run.created_at ? `Completed ${escapeHtml(dateTime(run.created_at))}` : ''}${run.duration_seconds !== null && run.duration_seconds !== undefined ? ` · ${Math.round(run.duration_seconds / 60)} min` : ''}</small>`;
    }

    function renderSettings(settings, mode, readonlyFields = []) {
        const notice = notices[mode] || 'Changes apply during the next portfolio scan.';
        if (mode === 'options') {
            return `<form id="algo-config-form" class="algo-config-form" data-account="options">
                <div class="algo-options-config">${optionsSettingFields(settings, readonlyFields)}</div>
                <div class="algo-config-actions"><p class="algo-save-notice" aria-live="polite">${escapeHtml(notice)}</p><button type="submit" class="btn btn-primary algo-save-button" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : 'Save options settings'} <span>→</span></button></div>
            </form>`;
        }
        return `<form id="algo-config-form" class="algo-config-form" data-account="equity">
            <p class="algo-field-help">A regime weight of zero disables that route; a positive weight enables it and scales its risk. Position percentages are ceilings, further limited by global exposure, sector, cash, and trade risk rules.</p>
            <div class="algo-options-config">${equitySettingSections(settings)}</div>
            <div class="algo-config-actions"><p class="algo-save-notice" aria-live="polite">${escapeHtml(notice)}</p><button type="submit" class="btn btn-primary algo-save-button" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : 'Save strategy configuration'} <span>→</span></button></div>
        </form>`;
    }

    function renderAccountSwitch() {
        return `<div class="algo-account-switch algo-title-account-switch" role="group" aria-label="Algo Trader paper account">${[['equity', 'Equity Account'], ['options', 'Options Account']].map(([mode, label]) => `<button type="button" data-account-mode="${mode}" class="${accountMode === mode ? 'active' : ''}" aria-pressed="${accountMode === mode}">${label}</button>`).join('')}</div>`;
    }

    function rememberOpenConfigSections(mode) {
        openConfigSections[mode] = new Set([...host.querySelectorAll('details[data-config-section]')]
            .filter(section => section.open && section.dataset.configSection.startsWith(`${mode}:`))
            .map(section => section.dataset.configSection));
    }

    function restoreOpenConfigSections(mode) {
        const openSections = openConfigSections[mode] || new Set();
        for (const section of host.querySelectorAll('details[data-config-section]')) {
            if (openSections.has(section.dataset.configSection)) section.open = true;
        }
    }

    function render() {
        if (!host) return;
        const renderedMode = host.querySelector('#algo-config-form')?.dataset.account;
        if (renderedMode) rememberOpenConfigSections(renderedMode);
        const snapshot = snapshots[accountMode];
        const isOptions = accountMode === 'options';
        if (!snapshot) {
            const loading = loadingAccounts.has(accountMode);
            host.innerHTML = `<main class="algo-page"><header class="algo-page-header"><div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1>${renderAccountSwitch()}</div></header><section class="card algo-connection-card"><div class="algo-loading-mark">↻</div><strong>${loading ? `Connecting to ${isOptions ? 'Options' : 'Equity'} Account…` : `${isOptions ? 'Options' : 'Equity'} Account data unavailable`}</strong><p>${escapeHtml(errors[accountMode] || 'Loading the paper-account snapshot and research records.')}</p><button type="button" class="btn btn-secondary" data-action="retry">Retry connection</button></section></main>`;
            return;
        }

        const account = snapshot.account || {};
        const performance = snapshot.performance || {};
        const metrics = performance.metrics || {};
        const history = performance.history || [];
        const settings = draftSettingsByAccount[accountMode] || snapshot.settings || {};
        const research = snapshot.research || { latest: null, history: [] };
        const optimizerRuns = research.optimizer?.history || [];
        if (!optimizerRuns.some(run => run.cycle_id === selectedOptimizerRunId)) {
            selectedOptimizerRunId = optimizerRuns[0]?.cycle_id || null;
        }
        const runs = visibleRuns();
        if (!runs.some(run => run.date === selectedRunDate)) selectedRunDate = runs[0]?.date || null;
        const selectedRun = runs.find(run => run.date === selectedRunDate) || null;
        const activities = snapshot.activity || [];
        const activityPageCount = Math.max(1, Math.ceil(activities.length / ACTIVITY_PAGE_SIZE));
        activityPage = Math.min(activityPage, activityPageCount - 1);
        const activityStart = activityPage * ACTIVITY_PAGE_SIZE;
        const visibleActivities = activities.slice(activityStart, activityStart + ACTIVITY_PAGE_SIZE);
        const latestSnapshot = history[history.length - 1];
        const snapshotDate = latestSnapshot?.date ? dateOnly(latestSnapshot.date) : 'no snapshots';
        const pauseReasons = Array.isArray(account.entry_pause_reasons) ? account.entry_pause_reasons : [];
        const marketStatus = String(account.market_status || account.market_state || '').toUpperCase();
        const regimeLabel = String(account.regime || 'unknown').replaceAll('_', ' ').toUpperCase();
        const scanStatus = String(account.last_scan_status || 'unknown').trim() || 'unknown';
        const coordinatorStatusLabel = ['ok', 'complete', 'completed', 'success', 'succeeded'].includes(scanStatus.toLowerCase())
            ? 'OK' : scanStatus.replaceAll('_', ' ').toUpperCase();
        const regimeEngineLabel = String(account.regime_engine || 'unknown').replaceAll('_', ' ').toUpperCase();
        const closedMarketQuotePause = marketStatus === 'CLOSED'
            && pauseReasons.length === 1
            && pauseReasons[0] === 'owned positions lack fresh protective quotes';
        const optionsMarketClosedPause = isOptions
            && marketStatus === 'CLOSED'
            && account.entries_paused === true
            && pauseReasons.length === 1
            && pauseReasons[0] === 'Options market is closed';
        const marketClosedPause = closedMarketQuotePause || optionsMarketClosedPause;
        const entryStatus = marketClosedPause ? 'MARKET CLOSED' : account.entries_paused === true ? 'ENTRIES PAUSED' : account.entries_paused === false ? 'ENTRIES ENABLED' : 'ENTRY STATUS UNKNOWN';
        const entryClass = marketClosedPause ? 'algo-state-pill market-closed' : account.entries_paused === true ? 'algo-state-pill paused' : account.entries_paused === false ? 'algo-state-pill' : 'algo-state-pill unknown';
        const pauseNoticeTitle = marketClosedPause ? 'Market closed' : 'New entries are paused';
        const pauseNoticeDetail = optionsMarketClosedPause
            ? 'The options marks and regime shown are from the last market session. New entries can resume when the market reopens.'
            : closedMarketQuotePause
            ? 'Protective quotes will be checked again when the market reopens. New entries remain paused until then.'
            : pauseReasons.join(' · ');
        const strategyListLabel = isOptions ? account.active_strategies_label || 'Strategies available in this regime' : 'Active strategies';
        const noStrategiesMessage = isOptions ? 'No enabled strategy sleeves for this regime' : 'No active strategies in the last scan';
        const strategyScope = isOptions
            ? `${regimeLabel} regime · ${regimeEngineLabel} engine`
            : `${account.universe_count ?? 0} tickers · ${regimeEngineLabel} regime engine`;

        host.innerHTML = `<main class="algo-page">
            <header class="algo-page-header">
                <div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1>${renderAccountSwitch()}</div>
                <div class="algo-header-status"><span class="algo-demo-tag"><i></i> ALPACA ${isOptions ? 'OPTIONS ' : ''}PAPER</span><span class="${entryClass}"><i></i> ${entryStatus}</span></div>
            </header>

            ${account.entries_paused && pauseReasons.length ? `<section class="algo-paused-notice ${marketClosedPause ? 'market-closed' : ''}"><strong>${pauseNoticeTitle}</strong><span>${escapeHtml(pauseNoticeDetail)}</span></section>` : ''}

            <section class="algo-kpi-grid" aria-label="Algo account KPIs">
                <article class="algo-kpi algo-kpi-featured"><div class="algo-kpi-label">ACCOUNT EQUITY <span class="algo-kpi-period">${escapeHtml(snapshotDate)}</span></div><strong>${money(metrics.equity)}</strong><div class="algo-kpi-foot"><span>${isOptions ? 'Open positions' : 'Cash'} ${isOptions ? latestSnapshot?.position_count ?? '—' : money(latestSnapshot?.cash)}</span><span>${isOptions ? 'Options equity' : 'Invested'} ${isOptions ? money(metrics.equity) : money(latestSnapshot?.invested)}</span></div><div class="algo-kpi-spark"><svg viewBox="0 0 120 32" aria-hidden="true"><path d="${sparklinePath(history)}"/></svg></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">EQUITY CHANGE</div><strong class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)}</strong><div class="algo-kpi-foot"><span class="${classForReturn(metrics.total_pnl)}">${signedMoney(metrics.total_pnl)}</span><span>since first snapshot</span></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">LATEST DAILY P&amp;L</div><strong class="${classForReturn(metrics.daily_pnl)}">${signedMoney(metrics.daily_pnl)}</strong><div class="algo-kpi-foot"><span class="${classForReturn(metrics.daily_pnl_pct)}">${signedPercent(metrics.daily_pnl_pct, 3)}</span><span>last account snapshot</span></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">MAX DRAWDOWN</div><strong class="algo-kpi-negative">${metrics.max_drawdown_pct === null ? '—' : `${metrics.max_drawdown_pct.toFixed(2)}%`}</strong><div class="algo-kpi-foot"><span>snapshot series</span><span>${history.length} points</span></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">OPEN POSITIONS</div><strong>${latestSnapshot?.position_count ?? '—'}</strong><div class="algo-kpi-foot"><span>Active strategies</span><span>${account.active_strategies?.length ?? 0}</span></div></article>
            </section>

            <section class="algo-primary-grid">
                <article class="card algo-panel algo-chart-panel" data-chart-readout>
                    <div class="algo-section-heading algo-chart-heading"><div><span class="algo-eyebrow">ACCOUNT PERFORMANCE</span><h2>Equity history</h2><p>Recorded paper-account equity · USD</p></div><div class="algo-chart-summary"><strong data-chart-value data-default-chart-value="${money(metrics.equity)}">${money(metrics.equity)}</strong><span class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)} since first snapshot</span></div></div>
                    <div class="algo-range-controls" role="group" aria-label="Chart time range">${RANGES.map(value => `<button type="button" class="algo-range-btn ${range === value ? 'active' : ''}" data-range="${value}" aria-pressed="${range === value}">${value}</button>`).join('')}</div>
                    <div class="algo-chart-wrap">${renderChart(history, range, charts)}</div>
                    <div class="algo-chart-legend"><span><i></i> Account equity</span><span>Source: ${isOptions ? 'Options paper risk journal' : 'Algo intraday and close snapshots'}</span></div>
                </article>

                <article class="card algo-panel algo-strategy-panel">
                    <div class="algo-section-heading"><div><span class="algo-eyebrow">COORDINATOR</span><h2>${escapeHtml(regimeLabel)}</h2></div><span class="algo-state-pill"><i></i> ${escapeHtml(coordinatorStatusLabel)}</span></div>
                    <div class="algo-strategy-score"><div class="algo-score-ring"><span>${account.active_strategies?.length ?? 0}</span><small>/strats</small></div><div><strong>${escapeHtml(strategyListLabel)}</strong><span>${isOptions ? escapeHtml(strategyScope) : strategyScope}</span></div></div>
                    <div class="algo-active-strategy-list">${(account.active_strategies || []).map(strategy => `<span>${escapeHtml(strategy)}</span>`).join('') || `<span class="algo-muted-text">${escapeHtml(noStrategiesMessage)}</span>`}</div>
                    <div class="algo-strategy-list"><div><span>Last scan</span><strong>${escapeHtml(dateTime(account.last_scan_at))}</strong></div><div><span>Account mode</span><strong>Alpaca ${isOptions ? 'options ' : ''}paper</strong></div><div><span>Sharpe ratio</span><strong>${metrics.sharpe_ratio === null ? '—' : metrics.sharpe_ratio.toFixed(2)} <small>snapshot based</small></strong></div></div>
                    <button type="button" class="algo-text-link" data-action="open-config">Manage ${isOptions ? 'options' : 'runtime'} settings <span>↗</span></button>
                </article>
            </section>

            ${renderStrategyPerformance(snapshot.strategy_performance, isOptions, settings)}

            <section class="card algo-panel algo-config-panel" id="algo-config">
                <div class="algo-section-heading algo-config-heading"><div><span class="algo-eyebrow">${isOptions ? 'OPTIONS CONFIGURATION' : 'RUNTIME CONFIGURATION'}</span><h2>${isOptions ? 'Options strategy settings' : 'Algo strategy settings'}</h2><p>${isOptions ? 'Independent settings for the options paper account and research engine.' : 'These settings are read by the Algo coordinator and autoresearch runtime.'}</p></div><span class="algo-local-save">${isOptions ? 'OPTIONS CONFIG' : 'CONNECTED CONFIG'}</span></div>
                ${renderSettings(settings, accountMode, snapshot.settings_readonly_fields || [])}
            </section>

            <section class="algo-secondary-grid algo-secondary-grid-equity">
                <article class="card algo-panel algo-activity-panel">
                    <div class="algo-section-heading"><div><span class="algo-eyebrow">${isOptions ? 'OPTIONS ENGINE &amp; JOURNAL' : 'COORDINATOR &amp; JOURNAL'}</span><h2>Latest activity</h2></div><span class="algo-feed-label"><i></i> REFRESHING</span></div>
                    ${activities.length ? `<ol class="algo-activity-list">${visibleActivities.map(item => `<li class="algo-activity-item"><span class="algo-activity-icon ${escapeHtml(item.kind)}" aria-hidden="true">${activityIcon(item.kind)}</span><div class="algo-activity-content"><div class="algo-activity-title"><strong>${escapeHtml(item.title)}</strong><time>${escapeHtml(timeOnly(item.at))}</time></div><p>${escapeHtml(item.detail || '')}</p><span class="algo-activity-tag ${statusClass(item.status)}">${escapeHtml(item.status || item.kind)}</span></div></li>`).join('')}</ol><div class="journal-pagination algo-activity-pagination"><span>Showing ${activityStart + 1}–${Math.min(activityStart + visibleActivities.length, activities.length)} of ${activities.length} entries · ${ACTIVITY_PAGE_SIZE} per page</span>${activityPageCount > 1 ? `<div class="journal-pagination-actions"><button type="button" class="btn btn-secondary" data-activity-page="${activityPage - 1}" ${activityPage === 0 ? 'disabled' : ''} aria-label="Previous activity page">Previous</button><span>Page ${activityPage + 1} of ${activityPageCount}</span><button type="button" class="btn btn-secondary" data-activity-page="${activityPage + 1}" ${activityPage >= activityPageCount - 1 ? 'disabled' : ''} aria-label="Next activity page">Next</button></div>` : ''}</div>` : '<div class="algo-empty">No recent scans, signals or order journal entries are available.</div>'}
                    <div class="algo-view-more"><span>Entries are read from the Algo state and trade journal</span><span aria-hidden="true">↻</span></div>
                </article>
            </section>

            <section class="card algo-panel algo-history-panel" id="algo-history">
                <div class="algo-section-heading algo-history-heading"><div><span class="algo-eyebrow">EXPERIMENT ARCHIVE</span><h2>${isOptions ? 'Options research history' : 'Autoresearch history'}</h2><p>${isOptions ? 'Daily options strategy research reports.' : 'Daily saved research reports from /opt/algo-trader/data/research.'}</p></div><div class="algo-history-filters" role="group" aria-label="Filter research runs">${[['all', 'All runs'], ['promoted', isOptions ? 'Approved' : 'Promoted'], ['no-promotion', isOptions ? 'No approval' : 'No promotion']].map(([key, label]) => `<button type="button" data-research-filter="${key}" class="${researchFilter === key ? 'active' : ''}" aria-pressed="${researchFilter === key}">${label}</button>`).join('')}</div></div>
                <div class="algo-history-layout"><div class="algo-run-list" role="list">${runs.map(run => `<button type="button" role="listitem" class="algo-run-row ${run.date === selectedRunDate ? 'selected' : ''}" data-run-date="${escapeHtml(run.date)}" aria-pressed="${run.date === selectedRunDate}"><span class="algo-run-version">${escapeHtml(run.date || 'Undated')}<small>${run.created_at ? escapeHtml(timeOnly(run.created_at)) : 'saved report'}</small></span><span class="algo-run-name">${escapeHtml(run.result || 'Research report')}<small>${escapeHtml(run.options_strategies ? 'options research · research only' : run.portfolio_status || 'portfolio result unavailable')} · ${run.active_trials ?? '—'} ${isOptions ? 'strategies' : 'active trials'}</small></span><span class="algo-run-return ${classForReturn(run.portfolio_return_pct)}">${run.options_strategies ? `${run.promoted ?? 0} approved` : signedPercent(run.portfolio_return_pct)}<small>${isOptions ? 'paper config approval' : 'portfolio return'}</small></span><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span><span class="algo-run-chevron">›</span></button>`).join('') || '<div class="algo-empty">No reports match this filter.</div>'}</div>
                    <aside class="algo-run-detail">${renderRunDetail(selectedRun)}</aside></div>
            </section>

            ${!isOptions ? renderOptimizerPanel(research.optimizer, selectedOptimizerRunId) : ''}

        </main>`;
        restoreOpenConfigSections(accountMode);
        charts.attachInteractiveLineCharts(host);
    }

    function sparklinePath(history) {
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

    function readSettingsFromForm(form, mode = accountMode) {
        const source = mode === 'equity' ? snapshots.equity?.settings : snapshots.options?.settings;
        const settings = JSON.parse(JSON.stringify(source || {}));
        for (const input of form.querySelectorAll('[data-setting-path]')) {
            let value;
            if (input.type === 'checkbox') value = input.checked;
            else if (input.tagName === 'TEXTAREA') value = JSON.parse(input.value);
            else if (input.type === 'number') {
                value = input.dataset.nullable === 'true' && input.value.trim() === '' ? null : Number(input.value);
                if (input.dataset.percent === 'true' && value !== null) value /= 100;
            }
            else value = input.value;
            setPath(settings, input.dataset.settingPath, value);
        }
        return settings;
    }

    async function load({ silent = false } = {}) {
        if (document.hidden) return;
        const mode = accountMode;
        if (requestInFlight.has(mode)) return;
        requestInFlight.add(mode);
        if (!snapshots[mode] && !silent) loadingAccounts.add(mode);
        if (loadingAccounts.has(mode) && mode === accountMode) render();
        const data = await request(`/algo-trader?account=${mode}`, {}, () => {});
        requestInFlight.delete(mode);
        loadingAccounts.delete(mode);
        if (!data) {
            if (!snapshots[mode]) errors[mode] = 'Check that OpenStock can read this paper account and its Algo Trader data.';
            if (mode === accountMode) render();
            return;
        }
        if (mode === 'options' && data.account_type !== 'options') {
            snapshots.options = null;
            errors.options = 'OpenStock returned Equity data for this Options request. The running API needs the Options account route enabled.';
            if (mode === accountMode) render();
            return;
        }
        snapshots[mode] = data;
        errors[mode] = '';
        if (!dirtyAccounts.has(mode)) draftSettingsByAccount[mode] = null;
        if (mode === accountMode && !dirtyAccounts.has(mode)) render();
    }

    function startPolling() {
        if (pollTimer) return;
        pollTimer = window.setInterval(() => load({ silent: true }), 15000);
    }

    function stopPolling() {
        if (pollTimer) window.clearInterval(pollTimer);
        pollTimer = null;
    }

    host?.addEventListener('input', event => {
        if (event.target.closest('#algo-config-form')) {
            const mode = event.target.form.dataset.account || accountMode;
            dirtyAccounts.add(mode);
            try {
                draftSettingsByAccount[mode] = readSettingsFromForm(event.target.form, mode);
            } catch { /* Keep malformed JSON visible so the user can correct it. */ }
        }
    });
    host?.addEventListener('change', event => {
        if (event.target.closest('#algo-config-form')) {
            const mode = event.target.form.dataset.account || accountMode;
            dirtyAccounts.add(mode);
            try {
                draftSettingsByAccount[mode] = readSettingsFromForm(event.target.form, mode);
            } catch { /* Keep malformed JSON visible so the user can correct it. */ }
        }
    });
    host?.addEventListener('click', event => {
        const button = event.target.closest('button');
        if (!button || !host.contains(button)) return;
        if (button.dataset.accountMode) {
            const mode = button.dataset.accountMode;
            if (!['equity', 'options'].includes(mode) || mode === accountMode) return;
            const currentForm = host.querySelector('#algo-config-form');
            if (currentForm && dirtyAccounts.has(accountMode)) {
                try { draftSettingsByAccount[accountMode] = readSettingsFromForm(currentForm, accountMode); }
                catch {
                    notices[accountMode] = 'Fix the JSON values before switching accounts.';
                    const notice = currentForm.querySelector('.algo-save-notice');
                    if (notice) notice.textContent = notices[accountMode];
                    return;
                }
            }
            accountMode = mode;
            errors[mode] = '';
            researchFilter = 'all';
            selectedRunDate = null;
            activityPage = 0;
            notices[mode] ||= '';
            render();
            load({ silent: Boolean(snapshots[mode]) });
        } else if (button.dataset.range) {
            range = button.dataset.range;
            render();
        } else if (button.dataset.activityPage !== undefined) {
            const requestedPage = Number(button.dataset.activityPage);
            if (Number.isInteger(requestedPage)) {
                activityPage = requestedPage;
                render();
            }
        } else if (button.dataset.researchFilter) {
            researchFilter = button.dataset.researchFilter;
            selectedRunDate = null;
            render();
        } else if (button.dataset.optimizerRun) {
            selectedOptimizerRunId = button.dataset.optimizerRun;
            render();
        } else if (button.dataset.runDate) {
            selectedRunDate = button.dataset.runDate;
            render();
        } else if (button.dataset.action === 'open-history') {
            document.getElementById('algo-history')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else if (button.dataset.action === 'open-config') {
            document.getElementById('algo-config')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else if (button.dataset.action === 'retry') {
            errors[accountMode] = '';
            load();
        }
    });
    host?.addEventListener('submit', async event => {
        if (event.target.id !== 'algo-config-form') return;
        event.preventDefault();
        if (saving) return;
        const form = event.target;
        const mode = form.dataset.account || accountMode;
        try {
            draftSettingsByAccount[mode] = readSettingsFromForm(form, mode);
        } catch {
            notices[mode] = 'Fix the JSON values before saving settings.';
            const notice = form.querySelector('.algo-save-notice');
            if (notice) notice.textContent = notices[mode];
            return;
        }
        dirtyAccounts.add(mode);
        saving = true;
        notices[mode] = `Saving ${mode} settings…`;
        if (mode === accountMode) render();
        let saveError = '';
        const result = await request(`/algo-trader/config?account=${mode}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings: draftSettingsByAccount[mode] }) }, (message, data) => { saveError = data?.error || message || ''; });
        saving = false;
        if (!result?.settings) {
            notices[mode] = saveError ? `Save failed: ${saveError}` : 'Save failed. Check the values and retry.';
            if (mode === accountMode) render();
            return;
        }
        snapshots[mode].settings = result.settings;
        draftSettingsByAccount[mode] = null;
        dirtyAccounts.delete(mode);
        notices[mode] = `Saved · applies on the ${result.applied || 'next portfolio scan'}.`;
        if (mode === accountMode) render();
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && pollTimer) load({ silent: true });
    });

    return { load, render, startPolling, stopPolling };
}
