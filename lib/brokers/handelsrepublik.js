'use strict';

// This adapter exposes quote and historical candle reads only. Do not add
// portfolio, orders, preview, or mutation methods here.
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { BrokerError } = require('./broker-client');

const AUTH_RETRY_DELAYS_MS = [15, 30, 60].map(minutes => minutes * 60_000);
const AUTH_LOCK_STALE_MS = 2 * 60_000;

function enabled(value) { return String(value ?? 'true').toLowerCase() === 'true'; }
function finite(value) { const number = Number(value); return Number.isFinite(number) && number > 0 ? number : null; }
function iso(value) { const date = value ? new Date(value) : null; return date && !Number.isNaN(date.getTime()) ? date.toISOString() : null; }
// SDK transport errors can contain request headers or session details. Never
// forward their text into OpenStock logs or responses.
function safeError() { return 'request failed'; }
function filesystemFailure(error) {
  const code = String(error?.code || error?.cause?.code || '');
  if (code === 'EACCES' || code === 'EPERM') return 'SDK session file access denied';
  if (code === 'ENOENT') return 'SDK module or session file not found';
  return null;
}
function sessionRejected(error) { return error?.status === 401 || error?.status === 403; }

class HandelsrepublikQuoteClient {
  constructor({
    sessionFile = process.env.TRADE_REPUBLIC_SDK_SESSION_FILE || '',
    modulePath = process.env.TRADE_REPUBLIC_SDK_MODULE_PATH || '/opt/handelsrepublik/dist/index.js',
    timeoutMs = 10000,
    authTimeoutMs = process.env.TRADE_REPUBLIC_SDK_AUTH_TIMEOUT_MS || 45000,
    env = process.env,
    importer = specifier => import(specifier)
  } = {}) {
    this.sessionFile = sessionFile;
    this.modulePath = modulePath;
    this.timeoutMs = Math.min(Math.max(Number(timeoutMs) || 10000, 1000), 30000);
    this.authTimeoutMs = Math.min(Math.max(Number(authTimeoutMs) || 45000, 1000), 60000);
    this.env = env;
    this.importer = importer;
    this.phoneNumber = String(env.TRADE_REPUBLIC_SDK_PHONE_NUMBER || '').trim();
    this.pin = String(env.TRADE_REPUBLIC_SDK_PIN || '');
    this.authRetryFile = this.sessionFile ? `${this.sessionFile}.reauth-state.json` : '';
    this.authLockFile = this.sessionFile ? `${this.sessionFile}.reauth.lock` : '';
    this.client = null;
    // A dashboard refresh fans out several quote reads concurrently. Session
    // restoration is stateful (and refresh writes the session file), so it
    // must be single-flight. Quote reads themselves remain concurrent once
    // this promise resolves.
    this.connecting = null;
  }

  isEnabled() { return enabled(this.env.TRADE_REPUBLIC_SDK_ENABLED); }

  readAuthRetryState() {
    if (!this.authRetryFile) return null;
    try { return JSON.parse(fs.readFileSync(this.authRetryFile, 'utf8')); }
    catch (error) {
      if (error.code === 'ENOENT') return null;
      throw new BrokerError('SDK PIN reauthentication retry state is unreadable', 'AUTH_COOLDOWN', 503);
    }
  }

