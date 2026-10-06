'use strict';

const crypto = require('node:crypto');
const PAGE_SIZE = 100;
const MAX_PAGES = 1000;
const WATERMARK_OVERLAP_MS = 24 * 60 * 60 * 1000;

function object(value) { return value && typeof value === 'object' ? value : {}; }

function numberValue(value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number' || typeof value === 'string') {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    }
    const source = object(value);
    if (source.unscaled !== undefined && source.scale !== undefined) {
        try { return Number(BigInt(source.unscaled)) / (10 ** Number(source.scale)); } catch { return null; }
    }
    for (const key of ['value', 'amount', 'price', 'quantity', 'size', 'executionSize']) {
        if (source[key] !== undefined) {
            const parsed = numberValue(source[key]);
            if (parsed !== null) return parsed;
        }
    }
    return null;
}

function currencyValue(value) {
    const source = object(value);
    return String(source.currency || source.currencyId || source.unit || '').trim().toUpperCase() || null;
}

function iso(value) {
    if (value === null || value === undefined || value === '') return null;
    const numeric = typeof value === 'number' || /^\d{10,13}$/.test(String(value)) ? Number(value) : null;
    const date = numeric !== null ? new Date(numeric < 100_000_000_000 ? numeric * 1000 : numeric) : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function stringIdentity(value) {
    if (typeof value === 'string' || typeof value === 'number') return String(value).trim() || null;
    const source = object(value);
    return stringIdentity(source.tradeId ?? source.executionId ?? source.id ?? source.value);
}

function tradeRows(order) {
    const raw = object(order?.raw);
    const values = raw.trades ?? raw.executions ?? raw.fills;
    return Array.isArray(values) ? values : [];
}

function normalizeFill(order, fill, category) {
    const rawOrder = object(order?.raw);
    const instrument = object(rawOrder.instrument);
    const identity = stringIdentity(fill.tradeId ?? fill.executionId ?? fill.executionIdentity ?? fill.id);
    const instrumentId = String(order?.instrumentId || order?.isin || rawOrder.instrumentId || rawOrder.isin || instrument.instrumentId || instrument.isin || instrument.id || '').trim().toUpperCase();
    const side = String(fill.side || fill.tradeType || order?.side || rawOrder.side || rawOrder.action || '').toLowerCase();
    const priceValue = fill.executionPrice ?? fill.fillPrice ?? fill.price;
    const feeValue = fill.executionFees ?? fill.fees ?? fill.fee ?? fill.feeAmount;
    const price = numberValue(priceValue);
    const quantity = numberValue(fill.executionSize ?? fill.executedSize ?? fill.filledQuantity ?? fill.quantity ?? fill.size);
    const fees = numberValue(feeValue);
    const executedAt = iso(fill.executedAt ?? fill.executionTime ?? fill.filledAt ?? fill.time ?? fill.timestamp ?? order?.executedAt);
    const currency = currencyValue(priceValue) || String(fill.currency || order?.currency || rawOrder.currency || 'EUR').toUpperCase();
    const feeCurrency = currencyValue(feeValue) || currency;
    const assetType = String(instrument.type || rawOrder.instrumentType || category || 'stock').toLowerCase().includes('crypto') ? 'crypto' : 'stock';
    if (!identity || !instrumentId || !['buy', 'sell'].includes(side) || !(price > 0) || !(quantity > 0)
        || fees === null || fees < 0 || !executedAt || currency !== 'EUR' || feeCurrency !== 'EUR') return null;
    return {
        identity,
        instrumentId,
        side,
        quantity,
        priceEur: price,
        feesEur: fees,
        executedAt,
        assetType,
        name: String(order?.name || rawOrder.instrumentName || instrument.name || instrument.shortName || rawOrder.name || '').trim(),
        symbol: String(instrument.ticker || instrument.symbol || rawOrder.ticker || rawOrder.symbol || '').trim().toUpperCase()
    };
}

function symbolFromFill(fill) {
    const direct = String(fill.symbol || '').toUpperCase().replace(/[^A-Z0-9.\-]/g, '').slice(0, 16);
    if (direct) return direct;
    const cryptoId = fill.instrumentId.match(/^XF000([A-Z]{2,8})\d+$/);
    if (cryptoId) return cryptoId[1];
    const name = String(fill.name || '').toUpperCase().replace(/[^A-Z0-9.\-]/g, '').slice(0, 12);
    if (name) return name;
    return `TR-${fill.instrumentId.replace(/[^A-Z0-9]/g, '').slice(-6) || 'ASSET'}`;
}

function addIncompleteCandidate(db, order, fill = {}, category = 'brokerage') {
    const raw = object(order?.raw);
    const identity = stringIdentity(fill.tradeId ?? fill.executionId ?? fill.id ?? raw.id ?? order?.id)
        || `${category}:${order?.instrumentId || order?.isin || 'unknown'}:${order?.executedAt || ''}`;
    const side = String(fill.side || fill.tradeType || order?.side || raw.side || '').toUpperCase();
    const instrumentId = String(order?.instrumentId || order?.isin || raw.instrumentId || raw.isin || '').toUpperCase();
    const amountValue = fill.executionAmount ?? fill.amount ?? order?.amount;
    db.addBrokerReconciliationCandidate?.({
        broker: 'trade_republic',
        candidate_key: crypto.createHash('sha256').update(`incomplete:${category}:${identity}`).digest('hex'),
        external_ref_masked: '••••',
        status: 'EXECUTED',
        title: `${String(order?.name || 'Trade Republic trade').slice(0, 120)} ${side}`.trim(),
        subtitle: instrumentId || null,
        amount_eur: numberValue(amountValue),
        currency: currencyValue(amountValue) || String(order?.currency || raw.currency || 'EUR').toUpperCase(),
        occurred_at: iso(fill.executedAt ?? fill.executionTime ?? fill.filledAt ?? order?.executedAt),
        reason: 'Executed order is missing a stable fill identity or complete EUR quantity, price, fee, and time data'
    });
}

function reconciliationCandidateKey(identity) {
    return crypto.createHash('sha256').update(`trade_republic:${identity}`).digest('hex');
}

async function fetchPages(client, category, watermark, { pageSize = PAGE_SIZE, maxPages = MAX_PAGES } = {}) {
    const orders = [];
    let newestUpdate = watermark;
    let page = 1;
    while (page <= maxPages) {
        const rows = await client.orders.executed({ instrumentCategory: category, page, pageSize, sort: 'orderUpdatedAt,desc' });
        const batch = Array.isArray(rows) ? rows : [];
        if (!batch.length) break;
        orders.push(...batch);
        const updates = batch.map(order => iso(order.updatedAt || object(order.raw).updatedAt)).filter(Boolean);
        for (const updatedAt of updates) if (!newestUpdate || Date.parse(updatedAt) > Date.parse(newestUpdate)) newestUpdate = updatedAt;
        const olderThanWatermark = watermark && batch.every(order => {
            const updatedAt = iso(order.updatedAt || object(order.raw).updatedAt);
            return updatedAt && Date.parse(updatedAt) <= Date.parse(watermark);
        });
        if (olderThanWatermark) break;
        if (batch.length < pageSize) break;
        page += 1;
    }
    if (page > maxPages) throw new Error(`Executed order history exceeded ${maxPages} pages`);
    return { orders, newestUpdate };
}

async function syncTradeRepublicExecutions({ db, sdkClient, now = () => new Date(), pageSize = PAGE_SIZE, maxPages = MAX_PAGES } = {}) {
    if (!db || !sdkClient) return { skipped: true, reason: 'not_configured' };
    if (typeof sdkClient.isEnabled === 'function' && !sdkClient.isEnabled()) return { skipped: true, reason: 'disabled' };
    if (!sdkClient.sessionFile) return { skipped: true, reason: 'session_not_configured' };
    const client = await sdkClient.connect();
    if (!client?.orders?.executed || typeof db.recordBrokerExecution !== 'function') throw new Error('Handelsrepublik execution sync is unavailable');

    const categories = ['brokerage', 'crypto'];
    const batches = [];
    const states = new Map();
    for (const category of categories) {
        const stateKey = `trade_republic_executions_${category}`;
        const prior = db.getBrokerSyncState(stateKey);
        const watermark = prior?.updated_at || null;
        const scanWatermark = watermark ? new Date(Math.max(0, Date.parse(watermark) - WATERMARK_OVERLAP_MS)).toISOString() : null;
        const batch = await fetchPages(client, category, scanWatermark, { pageSize, maxPages });
        const nextWatermark = batch.newestUpdate && (!watermark || Date.parse(batch.newestUpdate) > Date.parse(watermark))
            ? batch.newestUpdate
            : watermark || now().toISOString();
        states.set(stateKey, nextWatermark);
        batches.push({ category, ...batch, watermark: scanWatermark });
    }

    const fills = [];
    let ordersScanned = 0;
    let incompleteFills = 0;
    for (const batch of batches) {
        ordersScanned += batch.orders.length;
        for (const order of batch.orders) {
            const rows = tradeRows(order);
            if (!rows.length) {
                addIncompleteCandidate(db, order, {}, batch.category);
                incompleteFills += 1;
            }
            for (const row of rows) {
                const fill = normalizeFill(order, row, batch.category);
                if (!fill) {
                    addIncompleteCandidate(db, order, row, batch.category);
                    incompleteFills += 1;
                    continue;
                }
                if (batch.watermark && Date.parse(fill.executedAt) < Date.parse(batch.watermark)) continue;
                fills.push(fill);
            }
        }
    }
    fills.sort((left, right) => Date.parse(left.executedAt) - Date.parse(right.executedAt) || left.identity.localeCompare(right.identity));

    let imported = 0, alreadyRecorded = 0, skippedExisting = 0, matchedManual = 0;
    for (const fill of fills) {
        const candidateKey = reconciliationCandidateKey(fill.identity);
        if (db.getBrokerExecution(fill.identity)) {
            alreadyRecorded += 1;
            db.resolveBrokerReconciliationCandidate?.(candidateKey);
            continue;
        }
        let stock = db.getStockByBrokerInstrument(fill.instrumentId);
        if (!stock) {
            stock = db.ensureBrokerJournalStock({ instrumentId: fill.instrumentId, symbol: symbolFromFill(fill), name: fill.name, currency: 'EUR', assetType: fill.assetType });
        }
        try {
            const result = db.recordBrokerExecution({
                broker: 'trade_republic',
                execution_identity: fill.identity,
                order_intent_id: null,
                stock_id: stock.id,
                account_ref_masked: '••••',
                side: fill.side,
                quantity: fill.quantity,
                execution_price_eur: fill.priceEur,
                fees_eur: fill.feesEur,
                executed_at: fill.executedAt,
                currency: 'EUR'
            });
            if (result.existing) alreadyRecorded += 1;
            else imported += 1;
            db.resolveBrokerReconciliationCandidate?.(candidateKey);
        } catch (error) {
            if (error.code === 'SQLITE_CONSTRAINT_UNIQUE' && db.getBrokerExecution(fill.identity)) {
                alreadyRecorded += 1;
                db.resolveBrokerReconciliationCandidate?.(candidateKey);
                continue;
            }
            if (/oversell Journal lots/i.test(String(error.message))) {
                const manualMatch = db.recordBrokerExecutionAgainstManualTrade?.({
                    broker: 'trade_republic',
                    execution_identity: fill.identity,
                    order_intent_id: null,
                    stock_id: stock.id,
                    account_ref_masked: '••••',
                    side: fill.side,
                    quantity: fill.quantity,
                    execution_price_eur: fill.priceEur,
                    fees_eur: fill.feesEur,
                    executed_at: fill.executedAt,
                    currency: 'EUR'
                });
                if (manualMatch) {
                    imported += 1;
                    matchedManual += 1;
                    db.resolveBrokerReconciliationCandidate?.(candidateKey);
                    continue;
                }
                skippedExisting += 1;
                db.addBrokerReconciliationCandidate?.({
                    broker: 'trade_republic',
                    candidate_key: candidateKey,
                    external_ref_masked: '••••',
                    status: 'EXECUTED',
                    title: `${symbolFromFill(fill)} ${fill.side.toUpperCase()}`,
                    subtitle: fill.name || fill.instrumentId,
                    amount_eur: fill.quantity * fill.priceEur,
                    currency: 'EUR',
                    occurred_at: fill.executedAt,
                    reason: 'Confirmed sell exceeds Journal lots; earlier purchase history may be incomplete'
                });
                continue;
            }
            throw error;
        }
    }
    for (const [key, value] of states) db.setBrokerSyncState(key, value);
    return { skipped: false, orders_scanned: ordersScanned, fills_seen: fills.length, imported, already_recorded: alreadyRecorded, matched_manual: matchedManual, skipped_oversells: skippedExisting, incomplete_fills: incompleteFills };
}

module.exports = { PAGE_SIZE, MAX_PAGES, WATERMARK_OVERLAP_MS, numberValue, currencyValue, iso, normalizeFill, fetchPages, syncTradeRepublicExecutions, reconciliationCandidateKey };
