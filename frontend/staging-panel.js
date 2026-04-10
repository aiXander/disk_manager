import { AppState, api, formatBytes } from './app.js';

export function initStagingPanel() {
    const panel = document.getElementById('staging-panel');
    if (!panel) return;

    panel.innerHTML = `
        <button class="staging-collapse-btn" id="staging-collapse-btn" title="Collapse panel">&rsaquo;</button>
        <div class="staging-inner" id="staging-inner">
            <div class="staging-header">
                <h3 class="staging-title">Staged for Deletion</h3>
            </div>
            <div class="staging-summary" id="staging-summary"></div>
            <ul class="staging-list" id="staging-list"></ul>
            <div class="staging-actions staging-bottom-actions">
                <button class="btn btn-ghost btn-sm" id="staging-clear">Clear All</button>
                <button class="btn btn-danger btn-sm" id="staging-execute">Execute Deletion</button>
            </div>
            <div class="staging-modal-overlay" id="staging-modal-overlay" style="display:none;">
                <div class="staging-modal" id="staging-modal">
                    <h3>Confirm Deletion</h3>
                    <div id="staging-modal-summary"></div>
                    <div class="modal-toggle">
                        <label>
                            <input type="radio" name="delete-mode" value="trash" checked /> Move to Trash
                        </label>
                        <label>
                            <input type="radio" name="delete-mode" value="permanent" /> Permanent Delete
                        </label>
                    </div>
                    <div class="modal-actions">
                        <button class="btn" id="modal-cancel">Cancel</button>
                        <button class="btn btn-danger" id="modal-confirm">Confirm</button>
                    </div>
                    <div class="modal-result" id="modal-result" style="display:none;"></div>
                </div>
            </div>
        </div>
    `;

    const collapseBtn = document.getElementById('staging-collapse-btn');
    const listEl = document.getElementById('staging-list');
    const summaryEl = document.getElementById('staging-summary');
    const clearBtn = document.getElementById('staging-clear');
    const executeBtn = document.getElementById('staging-execute');
    const modalOverlay = document.getElementById('staging-modal-overlay');
    const modalSummary = document.getElementById('staging-modal-summary');
    const modalCancel = document.getElementById('modal-cancel');
    const modalConfirm = document.getElementById('modal-confirm');
    const modalResult = document.getElementById('modal-result');

    // Collapse/expand toggle
    let collapsed = false;
    collapseBtn.addEventListener('click', () => {
        collapsed = !collapsed;
        document.body.classList.toggle('staging-collapsed', collapsed);
        collapseBtn.innerHTML = collapsed ? '&lsaquo;' : '&rsaquo;';
        collapseBtn.title = collapsed ? 'Expand panel' : 'Collapse panel';
    });

    function render() {
        const items = AppState.stagedItems;

        // Summary
        const totalSize = items.reduce((s, i) => s + (i.size || 0), 0);
        const fileCount = items.filter((i) => !i.is_dir).length;
        const dirCount = items.filter((i) => i.is_dir).length;

        if (items.length === 0) {
            summaryEl.innerHTML = '';
        } else {
            summaryEl.innerHTML = `
                <div class="staging-summary-stats">
                    <div class="summary-stat">
                        <span class="summary-label">Files</span>
                        <span class="summary-value">${fileCount}</span>
                    </div>
                    <div class="summary-stat">
                        <span class="summary-label">Dirs</span>
                        <span class="summary-value">${dirCount}</span>
                    </div>
                    <div class="summary-stat">
                        <span class="summary-label">Size</span>
                        <span class="summary-value">${formatBytes(totalSize)}</span>
                    </div>
                </div>
            `;
        }

        // List
        listEl.innerHTML = '';
        if (items.length === 0) {
            const li = document.createElement('li');
            li.className = 'staged-empty';
            li.textContent = 'No items staged';
            listEl.appendChild(li);
        } else {
            items.forEach((item, idx) => {
                const li = document.createElement('li');
                li.className = 'staging-item';
                li.innerHTML = `
                    <span class="staging-item-name" title="${escapeHtml(item.path)}">${escapeHtml(item.name)}</span>
                    <span class="staging-item-size">${formatBytes(item.size)}</span>
                    <button class="btn btn-sm btn-danger staging-item-rm" data-idx="${idx}">x</button>
                `;
                li.querySelector('.staging-item-rm').addEventListener('click', () => {
                    AppState.stagedItems.splice(idx, 1);
                    render();
                });
                listEl.appendChild(li);
            });
        }

        executeBtn.disabled = items.length === 0;
        clearBtn.disabled = items.length === 0;
    }

    // Initial render
    render();

    // Listen for staging changes
    document.addEventListener('staging-changed', render);

    // Clear all
    clearBtn.addEventListener('click', async () => {
        AppState.stagedItems = [];
        if (AppState.currentScanId) {
            try {
                await api(`/scans/${AppState.currentScanId}/staging`, { method: 'DELETE' });
            } catch { /* ignore */ }
        }
        render();
    });

    // Execute deletion -- open modal
    executeBtn.addEventListener('click', () => {
        const items = AppState.stagedItems;
        const totalSize = items.reduce((s, i) => s + (i.size || 0), 0);
        modalSummary.innerHTML = `
            <p>About to delete <strong>${items.length}</strong> items (${formatBytes(totalSize)}).</p>
            <ul class="modal-item-preview">
                ${items.slice(0, 10).map((i) => `<li>${escapeHtml(i.name)} (${formatBytes(i.size)})</li>`).join('')}
                ${items.length > 10 ? `<li>... and ${items.length - 10} more</li>` : ''}
            </ul>
        `;
        modalResult.style.display = 'none';
        modalConfirm.disabled = false;
        modalOverlay.style.display = 'flex';
    });

    modalCancel.addEventListener('click', () => {
        modalOverlay.style.display = 'none';
    });

    modalOverlay.addEventListener('click', (e) => {
        if (e.target === modalOverlay) modalOverlay.style.display = 'none';
    });

    modalConfirm.addEventListener('click', async () => {
        const mode = document.querySelector('input[name="delete-mode"]:checked').value;
        const scanId = AppState.currentScanId;
        if (!scanId) return;

        modalConfirm.disabled = true;
        modalConfirm.textContent = 'Deleting...';

        try {
            // Sync staged items to backend first
            await api(`/scans/${scanId}/staging/add`, {
                method: 'POST',
                body: JSON.stringify({ paths: AppState.stagedItems.map((i) => i.path) }),
            });

            // Execute
            const result = await api(`/scans/${scanId}/staging/execute`, {
                method: 'POST',
                body: JSON.stringify({ permanent: mode === 'permanent' }),
            });

            const deleted = result.deleted_count || result.deleted || 0;
            const freed = result.freed_bytes || result.freed || 0;
            const failures = result.failures || [];

            modalResult.style.display = 'block';
            modalResult.innerHTML = `
                <p class="result-success">Deleted ${deleted} items, freed ${formatBytes(freed)}.</p>
                ${failures.length > 0 ? `<p class="result-failures">Failures: ${failures.length}</p><ul>${failures.slice(0, 5).map((f) => `<li>${escapeHtml(typeof f === 'string' ? f : f.path || JSON.stringify(f))}</li>`).join('')}</ul>` : ''}
            `;

            // Clear staging
            AppState.stagedItems = [];
            render();

            // Refresh main view
            document.dispatchEvent(new CustomEvent('filters-changed'));

            // Auto-close modal after delay
            setTimeout(() => {
                modalOverlay.style.display = 'none';
                modalConfirm.textContent = 'Confirm';
                modalConfirm.disabled = false;
            }, 3000);
        } catch (e) {
            modalResult.style.display = 'block';
            modalResult.innerHTML = `<p class="result-error">Error: ${e.message}</p>`;
            modalConfirm.textContent = 'Confirm';
            modalConfirm.disabled = false;
        }
    });
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
