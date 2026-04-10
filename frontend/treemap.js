import { AppState, api, formatBytes } from './app.js';

const CATEGORY_COLORS = {
    image: '#4e9af1',
    video: '#e05c5c',
    audio: '#f1a34e',
    document: '#6db66d',
    code: '#b88de8',
    archive: '#e8d08d',
    other: '#aaaaaa',
};

const EXT_CATEGORY = {
    // image
    jpg: 'image', jpeg: 'image', png: 'image', gif: 'image', bmp: 'image',
    svg: 'image', webp: 'image', tiff: 'image', tif: 'image', ico: 'image',
    heic: 'image', heif: 'image', raw: 'image', cr2: 'image', nef: 'image',
    // video
    mp4: 'video', mkv: 'video', avi: 'video', mov: 'video', wmv: 'video',
    flv: 'video', webm: 'video', m4v: 'video', mpg: 'video', mpeg: 'video',
    '3gp': 'video', ts: 'video',
    // audio
    mp3: 'audio', wav: 'audio', flac: 'audio', aac: 'audio', ogg: 'audio',
    wma: 'audio', m4a: 'audio', opus: 'audio', aiff: 'audio',
    // document
    pdf: 'document', doc: 'document', docx: 'document', xls: 'document',
    xlsx: 'document', ppt: 'document', pptx: 'document', txt: 'document',
    rtf: 'document', csv: 'document', odt: 'document', ods: 'document',
    epub: 'document', md: 'document',
    // code
    js: 'code', ts: 'code', py: 'code', java: 'code', c: 'code', cpp: 'code',
    h: 'code', hpp: 'code', cs: 'code', go: 'code', rs: 'code', rb: 'code',
    php: 'code', swift: 'code', kt: 'code', sh: 'code', bash: 'code',
    html: 'code', css: 'code', scss: 'code', json: 'code', xml: 'code',
    yaml: 'code', yml: 'code', toml: 'code', sql: 'code', r: 'code',
    // archive
    zip: 'archive', tar: 'archive', gz: 'archive', bz2: 'archive',
    xz: 'archive', '7z': 'archive', rar: 'archive', dmg: 'archive',
    iso: 'archive', pkg: 'archive', deb: 'archive', rpm: 'archive',
};

function categoryOf(name) {
    if (!name) return 'other';
    const dot = name.lastIndexOf('.');
    if (dot < 0) return 'other';
    const ext = name.slice(dot + 1).toLowerCase();
    return EXT_CATEGORY[ext] || 'other';
}

function colorOf(node) {
    if (node.data.is_dir) {
        return '#cccccc';
    }
    return CATEGORY_COLORS[categoryOf(node.data.name)] || CATEGORY_COLORS.other;
}

let breadcrumbPath = [];

// Context menu management
let activeContextMenu = null;
function removeContextMenu() {
    if (activeContextMenu) {
        activeContextMenu.remove();
        activeContextMenu = null;
    }
}

export function initTreemap() {
    document.addEventListener('click', removeContextMenu);
}

