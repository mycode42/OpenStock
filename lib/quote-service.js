'use strict';

const { safeError, getExchangeRate, fetchYahooFallbackQuote, parseTradeRepublicMonitorQuote } = require('./market-data');

/** One quote-selection and persistence path for monitor and manual refreshes. */
class QuoteService {
    constructor({ db, brokerClient = null, pytrClient = null, pytrLoginRecovery = null, fetchImpl = globalThis.fetch, config = {}, now = () => new Date(), log = () => {} } = {}) {
        if (!db) throw new Error('A database is required');
        this.db = db; this.brokerClient = brokerClient; this.pytrClient = pytrClient; this.pytrLoginRecovery = pytrLoginRecovery; this.fetchImpl = fetchImpl; this.config = config; this.now = now; this.log = log;
        this.exchangeRatePromise = null;
    }

    pytrQuote(mapping, pytrQuotes) {
        const quote = mapping?.isin ? pytrQuotes?.get(mapping.isin) : null;
        return quote ? { priceEur: quote.lastEur, bidEur: null, askEur: quote.askEur, lastEur: quote.lastEur, quoteTime: quote.quoteTime, observedAt: quote.observedAt, source: quote.source, mapping, name: quote.name } : null;
    }

    async yahooFallback(stock) {
        this.exchangeRatePromise ||= getExchangeRate({ ...this.config.fx, fetchImpl: this.fetchImpl, now: this.now().getTime(), log: this.log });
        return fetchYahooFallbackQuote({ symbol: stock.symbol, yahooUrl: this.config.yahooUrl, fx: this.config.fx, fetchImpl: this.fetchImpl, timeoutMs: this.config.timeoutMs, now: this.now().getTime(), log: this.log, exchangeRateResult: await this.exchangeRatePromise });
    }

    async fetch(stock, { brokerOnline = true, pytrQuotes = null, deferYahooFallback = false } = {}) {
        const mapping = stock.mapping || this.db.getBrokerMapping?.(stock.id);
        if (stock.price_source === 'trade_republic') {
            try {
                if (!brokerOnline) throw new Error('Trade Republic sidecar offline');
                if (!this.brokerClient) throw new Error('Trade Republic client unavailable');
                if (!mapping?.verified_at || !mapping.isin || !mapping.exchange || mapping.currency !== 'EUR') throw new Error('verified Trade Republic mapping unavailable');
                const raw = await this.brokerClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange });
                const parsed = parseTradeRepublicMonitorQuote(raw, this.now());
                return { ...parsed, lastEur: raw.last_eur ?? null, observedAt: raw.observed_at || this.now().toISOString(), mapping };
            } catch (error) {
                this.log(`${stock.symbol}: Trade Republic sidecar quote unavailable (${safeError(error)})`);
                const pytrQuote = this.pytrQuote(mapping, pytrQuotes);
                if (pytrQuote) return pytrQuote;
                if (deferYahooFallback) throw error;
            }
        }
        return this.yahooFallback(stock);
    }

    async fetchMany(stocks, { brokerOnline = true, pytrFallback = false } = {}) {
        const rows = Array.isArray(stocks) ? stocks : [];
        if (!pytrFallback) return Promise.all(rows.map(async stock => {
            try { return { stock, quote: await this.fetch(stock, { brokerOnline }) }; }
            catch (error) { return { stock, error }; }
        }));

        const initial = await Promise.all(rows.map(async stock => {
            try { return { stock, quote: await this.fetch(stock, { brokerOnline, deferYahooFallback: stock.price_source === 'trade_republic' }) }; }
            // Only Trade Republic selections are eligible for the pytr/Yahoo
            // fallback phase. Retrying a Yahoo-only failure made every failed
            // Yahoo request run twice and delayed the monitor cycle.
            catch (error) { return stock.price_source === 'trade_republic' ? { stock, sidecarError: error } : { stock, error }; }
        }));
        const pending = initial.filter(row => row.sidecarError);
        if (!pending.length) return initial;
        const mappedIsins = pending.map(({ stock }) => (stock.mapping || this.db.getBrokerMapping?.(stock.id))?.isin).filter(Boolean);
        let pytrQuotes = new Map();
        if (this.pytrClient && mappedIsins.length) {
            try { pytrQuotes = await this.pytrClient.getQuotes(mappedIsins); }
            catch (error) {
                this.log(`pytr price fallback (${safeError(error)})`);
                try { await this.pytrLoginRecovery?.request(); }
                catch (recoveryError) { this.log(`pytr login recovery request failed (${safeError(recoveryError)})`); }
            }
        }
        return Promise.all(initial.map(async row => {
            if (!row.sidecarError) return row;
            const mapping = row.stock.mapping || this.db.getBrokerMapping?.(row.stock.id);
            const quote = this.pytrQuote(mapping, pytrQuotes);
            if (quote) return { stock: row.stock, quote };
            try { return { stock: row.stock, quote: await this.yahooFallback(row.stock) }; }
            catch (error) { return { stock: row.stock, error }; }
        }));
    }

    persist(stock, quote, { recordedAt = null } = {}) {
        if (typeof this.db.persistQuote !== 'function') throw new Error('Quote persistence service is unavailable');
        return this.db.persistQuote({ ...stock, mapping: quote.mapping || stock.mapping }, quote, { recordedAt });
    }
}

module.exports = { QuoteService };
