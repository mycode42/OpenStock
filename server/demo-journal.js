'use strict';

// Sample trading journal for demo mode. Writes only to the database it is given;
// scripts/demo.js passes the isolated data/demo database, never a real one.
const { calculateDashboard, snapshotBucketAt } = require('../lib/analytics');

const DAY = 24 * 60 * 60 * 1000;
const FX_USD_EUR = 0.92;

// Lots are listed in opening order. Each stock has at most one open lot, so nothing merges.
const LOTS = [
    { id: 'DEMO-0001', symbol: 'AAPL', quantity: 10, entry: 172.5, entryFx: 0.93, openedDays: 120, exit: 205.1, exitFx: 0.92, closedDays: 70, dividend: 1.8, strategy: 'swing_momentum' },
    { id: 'DEMO-0002', symbol: 'MSFT', quantity: 5, entry: 380.2, entryFx: 0.93, openedDays: 100, exit: 412.9, exitFx: 0.91, closedDays: 40, strategy: 'swing_trend_following' },
    { id: 'DEMO-0003', symbol: 'TSLA', quantity: 8, entry: 265, entryFx: 0.92, openedDays: 75, exit: 238.4, exitFx: 0.92, closedDays: 20, strategy: 'intraday_mean_reversion', notes: 'Stopped out after the earnings gap' },
    { id: 'DEMO-0004', symbol: 'GOOGL', quantity: 12, entry: 152.3, entryFx: 0.92, openedDays: 50, exit: 171.9, exitFx: 0.92, closedDays: 12, strategy: 'swing_multi_factor' },
    { id: 'DEMO-0005', symbol: 'AAPL', quantity: 6, entry: 198.4, entryFx: 0.92, openedDays: 18, strategy: 'swing_momentum' },
    { id: 'DEMO-0006', symbol: 'AMZN', quantity: 4, entry: 186, entryFx: 0.92, openedDays: 9, strategy: 'swing_trend_following' },
    { id: 'DEMO-0007', symbol: 'MSFT', quantity: 3, entry: 421.5, entryFx: 0.92, openedDays: 4, strategy: 'swing_trend_following' },
];

// Current USD prices for the demo stocks; EUR values use the same FX rate as the lots.
const PRICES_USD = { AAPL: 209.4, MSFT: 418.3, TSLA: 241.2, GOOGL: 171.9, AMZN: 188.1 };

const CAPITAL = [
    { event_type: 'INITIAL', amount_eur: 20000, daysAgo: 150, notes: 'Demo starting capital' },
    { event_type: 'DEPOSIT', amount_eur: 5000, daysAgo: 60, notes: 'Demo deposit' },
];

function stamp(now, daysAgo, hour = 14, minute = 30) {
    const date = new Date(now.getTime() - daysAgo * DAY);
    date.setUTCHours(hour, minute, 0, 0);
    return date.toISOString().slice(0, 19).replace('T', ' ');
}

function tradeRow(now, stock, lot) {
    const closed = lot.closedDays !== undefined;
    return {
        trade_id: lot.id,
        stock_id: stock.id,
        opened_at: stamp(now, lot.openedDays),
        closed_at: closed ? stamp(now, lot.closedDays, 21, 0) : null,
        quantity: lot.quantity,
        currency: 'USD',
        entry_price: lot.entry,
        entry_fx_rate: lot.entryFx,
        entry_price_eur: lot.entry * lot.entryFx,
        entry_fee_eur: 1,
        exit_price: closed ? lot.exit : null,
        exit_fx_rate: closed ? lot.exitFx : null,
        exit_price_eur: closed ? lot.exit * lot.exitFx : null,
        exit_fee_eur: closed ? 1 : 0,
        dividend_eur: closed ? lot.dividend || 0 : 0,
        strategy: lot.strategy,
        notes: lot.notes || '',
    };
}

// Equity curve from the first deposit to today. Earlier points follow a gentle path toward the
// final value; the last point is taken from the live analytics so the chart ends where the dashboard does.
function snapshotRows(now, final) {
    const rows = [];
    const days = 150;
    for (let daysAgo = days; daysAgo >= 1; daysAgo -= 1) {
        const invested = daysAgo > 60 ? CAPITAL[0].amount_eur : CAPITAL[0].amount_eur + CAPITAL[1].amount_eur;
        const progress = (days - daysAgo) / days;
        const equity = invested * (1 + 0.035 * progress + 0.012 * Math.sin(daysAgo / 4));
        rows.push({
            snapshot_at: snapshotBucketAt(new Date(now.getTime() - daysAgo * DAY + 21 * 60 * 60 * 1000)),
            cash_eur: equity * 0.35,
            position_value_eur: equity * 0.65,
            equity_eur: equity,
            net_contributions_eur: invested,
            financing_eur: 0,
            source: 'demo',
        });
    }
    rows.push({
        snapshot_at: snapshotBucketAt(now),
        cash_eur: final.capital.available_cash_eur,
        position_value_eur: final.capital.invested_eur,
        equity_eur: final.capital.current_eur,
        net_contributions_eur: final.capital.net_contributions_eur || 0,
        financing_eur: final.capital.financing_eur || 0,
        source: 'demo',
    });
    return rows;
}

function seedDemoJournal({ db, now = new Date() }) {
    if (db.getAllTrades().length || db.getCapitalEvents().length) return false;
    const stocks = Object.fromEntries(Object.keys(PRICES_USD).map(symbol => [symbol, db.getStockBySymbol(symbol)]));
    if (Object.values(stocks).some(stock => !stock)) throw new Error('Demo stocks are missing from the database');

    db.db.transaction(() => {
        for (const event of CAPITAL) {
            db.addCapitalEvent({ event_type: event.event_type, amount_eur: event.amount_eur, occurred_at: stamp(now, event.daysAgo, 9, 0), notes: event.notes });
        }
        for (const lot of LOTS) db.addTrade(tradeRow(now, stocks[lot.symbol], lot));
        for (const [symbol, priceUsd] of Object.entries(PRICES_USD)) {
            db.updateStockPrice(stocks[symbol].id, priceUsd, priceUsd * FX_USD_EUR, FX_USD_EUR);
        }
        const final = calculateDashboard({
            stocks: db.getAllStocks(),
            trades: db.getAllTrades(),
            capitalEvents: db.getCapitalEvents(),
            snapshots: [],
            now: now.toISOString(),
        });
        for (const snapshot of snapshotRows(now, final)) db.addEquitySnapshot(snapshot);
    })();
    return true;
}

module.exports = { seedDemoJournal, LOTS, PRICES_USD };
