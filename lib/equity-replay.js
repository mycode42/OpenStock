'use strict';

const POSITION_EPSILON = 1e-12;
const MAX_PRICE_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function timestampMs(value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number' || /^\d{10,13}(?:\.\d+)?$/.test(String(value))) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? (numeric < 100_000_000_000 ? numeric * 1000 : numeric) : null;
    }
    const text = String(value);
    const parsed = Date.parse(text.includes('T') || /[+-]\d\d:\d\d$/.test(text) ? text : `${text.replace(' ', 'T')}Z`);
    return Number.isFinite(parsed) ? parsed : null;
}

function tradeIdLabel(value) {
    const tradeId = String(value || '');
    return tradeId.startsWith('TR-EXEC-') ? tradeId.slice('TR-EXEC-'.length) : '';
}

function isManualPartial(trade) {
    return !String(trade.trade_id || '').startsWith('TR-')
        && (String(trade.trade_id || '').includes('-SELL-') || String(trade.notes || '').includes('Partial sale of '));
}

function findMatchingBrokerSale(trade, executions) {
    if (!trade.closed_at || !Number.isFinite(Number(trade.exit_price_eur))) return null;
    const closedAt = timestampMs(trade.closed_at);
    const priceTolerance = Math.max(0.02, Math.abs(Number(trade.exit_price_eur)) * 0.0001);
    return executions.find(execution => execution.side === 'sell'
        && Number(execution.stock_id) === Number(trade.stock_id)
        && Math.abs(Number(execution.quantity) - Number(trade.quantity)) <= 0.000001
        && Math.abs(Number(execution.execution_price_eur) - Number(trade.exit_price_eur)) <= priceTolerance
        && closedAt !== null && Math.abs(timestampMs(execution.executed_at) - closedAt) <= 6 * 60 * 60 * 1000) || null;
}

function mergeEventsByTarget(mergeEvents) {
    const grouped = new Map();
    for (const row of mergeEvents || []) {
        if (!grouped.has(Number(row.target_trade_id))) grouped.set(Number(row.target_trade_id), []);
        try { grouped.get(Number(row.target_trade_id)).push(JSON.parse(row.source_trade_json)); }
        catch { /* Ignore malformed archival detail; active trade rows remain authoritative. */ }
    }
    return grouped;
}

function addFlow(events, { at, stockId = null, quantity = 0, delta = 0, kind }) {
    const time = timestampMs(at);
    if (time === null || !Number.isFinite(delta)) return;
    events.push({ at: time, stock_id: stockId === null ? null : Number(stockId), quantity: Number(quantity), delta: Number(delta), kind });
}

