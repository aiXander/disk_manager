// Nested, cushion-shaded treemap of one folder.
//
// The folder is fetched two levels deep; level-1 folders are frames with a
// header and their children are tiles inside. Folder tiles that are still
// big on screen are then refined progressively: their contents are fetched
// (depth 1) and drawn inside them, recursively, until every folder that has
// room shows what's in it. Files are coloured by category, collapsed folders
// (node_modules etc.) are ribbed blocks, "N smaller items" buckets are dotted.
/* global d3 */
import { state, categoryOfExt, colorOf, isStaged } from "./state.js";
import { bytes, items, tilde, extOf, escapeHtml } from "./format.js";
import { showTip, hideTip, menuOpen } from "./ui.js";

const HEADER = 19;
const REFINE_MIN_W = 46;
const REFINE_MIN_H = 40;
const REFINE_BUDGET = 90;     // max sub-folder fetches per render
const REFINE_CONCURRENCY = 6;
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const ZOOM_MS = reduceMotion ? 0 : 320;

const measureCtx = document.createElement("canvas").getContext("2d");
function fit(text, maxW, font) {
  measureCtx.font = font;
  if (measureCtx.measureText(text).width <= maxW) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (measureCtx.measureText(text.slice(0, mid) + "…").width <= maxW) lo = mid; else hi = mid - 1;
  }
  return lo < 2 ? "" : text.slice(0, lo) + "…";
}

const SYS = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif";
const FONT_LABEL = `500 11px ${SYS}`;
const FONT_SIZE = `400 10.5px ${SYS}`;
const FONT_HEAD = `600 11px ${SYS}`;

function nodeKind(d) {
  if (d.is_other) return "other";
  if (d.is_dir && d.collapsed) return "collapsed";
  if (d.is_dir) return "dir";
  return "file";
}

function nodeCategory(d) {
  return d.is_dir || d.is_other ? null : categoryOfExt(extOf(d.name));
}

const keyOf = (d) => d.path || `other:${d._parent || ""}:${d.name}`;
const W_ = (n) => n.x1 - n.x0;
const H_ = (n) => n.y1 - n.y0;
const hasHeader = (n) => W_(n) > 44 && H_(n) > HEADER + 14;

export class Treemap {
  /** cb: onSelect(d), onDrill(d, thenSelect), onContext(d, e), onOther(d), fetchSub(path) -> Promise<tree> */
  constructor(container, cb) {
    this.el = container;
    this.cb = cb;
    this.data = null;
    this.svg = d3.select(container).append("svg").attr("class", "treemap").attr("role", "img");
    this.defs = this.svg.append("defs");
    this.layer = null;
    this.gen = 0;
    this.rects = new Map(); // key -> node {x0,y0,x1,y1,data,parent,depth}
    this.selectedKey = null;
    this.buildStaticDefs();
    this.bindEvents();
    let t = null;
    this.lastSize = null;
    this.ro = new ResizeObserver(() => {
      clearTimeout(t);
      t = setTimeout(() => {
        const s = this.size().join("x");
        if (this.data && s !== this.lastSize && !this.el.closest("[hidden]")) this.render(this.data);
      }, 80);
    });
    this.ro.observe(container);
  }

  clear() {
    this.gen++;
    this.data = null;
    this.layer?.remove();
    this.layer = null;
    this.gSel = null;
    this.rects.clear();
    this.selectedKey = null;
  }

  size() {
    const r = this.el.getBoundingClientRect();
    return [Math.max(50, Math.floor(r.width)), Math.max(50, Math.floor(r.height))];
  }

