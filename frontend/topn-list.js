import { AppState, api, formatBytes, formatDate } from './app.js';

const PAGE_SIZE = 50;
let currentMode = 'files'; // 'files' | 'dirs'
let currentSort = { col: 'size', asc: false };
let allRows = [];
let renderedCount = 0;

// Default column widths as percentage of table width
const DEFAULT_COL_WIDTHS = {
    files: { chk: 4, name: 25, path: 35, size: 12, mtime: 14, ext: 10 },
    dirs:  { chk: 4, name: 25, path: 35, size: 12, mtime: 14, ext: 10 },
};

function getColWidths() {
    const saved = AppState.config.column_widths;
    if (saved && saved[currentMode]) {
        return { ...DEFAULT_COL_WIDTHS[currentMode], ...saved[currentMode] };
    }
    return { ...DEFAULT_COL_WIDTHS[currentMode] };
}

function saveColWidths(widths) {
    if (!AppState.config.column_widths) AppState.config.column_widths = {};
    AppState.config.column_widths[currentMode] = widths;
    // Persist to backend
    api('/config', {
        method: 'PATCH',
        body: JSON.stringify({ column_widths: AppState.config.column_widths }),
    }).catch((e) => console.warn('Failed to save column widths:', e.message));
}

function applyColWidths(tableEl) {
    if (!tableEl) return;
    const widths = getColWidths();
    const cols = tableEl.querySelectorAll('col');
    if (cols.length === 6) {
        cols[0].style.width = widths.chk + '%';
        cols[1].style.width = widths.name + '%';
        cols[2].style.width = widths.path + '%';
        cols[3].style.width = widths.size + '%';
        cols[4].style.width = widths.mtime + '%';
        cols[5].style.width = widths.ext + '%';
    }
}

export function initTopNList() {
    // initialized on first render
}

export async function renderTopN(scanId) {
    const container = document.getElementById('topn-container');
    if (!container) return;

    container.innerHTML = `
        <div class="topn-controls">
            <button class="btn btn-toggle active" id="topn-files-btn">Files</button>
            <button class="btn btn-toggle" id="topn-dirs-btn">Directories</button>
        </div>
        <div class="topn-table-wrap" id="topn-table-wrap">
            <table class="topn-table" id="topn-table">
                <colgroup>
                    <col/><col/><col/><col/><col/><col/>
                </colgroup>
                <thead>
                    <tr>
                        <th class="topn-col-chk"><input type="checkbox" id="topn-check-all" /></th>
                        <th data-col="name" class="sortable topn-col-name">Name</th>
                        <th data-col="path" class="sortable topn-col-path">Path</th>
                        <th data-col="size" class="sortable topn-col-size">Size</th>
                        <th data-col="mtime" class="sortable topn-col-mtime">Modified</th>
                        <th data-col="ext" class="sortable topn-col-ext">Extension</th>
                    </tr>
                </thead>
                <tbody id="topn-tbody"></tbody>
            </table>
            <div id="topn-sentinel" style="height:1px;"></div>
        </div>
    `;

    applyColWidths(document.getElementById('topn-table'));
    setupColumnResize();

    const filesBtn = document.getElementById('topn-files-btn');
    const dirsBtn = document.getElementById('topn-dirs-btn');

    filesBtn.addEventListener('click', () => {
        currentMode = 'files';
        filesBtn.classList.add('active');
        dirsBtn.classList.remove('active');
        applyColWidths(document.getElementById('topn-table'));
        fetchAndRender(scanId);
    });

    dirsBtn.addEventListener('click', () => {
        currentMode = 'dirs';
        dirsBtn.classList.add('active');
        filesBtn.classList.remove('active');
        applyColWidths(document.getElementById('topn-table'));
        fetchAndRender(scanId);
    });

    // Column sort
    container.querySelectorAll('th.sortable').forEach((th) => {
        th.addEventListener('click', () => {
            const col = th.dataset.col;
            if (currentSort.col === col) {
                currentSort.asc = !currentSort.asc;
            } else {
                currentSort = { col, asc: col === 'name' || col === 'path' || col === 'ext' };
            }
            sortAndRender();
        });
    });

    // Check all
    document.getElementById('topn-check-all').addEventListener('change', (e) => {
        const checked = e.target.checked;
        if (checked) {
            allRows.forEach((r) => {
                if (!AppState.stagedItems.find((s) => s.path === r.path)) {
                    AppState.stagedItems.push({ path: r.path, name: r.name, size: r.size, is_dir: r.is_dir });
                }
            });
        } else {
            const paths = new Set(allRows.map((r) => r.path));
            AppState.stagedItems = AppState.stagedItems.filter((s) => !paths.has(s.path));
        }
        document.dispatchEvent(new CustomEvent('staging-changed'));
        renderVisibleRows();
    });

    await fetchAndRender(scanId);
}

