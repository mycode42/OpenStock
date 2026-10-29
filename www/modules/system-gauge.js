import { apiRequest } from './api.js';
import { escapeHtml } from './escape.js';

// Top-bar load gauge: CPU and RAM at a glance, and an htop-style panel with per-core bars and top processes.
const POLL_MS = 2000;
let latest = null;
let open = false;
let timer = null;
let started = false;

function tone(percent) {
    if (percent === null || percent === undefined) return 'sys-muted';
    if (percent >= 85) return 'sys-high';
    if (percent >= 60) return 'sys-warn';
    return 'sys-ok';
}

function bar(percent, width = 'wide') {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    return `<span class="sys-bar sys-bar-${width} ${tone(value)}"><span style="width:${value}%"></span></span>`;
}

function gb(mb) {
    return (Number(mb || 0) / 1024).toFixed(1);
}

function uptimeText(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return days ? `${days}d ${hours}h` : `${hours}h ${minutes}m`;
}

function renderCompact(data) {
    const cpu = data.cpu.overall;
    const ram = data.memory.percent;
    return `<button type="button" class="sys-toggle" aria-expanded="${open}" aria-controls="sys-panel" title="Host load">
        <span class="sys-label">CPU</span>${bar(cpu, 'mini')}<span class="sys-value">${Math.round(cpu)}%</span>
        <span class="sys-label">RAM</span>${bar(ram, 'mini')}<span class="sys-value">${gb(data.memory.used_mb)}/${gb(data.memory.total_mb)} GB</span>
    </button>`;
}

function renderPanel(data) {
    const cores = data.cpu.cores.map((value, index) => `<div class="sys-core"><span>${index}</span>${bar(value, 'core')}<span class="sys-value">${Math.round(value)}%</span></div>`).join('');
    const zram = data.zram
        ? `<div class="sys-row"><span class="sys-label">zram</span>${gb(data.zram.data_mb)} GB stored in ${gb(data.zram.compressed_mb)} GB RAM${data.zram.ratio ? ` (${data.zram.ratio}:1)` : ''}</div>`
        : '';
    const research = data.research
        ? `<div class="sys-row sys-research"><span class="sys-label">Research</span>${escapeHtml(data.research.strategy)} · ${escapeHtml(data.research.id)} · ${data.research.processes} processes · ${gb(data.research.rss_mb)} GB · ${Math.round(data.research.cpu_percent)}% CPU</div>`
        : '<div class="sys-row"><span class="sys-label">Research</span><span class="sys-muted">no job running</span></div>';
    const rows = data.processes.map(item => `<tr><td>${escapeHtml(item.name)}</td><td>${item.pid}</td><td>${item.cpu_percent.toFixed(1)}</td><td>${Math.round(item.rss_mb)}</td></tr>`).join('');
    return `<div class="sys-panel-inner">
        <div class="sys-grid">
            <div><div class="sys-section">CPU · ${data.cpu.cores.length} cores</div><div class="sys-cores">${cores}</div></div>
            <div>
                <div class="sys-section">Memory</div>
                <div class="sys-row"><span class="sys-label">RAM</span>${bar(data.memory.percent)}<span class="sys-value">${gb(data.memory.used_mb)} / ${gb(data.memory.total_mb)} GB</span></div>
                <div class="sys-row"><span class="sys-label">Swap</span>${bar(data.swap.percent)}<span class="sys-value">${gb(data.swap.used_mb)} / ${gb(data.swap.total_mb)} GB</span></div>
                ${zram}
                <div class="sys-row"><span class="sys-label">Load</span>${data.load.join(' · ')} <span class="sys-muted">up ${uptimeText(data.uptime_seconds)}</span></div>
                ${research}
            </div>
        </div>
        <div class="sys-section">Top processes by CPU</div>
        <table class="sys-table"><thead><tr><th>Name</th><th>PID</th><th>CPU %</th><th>RSS MB</th></tr></thead><tbody>${rows}</tbody></table>
    </div>`;
}

function render(element) {
    if (!latest) {
        element.innerHTML = '<span class="sys-muted">load…</span>';
        return;
    }
    element.innerHTML = `${renderCompact(latest)}<div id="sys-panel" class="sys-panel" ${open ? '' : 'hidden'}>${renderPanel(latest)}</div>`;
}

// Draws into whatever #system-gauge is on the page now, so a lab re-render never leaves it stale.
export function renderSystemGauge() {
    const element = document.getElementById('system-gauge');
    if (element) render(element);
}

async function poll() {
    if (timer) window.clearTimeout(timer);
    if (!document.hidden) {
        const data = await apiRequest('/system/load');
        if (data) latest = data;
        renderSystemGauge();
    }
    timer = window.setTimeout(poll, POLL_MS);
}

// Starts once; the toggle and visibility handling are delegated to the document.
export function startSystemGauge() {
    if (started) return;
    started = true;
    document.addEventListener('click', event => {
        if (!event.target.closest?.('#system-gauge .sys-toggle')) return;
        open = !open;
        renderSystemGauge();
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) poll();
    });
    poll();
}
