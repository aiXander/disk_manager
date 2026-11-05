// List view: every file (or folder) under the current folder, from
// GET /files with server-side filters, sort and paging (infinite scroll).
import { api } from "./api.js";
import { state, on, isStaged, colorOf, select } from "./state.js";
import { bytes, count, items, relative, date, age, basename } from "./format.js";
import { $, h, toastError } from "./ui.js";
import { catLabel, setFilter } from "./catbar.js";
import { nav, nodeMenu, stagePaths, unstagePaths, toggleStage } from "./actions.js";

const DAY = 86400;
const PRESETS = {
  files: { label: "Files", is_dir: false, sort: "size_desc" },
  folders: { label: "Folders", is_dir: true, sort: "size_desc" },
  stale: { label: "Large and untouched", is_dir: false, sort: "size_desc", min_size: 10e6, ageDays: 365 },
};
const SIZES = [[0, "Any size"], [1e6, "Over 1 MB"], [10e6, "Over 10 MB"], [100e6, "Over 100 MB"], [1e9, "Over 1 GB"]];
const AGES = [[0, "Any age"], [30, "Over a month"], [182, "Over 6 months"], [365, "Over a year"], [730, "Over 2 years"], [1825, "Over 5 years"]];

const f = {
  preset: "files",
  sort: "size_desc",
  min_size: 0,
  ageDays: 0,
  ageField: "mtime", // or "atime"
  name: "",
};

let rows = [];
let total = 0;
let totalSize = 0;
let page = 0;
let loading = false;
let token = 0;
let multi = new Set();   // selected paths
let anchor = null;       // for shift-click ranges
let pageSize = 100;
let els = null;
let active = false;

export function initList() {
  const root = $("#list-view");
  const search = h("input", { type: "search", class: "input search", id: "list-search", placeholder: "Filter by name", spellcheck: "false", "aria-label": "Filter by name" });
  let st = null;
  search.addEventListener("input", () => { clearTimeout(st); st = setTimeout(() => { f.name = search.value.trim(); reload(); }, 220); });
  search.addEventListener("keydown", (e) => { if (e.key === "Escape") { search.value = ""; f.name = ""; reload(); search.blur(); e.stopPropagation(); } });

  const presetSeg = h("div", { class: "seg seg-sm", role: "radiogroup", "aria-label": "Show" },
    Object.entries(PRESETS).map(([k, p]) => h("button", { type: "button", role: "radio", dataset: { preset: k }, onclick: () => applyPreset(k) }, p.label)));
  const sizeSel = select_("Size", SIZES, (v) => { f.min_size = +v; reload(); });
  const ageSel = select_("Age", AGES, (v) => { f.ageDays = +v; reload(); });
  const ageField = select_("Age by", [["mtime", "by last modified"], ["atime", "by last opened"]], (v) => { f.ageField = v; if (f.ageDays) reload(); });

  const thead = h("thead", {}, h("tr", {},
    h("th", { class: "c-check" }, h("input", { type: "checkbox", "aria-label": "Select all loaded rows", id: "list-all", onchange: (e) => selectAll(e.target.checked) })),
    th("Name", "name", "c-name"),
    th("Size", "size", "c-size"),
    h("th", { class: "c-kind" }, "Kind"),
    th("Modified", "mtime", "c-date"),
    th("Last opened", "atime", "c-date"),
    h("th", { class: "c-act" }, h("span", { class: "sr-only" }, "Mark")),
  ));
  const tbody = h("tbody");
  const sentinel = h("div", { class: "list-sentinel" });
  const scroller = h("div", { class: "list-scroll" }, h("table", { class: "list-table" }, thead, tbody), sentinel);
  const status = h("div", { class: "list-status" });
  const bulk = h("div", { class: "bulk", hidden: true });
  const filterChips = h("div", { class: "list-catchips" });

  root.append(
    h("div", { class: "list-toolbar" }, presetSeg, sizeSel.wrap, ageSel.wrap, ageField.wrap, filterChips, h("div", { class: "spacer" }), search),
    h("div", { class: "list-subbar" }, status, bulk),
    scroller,
  );
  els = { root, tbody, thead, status, bulk, sentinel, scroller, presetSeg, sizeSel, ageSel, ageField, search, filterChips };

  new IntersectionObserver((ents) => {
    if (ents.some((e) => e.isIntersecting) && active && !loading && rows.length < total) loadPage();
  }, { root: scroller, rootMargin: "400px" }).observe(sentinel);

  tbody.addEventListener("click", onRowClick);
  tbody.addEventListener("dblclick", onRowDbl);
  tbody.addEventListener("contextmenu", onRowContext);
  on("staging", renderRows);
  on("filter", () => { if (active) reload(); });
  syncControls();
}

function select_(label, opts, onchange) {
  const s = h("select", { class: "select", "aria-label": label, onchange: (e) => onchange(e.target.value) },
    opts.map(([v, l]) => h("option", { value: v }, l)));
  return { el: s, wrap: h("label", { class: "select-wrap" }, s) };
}

