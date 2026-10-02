'use strict';

const { safeError, parseTradeRepublicMonitorQuote } = require('./market-data');

async function mapBounded(items, limit, task) {
    let cursor = 0;
    const workers = Array.from({ length: Math.min(Math.max(1, Math.floor(Number(limit) || 1)), items.length) }, async () => {
        while (cursor < items.length) {
            const index = cursor++;
            items[index] = await task(items[index]);
        }
    });
    await Promise.all(workers);
    return items;
}

function remainingTimeout(deadlineAt) {
    return deadlineAt === null ? undefined : Math.max(0, deadlineAt - Date.now());
}

/** Shared SDK → loopback sidecar quote path for the dashboard and monitor. */
class QuoteService {
    constructor({ db, brokerClient = null, sdkClient = null, config = {}, now = () => new Date(), log = () => {} } = {}) {
        if (!db) throw new Error('A database is required');
        this.db = db;
        this.brokerClient = brokerClient;
        this.sdkClient = sdkClient;
        this.config = config;
        this.now = now;
        this.log = log;
    }

    async fetch(stock, { timeoutMs } = {}) {
        const requestTimeoutMs = timeoutMs === undefined
            ? Number(this.config.timeoutMs) || undefined
            : Math.min(timeoutMs, Number(this.config.timeoutMs) || timeoutMs);
        const mapping = stock.mapping || this.db?.getBrokerMapping?.(stock.id);
        let lastError;

        if (this.sdkClient?.isEnabled?.()) {
            try {
                if (!mapping?.verified_at || !mapping.isin || !mapping.exchange || mapping.currency !== 'EUR') throw new Error('verified EUR Trade Republic mapping unavailable');
                const raw = await this.sdkClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange, timeoutMs: requestTimeoutMs });
                const parsed = parseTradeRepublicMonitorQuote(raw, this.now());
                return { ...parsed, source: 'trade_republic_sdk_order_price', lastEur: raw.last_eur ?? null, observedAt: raw.observed_at || this.now().toISOString(), mapping };
            } catch (error) {
                lastError = error;
                this.log(`${stock.symbol}: Handelsrepublik SDK quote unavailable (${safeError(error)})`);
            }
        }

        try {
            if (!this.brokerClient) throw new Error('Trade Republic UAPI sidecar unavailable');
            if (!mapping?.verified_at || !mapping.isin || !mapping.exchange || mapping.currency !== 'EUR') throw new Error('verified EUR Trade Republic mapping unavailable');
            const raw = await this.brokerClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange, timeoutMs: requestTimeoutMs });
            const parsed = parseTradeRepublicMonitorQuote(raw, this.now());
            return { ...parsed, lastEur: raw.last_eur ?? null, observedAt: raw.observed_at || this.now().toISOString(), mapping };
        } catch (error) {
            this.log(`${stock.symbol}: Trade Republic UAPI quote unavailable (${safeError(error)})`);
            throw lastError ? new AggregateError([lastError, error], 'SDK and sidecar quote requests failed') : error;
        }
    }

    async fetchMany(stocks, { concurrency = this.config.concurrency || 3, deadlineAt = null } = {}) {
        const rows = Array.isArray(stocks) ? stocks : [];
        return mapBounded(rows, concurrency, async stock => {
            const timeoutMs = remainingTimeout(deadlineAt);
            if (timeoutMs !== undefined && timeoutMs <= 0) return { stock, error: new Error('quote cycle deadline reached') };
            try {
                return { stock, quote: await this.fetch(stock, { timeoutMs }) };
            } catch (error) {
                return { stock, error };
            }
        });
    }

    persist(stock, quote, { recordedAt = null } = {}) {
        if (typeof this.db.persistQuote !== 'function') throw new Error('Quote persistence service is unavailable');
        return this.db.persistQuote({ ...stock, mapping: quote.mapping || stock.mapping }, quote, { recordedAt });
    }
}

module.exports = { QuoteService };