function buildCashFlowEvents({ trades = [], brokerExecutions = [], mergeEvents = [], capitalEvents = [] } = {}) {
    const events = [];
    const tradeById = new Map(trades.map(trade => [Number(trade.id), trade]));
    const sourcesByTarget = mergeEventsByTarget(mergeEvents);
    const ignoredBrokerBuys = new Set();
    for (const [targetId, sources] of sourcesByTarget) {
        const target = tradeById.get(targetId);
        if (!target || String(target.trade_id || '').startsWith('TR-')) continue;
        for (const source of sources) {
            const label = tradeIdLabel(source.trade_id);
            if (label) ignoredBrokerBuys.add(label);
        }
    }

    for (const execution of brokerExecutions) {
        if (execution.side === 'buy' && ignoredBrokerBuys.has(String(execution.execution_identity).slice(0, 20))) continue;
        const quantity = Number(execution.quantity), price = Number(execution.execution_price_eur), fee = Number(execution.fees_eur || 0);
        if (!(quantity > 0) || !(price > 0)) continue;
        if (!['buy', 'sell'].includes(execution.side)) continue;
        const sign = execution.side === 'buy' ? -1 : 1;
        const positionChange = execution.side === 'buy' ? quantity : -quantity;
        const cashDelta = execution.side === 'buy' ? -(quantity * price + fee) : quantity * price - fee;
        addFlow(events, { at: execution.executed_at, stockId: execution.stock_id, quantity: positionChange, delta: cashDelta, kind: `broker_${execution.side}` });
    }

    const addTradeDividend = trade => {
        const dividend = Number(trade.dividend_eur || 0);
        if (dividend) addFlow(events, { at: trade.closed_at || trade.opened_at, stockId: trade.stock_id, delta: dividend, kind: 'trade_dividend' });
    };

    const childrenByParent = new Map();
    for (const trade of trades) {
        const id = Number(trade.id);
        const tradeId = String(trade.trade_id || '');
        const parentId = tradeId.match(/-SELL-\d+-[a-f0-9]+-(\d+)$/i)?.[1];
        const parentNote = String(trade.notes || '').match(/from open trade (.+?)\.$/)?.[1];
        const parent = parentId ? tradeById.get(Number(parentId)) : trades.find(candidate => candidate.trade_id === parentNote);
        if (parent && Number(parent.id) !== id) {
            if (!childrenByParent.has(Number(parent.id))) childrenByParent.set(Number(parent.id), []);
            childrenByParent.get(Number(parent.id)).push(trade);
        }
    }

    for (const trade of trades) {
        if (String(trade.trade_id || '').startsWith('TR-')) {
            addTradeDividend(trade);
            continue;
        }
        const children = childrenByParent.get(Number(trade.id)) || [];
        const child = isManualPartial(trade);
        if (child) {
            if (findMatchingBrokerSale(trade, brokerExecutions)) {
                addTradeDividend(trade);
                continue;
            }
            if (trade.closed_at) {
                const quantity = Number(trade.quantity), price = Number(trade.exit_price_eur), fee = Number(trade.exit_fee_eur || 0);
                addFlow(events, { at: trade.closed_at, stockId: trade.stock_id, quantity: -quantity, delta: quantity * price - fee + Number(trade.dividend_eur || 0), kind: 'manual_partial_sell' });
                if (!children.some(candidate => candidate.trade_id === String(trade.trade_id).split('-SELL-')[0])) {
                    // A legacy partial-sale row can outlive its parent; its opening
                    // cost is present in the row itself and must remain in the ledger.
                    addFlow(events, { at: trade.opened_at, stockId: trade.stock_id, quantity, delta: -(quantity * Number(trade.entry_price_eur) + Number(trade.entry_fee_eur || 0)), kind: 'manual_orphan_buy' });
                }
            }
            continue;
        }

        const partialChildren = children.filter(isManualPartial);
        const totalQuantity = Number(trade.quantity) + partialChildren.reduce((sum, row) => sum + Number(row.quantity), 0);
        const totalCost = totalQuantity * Number(trade.entry_price_eur);
        const totalEntryFee = Number(trade.entry_fee_eur || 0) + partialChildren.reduce((sum, row) => sum + Number(row.entry_fee_eur || 0), 0);
        const normalSources = (sourcesByTarget.get(Number(trade.id)) || []).filter(source => {
            if (String(source.audit_type || '').startsWith('duplicate_manual_')) return false;
            if (tradeIdLabel(source.trade_id)) return false;
            return Number(source.quantity) > 0 && Number(source.entry_price_eur) > 0;
        });
        const sourceQuantity = normalSources.reduce((sum, source) => sum + Number(source.quantity), 0);
        const sourceCost = normalSources.reduce((sum, source) => sum + Number(source.quantity) * Number(source.entry_price_eur), 0);
        const sourceFees = normalSources.reduce((sum, source) => sum + Number(source.entry_fee_eur || 0), 0);
        const baseQuantity = totalQuantity - sourceQuantity;
        const baseCost = totalCost - sourceCost;
        const baseFee = totalEntryFee - sourceFees;
        if (baseQuantity > POSITION_EPSILON && baseCost > 0) {
            addFlow(events, { at: trade.opened_at, stockId: trade.stock_id, quantity: baseQuantity, delta: -(baseCost + baseFee), kind: 'manual_buy' });
        }
        for (const source of normalSources) {
            addFlow(events, { at: source.opened_at, stockId: trade.stock_id, quantity: Number(source.quantity), delta: -(Number(source.quantity) * Number(source.entry_price_eur) + Number(source.entry_fee_eur || 0)), kind: 'manual_rebuy' });
        }

        for (const childSale of partialChildren) {
            if (findMatchingBrokerSale(childSale, brokerExecutions)) continue;
            if (childSale.closed_at) addFlow(events, { at: childSale.closed_at, stockId: trade.stock_id, quantity: -Number(childSale.quantity), delta: Number(childSale.quantity) * Number(childSale.exit_price_eur) - Number(childSale.exit_fee_eur || 0) + Number(childSale.dividend_eur || 0), kind: 'manual_partial_sell' });
        }
        if (trade.closed_at) {
            if (findMatchingBrokerSale(trade, brokerExecutions)) addTradeDividend(trade);
            else {
                const quantity = Number(trade.quantity), price = Number(trade.exit_price_eur), fee = Number(trade.exit_fee_eur || 0);
                addFlow(events, { at: trade.closed_at, stockId: trade.stock_id, quantity: -quantity, delta: quantity * price - fee + Number(trade.dividend_eur || 0), kind: 'manual_sell' });
            }
        }
    }

    for (const event of capitalEvents) {
        const amount = Number(event.amount_eur);
        const delta = event.event_type === 'WITHDRAWAL' ? -amount : amount;
        addFlow(events, { at: event.occurred_at, delta, kind: `capital_${event.event_type}` });
    }
    return events.sort((left, right) => left.at - right.at || (left.quantity > 0 ? -1 : 0) - (right.quantity > 0 ? -1 : 0));
}

