// Home screen (choose folders, previous scans) and the live scan view.
import { api } from "./api.js";
import { state } from "./state.js";
import { bytes, count, ago, duration, tilde, learnHome, items, ltr, shortPath } from "./format.js";
import { $, h, toast, toastError, confirmDialog, modal } from "./ui.js";

let hooks = { openScan: () => {}, showScreen: () => {} };
let roots = [];          // chips in the scan form
let stream = null;       // current EventSource
let watching = null;     // scan id being watched
let rateSamples = [];
let watchStart = 0;      // client clock fallback when events lack elapsed_s

const DEFAULT_PICKS = ["~", "~/Downloads", "~/Documents", "~/Desktop", "~/Library/Caches"];

export function initScan(h_) {
  hooks = h_;
  const input = $("#scan-path");
  $("#scan-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = input.value.trim();
    if (v) addRoot(v);
    input.value = "";
    if (!roots.length) {
      input.focus();
      toast("Type a folder path first, for example ~/Documents.", { kind: "info" });
      return;
    }
    start([...roots]);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Backspace" && !input.value && roots.length) {
      roots.pop();
      renderChips();
    }
  });
  // A comma turns the typed path into a chip so several folders can be scanned together.
  input.addEventListener("input", () => {
    if (!input.value.includes(",")) return;
    const parts = input.value.split(",");
    const rest = parts.pop();
    parts.map((p) => p.trim()).filter(Boolean).forEach(addRoot);
    input.value = rest.trimStart();
  });
  $("#path-box").addEventListener("click", () => input.focus());
  $("#btn-cancel-scan").addEventListener("click", cancel);
}

function addRoot(p) {
  p = p.replace(/\/+$/, "") || "/";
  if (!roots.includes(p)) roots.push(p);
  renderChips();
}

function renderChips() {
  $("#path-chips").replaceChildren(...roots.map((r, i) =>
    h("span", { class: "path-chip" }, r,
      h("button", { type: "button", class: "chip-x", "aria-label": `Remove ${r}`, onclick: (e) => { e.stopPropagation(); roots.splice(i, 1); renderChips(); } }, "×"))));
  $("#scan-path").placeholder = roots.length ? "Add another folder" : "~/Documents";
  $("#btn-scan").textContent = roots.length > 1 ? `Scan ${roots.length} folders` : "Scan";
}

export async function renderHome() {
  (state.config?.last_scan_paths || []).forEach(learnHome);
  const picks = [...new Set([...(state.config?.last_scan_paths || []).map(tilde), ...DEFAULT_PICKS])].slice(0, 7);
  $("#quick-picks").replaceChildren(...picks.map((p) =>
    h("button", { type: "button", class: "pick", title: p, onclick: () => { addRoot(p); $("#scan-path").focus(); } }, shortPath(p, 30))));
  renderChips();
  await renderHistory($("#scan-list"), $("#history-sub"));
  setTimeout(() => $("#scan-path").focus(), 0);
}

/** Render the previous-scans list into `ul`. Returns the scans. */
async function renderHistory(ul, sub, { onOpen } = {}) {
  ul.replaceChildren(h("li", { class: "scan-row skeleton" }, "Loading scans…"));
  let scans = [];
  try {
    scans = await api.listScans();
  } catch (e) {
    ul.replaceChildren(h("li", { class: "scan-row empty" }, `Couldn't load previous scans: ${e.message}`));
    return [];
  }
  scans.forEach((s) => (s.roots || []).forEach(learnHome));
  if (sub) sub.textContent = scans.length ? items(scans.length, "scan") : "";
  if (!scans.length) {
    ul.replaceChildren(h("li", { class: "scan-row empty" }, "No scans yet. Your first one will show up here so you can reopen it later without rescanning."));
    return scans;
  }
  ul.replaceChildren(...scans.map((s) => scanRow(s, ul, sub, onOpen)));
  return scans;
}

