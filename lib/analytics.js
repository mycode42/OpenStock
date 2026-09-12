'use strict';

const DAY_MS = 24 * 60 * 60 * 1000;

function finite(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
}

function dateMs(value) {
    const ms = Date.parse(String(value || '').replace(' ', 'T') + (String(value || '').includes('Z') ? '' : 'Z'));
    return Number.isFinite(ms) ? ms : null;
}

function dayKey(value) {
    const ms = dateMs(value);
    return ms === null ? null : new Date(ms).toISOString().slice(0, 10);
}

function monthKey(value) {
    const day = dayKey(value);
    return day ? day.slice(0, 7) : null;
}

function bucketLast(points, bucketMs) {
    const buckets = new Map();
    for (const point of points) {
        const timestamp = dateMs(point.at);
        if (timestamp === null) continue;
        buckets.set(Math.floor(timestamp / bucketMs), point);
    }
    return [...buckets.values()].sort((a, b) => dateMs(a.at) - dateMs(b.at));
}

function snapshotBucketAt(value = new Date(), minutes = 15) {
    const size = minutes * 60 * 1000;
    const timestamp = value instanceof Date ? value.getTime() : dateMs(value);
    if (!Number.isFinite(timestamp)) throw new Error('Invalid snapshot date');
    return new Date(Math.floor(timestamp / size) * size).toISOString().slice(0, 19).replace('T', ' ');
}

function scaleEquityCurve(points, targetPoints = 120) {
    if (points.length <= targetPoints) return { points, resolution: 'snapshot' };
    const first = dateMs(points[0].at);
    const last = dateMs(points[points.length - 1].at);
    const duration = first === null || last === null ? 0 : Math.max(0, last - first);
    const options = [
        [15 * 60 * 1000, '15-minute'],
        [60 * 60 * 1000, 'hourly'],
        [4 * 60 * 60 * 1000, '4-hour'],
        [6 * 60 * 60 * 1000, '6-hour'],
        [DAY_MS, 'daily'],
        [7 * DAY_MS, 'weekly'],
        [30 * DAY_MS, 'monthly']
    ];
    const [bucketMs, resolution] = options.find(([size]) => duration / size <= targetPoints) || options[options.length - 1];
    const sampled = bucketLast(points, bucketMs);
    if (sampled[0] !== points[0]) sampled.unshift(points[0]);
    if (sampled[sampled.length - 1] !== points[points.length - 1]) sampled.push(points[points.length - 1]);
    return { points: sampled, resolution };
}

function signedCapitalEvent(event) {
    const amount = finite(event.amount_eur);
    return event.event_type === 'WITHDRAWAL' ? -amount : amount;
}

function calculateTrade(trade) {
    const quantity = finite(trade.quantity);
    const entry = finite(trade.entry_price_eur);
    const exit = finite(trade.exit_price_eur);
    const entryFees = finite(trade.entry_fee_eur);
    const exitFees = finite(trade.exit_fee_eur);
    const dividends = finite(trade.dividend_eur);
    const grossEntry = entry * quantity;
    const grossExit = exit * quantity;
    const closed = Boolean(trade.closed_at && exit > 0);
    const netPnl = closed ? grossExit + dividends - grossEntry - entryFees - exitFees : null;
    const returnPct = closed && grossEntry + entryFees > 0 ? (netPnl / (grossEntry + entryFees)) * 100 : null;
    const opened = dateMs(trade.opened_at);
    const closedAt = dateMs(trade.closed_at);
    return {
        ...trade,
        quantity,
        gross_entry_eur: grossEntry,
        gross_exit_eur: closed ? grossExit : null,
        dividend_eur: closed ? dividends : 0,
        net_pnl_eur: netPnl,
        return_pct: returnPct,
        holding_days: closed && opened !== null && closedAt !== null ? Math.max(0, (closedAt - opened) / DAY_MS) : null,
        is_closed: closed
    };
}