function th(label, key, cls) {
  return h("th", { class: `${cls} sortable`, dataset: { sort: key } },
    h("button", { type: "button", class: "th-btn", onclick: () => toggleSort(key) }, label, h("span", { class: "sort-ind", "aria-hidden": "true" })));
}

function toggleSort(key) {
  const cur = f.sort;
  const next = {
    size: cur === "size_desc" ? "size_asc" : "size_desc",
    name: cur === "name_asc" ? "name_desc" : "name_asc",
    mtime: cur === "mtime_asc" ? "mtime_desc" : "mtime_asc",
    atime: "atime_asc",
  }[key];
  f.sort = next;
  reload();
}

function applyPreset(k) {
  const p = PRESETS[k];
  f.preset = k;
  f.sort = p.sort;
  f.min_size = p.min_size || 0;
  f.ageDays = p.ageDays || 0;
  reload();
}

/** Open the list with a preset (used by the "largest files" shortcut). */
export function usePreset(k) { applyPreset(k); }

function syncControls() {
  for (const b of els.presetSeg.children) b.setAttribute("aria-checked", String(b.dataset.preset === f.preset));
  els.sizeSel.el.value = String(f.min_size);
  els.ageSel.el.value = String(f.ageDays);
  els.ageField.el.value = f.ageField;
  els.ageField.wrap.hidden = !f.ageDays;
  for (const t of els.thead.querySelectorAll("th.sortable")) {
    const key = t.dataset.sort;
    const dir = f.sort.startsWith(key + "_") ? f.sort.split("_")[1] : null;
    t.dataset.dir = dir || "";
    t.setAttribute("aria-sort", dir === "asc" ? "ascending" : dir === "desc" ? "descending" : "none");
  }
  els.filterChips.replaceChildren(...[...state.catFilter].map((c) =>
    h("button", { type: "button", class: "chip", style: `--c:${colorOf(c)}`, title: "Remove filter", onclick: () => { const n = new Set(state.catFilter); n.delete(c); setFilter(n); } },
      h("span", { class: "legend-dot" }), catLabel(c), h("span", { class: "chip-x", "aria-hidden": "true" }, "×"))));
}

function params() {
  const p = PRESETS[f.preset];
  const out = {
    under: state.path && !state.path.startsWith("__") ? state.path : undefined,
    is_dir: p.is_dir,
    sort: f.sort,
    page,
    page_size: pageSize,
    min_size: f.min_size || undefined,
    name_contains: f.name || undefined,
    categories: state.catFilter.size && !p.is_dir ? [...state.catFilter] : undefined,
  };
  if (f.ageDays) {
    const cutoff = Math.floor(Date.now() / 1000 - f.ageDays * DAY);
    out[f.ageField === "atime" ? "atime_before" : "mtime_before"] = cutoff;
  }
  return out;
}

export function setActive(on_) {
  active = on_;
  if (active) reload();
}

export function reload() {
  if (!els || !state.scan) return;
  pageSize = state.config?.page_size || 100;
  syncControls();
  rows = [];
  total = 0;
  page = 0;
  multi.clear();
  anchor = null;
  els.scroller.scrollTop = 0;
  if (!active) return;
  els.tbody.replaceChildren();
  loadPage(true);
}

async function loadPage(first = false) {
  const my = first ? ++token : token;
  loading = true;
  els.status.replaceChildren(h("span", { class: "list-loading" }, first ? "Loading…" : `${count(rows.length)} of ${count(total)}…`));
  try {
    const res = await api.files(state.scan.scan_id, params());
    if (my !== token) return;
    rows = rows.concat(res.items || []);
    total = res.total ?? rows.length;
    totalSize = res.total_size ?? 0;
    page += 1;
    renderRows();
  } catch (e) {
    if (my !== token) return;
    els.status.replaceChildren(h("span", { class: "list-err" }, `Couldn't load the list: ${e.message}`));
    toastError(e, "Couldn't load the list");
  } finally {
    if (my === token) loading = false;
  }
}

function renderRows() {
  if (!els || !active) return;
  const base = state.path && !state.path.startsWith("__") ? state.path : null;
  const kind = PRESETS[f.preset].is_dir ? "folder" : "file";
  els.status.replaceChildren(
    h("span", { class: "list-total" }, bytes(totalSize)),
    h("span", { class: "list-count" }, `in ${items(total, kind)}${rows.length < total ? `, showing ${count(rows.length)}` : ""}`),
  );
  if (!rows.length) {
    els.tbody.replaceChildren(h("tr", { class: "empty-row" }, h("td", { colspan: 7 },
      h("div", { class: "empty" }, h("b", {}, `No ${kind}s match.`), " Loosen the size, age or type filters above."))));
  } else {
    const frag = document.createDocumentFragment();
    rows.forEach((r, i) => frag.append(rowEl(r, i, base)));
    els.tbody.replaceChildren(frag);
  }
  renderBulk();
}

