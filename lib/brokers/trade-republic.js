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
function configuredKey(env = process.env, file = '') {
  const keyFile = file || env.TRADE_REPUBLIC_API_KEY_FILE || (env === process.env ? '/opt/openstock/.env' : '');
  // The sidecar key rotates when it is restarted. Read the OpenStock source file
  // for every request instead of retaining the value loaded into process.env.
  return secretFromFile(keyFile, env.TRADE_REPUBLIC_API_KEY_VARIABLE || 'TRADE_REPUBLIC_API_KEY') || env.TRADE_REPUBLIC_API_KEY || env.TR_UAPI_KEY || secretFromFile(keyFile, 'TR_UAPI_KEY');
}
function finite(value) { if (value === null || value === undefined || value === '') return null; const n = Number(value); return Number.isFinite(n) ? n : null; }
function iso(value) { const d = value ? new Date(value) : null; return d && !Number.isNaN(d.getTime()) ? d.toISOString() : null; }
function historyIso(value) { const number = Number(value); return Number.isFinite(number) && number > 1000000000 ? iso(number < 100000000000 ? number * 1000 : number) : iso(value); }
function mask(value) { const s = String(value || ''); return s.length < 6 ? '••••' : `${s.slice(0, 2)}••••${s.slice(-2)}`; }
function moneyValue(value) {
  if (!value || typeof value !== 'object') return finite(value);
  const amount = finite(pick(value, ['value', 'amount']));
  const digits = finite(pick(value, ['fractionDigits', 'fraction_digits'])) ?? 0;
  return amount === null ? null : amount / (10 ** Math.max(0, digits));
}
function pick(obj, keys) { for (const key of keys) if (obj && obj[key] !== undefined) return obj[key]; return null; }
const HISTORY_RANGES = new Set(['1d', '5d', '1m', '1y', 'max']);
function brokerHistoryRange(range) { const value = String(range || '5y').toLowerCase(); const mapped = ({ '1mo': '1m', '5y': 'max' })[value] || value; if (!HISTORY_RANGES.has(mapped)) throw new BrokerError('Unsupported Trade Republic history range', 'INVALID_RANGE', 400); return mapped; }

