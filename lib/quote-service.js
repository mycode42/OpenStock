'use strict';

/** One quote-selection and persistence path for monitor and manual refreshes. */
class QuoteService {
    constructor({ db, brokerClient = null, pytrClient = null, fetchImpl = globalThis.fetch, config = {}, now = () => new Date(), log = () => {} } = {}) {
        if (!db) throw new Error('A database is required');
        this.db = db; this.brokerClient = brokerClient; this.pytrClient = pytrClient; this.fetchImpl = fetchImpl; this.config = config; this.now = now; this.log = log;
        this.exchangeRatePromise = null;
    }

    async fetch(stock, { brokerOnline = true, preferPytr = false, pytrQuotes = null } = {}) {
        // Delayed to avoid a module cycle: monitor uses this service too.
        const { fetchYahooFallbackQuote, parseTradeRepublicMonitorQuote, safeError, getExchangeRate } = require('./monitor');
        const mapping = stock.mapping || this.db.getBrokerMapping?.(stock.id);
        if (stock.price_source === 'trade_republic') {
            const pytrQuote = preferPytr && mapping?.isin ? pytrQuotes?.get(mapping.isin) : null;
            if (pytrQuote) return { priceEur: pytrQuote.lastEur, bidEur: null, askEur: pytrQuote.askEur, lastEur: pytrQuote.lastEur, quoteTime: pytrQuote.quoteTime, observedAt: pytrQuote.observedAt, source: pytrQuote.source, mapping, name: pytrQuote.name };
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

    async fetchMany(stocks, { brokerOnline = true, preferPytr = false } = {}) {
        const rows = Array.isArray(stocks) ? stocks : [];
        const mappedIsins = rows.map(stock => (stock.mapping || this.db.getBrokerMapping?.(stock.id))?.isin).filter(Boolean);
        let pytrQuotes = new Map();
        if (preferPytr && this.pytrClient && mappedIsins.length) {
            try { pytrQuotes = await this.pytrClient.getQuotes(mappedIsins); }
            catch (error) { this.log(`pytr price fallback (${safeError(error)})`); }
        }
        return Promise.all(rows.map(async stock => {
            try { return { stock, quote: await this.fetch(stock, { brokerOnline, preferPytr, pytrQuotes }) }; }
            catch (error) { return { stock, error }; }
        }));
    }

    persist(stock, quote, { recordedAt = null } = {}) {
        if (typeof this.db.persistQuote !== 'function') throw new Error('Quote persistence service is unavailable');
        return this.db.persistQuote({ ...stock, mapping: quote.mapping || stock.mapping }, quote, { recordedAt });
    }
}

module.exports = { QuoteService };
