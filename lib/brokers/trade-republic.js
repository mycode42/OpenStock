const fs = require('node:fs');
const path = require('node:path');
const { BrokerClient, BrokerError } = require('./broker-client');

function secretFromFile(file, variable) {
  try {
    const line = fs.readFileSync(file, 'utf8').split(/\r?\n/).find(x => x.trim().startsWith(`${variable}=`));
    if (!line) return '';
    return line.slice(line.indexOf('=') + 1).trim().replace(/^['"]|['"]$/g, '');
  } catch { return ''; }
}
function configuredKey(env = process.env) {
  const file = env.TRADE_REPUBLIC_API_KEY_FILE || '/opt/trade-republic-uapi/.env';
  return env.TRADE_REPUBLIC_API_KEY || secretFromFile(file, env.TRADE_REPUBLIC_API_KEY_VARIABLE || 'TRADE_REPUBLIC_API_KEY') || env.TR_UAPI_KEY || secretFromFile(file, 'TR_UAPI_KEY');
}
function finite(value) { const n = Number(value); return Number.isFinite(n) ? n : null; }
function iso(value) { const d = value ? new Date(value) : null; return d && !Number.isNaN(d.getTime()) ? d.toISOString() : null; }
function mask(value) { const s = String(value || ''); return s.length < 6 ? '••••' : `${s.slice(0, 2)}••••${s.slice(-2)}`; }
function pick(obj, keys) { for (const key of keys) if (obj && obj[key] !== undefined) return obj[key]; return null; }

class TradeRepublicClient extends BrokerClient {
  constructor({ baseUrl = process.env.TRADE_REPUBLIC_BASE_URL || 'http://127.0.0.1:8000', fetchImpl = globalThis.fetch, timeoutMs = 8000, env = process.env } = {}) {
    super(); this.baseUrl = baseUrl.replace(/\/$/, ''); this.fetchImpl = fetchImpl; this.timeoutMs = Math.min(Math.max(Number(timeoutMs) || 8000, 1000), 30000); this.apiKey = configuredKey(env); this.accountNumbers = new Map();
  }
  async request(method, endpoint, body) {
    if (!this.apiKey) throw new BrokerError('Trade Republic API key is not configured', 'NOT_CONFIGURED', 503);
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}${endpoint}`, { method, signal: controller.signal, cache: 'no-store', headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-API-Key': this.apiKey }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (!response.ok) throw new BrokerError(`Trade Republic request failed (${response.status})`, 'HTTP_ERROR', response.status);
      return await response.json();
    } catch (error) { if (error instanceof BrokerError) throw error; throw new BrokerError(error.name === 'AbortError' ? 'Trade Republic request timed out' : 'Trade Republic request unavailable', error.name === 'AbortError' ? 'TIMEOUT' : 'UNAVAILABLE', 503); }
    finally { clearTimeout(timer); }
  }
  async health() { const started = Date.now(); try { await this.request('GET', '/api/accounts'); return { status: 'ok', latency_ms: Date.now() - started }; } catch (e) { return { status: 'error', code: e.code, latency_ms: Date.now() - started }; } }
  async getPortfolio() { return sanitizeCollection(await this.request('GET', '/api/portfolio'), 'portfolio'); }
  async getAccounts() { const raw=await this.request('GET', '/api/accounts'); return (Array.isArray(raw)?raw:(raw?.data||[])).map(item => { const data=item?.data||item; const id=pick(data,['securitiesAccountNumber','accountNumber','id']); const safe=sanitizeAccount(item); if(id) this.accountNumbers.set(safe.account_ref_masked,id); return safe; }); }
  async getOrders() { return sanitizeCollection(await this.request('GET', '/api/orders'), 'orders'); }
  async getTransactions() { return sanitizeCollection(await this.request('GET', '/api/transactions'), 'transactions'); }
  async getInstrument(isin) { return sanitizeInstrument(await this.request('POST', '/api/instrument', { id: isin }), isin); }
  async getTradeRepublicInstrument(isin) { return sanitizeInstrument(await this.request('POST', '/api/tr-instrument', { id: isin }), isin); }
  async getQuote({ isin, exchange }) { return sanitizeQuote(await this.request('POST', '/api/order-price', { instrument: isin, exchange }), { isin, exchange }); }
  async previewFees(order) { const privateOrder={...order,account_nb:this.accountNumbers.get(order.account_ref_masked)}; if(!privateOrder.account_nb) throw new BrokerError('A synchronized account must be selected for fee preview','ACCOUNT_REQUIRED',400); delete privateOrder.account_ref_masked; const raw=await this.request('POST', '/api/order-fees', privateOrder), data=raw?.data||raw||{}; return { fee_eur: finite(pick(data,['fee','feeAmount','totalFee'])), currency: String(pick(data,['currency','unit'])||'EUR').toUpperCase() }; }
  async placeOrder(order) { const raw=await this.request('POST', '/api/place-order', order), data=raw?.data||raw||{}; return { external_ref_masked: mask(pick(data,['orderId','id'])), status: String(pick(data,['status','state'])||'UNKNOWN').toUpperCase() }; }
}

function sanitizeQuote(raw, requested = {}) {
  const data = raw?.data || raw || {}; const bid = finite(pick(data, ['bid', 'buy', 'buyPrice', 'bidPrice'])); const ask = finite(pick(data, ['ask', 'sell', 'sellPrice', 'askPrice']));
  const mid = finite(pick(data, ['price', 'last', 'lastPrice'])); const currency = String(pick(data, ['currency', 'unit']) || 'EUR').toUpperCase();
  return { instrument: requested.isin, exchange: requested.exchange, bid_eur: currency === 'EUR' ? bid : null, ask_eur: currency === 'EUR' ? ask : null, last_eur: currency === 'EUR' ? mid : null, currency, quote_time: iso(pick(data, ['timestamp', 'time', 'quoteTime'])), observed_at: new Date().toISOString(), source: 'trade_republic_order_price' };
}
function sanitizeInstrument(raw, isin) { const data = raw?.data || raw || {}; return { isin, name: String(pick(data, ['name', 'shortName', 'longName']) || '').slice(0, 200) || null, currency: String(pick(data, ['currency', 'unit']) || '').toUpperCase() || null, exchange: String(pick(data, ['exchange', 'homeExchange']) || '').toUpperCase() || null }; }
function sanitizeAccount(raw) { const data = raw?.data || raw || {}; const id = pick(data, ['securitiesAccountNumber', 'accountNumber', 'id']); return { account_ref_masked: mask(id), currency: String(pick(data, ['currency']) || 'EUR').toUpperCase(), cash_eur: finite(pick(data, ['cashAmount', 'availableCashAmount', 'cash'])) }; }
function sanitizeCollection(raw, kind) { const list = Array.isArray(raw) ? raw : (raw?.data || raw?.[kind] || []); return Array.isArray(list) ? list.slice(0, 1000).map(item => ({ external_ref_masked: mask(pick(item, ['id', 'orderId', 'transactionId'])), external_id: mask(pick(item, ['id', 'orderId', 'transactionId'])), status: String(pick(item, ['status', 'state']) || 'UNKNOWN').toUpperCase().slice(0, 40), instrument: String(pick(item, ['instrument', 'isin']) || '').toUpperCase() || null, quantity: finite(pick(item, ['quantity', 'units'])), amount_eur: finite(pick(item, ['amount', 'value', 'total'])), currency: String(pick(item, ['currency', 'unit']) || 'EUR').toUpperCase(), occurred_at: iso(pick(item, ['timestamp', 'date', 'createdAt'])) })) : []; }
module.exports = { TradeRepublicClient, sanitizeQuote, sanitizeInstrument, sanitizeAccount, sanitizeCollection, configuredKey, mask, BrokerError };
