import { escapeHtml } from './escape.js';

const STORAGE_KEY = 'openstock.theme';
const MODE_STORAGE_KEY = 'openstock.colorMode';
const DEFAULT_THEME = 'openstock';
const DEFAULT_MODE = 'system';
const COLOR_MODES = ['system', 'light', 'dark'];
const THEMES = [
    {
        id: 'openstock',
        name: 'OpenStock',
        description: 'The familiar charcoal interface with a chartreuse accent.',
        colors: ['#050606', '#111514', '#c8ff00', '#d7e0da'],
        browserColor: '#050606',
    },
    {
        id: 'claw',
        name: 'Claw',
        description: 'Deep graphite, pine surfaces, and a lively green highlight.',
        colors: ['#07100c', '#13241a', '#b7f76b', '#e7f4ea'],
        browserColor: '#07100c',
    },
    {
        id: 'knot',
        name: 'Knot',
        description: 'High-contrast black surfaces, crisp lettering, and a focused crimson accent.',
        colors: ['#07080b', '#f5f5f7', '#e22643', '#32343d'],
        browserColor: '#07080b',
    },
    {
        id: 'dash',
        name: 'Dash',
        description: 'Quiet charcoal with stone neutrals and a warm amber highlight.',
        colors: ['#11100e', '#272522', '#e0a35f', '#f4eee4'],
        browserColor: '#11100e',
    },
    {
        id: 'absolutely',
        name: 'Absolutely',
        description: 'Bold charcoal contrast with a bright coral-orange accent.',
        colors: ['#0b0c0e', '#202226', '#ff815e', '#ffffff'],
        browserColor: '#0b0c0e',
    },
    {
        id: 'tide',
        name: 'Tide',
        description: 'Blue-black panels with cool sea-glass and cyan accents.',
        colors: ['#07121b', '#102431', '#56d9d1', '#e4f6f6'],
        browserColor: '#07121b',
    },
    {
        id: 'beacon',
        name: 'Beacon',
        description: 'Night-slate surfaces with a warm, clear amber accent.',
        colors: ['#11100d', '#211c14', '#f4bd58', '#f5eee0'],
        browserColor: '#11100d',
    },
    {
        id: 'phosphor',
        name: 'Phosphor',
        description: 'A restrained terminal palette with bright signal green.',
        colors: ['#050b08', '#0b1711', '#4ade80', '#dcfce7'],
        browserColor: '#050b08',
    },
    {
        id: 'crt',
        name: 'CRT',
        description: 'A sharp black console with bright white text and gold signals.',
        colors: ['#030303', '#151515', '#ffd166', '#ffffff'],
        browserColor: '#030303',
    },
    {
        id: 'miami',
        name: 'Miami',
        description: 'Midnight indigo with electric cyan and coral details.',
        colors: ['#100c24', '#1b1534', '#58e1ff', '#f6efff'],
        browserColor: '#100c24',
    },
    {
        id: 'manuscript',
        name: 'Manuscript',
        description: 'Warm paper, ink-like text, and a lapis-blue accent.',
        colors: ['#f4eedf', '#fff9eb', '#3459a1', '#33271e'],
        browserColor: '#f4eedf',
    },
    {
        id: 'rose',
        name: 'Rosé',
        description: 'Deep plum panels with a clear rose accent and soft warm neutrals.',
        colors: ['#160f18', '#281925', '#ed82b0', '#f8eaf0'],
        browserColor: '#160f18',
    },
];

