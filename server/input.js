function alertLevelInput(body, existing = null) {
    if (existing?.trigger_mode === 'PERCENT') {
        const input = {};
        if (body.change_pct !== undefined)
            input.change_pct = numberField(body.change_pct, 'Percentage move', { minimum: Number.EPSILON });
        return input;
    }
    const side = String(body.side ?? existing?.side ?? '').toUpperCase();
    if (!['BUY', 'SELL'].includes(side)) throw new Error('Side must be BUY or SELL');
    return {
        side,
        // The API speaks USD; the stored column keeps its legacy name.
        price_eur: numberField(body.price_usd ?? existing?.price_eur, 'Alert price', { minimum: Number.EPSILON }),
    };
}

function booleanField(value, label) {
    if ([true, 1, '1', 'true'].includes(value)) return 1;
    if ([false, 0, '0', 'false'].includes(value)) return 0;
    throw new Error(`${label} must be boolean`);
}

function toDatabaseDate(value, fieldName) {
    if (!value || typeof value !== 'string') throw new Error(`${fieldName} is required`);
    const databaseMatch = value.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$/);
    if (databaseMatch) {
        if (Number.isNaN(new Date(`${databaseMatch[1]}T${databaseMatch[2]}Z`).getTime()))
            throw new Error(`${fieldName} must be a valid date/time`);
        return value;
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) throw new Error(`${fieldName} must be a valid date/time`);
    return date
        .toISOString()
        .replace('T', ' ')
        .replace(/\.\d{3}Z$/, '');
}

function numberField(value, fieldName, { minimum = 0, required = true } = {}) {
    if ((value === undefined || value === null || value === '') && !required) return 0;
    const number = Number(value);
    if (!Number.isFinite(number) || number < minimum)
        throw new Error(`${fieldName} must be ${minimum > 0 ? 'greater than zero' : 'zero or greater'}`);
    return number;
}

function normaliseTrade(body, existing = null) {
    const currency = String(body.currency ?? existing?.currency ?? 'EUR')
        .trim()
        .toUpperCase();
    if (!/^[A-Z]{3}$/.test(currency)) throw new Error('Currency must be a three-letter code');
    const entryPrice = numberField(body.entry_price ?? existing?.entry_price, 'Entry price', {
        minimum: Number.EPSILON,
    });
    const entryFxRate = numberField(
        body.entry_fx_rate ?? existing?.entry_fx_rate ?? (currency === 'EUR' ? 1 : undefined),
        'Entry EUR conversion rate',
        { minimum: Number.EPSILON },
    );
    const closeRequested = body.closed_at !== undefined ? body.closed_at : existing?.closed_at;
    const closedAt = closeRequested ? toDatabaseDate(closeRequested, 'Close date/time') : null;
    const openedAt = toDatabaseDate(body.opened_at ?? existing?.opened_at, 'Open date/time');
    if (closedAt && new Date(`${closedAt}Z`) < new Date(`${openedAt}Z`))
        throw new Error('Close date/time cannot be before open date/time');
    const exitPrice = closedAt
        ? numberField(body.exit_price ?? existing?.exit_price, 'Exit price', { minimum: Number.EPSILON })
        : null;
    const exitFxRate = closedAt
        ? numberField(
              body.exit_fx_rate ?? existing?.exit_fx_rate ?? (currency === 'EUR' ? 1 : undefined),
              'Exit EUR conversion rate',
              { minimum: Number.EPSILON },
          )
        : null;
    return {
        trade_id:
            existing?.trade_id ||
            String(body.trade_id || `OST-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`).trim(),
        stock_id: Number(body.stock_id ?? existing?.stock_id),
        opened_at: openedAt,
        closed_at: closedAt,
        quantity: numberField(body.quantity ?? existing?.quantity, 'Quantity', { minimum: Number.EPSILON }),
        currency,
        entry_price: entryPrice,
        entry_fx_rate: entryFxRate,
        entry_price_eur: entryPrice * entryFxRate,
        entry_fee_eur: numberField(body.entry_fee_eur ?? existing?.entry_fee_eur ?? 1, 'Entry fee'),
        exit_price: exitPrice,
        exit_fx_rate: exitFxRate,
        exit_price_eur: closedAt ? exitPrice * exitFxRate : null,
        exit_fee_eur: closedAt
            ? numberField(
                  body.exit_fee_eur ?? (existing?.closed_at ? existing.exit_fee_eur : undefined) ?? 1,
                  'Exit fee',
              )
            : 0,
        dividend_eur: closedAt
            ? numberField(body.dividend_eur ?? existing?.dividend_eur ?? 0, 'Dividends received')
            : 0,
        strategy: String(body.strategy ?? existing?.strategy ?? '').trim(),
        notes: String(body.notes ?? existing?.notes ?? '').trim(),
    };
}

module.exports = {
    alertLevelInput,
    booleanField,
    toDatabaseDate,
    numberField,
    normaliseTrade,
};