export async function renderTreemap(scanId, rootPath) {
    const container = document.getElementById('treemap-container');
    if (!container) return;

    const params = new URLSearchParams({ depth: '3' });
    if (rootPath) params.set('root', rootPath);

    const f = AppState.filters;
    if (f.extensions.length) params.set('extensions', f.extensions.join(','));
    if (f.excludeExtensions) params.set('exclude_extensions', 'true');
    if (f.minSize) params.set('min_size', f.minSize);
    if (f.maxSize) params.set('max_size', f.maxSize);

    let treeData;
    try {
        treeData = await api(`/scans/${scanId}/tree?${params}`);
    } catch (e) {
        container.innerHTML = `<p class="error">Failed to load tree: ${e.message}</p>`;
        return;
    }

    // Build breadcrumb
    breadcrumbPath = [];
    if (rootPath) {
        const parts = rootPath.split('/').filter(Boolean);
        let acc = '';
        parts.forEach((p) => {
            acc += '/' + p;
            breadcrumbPath.push({ name: p, path: acc });
        });
    }

    container.innerHTML = '';
    removeContextMenu();

    // Breadcrumb bar
    const breadcrumb = document.createElement('div');
    breadcrumb.className = 'treemap-breadcrumb';
    const rootLink = document.createElement('span');
    rootLink.className = 'breadcrumb-link';
    rootLink.textContent = '/';
    rootLink.addEventListener('click', () => {
        AppState.treeZoomPath = null;
        renderTreemap(scanId, null);
    });
    breadcrumb.appendChild(rootLink);

    breadcrumbPath.forEach((crumb) => {
        const sep = document.createElement('span');
        sep.textContent = ' / ';
        breadcrumb.appendChild(sep);
        const link = document.createElement('span');
        link.className = 'breadcrumb-link';
        link.textContent = crumb.name;
        link.addEventListener('click', () => {
            AppState.treeZoomPath = crumb.path;
            renderTreemap(scanId, crumb.path);
        });
        breadcrumb.appendChild(link);
    });
    container.appendChild(breadcrumb);

    // SVG treemap
    const width = container.clientWidth || 900;
    const height = Math.max(400, container.clientHeight - 40);

    const root = d3
        .hierarchy(treeData)
        .sum((d) => (d.children ? 0 : d.size || 0))
        .sort((a, b) => b.value - a.value);

    d3.treemap().size([width, height]).paddingInner(1).round(true)(root);

    const svg = d3
        .select(container)
        .append('svg')
        .attr('width', width)
        .attr('height', height)
        .attr('class', 'treemap-svg');

    const cells = svg
        .selectAll('g')
        .data(root.leaves().filter((d) => d.x1 - d.x0 >= 4 && d.y1 - d.y0 >= 4))
        .join('g')
        .attr('transform', (d) => `translate(${d.x0},${d.y0})`);

    cells
        .append('rect')
        .attr('width', (d) => d.x1 - d.x0)
        .attr('height', (d) => d.y1 - d.y0)
        .attr('fill', (d) => colorOf(d))
        .attr('stroke', '#fff')
        .attr('stroke-width', 0.5)
        .attr('rx', 2);

    // Labels only if cell large enough
    cells
        .filter((d) => d.x1 - d.x0 > 40 && d.y1 - d.y0 > 18)
        .append('text')
        .attr('x', 4)
        .attr('y', 14)
        .attr('class', 'treemap-label')
        .text((d) => {
            const name = d.data.name || '';
            const maxChars = Math.floor((d.x1 - d.x0 - 8) / 7);
            return name.length > maxChars ? name.slice(0, maxChars - 1) + '\u2026' : name;
        });

    cells
        .filter((d) => d.x1 - d.x0 > 50 && d.y1 - d.y0 > 32)
        .append('text')
        .attr('x', 4)
        .attr('y', 28)
        .attr('class', 'treemap-size-label')
        .text((d) => formatBytes(d.value));

    // Tooltip
    const tooltip = document.createElement('div');
    tooltip.className = 'treemap-tooltip';
    tooltip.style.display = 'none';
    container.appendChild(tooltip);

    cells
        .on('mouseover', (event, d) => {
            tooltip.style.display = 'block';
            tooltip.textContent = `${d.data.path || d.data.name} (${formatBytes(d.value)})`;
        })
        .on('mousemove', (event) => {
            tooltip.style.left = event.offsetX + 12 + 'px';
            tooltip.style.top = event.offsetY + 12 + 'px';
        })
        .on('mouseout', () => {
            tooltip.style.display = 'none';
        });

    // Single-click: show context popup menu
    cells.on('click', (event, d) => {
        event.stopPropagation();
        removeContextMenu();
        showCellContextMenu(event, d, scanId, container);
    });

    // Double-click: directory -> zoom in; file -> open with OS
    cells.on('dblclick', (event, d) => {
        event.stopPropagation();
        removeContextMenu();
        if (d.data.is_dir) {
            AppState.treeZoomPath = d.data.path;
            renderTreemap(scanId, d.data.path);
        } else {
            // Open file with OS
            api(`/scans/${scanId}/preview/open`, {
                method: 'POST',
                body: JSON.stringify({ path: d.data.path }),
            }).catch((e) => console.warn('Failed to open file:', e.message));
        }
    });

    // Right-click: stage for deletion
    cells.on('contextmenu', (event, d) => {
        event.preventDefault();
        removeContextMenu();
        stageItem(d);
    });
}

function stageItem(d) {
    const item = { path: d.data.path, name: d.data.name, size: d.value, is_dir: !!d.data.is_dir };
    if (!AppState.stagedItems.find((s) => s.path === item.path)) {
        AppState.stagedItems.push(item);
        document.dispatchEvent(new CustomEvent('staging-changed'));
    }
}

function showCellContextMenu(event, d, scanId, container) {
    const menu = document.createElement('div');
    menu.className = 'treemap-context-menu';

    const rect = container.getBoundingClientRect();
    let x = event.clientX - rect.left;
    let y = event.clientY - rect.top;

    // Menu items
    const items = [];

    items.push({
        label: 'Open in File Explorer',
        icon: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
            <polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
        </svg>`,
        action: () => {
            api(`/scans/${scanId}/preview/open`, {
                method: 'POST',
                body: JSON.stringify({ path: d.data.path }),
            }).catch((e) => console.warn('Failed to open:', e.message));
        },
    });

    items.push({
        label: 'Stage for Deletion',
        icon: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
        </svg>`,
        danger: true,
        action: () => stageItem(d),
    });

    items.forEach((item) => {
        const btn = document.createElement('button');
        btn.className = 'treemap-context-item' + (item.danger ? ' treemap-context-item--danger' : '');
        btn.innerHTML = `${item.icon}<span>${item.label}</span>`;
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            removeContextMenu();
            item.action();
        });
        menu.appendChild(btn);
    });

    // Position - clamp to container bounds
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    container.appendChild(menu);

    // Adjust if overflowing
    const menuRect = menu.getBoundingClientRect();
    if (menuRect.right > rect.right) {
        menu.style.left = (x - menuRect.width) + 'px';
    }
    if (menuRect.bottom > rect.bottom) {
        menu.style.top = (y - menuRect.height) + 'px';
    }

    activeContextMenu = menu;
}
