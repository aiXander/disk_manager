// Browsing a finished scan: folder navigation, breadcrumb, map/list switch.
import { api } from "./api.js";
import { state, on, emit, select } from "./state.js";
import { bytes, parent, shortPath } from "./format.js";
import { $, $$, h, toast, toastError } from "./ui.js";
import { Treemap } from "./treemap.js";
import { loadSummary } from "./catbar.js";
import { setFolder } from "./inspector.js";
import { nav, nodeMenu } from "./actions.js";
import * as list from "./list.js";

const VIRTUAL = "__roots__";
const cache = new Map();    // folder path -> tree data (depth 2), for navigation
const subCache = new Map(); // folder path -> promise of tree data (depth 1), for map refinement
let tm = null;
let navToken = 0;
let onHash = () => {};

export function initBrowse({ onLocation }) {
  onHash = onLocation;
  tm = new Treemap($("#map-view"), {
    onSelect: (d) => select(d),
    onDrill: (d, thenSelect) => drill(d, thenSelect),
    onContext: (d, e) => nodeMenu(d, e),
    onOther: (d) => showInList({ path: d._parent, is_dir: true }),
    fetchSub,
  });
  nav.drill = (d) => drill(d);
  nav.showInList = showInList;

  for (const b of $$(".seg [data-view]")) b.addEventListener("click", () => setView(b.dataset.view));
  on("staging", () => tm.applyDecorations());
  on("filter", () => tm.applyDecorations());
  on("selection", (d) => {
    if (d && !d.path) return; // "N smaller items" buckets select themselves
    if ((d?.path || null) !== tm.selectedKey) tm.setSelected(d?.path || null);
  });
  on("data-changed", refreshAfterDelete);
}

const isVirtual = (p) => p === VIRTUAL;
const roots = () => state.scan?.roots || [];

async function fetchTree(path) {
  if (cache.has(path)) return cache.get(path);
  let data;
  if (isVirtual(path)) {
    const parts = await Promise.all(roots().map((r) => api.tree(state.scan.scan_id, r, 1)));
    const kids = parts.filter((p) => p && p.path);
    data = {
      name: "All scanned folders", path: VIRTUAL, virtual: true, is_dir: true,
      size: kids.reduce((s, k) => s + (k.size || 0), 0),
      file_count: kids.reduce((s, k) => s + (k.file_count || 0), 0),
      children: kids,
    };
  } else {
    data = await api.tree(state.scan.scan_id, path, 2);
  }
  cache.set(path, data);
  return data;
}

function fetchSub(path) {
  if (!subCache.has(path)) {
    const p = api.tree(state.scan.scan_id, path, 1);
    p.catch(() => subCache.delete(path));
    subCache.set(path, p);
  }
  return subCache.get(path);
}

export function resetBrowse() {
  tm?.clear();
  cache.clear();
  subCache.clear();
  state.path = null;
}

/** Top folder of the scan: the single root, or the virtual multi-root view. */
export const topPath = () => (roots().length > 1 ? VIRTUAL : roots()[0]);

/** Navigate to a folder. opts.zoomFrom: child path to zoom out from. opts.zoomInto: key to zoom into first. */
export async function goTo(path, opts = {}) {
  const my = ++navToken;
  const zoom = opts.zoomInto && state.view === "map" ? tm.zoomInto(opts.zoomInto) : Promise.resolve();
  let data;
  try {
    [data] = await Promise.all([fetchTree(path), zoom]);
  } catch (e) {
    if (my !== navToken) return;
    toastError(e, "Couldn't load that folder");
    if (tm.data) tm.render(tm.data);
    else showEmpty(`Couldn't load this folder. ${e.message}`, () => goTo(path, opts));
    return;
  }
  if (my !== navToken) return;
  if (!data || !data.name) {
    toast("That folder isn't part of this scan.", { kind: "error" });
    if (path !== topPath()) return goTo(topPath());
    showEmpty("This scan has no data. It may have been cancelled before anything was measured.");
    return;
  }
  state.path = path;
  if (path === topPath() && state.scan.total_bytes !== data.size) {
    state.scan.total_bytes = data.size; // reflects removals since the scan
    emit("scan", state.scan);
  }
  renderCrumbs(data);
  setFolder(data);
  if (!opts.keepSelection) select(opts.select || null);
  $("#map-view").classList.remove("is-empty");
  if (!data.children || !data.children.length) {
    showEmpty(data.collapsed ? "This folder's contents weren't itemised during the scan." : "This folder is empty.");
  } else {
    $("#map-view .tm-empty")?.remove();
    tm.render(data, { zoomFrom: opts.zoomFrom, fadeIn: !!opts.zoomInto });
    if (opts.select) tm.setSelected(opts.select.path);
  }
  loadSummary(isVirtual(path) ? null : path);
  if (state.view === "list") list.reload();
  onHash();
}