  clearAuthRetryState() {
    if (!this.authRetryFile) return;
    try { fs.unlinkSync(this.authRetryFile); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }

  assertAuthRetryAllowed({ force = false } = {}) {
    const state = this.readAuthRetryState();
    if (!force && Number(state?.retryAfter) > Date.now()) throw new BrokerError('SDK PIN reauthentication is paused by retry cooldown', 'AUTH_COOLDOWN', 503);
    if (!this.authLockFile) return;
    try {
      const age = Date.now() - fs.statSync(this.authLockFile).mtimeMs;
      if (age <= AUTH_LOCK_STALE_MS) throw new BrokerError('SDK PIN reauthentication is already in progress', 'AUTH_COOLDOWN', 503);
      fs.unlinkSync(this.authLockFile);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }

  async reauthenticate(client, { force = false } = {}) {
    this.assertAuthRetryAllowed({ force });
    fs.mkdirSync(path.dirname(this.authLockFile), { recursive: true, mode: 0o700 });
    let lock;
    try { lock = fs.openSync(this.authLockFile, 'wx', 0o600); }
    catch (error) {
      if (error.code === 'EEXIST') throw new BrokerError('SDK PIN reauthentication is already in progress', 'AUTH_COOLDOWN', 503);
      throw error;
    }
    fs.closeSync(lock);
    try {
      const state = this.readAuthRetryState();
      if (!force && Number(state?.retryAfter) > Date.now()) throw new BrokerError('SDK PIN reauthentication is paused by retry cooldown', 'AUTH_COOLDOWN', 503);
      const failures = Math.max(0, Number(state?.failures) || 0) + 1;
      const delay = AUTH_RETRY_DELAYS_MS[Math.min(failures - 1, AUTH_RETRY_DELAYS_MS.length - 1)];
      const tempFile = `${this.authRetryFile}.${process.pid}.tmp`;
      fs.writeFileSync(tempFile, JSON.stringify({ failures, retryAfter: Date.now() + delay }), { mode: 0o600 });
      fs.renameSync(tempFile, this.authRetryFile);
      await client.auth.loginWithPin({ phoneNumber: this.phoneNumber, pin: this.pin, timeoutMs: this.authTimeoutMs });
      try { fs.unlinkSync(this.authRetryFile); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    } finally {
      try { fs.unlinkSync(this.authLockFile); } catch {}
    }
  }

  async connect({ forceAuthRetry = false } = {}) {
    if (!this.isEnabled()) throw new BrokerError('Trade Republic SDK quote source is disabled', 'DISABLED', 503);
    if (!this.sessionFile) throw new BrokerError('Trade Republic SDK session file is not configured', 'NOT_CONFIGURED', 503);
    if (this.client) return this.client;
    if (this.connecting) return this.connecting;
    this.assertAuthRetryAllowed({ force: forceAuthRetry });
    const connecting = (async () => {
      let client = null;
      let failureReason = 'session restore or refresh failed';
      try {
        const sdk = await this.importer(pathToFileURL(path.resolve(this.modulePath)).href);
        const sessionStore = new sdk.FileSessionStore(this.sessionFile);
        client = sdk.TradeRepublicClient.create({ sessionStore, rawSchemaValidation: 'throw' });
        const restored = await client.auth.restoreSession();
        if (restored) {
          try {
            await client.auth.refreshSession({});
            this.clearAuthRetryState();
          }
          catch (error) {
            if (!sessionRejected(error)) {
              failureReason = 'saved SDK session refresh failed';
              throw error;
            }
            if (!this.phoneNumber || !this.pin) {
              failureReason = 'saved SDK session was rejected; PIN reauthentication is not configured';
              throw error;
            }
            await client.auth.clearSession();
            failureReason = 'SDK PIN reauthentication failed';
            await this.reauthenticate(client, { force: forceAuthRetry });
          }
        } else {
          if (!this.phoneNumber || !this.pin) {
            failureReason = 'saved SDK session is missing; PIN reauthentication is not configured';
            throw new Error('saved SDK session is unavailable');
          }
          failureReason = 'SDK PIN reauthentication failed';
          await this.reauthenticate(client, { force: forceAuthRetry });
        }
        this.client = client;
        return client;
      } catch (error) {
        try { client?.close(); } catch {}
        if (error instanceof BrokerError && error.code === 'AUTH_COOLDOWN') throw error;
        throw new BrokerError(`Trade Republic SDK quote source unavailable: ${filesystemFailure(error) || failureReason}`, 'UNAVAILABLE', 503);
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

  reconnect(failedClient, options) {
    // Do not tear down a replacement another concurrent request has already
    // established; all callers converge on the same replacement connection.
    if (this.client === failedClient) this.close();
    return this.connect(options);
  }

  async getQuote({ isin, exchange, timeoutMs, forceAuthRetry = false }) {
    if (!isin || !exchange) throw new BrokerError('Instrument and exchange are required for SDK quote', 'INVALID_REQUEST', 400);
    const options = { forceAuthRetry };
    const client = await this.connect(options);
    try { return await this.quoteFrom(client, { isin, exchange, timeoutMs }); }
    catch (error) {
      if (error instanceof BrokerError && error.code !== 'UNAVAILABLE') throw error;
      const replacement = await this.reconnect(client, options);
      return this.quoteFrom(replacement, { isin, exchange, timeoutMs });
    }
  }

  async getInstrumentHistory({ isin, exchange, range = '1y', assetType = 'stock' } = {}) {
    const assetId = String(isin || '').trim().toUpperCase();
    const exchangeId = String(exchange || '').trim().toUpperCase();
    const periods = {
      '1d': { timeframe: '10m', subtract: date => date.setDate(date.getDate() - 1) },
      '1mo': { timeframe: '1h', subtract: date => date.setMonth(date.getMonth() - 1) },
      '1y': { timeframe: '1d', subtract: date => date.setFullYear(date.getFullYear() - 1) },
      '5y': { timeframe: '1w', subtract: date => date.setFullYear(date.getFullYear() - 5) }
    };
    const period = periods[String(range)];
    if (!assetId || !exchangeId || !period) throw new BrokerError('Invalid SDK history request', 'INVALID_HISTORY_REQUEST', 400);
    try {
      const client = await this.connect();
      if (!client.market?.downloadCandles || !client.market?.availableCandleResolutions) throw new BrokerError('SDK historical candles are unavailable', 'HISTORY_UNAVAILABLE', 503);
      const available = await client.market.availableCandleResolutions({ assetId });
      const timeframe = available.includes(period.timeframe)
        ? period.timeframe
        : [...available].reverse().find(value => value === '1d' || value === '1w') || available.at(-1);
      if (!timeframe) throw new BrokerError('SDK returned no candle resolution', 'HISTORY_UNAVAILABLE', 503);
      const isCrypto = String(assetType).toLowerCase() === 'crypto';
      const options = { assetId, exchangeId, timeframe, unit: 'EUR' };
      if (isCrypto) {
        // Crypto/light candles accept named ranges. Their from/to responses fail
        // SDK schema validation, even though the same date query works for stocks.
        options.instrumentType = 'crypto';
        options.range = { '1d': '1d', '1mo': '1m', '1y': '1y', '5y': '5y' }[range];
      } else {
        const to = new Date();
        const from = new Date(to);
        period.subtract(from);
        options.from = from.toISOString();
        options.to = to.toISOString();
      }
      const candles = await client.market.downloadCandles(options, { maxCandlesPerRequest: 500 });
      const points = (Array.isArray(candles) ? candles : []).flatMap(candle => {
        const priceEur = finite(candle?.close);
        const timestamp = new Date(candle?.time);
        if (!priceEur || Number.isNaN(timestamp.getTime())) return [];
        const at = timestamp.toISOString();
        return [{ recordedAt: at.replace('T', ' ').replace(/\.\d{3}Z$/, ''), quoteTime: at, priceEur, priceUsd: null, exchangeRate: null, source: 'trade_republic_history' }];
      });
      return { isin: assetId, range, resolution: timeframe, points };
    } catch (error) {
      if (error instanceof BrokerError) throw error;
      throw new BrokerError(`Trade Republic SDK history unavailable: ${safeError(error)}`, 'HISTORY_UNAVAILABLE', 503);
    }
  }

  close() { try { this.client?.close(); } catch {} finally { this.client = null; } }
}

module.exports = { HandelsrepublikQuoteClient, enabled };
