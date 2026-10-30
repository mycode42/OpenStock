'use strict';

// A simulated Alpaca paper account for demo mode. It has the same methods as lib/brokers/alpaca.js and returns
// the same shapes, so the watchlist, quotes, journal and order code run unchanged. It never opens a network connection.
const { randomUUID } = require('node:crypto');
const { AlpacaError } = require('../lib/brokers/alpaca');
const { DEPOSIT_USD, WATCHLIST } = require('./demo-seed');

const DAY_MS = 86_400_000;
const BASE_PRICE = new Map(WATCHLIST.map(stock => [stock.symbol, stock.base]));

function phaseOf(symbol) {
    return [...symbol].reduce((sum, character) => (sum * 31 + character.charCodeAt(0)) % 997, 17) / 997 * Math.PI * 2;
}

// Stock bars exist only for weekdays from 04:00 to 20:00 New York time, the extended session Alpaca reports.
const sessionClock = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', hourCycle: 'h23', weekday: 'short' });
function inTradingSession(time) {
    const parts = Object.fromEntries(sessionClock.formatToParts(new Date(time)).map(part => [part.type, part.value]));
    return !['Sat', 'Sun'].includes(parts.weekday) && Number(parts.hour) >= 4 && Number(parts.hour) < 20;
}

function round(value, digits = 4) {
    return Number(value.toFixed(digits));
}

