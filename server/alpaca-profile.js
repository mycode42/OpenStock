'use strict';

const { makeQuote } = require('../lib/brokers/alpaca');
const { openOrdersByLevel } = require('../lib/alpaca-orders');
const { buildJournalAnalytics } = require('../lib/alpaca-journal');

function rawDatabase(db) {
    return db?.db || db;
}

function configuredWatchlist(db) {
    db = rawDatabase(db);
    const rows = db.prepare(`SELECT id, symbol, name, source_stock_id, enabled, archived, copied_at, copied_fx_rate
        FROM alpaca_watchlist WHERE enabled=1 AND archived=0 ORDER BY symbol`).all();
    const levels = db.prepare(`SELECT * FROM alpaca_alert_levels WHERE watchlist_id=?
        ORDER BY side, price_usd, id`);
    const openOrders = openOrdersByLevel(db);
    return rows.map(row => ({
        ...row,
        alert_levels: levels.all(row.id).map(level => ({ ...level, open_order: openOrders.get(level.id) || null })),
    }));
}

async function alpacaDashboardData(app) {
    const db = app.locals.db;
    const watchlist = configuredWatchlist(db);
    const [quotePayload, positions] = await Promise.all([
        app.locals.alpacaQuoteFeed.get(),
        watchlist.length ? app.locals.alpacaClient.getPositions() : Promise.resolve([])
    ]);
    const byId = new Map((quotePayload.prices || []).map(quote => [Number(quote.stock_id), quote]));
    const positionsBySymbol = new Map((positions || []).map(position => [String(position.symbol).toUpperCase(), position]));
    const stocks = watchlist.map(row => {
        const quote = byId.get(Number(row.id)) || null;
        const position = positionsBySymbol.get(row.symbol);
        return {
            id: row.id,
            symbol: row.symbol,
            name: row.name || row.symbol,
            currency: 'USD',
            enabled: row.enabled,
            archived: row.archived,
            current_price_usd: quote?.price_usd ?? null,
            last_price_update: quotePayload.observed_at || null,
            broker_quote: quote ? { bid_usd: quote.bid_usd, ask_usd: quote.ask_usd, quote_time: quote.quote_time, source: quote.source } : null,
            market_data: {
                source: quote?.source || null,
                label: quote ? (quote.feed === 'simulated' ? 'Simulated market data' : `Alpaca ${String(quote.feed || '').toUpperCase()} quotes`) : 'No Alpaca quote',
                quote_time: quote?.quote_time || null
            },
            position: position ? {
                owned: Math.abs(Number(position.qty || 0)) > 0,
                quantity: Number(position.qty || 0),
                buy_in_price_usd: Number(position.avg_entry_price || 0),
                source: 'alpaca'
            } : { owned: false, quantity: 0, buy_in_price_usd: null, source: 'alpaca' },
            alert_levels: row.alert_levels.map(level => ({
                ...level,
                price_usd: Number(level.price_usd),
                reference_price_usd: level.reference_price_usd === null ? null : Number(level.reference_price_usd)
            }))
        };
    });
    return {
        stocks,
        analytics: { broker: 'alpaca', currency: 'USD', quote_feed: quotePayload.feed }
    };
}

async function alpacaJournalData(app) {
    const client = app.locals.alpacaClient;
    const [account, positions, fills, activities, history] = await Promise.all([
        client.getAccount(),
        client.getPositions(),
        client.getActivities('FILL'),
        client.getActivities('CSD,CSW'),
        client.getPortfolioHistory({ period: '1A', timeframe: '1D' }).catch(() => null),
    ]);
    const accountView = {
        status: account.status,
        currency: account.currency || 'USD',
        cash: Number(account.cash),
        equity: Number(account.equity),
        last_equity: Number(account.last_equity),
        portfolio_value: Number(account.portfolio_value ?? account.equity),
        buying_power: Number(account.buying_power),
        daytrade_count: Number(account.daytrade_count || 0)
    };
    const positionView = (positions || []).map(position => ({
        symbol: String(position.symbol || '').toUpperCase(),
        qty: Number(position.qty),
        side: position.side || (Number(position.qty) < 0 ? 'short' : 'long'),
        avg_entry_price: Number(position.avg_entry_price),
        current_price: Number(position.current_price),
        market_value: Number(position.market_value),
        cost_basis: Number(position.cost_basis),
        unrealized_pl: Number(position.unrealized_pl),
        unrealized_plpc: Number(position.unrealized_plpc)
    }));
    const fillView = (fills || []).map(fill => ({
        id: fill.id,
        order_id: fill.order_id,
        symbol: String(fill.symbol || '').toUpperCase(),
        side: fill.side,
        qty: Number(fill.qty),
        price: Number(fill.price),
        transaction_time: fill.transaction_time || fill.date || null
    }));
    return {
        broker: 'alpaca',
        currency: 'USD',
        account: accountView,
        positions: positionView,
        // Recent fills for the table; the analytics below use the full history.
        fills: fillView.slice(-100).reverse(),
        analytics: buildJournalAnalytics({ account: accountView, positions: positionView, fills: fillView, activities: activities || [], history, now: new Date() })
    };
}

module.exports = { rawDatabase, configuredWatchlist, makeQuote, alpacaDashboardData, alpacaJournalData };