class TradeRepublicClient extends BrokerClient {
  constructor({ baseUrl = process.env.TRADE_REPUBLIC_BASE_URL || 'http://127.0.0.1:8000', fetchImpl = globalThis.fetch, timeoutMs = 8000, env = process.env } = {}) {
    super(); this.baseUrl = baseUrl.replace(/\/$/, ''); this.fetchImpl = fetchImpl; this.timeoutMs = Math.min(Math.max(Number(timeoutMs) || 8000, 1000), 30000); this.env = env; this.apiKeyFile = env.TRADE_REPUBLIC_API_KEY_FILE || (env === process.env ? '/opt/openstock/.env' : ''); this.accountNumbers = new Map();
  }
  async request(method, endpoint, body) {
    const apiKey = configuredKey(this.env, this.apiKeyFile);
    if (!apiKey) throw new BrokerError('Trade Republic API key is not configured', 'NOT_CONFIGURED', 503);
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}${endpoint}`, { method, signal: controller.signal, cache: 'no-store', headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-API-Key': apiKey }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
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
  async getOrders({ account_ref_masked: accountRef } = {}) {
    if (!accountRef) return sanitizeCollection(await this.request('GET', '/api/orders'), 'orders');
    const account_nb = this.resolveAccount(accountRef);
    if (!account_nb) throw new BrokerError('A synchronized account must be selected for order retrieval', 'ACCOUNT_REQUIRED', 400);
    return sanitizeCollection(await this.request('POST', '/api/orders', { account_nb }), 'orders');
  }
  async getTransactions() { return sanitizeCollection(await this.request('GET', '/api/transactions'), 'transactions'); }
  async getInterests() { return sanitizeReadOnly(await this.request('GET', '/api/interests'), 'interests'); }
  async getTickets() { return sanitizeReadOnly(await this.request('GET', '/api/tickets'), 'tickets'); }
  async getAccountActivity() { return sanitizeReadOnly(await this.request('GET', '/api/accounts-activity'), 'activity'); }
  async getAccountsHistory(range = '1y') { return sanitizeReadOnly(await this.request('POST', '/api/accounts-history', { range }), 'history'); }
  async getPriceAlarms() { return sanitizeReadOnly(await this.request('GET', '/api/price-alarms'), 'price_alarms'); }
  async getExchangeSchedule(exchange) { return sanitizeReadOnly(await this.request('POST', '/api/schedule-exchange', { symbol: exchange }), 'exchange_schedule'); }
  async getInstrument(isin) { return sanitizeInstrument(await this.request('POST', '/api/instrument', { id: isin }), isin); }
  async getTradeRepublicInstrument(isin) { return sanitizeInstrument(await this.request('POST', '/api/tr-instrument', { id: isin }), isin); }
  async getInstrumentHistory({ isin, range = '5y' }) { const brokerRange = brokerHistoryRange(range); return sanitizeInstrumentHistory(await this.request('POST', '/api/instrument-history', { id: isin, range: brokerRange }), { isin, range: brokerRange }); }
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
function cleanSymbol(value) { return String(value || '').toUpperCase().replace(/[^A-Z0-9.\-]/g, '').slice(0, 20) || null; }
function listingSymbol(listing) { return cleanSymbol(scalar(listing, ['symbolAtExchange', 'slug', 'symbol', 'ticker', 'code'])); }
function exchangeCode(value) { return String(scalar(value, ['slug', 'id', 'code', 'name']) || '').toUpperCase() || null; }
function sanitizeInstrument(raw, isin) {
  const data = raw?.data || raw || {};
  const listingValues = pick(data, ['listings', 'exchanges', 'instruments']);
  const listings = (Array.isArray(listingValues) ? listingValues : []).map(item => ({ exchange: exchangeCode(pick(item, ['exchange', 'exchangeId', 'venue', 'primaryExchange', 'slug', 'id'])), symbol: listingSymbol(item), name: String(scalar(item, ['shortName', 'nextGenName', 'nameAtExchange', 'name']) || '').slice(0, 200) || null, active: item?.active === undefined ? null : Boolean(item.active) })).filter(item => item.exchange || item.symbol);
  const primaryRaw = pick(data, ['primaryExchange']);
  const primaryExchange = primaryRaw ? { exchange: exchangeCode(primaryRaw), symbol: listingSymbol(primaryRaw), name: String(scalar(primaryRaw, ['shortName', 'nextGenName', 'nameAtExchange', 'name']) || '').slice(0, 200) || null, active: primaryRaw?.active === undefined ? null : Boolean(primaryRaw.active) } : null;
  const tradable = pick(data, ['tradable', 'isTradable', 'tradeable']);
  return { isin: String(isin || pick(data, ['isin', 'id']) || '').toUpperCase(), name: String(scalar(pick(data, ['shortName', 'nextGenName', 'name', 'longName', 'instrumentName'])) || '').slice(0, 200) || null,
    intlSymbol: cleanSymbol(scalar(pick(data, ['intlSymbol', 'internationalSymbol']), ['symbolAtExchange', 'slug', 'symbol', 'ticker', 'code'])), ticker: cleanSymbol(scalar(pick(data, ['ticker', 'symbol', 'homeSymbol']), ['symbolAtExchange', 'slug', 'symbol', 'ticker', 'code'])), homeSymbol: cleanSymbol(scalar(pick(data, ['homeSymbol']), ['symbolAtExchange', 'slug', 'symbol', 'ticker', 'code'])), primaryExchange, listings,
    tradable: tradable === undefined ? null : Boolean(tradable), type: String(scalar(pick(data, ['type', 'instrumentType'])) || '').slice(0, 40) || null,
    currency: String(scalar(pick(data, ['currency', 'currencyId', 'unit']), ['id', 'code', 'name']) || '').toUpperCase() || null,
    exchange: exchangeCode(pick(data, ['exchange', 'exchangeId', 'homeExchange', 'tradingVenue'])),
    orderModes: Array.isArray(pick(data, ['orderModes', 'modes'])) ? pick(data, ['orderModes', 'modes']).map(value => String(scalar(value, ['id', 'code', 'name']) || value).slice(0, 30)) : [] };
}
function sanitizeInstrumentHistory(raw, requested = {}) {
  const data = raw?.data || raw || {};
  const aggregates = Array.isArray(data.aggregates) ? data.aggregates : [];
  const points = aggregates.map(point => { const time = historyIso(point?.time); const close = finite(point?.close); const open = finite(point?.open); const high = finite(point?.high); const low = finite(point?.low); const volume = finite(point?.volume); const adjValue = finite(point?.adjValue); if (!time || close === null || close <= 0) return null; if ([open, high, low].some(value => value !== null && value <= 0)) return null; return { time, open, high, low, close, volume, adjValue, priceEur: close, quoteTime: time, recordedAt: time.replace('T', ' ').replace(/\.\d{3}Z$/, ''), source: 'trade_republic_history', exchangeRate: null }; }).filter(Boolean);
  return { isin: String(requested.isin || '').toUpperCase(), range: String(data.range || requested.range || ''), resolution: finite(data.resolution), points };
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
  // The sidecar returns orders as a map of securities account number to an
  // orders page. Keep the account association while flattening only known
  // array containers; never treat an arbitrary object as an order.
  if (kind === 'orders' && raw && typeof raw === 'object') return Object.entries(raw).flatMap(([account, page]) => {
    const data = page?.data && typeof page.data === 'object' ? page.data : page;
    const rows = Array.isArray(data) ? data
      : (Array.isArray(data?.items) ? data.items
        : (Array.isArray(data?.orders) ? data.orders
          : (Array.isArray(data?.content) ? data.content : [])));
    return rows.map(item => ({ item, account }));
  });
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
      amount_eur: amount ? moneyValue(amount) : moneyValue(pick(item, ['amount', 'value', 'total', 'currentValue'])),
      currency: String(amount ? pick(amount, ['currency', 'unit']) : pick(item, ['currency', 'unit']) || 'EUR').toUpperCase(),
      occurred_at: iso(pick(item, ['timestamp', 'time', 'date', 'createdAt'])),
      execution_identity: pick(item, ['executionId', 'execution_id', 'tradeId', 'executionIdentity']),
      execution_price_eur: finite(pick(item, ['executionPrice', 'execution_price_eur', 'fillPrice', 'price'])),
      execution_fees_eur: finite(pick(item, ['fees', 'fee', 'feeAmount', 'executionFees'])),
      executed_at: iso(pick(item, ['executedAt', 'executionTime', 'filledAt', 'timestamp', 'time', 'date', 'createdAt']))
      ,title: typeof item?.title === 'string' ? item.title.slice(0, 200) : null
      ,subtitle: typeof item?.subtitle === 'string' ? item.subtitle.slice(0, 500) : null
    };
  });
}
function sanitizeReadOnly(raw, kind) {
  const blocked = /personal|address|iban|bic|account(number|_nb)?$|card|cookie|token|credential|password|secret|session|url|icon|action/i;
  const identifier = /^(id|orderId|transactionId|alarmId|executionId|tradeId|.*_id|.*_ref)$/i;
  const accountLike = value => /^[A-Z]{2}[A-Z0-9]{10,34}$/i.test(value) || /^\d{8,}$/.test(value);
  const cardLike = value => /^\d{12,19}$/.test(value.replace(/[ -]/g, ''));
  const uuidLike = value => /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
  const walk = (value, key = '') => {
    if (blocked.test(key)) return undefined;
    if (Array.isArray(value)) return value.slice(0, 500).map(item => walk(item, key)).filter(item => item !== undefined);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).slice(0, 100).map(([k, v]) => {
      const safeKey = accountLike(String(k)) ? mask(k) : k;
      return [safeKey, walk(v, k)];
    }).filter(([, v]) => v !== undefined));
    if (typeof value === 'string') {
      if (identifier.test(key) || accountLike(value) || cardLike(value) || uuidLike(value)) return mask(value);
      return value.length > 500 ? value.slice(0, 500) : value;
    }
    return value;
  };
  return { kind, data: walk(raw) };
}
function parseOrderResponse(raw) {
  const data = raw?.data || raw || {};
  const status = String(pick(data, ['status', 'state', 'orderStatus']) || '').toUpperCase();
  const normalized = ['FILLED', 'EXECUTED', 'CANCELLED', 'REJECTED', 'OPEN', 'PENDING', 'SUBMITTED'].includes(status) ? status : 'UNKNOWN';
  const fill = data.execution || data.fill || data.filled || {};
  const price = finite(pick(fill, ['price', 'executionPrice', 'fillPrice'])) ?? finite(pick(data, ['executionPrice', 'fillPrice', 'price']));
  const quantity = finite(pick(fill, ['quantity', 'filledQuantity', 'units'])) ?? finite(pick(data, ['filledQuantity', 'quantity']));
  const fees = finite(pick(fill, ['fees', 'fee', 'feeAmount'])) ?? finite(pick(data, ['fees', 'fee', 'feeAmount']));
  const executedAt = iso(pick(fill, ['executedAt', 'timestamp', 'time'])) || iso(pick(data, ['executedAt', 'timestamp', 'time']));
  const executionId = pick(fill, ['executionId', 'tradeId', 'id']) || pick(data, ['executionId', 'tradeId']);
  const execution_details = ['FILLED', 'EXECUTED'].includes(normalized) && executionId && price > 0 && quantity > 0 && fees !== null && fees >= 0 && executedAt ? { identity: String(executionId), price_eur: price, quantity, fees_eur: fees, executed_at: executedAt } : null;
  return { external_ref_masked: mask(pick(data, ['orderId', 'id'])), status: normalized, execution: ['FILLED', 'EXECUTED'].includes(normalized) ? 'filled' : 'none', ...(execution_details ? { execution_details } : {}) };
}
module.exports = { TradeRepublicClient, sanitizeQuote, sanitizeInstrument, sanitizeInstrumentHistory, sanitizeAccount, sanitizeCollection, sanitizeReadOnly, configuredKey, mask, parseOrderResponse, brokerHistoryRange, BrokerError };