const STATUS_LABEL = { running: "Scanning", complete: null, cancelled: "Cancelled", error: "Failed", interrupted: "Interrupted" };

function scanRow(s, ul, sub, onOpen) {
  const status = STATUS_LABEL[s.status];
  const isCurrent = state.scan?.scan_id === s.scan_id;
  const open = () => { onOpen?.(); s.status === "running" ? watch(s.scan_id, s.roots) : hooks.openScan(s.scan_id); };
  const took = s.finished_at && s.started_at ? duration(s.finished_at - s.started_at) : null;
  return h("li", { class: `scan-row${isCurrent ? " current" : ""}` },
    h("button", { type: "button", class: "scan-open", onclick: open, title: s.error_msg || "Open this scan" },
      h("span", { class: "scan-roots", title: (s.roots || []).map(tilde).join(", ") }, (s.roots || []).map((r) => shortPath(r, 40)).join(", ") || s.scan_id),
      h("span", { class: "scan-size" }, s.total_bytes != null ? bytes(s.total_bytes) : "–"),
      h("span", { class: "scan-meta" },
        h("span", {}, ago(s.started_at)),
        s.total_files != null ? h("span", {}, items(s.total_files, "file")) : null,
        took ? h("span", {}, `took ${took}`) : null,
        status ? h("span", { class: `scan-status st-${s.status}` }, status) : null)),
    h("button", {
      type: "button", class: "icon-btn scan-del", "aria-label": "Delete this scan", title: "Delete scan data",
      onclick: async () => {
        const ok = await confirmDialog({
          title: "Delete this scan?",
          body: h("p", {}, `Removes the saved results for ${(s.roots || []).map(tilde).join(", ")}. Your files aren't touched.`),
          confirmLabel: "Delete scan", danger: true,
        });
        if (!ok) return;
        try {
          await api.deleteScan(s.scan_id);
          toast("Scan deleted.");
          if (state.scan?.scan_id === s.scan_id) hooks.closeScan?.();
          renderHistory(ul, sub, { onOpen });
        } catch (e) { toastError(e, "Couldn't delete the scan"); }
      },
    }, h("svg", { viewBox: "0 0 16 16", width: 13, height: 13, "aria-hidden": "true", html: '<path d="M5.5 2h5M3 4h10M4.5 4l.6 9a1 1 0 0 0 1 1h3.8a1 1 0 0 0 1-1l.6-9" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>' })),
  );
}

/** Modal list of scans, opened from the title-bar switcher. */
export function scanSwitcher() {
  modal((close) => {
    const ul = h("ul", { class: "scan-list" });
    renderHistory(ul, null, { onOpen: () => close() });
    return h("div", { class: "modal-inner" },
      h("div", { class: "modal-title-row" },
        h("h2", { class: "modal-title" }, "Scans"),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { close(); hooks.newScan(); } }, "New scan")),
      ul);
  }, { wide: true, label: "Scans" });
}

async function start(rs) {
  const btn = $("#btn-scan");
  btn.disabled = true;
  try {
    const { scan_id } = await api.startScan(rs, state.config?.max_depth);
    roots = [];
    renderChips();
    watch(scan_id, rs);
  } catch (e) {
    toastError(e, "Couldn't start the scan");
  } finally {
    btn.disabled = false;
  }
}

