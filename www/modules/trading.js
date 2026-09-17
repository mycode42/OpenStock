import { escapeHtml } from './escape.js';
import { money } from './formatting.js';

export function createTrading(context) {
  const { getStocks, request: af } = context;

  function renderTrading() {
    const el = document.getElementById('page-trading');
    const stocks = getStocks().filter(stock => stock.isin && stock.broker_mapping);
    let accounts = [];
    let portfolio = [];
    let intentKey = '';
    let intentSignature = '';
    let busy = false;
    let brokerStateReady = false;

    el.innerHTML = `<header><h1>Trading</h1><p class="subtitle">Broker-derived instrument identity with fail-closed, live account and holding checks</p></header><div id="trading-alert"></div>
      <section class="card"><form id="trading-form"><div class="form-row"><div class="form-group"><label for="tr-stock">Stock</label><select id="tr-stock" required>${stocks.map(stock => `<option value="${escapeHtml(stock.id)}">${escapeHtml(stock.symbol)} · ${escapeHtml(stock.isin)}</option>`).join('')}</select></div><div class="form-group"><label for="tr-account">Account</label><select id="tr-account" required><option value="">Loading Trade Republic accounts…</option></select></div></div>
      <div class="trading-derived" id="tr-derived"></div><div class="trading-account-summary" id="tr-account-summary"></div><div class="form-row"><div class="form-group"><label for="tr-side">Side</label><select id="tr-side"><option value="buy">BUY</option><option value="sell">SELL</option></select></div><div class="form-group"><label for="tr-mode">Mode</label><select id="tr-mode"><option value="market">Market</option><option value="limit">Limit</option><option value="stopMarket">Stop market</option></select></div><div class="form-group"><label for="tr-validity">Validity</label><select id="tr-validity"><option value="GFD">GFD</option><option value="GTD">GTD</option><option value="GTC">GTC</option></select></div></div>
      <div class="form-row"><div class="form-group conditional hidden" data-mode="limit"><label for="tr-limit">Limit price €</label><input id="tr-limit" type="number" min="0.01" step="0.01"></div><div class="form-group conditional hidden" data-mode="stopMarket"><label for="tr-stop">Stop price €</label><input id="tr-stop" type="number" min="0.01" step="0.01"></div><div class="form-group"><label for="tr-size-mode">Order size</label><select id="tr-size-mode"><option value="quantity">Shares / units</option><option value="amount">Euro value</option></select></div><div class="form-group sizing-input" data-sizing="quantity"><label for="tr-qty">Quantity</label><input id="tr-qty" type="number" min="1" step="1" required></div><div class="form-group sizing-input hidden" data-sizing="amount"><label for="tr-amount">Euro value</label><input id="tr-amount" type="number" min="0.01" step="0.01"></div></div><p class="journal-help">Euro sizing is converted to whole units using the current executable EUR price and excludes broker fees. GTD sends the documented validity value; the sidecar API does not accept a separate GTD date.</p>
      <div class="form-row"><div class="form-group"><label for="tr-confirm">Typed confirmation</label><input id="tr-confirm" placeholder="PLACE ORDER" required></div></div><p class="journal-help">OpenStock rechecks the selected Trade Republic account, position, and executable quote immediately before submitting an order. Journal entries do not authorize broker sells.</p><div class="btn-group"><button type="button" class="btn btn-secondary" id="tr-preview">Quote + fee preview</button><button type="submit" class="btn btn-danger" id="tr-submit">Submit only if every gate passes</button></div></form><div id="tr-result" class="journal-help"></div></section>
      <section class="card"><div class="section-title">Read-only Trade Republic data</div><div class="form-row"><div class="form-group"><label for="tr-history-range">History range</label><select id="tr-history-range"><option value="1d">1 day</option><option value="5d">5 days</option><option value="1m" selected>1 month</option><option value="1y">1 year</option><option value="max">Maximum</option></select></div></div><div class="btn-group"><button class="btn btn-secondary" data-ro="portfolio">Portfolio</button><button class="btn btn-secondary" data-ro="orders">Orders</button><button class="btn btn-secondary" data-ro="transactions">Transactions</button><button class="btn btn-secondary" data-ro="reconciliation-candidates">Pending Journal imports</button><button class="btn btn-secondary" data-ro="accounts-history">Account history</button><button class="btn btn-secondary" data-ro="instrument">Instrument</button><button class="btn btn-secondary" data-ro="tr-instrument">TR listing</button><button class="btn btn-secondary" data-ro="instrument-history">Instrument history</button><button class="btn btn-secondary" data-ro="schedule">Exchange schedule</button><button class="btn btn-secondary" data-ro="price-alarms">Price alarms</button><button class="btn btn-secondary" data-ro="interests">Interests</button><button class="btn btn-secondary" data-ro="tickets">Tickets</button><button class="btn btn-secondary" data-ro="activity">Activity</button></div><pre id="tr-readonly" class="readonly-output"></pre><p class="journal-help">Personal/card endpoints are intentionally not exposed. Broker alarm changes and cancellation remain disabled.</p></section>`;

    const form = el.querySelector('#trading-form');
    const previewButton = el.querySelector('#tr-preview');
    const submitButton = el.querySelector('#tr-submit');
    const selectedStock = () => stocks.find(stock => String(stock.id) === el.querySelector('#tr-stock').value);
    const formatUnits = value => Number.isFinite(Number(value)) ? Number(value).toLocaleString(undefined, { maximumFractionDigits: 6 }) : '0';

    function update() {
      const mode = el.querySelector('#tr-mode').value;
      const sizeMode = el.querySelector('#tr-size-mode').value;
      el.querySelectorAll('.conditional').forEach(input => input.classList.toggle('hidden', input.dataset.mode !== mode));
      el.querySelectorAll('.sizing-input').forEach(input => input.classList.toggle('hidden', input.dataset.sizing !== sizeMode));
      el.querySelector('#tr-qty').required = sizeMode === 'quantity';
      el.querySelector('#tr-amount').required = sizeMode === 'amount';

      const stock = selectedStock();
      el.querySelector('#tr-derived').textContent = stock
        ? `ISIN ${stock.isin} · Exchange ${stock.broker_mapping.exchange} · Currency ${stock.broker_mapping.currency} · Bid €${money(stock.broker_quote?.bid_eur)} / Ask €${money(stock.broker_quote?.ask_eur)}`
        : 'No verified Trade Republic mapping';

      const accountRef = el.querySelector('#tr-account').value;
      const account = accounts.find(row => row.account_ref_masked === accountRef);
      const position = stock && portfolio.find(row => row.account_ref_masked === accountRef && row.instrument === stock.isin);
      el.querySelector('#tr-account-summary').innerHTML = account
        ? `<span><strong>Available cash</strong> €${money(account.available_cash_eur)}</span><span><strong>${escapeHtml(stock?.symbol || 'Selected security')} held</strong> ${escapeHtml(formatUnits(position?.quantity))} units</span>`
        : '<span>Select a Trade Republic account to view cash and holdings.</span>';
    }

    el.querySelectorAll('select').forEach(input => input.addEventListener('change', update));
    form.addEventListener('input', event => {
      if (event.target.id === 'tr-confirm') return;
      intentKey = '';
      intentSignature = '';
    });
    update();
    setBusy(false);

    Promise.all([af('/broker/trade-republic/accounts'), af('/broker/trade-republic/portfolio')]).then(([accountRows, portfolioRows]) => {
      accounts = accountRows || [];
      portfolio = portfolioRows || [];
      brokerStateReady = Array.isArray(accountRows) && accountRows.length > 0;
      const select = el.querySelector('#tr-account');
      if (select) select.innerHTML = accounts.map(account => `<option value="${escapeHtml(account.account_ref_masked)}">${escapeHtml(account.account_ref_masked)} · ${escapeHtml(account.currency)}</option>`).join('') || '<option value="">No Trade Republic account available</option>';
      setBusy(false); update();
    });

    el.querySelector('#tr-preview').addEventListener('click', () => submit(false));
    form.addEventListener('submit', event => { event.preventDefault(); submit(true); });
    el.querySelectorAll('[data-ro]').forEach(button => button.addEventListener('click', () => readOnly(button.dataset.ro)));

    function generatedIntentKey() {
      const uuid = globalThis.crypto?.randomUUID?.();
      return uuid ? `ui-${uuid}` : `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    }

    function orderBody() {
      const sizeMode = el.querySelector('#tr-size-mode').value;
      const body = {
        stock_id: Number(el.querySelector('#tr-stock').value),
        account_ref: el.querySelector('#tr-account').value,
        type: el.querySelector('#tr-side').value,
        mode: el.querySelector('#tr-mode').value,
        validity: el.querySelector('#tr-validity').value,
        limit: el.querySelector('#tr-limit').value || undefined,
        stop: el.querySelector('#tr-stop').value || undefined
      };
      if (sizeMode === 'amount') body.amount_eur = Number(el.querySelector('#tr-amount').value);
      else body.quantity = Number(el.querySelector('#tr-qty').value);
      const signature = JSON.stringify(body);
      if (!intentKey || intentSignature !== signature) { intentKey = generatedIntentKey(); intentSignature = signature; }
      return { ...body, idempotency_key: intentKey, confirmation: el.querySelector('#tr-confirm').value };
    }

    function setBusy(value) {
      busy = value;
      previewButton.disabled = value || !brokerStateReady;
      submitButton.disabled = value || !brokerStateReady;
    }

    function formIsValid(place) {
      const confirmation = el.querySelector('#tr-confirm');
      if (place) return form.reportValidity();
      confirmation.required = false;
      const valid = form.reportValidity();
      confirmation.required = true;
      return valid;
    }

    async function submit(place) {
      if (busy || !formIsValid(place)) return;
      setBusy(true);
      try {
        const result = await af(place ? '/broker/trade-republic/orders' : '/broker/trade-republic/order-preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(orderBody()) }, (message, details) => {
          const labels = { account_selected: 'select a Trade Republic account', confirmed: 'type PLACE ORDER exactly', mapped: 'use a stock with a verified EUR mapping', valid_sizing_input: 'enter one valid whole-unit quantity or euro value', valid_quantity: 'use a quantity of at least one whole unit', sufficient_position: 'broker position is insufficient', notional_cap_ok: 'order exceeds the €1,000 cap', fresh_quote: 'refresh the executable broker quote', executable_quote: 'broker quote is unavailable', account_authoritative: 'broker account balance is unavailable', account_resolvable: 'selected broker account is unavailable', live_enabled: 'live execution is disabled', dry_run_disabled: 'dry-run is enabled', kill_switch_clear: 'kill switch is active', acknowledged: 'live-execution acknowledgement is missing' };
          const failures = (details?.failed_gates || []).map(gate => labels[gate] || gate.replaceAll('_', ' '));
          el.querySelector('#tr-result').textContent = failures.length ? `Cannot submit: ${failures.join('; ')}. No order was sent.` : message;
          // A preflight rejection never reached the broker, so let the user correct
          // the form and make a fresh attempt. Ambiguous broker outcomes stay frozen.
          if (place && details?.code === 'ORDER_SAFETY_GATE') { intentKey = ''; intentSignature = ''; }
        });
        if (!result) return;
        const reconciled = result.order?.reconciled === true;
        el.querySelector('#tr-result').textContent = place
          ? `Order ${result.order?.status}; Journal reconciliation: ${reconciled ? 'confirmed' : 'unchanged'}.${reconciled ? '' : ` ${result.warning || 'Pending or inconclusive orders retain the same hidden safety key; do not create a new order to retry.'}`}`
          : `Preview only · ${result.quantity} unit${result.quantity === 1 ? '' : 's'} · pricing €${money(result.sizing_price_eur)} · fee €${money(result.fee_eur)} · estimated total €${money(result.total_eur)}`;
        if (place && reconciled) {
          intentKey = '';
          intentSignature = '';
          el.querySelector('#tr-confirm').value = '';
        }
      } finally {
        setBusy(false);
      }
    }

    async function readOnly(kind) {
      const stockId = Number(el.querySelector('#tr-stock').value);
      const range = el.querySelector('#tr-history-range').value;
      const selected = {
        instrument: `/broker/trade-republic/instrument/${stockId}`,
        'tr-instrument': `/broker/trade-republic/tr-instrument/${stockId}`,
        'instrument-history': `/broker/trade-republic/instrument-history/${stockId}?range=${encodeURIComponent(range)}`,
        schedule: `/broker/trade-republic/schedule/${stockId}`
      };
      const result = await af(kind === 'accounts-history' ? '/broker/trade-republic/accounts-history' : selected[kind] || `/broker/trade-republic/${kind}`, kind === 'accounts-history' ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ range }) } : {});
      if (result) el.querySelector('#tr-readonly').textContent = JSON.stringify(result, null, 2);
    }
  }

  return { renderTrading };
}
