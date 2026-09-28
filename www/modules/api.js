// The dashboard may be served at a history-fallback path such as
// `/stock-monitor/dashboard/`.  A relative `./api` would then incorrectly
// resolve to `/stock-monitor/dashboard/api`.  Resolve all API calls from the
// document base, which is explicitly `/stock-monitor/` in index.html.
export const API_BASE = new URL('api', document.baseURI).pathname.replace(/\/$/, '');
let csrfToken = null;

export function setCsrfToken(token) { csrfToken = token || null; }

export async function apiRequest(path, options = {}, onError = () => {}) {
    try {
        const response = await fetch(API_BASE + path, {
            ...options,
            cache: 'no-store',
            credentials: 'same-origin',
            headers: { Accept: 'application/json', ...(csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase()) ? { 'X-CSRF-Token': csrfToken } : {}), ...(options.headers || {}) }
        });
        const data = await response.json();
        if (!response.ok) { onError(data.error || 'Error', data); return null; }
        return data;
    } catch (error) { onError(`Network: ${error.message}`); return null; }
}
