'use strict';

// A simulated Alpaca paper account for demo mode. It has the same methods as lib/brokers/alpaca.js and returns
// the same shapes, so the watchlist, quotes, journal and order code run unchanged. It never opens a network connection.
// Cash and positions are replayed from one fill ledger, so the account, the equity history and the Journal agree.
// Orders the demo database still tracks are rebuilt on start, so positions and open orders survive a restart.
const { randomUUID } = require('node:crypto');
const { AlpacaError } = require('../lib/brokers/alpaca');
const { CLOSED_TRADES, DEPOSIT_USD, WATCHLIST, basePrice, phaseOf, priceAt } = require('./demo-seed');
// Real 5-minute sessions (04:00-20:00 ET) exported from our market history, as basis points from each session's first open.
// Loaded on first use, so the live server never needs the file.
let demoSessions = null;
function demoSessionList() {
    if (!demoSessions) {
        try { demoSessions = require('./demo-sessions.json').sessions; }
        catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; demoSessions = []; }
    }
    return demoSessions;
}

const DAY_MS = 86_400_000;
const HISTORY_DAYS = 120;
const HISTORY_POINTS = 120;
const WATCHLIST_SYMBOLS = new Set(WATCHLIST.map(stock => stock.symbol));
// Local order statuses that a restarted demo broker can rebuild, mapped to the broker status they had.
const RESTORED_STATUS = { NEW: 'accepted', PARTIALLY_FILLED: 'accepted', FILLED: 'filled', CANCELED: 'canceled', PENDING_CANCEL: 'canceled' };

// Stock bars exist only for weekdays from 04:00 to 20:00 New York time, the extended session Alpaca reports.
const sessionClock = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', hourCycle: 'h23', weekday: 'short' });
function inTradingSession(time) {
    const parts = Object.fromEntries(sessionClock.formatToParts(new Date(time)).map(part => [part.type, part.value]));
    return !['Sat', 'Sun'].includes(parts.weekday) && Number(parts.hour) >= 4 && Number(parts.hour) < 20;
}

function round(value, digits = 4) {
    return Number(value.toFixed(digits));
}

// The database writes times as UTC text without a zone.
function parseStoredTime(value) {
    const time = Date.parse(`${String(value).replace(' ', 'T')}Z`);
    return Number.isFinite(time) ? time : null;
}

// Eastern date and minute-of-day for a timestamp, from the same zone the demo uses for sessions.
const easternClock = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
function easternSlot(time) {
    const parts = Object.fromEntries(easternClock.formatToParts(new Date(time)).map(part => [part.type, part.value]));
    const minutes = Number(parts.hour) * 60 + Number(parts.minute);
    return { day: `${parts.year}-${parts.month}-${parts.day}`, index: Math.floor((minutes - 240) / 5) };
}

// One session per symbol and day, chosen by a hash so the same day always draws the same shape.
function sessionFor(symbol, day) {
    const key = `${symbol}|${day}`;
    const sessions = demoSessionList();
    if (!sessions.length) return null;
    return sessions[[...key].reduce((sum, character) => (sum * 31 + character.charCodeAt(0)) >>> 0, 7) % sessions.length];
}

function bpsPrice(anchor, bps) {
    return anchor * (1 + bps / 10000);
}

function watchedPriceAt(symbol, time) {
    return WATCHLIST_SYMBOLS.has(symbol) ? priceAt(symbol, time) : null;
}

