import { escapeHtml } from './escape.js';

const usd = value => Number.isFinite(Number(value)) ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value)) : '—';
const number = value => Number.isFinite(Number(value)) ? String(Number(value)) : '';
const when = value => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' }).format(date);
};
const age = value => {
  const timestamp = Date.parse(value || '');
  if (!Number.isFinite(timestamp)) return 'No completed cycle yet';
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  return seconds < 60 ? 'just now' : seconds < 3600 ? `${Math.floor(seconds / 60)} min ago` : `${Math.floor(seconds / 3600)} hr ago`;
};

export function createTrader(context) {
  let status = null;
  let pollTimer = null;
  const pollMs = 30000;

  async function refresh() {
    const value = await context.request('/trader/status');
    if (value) { status = value; render(); }
  }
  function startPolling() {
    stopPolling();
    pollTimer = window.setInterval(() => {
      if (document.hidden || window.location.hash !== '#/trader') return;
      refresh();
    }, pollMs);
  }
  function stopPolling() { if (pollTimer !== null) { window.clearInterval(pollTimer); pollTimer = null; } }
  async function save(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = { symbols: form.symbols.value.split(',').map(value => value.trim()).filter(Boolean), strategy: 'percent_band', buyDropPercent: Number(form.buyDropPercent.value), sellRisePercent: Number(form.sellRisePercent.value), maxOrderUsd: Number(form.maxOrderUsd.value), enabled: form.enabled.checked };
    const value = await context.request('/trader/config', { method: 'PUT', body: JSON.stringify(payload), headers: { 'content-type': 'application/json' } });
    if (value) { status = value; context.showA('Paper strategy saved.', 'success', 'trader-alert'); render(); }
  }
  async function testBuy(event) {
    event.preventDefault();
    const symbol = event.currentTarget.symbol.value.trim().toUpperCase();
    if (!window.confirm(`Submit a one-share ${symbol} market buy to the Alpaca paper account?`)) return;
    const result = await context.request('/trader/test-buy', { method: 'POST', body: JSON.stringify({ symbol }), headers: { 'content-type': 'application/json' } });
    if (result?.order) context.showA(`Paper order accepted: ${result.order.symbol} × ${result.order.qty} (${result.order.status || 'submitted'}).`, 'success', 'trader-alert');
    await refresh();
  }
  function stateFor(symbol, strategy, loop) {
    if ((loop.pendingSymbols || []).includes(symbol)) return ['Needs reconciliation', 'warning'];
    if (!strategy.enabled) return ['Strategy disabled', 'muted'];
    if (!status.executionEnabled) return ['Execution gate blocked', 'warning'];
    if (!status.clock?.isOpen) return ['Market closed', 'muted'];
    return ['Monitoring', 'active'];
  }
  function symbolCard(symbol, strategy, loop) {
    const [label, tone] = stateFor(symbol, strategy, loop);
    return `<article class="trader-symbol-card"><div class="trader-symbol-heading"><strong>${escapeHtml(symbol)}</strong><span class="trader-state ${tone}">${escapeHtml(label)}</span></div><dl><div><dt>Buy rule</dt><dd>${number(strategy.buyDropPercent)}% below tracked high</dd></div><div><dt>Sell rule</dt><dd>${number(strategy.sellRisePercent)}% above strategy entry</dd></div><div><dt>Order cap</dt><dd>${usd(strategy.maxOrderUsd)} · 1 share</dd></div></dl><p>Price and trigger distance are evaluated by the paper runner each cycle; this dashboard does not submit orders.</p></article>`;
  }
  function render() {
    const container = document.getElementById('page-trader');
    if (!container) return;
    if (!status) { container.innerHTML = '<div class="loading">Loading trader status…</div>'; return; }
    const strategy = status.strategy || {};
    const loop = status.loop || {};
    const symbols = (strategy.symbols || []).join(', ');
    const enabled = strategy.enabled && status.executionEnabled;
    const cycleClass = loop.running && loop.lastTickAt ? 'active' : 'muted';
    container.innerHTML = `<header class="section-header"><div><h1>Trader</h1><p class="subtitle">Alpaca paper strategy monitor · refreshes while this page is open</p></div><button class="btn btn-secondary" id="trader-refresh">Refresh now</button></header><div id="trader-alert"></div><div class="alert-box ${enabled ? 'success' : 'info'}"><strong>${escapeHtml(status.environment === 'paper' ? 'Paper account' : 'Unavailable')}:</strong> ${enabled ? 'Strategy execution is enabled.' : 'Strategy execution is not enabled.'} Live trading is not supported.</div><section class="trader-overview" aria-label="Strategy overview"><article class="trader-kpi"><span>Strategy state</span><strong class="${enabled ? 'positive' : ''}">${enabled ? 'Monitoring' : 'Not active'}</strong><small>${escapeHtml(strategy.strategy === 'percent_band' ? 'Percent-band · paper only' : 'Strategy unavailable')}</small></article><article class="trader-kpi"><span>Market</span><strong>${status.clock?.isOpen ? 'Open' : 'Closed'}</strong><small>${status.clock?.isOpen ? 'Runner may evaluate configured symbols' : `Next open: ${when(status.clock?.nextOpen)}`}</small></article><article class="trader-kpi"><span>Last completed cycle</span><strong class="${cycleClass}">${escapeHtml(age(loop.lastTickAt))}</strong><small>${escapeHtml(loop.lastResult || 'No runner result yet')}</small></article><article class="trader-kpi"><span>Paper buying power</span><strong>${usd(status.account?.buyingPowerUsd)}</strong><small>Cash ${usd(status.account?.cashUsd)} · account ${escapeHtml(status.account?.status || 'unknown')}</small></article></section><section class="card"><div class="section-header"><div><div class="section-title">Configured symbols</div><p class="subtitle">Strategy rules and current runner status. A pending symbol is frozen until reconciled.</p></div></div><div class="trader-symbol-grid">${(strategy.symbols || []).map(symbol => symbolCard(symbol, strategy, loop)).join('') || '<p class="journal-empty">No symbols configured.</p>'}</div><p class="trader-observed">Last runner observation: ${when(loop.lastTickAt)}. Dashboard polling: every 30 seconds while this page is visible.</p></section><section class="trader-status-grid"><article class="card"><div class="section-title">Runner safeguards</div><dl class="trader-facts"><div><dt>Environment</dt><dd>${escapeHtml(status.environment || '—')}</dd></div><div><dt>Environment opt-in</dt><dd>${status.environmentGate ? 'Ready' : 'Blocked'}</dd></div><div><dt>Acknowledgement</dt><dd>${status.acknowledgementGate ? 'Ready' : 'Blocked'}</dd></div><div><dt>Loop state</dt><dd>${loop.running ? (loop.inFlight ? 'Evaluating' : 'Running') : 'Stopped'}</dd></div><div><dt>Hard order ceiling</dt><dd>${usd(status.hardMaxOrderUsd)}</dd></div><div><dt>Account availability</dt><dd>${status.account?.tradingBlocked || status.account?.accountBlocked ? 'Blocked' : 'Available'}</dd></div></dl></article><article class="card"><div class="section-title">Percent-band strategy</div><p class="journal-help">A paper-only cycle tracks the high ask, buys one whole share after the configured drop, then sells the strategy-managed share after the configured rise. Unknown outcomes freeze the symbol for reconciliation.</p><form id="trader-config" class="trader-form"><label>US tickers<input name="symbols" required maxlength="64" value="${escapeHtml(symbols)}" placeholder="SPY, AAPL"></label><label>Buy drop (%)<input name="buyDropPercent" type="number" min="0.01" max="20" step="0.01" required value="${number(strategy.buyDropPercent)}"></label><label>Sell rise (%)<input name="sellRisePercent" type="number" min="0.01" max="20" step="0.01" required value="${number(strategy.sellRisePercent)}"></label><label>Maximum order (USD)<input name="maxOrderUsd" type="number" min="1" max="${number(status.hardMaxOrderUsd) || 50}" step="1" required value="${number(strategy.maxOrderUsd)}"></label><label class="checkbox"><input name="enabled" type="checkbox" ${strategy.enabled ? 'checked' : ''}> Enable paper strategy</label><button class="btn btn-primary" type="submit">Save paper strategy</button></form></article></section><section class="card"><div class="section-header"><div><div class="section-title">Paper test order</div><p class="subtitle">Requires the enabled paper strategy, a configured symbol, open market, a fresh ask, and the runner cap.</p></div></div><form id="trader-test-buy" class="form-row"><div class="form-group"><label for="trader-symbol">Configured ticker</label><input id="trader-symbol" name="symbol" required maxlength="12" placeholder="SPY"></div><div class="form-group"><label>&nbsp;</label><button class="btn btn-danger" type="submit" ${status.executionEnabled ? '' : 'disabled'}>Place 1-share paper buy</button></div></form></section>`;
    container.querySelector('#trader-refresh')?.addEventListener('click', refresh);
    container.querySelector('#trader-config')?.addEventListener('submit', save);
    container.querySelector('#trader-test-buy')?.addEventListener('submit', testBuy);
  }
  return { render, refresh, startPolling, stopPolling, setStatus: value => { status = value; } };
}
