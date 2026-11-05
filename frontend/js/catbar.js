// Category breakdown for the current folder (GET /summary). Clicking a
// category toggles it in state.catFilter: the map dims everything else and
// the list filters to it.
import { api } from "./api.js";
import { state, emit, colorOf } from "./state.js";
import { bytes, count, escapeHtml } from "./format.js";
import { $, h, showTip, hideTip } from "./ui.js";

let token = 0;
let last = null;

const LABEL = { image: "Images", video: "Video", audio: "Audio", document: "Documents", code: "Code", archive: "Archives", data: "Data", other: "Other" };
export const catLabel = (c) => LABEL[c] || c;

export async function loadSummary(under) {
  const my = ++token;
  const el = $("#catbar");
  el.classList.add("loading");
  try {
    const s = await api.summary(state.scan.scan_id, under || undefined);
    if (my !== token) return;
    last = s;
    render();
  } catch (e) {
    if (my !== token) return;
    last = null;
    el.replaceChildren(h("div", { class: "catbar-error" }, `Category breakdown unavailable: ${e.message}`));
  } finally {
    if (my === token) el.classList.remove("loading");
  }
}

function render() {
  const el = $("#catbar");
  if (!last) return;
  const col = last.collapsed || { size: 0, dirs: 0, files: 0 };
  // Bytes inside collapsed folders (node_modules, .git, depth-limited dirs) have
  // no per-file rows, so they get their own segment; without it the bar total
  // undercounts the folder.
  const total = (last.total_size || 0) + col.size;
  const cats = [...(last.by_category || [])].filter((c) => c.size > 0).sort((a, b) => b.size - a.size);
  const filter = state.catFilter;

  const bar = h("div", { class: "catbar-track", role: "group", "aria-label": "Space by file type" });
  for (const c of cats) {
    const pct = total ? (c.size / total) * 100 : 0;
    const seg = h("button", {
      type: "button",
      class: `catbar-seg${filter.size && !filter.has(c.category) ? " off" : ""}`,
      style: `flex-grow:${Math.max(pct, 0.15)};--c:${colorOf(c.category)}`,
      "aria-pressed": String(filter.has(c.category)),
      "aria-label": `${catLabel(c.category)}: ${bytes(c.size)}`,
      onclick: () => toggle(c.category),
      onmousemove: (e) => showTip(e.clientX, e.clientY, tipHtml(c, total)),
      onmouseleave: hideTip,
    });
    bar.append(seg);
  }
  if (col.size > 0) {
    bar.append(h("div", {
      class: `catbar-seg collapsed${filter.size ? " off" : ""}`,
      style: `flex-grow:${Math.max((col.size / total) * 100, 0.15)}`,
      "aria-label": `Bundled folders: ${bytes(col.size)}`,
      onmousemove: (e) => showTip(e.clientX, e.clientY, collapsedTip(col, total)),
      onmouseleave: hideTip,
    }));
  }

  const legend = h("div", { class: "catbar-legend" });
  for (const c of cats) {
    const on = filter.has(c.category);
    legend.append(h("button", {
      type: "button",
      class: `legend-item${on ? " on" : ""}${filter.size && !on ? " off" : ""}`,
      style: `--c:${colorOf(c.category)}`,
      "aria-pressed": String(on),
      onclick: () => toggle(c.category),
      title: `Show only ${catLabel(c.category).toLowerCase()}`,
    }, h("span", { class: "legend-dot" }), h("span", { class: "legend-name" }, catLabel(c.category)), h("span", { class: "legend-size" }, bytes(c.size))));
  }
  if (col.size > 0) {
    legend.append(h("span", {
      class: `legend-item static${filter.size ? " off" : ""}`,
      title: "Folders measured as one block (node_modules, .git, … or beyond the depth limit). Their files are not listed individually.",
    }, h("span", { class: "legend-dot collapsed" }), h("span", { class: "legend-name" }, "Bundled folders"), h("span", { class: "legend-size" }, bytes(col.size))));
  }
  if (filter.size) {
    legend.append(h("button", { type: "button", class: "legend-clear", onclick: () => setFilter(new Set()) }, "Show all types"));
  }
  el.replaceChildren(
    h("div", { class: "catbar-head" },
      h("span", { class: "catbar-total" }, `${bytes(total)} in ${count((last.total_files || 0) + col.files)} files`)),
    cats.length ? bar : h("div", { class: "catbar-track empty" }),
    legend,
  );
}

function tipHtml(c, total) {
  const exts = (last.top_extensions || []).filter((x) => catOfTop(x) === c.category).slice(0, 5);
  const pct = total ? ((c.size / total) * 100).toFixed(c.size / total < 0.1 ? 1 : 0) : 0;
  return `<div class="tip-name">${escapeHtml(catLabel(c.category))}</div>
    <div class="tip-size">${bytes(c.size)} <span class="tip-pct">${pct}%</span></div>
    <div class="tip-facts">${count(c.count)} files</div>
    ${exts.length ? `<div class="tip-exts">${exts.map((x) => `<span>${escapeHtml(x.extension || "none")}</span><span>${bytes(x.size)}</span>`).join("")}</div>` : ""}
    <div class="tip-hint">Click to show only ${escapeHtml(catLabel(c.category).toLowerCase())}</div>`;
}

function collapsedTip(col, total) {
  const pct = total ? ((col.size / total) * 100).toFixed(0) : 0;
  return `<div class="tip-name">Bundled folders</div>
    <div class="tip-size">${bytes(col.size)} <span class="tip-pct">${pct}%</span></div>
    <div class="tip-facts">${count(col.files)} files in ${count(col.dirs)} folders measured as one block</div>
    <div class="tip-hint">node_modules, .git, .venv, … and folders past the depth limit. Not split by file type.</div>`;
}

function catOfTop(x) {
  if (x.category) return x.category;
  return state.extToCat.get((x.extension || "").toLowerCase()) || "other";
}

function toggle(cat) {
  const next = new Set(state.catFilter);
  if (next.has(cat)) next.delete(cat); else next.add(cat);
  setFilter(next);
}

export function setFilter(set) {
  state.catFilter = set;
  render();
  emit("filter", set);
}