function rowEl(r, i, base) {
  const cat = r.is_dir ? null : r.category || "other";
  const staged = isStaged(r.path);
  const sel = multi.has(r.path);
  const relDir = relative(r.path, base);
  const dirPart = relDir.includes("/") ? relDir.slice(0, relDir.lastIndexOf("/")) : "";
  return h("tr", {
    class: `${sel ? "sel" : ""}${staged ? " staged" : ""}${state.selection?.path === r.path ? " focus" : ""}`,
    dataset: { i },
  },
    h("td", { class: "c-check" }, h("input", { type: "checkbox", checked: sel, "aria-label": `Select ${r.name}`, dataset: { role: "check" } })),
    h("td", { class: "c-name" },
      h("span", { class: `kind-dot ${r.is_dir ? (r.collapsed ? "collapsed" : "dir") : ""}`, style: cat ? `--c:${colorOf(cat)}` : null }),
      h("span", { class: "row-name", title: r.path }, r.name || basename(r.path)),
      dirPart ? h("span", { class: "row-dir", title: r.path }, dirPart) : null),
    h("td", { class: "c-size" }, bytes(r.size)),
    h("td", { class: "c-kind" }, r.is_dir ? (r.collapsed ? "Folder, not itemised" : `Folder, ${items(r.file_count ?? 0, "file")}`) : `${catLabel(cat)}${r.extension ? ` ${r.extension}` : ""}`),
    h("td", { class: "c-date", title: date(r.mtime) }, r.mtime ? age(r.mtime) : "–"),
    h("td", { class: "c-date", title: date(r.atime) }, r.atime ? age(r.atime) : "–"),
    h("td", { class: "c-act" }, h("button", {
      type: "button", class: `row-mark${staged ? " on" : ""}`, dataset: { role: "mark" },
      "aria-label": staged ? `Unmark ${r.name}` : `Mark ${r.name} for removal`, title: staged ? "Unmark" : "Mark for removal",
    }, staged ? "Marked" : "Mark")),
  );
}

function renderBulk() {
  const n = multi.size;
  els.bulk.hidden = n === 0;
  $("#list-all").checked = n > 0 && n === rows.length;
  if (!n) return;
  const picked = rows.filter((r) => multi.has(r.path));
  const size = picked.reduce((s, r) => s + (r.size || 0), 0);
  const unstaged = picked.filter((r) => !isStaged(r.path));
  els.bulk.replaceChildren(
    h("span", { class: "bulk-count" }, `${count(n)} selected, ${bytes(size)}`),
    unstaged.length
      ? h("button", { type: "button", class: "btn btn-mark btn-sm", onclick: () => stagePaths(unstaged.map((r) => r.path)) }, `Mark ${count(unstaged.length)} for removal`)
      : h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => unstagePaths(picked.filter((r) => state.stagedPaths.has(r.path)).map((r) => r.path)) }, "Unmark"),
    h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { multi.clear(); renderRows(); } }, "Clear selection"),
  );
}

function selectAll(on_) {
  multi = new Set(on_ ? rows.map((r) => r.path) : []);
  renderRows();
}

function rowOf(e) {
  const tr = e.target.closest("tr[data-i]");
  return tr ? { tr, i: +tr.dataset.i, r: rows[+tr.dataset.i] } : null;
}

function onRowClick(e) {
  const hit = rowOf(e);
  if (!hit) return;
  const { i, r } = hit;
  const role = e.target.dataset.role;
  if (role === "mark") { toggleStage(r); return; }
  if (role === "check" || e.metaKey || e.ctrlKey) {
    if (multi.has(r.path)) multi.delete(r.path); else multi.add(r.path);
    anchor = i;
  } else if (e.shiftKey && anchor != null) {
    const [a, b] = [Math.min(anchor, i), Math.max(anchor, i)];
    for (let k = a; k <= b; k++) multi.add(rows[k].path);
  } else {
    multi.clear();
    anchor = i;
    select(r);
  }
  renderRows();
}

function onRowDbl(e) {
  const hit = rowOf(e);
  if (!hit || e.target.dataset.role) return;
  if (hit.r.is_dir && !hit.r.collapsed) nav.showInList(hit.r);
}

function onRowContext(e) {
  const hit = rowOf(e);
  if (!hit) return;
  e.preventDefault();
  const picked = multi.has(hit.r.path) ? rows.filter((r) => multi.has(r.path)) : null;
  if (!picked) select(hit.r);
  nodeMenu(hit.r, e, picked);
}

/** Paths selected via multi-select (for the S shortcut). */
export function multiSelection() {
  return rows.filter((r) => multi.has(r.path));
}

export function focusSearch() { els?.search.focus(); els?.search.select(); }
