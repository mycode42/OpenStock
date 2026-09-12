export function money(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(2) : '—';
}
export function parseOpenStockDate(value) {
    if (!value) return null;
    const text = String(value).trim();
    if (!text) return null;

    // SQLite returns `YYYY-MM-DD HH:MM:SS`; convert it to explicit UTC ISO first.
    const sqliteMatch = text.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.\d+)?$/);
    if (sqliteMatch) {
        return new Date(`${sqliteMatch[1]}T${sqliteMatch[2]}Z`);
    }

    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
}

export function formatLocalDate(value) {
    const date = parseOpenStockDate(value);
    if (!date) return '—';
    return date.toLocaleString('en-GB', {
        timeZone: 'Europe/Berlin',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        timeZoneName: 'short',
        hour12: false
    });
}

export function formatLocalDateTime(value) {
    const date = parseOpenStockDate(value);
    if (!date) return 'Never updated';
    return date.toLocaleString('en-GB', {
        timeZone: 'Europe/Berlin',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        timeZoneName: 'short',
        hour12: false
    });
}

export function formatChartDate(value) {
    const date = parseOpenStockDate(value);
    if (!date) return '—';
    return date.toLocaleDateString('en-GB', {
        timeZone: 'Europe/Berlin', month: 'short', day: 'numeric'
    });
}

export function chartRangeLabel(range) {
    return { '1d': '1-day', '1mo': '1-month', '1y': '1-year', '5y': '5-year' }[range] || range;
}

