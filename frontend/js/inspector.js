// Details panel: preview + metadata + actions for the selected node, or an
// overview of the current folder when nothing is selected.
import { api } from "./api.js";
import { state, on, isStaged, colorOf, categoryOfExt, select } from "./state.js";
import { bytes, count, items, tilde, dateTime, age, extOf, basename } from "./format.js";
import { $, h } from "./ui.js";
import { catLabel } from "./catbar.js";
import { nav, reveal, openItem, toggleStage, copyPath } from "./actions.js";

let token = 0;
let folder = null; // current folder tree data (for the overview)

const THUMB_CATS = new Set(["image", "video"]);
const THUMB_EXTS = new Set([".pdf"]);
const NO_TEXT_CATS = new Set(["image", "video", "audio", "archive"]);
const TEXT_DATA_EXTS = new Set([".log", ".csv", ".tsv"]);

export function initInspector() {
  on("selection", render);
  on("staging", () => render(state.selection, { keepPreview: true }));
}

export function setFolder(data) {
  folder = data;
  if (!state.selection) render(null);
}

function render(node, { keepPreview = false } = {}) {
  const el = $("#inspector");
  if (!node) return renderFolder(el);
  if (keepPreview && el.dataset.path === (node.path || "") && el.querySelector(".insp-actions")) {
    el.querySelector(".insp-actions").replaceWith(actionsFor(node));
    el.querySelector(".insp-badge")?.classList.toggle("on", isStaged(node.path));
    return;
  }
  const my = ++token;
  el.dataset.path = node.path || "";
  if (node.is_other) return renderOther(el, node);

  const cat = node.is_dir ? null : categoryOfExt(extOf(node.name));
  const preview = h("div", { class: "insp-preview" }, swatch(node, cat));
  const metaList = h("dl", { class: "insp-meta" });
  fillMeta(metaList, node, cat);

  el.replaceChildren(
    preview,
    h("div", { class: "insp-head" },
      h("h2", { class: "insp-name", title: node.name }, node.name || basename(node.path)),
      h("div", { class: "insp-size" }, bytes(node.size),
        h("span", { class: `insp-badge${isStaged(node.path) ? " on" : ""}` }, "Marked for removal")),
      h("button", { class: "insp-path", type: "button", title: "Copy path", onclick: () => copyPath(node) }, tilde(node.path))),
    actionsFor(node),
    metaList,
  );

  const wantThumb = !node.is_dir && (THUMB_CATS.has(cat) || THUMB_EXTS.has(extOf(node.name)));
  if (wantThumb) {
    const img = h("img", { class: "insp-thumb", alt: "", decoding: "async" });
    img.addEventListener("load", () => { if (my === token) { preview.replaceChildren(img); preview.classList.add("has-thumb"); } });
    img.src = api.thumbnailUrl(state.scan.scan_id, node.path);
  }
  if (!node.is_dir && !wantThumb && !NO_TEXT_CATS.has(cat) && (cat !== "data" || TEXT_DATA_EXTS.has(extOf(node.name)))) {
    api.text(state.scan.scan_id, node.path).then((t) => {
      if (my !== token || !t || typeof t.text !== "string") return;
      const pre = h("pre", { class: "insp-text" }, t.text.slice(0, 6000));
      preview.replaceChildren(pre);
      preview.classList.add("has-text");
      if (t.truncated || t.text.length > 6000) preview.append(h("div", { class: "insp-text-more" }, "Preview shows the start of the file"));
    }).catch(() => {});
  }
  api.meta(state.scan.scan_id, node.path).then((m) => {
    if (my !== token || !m) return;
    fillMeta(metaList, { ...node, ...m }, cat);
    if (m.exists === false) {
      el.querySelector(".insp-head").append(h("div", { class: "insp-warn" }, "No longer on disk. It was moved or deleted after the scan."));
    }
  }).catch(() => {});
}

function swatch(node, cat) {
  if (node.is_dir) {
    return h("div", { class: `insp-swatch ${node.collapsed ? "collapsed" : "dir"}` },
      h("span", {}, node.collapsed ? "Not itemised" : items(node.file_count ?? 0, "file")));
  }
  const ext = extOf(node.name);
  return h("div", { class: "insp-swatch", style: `--c:${colorOf(cat)}` }, h("span", {}, ext || catLabel(cat)));
}

