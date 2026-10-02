'use strict';

// This adapter intentionally exposes only executable quote reads. Do not add
// portfolio, orders, preview, or mutation methods here.
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { BrokerError } = require('./broker-client');

function enabled(value) { return String(value ?? 'true').toLowerCase() === 'true'; }
function finite(value) { const number = Number(value); return Number.isFinite(number) && number > 0 ? number : null; }
function iso(value) { const date = value ? new Date(value) : null; return date && !Number.isNaN(date.getTime()) ? date.toISOString() : null; }
// SDK transport errors can contain request headers or session details. Never
// forward their text into OpenStock logs or responses.
function safeError() { return 'request failed'; }

class HandelsrepublikQuoteClient {
  constructor({
    sessionFile = process.env.TRADE_REPUBLIC_SDK_SESSION_FILE || '',
    modulePath = process.env.TRADE_REPUBLIC_SDK_MODULE_PATH || '/opt/handelsrepublik/dist/index.js',
    timeoutMs = 10000,
    env = process.env,
    importer = specifier => import(specifier)
  } = {}) {
    this.sessionFile = sessionFile;
    this.modulePath = modulePath;
    this.timeoutMs = Math.min(Math.max(Number(timeoutMs) || 10000, 1000), 30000);
    this.env = env;
    this.importer = importer;
    this.client = null;
    // A dashboard refresh fans out several quote reads concurrently. Session
    // restoration is stateful (and refresh writes the session file), so it
    // must be single-flight. Quote reads themselves remain concurrent once
    // this promise resolves.
    this.connecting = null;
  }

  isEnabled() { return enabled(this.env.TRADE_REPUBLIC_SDK_ENABLED); }

  async connect() {
    if (!this.isEnabled()) throw new BrokerError('Trade Republic SDK quote source is disabled', 'DISABLED', 503);
    if (!this.sessionFile) throw new BrokerError('Trade Republic SDK session file is not configured', 'NOT_CONFIGURED', 503);
    if (this.client) return this.client;
    if (this.connecting) return this.connecting;
    const connecting = (async () => {
      let client = null;
      try {
        const sdk = await this.importer(pathToFileURL(path.resolve(this.modulePath)).href);
        const sessionStore = new sdk.FileSessionStore(this.sessionFile);
        client = sdk.TradeRepublicClient.create({ sessionStore, rawSchemaValidation: 'throw' });
        const restored = await client.auth.restoreSession();
        if (!restored) throw new Error('saved SDK session is unavailable');
        await client.auth.refreshSession({});
        this.client = client;
        return client;
      } catch (error) {
        try { client?.close(); } catch {}
        throw new BrokerError(`Trade Republic SDK quote source unavailable: ${safeError(error)}`, 'UNAVAILABLE', 503);
      }
    })();
    this.connecting = connecting;
    try { return await connecting; }
    finally { if (this.connecting === connecting) this.connecting = null; }
  }

  async quoteFrom(client, { isin, exchange, timeoutMs = this.timeoutMs }) {
    try {
      const [sell, buy] = await Promise.all([
        client.trading.priceForOrder({ instrumentId: isin, exchangeId: exchange, side: 'sell', unit: 'EUR' }, { timeoutMs: Math.max(1, Math.min(this.timeoutMs, Number(timeoutMs) || this.timeoutMs)) }),
        client.trading.priceForOrder({ instrumentId: isin, exchangeId: exchange, side: 'buy', unit: 'EUR' }, { timeoutMs: Math.max(1, Math.min(this.timeoutMs, Number(timeoutMs) || this.timeoutMs)) })
      ]);
      const bid = finite(sell?.bid) ?? finite(sell?.price);
      const ask = finite(buy?.ask) ?? finite(buy?.price);
      const bidUnit = String(sell?.unit || 'EUR').toUpperCase();
      const askUnit = String(buy?.unit || 'EUR').toUpperCase();
      if ((bid === null && ask === null) || bidUnit !== 'EUR' || askUnit !== 'EUR') throw new Error('SDK returned no EUR executable quote');
      const quoteTime = iso(sell?.time) || iso(buy?.time) || new Date().toISOString();
      return { instrument: isin, exchange, bid_eur: bid, ask_eur: ask, bid_time: iso(sell?.time) || quoteTime, ask_time: iso(buy?.time) || quoteTime, bid_unit: bidUnit, ask_unit: askUnit, currency: 'EUR', quote_time: quoteTime, observed_at: new Date().toISOString(), source: 'trade_republic_sdk_order_price' };
    } catch (error) {
      if (error instanceof BrokerError) throw error;
      throw new BrokerError(`Trade Republic SDK quote unavailable: ${safeError(error)}`, 'UNAVAILABLE', 503);
    }
  }

  reconnect(failedClient) {
    // Do not tear down a replacement another concurrent request has already
    // established; all callers converge on the same replacement connection.
    if (this.client === failedClient) this.close();
    return this.connect();
  }

  async getQuote({ isin, exchange, timeoutMs }) {
    if (!isin || !exchange) throw new BrokerError('Instrument and exchange are required for SDK quote', 'INVALID_REQUEST', 400);
    const client = await this.connect();
    try { return await this.quoteFrom(client, { isin, exchange, timeoutMs }); }
    catch (error) {
      if (error instanceof BrokerError && error.code !== 'UNAVAILABLE') throw error;
      const replacement = await this.reconnect(client);
      return this.quoteFrom(replacement, { isin, exchange, timeoutMs });
    }
  }

  close() { try { this.client?.close(); } catch {} finally { this.client = null; } }
}

module.exports = { HandelsrepublikQuoteClient, enabled };