  buildStaticDefs() {
    const d = this.defs;
    const hatch = d.append("pattern").attr("id", "tm-hatch").attr("patternUnits", "userSpaceOnUse")
      .attr("width", 7).attr("height", 7).attr("patternTransform", "rotate(45)");
    hatch.append("rect").attr("width", 7).attr("height", 7).attr("fill", "rgba(24,8,16,0.3)");
    hatch.append("rect").attr("width", 1.8).attr("height", 7).attr("fill", "#ff7eb6").attr("opacity", 0.85);

    const dots = d.append("pattern").attr("id", "tm-dots").attr("patternUnits", "userSpaceOnUse").attr("width", 5).attr("height", 5);
    dots.append("rect").attr("width", 5).attr("height", 5).attr("fill", "#30353d");
    dots.append("circle").attr("cx", 2.5).attr("cy", 2.5).attr("r", 0.9).attr("fill", "#59616c");

    const ribs = d.append("pattern").attr("id", "tm-ribs").attr("patternUnits", "userSpaceOnUse").attr("width", 4).attr("height", 4);
    ribs.append("rect").attr("width", 4).attr("height", 4).attr("fill", "#2a2f37");
    ribs.append("rect").attr("width", 4).attr("height", 1).attr("fill", "#373d47");

    const dir = d.append("radialGradient").attr("id", "tm-dir").attr("cx", "35%").attr("cy", "30%").attr("r", "85%");
    dir.append("stop").attr("offset", "0%").attr("stop-color", "#555e6a");
    dir.append("stop").attr("offset", "100%").attr("stop-color", "#343a43");
  }

  buildCategoryDefs() {
    this.defs.selectAll(".cg").remove();
    for (const c of [...Object.keys(state.categories), "other"]) {
      const base = d3.color(colorOf(c));
      const g = this.defs.append("radialGradient").attr("class", "cg").attr("id", `tm-c-${c}`)
        .attr("cx", "35%").attr("cy", "30%").attr("r", "90%");
      g.append("stop").attr("offset", "0%").attr("stop-color", base.brighter(0.45).formatHex());
      g.append("stop").attr("offset", "55%").attr("stop-color", base.formatHex());
      g.append("stop").attr("offset", "100%").attr("stop-color", base.darker(0.75).formatHex());
    }
    this.catDefsBuilt = true;
  }

  /** Lay out `data` into the rect [x0,y0,x1,y1]; depth 0 is the container itself. */
  layoutInto(data, x0, y0, x1, y1) {
    const root = d3.hierarchy(data, (d) => (d.children && d.children.length ? d.children : null))
      .sum((d) => (d.children && d.children.length ? 0 : Math.max(0, d.size || 0)))
      .sort((a, b) => b.value - a.value);
    root.each((n) => { if (n.data.is_other && n.parent) n.data._parent = n.parent.data.path; });
    d3.treemap()
      .size([Math.max(1, x1 - x0), Math.max(1, y1 - y0)])
      .tile(d3.treemapSquarify.ratio(1.25))
      .paddingInner((n) => (n.depth === 0 ? 3 : 1))
      .paddingOuter((n) => (n.depth === 0 ? 0 : 2))
      .paddingTop((n) => (n.depth === 0 ? 0 : hasHeader(n) ? HEADER : 2))
      .round(true)(root);
    root.each((n) => { n.x0 += x0; n.x1 += x0; n.y0 += y0; n.y1 += y0; });
    return root;
  }

