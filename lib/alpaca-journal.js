'use strict';

const DAY_MS = 86_400_000;
const TRADING_DAYS = 252;

function num(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
}

function average(values) {
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

// Rebuilds round trips per symbol from fills: a trade opens on the first buy from flat and closes when sold back to flat.
// Sells with no open trade belong to positions from before the history window and are skipped.
function buildTrades(fills) {
    const ordered = [...fills].sort((left, right) => Date.parse(left.transaction_time) - Date.parse(right.transaction_time)
        || String(left.id).localeCompare(String(right.id), undefined, { numeric: true }));
    const open = new Map();
    const trades = [];
    for (const fill of ordered) {
        const symbol = String(fill.symbol || '').toUpperCase();
        const qty = Math.abs(num(fill.qty));
        const price = num(fill.price);
        if (!symbol || qty <= 0 || price <= 0) continue;
        const side = String(fill.side || '').toLowerCase();
        let trade = open.get(symbol);
        if (side === 'buy') {
            if (!trade) {
                trade = { symbol, opened_at: fill.transaction_time, buys: [], sells: [], open_qty: 0, closed_at: null };
                open.set(symbol, trade);
                trades.push(trade);
            }
            trade.buys.push({ qty, price, at: fill.transaction_time });
            trade.open_qty += qty;
        } else if (side === 'sell' && trade) {
            const sold = Math.min(qty, trade.open_qty);
            trade.sells.push({ qty: sold, price, at: fill.transaction_time });
            trade.open_qty -= sold;
            if (trade.open_qty <= 1e-9) {
                trade.closed_at = fill.transaction_time;
                open.delete(symbol);
            }
        }
    }
    return trades;
}

function summarize(trade, number) {
    const cost = trade.buys.reduce((sum, leg) => sum + leg.qty * leg.price, 0);
    const buyQty = trade.buys.reduce((sum, leg) => sum + leg.qty, 0);
    const proceeds = trade.sells.reduce((sum, leg) => sum + leg.qty * leg.price, 0);
    const sellQty = trade.sells.reduce((sum, leg) => sum + leg.qty, 0);
    const isClosed = Boolean(trade.closed_at);
    const netPnl = isClosed ? proceeds - cost : null;
    const legs = trade.sells.length && isClosed ? trade.sells.slice(0, -1) : trade.sells;
    return {
        trade_number: number,
        symbol: trade.symbol,
        opened_at: trade.opened_at,
        closed_at: trade.closed_at,
        is_closed: isClosed,
        quantity: isClosed ? buyQty : trade.open_qty,
        entry_price_usd: buyQty ? cost / buyQty : null,
        exit_price_usd: sellQty ? proceeds / sellQty : null,
        net_pnl_usd: netPnl,
        return_pct: netPnl !== null && cost > 0 ? netPnl / cost * 100 : null,
        currency: 'USD',
        asset_type: 'stock',
        group_details: {
            merged_entries: trade.buys.map(leg => ({
                kind: 'broker_execution', side: 'buy', executed_at: leg.at, quantity: leg.qty,
                execution_price_usd: leg.price, fees_usd: 0, currency: 'USD',
            })),
            partial_sales: legs.map(leg => ({
                closed_at: leg.at, quantity: leg.qty, exit_price_usd: leg.price, exit_fee_usd: 0, currency: 'USD',
            })),
        },
    };
}

function groupRows(closed, keyOf) {
    const groups = new Map();
    for (const trade of closed) {
        const key = keyOf(trade);
        const row = groups.get(key) || { key, pnl_usd: 0, trades: 0, wins: 0 };
        row.pnl_usd += trade.net_pnl_usd;
        row.trades += 1;
        if (trade.net_pnl_usd > 0) row.wins += 1;
        groups.set(key, row);
    }
    return [...groups.values()].sort((left, right) => left.key.localeCompare(right.key));
}

function buildJournalAnalytics({ account, positions = [], fills = [], activities = [], history = null, now = new Date() }) {
    const trades = buildTrades(fills).map((trade, index) => summarize(trade, index + 1));
    const closed = trades.filter(trade => trade.is_closed);
    const wins = closed.filter(trade => trade.net_pnl_usd > 0).length;
    const realizedUsd = closed.reduce((sum, trade) => sum + trade.net_pnl_usd, 0);
    const unrealizedUsd = positions.reduce((sum, position) => sum + num(position.unrealized_pl), 0);

    const capitalFlows = activities.filter(activity => ['CSD', 'CSW'].includes(activity.activity_type));
    const netContributions = capitalFlows.reduce((sum, activity) => sum + num(activity.net_amount), 0);
    const equity = num(account?.equity);

    const daily = groupRows(closed, trade => String(trade.closed_at).slice(0, 10)).map(row => ({ day: row.key, pnl_usd: row.pnl_usd, trades: row.trades, wins: row.wins }));
    const monthly = groupRows(closed, trade => String(trade.closed_at).slice(0, 7)).map(row => ({ month: row.key, pnl_usd: row.pnl_usd, trades: row.trades, wins: row.wins }));
    const averageDaily = average(daily.map(row => row.pnl_usd));
    const averageMonthly = average(monthly.map(row => row.pnl_usd));
    const contributedBase = netContributions > 0 ? netContributions : null;
    const yearPrefix = String(now.getUTCFullYear());

    const bySymbol = new Map();
    for (const trade of closed) {
        const row = bySymbol.get(trade.symbol) || { symbol: trade.symbol, pnl_usd: 0, trades: 0, wins: 0, returns: [], holdDays: [] };
        row.pnl_usd += trade.net_pnl_usd;
        row.trades += 1;
        if (trade.net_pnl_usd > 0) row.wins += 1;
        if (trade.return_pct !== null) row.returns.push(trade.return_pct);
        row.holdDays.push((Date.parse(trade.closed_at) - Date.parse(trade.opened_at)) / DAY_MS);
        bySymbol.set(trade.symbol, row);
    }

    // Portfolio history starts at zero before the account was funded; those points would draw a false ramp.
    const equityCurve = (history?.timestamp || [])
        .map((timestamp, index) => ({ at: new Date(Number(timestamp) * 1000).toISOString(), equity_usd: history.equity?.[index] == null ? NaN : Number(history.equity[index]) }))
        .filter(point => Number.isFinite(point.equity_usd));
    const firstFunded = equityCurve.findIndex(point => point.equity_usd > 0);
    if (firstFunded > 0) equityCurve.splice(0, firstFunded);

    return {
        equity_available: equityCurve.length >= 2,
        equity_curve: equityCurve,
        equity_curve_resolution: 'daily',
        capital: {
            current_usd: equity,
            available_cash_usd: num(account?.cash),
            net_contributions_usd: netContributions,
            unrealized_pnl_usd: unrealizedUsd,
            current_return_pct: contributedBase ? (equity - contributedBase) / contributedBase * 100 : null,
        },
        realized: { net_usd: realizedUsd },
        trades: { total: trades.length, open: trades.length - closed.length, closed: closed.length, win_rate_pct: closed.length ? wins / closed.length * 100 : null },
        performance: {
            average_daily_pnl_usd: averageDaily,
            average_daily_return_pct: averageDaily !== null && contributedBase ? averageDaily / contributedBase * 100 : null,
            average_monthly_pnl_usd: averageMonthly,
            average_monthly_return_pct: averageMonthly !== null && contributedBase ? averageMonthly / contributedBase * 100 : null,
            ytd_pnl_usd: closed.filter(trade => trade.closed_at.startsWith(yearPrefix)).reduce((sum, trade) => sum + trade.net_pnl_usd, 0),
            estimated_yearly_profit_usd: averageDaily !== null ? averageDaily * TRADING_DAYS : null,
        },
        daily_performance: daily,
        monthly_performance: monthly,
        stock_performance: [...bySymbol.values()].map(row => ({
            symbol: row.symbol,
            pnl_usd: row.pnl_usd,
            trades: row.trades,
            win_rate_pct: row.wins / row.trades * 100,
            average_return_pct: average(row.returns),
            average_holding_days: average(row.holdDays),
        })).sort((left, right) => right.pnl_usd - left.pnl_usd),
        capital_events: capitalFlows.map(activity => ({
            event_type: activity.activity_type === 'CSD' ? 'DEPOSIT' : 'WITHDRAWAL',
            amount_usd: Math.abs(num(activity.net_amount)),
            occurred_at: `${activity.date}T00:00:00Z`,
        })).sort((left, right) => right.occurred_at.localeCompare(left.occurred_at)),
        trade_list: trades.sort((left, right) => right.trade_number - left.trade_number),
    };
}

module.exports = { buildJournalAnalytics, buildTrades };