/** Show the live view for `scanId` and subscribe to its event stream. */
export function watch(scanId, rs = []) {
  closeStream();
  watching = scanId;
  rateSamples = [];
  watchStart = performance.now();
  hooks.showScreen("scanning");
  $("#live-roots").textContent = rs.map(tilde).join(", ");
  render({ files_scanned: 0, dirs_scanned: 0, bytes_seen: 0, elapsed_s: 0, errors: 0, current_path: "", phase: "walking" });
  $("#btn-cancel-scan").disabled = false;
  $("#btn-cancel-scan").textContent = "Cancel scan";
  $("#scanning").classList.remove("done");

  const es = new EventSource(api.streamUrl(scanId));
  stream = es;
  es.addEventListener("progress", (e) => render(JSON.parse(e.data)));
  for (const t of ["complete", "cancelled", "error"]) {
    es.addEventListener(t, (e) => {
      let d = {};
      try { d = JSON.parse(e.data); } catch { /* keep empty */ }
      finish(scanId, t, d);
    });
  }
  // EventSource reconnects by itself; if it gives up, ask the server directly.
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && watching === scanId) {
      api.getScan(scanId).then((s) => {
        if (s.status === "running") setTimeout(() => watching === scanId && watch(scanId, rs), 1500);
        else finish(scanId, s.status === "complete" ? "complete" : s.status, { error: s.error_msg });
      }).catch((err) => { toastError(err, "Lost track of the scan"); stopWatching(); hooks.showScreen("home"); });
    }
  };
}

function render(d) {
  if (d.elapsed_s == null) d = { ...d, elapsed_s: (performance.now() - watchStart) / 1000 };
  learnHome(d.current_path);
  $("#live-bytes").textContent = bytes(d.bytes_seen || 0);
  $("#live-files").textContent = count(d.files_scanned || 0);
  $("#live-dirs").textContent = count(d.dirs_scanned || 0);
  $("#live-elapsed").textContent = duration(d.elapsed_s || 0);
  $("#live-errors").textContent = count(d.errors || 0);
  $("#live-errors").parentElement.classList.toggle("warn", (d.errors || 0) > 0);
  const p = $("#live-path");
  p.textContent = d.current_path ? ltr(tilde(d.current_path)) : "Starting…";
  p.title = d.current_path || "";
  // Rate over the last ~3 s of samples.
  const now = performance.now();
  rateSamples.push([now, d.files_scanned || 0]);
  while (rateSamples.length > 2 && now - rateSamples[0][0] > 3000) rateSamples.shift();
  const [t0, f0] = rateSamples[0];
  const rate = now - t0 > 400 ? ((d.files_scanned - f0) / (now - t0)) * 1000 : null;
  $("#live-rate").textContent = rate != null ? `${count(Math.round(rate))} files/s` : "–";
  $("#live-phase").textContent = d.phase === "finalizing" ? "Adding up folder totals…" : "";
  $("#live-caption").textContent = d.phase === "finalizing" ? "found, adding up folders" : "found so far";
}

function finish(scanId, kind, d) {
  if (watching !== scanId) return;
  stopWatching();
  if (d?.elapsed_s == null) d = { ...d, elapsed_s: (performance.now() - watchStart) / 1000 };
  if (d.files_scanned != null) render(d);
  if (kind === "complete") {
    $("#scanning").classList.add("done");
    const msg = `Scanned ${items(d.files_scanned ?? 0, "file")} in ${duration(d.elapsed_s ?? 0)}.`;
    toast(d.errors ? `${msg} ${count(d.errors)} items couldn't be read.` : msg, { kind: "success" });
    hooks.openScan(scanId);
  } else if (kind === "cancelled") {
    toast("Scan cancelled.", {
      detail: "What was measured so far is kept. Folder sizes will be incomplete.",
      action: { label: "Open anyway", run: () => hooks.openScan(scanId) },
      timeout: 8000,
    });
    hooks.showScreen("home");
  } else {
    toast("The scan failed.", { kind: "error", detail: d?.error || "No details from the server." });
    hooks.showScreen("home");
  }
}

function closeStream() {
  stream?.close();
  stream = null;
}

function stopWatching() {
  closeStream();
  watching = null;
}

async function cancel() {
  if (!watching) return;
  const btn = $("#btn-cancel-scan");
  btn.disabled = true;
  btn.textContent = "Cancelling…";
  try {
    await api.cancelScan(watching);
  } catch (e) {
    toastError(e, "Couldn't cancel the scan");
    btn.disabled = false;
    btn.textContent = "Cancel scan";
  }
}