  /** Draw `data`. opts.zoomFrom: key of a child to zoom out from; opts.fadeIn. */
  render(data, opts = {}) {
    if (!this.catDefsBuilt) this.buildCategoryDefs();
    this.data = data;
    this.gen++;
    hideTip();
    const [W, H] = this.size();
    this.lastSize = `${W}x${H}`;
    const root = this.layoutInto(data, 0, 0, W, H);
    this.svg.attr("viewBox", `0 0 ${W} ${H}`).attr("width", W).attr("height", H)
      .attr("aria-label", `Treemap of ${data.name}`);
    this.rects.clear();
    root.each((n) => this.rects.set(keyOf(n.data), n));

    this.layer?.remove();
    const layer = this.svg.append("g").attr("class", "tm-layer");
    this.layer = layer;
    this.gFrames = layer.append("g").attr("class", "tm-frames");
    this.gLeaves = layer.append("g").attr("class", "tm-leaves");
    this.gOver = layer.append("g").attr("class", "tm-overlays");
    this.gSel = layer.append("rect").attr("class", "tm-sel").attr("display", "none");

    const nodes = root.descendants().filter((n) => n.depth > 0 && W_(n) >= 1 && H_(n) >= 1);
    for (const n of nodes) (n.children ? this.drawFrame(n, this.gFrames) : this.drawLeaf(n, this.gLeaves));
    this.applyDecorations();

    const from = opts.zoomFrom && this.rects.get(opts.zoomFrom);
    if (from && ZOOM_MS) {
      layer.attr("transform", zoomTransform(from, W, H)).style("opacity", 0.4)
        .transition().duration(ZOOM_MS).ease(d3.easeCubicInOut)
        .attr("transform", "translate(0,0) scale(1,1)").style("opacity", 1)
        .on("end", () => this.refine(nodes));
    } else {
      if (opts.fadeIn && ZOOM_MS) layer.style("opacity", 0).transition().duration(160).style("opacity", 1);
      this.refine(nodes);
    }
  }

  // ---- drawing -----------------------------------------------------------

  drawFrame(n, parentG) {
    const g = parentG.append("g").datum(n).attr("class", "tm-frame").attr("transform", `translate(${n.x0},${n.y0})`);
    g.append("rect").attr("class", "tm-frame-bg").attr("width", W_(n)).attr("height", H_(n)).attr("rx", 3);
    if (!hasHeader(n)) return g;
    const w = W_(n);
    const head = g.append("g").attr("class", "tm-head").datum(n);
    head.append("rect").attr("class", "tm-head-hit").attr("width", w).attr("height", HEADER);
    const sizeText = bytes(n.data.size);
    measureCtx.font = FONT_SIZE;
    const showSize = w > 110;
    const sw = showSize ? measureCtx.measureText(sizeText).width + 10 : 0;
    head.append("text").attr("class", "tm-head-name").attr("x", 6).attr("y", 13).text(fit(n.data.name, w - 12 - sw, FONT_HEAD));
    if (showSize) head.append("text").attr("class", "tm-head-size").attr("x", w - 6).attr("y", 13).attr("text-anchor", "end").text(sizeText);
    return g;
  }

  drawLeaf(n, parentG) {
    const w = W_(n), h = H_(n);
    const g = parentG.append("g").datum(n)
      .attr("class", `tm-leaf k-${nodeKind(n.data)}`)
      .attr("transform", `translate(${n.x0},${n.y0})`);
    g.append("rect").attr("class", "tm-tile")
      .attr("width", w).attr("height", h)
      .attr("rx", w > 8 && h > 8 ? 2 : 0)
      .attr("fill", fillFor(n.data));
    if (w >= 38 && h >= 16) {
      const label = fit(n.data.name, w - 10, FONT_LABEL);
      if (label) {
        g.append("text").attr("class", "tm-label").attr("x", 5).attr("y", 13).text(label);
        if (h >= 30) g.append("text").attr("class", "tm-sub").attr("x", 5).attr("y", 26).text(fit(bytes(n.data.size), w - 10, FONT_SIZE));
        if (n.data.collapsed && h >= 44 && w >= 80) g.append("text").attr("class", "tm-sub tm-note").attr("x", 5).attr("y", 39).text(fit("contents not itemised", w - 10, FONT_SIZE));
      }
    }
    n._el = g;
    return g;
  }

  // ---- progressive refinement ---------------------------------------------

