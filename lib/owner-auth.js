const crypto = require('node:crypto');

const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const OWNER_SESSION_TTL_MS = 24 * 60 * 60 * 1000;

function constantTimeEqual(left, right) {
    const a = Buffer.from(String(left || ''));
    const b = Buffer.from(String(right || ''));
    return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function parseCookies(header = '') {
    return Object.fromEntries(String(header).split(';').map(part => part.trim()).filter(Boolean).map(part => {
        const index = part.indexOf('=');
        return index < 0 ? [part, ''] : [part.slice(0, index), decodeURIComponent(part.slice(index + 1))];
    }));
}

function appendCookie(res, value) {
    const existing = res.getHeader('Set-Cookie');
    res.setHeader('Set-Cookie', existing ? [...(Array.isArray(existing) ? existing : [existing]), value] : value);
}

function sameOrigin(req) {
    if (!req.headers.origin) return true;
    try { return new URL(req.headers.origin).host === req.headers.host; }
    catch { return false; }
}

function createOwnerAuth({ ownerToken, now = () => Date.now(), sessionTtlMs = OWNER_SESSION_TTL_MS } = {}) {
    if (!ownerToken || String(ownerToken).length < 32) return null;
    const sessions = new Map();
    const cookieName = 'openstock_owner';
    const cookieOptions = `Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${Math.floor(sessionTtlMs / 1000)}`;
    const expiredCookie = 'Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0';

    function getSession(req) {
        const id = parseCookies(req.headers.cookie)[cookieName];
        const session = id && sessions.get(id);
        if (!session || session.expiresAt <= now()) { if (id) sessions.delete(id); return null; }
        return { id, ...session };
    }

    function login(req, res) {
        if (!sameOrigin(req)) return res.status(403).json({ error: 'Same-origin login required' });
        if (!constantTimeEqual(req.body?.token, ownerToken)) return res.status(401).json({ error: 'Invalid owner credential' });
        const id = crypto.randomBytes(32).toString('base64url');
        const csrfToken = crypto.randomBytes(32).toString('base64url');
        sessions.set(id, { csrfToken, expiresAt: now() + sessionTtlMs });
        appendCookie(res, `${cookieName}=${id}; ${cookieOptions}`);
        return res.status(200).json({ authenticated: true, csrfToken, expiresAt: new Date(now() + sessionTtlMs).toISOString() });
    }

    function requireOwner(req, res, next) {
        const session = getSession(req);
        if (!session) return res.status(401).json({ error: 'Owner authentication required', code: 'OWNER_AUTH_REQUIRED' });
        if (MUTATING_METHODS.has(req.method)) {
            if (!sameOrigin(req)) return res.status(403).json({ error: 'Same-origin mutation required', code: 'ORIGIN_REQUIRED' });
            if (!constantTimeEqual(req.headers['x-csrf-token'], session.csrfToken)) return res.status(403).json({ error: 'Valid CSRF token required', code: 'CSRF_REQUIRED' });
        }
        req.ownerSession = session;
        next();
    }

    function logout(req, res) {
        if (req.ownerSession?.id) sessions.delete(req.ownerSession.id);
        appendCookie(res, `${cookieName}=; ${expiredCookie}`);
        return res.status(204).end();
    }

    return { login, logout, requireOwner, getSession };
}

module.exports = { createOwnerAuth, sameOrigin };
