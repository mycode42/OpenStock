// The only HTML escaping helper used by browser templates.
export function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
}

export function escapeAttribute(value) { return escapeHtml(value); }