function setupColumnResize() {
    const table = document.getElementById('topn-table');
    if (!table) return;
    const ths = table.querySelectorAll('thead th');

    ths.forEach((th, idx) => {
        if (idx === 0) return; // skip checkbox column
        const handle = document.createElement('div');
        handle.className = 'col-resize-handle';
        th.style.position = 'relative';
        th.appendChild(handle);

        let startX, startWidth, tableWidth;

        handle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            startX = e.clientX;
            tableWidth = table.offsetWidth;
            startWidth = th.offsetWidth;

            const onMouseMove = (e) => {
                const diff = e.clientX - startX;
                const newPct = ((startWidth + diff) / tableWidth) * 100;
                if (newPct >= 4 && newPct <= 60) {
                    const cols = table.querySelectorAll('col');
                    cols[idx].style.width = newPct + '%';
                }
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                // Save current widths
                const cols = table.querySelectorAll('col');
                const keys = ['chk', 'name', 'path', 'size', 'mtime', 'ext'];
                const widths = {};
                keys.forEach((k, i) => {
                    widths[k] = parseFloat(((cols[i].offsetWidth || table.offsetWidth * DEFAULT_COL_WIDTHS[currentMode][k] / 100) / table.offsetWidth * 100).toFixed(1));
                });
                saveColWidths(widths);
            };

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    });
}

async function fetchAndRender(scanId) {
    const f = AppState.filters;
    const hasFilters = f.extensions.length || f.minSize || f.maxSize;

    let data;
    try {
        if (hasFilters) {
            const params = new URLSearchParams();
            if (f.extensions.length) params.set('extensions', f.extensions.join(','));
            if (f.excludeExtensions) params.set('exclude_extensions', 'true');
            if (f.minSize) params.set('min_size', f.minSize);
            if (f.maxSize) params.set('max_size', f.maxSize);
            params.set('is_dir', currentMode === 'dirs' ? 'true' : 'false');
            params.set('sort', 'size_desc');
            params.set('page_size', '1000');
            data = await api(`/scans/${scanId}/files?${params}`);
        } else {
            const params = new URLSearchParams({
                n: AppState.topN,
                mode: currentMode,
                sort: 'size',
            });
            data = await api(`/scans/${scanId}/top?${params}`);
        }
    } catch (e) {
        const tbody = document.getElementById('topn-tbody');
        if (tbody) tbody.innerHTML = `<tr><td colspan="6" class="error">Error: ${e.message}</td></tr>`;
        return;
    }

    allRows = Array.isArray(data) ? data : data.items || data.files || [];
    // Normalize fields
    allRows = allRows.map((r) => ({
        name: r.name || r.path?.split('/').pop() || '',
        path: r.path || '',
        size: r.size || 0,
        mtime: r.mtime || r.modified || 0,
        ext: r.extension || r.ext || (r.name || '').split('.').pop() || '',
        is_dir: r.is_dir || false,
    }));

    currentSort = { col: 'size', asc: false };
    sortAndRender();
    setupIntersectionObserver();
}

function sortAndRender() {
    const { col, asc } = currentSort;
    allRows.sort((a, b) => {
        let va = a[col];
        let vb = b[col];
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
        return 0;
    });

    renderedCount = 0;
    const tbody = document.getElementById('topn-tbody');
    if (tbody) tbody.innerHTML = '';
    renderBatch();
}

function renderBatch() {
    const tbody = document.getElementById('topn-tbody');
    if (!tbody) return;

    const end = Math.min(renderedCount + PAGE_SIZE, allRows.length);
    const frag = document.createDocumentFragment();

    for (let i = renderedCount; i < end; i++) {
        const r = allRows[i];
        const tr = document.createElement('tr');
        tr.dataset.path = r.path;

        const isStaged = AppState.stagedItems.some((s) => s.path === r.path);

        tr.innerHTML = `
            <td class="topn-chk"><input type="checkbox" ${isStaged ? 'checked' : ''} /></td>
            <td class="topn-name" title="${escapeHtml(r.name)}">${escapeHtml(r.name)}</td>
            <td class="topn-path" title="${escapeHtml(r.path)}">${escapeHtml(r.path)}</td>
            <td class="topn-size">${formatBytes(r.size)}</td>
            <td class="topn-mtime">${formatDate(r.mtime)}</td>
            <td class="topn-ext">${escapeHtml(r.ext)}</td>
        `;

        // Checkbox staging
        const cb = tr.querySelector('input[type="checkbox"]');
        cb.addEventListener('change', (e) => {
            e.stopPropagation();
            if (cb.checked) {
                if (!AppState.stagedItems.find((s) => s.path === r.path)) {
                    AppState.stagedItems.push({ path: r.path, name: r.name, size: r.size, is_dir: r.is_dir });
                }
            } else {
                AppState.stagedItems = AppState.stagedItems.filter((s) => s.path !== r.path);
            }
            document.dispatchEvent(new CustomEvent('staging-changed'));
        });

        // Click row -> preview
        tr.addEventListener('click', (e) => {
            if (e.target.tagName === 'INPUT') return;
            document.dispatchEvent(
                new CustomEvent('show-preview', {
                    detail: { path: r.path, scanId: AppState.currentScanId },
                })
            );
        });

        frag.appendChild(tr);
    }

    tbody.appendChild(frag);
    renderedCount = end;
}

function renderVisibleRows() {
    const tbody = document.getElementById('topn-tbody');
    if (!tbody) return;
    tbody.querySelectorAll('tr').forEach((tr) => {
        const path = tr.dataset.path;
        const cb = tr.querySelector('input[type="checkbox"]');
        if (cb) cb.checked = AppState.stagedItems.some((s) => s.path === path);
    });
}

function setupIntersectionObserver() {
    const sentinel = document.getElementById('topn-sentinel');
    if (!sentinel) return;

    const observer = new IntersectionObserver(
        (entries) => {
            if (entries[0].isIntersecting && renderedCount < allRows.length) {
                renderBatch();
            }
        },
        { root: document.getElementById('topn-table-wrap'), rootMargin: '200px' }
    );
    observer.observe(sentinel);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
