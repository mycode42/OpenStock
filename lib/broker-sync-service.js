'use strict';

function normalizedBrokerRows(raw, kind, sanitizeCollection, observedAt = new Date().toISOString()) {
    const rows = Array.isArray(raw) && !raw.some(row => row?.amount && typeof row.amount === 'object') ? raw : sanitizeCollection(raw, kind);
    return rows.map(row => {
        const base = { broker: 'trade_republic', observed_at: row.observed_at || observedAt, raw_hash: null };
        if (kind === 'portfolio') return { ...base, account_ref_masked: row.account_ref_masked || '••••', isin: row.isin || row.instrument || null, quantity: row.quantity, currency: row.currency || 'EUR', value_eur: row.value_eur ?? row.amount_eur ?? null, external_ref_masked: row.external_ref_masked || '••••' };
        return { ...base, external_ref_masked: row.external_ref_masked || '••••', status: row.status || 'UNKNOWN', isin: row.isin || row.instrument || null, side: row.side || null, quantity: row.quantity ?? null, amount_eur: row.amount_eur ?? null, occurred_at: row.occurred_at || null, execution_identity: row.execution_identity || null, execution_price_eur: row.execution_price_eur ?? null, execution_fees_eur: row.execution_fees_eur ?? null, executed_at: row.executed_at || null, title: row.title || null, subtitle: row.subtitle || null };
    });
}

module.exports = { normalizedBrokerRows };