function fillMeta(dl, n, cat) {
  const rows = [];
  rows.push(["Kind", n.is_dir ? (n.collapsed ? "Folder, contents not itemised" : "Folder") : `${catLabel(cat)}${extOf(n.name) ? ` (${extOf(n.name)})` : ""}`]);
  rows.push(["On disk", bytes(n.size)]);
  if (n.apparent_size != null && n.apparent_size !== n.size) rows.push(["Logical size", bytes(n.apparent_size)]);
  if (n.is_dir) rows.push(["Files", count(n.file_count)]);
  if (n.mtime) rows.push(["Modified", `${dateTime(n.mtime)}`, age(n.mtime)]);
  if (n.atime) rows.push(["Last opened", `${dateTime(n.atime)}`, age(n.atime)]);
  if (n.birthtime) rows.push(["Created", `${dateTime(n.birthtime)}`, age(n.birthtime)]);
  dl.replaceChildren(...rows.flatMap(([k, v, a]) => [h("dt", {}, k), h("dd", {}, v, a ? h("span", { class: "insp-age" }, a) : null)]));
}

function actionsFor(node) {
  const direct = state.stagedPaths.has(node.path);
  const viaParent = !direct && isStaged(node.path);
  const canDrill = node.is_dir && !node.collapsed && node.path !== state.path;
  return h("div", { class: "insp-actions" },
    h("button", {
      type: "button", class: direct ? "btn btn-ghost" : "btn btn-mark", disabled: viaParent,
      onclick: () => toggleStage(node), title: "S",
    }, direct ? "Unmark" : viaParent ? "Parent folder is marked" : "Mark for removal"),
    h("div", { class: "insp-actions-row" },
      canDrill ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => nav.drill(node), title: "Return" }, "Open folder") : null,
      node.is_dir ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => nav.showInList(node) }, "List contents") : null,
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => reveal(node), title: "R" }, "Reveal in Finder"),
      !node.is_dir ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => openItem(node), title: "O" }, "Open") : null,
    ),
  );
}

function renderOther(el, node) {
  el.replaceChildren(
    h("div", { class: "insp-preview" }, h("div", { class: "insp-swatch other" }, h("span", {}, items(node.count ?? 0)))),
    h("div", { class: "insp-head" },
      h("h2", { class: "insp-name" }, node.name),
      h("div", { class: "insp-size" }, bytes(node.size)),
      h("p", { class: "insp-note" }, "Items too small to draw individually are folded into one block. List them to see each one.")),
    h("div", { class: "insp-actions" },
      h("button", { type: "button", class: "btn btn-primary", onclick: () => nav.showInList({ path: node._parent, is_dir: true }) }, "List these items")),
  );
}

function renderFolder(el) {
  token++;
  el.dataset.path = "";
  if (!folder || !folder.name) { el.replaceChildren(); return; }
  const kids = (folder.children || []).filter((c) => !c.is_other).slice(0, 10);
  const max = kids[0]?.size || 1;
  el.replaceChildren(
    h("div", { class: "insp-head" },
      h("h2", { class: "insp-name" }, folder.name),
      h("div", { class: "insp-size" }, bytes(folder.size)),
      folder.path && !folder.virtual ? h("div", { class: "insp-path static" }, tilde(folder.path)) : null,
      h("p", { class: "insp-note" }, `${items(folder.file_count ?? 0, "file")}. Click a block to see details, double-click a folder to open it.`)),
    h("div", { class: "insp-section-title" }, "Largest here"),
    h("ol", { class: "insp-top" },
      kids.map((c) => h("li", {},
        h("button", { type: "button", class: "insp-top-item", onclick: () => select(c), ondblclick: () => c.is_dir && !c.collapsed && nav.drill(c) },
          h("span", { class: "insp-top-name" }, c.name),
          h("span", { class: "insp-top-size" }, bytes(c.size)),
          h("span", { class: "insp-top-bar", style: `--w:${(c.size / max) * 100}%;--c:${c.is_dir ? "#59626e" : colorOf(categoryOfExt(extOf(c.name)))}` }))))),
  );
}
