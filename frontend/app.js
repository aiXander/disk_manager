// AppState — single source of truth
const AppState = {
    currentScanId: null,
    filters: {
        extensions: [],
        excludeExtensions: false,
        mtimeAfterDays: null,
        mtimeBeforeDays: null,
        atimeAfterDays: null,
        minSize: null,
        maxSize: null,
        isDir: null,
    },
    topN: 25,
    viewMode: 'treemap', // 'treemap' | 'topn'
    stagedItems: [],
    treeZoomPath: null,
    previewPath: null,
    config: {},
};

export { AppState };

// API helper
export async function api(path, options = {}) {
    const resp = await fetch(`/api/v1${path}`, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
}

// Format bytes to human-readable
export function formatBytes(bytes) {
    if (bytes == null || isNaN(bytes)) return '0 B';
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const k = 1024;
    const i = Math.floor(Math.log(Math.abs(bytes)) / Math.log(k));
    const idx = Math.min(i, units.length - 1);
    return `${(bytes / Math.pow(k, idx)).toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
}

// Format unix timestamp to locale string
export function formatDate(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

// Debounce helper
export function debounce(fn, ms) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), ms);
    };
}

// Boot: load config, load scan list, init all panels
async function boot() {
    try {
        AppState.config = await api('/config');
        AppState.topN = AppState.config.top_n || 25;
    } catch (e) {
        console.warn('Failed to load config, using defaults:', e.message);
    }

    const { initScanPanel } = await import('./scan-panel.js');
    const { initFilterBar } = await import('./filter-bar.js');
    const { initMainView } = await import('./main-view.js');
    const { initPreviewPanel } = await import('./preview-panel.js');
    const { initStagingPanel } = await import('./staging-panel.js');
    const { initConfigPanel } = await import('./config-panel.js');

    initScanPanel();
    initFilterBar();
    initMainView();
    initPreviewPanel();
    initStagingPanel();
    initConfigPanel();
}

document.addEventListener('DOMContentLoaded', boot);
