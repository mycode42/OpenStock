'use strict';

/** One quote-selection and persistence path for monitor and manual refreshes. */
class QuoteService {
    constructor({ db, brokerClient = null, fetchImpl = globalThis.fetch, config = {}, now = () => new Date(), log = () => {} } = {}) {
        if (!db) throw new Error('A database is required');
        this.db = db; this.brokerClient = brokerClient; this.fetchImpl = fetchImpl; this.config = config; this.now = now; this.log = log;
        this.exchangeRatePromise = null;
    }

    async fetch(stock, { brokerOnline = true } = {}) {
        // Delayed to avoid a module cycle: monitor uses this service too.
        const { fetchYahooFallbackQuote, parseTradeRepublicMonitorQuote, safeError, getExchangeRate } = require('./monitor');
        const mapping = stock.mapping || this.db.getBrokerMapping?.(stock.id);
        if (stock.price_source === 'trade_republic') {
            try {
                if (!brokerOnline) throw new Error('Trade Republic sidecar offline');
                if (!this.brokerClient) throw new Error('Trade Republic client unavailable');
                if (!mapping?.verified_at || !mapping.isin || !mapping.exchange || mapping.currency !== 'EUR') throw new Error('verified Trade Republic mapping unavailable');
                const raw = await this.brokerClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange });
                const parsed = parseTradeRepublicMonitorQuote(raw, this.now());
                return { ...parsed, lastEur: raw.last_eur ?? null, observedAt: raw.observed_at || this.now().toISOString(), mapping };
            } catch (error) { this.log(`${stock.symbol}: Trade Republic price fallback (${safeError(error)})`); }
        }
        this.exchangeRatePromise ||= getExchangeRate({ ...this.config.fx, fetchImpl: this.fetchImpl, now: this.now().getTime(), log: this.log });
        return fetchYahooFallbackQuote({ symbol: stock.symbol, yahooUrl: this.config.yahooUrl, fx: this.config.fx, fetchImpl: this.fetchImpl, timeoutMs: this.config.timeoutMs, now: this.now().getTime(), log: this.log, exchangeRateResult: await this.exchangeRatePromise });
    }

    persist(stock, quote, { recordedAt = null } = {}) {
        if (typeof this.db.persistQuote !== 'function') throw new Error('Quote persistence service is unavailable');
        return this.db.persistQuote({ ...stock, mapping: quote.mapping || stock.mapping }, quote, { recordedAt });
    }
}

module.exports = { QuoteService };
