
export function numberOrNull(value) {
    const number = Number(value);
    return value !== null && value !== undefined && value !== '' && Number.isFinite(number) ? number : null;
}

export function money(value, currency = 'USD', digits = 2) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return new Intl.NumberFormat(undefined, { style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(number);
}

export function signedMoney(value, currency = 'USD') {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${money(number, currency)}`;
}

export function signedPercent(value, digits = 2) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
}

export function signedScore(value) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${number.toFixed(4)}`;
}

export function dateTime(value, options = {}) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short', ...options }).format(date);
}

export function timeOnly(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(date);
}

export function dateOnly(value) {
    if (!value) return '';
    const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(date);
}

export function classForReturn(value) {
    const number = numberOrNull(value);
    return number === null || number === 0 ? '' : number > 0 ? 'up' : 'down';
}

export function statusClass(value) {
    const status = String(value || '').toLowerCase();
    if (['filled', 'complete', 'completed', 'ok', 'promoted', 'approved'].includes(status)) return 'promising';
    if (['failed', 'error', 'rejected', 'cancelled', 'canceled'].includes(status)) return 'rejected';
    if (['staged'].includes(status)) return 'staged';
    return '';
}

export function activityIcon(kind) {
    return ({ order: '⇄', scan: '◷', signal: '↗', research: '⌕' })[kind] || '·';
}
