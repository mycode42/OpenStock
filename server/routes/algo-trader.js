const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const { execFile } = require('node:child_process');

const FALLBACK_DEFAULT_SETTINGS = require('../algo-strategy-config.default.json');
const researchSummaryCache = new Map();
const strategyPnlCache = new Map();
let usdEurRateCache = null;
const STRATEGY_PNL_CACHE_MS = 90_000;
const USD_EUR_CACHE_MS = 60 * 60_000;
const OPTIONS_STRATEGY_IDS = Object.freeze([
    'options_vrp',
    'options_event_driven',
    'options_directional_overlay',
    'options_long_volatility_sleeve',
]);
const OPTIONS_STRATEGY_ALIASES = Object.freeze({
    vrp: 'options_vrp',
    event_driven: 'options_event_driven',
    directional_overlay: 'options_directional_overlay',
    long_volatility: 'options_long_volatility_sleeve',
    options_long_volatility: 'options_long_volatility_sleeve',
});

function finite(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function safeObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function canonicalOptionsStrategyId(value) {
    const strategy = String(value || '').trim();
    return OPTIONS_STRATEGY_ALIASES[strategy] || strategy;
}

function runPythonJson(root, script, env = {}) {
    const configuredPython = process.env.ALGO_TRADER_PYTHON;
    const executable = configuredPython || '/usr/local/bin/poetry';
    const args = configuredPython ? ['-c', script] : ['run', 'python', '-c', script];
    return new Promise((resolve, reject) => {
        execFile(executable, args, {
            cwd: root,
            env: { ...process.env, ...env },
            timeout: 90_000,
            maxBuffer: 4 * 1024 * 1024,
        }, (error, stdout) => {
            try {
                resolve(JSON.parse(stdout));
            } catch {
                reject(error || new Error('Algo Trader fill report returned invalid JSON'));
            }
        });
    });
}

async function readEquityFillReport(root, accountId) {
    const cacheKey = `${path.resolve(root)}:${accountId || ''}`;
    const now = Date.now();
    const cached = strategyPnlCache.get(cacheKey);
    if (cached?.promise) return cached.promise;
    if (cached && cached.expiresAt > now) return cached.value;

    const script = [
        'import json, os',
        'from decimal import Decimal',
        'from reporting.performance import load_trade_journal',
        'from reporting.fills import get_account_fill_report',
        'from strategies.catalog import STRATEGY_CATALOG, canonical_strategy_id',
        'account_id = os.environ.get("OPENSTOCK_ALGO_ACCOUNT_ID") or None',
        'journal = load_trade_journal(mode="algo", account_id=account_id)',
        'journal = [({**row, "strategy": canonical_strategy_id(row["strategy"])} if isinstance(row.get("strategy"), str) else row) for row in journal]',
        'report = get_account_fill_report("algo", journal)',
        'latest = {}',
        'for fill in report.get("realized_fills", []):',
        '    for name, pnl in fill.get("strategy_pnl", {}).items():',
        '        basis = Decimal(str(fill.get("strategy_basis", {}).get(name) or "0"))',
        '        latest[name] = {"at": fill.get("transaction_time"), "action": "COVER" if fill.get("side") == "buy_to_cover" else "SELL", "symbol": fill.get("symbol"), "qty": fill.get("strategy_qty", {}).get(name), "status": "closed" if fill.get("basis_status") == "covered" else fill.get("basis_status", "unknown"), "pnl_usd": pnl, "pnl_pct": float(Decimal(str(pnl)) / basis * 100) if basis > 0 else None}',
        'for fill in report.get("open_fills", []):',
        '    name = fill.get("strategy")',
        '    if not name or (name in latest and str(fill.get("transaction_time") or "") <= str(latest[name].get("at") or "")): continue',
        '    pnl, basis = fill.get("unrealized_pnl"), Decimal(str(fill.get("basis") or "0"))',
        '    latest[name] = {"at": fill.get("transaction_time"), "action": "SHORT" if fill.get("side") == "sell_short" else "BUY", "symbol": fill.get("symbol"), "qty": fill.get("qty"), "status": "open position" if pnl is not None else "open · mark unavailable", "pnl_usd": pnl, "pnl_pct": float(Decimal(str(pnl)) / basis * 100) if pnl is not None and basis > 0 else None}',
        'keys = ("strategy_pnl", "strategy_basis", "strategy_open_pnl", "strategy_open_basis", "strategy_open_pnl_complete", "realized_pnl_complete", "strategy_attribution_complete", "source", "fetched_at")',
        'payload = {key: report.get(key) for key in keys}',
        'payload["strategy_latest_activity"] = latest',
        'payload["known_strategies"] = list(STRATEGY_CATALOG)',
        'print(json.dumps(payload, separators=(",", ":")))',
    ].join('\n');
    const promise = runPythonJson(root, script, { OPENSTOCK_ALGO_ACCOUNT_ID: String(accountId || '') })
        .then(value => {
            strategyPnlCache.set(cacheKey, { value, expiresAt: Date.now() + STRATEGY_PNL_CACHE_MS });
            return value;
        })
        .catch(() => {
            const staleValue = cached?.value ?? cached?.staleValue;
            const value = staleValue ? { ...staleValue, stale: true } : { unavailable: true };
            strategyPnlCache.set(cacheKey, { value, expiresAt: Date.now() + 30_000 });
            return value;
        });
    strategyPnlCache.set(cacheKey, {
        promise,
        staleValue: cached?.value ?? cached?.staleValue ?? null,
        expiresAt: now + STRATEGY_PNL_CACHE_MS,
    });
    return promise;
}

function cachedEquityFillReport(root, accountId) {
    const cacheKey = `${path.resolve(root)}:${accountId || ''}`;
    const cached = strategyPnlCache.get(cacheKey);
    if (cached && cached.expiresAt > Date.now()) {
        return cached.value ?? cached.staleValue ?? { loading: true };
    }
    if (!cached?.promise) void readEquityFillReport(root, accountId);
    return cached?.value ?? cached?.staleValue ?? { loading: true };
}

async function readUsdEurRate() {
    const now = Date.now();
    if (usdEurRateCache && usdEurRateCache.expiresAt > now) return usdEurRateCache.value;
    try {
        const response = await fetch('https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR', {
            signal: AbortSignal.timeout(3_000),
        });
        if (!response.ok) throw new Error('FX service unavailable');
        const payload = await response.json();
        const rate = finite(safeObject(payload.rates).EUR);
        if (rate === null || rate <= 0) throw new Error('FX rate unavailable');
        const value = { rate, date: String(payload.date || ''), source: 'ECB reference rate via Frankfurter' };
        usdEurRateCache = { value, expiresAt: now + USD_EUR_CACHE_MS };
        return value;
    } catch {
        if (usdEurRateCache?.value) {
            usdEurRateCache.expiresAt = now + 5 * 60_000;
            return usdEurRateCache.value;
        }
        usdEurRateCache = { value: null, expiresAt: now + 5 * 60_000 };
        return null;
    }
}

function attachEuroAmounts(performance, fx) {
    const convert = value => {
        const amount = finite(value);
        return amount === null || !fx ? null : amount * fx.rate;
    };
    return {
        ...performance,
        currency: 'EUR',
        fx_rate: fx?.rate ?? null,
        fx_date: fx?.date || null,
        fx_source: fx?.source || null,
        strategies: (performance.strategies || []).map(strategy => ({
            ...strategy,
            latest_pnl_eur: convert(strategy.latest_pnl_usd),
            cumulative_pnl_eur: convert(strategy.cumulative_pnl_usd),
        })),
    };
}

function equityStrategyPerformance(report) {
    if (report?.loading) {
        return {
            status: 'loading',
            message: 'Reconciling broker fills and current positions.',
            strategies: [],
        };
    }
    if (!report || report.unavailable) {
        return {
            status: 'unavailable',
            message: 'Broker fill history could not be reconciled.',
            strategies: [],
        };
    }
    const names = new Set([
        ...(Array.isArray(report.known_strategies) ? report.known_strategies : []),
        ...Object.keys(safeObject(report.strategy_pnl)),
        ...Object.keys(safeObject(report.strategy_basis)),
        ...Object.keys(safeObject(report.strategy_open_pnl)),
        ...Object.keys(safeObject(report.strategy_open_basis)),
        ...Object.keys(safeObject(report.strategy_latest_activity)),
    ]);
    const byName = new Map([...names].map(name => [name, {
        strategy: name,
        realizedPnl: finite(report.strategy_pnl?.[name]) ?? 0,
        realizedBasis: finite(report.strategy_basis?.[name]) ?? 0,
        openPnl: finite(report.strategy_open_pnl?.[name]) ?? 0,
        openBasis: finite(report.strategy_open_basis?.[name]) ?? 0,
        hasUnpricedOpenLot: false,
        latest: null,
    }]));
    const recordLatest = (name, activity) => {
        if (!name || !byName.has(name)) return;
        const current = byName.get(name).latest;
        if (!current || String(activity.at || '').localeCompare(String(current.at || '')) > 0) {
            byName.get(name).latest = activity;
        }
    };
    for (const [name, activity] of Object.entries(safeObject(report.strategy_latest_activity))) {
        if (activity.pnl_usd === null && String(activity.status || '').startsWith('open')) {
            byName.get(name).hasUnpricedOpenLot = true;
        }
        recordLatest(name, activity);
    }
    const complete = report.stale !== true
        && report.realized_pnl_complete === true
        && report.strategy_attribution_complete === true
        && report.strategy_open_pnl_complete === true;
    const strategies = [...byName.values()].map(row => {
        const hasActivity = Boolean(row.latest) || row.realizedBasis !== 0 || row.openBasis !== 0
            || row.realizedPnl !== 0 || row.openPnl !== 0 || row.hasUnpricedOpenLot;
        const pnl = !hasActivity || row.hasUnpricedOpenLot ? null : row.realizedPnl + row.openPnl;
        const basis = row.realizedBasis + row.openBasis;
        return {
            strategy: row.strategy,
            latest_activity: row.latest,
            latest_pnl_usd: row.latest?.pnl_usd ?? null,
            latest_pnl_pct: row.latest?.pnl_pct ?? null,
            cumulative_pnl_usd: pnl,
            cumulative_pnl_pct: pnl !== null && basis > 0 ? pnl / basis * 100 : null,
            complete,
        };
    }).sort((left, right) => {
        const a = finite(left.cumulative_pnl_usd);
        const b = finite(right.cumulative_pnl_usd);
        if (a === null) return b === null ? left.strategy.localeCompare(right.strategy) : 1;
        if (b === null) return -1;
        return b - a;
    });
    const hasActivity = strategies.some(strategy => strategy.latest_activity
        || strategy.cumulative_pnl_usd !== null);
    return {
        status: hasActivity ? complete ? 'ready' : 'partial' : complete ? 'empty' : 'partial',
        message: report.stale === true
            ? 'Showing the last reconciled broker report because the latest refresh failed.'
            : complete
                ? 'Realized P/L and current positions are reconciled to broker fills.'
                : 'Only strategy-attributed fills with a supported cost basis are included; incomplete attribution is marked partial.',
        strategies,
        fx_rate: null,
        source: 'Alpaca paper fills and positions',
    };
}

function optionsStrategyPerformance(orders, fills, strategyGreeks, riskRows) {
    const ordersByClientId = new Map();
    const exitOrdersByEntryId = new Map();
    for (const row of orders) {
        const clientId = String(row?.client_order_id || '');
        if (clientId) {
            const prior = ordersByClientId.get(clientId) || {};
            ordersByClientId.set(clientId, { ...prior, ...row, signal: row.signal || prior.signal, order: row.order || prior.order });
        }
        if (row?.kind === 'long_volatility_exit' && row.entry_client_order_id) {
            const prior = exitOrdersByEntryId.get(String(row.entry_client_order_id));
            if (!prior || String(row.logged_at || '').localeCompare(String(prior.logged_at || '')) >= 0) {
                exitOrdersByEntryId.set(String(row.entry_client_order_id), row);
            }
        }
    }
    const fillsByClientId = new Map();
    for (const row of fills) {
        const clientId = String(row?.client_order_id || '');
        if (clientId) fillsByClientId.set(clientId, row);
    }
    const latestRisk = [...riskRows].sort((a, b) => String(a.logged_at || '').localeCompare(String(b.logged_at || ''))).at(-1) || {};
    const currentPositionsBySymbol = new Map((Array.isArray(latestRisk.open_option_positions) ? latestRisk.open_option_positions : [])
        .map(position => [String(position.symbol || '').toUpperCase(), position]).filter(([symbol]) => symbol));
    const currentPositionSymbols = new Set(currentPositionsBySymbol.keys());
    const knownLifecycleEntries = new Set();
    const longVolEntryIds = new Set(strategyGreeks
        .filter(row => row?.kind === 'long_volatility_position_mark' && row.entry_client_order_id)
        .map(row => String(row.entry_client_order_id)));
    const legDetails = row => {
        const order = safeObject(row?.order);
        const signal = safeObject(row?.signal);
        const signalStructure = safeObject(signal.structure);
        const signalLegs = Array.isArray(signalStructure.legs) ? signalStructure.legs : [];
        const submittedLegs = Array.isArray(order.legs) ? order.legs : [];
        if (submittedLegs.length) return submittedLegs.map(leg => ({
            symbol: String(leg.symbol || '').toUpperCase(),
            side: String(leg.side || '').toLowerCase(),
            intent: String(leg.position_intent || '').toLowerCase(),
        })).filter(leg => leg.symbol);
        if (order.symbol) return [{
            symbol: String(order.symbol).toUpperCase(),
            side: String(order.side || '').toLowerCase(),
            intent: String(order.position_intent || '').toLowerCase(),
        }];
        return signalLegs.map(leg => ({
            symbol: String(safeObject(leg.contract).symbol || leg.symbol || '').toUpperCase(),
            side: String(leg.side || '').toLowerCase(),
            intent: String(leg.position_intent || '').toLowerCase(),
        })).filter(leg => leg.symbol);
    };
    const strategyForOrder = row => canonicalOptionsStrategyId(safeObject(row?.signal).strategy);
    const executionLegs = (fill, order, submittedLegs) => {
        const brokerLegs = safeObject(order?.broker_response).legs;
        const journalLegs = Array.isArray(fill?.legs) ? fill.legs : [];
        const candidates = [journalLegs, Array.isArray(brokerLegs) ? brokerLegs : []];
        const executions = candidates.find(rows => rows.some(leg => (
            leg && typeof leg === 'object'
            && finite(leg.filled_qty) !== null
            && finite(leg.filled_avg_price) !== null
        ))) || [];
        return submittedLegs.map(submitted => {
            const execution = executions.find(leg => String(leg?.symbol || '').toUpperCase() === submitted.symbol) || {};
            const isSingleLeg = submittedLegs.length === 1;
            return {
                ...submitted,
                qty: finite(execution.filled_qty) ?? (isSingleLeg ? finite(fill?.filled_qty) : null),
                price: finite(execution.filled_avg_price) ?? (isSingleLeg ? finite(fill?.filled_avg_price) : null),
                multiplier: finite(execution.multiplier) ?? 100,
            };
        });
    };
    const ownersBySymbol = new Map();
    const lotsBySymbol = new Map();
    const realizedByClientId = new Map();
    const strategies = new Map();
    const ensure = name => {
        if (!name) return null;
        if (!strategies.has(name)) strategies.set(name, {
            strategy: name,
            latest_activity: null,
            latest_at: '',
            realized_pnl: 0,
            realized_basis: 0,
            open_pnl: 0,
            open_basis: 0,
            has_untracked_close: false,
            has_unpriced_position: false,
            known_activity: false,
        });
        return strategies.get(name);
    };
    for (const name of OPTIONS_STRATEGY_IDS) ensure(name);
    const activityAction = (legs, fallback = 'TRADE') => {
        const intents = legs.map(leg => leg.intent);
        if (intents.length && intents.every(intent => intent.includes('to_close'))) {
            return legs.length > 1 ? 'CLOSE SPREAD' : legs[0].side === 'buy' ? 'COVER' : 'SELL';
        }
        if (intents.length && intents.every(intent => intent.includes('to_open'))) {
            return legs.length > 1 ? 'OPEN SPREAD' : legs[0].side === 'buy' ? 'BUY' : 'SHORT';
        }
        return fallback;
    };
    const recordLatest = (name, activity) => {
        const strategy = ensure(name);
        if (!strategy) return;
        strategy.known_activity = true;
        if (!strategy.latest_activity || String(activity.at || '').localeCompare(strategy.latest_at) >= 0) {
            strategy.latest_at = String(activity.at || '');
            strategy.latest_activity = activity;
        }
    };

    const orderedFills = [...fills].sort((a, b) => String(a?.logged_at || '').localeCompare(String(b?.logged_at || '')));
    for (const fill of orderedFills) {
        const clientId = String(fill?.client_order_id || '');
        const order = ordersByClientId.get(clientId) || {};
        const entryId = String(order.entry_client_order_id || clientId);
        const entryOrder = ordersByClientId.get(entryId) || order;
        const strategyName = strategyForOrder(entryOrder) || strategyForOrder(order);
        if (!strategyName) continue;
        const legs = legDetails(order.order ? order : entryOrder);
        const action = order.kind === 'long_volatility_exit'
            ? 'SELL'
            : activityAction(legs, 'TRADE');
        recordLatest(strategyName, {
            at: isoTimestamp(fill.logged_at),
            action,
            symbol: String(safeObject(order.signal).underlying || safeObject(entryOrder.signal).underlying || '').toUpperCase(),
            qty: finite(fill.filled_qty),
            client_id: clientId,
            status: String(fill.status || 'filled'),
            pnl_usd: null,
            pnl_pct: null,
        });
        if (longVolEntryIds.has(clientId) || order.kind === 'long_volatility_exit') continue;
        const executions = executionLegs(fill, order, legs);
        const hasCloseLeg = legs.some(leg => leg.intent.includes('to_close'));
        const closeResult = { pnl: 0, basis: 0, qty: 0, complete: true };
        for (const [index, leg] of legs.entries()) {
            const intent = leg.intent;
            const isOpen = intent.includes('to_open');
            const isClose = intent.includes('to_close');
            if (!isOpen && !isClose) continue;
            const execution = executions[index] || {};
            const qty = execution.qty;
            const price = execution.price;
            if (isOpen) {
                const owners = ownersBySymbol.get(leg.symbol) || [];
                owners.push({ strategy: strategyName, clientId, at: fill.logged_at, leg });
                ownersBySymbol.set(leg.symbol, owners);
                if (qty === null || qty <= 0 || price === null || price < 0) {
                    ensure(strategyName).has_unpriced_position = true;
                    continue;
                }
                const lots = lotsBySymbol.get(leg.symbol) || [];
                lots.push({
                    qty,
                    price,
                    multiplier: execution.multiplier,
                    isLong: intent.startsWith('buy_'),
                    strategy: strategyName,
                    clientId,
                });
                lotsBySymbol.set(leg.symbol, lots);
                continue;
            }
            if (qty === null || qty <= 0 || price === null || price < 0) {
                closeResult.complete = false;
                continue;
            }
            let remaining = qty;
            const closingLong = intent.startsWith('sell_');
            const lots = lotsBySymbol.get(leg.symbol) || [];
            for (const lot of lots) {
                if (remaining <= 0) break;
                if (lot.qty <= 0 || lot.isLong !== closingLong) continue;
                const matched = Math.min(remaining, lot.qty);
                if (lot.strategy !== strategyName) {
                    closeResult.complete = false;
                    ensure(lot.strategy).has_untracked_close = true;
                    ensure(strategyName).has_untracked_close = true;
                } else {
                    const basis = matched * lot.price * lot.multiplier;
                    const pnl = matched * (lot.isLong ? price - lot.price : lot.price - price) * lot.multiplier;
                    closeResult.basis += basis;
                    closeResult.pnl += pnl;
                    closeResult.qty += matched;
                    ensure(strategyName).realized_basis += basis;
                    ensure(strategyName).realized_pnl += pnl;
                    knownLifecycleEntries.add(lot.clientId);
                }
                lot.qty -= matched;
                remaining -= matched;
            }
            if (remaining > 0) closeResult.complete = false;
        }
        if (hasCloseLeg) {
            if (!closeResult.complete || closeResult.qty <= 0) ensure(strategyName).has_untracked_close = true;
            const prior = realizedByClientId.get(clientId) || { pnl: 0, basis: 0, qty: 0, complete: true };
            prior.pnl += closeResult.pnl;
            prior.basis += closeResult.basis;
            prior.qty += closeResult.qty;
            prior.complete = prior.complete && closeResult.complete && closeResult.qty > 0;
            realizedByClientId.set(clientId, prior);
        }
    }

    for (const strategy of strategies.values()) {
        const latest = strategy.latest_activity;
        const realized = latest ? realizedByClientId.get(latest.client_id) : null;
        if (!latest || !realized) continue;
        latest.status = realized.complete ? 'closed' : 'closed · P/L incomplete';
        latest.pnl_usd = realized.complete ? realized.pnl : null;
        latest.pnl_pct = realized.complete && realized.basis > 0 ? realized.pnl / realized.basis * 100 : null;
    }

    const latestLongVolMarks = new Map();
    for (const row of strategyGreeks) {
        if (row?.kind !== 'long_volatility_position_mark' || !row.entry_client_order_id) continue;
        const entryId = String(row.entry_client_order_id);
        const prior = latestLongVolMarks.get(entryId);
        if (!prior || String(row.logged_at || '').localeCompare(String(prior.logged_at || '')) >= 0) {
            latestLongVolMarks.set(entryId, row);
        }
    }
    const longVolOpenSymbols = new Set();
    for (const [entryId, mark] of latestLongVolMarks) {
        const entryOrder = ordersByClientId.get(entryId) || {};
        const name = strategyForOrder(entryOrder);
        const entryFill = fillsByClientId.get(entryId);
        const strategy = ensure(name);
        if (!strategy || !entryFill) continue;
        const entryLegs = legDetails(entryOrder);
        const isOpen = entryLegs.length > 0 && entryLegs.every(leg => currentPositionSymbols.has(leg.symbol));
        if (isOpen) for (const leg of entryLegs) longVolOpenSymbols.add(leg.symbol);
        const exitOrder = exitOrdersByEntryId.get(entryId);
        const exitClientId = String(exitOrder?.client_order_id || '');
        const exitFill = exitClientId ? fillsByClientId.get(exitClientId) : null;
        const entryBasis = finite(mark.entry_debit);
        let pnl = null;
        let basis = entryBasis;
        let at = entryFill.logged_at;
        let action = 'BUY';
        let status = 'open position';
        const exitStatus = String(exitFill?.status || safeObject(exitOrder?.broker_response).status || '').toLowerCase();
        if (exitFill && exitStatus === 'filled') {
            const exitPrice = finite(exitFill.filled_avg_price);
            const exitQty = finite(exitFill.filled_qty);
            if (exitPrice !== null && exitQty !== null && entryBasis !== null) {
                knownLifecycleEntries.add(entryId);
                // Alpaca records net credits for multi-leg sell-to-close orders
                // with a negative aggregate average price. The long-volatility
                // position's proceeds are positive, so use the credit magnitude.
                pnl = Math.abs(exitPrice) * exitQty * 100 - entryBasis;
                basis = entryBasis;
                at = exitFill.logged_at;
                action = 'SELL';
                status = 'closed';
            }
        } else if (isOpen) {
            const currentValue = finite(mark.current_value_at_bid);
            if (currentValue !== null && entryBasis !== null) pnl = currentValue - entryBasis;
            knownLifecycleEntries.add(entryId);
        } else {
            strategy.has_untracked_close = true;
            status = 'closed · exit fill unavailable';
        }
        if (pnl === null || basis === null || basis <= 0) strategy.has_unpriced_position = true;
        else if (status === 'closed') {
            strategy.realized_pnl += pnl;
            strategy.realized_basis += basis;
        } else {
            strategy.open_pnl += pnl;
            strategy.open_basis += basis;
        }
        recordLatest(name, {
            at: isoTimestamp(at), action,
            symbol: String(mark.underlying || '').toUpperCase(),
            qty: finite(entryFill.filled_qty),
            status,
            pnl_usd: pnl,
            pnl_pct: pnl !== null && basis > 0 ? pnl / basis * 100 : null,
        });
    }

    const openEntryIds = new Set();
    for (const position of Array.isArray(latestRisk.open_option_positions) ? latestRisk.open_option_positions : []) {
        const symbol = String(position.symbol || '').toUpperCase();
        if (!symbol || longVolOpenSymbols.has(symbol)) continue;
        const qty = finite(position.qty);
        const midpoint = finite(position.midpoint);
        const multiplier = finite(position.multiplier) ?? 100;
        const brokerEntry = finite(position.average_entry_price);
        const lots = (lotsBySymbol.get(symbol) || []).filter(lot => lot.qty > 0);
        if (!lots.length) continue;
        const fillQty = lots.reduce((total, lot) => total + lot.qty * (lot.isLong ? 1 : -1), 0);
        const fillEntry = lots.reduce((total, lot) => total + lot.qty * lot.price, 0) / Math.abs(fillQty || 1);
        const entryMismatch = brokerEntry !== null && Math.abs(fillEntry - brokerEntry) > 0.02;
        if (qty === null || midpoint === null || multiplier <= 0 || Math.abs(fillQty - qty) > 1e-8 || entryMismatch) {
            for (const lot of lots) ensure(lot.strategy).has_unpriced_position = true;
            continue;
        }
        for (const lot of lots) {
            const strategy = ensure(lot.strategy);
            const lotMultiplier = multiplier || lot.multiplier;
            strategy.open_pnl += lot.qty * (lot.isLong ? midpoint - lot.price : lot.price - midpoint) * lotMultiplier;
            strategy.open_basis += lot.qty * lot.price * lotMultiplier;
            openEntryIds.add(lot.clientId);
        }
    }

    for (const [symbol, lots] of lotsBySymbol) {
        const openLots = lots.filter(lot => lot.qty > 0);
        if (!openLots.length) continue;
        const actual = finite(currentPositionsBySymbol.get(symbol)?.qty);
        const expected = openLots.reduce((total, lot) => total + lot.qty * (lot.isLong ? 1 : -1), 0);
        if (actual === null || Math.abs(actual - expected) > 1e-8) {
            for (const lot of openLots) ensure(lot.strategy).has_unpriced_position = true;
        }
    }

    for (const owners of ownersBySymbol.values()) {
        for (const owner of owners) {
            if (!knownLifecycleEntries.has(owner.clientId) && !openEntryIds.has(owner.clientId)) {
                ensure(owner.strategy).has_untracked_close = true;
            }
        }
    }

    for (const strategy of strategies.values()) {
        const latest = strategy.latest_activity;
        if (!latest || !openEntryIds.has(latest.client_id) || strategy.has_unpriced_position) continue;
        latest.status = 'open position';
        latest.pnl_usd = strategy.open_pnl;
        latest.pnl_pct = strategy.open_basis > 0 ? strategy.open_pnl / strategy.open_basis * 100 : null;
    }

    const unmatchedPositions = (Array.isArray(latestRisk.open_option_positions) ? latestRisk.open_option_positions : [])
        .some(position => {
            const symbol = String(position.symbol || '').toUpperCase();
            return symbol && !longVolOpenSymbols.has(symbol) && !(ownersBySymbol.get(symbol) || []).length;
        });
    const rows = [...strategies.values()].map(strategy => {
        const hasActivity = strategy.known_activity || strategy.realized_basis !== 0 || strategy.open_basis !== 0
            || strategy.realized_pnl !== 0 || strategy.open_pnl !== 0
            || strategy.has_unpriced_position || strategy.has_untracked_close;
        const cumulativePnl = !hasActivity || strategy.has_unpriced_position || strategy.has_untracked_close
            ? null : strategy.realized_pnl + strategy.open_pnl;
        const cumulativeBasis = strategy.realized_basis + strategy.open_basis;
        const latest = strategy.latest_activity;
        let latestPnl = latest?.pnl_usd ?? null;
        let latestPnlPct = latest?.pnl_pct ?? null;
        if (latest && latest.status === 'open position' && !strategy.has_unpriced_position) {
            latestPnl = strategy.open_pnl;
            latestPnlPct = strategy.open_basis > 0 ? strategy.open_pnl / strategy.open_basis * 100 : null;
        }
        return {
            strategy: strategy.strategy,
            latest_activity: latest,
            latest_pnl_usd: latestPnl,
            latest_pnl_pct: latestPnlPct,
            cumulative_pnl_usd: cumulativePnl,
            cumulative_pnl_pct: cumulativePnl !== null && cumulativeBasis > 0 ? cumulativePnl / cumulativeBasis * 100 : null,
            complete: hasActivity && cumulativePnl !== null && !unmatchedPositions,
        };
    }).sort((left, right) => {
        const a = finite(left.cumulative_pnl_usd);
        const b = finite(right.cumulative_pnl_usd);
        if (a === null) return b === null ? left.strategy.localeCompare(right.strategy) : 1;
        if (b === null) return -1;
        return b - a;
    });
    const hasActivity = rows.some(row => row.latest_activity || row.cumulative_pnl_usd !== null);
    const hasUnknownHistory = unmatchedPositions || rows.some(row => (
        (row.latest_activity || row.cumulative_pnl_usd !== null) && !row.complete
    ));
    return {
        status: hasActivity ? hasUnknownHistory ? 'partial' : 'ready' : hasUnknownHistory ? 'partial' : 'empty',
        message: hasUnknownHistory
            ? 'Open positions are marked from the latest account journal. Closed strategy P/L is shown only where an exit fill is recorded.'
            : 'Strategy P/L is calculated from confirmed option fills and the latest position marks.',
        source: 'Options paper fills and position marks',
        known_strategies: OPTIONS_STRATEGY_IDS,
        strategies: rows,
        fx_rate: null,
    };
}

async function readJson(filePath, { optional = false } = {}) {
    try {
        const value = JSON.parse(await fs.readFile(filePath, 'utf8'));
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Expected JSON object');
        return value;
    } catch (error) {
        if (optional && error.code === 'ENOENT') return null;
        throw error;
    }
}

function validateSettings(value, defaults = FALLBACK_DEFAULT_SETTINGS) {
    const settings = mergeConfig(defaults, safeObject(value));
    validateConfigShape(settings, defaults);
    const pctPaths = [
        'general.risk_per_trade_pct', 'portfolio.max_name_pct', 'portfolio.max_position_pct',
        'portfolio.max_strategy_position_pct', 'portfolio.max_gross_pct', 'portfolio.max_net_long_pct',
        'portfolio.max_net_short_pct', 'portfolio.max_short_exposure_pct', 'portfolio.min_cash_pct',
        'portfolio.daily_loss_limit_pct', 'portfolio.weekly_loss_limit_pct', 'portfolio.no_buy_if_down_pct',
        'portfolio.max_tactical_pct',
    ];
    for (const pathName of pctPaths) {
        const percent = Number(pathName.split('.').reduce((value, key) => value?.[key], settings));
        if (!Number.isFinite(percent) || percent < 0 || percent > 1) throw new Error(pathName + ' must be between 0 and 1');
    }
    const portfolio = settings.portfolio;
    if (portfolio.max_gross_pct > 1 - portfolio.min_cash_pct) {
        throw new Error('portfolio.max_gross_pct cannot exceed the configured cash floor');
    }
    if (portfolio.max_position_pct > portfolio.max_strategy_position_pct
        || portfolio.max_net_long_pct > portfolio.max_gross_pct
        || portfolio.max_net_short_pct > portfolio.max_gross_pct
        || portfolio.max_tactical_pct > portfolio.max_gross_pct) {
        throw new Error('portfolio position and net exposure limits must fit within the configured gross cap');
    }
    if (portfolio.max_intraday_entries + portfolio.max_swing_entries > portfolio.max_daily_entries) {
        throw new Error('portfolio intraday and swing entry limits cannot exceed the daily entry limit');
    }
    if (settings.general.model_min_confidence < 0 || settings.general.model_min_confidence > 1) {
        throw new Error('general.model_min_confidence must be between 0 and 1');
    }
    if (settings.general.model_training_window < 252) {
        throw new Error('general.model_training_window must be at least 252');
    }
    if (settings.general.risk_per_trade_pct <= 0 || settings.general.risk_per_trade_pct > 0.02) {
        throw new Error('general.risk_per_trade_pct must be between 0 and 0.02');
    }
    if (settings.general.take_profit_r_multiple <= 0 || settings.general.take_profit_r_multiple > 10) {
        throw new Error('general.take_profit_r_multiple must be between 0 and 10');
    }
    if (settings.general.volatile_policy_risk_multiplier <= 0 || settings.general.volatile_policy_risk_multiplier > 1.5) {
        throw new Error('general.volatile_policy_risk_multiplier must be between 0 and 1.5');
    }
    if (!['committee', 'rule', 'hmm', 'gmm'].includes(settings.general.regime_engine)) {
        throw new Error('general.regime_engine must be committee, rule, hmm, or gmm');
    }
    if (!['core', 'technical', 'macro', 'marketwide'].includes(settings.general.regime_feature_set)) {
        throw new Error('general.regime_feature_set is unsupported');
    }
    for (const [key, value] of Object.entries(settings.general.volatility_risk_scales)) {
        if (!Number.isFinite(value) || value < 0 || value > 1.5) {
            throw new Error('general.volatility_risk_scales.' + key + ' must be between 0 and 1.5');
        }
    }
    const integerPaths = [
        'general.model_training_window',
        'portfolio.max_daily_entries', 'portfolio.max_intraday_entries', 'portfolio.max_swing_entries',
        'portfolio.max_intraday_core_entries', 'portfolio.max_open_positions',
        'portfolio.max_positions_per_strategy', 'portfolio.max_short_positions', 'portfolio.max_tactical_positions',
        'portfolio.swing_open_delay_minutes', 'portfolio.intraday_close_cutoff_minutes',
    ];
    for (const pathName of integerPaths) {
        const value = Number(pathName.split('.').reduce((item, key) => item?.[key], settings));
        if (!Number.isInteger(value) || value < 0) throw new Error(pathName + ' must be a non-negative integer');
    }
    for (const key of ['max_name_pct', 'max_position_pct', 'max_strategy_position_pct', 'max_gross_pct', 'max_net_long_pct', 'max_net_short_pct']) {
        if (portfolio[key] <= 0) throw new Error('portfolio.' + key + ' must be positive');
    }
    for (const [strategy, config] of Object.entries(settings.strategies)) {
        if (config.horizon !== defaults.strategies[strategy].horizon
            || config.bar_interval !== defaults.strategies[strategy].bar_interval) {
            throw new Error('Strategy horizon and bar interval are fixed by the catalog');
        }
        if (config.max_position_pct <= 0 || config.max_position_pct > portfolio.max_strategy_position_pct) {
            throw new Error('strategies.' + strategy + '.max_position_pct must be positive and within the portfolio strategy cap');
        }
        if (config.risk_multiplier <= 0 || config.risk_multiplier > 1.5) {
            throw new Error('strategies.' + strategy + '.risk_multiplier must be between 0 and 1.5');
        }
        if (config.hard_stop_pct <= 0 || config.hard_stop_pct > 0.5
            || config.trailing_stop_pct <= 0 || config.trailing_stop_pct > 0.5
            || config.trailing_activation_pct <= 0 || config.trailing_activation_pct > 0.5) {
            throw new Error('strategies.' + strategy + ' stop settings must be between 0 and 0.5');
        }
        for (const key of ['max_entries_per_day', 'max_positions', 'minimum_hold_minutes', 'cooldown_minutes']) {
            if (!Number.isInteger(config[key]) || config[key] < 0) {
                throw new Error('strategies.' + strategy + '.' + key + ' must be a non-negative integer');
            }
        }
        for (const [regime, weight] of Object.entries(config.regime_weights)) {
            if (!Number.isFinite(weight) || weight < 0 || weight > 1.5) {
                throw new Error('strategies.' + strategy + '.regime_weights.' + regime + ' must be between 0 and 1.5');
            }
        }
    }
    const eventConfidence = settings.strategies.swing_event_driven.minimum_confidence;
    if (!Number.isInteger(eventConfidence) || eventConfidence < 0 || eventConfidence > 100) {
        throw new Error('strategies.swing_event_driven.minimum_confidence must be an integer between 0 and 100');
    }
    for (const [name, value] of Object.entries(settings.portfolio.regime_gross_caps)) {
        if (!Number.isFinite(value) || value <= 0 || value > portfolio.max_gross_pct) {
            throw new Error('portfolio.regime_gross_caps.' + name + ' must be within the gross exposure cap');
        }
    }
    for (const [name, value] of Object.entries(settings.portfolio.sector_caps)) {
        if (!Number.isFinite(value) || value <= 0 || value > portfolio.max_gross_pct) {
            throw new Error('portfolio.sector_caps.' + name + ' must be within the gross exposure cap');
        }
    }
    return settings;
}

async function loadSettings(root) {
    let defaults = FALLBACK_DEFAULT_SETTINGS;
    try {
        defaults = await readJson(path.join(root, 'portfolio', 'algo-strategy-config.default.json'));
    } catch (error) {
        if (error.code !== 'ENOENT') throw error;
    }
    const overrides = await readJson(path.join(root, 'data', 'algo-strategy-config.json'), { optional: true });
    return validateSettings(overrides || {}, defaults);
}

async function writeSettings(root, settings) {
    const dataDir = path.join(root, 'data');
    await fs.mkdir(dataDir, { recursive: true });
    const destination = path.join(dataDir, 'algo-strategy-config.json');
    const temporary = path.join(dataDir, `.algo-strategy-config.${process.pid}.${randomUUID()}.tmp`);
    try {
        const handle = await fs.open(temporary, 'wx', 0o600);
        try {
            await handle.writeFile(`${JSON.stringify(settings, null, 2)}\n`, 'utf8');
            await handle.sync();
        } finally {
            await handle.close();
        }
        await fs.rename(temporary, destination);
    } catch (error) {
        await fs.rm(temporary, { force: true });
        throw error;
    }
}

function isoTimestamp(value) {
    if (!value) return null;
    const text = String(value).trim().replace(' ', 'T');
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : text;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function snapshotView(snapshot) {
    return {
        date: String(snapshot.date || '').slice(0, 10),
        timestamp: isoTimestamp(snapshot.timestamp),
        snapshot_kind: typeof snapshot.snapshot_kind === 'string' ? snapshot.snapshot_kind : null,
        equity: finite(snapshot.equity),
        cash: finite(snapshot.cash),
        invested: finite(snapshot.invested),
        position_count: finite(snapshot.position_count),
        daily_pnl: finite(snapshot.daily_pnl),
        daily_pnl_pct: finite(snapshot.daily_pnl_pct),
    };
}

function calculateMetrics(history) {
    if (!history.length) {
        return { starting_equity: null, equity: null, total_pnl: null, total_return_pct: null, daily_pnl: null, daily_pnl_pct: null, max_drawdown_pct: null, sharpe_ratio: null };
    }
    const valid = history.filter(item => item.equity !== null && item.equity > 0);
    if (!valid.length) {
        return { starting_equity: null, equity: null, total_pnl: null, total_return_pct: null, daily_pnl: null, daily_pnl_pct: null, max_drawdown_pct: null, sharpe_ratio: null };
    }
    const first = valid[0];
    const latest = valid[valid.length - 1];
    let peak = valid[0].equity;
    let maxDrawdown = 0;
    for (const item of valid) {
        peak = Math.max(peak, item.equity);
        maxDrawdown = Math.min(maxDrawdown, (item.equity / peak - 1) * 100);
    }
    const daily = [];
    for (const item of valid) {
        const day = String(item.date || item.timestamp || '').slice(0, 10);
        if (daily.length && daily[daily.length - 1].day === day) daily[daily.length - 1] = { day, equity: item.equity };
        else daily.push({ day, equity: item.equity });
    }
    const returns = daily.slice(1).map((item, index) => (item.equity / daily[index].equity - 1) * 100).filter(Number.isFinite);
    let sharpe = null;
    if (returns.length >= 3) {
        const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
        const variance = returns.reduce((sum, value) => sum + (value - mean) ** 2, 0) / returns.length;
        if (variance > 0) sharpe = mean / Math.sqrt(variance) * Math.sqrt(252);
    }
    return {
        starting_equity: first.equity,
        equity: latest.equity,
        total_pnl: latest.equity - first.equity,
        total_return_pct: (latest.equity / first.equity - 1) * 100,
        daily_pnl: latest.daily_pnl,
        daily_pnl_pct: latest.daily_pnl_pct,
        max_drawdown_pct: maxDrawdown,
        sharpe_ratio: sharpe,
    };
}

async function readSnapshotHistory(dataDir, accountId) {
    const root = path.join(dataDir, 'snapshots');
    const directories = [root, path.join(root, 'intraday')];
    const files = [];
    for (const directory of directories) {
        let names;
        try { names = await fs.readdir(directory); }
        catch (error) { if (error.code === 'ENOENT') continue; throw error; }
        for (const name of names) {
            const isIntraday = directory !== root;
            const matches = isIntraday
                ? /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(name)
                : /^\d{4}-\d{2}-\d{2}.*\.json$/.test(name);
            if (matches) files.push({ path: path.join(directory, name), isIntraday });
        }
    }
    files.sort((left, right) => path.basename(left.path).localeCompare(path.basename(right.path)));
    const snapshotGroups = await Promise.all(files.slice(-740).map(async file => {
        try {
            return file.isIntraday ? await readJsonLines(file.path, 50) : [await readJson(file.path)];
        }
        catch { return []; }
    }));
    const snapshots = snapshotGroups.flat();
    const matching = snapshots.filter(item => item && item.trading_mode === 'algo' && (!accountId || item.account_id === accountId));
    return matching.map(snapshotView)
        .filter(item => item.date && item.equity !== null)
        .sort((a, b) => (a.timestamp || a.date).localeCompare(b.timestamp || b.date));
}

function summarizeResearch(report, fallbackDate = null) {
    if (!report) return null;
    const forward = safeObject(report.forward_validation);
    const portfolio = safeObject(safeObject(safeObject(report.fitness_timeline).current).portfolio);
    const fitnessTimeline = safeObject(report.fitness_timeline);
    const fitnessStrategies = safeObject(safeObject(fitnessTimeline.current).strategies);
    const comparison = safeObject(fitnessTimeline.comparison_to_previous);
    const strategyChanges = safeObject(comparison.strategy_changes);
    const orchestration = safeObject(report.orchestration);
    const adaptive = safeObject(report.adaptive_research);
    const factor = safeObject(report.factor_model);
    const promoted = Array.isArray(forward.promoted) ? forward.promoted.length : 0;
    const staged = Array.isArray(forward.staged_candidate_ids) ? forward.staged_candidate_ids.length : 0;
    const rejected = Array.isArray(forward.rejected_candidate_ids) ? forward.rejected_candidate_ids.length : 0;
    const factorOnly = orchestration.factor_only === true;
    const discovery = safeObject(adaptive.discovery);
    const verification = safeObject(adaptive.verification);
    return {
        date: String(report.run_date_et || report.created_at || fallbackDate || '').slice(0, 10),
        created_at: isoTimestamp(report.created_at),
        status: String(report.status || 'unknown'),
        active_trials: finite(forward.active_trial_count),
        promoted,
        staged,
        rejected,
        portfolio_return_pct: finite(portfolio.compounded_fold_return_pct),
        portfolio_status: String(portfolio.status || 'unavailable'),
        strategy_fitness: Object.entries(fitnessStrategies).map(([name, strategy]) => {
            const verification = safeObject(safeObject(strategy).verification);
            const change = safeObject(strategyChanges[name]);
            const delta = comparison.available === true && comparison.comparable === true
                ? finite(change.oos_fitness_delta)
                : null;
            return {
                name,
                fitness: finite(verification.fitness),
                delta_vs_previous: delta,
                previous_run_date: comparison.prior_run_date_et || null,
            };
        }),
        duration_seconds: finite(safeObject(report.stage_timings).total_seconds),
        factor_only: factorOnly,
        factor_model: factorOnly ? {
            family: String(factor.model_family || 'factor model'),
            observations: finite(factor.observation_count),
            rolling_evaluations: finite(factor.rolling_evaluation_count),
            training_samples: finite(factor.current_training_sample_count),
            forward_horizon_sessions: finite(factor.forward_horizon_sessions),
            fundamental_data_status: String(factor.fundamental_data_status || 'unavailable'),
        } : null,
        discovery_status: String(discovery.status || 'unavailable'),
        discovery_reason: String(discovery.reason || ''),
        verification_status: String(verification.status || 'unavailable'),
        verification_reason: String(verification.reason || ''),
        result: promoted ? `${promoted} promoted` : staged ? `${staged} staged` : factorOnly ? 'Factor-only model run' : String(report.status) === 'completed' ? 'No candidate promoted' : String(report.status || 'Unknown'),
    };
}

function summarizeOptimizer(report, fallbackId = '') {
    if (!report) return null;
    const data = safeObject(report.data);
    const adaptive = safeObject(report.adaptive_research);
    const discovery = safeObject(safeObject(adaptive.discovery).strategies);
    const strategies = Object.entries(discovery).map(([name, rawStrategy]) => {
        const strategy = safeObject(rawStrategy);
        const selected = safeObject(strategy.selected_candidate);
        const metrics = safeObject(selected.metrics);
        const attempts = Array.isArray(safeObject(strategy.window_search).attempts)
            ? strategy.window_search.attempts : [];
        const iterations = Array.isArray(strategy.search_iteration_summaries)
            ? strategy.search_iteration_summaries : [];
        const bestAttempt = attempts.reduce((best, attempt) => {
            if (!best || finite(attempt.selected_fitness) > finite(best.selected_fitness)) return attempt;
            return best;
        }, null);
        const bestIteration = iterations.reduce((best, iteration) => {
            if (!best || finite(iteration.selected_fitness) > finite(best.selected_fitness)) return iteration;
            return best;
        }, null);
        const best = bestAttempt || bestIteration;
        return {
            name,
            status: String(strategy.status || 'unknown'),
            archive_sequence: finite(strategy.archive_sequence),
            new_variants: finite(safeObject(strategy.adaptive_novelty).new_parameter_variants_on_this_data),
            search_iterations: finite(strategy.search_iterations),
            selection_status: String(strategy.selection_status || 'unavailable'),
            selected_candidate: String(selected.name || 'unavailable'),
            selected_eligible: selected.eligible === true,
            selected_return_pct: finite(metrics.net_return_pct),
            selected_closed_trades: finite(metrics.closed_trade_count),
            top_tested_candidate: String(best?.selected_candidate || ''),
            top_tested_fitness: finite(best?.selected_fitness),
            top_tested_delta: finite(best?.fitness_delta ?? best?.fitness_delta_vs_baseline),
            top_tested_window_days: finite(best?.window_days),
        };
    });
    return {
        cycle_id: String(report.invocation_id || fallbackId || report.run_id || ''),
        run_id: String(report.run_id || (report.invocation_id ? `optimization-${report.invocation_id}` : fallbackId ? `optimization-${fallbackId}` : '')),
        date: String(report.run_date_et || report.created_at || '').slice(0, 10),
        created_at: isoTimestamp(report.created_at),
        status: String(report.status || 'unknown'),
        duration_seconds: finite(safeObject(report.stage_timings).total_seconds),
        range_start: String(safeObject(data.strategy_research_range).start || data.range_start || ''),
        range_end: String(safeObject(data.strategy_research_range).end || data.research_data_end || ''),
        strategy_count: strategies.length,
        new_variants: strategies.reduce((total, strategy) => total + (strategy.new_variants || 0), 0),
        search_iterations: strategies.reduce((total, strategy) => total + (strategy.search_iterations || 0), 0),
        archive_sequence: strategies.reduce((latest, strategy) => Math.max(latest, strategy.archive_sequence || 0), 0),
        strategies,
    };
}

async function readOptimizer(dataDir) {
    const optimizerDir = path.join(dataDir, 'research', 'algo-auto', 'optimizer');
    const cyclesDir = path.join(optimizerDir, 'cycles');
    const entries = [];
    try {
        const names = await fs.readdir(cyclesDir);
        for (const name of names.filter(value => /^[a-f0-9]{32}\.json$/i.test(value))) {
            const reportPath = path.join(cyclesDir, name);
            try {
                const stat = await fs.stat(reportPath);
                entries.push({ name, reportPath, mtimeMs: stat.mtimeMs, size: stat.size });
            } catch { /* A cycle may be rotated while the directory is read. */ }
        }
    } catch (error) {
        if (error.code !== 'ENOENT') throw error;
    }
    entries.sort((a, b) => b.mtimeMs - a.mtimeMs);
    const history = [];
    for (const entry of entries.slice(0, 100)) {
        try {
            const cached = researchSummaryCache.get(entry.reportPath);
            const summary = cached && cached.mtimeMs === entry.mtimeMs && cached.size === entry.size
                ? cached.summary
                : summarizeOptimizer(await readJson(entry.reportPath), entry.name.slice(0, -5));
            if (!cached || cached.mtimeMs !== entry.mtimeMs || cached.size !== entry.size) {
                researchSummaryCache.set(entry.reportPath, {
                    mtimeMs: entry.mtimeMs, size: entry.size, summary,
                });
            }
            history.push(summary);
        } catch { /* One incomplete or corrupt report does not hide older cycles. */ }
    }
    const latestPath = path.join(optimizerDir, 'latest.json');
    try {
        const stat = await fs.stat(latestPath);
        const cached = researchSummaryCache.get(latestPath);
        const latest = cached && cached.mtimeMs === stat.mtimeMs && cached.size === stat.size
            ? cached.summary
            : summarizeOptimizer(await readJson(latestPath));
        if (!cached || cached.mtimeMs !== stat.mtimeMs || cached.size !== stat.size) {
            researchSummaryCache.set(latestPath, { mtimeMs: stat.mtimeMs, size: stat.size, summary: latest });
        }
        if (latest && !history.some(item => item.cycle_id && item.cycle_id === latest.cycle_id)) {
            history.push(latest);
        }
    } catch (error) { if (error.code !== 'ENOENT') { /* Ignore a malformed latest pointer. */ } }
    history.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    return { latest: history[0] || null, history };
}

async function readResearch(dataDir) {
    const directory = path.join(dataDir, 'research', 'algo-auto');
    let names = [];
    try { names = await fs.readdir(directory); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const dates = names.filter(name => /^\d{4}-\d{2}-\d{2}\.json$/.test(name)).sort().slice(-90);
    const history = [];
    for (const name of dates) {
        const reportPath = path.join(directory, name);
        try {
            const stat = await fs.stat(reportPath);
            const cached = researchSummaryCache.get(reportPath);
            if (cached && cached.mtimeMs === stat.mtimeMs && cached.size === stat.size) {
                history.push(cached.summary);
            } else {
                const summary = summarizeResearch(await readJson(reportPath), name.slice(0, 10));
                researchSummaryCache.set(reportPath, { mtimeMs: stat.mtimeMs, size: stat.size, summary });
                history.push(summary);
            }
        } catch { /* One corrupt archived report does not hide the rest. */ }
    }
    let latestReport = null;
    const latestPath = path.join(directory, 'latest.json');
    try {
        const stat = await fs.stat(latestPath);
        const cached = researchSummaryCache.get(latestPath);
        if (cached && cached.mtimeMs === stat.mtimeMs && cached.size === stat.size) latestReport = cached.summary;
        else {
            const report = await readJson(latestPath);
            latestReport = summarizeResearch(report);
            researchSummaryCache.set(latestPath, { mtimeMs: stat.mtimeMs, size: stat.size, summary: latestReport });
        }
    }
    catch (error) { if (error.code !== 'ENOENT') latestReport = null; }
    if (!latestReport && history.length) latestReport = history[history.length - 1] || null;
    return { latest: latestReport, history: history.reverse(), optimizer: await readOptimizer(dataDir) };
}

async function readJournal(dataDir, accountId) {
    const filePath = path.join(dataDir, 'trade_journal.jsonl');
    let handle;
    try { handle = await fs.open(filePath, 'r'); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    try {
        const stat = await handle.stat();
        const length = Math.min(stat.size, 2 * 1024 * 1024);
        const buffer = Buffer.alloc(length);
        await handle.read(buffer, 0, length, stat.size - length);
        let lines = buffer.toString('utf8').split(/\r?\n/);
        if (stat.size > length) lines = lines.slice(1);
        const rows = [];
        for (const line of lines.slice(-300)) {
            try {
                const row = JSON.parse(line);
                if (row && row.trading_mode === 'algo' && (!accountId || row.account_id === accountId)) rows.push(row);
            } catch { /* Ignore a partial or malformed append-only row. */ }
        }
        return rows;
    } finally {
        await handle.close();
    }
}

function activityView(state, journal, research) {
    const portfolio = safeObject(state.strategy_portfolio);
    const activities = [];
    for (const row of journal) {
        const ticker = String(row.ticker || row.symbol || '').toUpperCase();
        if (!ticker) continue;
        const action = String(row.action || row.side || 'order').toUpperCase();
        const status = String(row.status || row.broker_status || 'recorded').toLowerCase();
        const strategy = String(row.strategy || 'strategy');
        activities.push({
            at: isoTimestamp(row.timestamp),
            kind: 'order',
            title: `${action} order · ${ticker}`,
            detail: `${strategy} · ${status}${row.qty ? ` · ${row.qty} shares` : ''}`,
            status,
        });
    }
    const scanAt = isoTimestamp(portfolio.last_scan_at);
    if (scanAt) activities.push({
        at: scanAt,
        kind: 'scan',
        title: 'Portfolio scan',
        detail: `${portfolio.last_scan_status || 'status unavailable'} · ${Array.isArray(portfolio.last_signals) ? portfolio.last_signals.length : 0} signals · ${Array.isArray(portfolio.active_strategies) ? portfolio.active_strategies.length : 0} active strategies`,
        status: portfolio.entries_paused ? 'entries paused' : 'complete',
    });
    for (const signal of Array.isArray(portfolio.last_signals) ? portfolio.last_signals : []) {
        const ticker = String(signal.ticker || signal.symbol || '').toUpperCase();
        const side = String(signal.action || signal.side || signal.direction || 'signal').toUpperCase();
        activities.push({
            at: scanAt,
            kind: 'signal',
            title: `${side} signal${ticker ? ` · ${ticker}` : ''}`,
            detail: `${signal.strategy || 'Algo strategy'}${signal.reason || signal.reasoning ? ` · ${String(signal.reason || signal.reasoning).slice(0, 110)}` : ''}`,
            status: 'signal',
        });
    }
    if (research?.latest?.created_at) activities.push({
        at: research.latest.created_at,
        kind: 'research',
        title: 'Autoresearch completed',
        detail: `${research.latest.result} · ${research.latest.status}`,
        status: research.latest.status,
    });
    return activities.filter(item => item.at).sort((a, b) => b.at.localeCompare(a.at)).slice(0, 24);
}

async function buildPayload(root) {
    const dataDir = path.join(root, 'data');
    const state = await readJson(path.join(dataDir, 'algo_state.json'));
    if (!state.strategy_portfolio || typeof state.strategy_portfolio !== 'object') throw new Error('Algo coordinator state is incomplete');
    const portfolio = safeObject(state.strategy_portfolio);
    const settings = await loadSettings(root);
    const fillReport = cachedEquityFillReport(root, portfolio.account_id);
    const [history, journal, research] = await Promise.all([
        readSnapshotHistory(dataDir, portfolio.account_id),
        readJournal(dataDir, portfolio.account_id),
        readResearch(dataDir),
    ]);
    const strategyPerformance = equityStrategyPerformance(fillReport);
    const hasStrategyPnl = strategyPerformance.strategies.some(strategy => (
        strategy.latest_pnl_usd !== null || strategy.cumulative_pnl_usd !== null
    ));
    const fx = hasStrategyPnl ? await readUsdEurRate() : null;
    return {
        account: {
            mode: 'paper',
            market_state: String(state.market_state || 'UNKNOWN'),
            market_status: String(state.market_state || 'UNKNOWN'),
            regime: String(safeObject(portfolio.market_regime).regime || 'unknown'),
            volatility_state: String(safeObject(portfolio.market_regime).volatility_state || 'unknown'),
            regime_engine: String(safeObject(portfolio.market_regime).regime_engine || 'unknown'),
            last_scan_at: isoTimestamp(portfolio.last_scan_at),
            last_scan_status: String(portfolio.last_scan_status || 'unknown'),
            entries_paused: typeof portfolio.entries_paused === 'boolean' ? portfolio.entries_paused : null,
            entry_pause_reasons: Array.isArray(portfolio.entry_pause_reasons) ? portfolio.entry_pause_reasons.map(reason => String(reason).slice(0, 180)) : [],
            active_strategies: Array.isArray(portfolio.active_strategies) ? portfolio.active_strategies : [],
            universe_count: Array.isArray(safeObject(portfolio.universe).tickers) ? portfolio.universe.tickers.length : 0,
        },
        performance: { history, metrics: calculateMetrics(history) },
        strategy_performance: attachEuroAmounts(strategyPerformance, fx),
        activity: activityView(state, journal, research),
        research,
        settings,
    };
}

function mergeConfig(base, override) {
    const result = { ...base };
    for (const [key, value] of Object.entries(safeObject(override))) {
        result[key] = value && typeof value === 'object' && !Array.isArray(value)
            && base[key] && typeof base[key] === 'object' && !Array.isArray(base[key])
            ? mergeConfig(base[key], value)
            : value;
    }
    return result;
}

function validateConfigShape(value, template, prefix = '') {
    if (template === null) {
        if (value !== null && (typeof value !== 'number' || !Number.isFinite(value))) {
            throw new Error(`${prefix} must be null or a finite number`);
        }
        return;
    }
    if (template && typeof template === 'object' && !Array.isArray(template)) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${prefix || 'settings'} must be an object`);
        for (const [key, item] of Object.entries(value)) {
            if (!Object.hasOwn(template, key)) {
                if (prefix === 'dividends') {
                    if (typeof item !== 'number' || !Number.isFinite(item) || item < 0) throw new Error(`dividends.${key} must be a finite non-negative number`);
                    continue;
                }
                throw new Error(`Unsupported setting: ${prefix ? `${prefix}.` : ''}${key}`);
            }
            validateConfigShape(item, template[key], prefix ? `${prefix}.${key}` : key);
        }
        return;
    }
    if (Array.isArray(template)) {
        if (!Array.isArray(value) || value.some(item => typeof item !== typeof template[0])) throw new Error(`${prefix} must be an array of ${typeof template[0]}`);
        if (value.some(item => typeof item === 'number' && !Number.isFinite(item))) throw new Error(`${prefix} values must be finite`);
        return;
    }
    if (typeof template === 'number') {
        if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${prefix} must be a finite number`);
        return;
    }
    if (typeof value !== typeof template) throw new Error(`${prefix} must be ${typeof template}`);
}

function validateOptionsConfig(config) {
    const nonNegative = [
        ['risk', 'max_risk_per_trade_pct'], ['risk', 'max_total_defined_risk_pct'], ['risk', 'max_daily_loss_pct'],
        ['risk', 'max_delta_notional_pct'], ['risk', 'max_vega_per_vol_point_pct'], ['risk', 'max_gamma_1pct_loss_pct'],
        ['risk', 'max_theta_loss_per_day_pct'], ['risk', 'max_margin_pct'], ['vrp', 'min_iv_minus_rv'],
        ['vrp', 'short_delta_min'], ['vrp', 'short_delta_max'], ['vrp', 'wing_delta_max'],
        ['event_driven', 'min_move_edge_pct'], ['event_driven', 'min_llm_confidence'],
    ];
    for (const [section, key] of nonNegative) {
        if (config[section][key] < 0) throw new Error(`${section}.${key} must be non-negative`);
    }
    const maxIvToRealizedRatio = config.long_volatility?.max_iv_to_realized_ratio;
    if (maxIvToRealizedRatio !== undefined && maxIvToRealizedRatio !== null && maxIvToRealizedRatio <= 0) {
        throw new Error('long_volatility.max_iv_to_realized_ratio must be null or positive');
    }
    if (config.market_data_feed !== 'opra' && config.market_data_feed !== 'indicative') throw new Error('market_data_feed must be opra or indicative');
    if (config.risk.max_risk_per_trade_pct <= 0 || config.risk.max_contracts_per_trade < 1) throw new Error('risk budget and max_contracts_per_trade must be positive');
    if (config.vrp.short_delta_min > config.vrp.short_delta_max || config.vrp.max_dte < config.vrp.min_dte) throw new Error('VRP ranges are inverted');
    for (const [key, value] of Object.entries(config.regime_risk_multipliers || {})) {
        if (!Number.isFinite(value) || value < 0 || value > 2) throw new Error(`regime_risk_multipliers.${key} must be between 0 and 2`);
    }
    return config;
}

function optionsConfigPath(root) {
    const overridePath = process.env.ALPACA_OPTIONS_CONFIG;
    return overridePath ? path.resolve(root, overridePath) : path.join(root, 'data', 'options', 'config.json');
}

async function loadOptionsSettings(root) {
    const defaults = await readJson(path.join(root, 'options', 'config.default.json'));
    const override = await readJson(optionsConfigPath(root), { optional: true }) || {};
    const settings = mergeConfig(defaults, override);
    if (process.env.ALPACA_OPTIONS_FEED) settings.market_data_feed = process.env.ALPACA_OPTIONS_FEED.trim().toLowerCase();
    validateConfigShape(settings, defaults);
    return validateOptionsConfig(settings);
}

async function writeOptionsSettings(root, settings) {
    const defaults = await readJson(path.join(root, 'options', 'config.default.json'));
    const merged = mergeConfig(await loadOptionsSettings(root), settings);
    validateConfigShape(merged, defaults);
    validateOptionsConfig(merged);
    const destination = optionsConfigPath(root);
    await fs.mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
    const temporary = `${destination}.${process.pid}.${randomUUID()}.tmp`;
    try {
        const handle = await fs.open(temporary, 'wx', 0o600);
        try {
            await handle.writeFile(`${JSON.stringify(merged, null, 2)}\n`, 'utf8');
            await handle.sync();
        } finally {
            await handle.close();
        }
        await fs.rename(temporary, destination);
    } catch (error) {
        await fs.rm(temporary, { force: true });
        throw error;
    }
}

async function readJsonLines(filePath, limit = 500) {
    let handle;
    try { handle = await fs.open(filePath, 'r'); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    try {
        const stat = await handle.stat();
        const length = Math.min(stat.size, 2 * 1024 * 1024);
        const buffer = Buffer.alloc(length);
        await handle.read(buffer, 0, length, stat.size - length);
        let lines = buffer.toString('utf8').split(/\r?\n/);
        if (stat.size > length) lines = lines.slice(1);
        const rows = [];
        for (const line of lines.slice(-limit)) {
            try { const row = JSON.parse(line); if (row && typeof row === 'object' && !Array.isArray(row)) rows.push(row); }
            catch { /* Ignore a partial or malformed append-only row. */ }
        }
        return rows;
    } finally {
        await handle.close();
    }
}

async function optionsAccountDirectory(dataDir) {
    const accountId = String(process.env.ALPACA_OPTIONS_ACCOUNT_ID || '').trim();
    if (accountId) {
        const digest = createHash('sha256').update(accountId).digest('hex').slice(0, 16);
        return path.join(dataDir, 'options', 'accounts', digest);
    }
    const directory = path.join(dataDir, 'options', 'accounts');
    try {
        const names = (await fs.readdir(directory, { withFileTypes: true }))
            .filter(entry => entry.isDirectory() && /^[a-f0-9]{16}$/.test(entry.name))
            .map(entry => entry.name);
        return names.length === 1 ? path.join(directory, names[0]) : null;
    } catch (error) {
        if (error.code === 'ENOENT') return null;
        throw error;
    }
}

function summarizeOptionsResearch(report) {
    if (!report) return null;
    const strategies = Object.entries(safeObject(report.strategies)).map(([name, row]) => ({
        name,
        status: String(row.status || 'unknown'),
        approved: row.approved === true,
        underlying: String(row.underlying || ''),
        data_mode: String(row.data_mode || ''),
        walk_forward: safeObject(row.walk_forward),
        holdout: safeObject(row.holdout),
    }));
    const approved = strategies.filter(row => row.approved).length;
    const completed = strategies.filter(row => row.status === 'completed').length;
    const result = strategies.map(row => `${row.name.replaceAll('_', ' ')} · ${row.status}`).join(' / ')
        || String(report.reason || report.status || 'No options research results');
    return {
        date: String(report.run_date_et || report.created_at || '').slice(0, 10),
        created_at: isoTimestamp(report.created_at),
        status: String(report.status || 'unknown'),
        result,
        active_trials: strategies.length,
        promoted: approved,
        staged: Math.max(0, completed - approved),
        rejected: strategies.length - completed,
        portfolio_return_pct: null,
        portfolio_status: 'research only',
        options_strategies: strategies,
    };
}

async function readOptionsResearch(dataDir) {
    const directory = path.join(dataDir, 'options', 'research', 'automation');
    let names;
    try { names = await fs.readdir(directory); }
    catch (error) { if (error.code === 'ENOENT') return { latest: null, history: [] }; throw error; }
    const dated = names.filter(name => /^\d{4}-\d{2}-\d{2}\.json$/.test(name)).sort().slice(-90);
    const history = [];
    for (const name of dated) {
        try { history.push(summarizeOptionsResearch(await readJson(path.join(directory, name)))); }
        catch { /* One malformed research archive does not hide the rest. */ }
    }
    let latest = null;
    try { latest = summarizeOptionsResearch(await readJson(path.join(directory, 'latest.json'))); }
    catch { /* The latest report is optional until options research runs. */ }
    if (!latest && history.length) latest = history[history.length - 1];
    if (latest && !history.some(item => item?.date === latest.date)) history.push(latest);
    return { latest, history: history.filter(Boolean).sort((a, b) => b.date.localeCompare(a.date)) };
}

function optionsSnapshotView(row, previous) {
    const equity = finite(row.options_equity ?? safeObject(row.options_account_summary).options_equity);
    const previousEquity = finite(previous?.options_equity ?? safeObject(previous?.options_account_summary).options_equity);
    const dailyPnl = equity !== null && previousEquity !== null ? equity - previousEquity : null;
    return {
        date: String(row.logged_at || '').slice(0, 10),
        timestamp: isoTimestamp(row.logged_at),
        snapshot_kind: 'options',
        equity,
        cash: null,
        invested: null,
        position_count: Array.isArray(row.open_option_positions) ? row.open_option_positions.length : 0,
        daily_pnl: dailyPnl,
        daily_pnl_pct: dailyPnl !== null && previousEquity > 0 ? dailyPnl / previousEquity * 100 : null,
    };
}

function optionsStrategiesForRegime(settings, regime) {
    const currentRegime = String(regime || '').trim().toLowerCase();
    if (!currentRegime) return [];

    const globalWeights = safeObject(settings.regime_risk_multipliers);
    const regimeWeight = config => {
        const weights = { ...globalWeights, ...safeObject(safeObject(config).regime_risk_multipliers) };
        const value = finite(weights[currentRegime]);
        return value !== null && value > 0;
    };
    const strategies = [];
    const longVolatility = safeObject(settings.long_volatility);
    const exclusiveLongVolatility = longVolatility.enabled === true && longVolatility.mode === 'exclusive';

    if (!exclusiveLongVolatility && safeObject(settings.event_driven).enabled !== false
        && regimeWeight(settings.event_driven)) {
        strategies.push('Event Driven');
    }

    const allowedLongVolatilityRegimes = Array.isArray(longVolatility.allowed_regimes)
        ? longVolatility.allowed_regimes.map(value => String(value).toLowerCase())
        : ['volatile'];
    if (longVolatility.enabled !== false && allowedLongVolatilityRegimes.includes(currentRegime)
        && regimeWeight(longVolatility)) {
        strategies.push('Long Volatility');
    }

    const vrp = safeObject(settings.vrp);
    const allowedVrpRegimes = Array.isArray(vrp.allowed_regimes)
        ? vrp.allowed_regimes.map(value => String(value).toLowerCase())
        : [];
    if (!exclusiveLongVolatility && vrp.enabled !== false && allowedVrpRegimes.includes(currentRegime)
        && regimeWeight(vrp)) {
        strategies.push('Volatility Risk Premium');
    }
    return strategies;
}

function optionsActivityView(decisions, orders, fills, assignments, research) {
    const activities = [];
    for (const row of decisions) {
        // Cycle summaries are stored in the decisions journal too, but they do
        // not represent signals and have no approval decision to display.
        if (!row || !row.signal || typeof row.approved !== 'boolean') continue;
        const signal = safeObject(row.signal);
        const underlying = String(signal.underlying || '').toUpperCase();
        const strategy = String(signal.strategy || 'Options strategy').replaceAll('_', ' ');
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'signal',
            title: `${row.approved === true ? 'Approved' : 'Blocked'} options signal${underlying ? ` · ${underlying}` : ''}`,
            detail: `${strategy}${row.reason ? ` · ${String(row.reason).slice(0, 110)}` : ''}`,
            status: row.approved === true ? 'approved' : 'rejected',
        });
    }
    const finalOrderIntents = new Set(orders
        .filter(row => row?.client_order_id && row.broker_response)
        .map(row => String(row.client_order_id)));
    for (const row of orders) {
        if (row?.kind === 'cycle_summary') {
            const status = String(row.status || '').toLowerCase();
            const submitted = Number(row.submitted_count) || 0;
            activities.push({
                at: isoTimestamp(row.logged_at), kind: 'scan', title: 'Options scan',
                detail: `${row.candidate_count || 0} candidates · ${row.approved_count || 0} approved · ${row.submitted_count || 0} submitted`,
                status: submitted > 0 ? 'submitted' : status === 'cycle_no_order' ? 'no order' : 'order intent',
            });
            continue;
        }
        if (String(row?.status || '').toLowerCase() === 'submitting'
            && row.client_order_id && finalOrderIntents.has(String(row.client_order_id))) continue;
        const signal = safeObject(row.signal);
        const underlying = String(signal.underlying || '').toUpperCase();
        const status = String(row.status || safeObject(row.broker_response).status || 'recorded').toLowerCase();
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'order',
            title: `${status === 'dry_run' ? 'Paper order preview' : 'Options order'}${underlying ? ` · ${underlying}` : ''}`,
            detail: `${String(signal.strategy || 'Options strategy').replaceAll('_', ' ')} · ${status}`,
            status,
        });
    }
    const ordersByClientId = new Map(orders
        .filter(row => row?.client_order_id && row.signal)
        .map(row => [String(row.client_order_id), row]));
    for (const row of fills) {
        const order = ordersByClientId.get(String(row.client_order_id || '')) || {};
        const signal = safeObject(order.signal);
        const underlying = String(signal.underlying || '').toUpperCase();
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'order',
            title: `Options fill${underlying ? ` · ${underlying}` : ''}`,
            detail: `${String(signal.strategy || 'Options strategy').replaceAll('_', ' ')} · ${row.filled_qty || 0} contracts filled`,
            status: 'filled',
        });
    }
    for (const row of assignments) {
        const event = safeObject(row.broker_activity || row.simulation);
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'signal',
            title: 'Assignment activity',
            detail: String(event.type || event.symbol || 'Options assignment recorded').slice(0, 120),
            status: 'recorded',
        });
    }
    if (research?.latest?.created_at) activities.push({
        at: research.latest.created_at, kind: 'research', title: 'Options research completed',
        detail: `${research.latest.result} · ${research.latest.status}`, status: research.latest.status,
    });
    return activities.filter(item => item.at).sort((a, b) => b.at.localeCompare(a.at)).slice(0, 24);
}

