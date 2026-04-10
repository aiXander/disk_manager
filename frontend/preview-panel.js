import { AppState, api, formatBytes, formatDate } from './app.js';

export function initPreviewPanel() {
    const panel = document.getElementById('preview-panel');
    if (!panel) return;

    panel.innerHTML = `
        <div class="preview-overlay" id="preview-overlay"></div>
        <div class="preview-drawer" id="preview-drawer">
            <div class="preview-header">
                <h3 id="preview-title">Preview</h3>
                <button class="btn btn-sm" id="preview-close">X</button>
            </div>
            <div class="preview-thumb" id="preview-thumb"></div>
            <div class="preview-meta" id="preview-meta"></div>
            <div class="preview-actions">
                <button class="btn" id="preview-open-finder">Open in Finder</button>
                <button class="btn btn-danger" id="preview-stage">Stage for Deletion</button>
            </div>
        </div>
    `;

    const overlay = document.getElementById('preview-overlay');
    const drawer = document.getElementById('preview-drawer');
    const titleEl = document.getElementById('preview-title');
    const thumbEl = document.getElementById('preview-thumb');
    const metaEl = document.getElementById('preview-meta');
    const closeBtn = document.getElementById('preview-close');
    const openFinderBtn = document.getElementById('preview-open-finder');
    const stageBtn = document.getElementById('preview-stage');

    let currentPath = null;
    let currentScanId = null;

    function open() {
        panel.classList.add('open');
    }

    function close() {
        panel.classList.remove('open');
        currentPath = null;
    }

    closeBtn.addEventListener('click', close);
    overlay.addEventListener('click', close);

    document.addEventListener('show-preview', async (e) => {
        const { path, scanId } = e.detail;
        currentPath = path;
        currentScanId = scanId || AppState.currentScanId;
        AppState.previewPath = path;

        titleEl.textContent = path.split('/').pop() || path;
        thumbEl.innerHTML = '<p class="loading">Loading...</p>';
        metaEl.innerHTML = '';

        open();

        // Load thumbnail
        try {
            const thumbUrl = `/api/v1/scans/${currentScanId}/preview/thumbnail?path=${encodeURIComponent(path)}`;
            const img = document.createElement('img');
            img.src = thumbUrl;
            img.alt = 'Thumbnail';
            img.className = 'preview-img';
            img.onerror = () => {
                thumbEl.innerHTML = '<p class="no-thumb">No preview available</p>';
            };
            img.onload = () => {
                thumbEl.innerHTML = '';
                thumbEl.appendChild(img);
            };
        } catch {
            thumbEl.innerHTML = '<p class="no-thumb">No preview available</p>';
        }

        // Load metadata
        try {
            const meta = await api(`/scans/${currentScanId}/preview/meta?path=${encodeURIComponent(path)}`);
            metaEl.innerHTML = `
                <table class="meta-table">
                    <tr><td>Size</td><td>${formatBytes(meta.size)}</td></tr>
                    <tr><td>Modified</td><td>${formatDate(meta.mtime)}</td></tr>
                    <tr><td>Accessed</td><td>${formatDate(meta.atime)}</td></tr>
                    <tr><td>Created</td><td>${formatDate(meta.ctime)}</td></tr>
                    ${meta.extension ? `<tr><td>Type</td><td>${meta.extension}</td></tr>` : ''}
                    ${meta.mime_type ? `<tr><td>MIME</td><td>${meta.mime_type}</td></tr>` : ''}
                    ${meta.owner ? `<tr><td>Owner</td><td>${meta.owner}</td></tr>` : ''}
                    ${meta.permissions ? `<tr><td>Permissions</td><td>${meta.permissions}</td></tr>` : ''}
                    <tr><td>Path</td><td class="meta-path">${escapeHtml(meta.path || path)}</td></tr>
                </table>
            `;
        } catch (e) {
            metaEl.innerHTML = `<p class="error">Could not load metadata: ${e.message}</p>`;
        }
    });

    // Open in Finder
    openFinderBtn.addEventListener('click', async () => {
        if (!currentPath || !currentScanId) return;
        try {
            await api(`/scans/${currentScanId}/preview/open`, {
                method: 'POST',
                body: JSON.stringify({ path: currentPath }),
            });
        } catch (e) {
            alert('Failed to open: ' + e.message);
        }
    });

    // Stage for deletion
    stageBtn.addEventListener('click', () => {
        if (!currentPath) return;
        const name = currentPath.split('/').pop();
        if (!AppState.stagedItems.find((s) => s.path === currentPath)) {
            AppState.stagedItems.push({ path: currentPath, name, size: 0, is_dir: false });
            document.dispatchEvent(new CustomEvent('staging-changed'));
        }
        close();
    });
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
