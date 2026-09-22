'use strict';

function normalizeOrderSizing(body, sizingPrice) {
    const hasQuantity = body.quantity !== undefined && body.quantity !== null && body.quantity !== '';
    const hasAmount = body.amount_eur !== undefined && body.amount_eur !== null && body.amount_eur !== '';
    if (hasQuantity === hasAmount) return { valid: false, error: 'Enter either a quantity or a euro value' };
    if (hasQuantity) {
        const quantity = Number(body.quantity);
        return Number.isInteger(quantity) && quantity > 0 ? { valid: true, quantity, sizing_mode: 'quantity', requested_amount_eur: null, sizing_price_eur: Number(sizingPrice) || null } : { valid: false, error: 'Quantity must be a positive whole number' };
    }
    const requestedAmount = Number(body.amount_eur), price = Number(sizingPrice);
    if (!(Number.isFinite(requestedAmount) && requestedAmount > 0)) return { valid: false, error: 'Euro value must be positive' };
    if (!(Number.isFinite(price) && price > 0)) return { valid: false, error: 'A current EUR price is required for euro-value sizing' };
    const quantity = Math.floor(requestedAmount / price);
    return quantity > 0 ? { valid: true, quantity, sizing_mode: 'amount', requested_amount_eur: requestedAmount, sizing_price_eur: price } : { valid: false, error: 'Euro value is below the price of one security' };
}

function hasValidOrderSizingInput(body) {
    const hasQuantity = body.quantity !== undefined && body.quantity !== null && body.quantity !== '';
    const hasAmount = body.amount_eur !== undefined && body.amount_eur !== null && body.amount_eur !== '';
    return hasQuantity !== hasAmount && (hasQuantity ? Number.isInteger(Number(body.quantity)) && Number(body.quantity) > 0 : Number.isFinite(Number(body.amount_eur)) && Number(body.amount_eur) > 0);
}

function failedGateNames(gates) { return Object.entries(gates).filter(([, passed]) => !passed).map(([name]) => name); }

async function fetchLiveOrderAuthority(client, { accountRef, isin, exchange }) {
    const [accounts, portfolio, quote] = await Promise.all([client.getAccounts(), client.getPortfolio(), client.getQuote({ isin, exchange })]);
    const account = Array.isArray(accounts) ? accounts.find(row => row?.account_ref_masked === accountRef) : null;
    const position = Array.isArray(portfolio) ? portfolio.find(row => row?.account_ref_masked === accountRef && String(row?.instrument || row?.isin || '').toUpperCase() === isin) : null;
    return { account, position, quote };
}

module.exports = { normalizeOrderSizing, hasValidOrderSizingInput, failedGateNames, fetchLiveOrderAuthority };
