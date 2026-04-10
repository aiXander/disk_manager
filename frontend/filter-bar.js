import { AppState, debounce } from './app.js';

export function initFilterBar() {
    const bar = document.getElementById('filter-bar');
    if (!bar) return;

    bar.innerHTML = `
        <div class="filter-bar-inner">
            <div class="filter-group">
                <label>Extensions</label>
                <div class="ext-input-wrap">
                    <div class="ext-tags" id="ext-tags"></div>
                    <input type="text" id="ext-input" placeholder="e.g. .jpg" />
                </div>
                <label class="checkbox-label">
                    <input type="checkbox" id="ext-exclude" /> Exclude these
                </label>
            </div>
            <div class="filter-group">
                <label>Min Size</label>
                <div class="size-input-row">
                    <input type="number" id="min-size" min="0" placeholder="0" />
                    <select id="min-size-unit"><option value="KB">KB</option><option value="MB">MB</option><option value="GB">GB</option></select>
                </div>
                <label>Max Size</label>
                <div class="size-input-row">
                    <input type="number" id="max-size" min="0" placeholder="--" />
                    <select id="max-size-unit"><option value="KB">KB</option><option value="MB" selected>MB</option><option value="GB">GB</option></select>
                </div>
            </div>
        </div>
    `;

    const extInput = document.getElementById('ext-input');
    const extTags = document.getElementById('ext-tags');
    const extExclude = document.getElementById('ext-exclude');
    const minSize = document.getElementById('min-size');
    const minSizeUnit = document.getElementById('min-size-unit');
    const maxSize = document.getElementById('max-size');
    const maxSizeUnit = document.getElementById('max-size-unit');
    let enabled = false;

    function setEnabled(state) {
        enabled = state;
        bar.querySelectorAll('input, select, button').forEach((el) => {
            el.disabled = !state;
        });
    }
    setEnabled(false);

    document.addEventListener('scan-ready', () => setEnabled(true));

    // --- Unit to bytes multiplier ---
    function unitMultiplier(unit) {
        return unit === 'KB' ? 1024 : unit === 'MB' ? 1024 ** 2 : 1024 ** 3;
    }

    // --- Dispatch filter changes ---
    const emitChange = debounce(() => {
        if (!enabled) return;

        AppState.filters.extensions = [...AppState.filters.extensions]; // already kept in sync
        AppState.filters.excludeExtensions = extExclude.checked;

        const minV = parseFloat(minSize.value);
        AppState.filters.minSize = minV > 0 ? minV * unitMultiplier(minSizeUnit.value) : null;
        const maxV = parseFloat(maxSize.value);
        AppState.filters.maxSize = maxV > 0 ? maxV * unitMultiplier(maxSizeUnit.value) : null;

        document.dispatchEvent(new CustomEvent('filters-changed'));
    }, 300);

    // --- Extension tags ---
    function renderExtTags() {
        extTags.innerHTML = '';
        AppState.filters.extensions.forEach((ext) => {
            const tag = document.createElement('span');
            tag.className = 'ext-tag';
            tag.textContent = ext;
            const rm = document.createElement('button');
            rm.className = 'ext-tag-rm';
            rm.textContent = 'x';
            rm.addEventListener('click', () => {
                AppState.filters.extensions = AppState.filters.extensions.filter((e) => e !== ext);
                renderExtTags();
                emitChange();
            });
            tag.appendChild(rm);
            extTags.appendChild(tag);
        });
    }

    extInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            let val = extInput.value.trim().replace(/^\./, '');
            if (val && !AppState.filters.extensions.includes('.' + val)) {
                AppState.filters.extensions.push('.' + val);
                renderExtTags();
                emitChange();
            }
            extInput.value = '';
        }
    });

    extExclude.addEventListener('change', emitChange);

    // --- Size inputs ---
    minSize.addEventListener('input', emitChange);
    minSizeUnit.addEventListener('change', emitChange);
    maxSize.addEventListener('input', emitChange);
    maxSizeUnit.addEventListener('change', emitChange);

}
