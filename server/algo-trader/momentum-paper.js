'use strict';

// Read-only view of the momentum paper book: its own paper account (ALPACA_MOMENTUM_*), its rebalance state and
// its decision log. Nothing here places or changes an order.
const fs = require('node:fs/promises');
const path = require('node:path');
const { calculateMetrics, readSnapshotHistory } = require('./equity');

const PAPER_BASE = 'https://paper-api.alpaca.markets/v2';
const TIMEOUT_MS = 10_000;

async function readJsonFile(file, fallback) {
    try { return JSON.parse(await fs.readFile(file, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT' || error instanceof SyntaxError) return fallback; throw error; }
}

async function readDecisions(file) {
    let text = '';
    try { text = await fs.readFile(file, 'utf8'); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    return text.split('\n').filter(Boolean).flatMap(line => {
        try { return [JSON.parse(line)]; } catch { return []; }
    });
}

async function alpacaGet(base, headers, route, fetchImpl) {
    const response = await fetchImpl(`${base}${route}`, { headers, signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!response.ok) throw new Error(`${route} returned ${response.status}`);
    return response.json();
}

// Equity history from Alpaca's portfolio history, in the same shape the equity and options pages chart.
function historyRows(portfolio) {
    const stamps = Array.isArray(portfolio?.timestamp) ? portfolio.timestamp : [];
    const values = Array.isArray(portfolio?.equity) ? portfolio.equity : [];
    const rows = [];
    let previous = null;
    stamps.forEach((stamp, index) => {
        const equity = Number(values[index]);
        if (!Number.isFinite(equity) || equity <= 0) return;
        const at = new Date(Number(stamp) * 1000);
        const change = previous === null ? null : equity - previous;
        rows.push({
            date: at.toISOString().slice(0, 10), timestamp: at.toISOString(), equity,
            daily_pnl: change, daily_pnl_pct: change === null || previous <= 0 ? null : change / previous * 100,
            snapshot_kind: 'daily',
        });
        previous = equity;
    });
    return rows;
}

// Recent orders as activity, in the shape the activity feed uses.
function activityRows(orders) {
    return (Array.isArray(orders) ? orders : []).slice(0, 30).map(order => ({
        at: order.submitted_at || order.created_at || null,
        kind: 'order',
        title: `${String(order.side || '').toUpperCase()} ${order.qty ?? order.notional ?? ''} ${order.symbol || ''}`.trim(),
        detail: `${order.type || 'order'}${order.limit_price ? ` at ${order.limit_price}` : ''} · ${order.status || 'unknown'}`,
        status: order.status || 'unknown',
    })).filter(item => item.at);
}

function position(row) {
    return {
        symbol: String(row.symbol),
        qty: Number(row.qty),
        avg_entry_price: Number(row.avg_entry_price),
        current_price: Number(row.current_price),
        market_value: Number(row.market_value),
        unrealized_pl: Number(row.unrealized_pl),
        unrealized_plpc: Number(row.unrealized_plpc),
    };
}

async function buildMomentumPayload({ root, env = process.env, fetchImpl = fetch, now = new Date() }) {
    const bookDir = path.join(root, 'data', 'research', 'momentum-paper');
    const key = (env.ALPACA_MOMENTUM_API_KEY || '').trim();
    const secret = (env.ALPACA_MOMENTUM_API_SECRET || '').trim();
    const state = await readJsonFile(path.join(bookDir, 'state.json'), {});
    const decisions = await readDecisions(path.join(bookDir, 'decisions.jsonl'));
    const latestQuarter = decisions.length ? decisions[decisions.length - 1].quarter : null;
    const basket = latestQuarter
        ? (decisions.filter(item => item.quarter === latestQuarter)[0]?.basket || []).map(row => ({
            symbol: row.symbol, momentum_6m: row.momentum_6m, weight: row.weight, close: row.close,
        }))
        : [];
    const base = { generated_at: now.toISOString(), account: 'momentum', last_rebalance: state.last_rebalance || null,
        last_quarter: state.last_quarter || null, quarter_orders: decisions.filter(item => item.quarter === latestQuarter).length,
        basket, latest_quarter: latestQuarter };
    if (!key || !secret) return { ...base, available: false, reason: 'Momentum paper account keys are not set' };
    const configured = (env.ALPACA_MOMENTUM_BASE_URL || '').trim().replace(/\/$/, '') || PAPER_BASE;
    if (configured !== PAPER_BASE && env.ALPACA_ALLOW_LIVE !== '1') {
        return { ...base, available: false, reason: 'Momentum account URL is not the paper endpoint' };
    }
    const headers = { 'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret };
    try {
        const account = await alpacaGet(configured, headers, '/account', fetchImpl);
        const positions = await alpacaGet(configured, headers, '/positions', fetchImpl);
        const portfolio = await alpacaGet(configured, headers, '/account/portfolio/history?period=1A&timeframe=1D', fetchImpl)
            .catch(() => null);
        const orders = await alpacaGet(configured, headers, '/orders?status=all&limit=30&direction=desc', fetchImpl)
            .catch(() => []);
        const dailyHistory = historyRows(portfolio);
        const accountId = String(account.id || account.account_number || '').trim();
        const snapshots = accountId
            ? await readSnapshotHistory(path.join(root, 'data'), accountId, { tradingMode: 'momentum' })
            : [];
        const firstSnapshotAt = snapshots[0]?.timestamp || null;
        const history = firstSnapshotAt
            ? [...dailyHistory.filter(point => point.timestamp && point.timestamp < firstSnapshotAt), ...snapshots]
            : snapshots.length ? snapshots : dailyHistory;
        return {
            ...base, available: true,
            equity: Number(account.equity), cash: Number(account.cash),
            last_equity: Number(account.last_equity),
            positions: positions.map(position).sort((a, b) => b.market_value - a.market_value),
            history, metrics: calculateMetrics(history), activity: activityRows(orders),
        };
    } catch (error) {
        console.error(`Momentum paper account unavailable: ${error.message}`);
        return { ...base, available: false, reason: 'Momentum paper account is unavailable right now' };
    }
}

module.exports = { buildMomentumPayload };