// storedOrders are rows of alpaca_orders from the demo database. Rows without a broker id were never acknowledged, so they are skipped.
function createDemoBroker({ now = () => new Date(), ordersEnabled = false, storedOrders = [] } = {}) {
    const fundedAt = now().getTime() - HISTORY_DAYS * DAY_MS;
    const deposits = [{ id: 'demo-deposit', activity_type: 'CSD', net_amount: String(DEPOSIT_USD), date: new Date(fundedAt).toISOString().slice(0, 10) }];
    const ledger = [];
    const orders = new Map();

    function addFill({ id, orderId, symbol, side, qty, price, time }) {
        ledger.push({ id, order_id: orderId, symbol, side, qty, price, time });
        ledger.sort((left, right) => left.time - right.time || left.id.localeCompare(right.id));
    }

    // Cash and holdings as they stood at a moment, replayed from the ledger.
    function stateAt(time) {
        let cash = time >= fundedAt ? DEPOSIT_USD : 0;
        const holdings = new Map();
        for (const fill of ledger) {
            if (fill.time > time) break;
            const holding = holdings.get(fill.symbol) || { qty: 0, cost: 0 };
            const value = fill.qty * fill.price;
            if (fill.side === 'buy') {
                holding.qty += fill.qty;
                holding.cost += value;
                cash -= value;
            } else {
                holding.cost -= (holding.cost / holding.qty) * fill.qty;
                holding.qty -= fill.qty;
                cash += value;
            }
            if (holding.qty > 1e-9) holdings.set(fill.symbol, holding);
            else holdings.delete(fill.symbol);
        }
        return { cash, holdings };
    }

    function equityAt(time) {
        const { cash, holdings } = stateAt(time);
        let total = cash;
        for (const [symbol, holding] of holdings) total += holding.qty * priceAt(symbol, time);
        return total;
    }

    // Sample history: a stock that was held in full at the time, bought and sold at fixed fractions of its base price.
    const seededAt = days => now().getTime() - days * DAY_MS;
    for (const stock of WATCHLIST) {
        if (!stock.held) continue;
        addFill({ id: `demo-buy-${stock.symbol}`, orderId: `demo-order-${stock.symbol}`, symbol: stock.symbol, side: 'buy', qty: stock.held, price: stock.buyIn, time: seededAt(60) });
    }
    CLOSED_TRADES.forEach((trade, index) => {
        const base = basePrice(trade.symbol);
        const entry = round(base * trade.entry_vs_base, 2);
        const exit = round(base * trade.exit_vs_base, 2);
        addFill({ id: `demo-closed-${index}-buy`, orderId: `demo-order-closed-${index}-buy`, symbol: trade.symbol, side: 'buy', qty: trade.qty, price: entry, time: seededAt(trade.bought_days_ago) });
        addFill({ id: `demo-closed-${index}-sell`, orderId: `demo-order-closed-${index}-sell`, symbol: trade.symbol, side: 'sell', qty: trade.qty, price: exit, time: seededAt(trade.sold_days_ago) });
    });

    function publicOrder(order) {
        return {
            id: order.id, client_order_id: order.client_order_id, symbol: order.symbol, side: order.side, qty: String(order.qty),
            filled_qty: String(order.filled_qty), type: order.type, limit_price: String(order.limit_price), status: order.status,
            filled_avg_price: order.filled_avg_price == null ? null : String(order.filled_avg_price),
            submitted_at: order.submitted_at, filled_at: order.filled_at,
        };
    }

    function fillOrder(order, price, time) {
        order.status = 'filled';
        order.filled_qty = order.qty;
        order.filled_avg_price = price;
        order.filled_at = new Date(time).toISOString();
        addFill({ id: `fill-${order.id}`, orderId: order.id, symbol: order.symbol, side: order.side, qty: order.qty, price, time });
    }

    // Like Alpaca, a new order is acknowledged first and fills on a later status check. A limit fills once the market reaches its price.
    function settle(order) {
        if (!['new', 'accepted'].includes(order.status)) return order;
        const quote = quoteFor(order.symbol);
        if (!quote) return order;
        const marketable = order.side === 'buy' ? order.limit_price >= quote.ap : order.limit_price <= quote.bp;
        if (marketable) fillOrder(order, order.limit_price, now().getTime());
        return order;
    }

    function findOrder(id) {
        const order = orders.get(id);
        if (!order) throw new AlpacaError('order not found', 404);
        return settle(order);
    }

    function quoteFor(symbol) {
        const price = watchedPriceAt(symbol, now().getTime());
        if (price === null) return null;
        const spread = price * 0.0004;
        return { bp: round(price - spread / 2), ap: round(price + spread / 2), t: now().toISOString() };
    }

    function requireOrders() {
        if (!ordersEnabled) {
            throw new AlpacaError('Demo orders are simulated only in the demo database. Start with npm run demo to try them.', 409);
        }
    }

    for (const row of storedOrders) {
        const status = RESTORED_STATUS[row.status];
        if (!status || !row.broker_order_id) continue;
        const order = {
            id: row.broker_order_id, client_order_id: row.client_order_id, symbol: row.symbol, side: String(row.side).toLowerCase(),
            qty: Number(row.qty), type: 'limit', limit_price: Number(row.limit_price_usd), status: 'accepted',
            filled_qty: 0, filled_avg_price: null, filled_at: null, submitted_at: row.submitted_at,
        };
        orders.set(order.id, order);
        if (status === 'filled') fillOrder(order, Number(row.filled_avg_price_usd ?? order.limit_price), parseStoredTime(row.updated_at) ?? now().getTime());
        else if (status === 'canceled') order.status = 'canceled';
    }

    function intradayBars(symbol, minutes, start, end, limit) {
        const from = new Date(start).getTime();
        const to = new Date(end).getTime();
        const nowTime = now().getTime();
        const today = easternSlot(nowTime).day;
        const bars = [];
        const span = Math.max(1, minutes / 5);
        for (let time = from; time <= to; time += minutes * 60_000) {
            const slot = easternSlot(time);
            if (slot.index < 0 || slot.index >= 192) continue;
            const session = sessionFor(symbol, slot.day);
            const last = Math.min(191, slot.index + span - 1);
            const bars5 = session.bars_bps;
            let high = -Infinity;
            let low = Infinity;
            for (let index = slot.index; index <= last; index += 1) {
                high = Math.max(high, bars5[index][1]);
                low = Math.min(low, bars5[index][2]);
            }
            // Today's session is anchored so its latest bar closes on the live quote. Earlier days start from the reference price.
            const anchor = slot.day === today
                ? priceAt(symbol, nowTime) / (1 + sessionFor(symbol, today).bars_bps[Math.min(191, easternSlot(nowTime).index)][3] / 10000)
                : basePrice(symbol);
            const close = bpsPrice(anchor, bars5[last][3]);
            const open = bpsPrice(anchor, bars5[slot.index][0]);
            bars.push({
                t: new Date(time).toISOString(), o: round(open), h: round(bpsPrice(anchor, high)), l: round(bpsPrice(anchor, low)),
                c: round(close), v: 1000 + Math.abs(bars5[last][3] - bars5[slot.index][0]) * 40,
            });
        }
        return { feed: 'simulated', bars: bars.slice(-limit) };
    }

    function fillRows() {
        return ledger.map(fill => ({
            id: fill.id, activity_type: 'FILL', order_id: fill.order_id, symbol: fill.symbol, side: fill.side,
            qty: String(fill.qty), price: String(fill.price), transaction_time: new Date(fill.time).toISOString(),
        }));
    }

    return {
        simulated: true,

        async getAccount() {
            const time = now().getTime();
            const { cash } = stateAt(time);
            const total = equityAt(time);
            return {
                id: 'demo-paper', account_number: 'DEMO-PAPER', status: 'ACTIVE', currency: 'USD', trading_blocked: false,
                cash: round(cash, 2), equity: round(total, 2), last_equity: round(equityAt(time - DAY_MS), 2), portfolio_value: round(total, 2),
                buying_power: round(cash, 2), daytrade_count: 0,
            };
        },

        async getPositions() {
            const time = now().getTime();
            return [...stateAt(time).holdings.entries()].map(([symbol, holding]) => {
                const price = priceAt(symbol, time);
                const avgPrice = holding.cost / holding.qty;
                const marketValue = holding.qty * price;
                const costBasis = holding.qty * avgPrice;
                return {
                    symbol, qty: String(holding.qty), side: 'long', asset_class: 'us_equity',
                    avg_entry_price: String(round(avgPrice, 4)), current_price: String(round(price, 4)),
                    market_value: String(round(marketValue, 2)), cost_basis: String(round(costBasis, 2)),
                    unrealized_pl: String(round(marketValue - costBasis, 2)), unrealized_plpc: String(round((marketValue - costBasis) / costBasis, 6)),
                };
            });
        },

        async getFillActivities() {
            return fillRows().reverse();
        },

        async getActivities(types) {
            const wanted = String(types).split(',').map(value => value.trim());
            return [...fillRows(), ...deposits].filter(row => wanted.includes(row.activity_type));
        },

        // Equity from the same ledger as the account, so the last point is the account equity and the curve starts at the deposit.
        async getPortfolioHistory() {
            const end = now().getTime();
            const timestamp = [];
            const values = [];
            for (let index = 0; index < HISTORY_POINTS; index += 1) {
                const time = fundedAt + (end - fundedAt) * index / (HISTORY_POINTS - 1);
                timestamp.push(Math.floor(time / 1000));
                values.push(round(equityAt(time), 2));
            }
            values[values.length - 1] = round(equityAt(end), 2);
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
            const intradayMinutes = { '1Min': 1, '5Min': 5, '30Min': 30 }[timeframe];
            // Real replayed sessions when the fixture exists; otherwise the simulated bars below, so the demo always runs.
            if (intradayMinutes && WATCHLIST_SYMBOLS.has(upper) && demoSessionList().length) return intradayBars(upper, intradayMinutes, start, end, limit);
            const step = { '1Min': 60_000, '5Min': 300_000, '30Min': 1_800_000, '1Day': DAY_MS, '1Week': 7 * DAY_MS }[timeframe] || DAY_MS;
            const from = new Date(start).getTime();
            const to = new Date(end).getTime();
            const bars = [];
            for (let time = from; time <= to; time += step) {
                if (!inTradingSession(time)) continue;
                const price = watchedPriceAt(upper, time);
                if (price === null) return { feed: 'simulated', bars: [] };
                const wave = Math.sin(time / 3_600_000 + phaseOf(upper)) * 0.01;
                const close = round(price * (1 + wave), 4);
                bars.push({ t: new Date(time).toISOString(), o: round(close * 0.999), h: round(close * 1.002), l: round(close * 0.997), c: close, v: 1000 + Math.floor(Math.abs(wave) * 1e6) });
            }
            // Like Alpaca, return the most recent bars when the range holds more than the limit.
            return { feed: 'simulated', bars: bars.slice(-limit) };
        },

        async getAsset(symbol) {
            const upper = String(symbol).toUpperCase();
            return { symbol: upper, tradable: WATCHLIST_SYMBOLS.has(upper), status: 'active', exchange: 'NASDAQ', asset_class: 'us_equity' };
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
            if (side === 'sell' && (stateAt(now().getTime()).holdings.get(symbol)?.qty || 0) < qty) throw new AlpacaError('insufficient qty available for order', 403);
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
