// Resolve API calls from the document base so the app works both at `/` and
// behind a reverse proxy path such as `/openstock/`.
export const API_BASE = new URL('api', typeof document === 'undefined' ? 'http://localhost:3000/' : document.baseURI).pathname.replace(/\/$/, '');
export async function apiRequest(path, options = {}, onError = () => {}) {
    try {
        const response = await fetch(API_BASE + path, {
            ...options,
            cache: 'no-store',
            credentials: 'same-origin',
            headers: { Accept: 'application/json', ...(options.headers || {}) }
        });
        const data = await response.json();
        if (!response.ok) { onError(data.error || 'Error', data); return null; }
        return data;
    } catch (error) { onError(`Network: ${error.message}`); return null; }
}
