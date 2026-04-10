import { AppState, api, debounce } from './app.js';

export function initConfigPanel() {
    // Create the modal if it doesn't exist
    let modal = document.getElementById('config-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'config-modal';
        modal.className = 'config-modal-overlay';
        modal.style.display = 'none';
        document.body.appendChild(modal);
    }

    modal.innerHTML = `
        <div class="config-modal-content">
            <div class="config-header">
                <h3>Settings</h3>
                <button class="btn btn-sm" id="config-close">X</button>
            </div>
            <div class="config-body">
                <div class="config-field">
                    <label for="config-topn">Top-N (1-100): <span id="config-topn-val">${AppState.topN}</span></label>
                    <input type="range" id="config-topn" min="1" max="100" value="${AppState.topN}" />
                </div>
                <div class="config-field">
                    <label for="config-max-depth">Max Depth</label>
                    <input type="number" id="config-max-depth" min="1" max="50" value="${AppState.config.max_depth || 10}" />
                </div>
                <div class="config-field">
                    <label>Skip Directories</label>
                    <div class="skip-dirs-list" id="skip-dirs-list"></div>
                    <div class="skip-dirs-add">
                        <input type="text" id="skip-dir-input" placeholder="/path/to/skip" />
                        <button class="btn btn-sm" id="skip-dir-add-btn">Add</button>
                    </div>
                </div>
                <div class="config-field">
                    <label for="config-page-size">Page Size</label>
                    <input type="number" id="config-page-size" min="10" max="1000" step="10" value="${AppState.config.page_size || 100}" />
                </div>
            </div>
        </div>
    `;

    const closeBtn = document.getElementById('config-close');
    const topnSlider = document.getElementById('config-topn');
    const topnVal = document.getElementById('config-topn-val');
    const maxDepthInput = document.getElementById('config-max-depth');
    const skipDirsList = document.getElementById('skip-dirs-list');
    const skipDirInput = document.getElementById('skip-dir-input');
    const skipDirAddBtn = document.getElementById('skip-dir-add-btn');
    const pageSizeInput = document.getElementById('config-page-size');

    let skipDirs = Array.isArray(AppState.config.skip_dirs) ? [...AppState.config.skip_dirs] : [];

    // Render skip dirs
    function renderSkipDirs() {
        skipDirsList.innerHTML = '';
        skipDirs.forEach((dir, idx) => {
            const row = document.createElement('div');
            row.className = 'skip-dir-row';
            row.innerHTML = `
                <span class="skip-dir-path">${escapeHtml(dir)}</span>
                <button class="btn btn-sm btn-danger skip-dir-rm" data-idx="${idx}">x</button>
            `;
            row.querySelector('.skip-dir-rm').addEventListener('click', () => {
                skipDirs.splice(idx, 1);
                renderSkipDirs();
                saveConfig();
            });
            skipDirsList.appendChild(row);
        });
    }
    renderSkipDirs();

    skipDirAddBtn.addEventListener('click', () => {
        const val = skipDirInput.value.trim();
        if (val && !skipDirs.includes(val)) {
            skipDirs.push(val);
            skipDirInput.value = '';
            renderSkipDirs();
            saveConfig();
        }
    });

    skipDirInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            skipDirAddBtn.click();
        }
    });

    // Save config with debounce
    const saveConfig = debounce(async () => {
        const patch = {
            top_n: parseInt(topnSlider.value, 10),
            max_depth: parseInt(maxDepthInput.value, 10),
            skip_dirs: skipDirs,
            page_size: parseInt(pageSizeInput.value, 10),
        };

        AppState.topN = patch.top_n;
        AppState.config = { ...AppState.config, ...patch };

        try {
            await api('/config', {
                method: 'PATCH',
                body: JSON.stringify(patch),
            });
        } catch (e) {
            console.warn('Failed to save config:', e.message);
        }
    }, 1000);

    topnSlider.addEventListener('input', () => {
        topnVal.textContent = topnSlider.value;
        saveConfig();
    });

    maxDepthInput.addEventListener('input', saveConfig);
    pageSizeInput.addEventListener('input', saveConfig);

    // Open/close
    function openModal() {
        modal.style.display = 'flex';
    }

    closeBtn.addEventListener('click', () => {
        modal.style.display = 'none';
    });

    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.style.display = 'none';
    });

    // Gear icon trigger
    const gearBtn = document.getElementById('config-btn');
    if (gearBtn) {
        gearBtn.addEventListener('click', openModal);
    }

    // Also expose on custom event for flexibility
    document.addEventListener('open-config', openModal);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
