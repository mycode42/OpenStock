import { apiRequest } from './api.js';
import { renderAccountLoading, renderAccountPage } from './algo-trader/account-view.js?v=3.5.0&config-removed=3';
import { renderResearchPage } from './algo-trader/research-view.js?v=3.5.0&research-control=2';
import { mountResearchLab } from './algo-trader/research-lab.js?v=3.5.0&equity-runs=30';
import { mountDataStatus } from './algo-trader/data-status.js?v=3.5.0&demo-samples=3';
import { renderMomentumPage } from './algo-trader/momentum-view.js?v=3.5.0&momentum=3';

export function createAlgoTrader({ request, charts }) {
    const host = document.getElementById('page-algo-trader');
    const researchHost = document.getElementById('page-algo-research');
    const snapshots = { equity: null, options: null };
    const errors = { equity: '', options: '' };
    const loadingAccounts = new Set();
    const requestInFlight = new Set();
    let accountMode = 'equity';
    // The momentum paper book has its own read-only view: its payload and loading state.
    const momentum = { payload: null, loading: false, error: '' };
    let researchMode = 'equity';
    let range = '1D';
    const researchFilters = { equity: 'all', options: 'all' };
    const selectedRunDates = { equity: null, options: null };
    const researchPages = { equity: 1, options: 1 };
    const ACCOUNT_TIMEOUT_MS = 45_000;
    let activityPage = 0;
    let pollTimer = null;

    function render() {
        if (!host) return;
        if (accountMode === 'momentum') {
            host.innerHTML = renderMomentumPage({ payload: momentum.payload, range, charts, loading: momentum.loading, error: momentum.error });
            charts.attachInteractiveLineCharts(host);
            return;
        }
        const snapshot = snapshots[accountMode];
        if (!snapshot) {
            host.innerHTML = renderAccountLoading({ mode: accountMode, loading: loadingAccounts.has(accountMode), error: errors[accountMode] });
            return;
        }
        const settings = snapshot.settings || {};
        const page = renderAccountPage({
            mode: accountMode,
            snapshot,
            settings,
            range,
            activityPage,
            charts,
        });
        activityPage = page.activityPage;
        host.innerHTML = page.html;
        charts.attachInteractiveLineCharts(host);
    }


    function renderResearch() {
        if (!researchHost) return;
        const view = renderResearchPage({
            mode: researchMode,
            snapshot: snapshots[researchMode],
            filter: researchFilters[researchMode],
            selectedDate: selectedRunDates[researchMode],
            page: researchPages[researchMode],
            loading: loadingAccounts.has(researchMode),
            error: errors[researchMode],
        });
        selectedRunDates[researchMode] = view.selectedDate;
        researchPages[researchMode] = view.page || 1;
        // Same markup as the last render: leave the DOM alone, so nothing flickers on the 15-second refresh.
        // The router replaces the host with its loading placeholder on navigation, so repaint when our markup is gone.
        if (researchHost.renderedHtml === view.html && researchHost.querySelector('.algo-research-page')) return;
        // The lab keeps its DOM node across the archive refresh, so typed values and the live log survive.
        const existingLab = researchHost.querySelector('#research-lab-host');
        const scroll = window.scrollY;
        researchHost.innerHTML = view.html;
        researchHost.renderedHtml = view.html;
        const statusHost = researchHost.querySelector('#research-data-status');
        if (statusHost) mountDataStatus(statusHost);
        const slot = researchHost.querySelector('#research-lab-slot');
        const lab = existingLab || Object.assign(document.createElement('div'), { id: 'research-lab-host' });
        if (slot) {
            slot.replaceWith(lab);
            mountResearchLab(lab, researchMode);
        }
        // Restore only after the lab is back in place: while it is missing the page is shorter, and the browser clamps the scroll.
        window.scrollTo(0, scroll);
    }

    async function loadAccount(mode, { silent = false } = {}) {
        if (document.hidden) return;
        if (requestInFlight.has(mode)) return;
        requestInFlight.add(mode);
        if (!snapshots[mode] && !silent) loadingAccounts.add(mode);
        if (loadingAccounts.has(mode) && mode === accountMode) render();
        if (loadingAccounts.has(mode) && mode === researchMode) renderResearch();
        // A stalled response must not hold the in-flight guard forever: the timeout ends the request and clears the loading state.
        const data = await request(`/algo-trader?account=${mode}`, { signal: AbortSignal.timeout(ACCOUNT_TIMEOUT_MS) }, () => {});
        requestInFlight.delete(mode);
        loadingAccounts.delete(mode);
        if (!data) {
            if (!snapshots[mode]) errors[mode] = 'Check that OpenStock can read this paper account and its Algo Trader data.';
            if (mode === accountMode) render();
            if (mode === researchMode) renderResearch();
            return;
        }
        if (mode === 'options' && data.account_type !== 'options') {
            snapshots.options = null;
            errors.options = 'OpenStock returned Equity data for this Options request. The running API needs the Options account route enabled.';
            if (mode === accountMode) render();
            if (mode === researchMode) renderResearch();
            return;
        }
        const previous = snapshots[mode];
        const previousError = errors[mode];
        snapshots[mode] = data;
        errors[mode] = '';
        if (mode === accountMode) render();
        // The research page reads only the research archive and the demo flag, so repaint only when those change.
        const researchChanged = !previous || previous.demo_mode !== data.demo_mode
            || JSON.stringify(previous.research) !== JSON.stringify(data.research) || previousError !== '';
        if (mode === researchMode && researchChanged) renderResearch();
    }

    async function loadMomentum() {
        momentum.loading = true;
        if (accountMode === 'momentum') render();
        const payload = await apiRequest('/algo-trader/momentum-paper', {}, message => { momentum.error = message; });
        momentum.loading = false;
        if (payload) { momentum.payload = payload; momentum.error = ''; }
        else if (!momentum.payload) momentum.error = 'The momentum paper view could not be loaded.';
        if (accountMode === 'momentum') render();
    }

    function load({ silent = false } = {}) {
        if (accountMode === 'momentum') return loadMomentum();
        return loadAccount(accountMode, { silent });
    }

    async function loadResearch({ silent = false } = {}) {
        await loadAccount(researchMode, { silent });
    }

    let pollLoad = null;
    function startPolling() {
        stopPolling();
        pollLoad = load;
        pollTimer = window.setInterval(() => pollLoad?.({ silent: true }), 15000);
    }

    function startResearchPolling() {
        stopPolling();
        pollLoad = loadResearch;
        pollTimer = window.setInterval(() => pollLoad?.({ silent: true }), 15000);
    }

    function stopPolling() {
        if (pollTimer) window.clearInterval(pollTimer);
        pollTimer = null;
        pollLoad = null;
    }

    host?.addEventListener('click', event => {
        const button = event.target.closest('button');
        if (!button || !host.contains(button)) return;
        if (button.dataset.accountMode) {
            const mode = button.dataset.accountMode;
            if (!['equity', 'options', 'momentum'].includes(mode) || mode === accountMode) return;
            accountMode = mode;
            errors[mode] = '';
            activityPage = 0;
            render();
            load({ silent: Boolean(snapshots[mode]) });
        } else if (button.dataset.range) {
            range = button.dataset.range;
            render();
        } else if (button.dataset.activityPage !== undefined) {
            const requestedPage = Number(button.dataset.activityPage);
            if (Number.isInteger(requestedPage)) {
                activityPage = requestedPage;
                render();
            }
        } else if (button.dataset.action === 'retry') {
            errors[accountMode] = '';
            load();
        }
    });
    researchHost?.addEventListener('click', event => {
        const button = event.target.closest('button');
        if (!button || !researchHost.contains(button)) return;
        if (button.dataset.researchMode) {
            const mode = button.dataset.researchMode;
            if (!['equity', 'options'].includes(mode) || mode === researchMode) return;
            researchMode = mode;
            errors[mode] = '';
            renderResearch();
            loadResearch({ silent: Boolean(snapshots[mode]) });
        } else if (button.dataset.researchFilter) {
            researchFilters[researchMode] = button.dataset.researchFilter;
            selectedRunDates[researchMode] = null;
            researchPages[researchMode] = 1;
            renderResearch();
        } else if (button.dataset.researchPage !== undefined) {
            const requestedPage = Number(button.dataset.researchPage);
            if (Number.isInteger(requestedPage) && requestedPage >= 1) {
                researchPages[researchMode] = requestedPage;
                renderResearch();
            }
        } else if (button.dataset.runDate) {
            selectedRunDates[researchMode] = button.dataset.runDate;
            renderResearch();
        } else if (button.dataset.researchAction === 'retry') {
            errors[researchMode] = '';
            loadResearch();
        }
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && pollTimer) pollLoad?.({ silent: true });
    });

    return { load, render, loadResearch, renderResearch, startPolling, startResearchPolling, stopPolling };
}
