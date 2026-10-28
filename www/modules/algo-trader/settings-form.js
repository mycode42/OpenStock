import { escapeHtml } from '../escape.js';

function setPath(target, path, value) {
    const keys = path.split('.');
    const finalKey = keys.pop();
    let current = target;
    for (const key of keys) current = current[key] ||= {};
    current[finalKey] = value;
}

function optionsSettingFields(settings, readonlyFields = []) {
    const fields = [];
    const visit = (value, path = '') => {
        if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length) {
            for (const [key, child] of Object.entries(value)) visit(child, path ? `${path}.${key}` : key);
            return;
        }
        fields.push({ path, value, json: Array.isArray(value) || (value && typeof value === 'object'), readonly: readonlyFields.includes(path) });
    };
    visit(settings);
    const groups = new Map();
    for (const field of fields) {
        const [section, ...rest] = field.path.split('.');
        const group = rest.length ? section : 'General';
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(field);
    }
    return [...groups.entries()].map(([group, rows]) => `<details class="algo-options-config-section" data-config-section="options:${escapeHtml(group)}"><summary>${escapeHtml(group.replaceAll('_', ' '))}</summary><div class="algo-options-config-fields">${rows.map(field => {
        const label = field.path.replaceAll('_', ' ').replaceAll('.', ' · ');
        const disabled = field.readonly ? 'disabled' : '';
        const input = typeof field.value === 'boolean'
            ? `<label class="algo-config-toggle"><input type="checkbox" data-setting-path="${escapeHtml(field.path)}" ${field.value ? 'checked' : ''} ${disabled}><span><strong>${escapeHtml(label)}</strong></span><i></i></label>`
            : field.json
                ? `<div class="algo-config-field"><label>${escapeHtml(label)}</label><textarea data-setting-path="${escapeHtml(field.path)}" rows="2" ${disabled}>${escapeHtml(JSON.stringify(field.value))}</textarea></div>`
                : field.path === 'market_data_feed'
                    ? `<div class="algo-config-field"><label>${escapeHtml(label)}</label><select data-setting-path="${escapeHtml(field.path)}" ${disabled}><option value="opra" ${field.value === 'opra' ? 'selected' : ''}>OPRA</option><option value="indicative" ${field.value === 'indicative' ? 'selected' : ''}>Indicative</option></select></div>`
                    : `<div class="algo-config-field"><label>${escapeHtml(label)}</label><input data-setting-path="${escapeHtml(field.path)}" type="${typeof field.value === 'number' || field.value === null ? 'number' : 'text'}" ${typeof field.value === 'number' || field.value === null ? 'step="any"' : ''} ${field.value === null ? 'data-nullable="true" placeholder="Optional — leave blank to disable"' : ''} value="${escapeHtml(field.value ?? '')}" ${disabled}></div>`;
        return input;
    }).join('')}</div></details>`).join('');
}