function createDemoBroker({ now = () => new Date(), ordersEnabled = false } = {}) {
    const holdings = new Map();
    const fills = [];
    const orders = new Map();
    const deposits = [{ id: randomUUID(), activity_type: 'CSD', net_amount: String(DEPOSIT_USD), date: new Date(now().getTime() - 120 * DAY_MS).toISOString().slice(0, 10) }];
    let cash = DEPOSIT_USD;

    for (const stock of WATCHLIST) {
        if (!stock.held) continue;
        holdings.set(stock.symbol, { qty: stock.held, avgPrice: stock.buyIn });
        cash -= stock.held * stock.buyIn;
        fills.push({
            id: randomUUID(), activity_type: 'FILL', order_id: randomUUID(), symbol: stock.symbol, side: 'buy',
            qty: String(stock.held), price: String(stock.buyIn), transaction_time: new Date(now().getTime() - 60 * DAY_MS).toISOString(),
        });
    }

    function priceAt(symbol, time) {
        const base = BASE_PRICE.get(symbol);
        if (!base) return null;
        return base * (1 + Math.sin(time / 45_000 + phaseOf(symbol)) * 0.0007);
    }

    function quoteFor(symbol) {
        const price = priceAt(symbol, now().getTime());
        if (price === null) return null;
        const spread = price * 0.0004;
        return { bp: round(price - spread / 2), ap: round(price + spread / 2), t: now().toISOString() };
    }

    function marketValueOf(symbol) {
        const holding = holdings.get(symbol);
        const price = priceAt(symbol, now().getTime());
        return holding && price !== null ? holding.qty * price : 0;
    }

    function equity() {
        let total = cash;
        for (const symbol of holdings.keys()) total += marketValueOf(symbol);
        return total;
    }

    const openingEquity = equity();

    function publicOrder(order) {
        return {
            id: order.id, client_order_id: order.client_order_id, symbol: order.symbol, side: order.side, qty: String(order.qty),
            filled_qty: String(order.filled_qty), type: order.type, limit_price: String(order.limit_price), status: order.status,
            filled_avg_price: order.filled_avg_price == null ? null : String(order.filled_avg_price),
            submitted_at: order.submitted_at, filled_at: order.filled_at,
        };
    }

    function applyFill(order, price) {
        const holding = holdings.get(order.symbol) || { qty: 0, avgPrice: 0 };
        if (order.side === 'buy') {
            const qty = holding.qty + order.qty;
            holding.avgPrice = (holding.qty * holding.avgPrice + order.qty * price) / qty;
            holding.qty = qty;
            cash -= order.qty * price;
        } else {
            holding.qty -= order.qty;
            cash += order.qty * price;
        }
        if (holding.qty > 0) holdings.set(order.symbol, holding);
        else holdings.delete(order.symbol);
        order.status = 'filled';
        order.filled_qty = order.qty;
        order.filled_avg_price = price;
        order.filled_at = now().toISOString();
        fills.push({
            id: randomUUID(), activity_type: 'FILL', order_id: order.id, symbol: order.symbol, side: order.side,
            qty: String(order.qty), price: String(price), transaction_time: order.filled_at,
        });
    }

    // Like Alpaca, a new order is acknowledged first and fills on a later status check. A limit fills once the market reaches its price.
    function settle(order) {
        if (!['new', 'accepted'].includes(order.status)) return order;
        const quote = quoteFor(order.symbol);
        if (!quote) return order;
        const marketable = order.side === 'buy' ? order.limit_price >= quote.ap : order.limit_price <= quote.bp;
        if (marketable) applyFill(order, order.limit_price);
        return order;
    }

    function findOrder(id) {
        const order = orders.get(id);
        if (!order) throw new AlpacaError('order not found', 404);
        return settle(order);
    }

    function requireOrders() {
        if (!ordersEnabled) {
            throw new AlpacaError('Demo orders are simulated only in the demo database. Start with npm run demo to try them.', 409);
        }
    }

    return {
        simulated: true,

        async getAccount() {
            const total = equity();
            return {
                id: 'demo-paper', account_number: 'DEMO-PAPER', status: 'ACTIVE', currency: 'USD', trading_blocked: false,
                cash: round(cash, 2), equity: round(total, 2), last_equity: round(openingEquity, 2), portfolio_value: round(total, 2),
                buying_power: round(cash, 2), daytrade_count: 0,
            };
        },

        async getPositions() {
            return [...holdings.entries()].map(([symbol, holding]) => {
                const price = priceAt(symbol, now().getTime());
                const marketValue = holding.qty * price;
                const costBasis = holding.qty * holding.avgPrice;
                return {
                    symbol, qty: String(holding.qty), side: 'long', asset_class: 'us_equity',
                    avg_entry_price: String(round(holding.avgPrice, 4)), current_price: String(round(price, 4)),
                    market_value: String(round(marketValue, 2)), cost_basis: String(round(costBasis, 2)),
                    unrealized_pl: String(round(marketValue - costBasis, 2)), unrealized_plpc: String(round((marketValue - costBasis) / costBasis, 6)),
                };
            });
        },

        async getFillActivities() {
            return [...fills].reverse();
        },

        async getActivities(types) {
            const wanted = String(types).split(',').map(value => value.trim());
            return [...fills, ...deposits].filter(row => wanted.includes(row.activity_type));
        },

        async getPortfolioHistory() {
            const end = now().getTime();
            const endEquity = equity();
            const points = 120;
            const timestamp = [];
            const values = [];
            for (let index = 0; index < points; index += 1) {
                const remaining = (points - 1 - index) / (points - 1);
                timestamp.push(Math.floor((end - remaining * (points - 1) * DAY_MS) / 1000));
                values.push(round(endEquity * (1 - 0.03 * remaining) + Math.sin(index * 0.7) * 120 * remaining, 2));
            }
            values[values.length - 1] = round(endEquity, 2);
            return { timestamp, equity: values, base_value: DEPOSIT_USD, timeframe: '1D' };
        },

        async getLatestQuotes(symbols) {
            const quotes = {};
            for (const symbol of symbols || []) {
                const quote = quoteFor(String(symbol).toUpperCase());
                if (quote) quotes[String(symbol).toUpperCase()] = quote;
            }
            return { feed: 'simulated', quotes };
        },

        async getBars(symbol, { timeframe = '1Day', start, end, limit = 1000 } = {}) {
            const upper = String(symbol).toUpperCase();
            const step = { '1Min': 60_000, '5Min': 300_000, '30Min': 1_800_000, '1Day': DAY_MS, '1Week': 7 * DAY_MS }[timeframe] || DAY_MS;
            const from = new Date(start).getTime();
            const to = new Date(end).getTime();
            const bars = [];
            for (let time = from; time <= to; time += step) {
                if (!inTradingSession(time)) continue;
                const price = priceAt(upper, time);
                if (price === null) return { feed: 'simulated', bars: [] };
                const wave = Math.sin(time / 3_600_000 + phaseOf(upper)) * 0.01;
                const close = round(price * (1 + wave), 4);
                bars.push({ t: new Date(time).toISOString(), o: round(close * 0.999), h: round(close * 1.002), l: round(close * 0.997), c: close, v: 1000 + Math.floor(Math.abs(wave) * 1e6) });
            }
            // Like Alpaca, return the most recent bars when the range holds more than the limit.
            return { feed: 'simulated', bars: bars.slice(-limit) };
        },

        async getAsset(symbol) {
            return { symbol: String(symbol).toUpperCase(), tradable: BASE_PRICE.has(String(symbol).toUpperCase()), status: 'active', exchange: 'NASDAQ', asset_class: 'us_equity' };
        },

        async submitOrder(body) {
            requireOrders();
            const symbol = String(body.symbol || '').toUpperCase();
            const quote = quoteFor(symbol);
            if (!quote) throw new AlpacaError('symbol is not in the demo watchlist', 422);
            if (body.type !== 'limit') throw new AlpacaError('demo orders are limit orders only', 422);
            const side = String(body.side).toLowerCase();
            const qty = Number(body.qty);
            const limit = Number(body.limit_price);
            if (!(qty > 0) || !(limit > 0)) throw new AlpacaError('qty and limit_price must be positive', 422);
            if (side === 'sell' && (holdings.get(symbol)?.qty || 0) < qty) throw new AlpacaError('insufficient qty available for order', 403);
            const order = {
                id: randomUUID(), client_order_id: body.client_order_id, symbol, side, qty, type: 'limit', limit_price: limit,
                status: 'accepted', filled_qty: 0, filled_avg_price: null, filled_at: null, submitted_at: now().toISOString(),
            };
            orders.set(order.id, order);
            return publicOrder(order);
        },

        async getOrder(id) {
            return publicOrder(findOrder(id));
        },

        async getOrderByClientId(clientOrderId) {
            const order = [...orders.values()].find(row => row.client_order_id === clientOrderId);
            if (!order) throw new AlpacaError('order not found', 404);
            return publicOrder(settle(order));
        },

        async cancelOrder(id) {
            const order = findOrder(id);
            if (['new', 'accepted'].includes(order.status)) order.status = 'canceled';
            return undefined;
        },
    };
}

module.exports = { createDemoBroker };
