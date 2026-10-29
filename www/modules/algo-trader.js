import { renderAccountLoading, renderAccountPage } from './algo-trader/account-view.js';
import { renderResearchPage } from './algo-trader/research-view.js';
import { mountResearchLab } from './algo-trader/research-lab.js';
import { readSettingsFromForm as readFormSettings, renderSettingsForm } from './algo-trader/settings-form.js';

export function createAlgoTrader({ request, charts }) {
    const host = document.getElementById('page-algo-trader');
    const researchHost = document.getElementById('page-algo-research');
    const snapshots = { equity: null, options: null };
    const errors = { equity: '', options: '' };
    const loadingAccounts = new Set();
    const requestInFlight = new Set();
    const draftSettingsByAccount = { equity: null, options: null };
    const dirtyAccounts = new Set();
    const openConfigSections = { equity: new Set(), options: new Set() };
    const notices = { equity: '', options: '' };
    let accountMode = 'equity';
    let researchMode = 'equity';
    let saving = false;
    let range = '1D';
    const researchFilters = { equity: 'all', options: 'all' };
    const selectedRunDates = { equity: null, options: null };
    let activityPage = 0;
    let pollTimer = null;

    function readSettingsFromForm(form, mode = accountMode) {
        return readFormSettings(form, snapshots[mode]?.settings);
    }

    function rememberOpenConfigSections(mode) {
        openConfigSections[mode] = new Set([...host.querySelectorAll('details[data-config-section]')]
            .filter(section => section.open && section.dataset.configSection.startsWith(`${mode}:`))
            .map(section => section.dataset.configSection));
    }

    function restoreOpenConfigSections(mode) {
        const openSections = openConfigSections[mode] || new Set();
        for (const section of host.querySelectorAll('details[data-config-section]')) {
            if (openSections.has(section.dataset.configSection)) section.open = true;
        }
    }

    function render() {
        if (!host) return;
        const renderedMode = host.querySelector('#algo-config-form')?.dataset.account;
        if (renderedMode) rememberOpenConfigSections(renderedMode);
        const snapshot = snapshots[accountMode];
        if (!snapshot) {
            host.innerHTML = renderAccountLoading({ mode: accountMode, loading: loadingAccounts.has(accountMode), error: errors[accountMode] });
            return;
        }
        const settings = draftSettingsByAccount[accountMode] || snapshot.settings || {};
        const page = renderAccountPage({
            mode: accountMode,
            snapshot,
            settings,
            range,
            activityPage,
            charts,
            renderConfig: () => renderSettingsForm({
                settings,
                mode: accountMode,
                readonlyFields: snapshot.settings_readonly_fields || [],
                notice: notices[accountMode] || 'Changes apply during the next portfolio scan.',
                saving,
            }),
        });
        activityPage = page.activityPage;
        host.innerHTML = page.html;
        restoreOpenConfigSections(accountMode);
        charts.attachInteractiveLineCharts(host);
    }


    function renderResearch() {
        if (!researchHost) return;
        const view = renderResearchPage({
            mode: researchMode,
            snapshot: snapshots[researchMode],
            filter: researchFilters[researchMode],
            selectedDate: selectedRunDates[researchMode],
            loading: loadingAccounts.has(researchMode),
            error: errors[researchMode],
        });
        selectedRunDates[researchMode] = view.selectedDate;
        // The lab keeps its DOM node across the 15-second archive refresh, so typed values and the live log survive.
        const existingLab = researchHost.querySelector('#research-lab-host');
        researchHost.innerHTML = view.html;
        const slot = researchHost.querySelector('#research-lab-slot');
        const lab = existingLab || Object.assign(document.createElement('div'), { id: 'research-lab-host' });
        if (slot) {
            slot.replaceWith(lab);
            mountResearchLab(lab, researchMode);
        }
    }

    async function loadAccount(mode, { silent = false } = {}) {
        if (document.hidden) return;
        if (requestInFlight.has(mode)) return;
        requestInFlight.add(mode);
        if (!snapshots[mode] && !silent) loadingAccounts.add(mode);
        if (loadingAccounts.has(mode) && mode === accountMode) render();
        if (loadingAccounts.has(mode) && mode === researchMode) renderResearch();
        const data = await request(`/algo-trader?account=${mode}`, {}, () => {});
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
        snapshots[mode] = data;
        errors[mode] = '';
        if (!dirtyAccounts.has(mode)) draftSettingsByAccount[mode] = null;
        if (mode === accountMode && !dirtyAccounts.has(mode)) render();
        if (mode === researchMode) renderResearch();
    }

    function load({ silent = false } = {}) {
        return loadAccount(accountMode, { silent });
    }

    function loadResearch({ silent = false } = {}) {
        return loadAccount(researchMode, { silent });
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

    host?.addEventListener('input', event => {
        if (event.target.closest('#algo-config-form')) {
            const mode = event.target.form.dataset.account || accountMode;
            dirtyAccounts.add(mode);
            try {
                draftSettingsByAccount[mode] = readSettingsFromForm(event.target.form, mode);
            } catch { /* Keep malformed JSON visible so the user can correct it. */ }
        }
    });
    host?.addEventListener('change', event => {
        if (event.target.closest('#algo-config-form')) {
            const mode = event.target.form.dataset.account || accountMode;
            dirtyAccounts.add(mode);
            try {
                draftSettingsByAccount[mode] = readSettingsFromForm(event.target.form, mode);
            } catch { /* Keep malformed JSON visible so the user can correct it. */ }
        }
    });
    host?.addEventListener('click', event => {
        const button = event.target.closest('button');
        if (!button || !host.contains(button)) return;
        if (button.dataset.accountMode) {
            const mode = button.dataset.accountMode;
            if (!['equity', 'options'].includes(mode) || mode === accountMode) return;
            const currentForm = host.querySelector('#algo-config-form');
            if (currentForm && dirtyAccounts.has(accountMode)) {
                try { draftSettingsByAccount[accountMode] = readSettingsFromForm(currentForm, accountMode); }
                catch {
                    notices[accountMode] = 'Fix the JSON values before switching accounts.';
                    const notice = currentForm.querySelector('.algo-save-notice');
                    if (notice) notice.textContent = notices[accountMode];
                    return;
                }
            }
            accountMode = mode;
            errors[mode] = '';
            activityPage = 0;
            notices[mode] ||= '';
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
        } else if (button.dataset.action === 'open-config') {
            document.getElementById('algo-config')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
            renderResearch();
        } else if (button.dataset.runDate) {
            selectedRunDates[researchMode] = button.dataset.runDate;
            renderResearch();
        } else if (button.dataset.researchAction === 'retry') {
            errors[researchMode] = '';
            loadResearch();
        }
    });
    host?.addEventListener('submit', async event => {
        if (event.target.id !== 'algo-config-form') return;
        event.preventDefault();
        if (saving) return;
        const form = event.target;
        const mode = form.dataset.account || accountMode;
        try {
            draftSettingsByAccount[mode] = readSettingsFromForm(form, mode);
        } catch {
            notices[mode] = 'Fix the JSON values before saving settings.';
            const notice = form.querySelector('.algo-save-notice');
            if (notice) notice.textContent = notices[mode];
            return;
        }
        dirtyAccounts.add(mode);
        saving = true;
        notices[mode] = `Saving ${mode} settings…`;
        if (mode === accountMode) render();
        let saveError = '';
        const result = await request(`/algo-trader/config?account=${mode}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings: draftSettingsByAccount[mode] }) }, (message, data) => { saveError = data?.error || message || ''; });
        saving = false;
        if (!result?.settings) {
            notices[mode] = saveError ? `Save failed: ${saveError}` : 'Save failed. Check the values and retry.';
            if (mode === accountMode) render();
            return;
        }
        snapshots[mode].settings = result.settings;
        draftSettingsByAccount[mode] = null;
        dirtyAccounts.delete(mode);
        notices[mode] = `Saved · applies on the ${result.applied || 'next portfolio scan'}.`;
        if (mode === accountMode) render();
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && pollTimer) pollLoad?.({ silent: true });
    });

    return { load, render, loadResearch, renderResearch, startPolling, startResearchPolling, stopPolling };
}
