const path = require('node:path');
const { finite, isoTimestamp, runPythonJson, safeObject } = require('./io');

const strategyPnlCache = new Map();
let usdEurRateCache = null;
const STRATEGY_PNL_CACHE_MS = 90_000;
const USD_EUR_CACHE_MS = 60 * 60_000;
const OPTIONS_STRATEGY_IDS = Object.freeze([
    'options_vrp',
    'options_llm_driven',
    'options_directional_overlay',
    'options_long_volatility_sleeve',
]);
const OPTIONS_STRATEGY_ALIASES = Object.freeze({
    vrp: 'options_vrp',
    event_driven: 'options_llm_driven',
    options_event_driven: 'options_llm_driven',
    llm_driven: 'options_llm_driven',
    directional_overlay: 'options_directional_overlay',
    long_volatility: 'options_long_volatility_sleeve',
    options_long_volatility: 'options_long_volatility_sleeve',
});

function canonicalOptionsStrategyId(value) {
    const strategy = String(value || '').trim();
    return OPTIONS_STRATEGY_ALIASES[strategy] || strategy;
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

module.exports = {
    canonicalOptionsStrategyId,
    OPTIONS_STRATEGY_IDS,
    OPTIONS_STRATEGY_ALIASES,
    readEquityFillReport,
    cachedEquityFillReport,
    readUsdEurRate,
    usdEurRateCache,
    USD_EUR_CACHE_MS,
    attachEuroAmounts,
    equityStrategyPerformance,
    optionsStrategyPerformance,
    strategyPnlCache,
    STRATEGY_PNL_CACHE_MS,
};
