import { AppState, api } from './app.js';

export function initScanPanel() {
    const panel = document.getElementById('scan-panel');
    if (!panel) return;

    panel.innerHTML = `
        <div class="scan-panel-inner">
            <div class="scan-paths" id="scan-paths"></div>
            <button class="btn btn-sm" id="add-path-btn" title="Add path">+ Add Path</button>
            <div class="scan-actions">
                <button class="btn btn-primary" id="start-scan-btn">Start Scan</button>
                <select id="scan-session-select">
                    <option value="">-- Past Scans --</option>
                </select>
            </div>
            <div class="scan-progress" id="scan-progress" style="display:none;">
                <div class="progress-bar-track">
                    <div class="progress-bar-fill" id="progress-bar-fill"></div>
                </div>
                <span class="progress-text" id="progress-text">0%</span>
            </div>
        </div>
    `;

    const pathsContainer = document.getElementById('scan-paths');
    const addPathBtn = document.getElementById('add-path-btn');
    const startScanBtn = document.getElementById('start-scan-btn');
    const sessionSelect = document.getElementById('scan-session-select');
    const progressWrap = document.getElementById('scan-progress');
    const progressFill = document.getElementById('progress-bar-fill');
    const progressText = document.getElementById('progress-text');

    // --- Path inputs ---
    function addPathInput(value = '') {
        const row = document.createElement('div');
        row.className = 'scan-path-row';
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'path-input';
        input.placeholder = '/path/to/scan';
        input.value = value;
        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn btn-sm btn-danger';
        removeBtn.textContent = 'x';
        removeBtn.addEventListener('click', () => {
            if (pathsContainer.querySelectorAll('.scan-path-row').length > 1) {
                row.remove();
            }
        });
        row.appendChild(input);
        row.appendChild(removeBtn);
        pathsContainer.appendChild(row);
    }

    // Pre-populate from config
    const lastPaths = AppState.config.last_scan_paths;
    if (Array.isArray(lastPaths) && lastPaths.length > 0) {
        lastPaths.forEach((p) => addPathInput(p));
    } else {
        addPathInput();
    }

    addPathBtn.addEventListener('click', () => addPathInput());

    function getPathValues() {
        return Array.from(pathsContainer.querySelectorAll('.path-input'))
            .map((i) => i.value.trim())
            .filter(Boolean);
    }

    // --- Load past scans ---
    async function loadScans() {
        try {
            const data = await api('/scans');
            const scans = Array.isArray(data) ? data : data.scans || [];
            sessionSelect.innerHTML = '<option value="">-- Past Scans --</option>';
            scans.forEach((s) => {
                const opt = document.createElement('option');
                opt.value = s.id;
                const roots = s.roots ? s.roots.split(',').map(r => r.trim()).join(', ') : null;
                const label = roots || s.id;
                const ts = s.started_at || s.finished_at;
                const dateStr = ts ? new Date(ts * 1000).toLocaleDateString() : 'Unknown date';
                opt.textContent = `${label} (${dateStr})`;
                sessionSelect.appendChild(opt);
            });
        } catch (e) {
            console.warn('Failed to load scans:', e.message);
        }
    }
    loadScans();

    // --- Select past session ---
    sessionSelect.addEventListener('change', () => {
        const id = sessionSelect.value;
        if (!id) return;
        AppState.currentScanId = id;
        document.dispatchEvent(new CustomEvent('scan-ready', { detail: { scanId: id } }));
    });

    // --- Start scan ---
    startScanBtn.addEventListener('click', async () => {
        const paths = getPathValues();
        if (paths.length === 0) {
            alert('Enter at least one path to scan.');
            return;
        }

        startScanBtn.disabled = true;
        progressWrap.style.display = 'flex';
        progressFill.style.width = '0%';
        progressText.textContent = 'Starting...';

        try {
            const scan = await api('/scans', {
                method: 'POST',
                body: JSON.stringify({ roots: paths }),
            });
            const scanId = scan.scan_id;
            AppState.currentScanId = scanId;

            // Connect to SSE stream
            const evtSource = new EventSource(`/api/v1/scans/${scanId}/stream`);

            evtSource.addEventListener('progress', (e) => {
                const d = JSON.parse(e.data);
                const pct = d.percent != null ? d.percent : 0;
                progressFill.style.width = `${pct}%`;
                progressText.textContent = d.message || `${Math.round(pct)}%`;
            });

            evtSource.addEventListener('complete', (e) => {
                evtSource.close();
                progressFill.style.width = '100%';
                progressText.textContent = 'Complete';
                startScanBtn.disabled = false;
                loadScans();
                document.dispatchEvent(new CustomEvent('scan-ready', { detail: { scanId } }));
            });

            evtSource.addEventListener('error', (e) => {
                // SSE can fire error on close; check readyState
                if (evtSource.readyState === EventSource.CLOSED) return;
                evtSource.close();
                progressText.textContent = 'Error during scan';
                startScanBtn.disabled = false;
            });

            evtSource.onerror = () => {
                if (evtSource.readyState === EventSource.CLOSED) return;
                evtSource.close();
                progressText.textContent = 'Connection lost';
                startScanBtn.disabled = false;
            };
        } catch (e) {
            alert('Scan failed: ' + e.message);
            startScanBtn.disabled = false;
            progressWrap.style.display = 'none';
        }
    });
}
