// Fixed 1D window for every chart: 04:00 to 20:00 New York time, the extended session.
// Defined in New York time so it stays aligned with US clock changes.
const ZONE = 'America/New_York';
const OPEN_HOUR = 4;
const SESSION_HOURS = 16;
const TICK_HOURS = [4, 8, 12, 16, 20];
const HOUR_MS = 3_600_000;

const zoneFormat = new Intl.DateTimeFormat('en-US', {
    timeZone: ZONE, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
});

// New York wall-clock parts of an instant.
function zoneParts(date) {
    const parts = Object.fromEntries(zoneFormat.formatToParts(date).map(part => [part.type, part.value]));
    return { year: Number(parts.year), month: Number(parts.month), day: Number(parts.day), hour: Number(parts.hour), minute: Number(parts.minute) };
}

// The instant at which New York shows the given wall-clock hour on the given date.
function zoneInstant(year, month, day, hour) {
    const wall = Date.UTC(year, month - 1, day, hour);
    let instant = wall;
    for (let attempt = 0; attempt < 2; attempt += 1) {
        const parts = zoneParts(new Date(instant));
        instant += wall - Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute);
    }
    return new Date(instant);
}

export function sessionWindow(now = new Date()) {
    const today = zoneParts(now);
    let start = zoneInstant(today.year, today.month, today.day, OPEN_HOUR);
    if (start > now) {
        const previous = new Date(Date.UTC(today.year, today.month - 1, today.day - 1));
        start = zoneInstant(previous.getUTCFullYear(), previous.getUTCMonth() + 1, previous.getUTCDate(), OPEN_HOUR);
    }
    return { start, end: new Date(start.getTime() + SESSION_HOURS * HOUR_MS) };
}

// 0 at 04:00, 1 at 20:00; times outside the window are clamped to the edges.
export function sessionFraction(timestamp, { start, end }) {
    const time = new Date(timestamp).getTime();
    return Math.min(1, Math.max(0, (time - start.getTime()) / (end.getTime() - start.getTime())));
}

// On-the-hour axis ticks: 04:00, 08:00, 12:00, 16:00 and 20:00 New York time.
export function sessionTicks({ start, end }) {
    const day = zoneParts(start);
    return TICK_HOURS.map(hour => {
        const time = zoneInstant(day.year, day.month, day.day, hour);
        return { fraction: sessionFraction(time, { start, end }), time };
    });
}

// Axis label for a session time, shown in New York time.
export function formatSessionTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('en-GB', { timeZone: ZONE, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date);
}
