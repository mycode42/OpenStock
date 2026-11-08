'use strict';

// A small fixed-window limiter per client address. In memory, so it resets on restart.
function createRateLimiter({ windowMs = 60_000, max = 240, now = () => Date.now() } = {}) {
    const windows = new Map();
    let lastSweep = now();
    return function rateLimit(req, res, next) {
        const at = now();
        if (at - lastSweep >= windowMs) {
            for (const [key, entry] of windows) if (entry.reset <= at) windows.delete(key);
            lastSweep = at;
        }
        const key = req.ip || req.socket?.remoteAddress || 'unknown';
        let entry = windows.get(key);
        if (!entry || entry.reset <= at) {
            entry = { count: 0, reset: at + windowMs };
            windows.set(key, entry);
        }
        entry.count += 1;
        if (entry.count > max) {
            res.set('Retry-After', String(Math.max(1, Math.ceil((entry.reset - at) / 1000))));
            return res.status(429).json({ error: 'Too many requests. Slow down and try again shortly.' });
        }
        return next();
    };
}

module.exports = { createRateLimiter };
