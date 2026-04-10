import { AppState } from './app.js';
import { initTreemap, renderTreemap } from './treemap.js';
import { initTopNList, renderTopN } from './topn-list.js';

export function initMainView() {
    const container = document.getElementById('main-view');
    if (!container) return;

    // Build the toolbar with view toggle + parent button
    container.innerHTML = `
        <div class="view-toolbar">
            <div class="view-toolbar-left">
                <button id="parent-folder-btn" class="btn btn-ghost btn-sm" title="Go to parent folder" style="display:none;">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="15 18 9 12 15 6"/>
                    </svg>
                    Up
                </button>
                <div class="view-toggle">
                    <button id="view-treemap-btn" class="toggle-btn active" title="Treemap View">
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/>
                            <rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/>
                        </svg>
                        Treemap
                    </button>
                    <button id="view-topn-btn" class="toggle-btn" title="Top-N List">
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>
                            <line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/>
                            <line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>
                        </svg>
                        Top-N
                    </button>
                </div>
            </div>
            <div class="view-info">
                <span id="view-item-count" class="view-stat"></span>
                <span id="view-total-size" class="view-stat"></span>
            </div>
        </div>
        <div id="treemap-container" class="view-container active"></div>
        <div id="topn-container" class="view-container"></div>
    `;

    initTreemap();
    initTopNList();

    const btnTreemap = document.getElementById('view-treemap-btn');
    const btnTopN = document.getElementById('view-topn-btn');
    const parentBtn = document.getElementById('parent-folder-btn');

    // View toggle
    btnTreemap.addEventListener('click', () => {
        AppState.viewMode = 'treemap';
        btnTreemap.classList.add('active');
        btnTopN.classList.remove('active');
        refresh();
    });

    btnTopN.addEventListener('click', () => {
        AppState.viewMode = 'topn';
        btnTopN.classList.add('active');
        btnTreemap.classList.remove('active');
        refresh();
    });

    // Parent folder button
    parentBtn.addEventListener('click', () => {
        if (AppState.treeZoomPath) {
            const parts = AppState.treeZoomPath.split('/').filter(Boolean);
            parts.pop();
            AppState.treeZoomPath = parts.length > 0 ? '/' + parts.join('/') : null;
        } else {
            AppState.treeZoomPath = null;
        }
        refresh();
    });

    function updateParentBtn() {
        if (AppState.viewMode === 'treemap' && AppState.treeZoomPath) {
            parentBtn.style.display = '';
        } else {
            parentBtn.style.display = 'none';
        }
    }

    function refresh() {
        const scanId = AppState.currentScanId;
        if (!scanId) return;

        const treemapEl = document.getElementById('treemap-container');
        const topnEl = document.getElementById('topn-container');

        updateParentBtn();

        if (AppState.viewMode === 'treemap') {
            treemapEl.classList.add('active');
            topnEl.classList.remove('active');
            renderTreemap(scanId, AppState.treeZoomPath);
        } else {
            treemapEl.classList.remove('active');
            topnEl.classList.add('active');
            renderTopN(scanId);
        }
    }

    document.addEventListener('scan-ready', () => {
        AppState.treeZoomPath = null;
        refresh();
    });

    document.addEventListener('filters-changed', refresh);
}