function showEmpty(msg, retry) {
  const v = $("#map-view");
  v.classList.add("is-empty");
  tm.clear();
  $("#map-view .tm-empty")?.remove();
  v.append(h("div", { class: "tm-empty" }, h("p", {}, msg),
    retry ? h("button", { type: "button", class: "btn btn-ghost", onclick: retry }, "Try again")
      : state.path && state.path !== topPath() ? h("button", { type: "button", class: "btn btn-ghost", onclick: up }, "Go up a level") : null));
}

function drill(d, thenSelect) {
  if (!d?.path || !d.is_dir) return;
  if (d.collapsed) {
    toast(`${d.name} was measured as one block, so it can't be opened.`, { detail: "Use List or Reveal in Finder to look inside." });
    return;
  }
  if (d.path === state.path) return;
  return goTo(d.path, { zoomInto: d.path, select: thenSelect || null });
}

export function up() {
  const cur = state.path;
  if (!cur || isVirtual(cur)) return;
  let next;
  if (roots().includes(cur)) {
    if (roots().length < 2) return;
    next = VIRTUAL;
  } else {
    next = parent(cur);
  }
  const from = cache.get(cur);
  return goTo(next, { zoomFrom: cur, select: from ? { ...from, children: undefined } : null });
}

function showInList(d) {
  if (!d?.path) return;
  setView("list");
  if (d.path !== state.path) goTo(d.path);
}

export function setView(v) {
  state.view = v;
  for (const b of $$(".seg [data-view]")) b.setAttribute("aria-selected", String(b.dataset.view === v));
  $("#map-view").hidden = v !== "map";
  $("#list-view").hidden = v !== "list";
  list.setActive(v === "list");
  if (v === "map" && tm.data) tm.render(tm.data);
  onHash();
  emit("view", v);
}

function renderCrumbs(data) {
  const el = $("#crumbs");
  const segs = [];
  const rs = roots();
  if (rs.length > 1) segs.push({ label: "All folders", path: VIRTUAL });
  if (!isVirtual(state.path)) {
    const root = rs.find((r) => state.path === r || state.path.startsWith(r + "/")) || rs[0];
    segs.push({ label: shortPath(root, 30), path: root });
    const rest = state.path.slice(root.length).split("/").filter(Boolean);
    let acc = root;
    for (const part of rest) {
      acc = acc === "/" ? `/${part}` : `${acc}/${part}`;
      segs.push({ label: part, path: acc });
    }
  }
  el.replaceChildren(
    ...segs.flatMap((s, i) => {
      const last = i === segs.length - 1;
      const btn = h("button", {
        type: "button", class: `crumb${last ? " current" : ""}`, "aria-current": last ? "page" : null,
        onclick: () => !last && goTo(s.path, { zoomFrom: childOnPath(s.path) }),
        title: isVirtual(s.path) ? "All scanned folders" : s.path,
      }, s.label);
      return i ? [h("span", { class: "crumb-sep", "aria-hidden": "true" }, "/"), btn] : [btn];
    }),
    h("span", { class: "crumb-size" }, bytes(data.size)),
  );
  el.scrollLeft = el.scrollWidth;
}

/** When jumping up several levels, zoom out from the child that leads to where we were. */
function childOnPath(target) {
  const cur = state.path;
  if (!cur || isVirtual(cur) || cur === target) return null;
  if (isVirtual(target)) return roots().find((r) => cur === r || cur.startsWith(r + "/")) || null;
  if (!cur.startsWith(target === "/" ? "/" : target + "/")) return null;
  const rest = cur.slice(target.length).split("/").filter(Boolean);
  return target === "/" ? `/${rest[0]}` : `${target}/${rest[0]}`;
}

async function refreshAfterDelete({ deleted }) {
  cache.clear();
  subCache.clear();
  if (!state.scan) return;
  try { state.scan = await api.getScan(state.scan.scan_id); emit("scan", state.scan); } catch { /* keep old */ }
  // If the folder we're in was deleted, climb to the nearest surviving ancestor.
  let p = state.path;
  const gone = (x) => deleted.some((d) => x === d || x.startsWith(d + "/"));
  while (p && !isVirtual(p) && gone(p)) p = roots().includes(p) ? topPath() : parent(p);
  select(null);
  await goTo(p || topPath());
}

