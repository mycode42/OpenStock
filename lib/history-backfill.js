const DEFAULT_DELAY_MS = 1500;
const DEFAULT_TRADE_REPUBLIC_RANGES = Object.freeze(['5y', '1y', '1mo', '1d']);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

class HistoryBackfillQueue {
    constructor({ db, brokerClient = null, delayMs = DEFAULT_DELAY_MS, tradeRepublicRanges = DEFAULT_TRADE_REPUBLIC_RANGES, logger = console } = {}) {
        if (!db) throw new Error('A database is required');
        this.db = db;
        this.brokerClient = brokerClient;
        this.delayMs = Math.max(0, Number(delayMs) || 0);
        this.tradeRepublicRanges = [...new Set(Array.isArray(tradeRepublicRanges) ? tradeRepublicRanges : DEFAULT_TRADE_REPUBLIC_RANGES)];
        this.logger = logger;
        this.pending = new Map();
        this.activeRun = null;
    }

    enqueue(stock, { force = false } = {}) {
        if (!stock || Number(stock.archived) || !Number(stock.enabled)) return false;
        const id = Number(stock.id);
        const mapping = this.db.getBrokerMapping?.(id);
        const mappedTradeRepublic = Boolean(mapping?.verified_at && mapping.currency === 'EUR');
        if (!mappedTradeRepublic) return false;
        const historyReady = this.db.hasCompletedHistoryBackfill?.(id, 'trade_republic', this.tradeRepublicRanges);
        if (!Number.isInteger(id) || this.pending.has(id) || (!force && historyReady)) return false;
        this.pending.set(id, { stock, force });
        this.run().catch(error => this.logger.error(`History backfill queue failed: ${error.message}`));
        return true;
    }

    enqueueMissing() {
        let count = 0;
        for (const stock of this.db.getActiveStocks()) if (this.enqueue(stock)) count++;
        return count;
    }

    enqueueTradeRepublic(stock) { return this.enqueue(stock, { force: true }); }

    async run() {
        if (this.activeRun) return this.activeRun;
        this.activeRun = this.drain();
        try {
            return await this.activeRun;
        } finally {
            this.activeRun = null;
        }
    }

    async drain() {
        while (this.pending.size) {
            const [id, pending] = this.pending.entries().next().value;
            this.pending.delete(id);
            const stock = this.db.getStockById(id);
            const mapping = stock && this.db.getBrokerMapping(stock.id);
            const mappedTradeRepublic = Boolean(mapping?.verified_at && mapping.currency === 'EUR');
            const historyReady = mappedTradeRepublic && this.db.hasCompletedHistoryBackfill?.(id, 'trade_republic', this.tradeRepublicRanges);
            if (!stock || Number(stock.archived) || !Number(stock.enabled) || (!pending.force && historyReady)) continue;
            try {
                if (!mappedTradeRepublic) continue;
                const result = await this.syncTradeRepublic(stock);
                this.logger.log(`${stock.symbol}: ${result.points} Trade Republic history points across ${result.ranges.length} ranges`);
            } catch (error) {
                this.logger.error(`${stock.symbol}: history backfill failed: ${error.code || error.message}`);
            }
            if (this.pending.size && this.delayMs) await wait(this.delayMs);
        }
    }

    async syncTradeRepublic(stock) {
        const mapping = this.db.getBrokerMapping(stock?.id);
        if (!stock || !mapping?.verified_at || mapping.currency !== 'EUR') throw new Error('verified Trade Republic mapping unavailable');
        if (!this.brokerClient) throw new Error('Trade Republic client unavailable');
        let saved = 0;
        const ranges = [];
        for (const range of this.tradeRepublicRanges) {
            this.db.startHistoryBackfill?.(stock.id, 'trade_republic', range);
            try {
                const history = await this.brokerClient.getInstrumentHistory({ isin: mapping.isin, range });
                const savedForRange = this.db.addHistoricalPriceSnapshots(stock.id, history.points, 'trade_republic_history');
                this.db.completeHistoryBackfill?.(stock.id, 'trade_republic', range, savedForRange);
                saved += savedForRange;
                ranges.push({ requested: range, range: history.range, resolution: history.resolution, points: history.points.length });
            } catch (error) {
                this.db.failHistoryBackfill?.(stock.id, 'trade_republic', range, error.code || error.message);
                throw error;
            }
            if (this.delayMs && range !== this.tradeRepublicRanges[this.tradeRepublicRanges.length - 1]) await wait(this.delayMs);
        }
        return { stock_id: stock.id, source: 'trade_republic_history', points: saved, ranges };
    }
}

async function backfillStocks({ db, stocks = db.getActiveStocks(), brokerClient = null, delayMs = DEFAULT_DELAY_MS, logger = console } = {}) {
    const queue = new HistoryBackfillQueue({ db, brokerClient, delayMs, logger });
    for (const stock of stocks) queue.enqueue(stock);
    await queue.run();
    return queue;
}

module.exports = { DEFAULT_DELAY_MS, DEFAULT_TRADE_REPUBLIC_RANGES, HistoryBackfillQueue, backfillStocks };
