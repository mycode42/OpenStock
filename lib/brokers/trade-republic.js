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
function finite(value) { if (value === null || value === undefined || value === '') return null; const n = Number(value); return Number.isFinite(n) ? n : null; }
function iso(value) { const d = value ? new Date(value) : null; return d && !Number.isNaN(d.getTime()) ? d.toISOString() : null; }
function historyIso(value) { const number = Number(value); return Number.isFinite(number) && number > 1000000000 ? iso(number < 100000000000 ? number * 1000 : number) : iso(value); }
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
      if (!response.ok) {
        const deterministic = [400, 409, 422].includes(response.status);
        throw new BrokerError(`Trade Republic request failed (${response.status})`, deterministic ? 'REJECTED' : 'HTTP_ERROR', response.status >= 500 ? 503 : response.status);
      }
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
  async getInstrumentHistory({ isin, range = '5y' }) { return sanitizeInstrumentHistory(await this.request('POST', '/api/instrument-history', { id: isin, range }), { isin, range }); }
  async getQuote({ isin, exchange }) { return sanitizeQuote(await this.request('POST', '/api/order-price', { instrument: isin, exchange }), { isin, exchange }); }
  async previewFees(order) { const privateOrder={...order,account_nb:this.accountNumbers.get(order.account_ref_masked)}; if(!privateOrder.account_nb) throw new BrokerError('A synchronized account must be selected for fee preview','ACCOUNT_REQUIRED',400); delete privateOrder.account_ref_masked; const raw=await this.request('POST', '/api/order-fees', privateOrder), data=raw?.data||raw||{}; return { fee_eur: finite(pick(data,['fee','feeAmount','totalFee'])), currency: String(pick(data,['currency','unit'])||'EUR').toUpperCase() }; }
  resolveAccount(accountRefMasked) { return this.accountNumbers.get(String(accountRefMasked || '')) || null; }
  async placeOrder(order) {
    const privateOrder = { ...order, account_nb: this.resolveAccount(order.account_ref_masked) };
    delete privateOrder.account_ref_masked;
    if (!privateOrder.account_nb) throw new BrokerError('Synchronized account is required', 'ACCOUNT_REQUIRED', 400);
    const raw = await this.request('POST', '/api/place-order', privateOrder);
    return parseOrderResponse(raw);
  }
}