function calculateDashboard({ trades = [], capitalEvents = [], stocks = [], snapshots = [], now = new Date().toISOString() }) {
    const calculatedTrades = trades.map(calculateTrade);
    const completed = calculatedTrades.filter(trade => trade.is_closed);
    const open = calculatedTrades.filter(trade => !trade.is_closed);
    const gains = completed.filter(trade => trade.net_pnl_eur > 0);
    const losses = completed.filter(trade => trade.net_pnl_eur < 0);
    const realizedGain = gains.reduce((sum, trade) => sum + trade.net_pnl_eur, 0);
    const realizedLoss = losses.reduce((sum, trade) => sum + trade.net_pnl_eur, 0);
    const netRealized = realizedGain + realizedLoss;
    const totalDividends = completed.reduce((sum, trade) => sum + finite(trade.dividend_eur), 0);
    const totalFees = calculatedTrades.reduce((sum, trade) => sum + finite(trade.entry_fee_eur) + (trade.is_closed ? finite(trade.exit_fee_eur) : 0), 0);
    const stockById = new Map(stocks.map(stock => [Number(stock.id), stock]));
    const positions = open.map(trade => {
        const stock = stockById.get(Number(trade.stock_id));
        const price = finite(stock?.current_price_eur);
        const value = price > 0 ? price * trade.quantity : null;
        return { ...trade, current_price_eur: price || null, position_value_eur: value, unrealized_pnl_eur: value === null ? null : value - trade.gross_entry_eur - finite(trade.entry_fee_eur) };
    });
    const knownPositions = positions.filter(position => position.position_value_eur !== null);
    const positionValue = knownPositions.reduce((sum, position) => sum + position.position_value_eur, 0);
    const unknownPositions = positions.length - knownPositions.length;
    const capitalIn = capitalEvents.filter(event => ['INITIAL', 'DEPOSIT'].includes(event.event_type)).reduce((sum, event) => sum + finite(event.amount_eur), 0);
    const withdrawals = capitalEvents.filter(event => event.event_type === 'WITHDRAWAL').reduce((sum, event) => sum + finite(event.amount_eur), 0);
    const financing = capitalEvents.filter(event => event.event_type === 'FINANCING').reduce((sum, event) => sum + finite(event.amount_eur), 0);
    const netContributions = capitalIn - withdrawals;
    const ledgerCash = capitalEvents.reduce((sum, event) => sum + signedCapitalEvent(event), 0)
        - calculatedTrades.reduce((sum, trade) => sum + trade.gross_entry_eur + finite(trade.entry_fee_eur), 0)
        + completed.reduce((sum, trade) => sum + finite(trade.gross_exit_eur) - finite(trade.exit_fee_eur) + finite(trade.dividend_eur), 0);
    const equityAvailable = capitalEvents.length > 0 && unknownPositions === 0;
    const currentCapital = equityAvailable ? ledgerCash + positionValue : null;
    const unrealized = unknownPositions === 0 ? knownPositions.reduce((sum, position) => sum + position.unrealized_pnl_eur, 0) : null;
    const currentReturn = netContributions > 0 && currentCapital !== null ? ((currentCapital - netContributions) / netContributions) * 100 : null;
    const daily = completed.filter(trade => dayKey(trade.closed_at) === dayKey(now)).reduce((sum, trade) => sum + trade.net_pnl_eur, 0);
    const nowMs = dateMs(now) || Date.now();
    const weekly = completed.filter(trade => { const ms = dateMs(trade.closed_at); return ms !== null && ms >= nowMs - 7 * DAY_MS; }).reduce((sum, trade) => sum + trade.net_pnl_eur, 0);
    const monthly = completed.filter(trade => monthKey(trade.closed_at) === monthKey(now)).reduce((sum, trade) => sum + trade.net_pnl_eur, 0);
    const ytd = completed.filter(trade => dayKey(trade.closed_at)?.slice(0, 4) === new Date(nowMs).getUTCFullYear().toString()).reduce((sum, trade) => sum + trade.net_pnl_eur, 0);
    const closedByTime = [...completed].sort((a, b) => dateMs(a.closed_at) - dateMs(b.closed_at));
    let bestStreak = 0, worstStreak = 0, positiveStreak = 0, negativeStreak = 0;
    for (const trade of closedByTime) {
        if (trade.net_pnl_eur > 0) { positiveStreak++; negativeStreak = 0; }
        else if (trade.net_pnl_eur < 0) { negativeStreak++; positiveStreak = 0; }
        else { positiveStreak = 0; negativeStreak = 0; }
        bestStreak = Math.max(bestStreak, positiveStreak);
        worstStreak = Math.max(worstStreak, negativeStreak);
    }
    const monthlyPerformance = Object.values(completed.reduce((groups, trade) => {
        const month = monthKey(trade.closed_at);
        if (!month) return groups;
        groups[month] ||= { month, pnl_eur: 0, trades: 0, wins: 0 };
        groups[month].pnl_eur += trade.net_pnl_eur;
        groups[month].trades++;
        if (trade.net_pnl_eur > 0) groups[month].wins++;
        return groups;
    }, {})).sort((a, b) => a.month.localeCompare(b.month));
    const stockPerformance = Object.values(completed.reduce((groups, trade) => {
        const key = trade.symbol || `Stock #${trade.stock_id}`;
        groups[key] ||= { symbol: key, pnl_eur: 0, trades: 0, wins: 0, total_return_pct: 0, total_holding_days: 0 };
        groups[key].pnl_eur += trade.net_pnl_eur;
        groups[key].trades++;
        groups[key].wins += trade.net_pnl_eur > 0 ? 1 : 0;
        groups[key].total_return_pct += trade.return_pct;
        groups[key].total_holding_days += trade.holding_days;
        return groups;
    }, {})).map(row => ({
        ...row,
        win_rate_pct: (row.wins / row.trades) * 100,
        average_return_pct: row.total_return_pct / row.trades,
        average_holding_days: row.total_holding_days / row.trades
    })).sort((a, b) => b.pnl_eur - a.pnl_eur);
    const closedDays = new Set(completed.map(trade => dayKey(trade.closed_at)).filter(Boolean));
    const firstCapitalDate = capitalEvents.filter(event => ['INITIAL', 'DEPOSIT'].includes(event.event_type)).map(event => dateMs(event.occurred_at)).filter(Number.isFinite).sort((a, b) => a - b)[0];
    const elapsedDays = firstCapitalDate ? Math.max(0, (nowMs - firstCapitalDate) / DAY_MS) : null;
    const annualized = currentReturn !== null && elapsedDays >= 30 ? (Math.pow(1 + currentReturn / 100, 365.25 / elapsedDays) - 1) * 100 : null;
    const annualProfit = annualized !== null && currentCapital !== null ? currentCapital * annualized / 100 : null;
    const equityPoints = snapshots.map(snapshot => ({ at: snapshot.snapshot_at, equity_eur: finite(snapshot.equity_eur), cash_eur: finite(snapshot.cash_eur), position_value_eur: finite(snapshot.position_value_eur), net_contributions_eur: finite(snapshot.net_contributions_eur) })).sort((a, b) => dateMs(a.at) - dateMs(b.at));
    let peak = -Infinity, maxDrawdown = 0, currentDrawdown = null;
    if (equityPoints.length >= 2) {
        for (const point of equityPoints) {
            peak = Math.max(peak, point.equity_eur);
            if (peak > 0) maxDrawdown = Math.min(maxDrawdown, ((point.equity_eur - peak) / peak) * 100);
        }
        currentDrawdown = peak > 0 ? ((equityPoints[equityPoints.length - 1].equity_eur - peak) / peak) * 100 : null;
    }
    const dailyEquityPoints = bucketLast(equityPoints, DAY_MS);
    const dailyReturns = dailyEquityPoints.slice(1).map((point, index) => {
        const prior = dailyEquityPoints[index];
        return prior.equity_eur > 0 ? (point.equity_eur - prior.equity_eur) / prior.equity_eur : null;
    }).filter(Number.isFinite);
    let volatility = null, sharpe = null;
    if (dailyReturns.length >= 30) {
        const mean = dailyReturns.reduce((sum, value) => sum + value, 0) / dailyReturns.length;
        const variance = dailyReturns.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (dailyReturns.length - 1);
        const stdev = Math.sqrt(variance);
        volatility = stdev * Math.sqrt(252) * 100;
        sharpe = stdev > 0 ? (mean / stdev) * Math.sqrt(252) : null;
    }
    const scaledEquity = scaleEquityCurve(equityPoints);
    const exposure = currentCapital && currentCapital > 0 && unknownPositions === 0 ? (positionValue / currentCapital) * 100 : null;
    const cashPercent = currentCapital && currentCapital > 0 ? (ledgerCash / currentCapital) * 100 : null;
    return {
        as_of: now, positions, equity_available: equityAvailable, unknown_position_prices: unknownPositions,
        capital: { starting_eur: capitalEvents.find(event => event.event_type === 'INITIAL')?.amount_eur ?? null, current_eur: currentCapital, available_cash_eur: capitalEvents.length ? ledgerCash : null, invested_eur: unknownPositions ? null : positionValue, net_contributions_eur: netContributions || null, financing_eur: financing || null, current_return_pct: currentReturn, unrealized_pnl_eur: unrealized },
        realized: { gains_eur: realizedGain, losses_eur: realizedLoss, net_eur: netRealized, dividends_eur: totalDividends, total_fees_eur: totalFees },
        trades: { completed: completed.length, open: open.length, winning: gains.length, losing: losses.length, win_rate_pct: completed.length ? (gains.length / completed.length) * 100 : null, average_gain_eur: gains.length ? realizedGain / gains.length : null, average_loss_eur: losses.length ? realizedLoss / losses.length : null, average_return_pct: completed.length ? completed.reduce((sum, trade) => sum + trade.return_pct, 0) / completed.length : null, largest_win_eur: gains.length ? Math.max(...gains.map(trade => trade.net_pnl_eur)) : null, largest_loss_eur: losses.length ? Math.min(...losses.map(trade => trade.net_pnl_eur)) : null, average_holding_days: completed.length ? completed.reduce((sum, trade) => sum + trade.holding_days, 0) / completed.length : null, trades_per_month: elapsedDays && elapsedDays > 0 ? completed.length / (elapsedDays / 30.44) : null, average_profit_eur: gains.length ? realizedGain / gains.length : null, average_loss_eur: losses.length ? realizedLoss / losses.length : null, profit_factor: realizedLoss < 0 ? realizedGain / Math.abs(realizedLoss) : null, expectancy_eur: completed.length ? netRealized / completed.length : null, consecutive_wins: bestStreak, consecutive_losses: worstStreak },
        performance: { daily_pnl_eur: daily, weekly_pnl_eur: weekly, monthly_pnl_eur: monthly, ytd_pnl_eur: ytd, ytd_return_pct: firstCapitalDate && new Date(firstCapitalDate).getUTCFullYear() === new Date(nowMs).getUTCFullYear() ? currentReturn : null, annualized_return_pct: annualized, estimated_yearly_profit_eur: annualProfit, estimate_methodology: annualized === null ? 'Requires at least 30 days since the first capital event and a positive capital base.' : `Compounded observed return over ${elapsedDays.toFixed(0)} calendar days; this is an estimate, not a forecast.` },
        risk: { max_drawdown_pct: equityPoints.length >= 2 ? maxDrawdown : null, current_drawdown_pct: currentDrawdown, best_day_eur: closedDays.size ? Math.max(...Object.values(completed.reduce((g, t) => { const d = dayKey(t.closed_at); g[d] = (g[d] || 0) + t.net_pnl_eur; return g; }, {}))) : null, worst_day_eur: closedDays.size ? Math.min(...Object.values(completed.reduce((g, t) => { const d = dayKey(t.closed_at); g[d] = (g[d] || 0) + t.net_pnl_eur; return g; }, {}))) : null, best_month_eur: monthlyPerformance.length ? Math.max(...monthlyPerformance.map(row => row.pnl_eur)) : null, worst_month_eur: monthlyPerformance.length ? Math.min(...monthlyPerformance.map(row => row.pnl_eur)) : null, largest_position_eur: knownPositions.length ? Math.max(...knownPositions.map(position => position.position_value_eur)) : null, portfolio_exposure_pct: exposure, cash_pct: cashPercent, volatility_pct: volatility, sharpe_ratio: sharpe },
        equity_curve: scaledEquity.points, equity_curve_resolution: scaledEquity.resolution, monthly_performance: monthlyPerformance, stock_performance: stockPerformance, recent_trades: calculatedTrades.sort((a, b) => dateMs(b.closed_at || b.opened_at) - dateMs(a.closed_at || a.opened_at)).slice(0, 10)
    };
}

module.exports = { calculateTrade, calculateDashboard, signedCapitalEvent, bucketLast, scaleEquityCurve, snapshotBucketAt };
