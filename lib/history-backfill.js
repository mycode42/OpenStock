const { fetchYahooChart, parseDailyCloses } = require('./yahoo-chart');

const DEFAULT_DELAY_MS = 1500;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

class HistoryBackfillQueue {
    constructor({ db, fetchImpl = globalThis.fetch, delayMs = DEFAULT_DELAY_MS, range = '5y', logger = console } = {}) {
        if (!db) throw new Error('A database is required');
        this.db = db;
        this.fetchImpl = fetchImpl;
        this.delayMs = Math.max(0, Number(delayMs) || 0);
        this.range = range;
        this.logger = logger;
        this.pending = new Map();
        this.activeRun = null;
    }

    enqueue(stock) {
        if (!stock || Number(stock.archived) || !Number(stock.enabled)) return false;
        const id = Number(stock.id);
        if (!Number.isInteger(id) || this.pending.has(id) || this.db.hasDailyBackfill(id)) return false;
        this.pending.set(id, stock);
        this.run().catch(error => this.logger.error(`History backfill queue failed: ${error.message}`));
        return true;
    }

    enqueueMissing() {
        let count = 0;
        for (const stock of this.db.getActiveStocks()) if (this.enqueue(stock)) count++;
        return count;
    }

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
            const [id] = this.pending.entries().next().value;
            this.pending.delete(id);
            const stock = this.db.getStockById(id);
            if (!stock || Number(stock.archived) || !Number(stock.enabled) || this.db.hasDailyBackfill(id)) continue;
            try {
                const chart = await fetchYahooChart(stock.symbol, { fetchImpl: this.fetchImpl, range: this.range });
                const saved = this.db.addHistoricalPriceSnapshots(stock.id, parseDailyCloses(chart));
                this.logger.log(`${stock.symbol}: ${saved} daily history points`);
            } catch (error) {
                this.logger.error(`${stock.symbol}: history backfill failed: ${error.message}`);
            }
            if (this.pending.size && this.delayMs) await wait(this.delayMs);
        }
    }
}

async function backfillStocks({ db, stocks = db.getActiveStocks(), fetchImpl = globalThis.fetch, delayMs = DEFAULT_DELAY_MS, range = '5y', logger = console } = {}) {
    const queue = new HistoryBackfillQueue({ db, fetchImpl, delayMs, range, logger });
    for (const stock of stocks) queue.enqueue(stock);
    await queue.run();
    return queue;
}

module.exports = { DEFAULT_DELAY_MS, HistoryBackfillQueue, backfillStocks };
