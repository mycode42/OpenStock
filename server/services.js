const { calculateTrade, calculateDashboard, snapshotBucketAt } = require('../lib/analytics');
const { TradeRepublicClient, BrokerError, configuredKey } = require('../lib/brokers/trade-republic');

function dashboardData(db) {
    return calculateDashboard({
        stocks: db.getAllStocks(),
        trades: db.getAllTrades(),
        capitalEvents: db.getCapitalEvents(),
        snapshots: db.getEquitySnapshots(),
    });
}

function captureEquitySnapshot(db, source = 'dashboard') {
    const analytics = dashboardData(db);
    if (!analytics.equity_available) return null;
    const at = snapshotBucketAt(new Date());
    db.addEquitySnapshot({
        snapshot_at: at,
        cash_eur: analytics.capital.available_cash_eur,
        position_value_eur: analytics.capital.invested_eur,
        equity_eur: analytics.capital.current_eur,
        net_contributions_eur: analytics.capital.net_contributions_eur || 0,
        financing_eur: analytics.capital.financing_eur || 0,
        source,
    });
    return at;
}

function queueStockHistory(historyBackfill, stock) {
    try {
        historyBackfill.enqueue(stock);
    } catch (error) {
        console.error(`Unable to queue history for ${stock?.symbol || 'stock'}:`, error.message);
    }
}

function brokerError(res, error) {
    return res
        .status(error instanceof BrokerError ? error.status : 502)
        .json({ error: error.message || 'Broker unavailable', code: error.code || 'BROKER_ERROR' });
}

module.exports = {
    dashboardData,
    captureEquitySnapshot,
    queueStockHistory,
    brokerError,
};