function priceTimestamp(row) {
    return timestampMs(row.quote_time) ?? timestampMs(row.recorded_at);
}

function buildReplaySnapshots({ events, priceHistory, stocks, startAt, now = new Date(), maxPriceAgeMs = MAX_PRICE_AGE_MS } = {}) {
    const historyByStock = new Map();
    for (const row of priceHistory || []) {
        const at = priceTimestamp(row), price = Number(row.price_eur);
        if (at === null || !(price > 0)) continue;
        if (!historyByStock.has(Number(row.stock_id))) historyByStock.set(Number(row.stock_id), []);
        historyByStock.get(Number(row.stock_id)).push({ at, price });
    }
    for (const rows of historyByStock.values()) rows.sort((left, right) => left.at - right.at);
    const stockById = new Map((stocks || []).map(stock => [Number(stock.id), stock]));
    const firstMs = timestampMs(startAt), nowMs = now instanceof Date ? now.getTime() : timestampMs(now);
    if (firstMs === null || nowMs === null || firstMs > nowMs) throw new Error('Invalid equity replay range');
    const startDay = new Date(firstMs); startDay.setUTCHours(0, 0, 0, 0);
    const lastDay = new Date(nowMs); lastDay.setUTCHours(0, 0, 0, 0);
    const dailyTimes = [];
    for (let time = startDay.getTime(); time < lastDay.getTime(); time += 24 * 60 * 60 * 1000) dailyTimes.push(time + 23 * 60 * 60 * 1000 + 55 * 60 * 1000);
    dailyTimes.push(nowMs);

    const quantities = new Map();
    let cash = 0, netContributions = 0, financing = 0, eventIndex = 0;
    const points = [];
    for (const at of dailyTimes) {
        while (eventIndex < events.length && events[eventIndex].at <= at) {
            const event = events[eventIndex++];
            cash += event.delta;
            if (event.stock_id !== null) quantities.set(event.stock_id, (quantities.get(event.stock_id) || 0) + event.quantity);
            if (event.kind === 'capital_INITIAL' || event.kind === 'capital_DEPOSIT' || event.kind === 'capital_WITHDRAWAL') netContributions += event.delta;
            if (event.kind === 'capital_FINANCING') financing += event.delta;
        }
        let positionValue = 0;
        const missingPrices = [];
        for (const [stockId, rawQuantity] of quantities) {
            const quantity = Math.abs(rawQuantity) < POSITION_EPSILON ? 0 : rawQuantity;
            if (quantity <= 0) continue;
            const rows = historyByStock.get(stockId) || [];
            let low = 0, high = rows.length;
            while (low < high) { const middle = (low + high) >> 1; if (rows[middle].at <= at) low = middle + 1; else high = middle; }
            const historical = rows[low - 1];
            let price = historical && at - historical.at <= maxPriceAgeMs ? historical.price : null;
            if (at === nowMs) {
                const currentPrice = Number(stockById.get(stockId)?.current_price_eur);
                if (currentPrice > 0) price = currentPrice;
            }
            if (!(price > 0)) { missingPrices.push(stockById.get(stockId)?.symbol || `stock-${stockId}`); continue; }
            positionValue += quantity * price;
        }
        const timestamp = new Date(at).toISOString().slice(0, 19).replace('T', ' ');
        points.push({ snapshot_at: timestamp, cash_eur: cash, position_value_eur: positionValue, equity_eur: cash + positionValue, net_contributions_eur: netContributions, financing_eur: financing, missing_prices: missingPrices });
    }
    return points;
}

module.exports = { POSITION_EPSILON, buildCashFlowEvents, buildReplaySnapshots, findMatchingBrokerSale, timestampMs };
