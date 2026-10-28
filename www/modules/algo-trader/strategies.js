import { escapeHtml } from '../escape.js';
import { numberOrNull, signedMoney, signedPercent, dateTime, classForReturn } from './format.js';

const EQUITY_STRATEGY_ALIASES = Object.freeze({
    mean_reversion: 'intraday_mean_reversion',
    trend_following: 'swing_trend_following',
    momentum: 'swing_momentum',
    intraday_trend: 'intraday_trend_following',
    multi_factor: 'swing_multi_factor',
    llm_driven: 'swing_llm_driven',
});

const EQUITY_STRATEGY_LABELS = Object.freeze({
    intraday_mean_reversion: 'Intraday Mean Reversion',
    swing_trend_following: 'Swing Trend Following',
    swing_momentum: 'Swing Momentum',
    swing_mean_reversion: 'Swing Mean Reversion',
    intraday_trend_following: 'Intraday Trend Following',
    intraday_momentum: 'Intraday Momentum',
    swing_multi_factor: 'Swing Multi-Factor',
    swing_llm_driven: 'Swing LLM-Driven',
});
const OPTIONS_STRATEGY_ALIASES = Object.freeze({
    vrp: 'options_vrp',
    llm_driven: 'options_llm_driven',
    directional_overlay: 'options_directional_overlay',
    long_volatility: 'options_long_volatility_sleeve',
    options_long_volatility: 'options_long_volatility_sleeve',
});
const OPTIONS_STRATEGY_LABELS = Object.freeze({
    options_vrp: 'Volatility Risk Premium (VRP)',
    options_llm_driven: 'LLM-Driven',
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

export function renderStrategyPerformance(performance, isOptions, settings = {}, demoMode = false) {
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
    const fxNote = demoMode
        ? 'Simulated EUR amounts'
        : scorecard.status === 'loading'
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
        <div class="algo-section-heading"><div><span class="algo-eyebrow">${demoMode ? 'DEMO STRATEGY RESULTS' : 'STRATEGY SCORECARD'}</span><h2 id="algo-strategy-pnl-title">Strategy P/L</h2><p>${demoMode ? 'Example results by strategy for preview only.' : `Latest confirmed activity and cumulative P/L by ${isOptions ? 'options' : 'equity'} strategy.`}</p></div><span class="algo-status-pill ${normalizedScorecard.status === 'ready' ? 'promising' : ''}">${escapeHtml(String(normalizedScorecard.status || 'unavailable').toUpperCase())}</span></div>
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
        <p class="algo-strategy-pnl-note">${escapeHtml(normalizedScorecard.message || (demoMode ? 'Simulated example figures.' : 'Only recorded, strategy-attributed activity is included.'))}${normalizedScorecard.source ? ` · Source: ${escapeHtml(normalizedScorecard.source)}` : ''}${demoMode ? '' : ' · Gross P/L before fees.'}</p>
    </section>`;
}