export function createSettings() {
    const host = document.getElementById('page-settings');
    let selectedTheme = DEFAULT_THEME;
    let selectedMode = DEFAULT_MODE;

    function readStoredTheme() {
        try {
            const stored = window.localStorage.getItem(STORAGE_KEY);
            return THEMES.some(theme => theme.id === stored) ? stored : DEFAULT_THEME;
        } catch {
            return DEFAULT_THEME;
        }
    }

    function readStoredMode() {
        try {
            const stored = window.localStorage.getItem(MODE_STORAGE_KEY);
            return COLOR_MODES.includes(stored) ? stored : DEFAULT_MODE;
        } catch {
            return DEFAULT_MODE;
        }
    }

    function effectiveMode(mode) {
        if (mode !== 'system') return mode;
        return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }

    function applyAppearance({ themeId = selectedTheme, mode = selectedMode, saveTheme = false, saveMode = false } = {}) {
        const theme = THEMES.find(item => item.id === themeId) || THEMES[0];
        selectedTheme = theme.id;
        selectedMode = COLOR_MODES.includes(mode) ? mode : DEFAULT_MODE;
        document.documentElement.dataset.theme = theme.id;
        document.documentElement.dataset.colorMode = selectedMode;
        document.documentElement.dataset.themeMode = effectiveMode(selectedMode);
        document.documentElement.style.colorScheme = document.documentElement.dataset.themeMode;
        const browserColor = getComputedStyle(document.documentElement).getPropertyValue('--os-page').trim() || theme.browserColor;
        document.querySelector('meta[name="theme-color"]')?.setAttribute('content', browserColor);
        try {
            if (saveTheme) window.localStorage.setItem(STORAGE_KEY, theme.id);
            if (saveMode) window.localStorage.setItem(MODE_STORAGE_KEY, selectedMode);
        } catch {
            // Keep the selected appearance for this page if storage is unavailable.
        }
    }

    function render() {
        if (!host) return;
        selectedTheme = document.documentElement.dataset.theme || selectedTheme;
        selectedMode = document.documentElement.dataset.colorMode || selectedMode;
        host.innerHTML = `<main class="settings-page">
            <header class="settings-page-heading"><div><span class="settings-eyebrow">OPENSTOCK PREFERENCES</span><h1>Website settings</h1><p>Choose a color theme for this browser. Your selection is saved locally.</p></div></header>
            <section class="card settings-appearance-panel" aria-labelledby="settings-appearance-title">
                <div class="settings-panel-heading"><div><span class="settings-eyebrow">APPEARANCE</span><h2 id="settings-appearance-title">Choose a theme</h2><p>Theme changes apply across the dashboard, journal, configuration, and Algo Trader pages.</p></div></div>
                <div class="settings-theme-grid" role="group" aria-label="Color theme">
                    ${THEMES.map(theme => `<button type="button" class="settings-theme-option ${theme.id === selectedTheme ? 'selected' : ''}" data-theme-choice="${theme.id}" aria-pressed="${theme.id === selectedTheme}">
                        <span class="settings-theme-preview" data-preview-theme="${theme.id}" aria-hidden="true"><span class="settings-preview-top"><i></i><i></i><i></i></span><span class="settings-preview-content"><i></i><span><i></i><i></i><i></i></span></span></span>
                        <span class="settings-theme-copy"><span><strong>${escapeHtml(theme.name)}</strong>${theme.id === selectedTheme ? '<small class="settings-theme-current">Current</small>' : ''}</span><small>${escapeHtml(theme.description)}</small></span>
                        <span class="settings-theme-swatches" aria-hidden="true">${theme.colors.map(color => `<i style="--swatch:${color}"></i>`).join('')}</span>
                    </button>`).join('')}
                </div>
                <div class="settings-color-mode">
                    <div><strong>Color mode</strong><p>System follows your device appearance preference.</p></div>
                    <div class="settings-mode-switch" role="group" aria-label="Color mode">
                        ${[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']].map(([mode, label]) => `<button type="button" data-color-mode="${mode}" class="${mode === selectedMode ? 'selected' : ''}" aria-pressed="${mode === selectedMode}">${label}</button>`).join('')}
                    </div>
                </div>
            </section>
        </main>`;
    }

    host?.addEventListener('click', event => {
        const modeChoice = event.target.closest('[data-color-mode]');
        if (modeChoice && host.contains(modeChoice)) {
            applyAppearance({ mode: modeChoice.dataset.colorMode, saveMode: true });
            render();
            return;
        }
        const choice = event.target.closest('[data-theme-choice]');
        if (!choice || !host.contains(choice)) return;
        applyAppearance({ themeId: choice.dataset.themeChoice, saveTheme: true });
        render();
    });

    window.addEventListener('storage', event => {
        if (event.key !== STORAGE_KEY && event.key !== MODE_STORAGE_KEY && event.key !== null) return;
        applyAppearance({ themeId: readStoredTheme(), mode: readStoredMode() });
        if (!host?.classList.contains('hidden')) render();
    });

    window.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', () => {
        if (selectedMode !== 'system') return;
        applyAppearance();
        if (!host?.classList.contains('hidden')) render();
    });

    return {
        initialize() { applyAppearance({ themeId: readStoredTheme(), mode: readStoredMode() }); },
        render,
    };
}