async function buildOptionsPayload(root) {
    const dataDir = path.join(root, 'data');
    const settings = await loadOptionsSettings(root);
    const accountDir = await optionsAccountDirectory(dataDir);
    const [riskRows, decisions, orders, fills, strategyGreeks, assignments, research, coordinatorState] = await Promise.all([
        accountDir ? readJsonLines(path.join(accountDir, 'risk_daily.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'decisions.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'orders.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'fills.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'strategy_greeks.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'assignment_events.jsonl')) : [],
        readOptionsResearch(dataDir),
        readJson(path.join(dataDir, 'algo_state.json'), { optional: true }),
    ]);
    const chronologicalRiskRows = [...riskRows].sort((a, b) => String(a.logged_at || '').localeCompare(String(b.logged_at || '')));
    const history = [];
    let currentDay = '';
    let lastRowOfDay = null;
    let previousDayRow = null;
    for (const row of chronologicalRiskRows) {
        const day = String(row.logged_at || '').slice(0, 10);
        if (currentDay && day !== currentDay) previousDayRow = lastRowOfDay;
        if (day !== currentDay) currentDay = day;
        const point = optionsSnapshotView(row, previousDayRow);
        if (point.date && point.equity !== null) history.push(point);
        lastRowOfDay = row;
    }
    const latestRisk = riskRows[riskRows.length - 1] || {};
    const universe = [...new Set(decisions.slice(-50).map(row => safeObject(row.signal).underlying).filter(Boolean))];
    const disabled = !settings.enabled;
    const unconfigured = !accountDir;
    const coordinatorPortfolio = safeObject(coordinatorState?.strategy_portfolio);
    const sharedMarketRegime = safeObject(coordinatorPortfolio.market_regime);
    const sharedRegime = String(sharedMarketRegime.regime || 'unknown').trim().toLowerCase();
    const marketStatus = String(coordinatorState?.market_state || '').toUpperCase();
    const marketClosed = marketStatus === 'CLOSED';
    const entryPauseReasons = [];
    if (disabled) entryPauseReasons.push('Options paper route is disabled in options configuration');
    if (unconfigured) entryPauseReasons.push('No options paper account journal was found');
    if (marketClosed && !disabled && !unconfigured) entryPauseReasons.push('Options market is closed');
    if (sharedRegime === 'unknown' && !disabled && !unconfigured && !marketClosed) {
        entryPauseReasons.push('Shared Algo coordinator regime is unavailable');
    }
    const entriesPaused = disabled || unconfigured || marketClosed || sharedRegime === 'unknown';
    const activeStrategies = optionsStrategiesForRegime(settings, sharedRegime);
    const strategyPerformance = optionsStrategyPerformance(orders, fills, strategyGreeks, riskRows);
    const hasStrategyPnl = strategyPerformance.strategies.some(strategy => (
        strategy.latest_pnl_usd !== null || strategy.cumulative_pnl_usd !== null
    ));
    const fx = hasStrategyPnl ? await readUsdEurRate() : null;
    return {
        account: {
            mode: 'paper',
            market_state: marketStatus || 'UNKNOWN',
            market_status: marketStatus || 'UNKNOWN',
            regime: sharedRegime,
            volatility_state: String(sharedMarketRegime.volatility_state || 'unknown'),
            regime_engine: String(sharedMarketRegime.regime_engine || 'unknown'),
            last_scan_at: isoTimestamp(coordinatorPortfolio.last_scan_at),
            last_scan_status: String(coordinatorPortfolio.last_scan_status || 'unknown'),
            entries_paused: entriesPaused,
            entry_pause_reasons: entryPauseReasons,
            active_strategies: activeStrategies,
            active_strategies_label: 'Strategies available in this regime',
            universe_count: universe.length,
        },
        performance: { history, metrics: calculateMetrics(history) },
        strategy_performance: attachEuroAmounts(strategyPerformance, fx),
        activity: optionsActivityView(decisions, orders, fills, assignments, research),
        research,
        settings,
        settings_readonly_fields: process.env.ALPACA_OPTIONS_FEED ? ['market_data_feed'] : [],
        account_type: 'options',
    };
}

module.exports = function registerAlgoTraderRoutes({ app, root = process.env.ALGO_TRADER_ROOT || '/opt/algo-trader' }) {
    app.get('/api/algo-trader', async (req, res) => {
        const account = req.query?.account || 'equity';
        if (!['equity', 'options'].includes(account)) return res.status(400).json({ error: 'Unsupported Algo Trader account' });
        try { res.json(account === 'options' ? await buildOptionsPayload(root) : await buildPayload(root)); }
        catch (error) {
            console.error(`Algo Trader dashboard unavailable: ${error.message}`);
            res.status(503).json({ error: 'Algo Trader data is unavailable' });
        }
    });

    app.put('/api/algo-trader/config', async (req, res) => {
        const account = req.query?.account || req.body?.account || 'equity';
        if (!['equity', 'options'].includes(account)) return res.status(400).json({ error: 'Unsupported Algo Trader account' });
        try {
            if (account === 'options') {
                if (!req.body || !req.body.settings || typeof req.body.settings !== 'object' || Array.isArray(req.body.settings)) throw new Error('settings must be an object');
                await writeOptionsSettings(root, req.body.settings);
                res.json({ settings: await loadOptionsSettings(root), applied: 'next options paper scan' });
                return;
            }
            const current = await loadSettings(root);
            const body = safeObject(req.body);
            if (!req.body || !req.body.settings || typeof req.body.settings !== 'object' || Array.isArray(req.body.settings)) throw new Error('settings must be an object');
            const defaults = await readJson(path.join(root, 'portfolio', 'algo-strategy-config.default.json')).catch(error => {
                if (error.code !== 'ENOENT') throw error;
                return FALLBACK_DEFAULT_SETTINGS;
            });
            const settings = validateSettings(mergeConfig(current, body.settings), defaults);
            await writeSettings(root, settings);
            res.json({ settings, applied: 'next portfolio scan' });
        } catch (error) {
            const isValidationError = /must be|Unsupported setting|Unsupported Algo Trader account|inverted|positive|between/.test(error.message);
            if (!isValidationError) console.error(`Unable to save Algo Trader settings: ${error.message}`);
            res.status(isValidationError ? 400 : 503).json({ error: isValidationError ? error.message : 'Algo Trader settings could not be saved' });
        }
    });
};

module.exports.buildPayload = buildPayload;
module.exports.equityStrategyPerformance = equityStrategyPerformance;
module.exports.optionsStrategyPerformance = optionsStrategyPerformance;
module.exports.attachEuroAmounts = attachEuroAmounts;