  /** Fill big folder tiles with their contents, biggest first, recursively. */
  refine(initial) {
    if (!this.cb.fetchSub) return;
    const gen = this.gen;
    const big = (n) => !n.children && n.data.is_dir && !n.data.collapsed && n.data.path && W_(n) >= REFINE_MIN_W && H_(n) >= REFINE_MIN_H;
    const queue = initial.filter(big).sort((a, b) => W_(b) * H_(b) - W_(a) * H_(a));
    let budget = REFINE_BUDGET;
    let running = 0;
    const pump = () => {
      while (running < REFINE_CONCURRENCY && queue.length && budget > 0 && gen === this.gen) {
        const n = queue.shift();
        budget--;
        running++;
        this.cb.fetchSub(n.data.path).then((sub) => {
          if (gen !== this.gen || !sub?.children?.length) return;
          const added = this.expand(n, sub);
          for (const c of added.filter(big)) queue.push(c);
          queue.sort((a, b) => W_(b) * H_(b) - W_(a) * H_(a));
        }).catch(() => {}).finally(() => { running--; pump(); });
      }
    };
    pump();
  }

  /** Replace leaf `n` with a frame containing `sub`'s children. Returns the new nodes. */
  expand(n, sub) {
    const inner = { x0: n.x0, y0: n.y0, x1: n.x1, y1: n.y1 };
    const headed = hasHeader(n);
    const pad = 2;
    const sroot = this.layoutInto({ ...sub, children: sub.children },
      inner.x0 + pad, inner.y0 + (headed ? HEADER : pad), inner.x1 - pad, inner.y1 - pad);
    const kids = (sroot.children || []).filter((c) => W_(c) >= 1 && H_(c) >= 1);
    if (!kids.length) return [];
    n._el?.remove();
    n._el = null;
    n.children = kids;
    const frame = this.drawFrame(n, this.gFrames);
    const added = [];
    const fresh = this.gLeaves.append("g").attr("class", "tm-refined");
    sroot.each((c) => {
      if (c === sroot) return;
      c.depth = n.depth + c.depth;
      if (c.parent === sroot) c.parent = n;
      if (W_(c) < 1 || H_(c) < 1) return;
      this.rects.set(keyOf(c.data), c);
      if (c.children) this.drawFrame(c, this.gFrames); else this.drawLeaf(c, fresh);
      added.push(c);
    });
    if (ZOOM_MS) {
      fresh.style("opacity", 0).transition().duration(180).style("opacity", 1);
      frame.style("opacity", 0).transition().duration(180).style("opacity", 1);
    }
    this.applyDecorations();
    return added;
  }

  // ---- interaction ---------------------------------------------------------

  bindEvents() {
    const svgEl = this.svg.node();
    const hit = (e) => {
      const el = e.target.closest(".tm-leaf, .tm-head");
      return el ? d3.select(el).datum() : null;
    };
    svgEl.addEventListener("mousemove", (e) => {
      const n = hit(e);
      if (n && !menuOpen()) this.tip(e, n.data); else hideTip();
    });
    svgEl.addEventListener("mouseleave", hideTip);
    svgEl.addEventListener("click", (e) => this.select(hit(e)?.data || null));
    svgEl.addEventListener("dblclick", (e) => {
      const n = hit(e);
      if (!n) return;
      const d = n.data;
      if (d.is_other) return this.cb.onOther(d);
      if (d.is_dir && !d.collapsed) return this.cb.onDrill(d);
      if (d.is_dir && d.collapsed) return this.cb.onDrill(d);
      // A file: zoom into the folder that contains it.
      if (n.parent && n.parent.depth > 0) this.cb.onDrill(n.parent.data, d);
    });
    svgEl.addEventListener("contextmenu", (e) => {
      const n = hit(e);
      if (!n) return;
      e.preventDefault();
      hideTip();
      this.select(n.data);
      this.cb.onContext(n.data, e);
    });
  }

  /** Zoom the current layer into `key`'s rect; resolves when done. */
  zoomInto(key) {
    const n = this.rects.get(key);
    if (!n || !ZOOM_MS || !this.layer) return Promise.resolve();
    hideTip();
    this.gen++; // stop refinement of the outgoing layer
    const [W, H] = this.size();
    return new Promise((resolve) => {
      this.layer.interrupt().transition().duration(ZOOM_MS).ease(d3.easeCubicInOut)
        .attr("transform", zoomTransform(n, W, H)).style("opacity", 0.55)
        .on("end", resolve).on("interrupt", resolve);
    });
  }