function equitySettingSections(settings) {
    const fractionPath = path => path.endsWith('_pct');
    const humanize = path => path.includes('.regime_weights.')
        ? path.split('.').at(-1).replaceAll('_', ' ') + ' route and risk weight'
        : path === 'strategies.swing_llm_driven.minimum_confidence'
            ? 'minimum event confidence'
        : path.split('.').at(-1).replaceAll('_', ' ');
    const valueInput = (path, value) => {
        const label = humanize(path);
        const eventConfidence = path === 'strategies.swing_llm_driven.minimum_confidence';
        if (typeof value === 'boolean') {
            return '<label class="algo-config-toggle"><input type="checkbox" data-setting-path="' + escapeHtml(path) + '"' + (value ? ' checked' : '') + '><span><strong>' + escapeHtml(label) + '</strong></span><i></i></label>';
        }
        if (Array.isArray(value) || (value && typeof value === 'object')) {
            return '<div class="algo-config-field"><label>' + escapeHtml(label) + '</label><textarea data-setting-path="' + escapeHtml(path) + '" rows="2">' + escapeHtml(JSON.stringify(value)) + '</textarea></div>';
        }
        if (typeof value === 'number') {
            const percentage = fractionPath(path);
            const display = percentage ? value * 100 : value;
            return '<div class="algo-config-field"><label>' + escapeHtml(label) + (percentage || eventConfidence ? ' · %' : '') + '</label><input data-setting-path="' + escapeHtml(path) + '" type="number" step="' + (eventConfidence ? '1' : 'any') + '"' + (percentage ? ' data-percent="true"' : '') + ' value="' + escapeHtml(display) + '"></div>';
        }
        const choices = path === 'general.regime_engine'
            ? ['committee', 'rule', 'hmm', 'gmm']
            : path === 'general.regime_feature_set'
                ? ['core', 'technical', 'macro', 'marketwide']
                : null;
        if (choices) {
            return '<div class="algo-config-field"><label>' + escapeHtml(label) + '</label><select data-setting-path="' + escapeHtml(path) + '">' + choices.map(choice => '<option value="' + choice + '"' + (choice === value ? ' selected' : '') + '>' + escapeHtml(choice === 'committee' ? 'committee · rule + HMM + GMM' : choice) + '</option>').join('') + '</select></div>';
        }
        const structural = path.endsWith('.horizon') || path.endsWith('.bar_interval');
        return '<div class="algo-config-field"><label>' + escapeHtml(label) + '</label><input data-setting-path="' + escapeHtml(path) + '" type="text" value="' + escapeHtml(value) + '"' + (structural ? ' disabled' : '') + '></div>';
    };
    const fields = (value, prefix) => Object.entries(value || {}).map(([key, child]) => {
        const path = prefix ? prefix + '.' + key : key;
        return child && typeof child === 'object' && !Array.isArray(child)
            ? fields(child, path)
            : valueInput(path, child);
    }).join('');
    const sections = [
        ['general', settings.general || {}],
        ['portfolio', settings.portfolio || {}],
    ].map(([name, value]) => '<details class="algo-options-config-section" data-config-section="equity:' + escapeHtml(name) + '"><summary>' + escapeHtml(name) + '</summary><div class="algo-options-config-fields">' + fields(value, name) + '</div></details>');
    const strategies = Object.entries(settings.strategies || {}).map(([name, value]) => '<details class="algo-options-config-section" data-config-section="equity:strategy:' + escapeHtml(name) + '"><summary>' + escapeHtml(name.replaceAll('_', ' ')) + '</summary><div class="algo-options-config-fields">' + fields(value, 'strategies.' + name) + '</div></details>').join('');
    sections.push('<details class="algo-options-config-section" data-config-section="equity:strategies"><summary>Strategies</summary><div class="algo-options-config">' + strategies + '</div></details>');
    return sections.join('');
}

export function renderSettingsForm({ settings, mode, readonlyFields = [], notice, saving }) {
    if (mode === 'options') {
        return `<form id="algo-config-form" class="algo-config-form" data-account="options">
            <div class="algo-options-config">${optionsSettingFields(settings, readonlyFields)}</div>
            <div class="algo-config-actions"><p class="algo-save-notice" aria-live="polite">${escapeHtml(notice)}</p><button type="submit" class="btn btn-primary algo-save-button" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : 'Save options settings'} <span>→</span></button></div>
        </form>`;
    }
    return `<form id="algo-config-form" class="algo-config-form" data-account="equity">
        <p class="algo-field-help">A regime weight of zero disables that route; a positive weight enables it and scales its risk. Position percentages are ceilings, further limited by global exposure, sector, cash, and trade risk rules.</p>
        <div class="algo-options-config">${equitySettingSections(settings)}</div>
        <div class="algo-config-actions"><p class="algo-save-notice" aria-live="polite">${escapeHtml(notice)}</p><button type="submit" class="btn btn-primary algo-save-button" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : 'Save strategy configuration'} <span>→</span></button></div>
    </form>`;
}

export function readSettingsFromForm(form, source) {
    const settings = JSON.parse(JSON.stringify(source || {}));
    for (const input of form.querySelectorAll('[data-setting-path]')) {
        let value;
        if (input.type === 'checkbox') value = input.checked;
        else if (input.tagName === 'TEXTAREA') value = JSON.parse(input.value);
        else if (input.type === 'number') {
            value = input.dataset.nullable === 'true' && input.value.trim() === '' ? null : Number(input.value);
            if (input.dataset.percent === 'true' && value !== null) value /= 100;
        }
        else value = input.value;
        setPath(settings, input.dataset.settingPath, value);
    }
    return settings;
}
