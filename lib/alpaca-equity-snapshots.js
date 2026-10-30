'use strict';

// Equity snapshots for the manual Alpaca account, on the same five-minute cadence as the Algo and options accounts.
// Each bucket holds one row, so a repeat capture in the same bucket updates it instead of adding a point.
const SNAPSHOT_MINUTES = 5;

function snapshotBucket(now) {
    const size = SNAPSHOT_MINUTES * 60_000;
    return new Date(Math.floor(now.getTime() / size) * size).toISOString();
}

async function captureAlpacaEquitySnapshot({ db, client, now = () => new Date() }) {
    const account = await client.getAccount();
    const equity = Number(account.equity);
    if (!Number.isFinite(equity)) throw new Error('Alpaca account equity is unavailable');
    const positions = (await client.getPositions()) || [];
    const positionValue = positions.reduce((sum, position) => sum + Number(position.market_value || 0), 0);
    db.addAlpacaEquitySnapshot({
        snapshot_at: snapshotBucket(now()),
        equity_usd: equity,
        cash_usd: Number(account.cash),
        position_value_usd: positionValue,
    });
}

module.exports = { SNAPSHOT_MINUTES, captureAlpacaEquitySnapshot, snapshotBucket };