  select(d) {
    this.selectedKey = d ? keyOf(d) : null;
    this.drawSelection();
    this.cb.onSelect(d);
  }

  /** Mark a key as selected without notifying (selection made elsewhere). */
  setSelected(key) {
    this.selectedKey = key;
    this.drawSelection();
  }

  drawSelection() {
    if (!this.gSel) return;
    const n = this.selectedKey && this.rects.get(this.selectedKey);
    if (!n || n.depth === 0) { this.gSel.attr("display", "none"); return; }
    this.gSel.attr("display", null).attr("x", n.x0 + 0.75).attr("y", n.y0 + 0.75)
      .attr("width", Math.max(0, W_(n) - 1.5)).attr("height", Math.max(0, H_(n) - 1.5)).attr("rx", 3);
    this.gSel.raise();
  }

  /** Staged hatching + category dimming; cheap enough to redo on every change. */
  applyDecorations() {
    if (!this.layer) return;
    const filter = state.catFilter;
    const staged = [];
    for (const n of this.rects.values()) {
      if (n.depth === 0 || !n.data.path) continue;
      if (isStaged(n.data.path) && !(n.parent && n.parent.depth > 0 && isStaged(n.parent.data.path))) staged.push(n);
    }
    this.gOver.selectAll("rect").data(staged, (n) => keyOf(n.data)).join("rect")
      .attr("class", "tm-staged")
      .attr("x", (n) => n.x0).attr("y", (n) => n.y0)
      .attr("width", W_).attr("height", H_).attr("rx", 3);
    this.gOver.raise();
    this.layer.selectAll(".tm-leaf").classed("dim", (n) => {
      if (!filter.size) return false;
      const c = nodeCategory(n.data);
      return !c || !filter.has(c);
    });
    this.drawSelection();
  }

  tip(e, d) {
    const kind = nodeKind(d);
    const cat = nodeCategory(d);
    const lines = [`<div class="tip-name">${escapeHtml(d.name)}</div>`, `<div class="tip-size">${bytes(d.size)}</div>`];
    const facts = [];
    if (kind === "dir" || kind === "collapsed") facts.push(items(d.file_count ?? 0, "file"));
    if (kind === "other") facts.push(`${items(d.count ?? 0)} folded together`);
    if (cat) facts.push(cat === "other" ? (extOf(d.name) || "no extension") : cat);
    if (d.apparent_size && d.size && Math.abs(d.apparent_size - d.size) / d.size > 0.15) facts.push(`${bytes(d.apparent_size)} logical`);
    if (facts.length) lines.push(`<div class="tip-facts">${facts.map(escapeHtml).join("<span class='sep'></span>")}</div>`);
    if (d.path) lines.push(`<div class="tip-path">${escapeHtml(tilde(d.path))}</div>`);
    const staged = isStaged(d.path);
    const hint = staged ? "Marked for removal"
      : kind === "collapsed" ? "Contents not itemised. Measured as one block."
      : kind === "dir" ? "Double-click to open"
      : kind === "other" ? "Double-click to list them"
      : null;
    if (hint) lines.push(`<div class="tip-hint${staged ? " staged" : ""}">${hint}</div>`);
    showTip(e.clientX, e.clientY, lines.join(""));
  }
}

function fillFor(d) {
  switch (nodeKind(d)) {
    case "other": return "url(#tm-dots)";
    case "collapsed": return "url(#tm-ribs)";
    case "dir": return "url(#tm-dir)";
    default: return `url(#tm-c-${nodeCategory(d)})`;
  }
}

function zoomTransform(n, W, H) {
  const kx = W / Math.max(1, W_(n));
  const ky = H / Math.max(1, H_(n));
  return `translate(${-n.x0 * kx},${-n.y0 * ky}) scale(${kx},${ky})`;
}