function sanitizeQuote(raw, requested = {}) {
  const data = raw?.data || raw || {};
  const sell = data.sell || {}; const buy = data.buy || {};
  const bidValue = finite(pick(sell, ['price', 'bidPrice', 'bid'])) ?? finite(pick(data, ['bid', 'bidPrice']));
  const askValue = finite(pick(buy, ['price', 'askPrice', 'ask'])) ?? finite(pick(data, ['ask', 'askPrice']));
  const bidUnit = String(pick(sell, ['unit', 'currency']) || pick(data, ['unit', 'currency']) || 'EUR').toUpperCase();
  const askUnit = String(pick(buy, ['unit', 'currency']) || pick(data, ['unit', 'currency']) || 'EUR').toUpperCase();
  const quoteTime = iso(pick(data, ['timestamp', 'quoteTime']));
  return {
    instrument: requested.isin, exchange: requested.exchange,
    bid_eur: bidUnit === 'EUR' ? bidValue : null, ask_eur: askUnit === 'EUR' ? askValue : null,
    bid_time: iso(pick(sell, ['time', 'timestamp'])) || quoteTime,
    ask_time: iso(pick(buy, ['time', 'timestamp'])) || quoteTime,
    bid_unit: bidUnit, ask_unit: askUnit, currency: bidUnit === askUnit ? bidUnit : null,
    quote_time: quoteTime, observed_at: new Date().toISOString(), source: 'trade_republic_order_price'
  };
}
function scalar(value, keys = ['id', 'code', 'name']) { return value && typeof value === 'object' ? pick(value, keys) : value; }
function listingSymbol(listing) { return String(scalar(listing, ['symbol', 'ticker', 'code', 'name']) || '').toUpperCase().replace(/[^A-Z0-9.\-]/g, '').slice(0, 20) || null; }
function sanitizeInstrument(raw, isin) {
  const data = raw?.data || raw || {};
  const listingValues = pick(data, ['listings', 'exchanges', 'instruments']);
  const listings = (Array.isArray(listingValues) ? listingValues : []).map(item => ({ exchange: String(scalar(pick(item, ['exchange', 'exchangeId', 'venue']), ['id', 'code', 'name']) || '').toUpperCase() || null, symbol: listingSymbol(item) })).filter(item => item.exchange || item.symbol);
  const tradable = pick(data, ['tradable', 'isTradable', 'tradeable']);
  return { isin: String(isin || pick(data, ['isin', 'id']) || '').toUpperCase(), name: String(scalar(pick(data, ['name', 'shortName', 'longName', 'instrumentName'])) || '').slice(0, 200) || null,
    intlSymbol: listingSymbol(pick(data, ['intlSymbol', 'internationalSymbol'])), ticker: listingSymbol(pick(data, ['ticker', 'symbol'])), listings,
    tradable: tradable === undefined ? null : Boolean(tradable), type: String(scalar(pick(data, ['type', 'instrumentType'])) || '').slice(0, 40) || null,
    currency: String(scalar(pick(data, ['currency', 'unit']), ['id', 'code', 'name']) || '').toUpperCase() || null,
    exchange: String(scalar(pick(data, ['exchange', 'homeExchange']), ['id', 'code', 'name']) || '').toUpperCase() || null,
    orderModes: Array.isArray(pick(data, ['orderModes', 'modes'])) ? pick(data, ['orderModes', 'modes']).map(value => String(scalar(value, ['id', 'code', 'name']) || value).slice(0, 30)) : [] };
}
function sanitizeInstrumentHistory(raw, requested = {}) {
  const data = raw?.data || raw || {};
  const aggregates = Array.isArray(data.aggregates) ? data.aggregates : [];
  const points = aggregates.map(point => { const time = historyIso(point?.time); const close = finite(point?.close); const open = finite(point?.open); const high = finite(point?.high); const low = finite(point?.low); const volume = finite(point?.volume); const adjValue = finite(point?.adjValue); if (!time || close === null || close <= 0) return null; if ([open, high, low].some(value => value !== null && value <= 0)) return null; return { time, open, high, low, close, volume, adjValue, priceEur: close, quoteTime: time, recordedAt: time.replace('T', ' ').replace(/\.\d{3}Z$/, ''), source: 'trade_republic_history', exchangeRate: null }; }).filter(Boolean);
  return { isin: String(requested.isin || '').toUpperCase(), range: String(data.range || requested.range || ''), resolution: String(data.resolution || '').slice(0, 20) || null, points };
}
function sanitizeAccount(raw) {
  const data = raw?.data || raw || {}; const id = pick(data, ['securitiesAccountNumber', 'accountNumber', 'id']);
  const cash = finite(pick(data, ['cashAmount', 'cash', 'balance']));
  const available = finite(pick(data, ['availableCashAmount', 'availableCash', 'availableBalance']));
  return { account_ref_masked: mask(id), currency: String(pick(data, ['currency']) || 'EUR').toUpperCase(), cash_eur: cash, available_cash_eur: available };
}
function collectionItems(raw, kind) {
  if (Array.isArray(raw)) return raw.map(item => ({ item, account: null }));
  if (Array.isArray(raw?.data)) return raw.data.map(item => ({ item, account: null }));
  if (Array.isArray(raw?.[kind])) return raw[kind].map(item => ({ item, account: null }));
  if (Array.isArray(raw?.items)) return raw.items.map(item => ({ item, account: null }));
  if (kind === 'portfolio' && raw && typeof raw === 'object') return Object.entries(raw).flatMap(([account, portfolio]) =>
    (Array.isArray(portfolio?.categories) ? portfolio.categories : []).flatMap(category =>
      (Array.isArray(category?.positions) ? category.positions : []).map(item => ({ item, account }))));
  return [];
}
function sanitizeCollection(raw, kind) {
  return collectionItems(raw, kind).slice(0, 1000).map(({ item, account }) => {
    const nested = item?.instrument && typeof item.instrument === 'object' ? item.instrument : null;
    const isin = pick(item, ['isin']) || pick(nested, ['isin', 'id']) || (typeof item?.instrument === 'string' ? item.instrument : null);
    const amount = item?.amount && typeof item.amount === 'object' ? item.amount : null;
    const rawSide = String(pick(item, ['side', 'type']) || '').toLowerCase();
    return {
      account_ref_masked: mask(account || pick(item, ['accountNumber', 'securitiesAccountNumber'])),
      external_ref_masked: mask(pick(item, ['id', 'orderId', 'transactionId', 'positionId'])),
      external_id: mask(pick(item, ['id', 'orderId', 'transactionId', 'positionId'])),
      status: String(pick(item, ['status', 'state']) || 'UNKNOWN').toUpperCase().slice(0, 40),
      instrument: String(isin || '').toUpperCase() || null,
      side: ['buy', 'sell'].includes(rawSide) ? rawSide : null,
      // Trade Republic portfolio positions expose holdings as netSize (and,
      // for some position types, virtualSize). Prefer those authoritative
      // fields before generic collection quantities so sell risk gates do not
      // silently treat every holding as missing.
      quantity: finite(pick(item, ['netSize', 'virtualSize', 'quantity', 'units', 'size'])),
      amount_eur: finite(amount ? pick(amount, ['value', 'amount']) : pick(item, ['amount', 'value', 'total', 'currentValue'])),
      currency: String(amount ? pick(amount, ['currency', 'unit']) : pick(item, ['currency', 'unit']) || 'EUR').toUpperCase(),
      occurred_at: iso(pick(item, ['timestamp', 'time', 'date', 'createdAt']))
    };
  });
}
function parseOrderResponse(raw) {
  const data = raw?.data || raw || {};
  const status = String(pick(data, ['status', 'state', 'orderStatus']) || '').toUpperCase();
  const normalized = ['FILLED', 'EXECUTED', 'CANCELLED', 'REJECTED', 'OPEN', 'PENDING', 'SUBMITTED'].includes(status) ? status : 'UNKNOWN';
  return { external_ref_masked: mask(pick(data, ['orderId', 'id'])), status: normalized, execution: ['FILLED', 'EXECUTED'].includes(normalized) ? 'filled' : 'none' };
}
module.exports = { TradeRepublicClient, sanitizeQuote, sanitizeInstrument, sanitizeInstrumentHistory, sanitizeAccount, sanitizeCollection, configuredKey, mask, parseOrderResponse, BrokerError };
