// Boot, screen switching, URL state and keyboard shortcuts.
import { api } from "./api.js";
import { state, setCategories, setStaging, on, select, isStaged } from "./state.js";
import { bytes, ago, learnHome, tilde, basename } from "./format.js";
import { $, h, toast, toastError, modal, modalOpen, menuOpen, closeMenu } from "./ui.js";
import { initScan, renderHome, watch, scanSwitcher } from "./scan.js";
import { initBrowse, goTo, up, setView, resetBrowse, topPath } from "./browse.js";
import { initInspector } from "./inspector.js";
import { initList, multiSelection, focusSearch, usePreset } from "./list.js";
import { initTray, loadStaging, executeFlow } from "./staging.js";
import { nav, reveal, openItem, toggleStage, stagePaths, unstagePaths } from "./actions.js";
import { openSettings } from "./settings.js";

function showScreen(name) {
  $("#app").dataset.screen = name;
  if (name === "scanning") {
    $(".scan-switch-name").textContent = "Scanning";
    document.title = "Scanning – Disk Manager";
  } else {
    renderSwitch();
  }
  if (name === "home") {
    renderHome();
    history.replaceState(null, "", location.pathname);
    document.title = "Disk Manager";
  }
}

function renderSwitch() {
  const s = state.scan;
  $(".scan-switch-name").textContent = s ? (s.roots || []).map(basename).join(", ") : "Disk Manager";
  $(".scan-switch-meta").textContent = s ? `scanned ${ago(s.finished_at || s.started_at)}` : "";
  $("#scan-switch").title = s ? `${(s.roots || []).map(tilde).join(", ")}, ${bytes(s.total_bytes)}. Click to switch scans.` : "";
  $("#app").toggleAttribute("data-has-scan", !!s);
}

async function openScan(id, path, view) {
  let scan;
  try {
    scan = await api.getScan(id);
  } catch (e) {
    toastError(e, "Couldn't open that scan");
    return showScreen("home");
  }
  if (scan.status === "running") return watch(id, scan.roots);
  (scan.roots || []).forEach(learnHome);
  state.scan = scan;
  state.catFilter = new Set();
  resetBrowse();
  setStaging({ items: [], total_size: 0, item_count: 0, dir_count: 0, file_count: 0 });
  renderSwitch();
  showScreen("browse");
  document.title = `${(scan.roots || []).map((r) => r.split("/").pop() || r).join(", ")} – Disk Manager`;
  if (scan.status !== "complete") {
    toast(`This scan was ${scan.status}, so some folder sizes may be too small.`, { kind: "info", timeout: 7000 });
  }
  loadStaging();
  setView(view === "list" ? "list" : "map");
  await goTo(path || topPath());
}

function closeScan() {
  state.scan = null;
  resetBrowse();
  renderSwitch();
  showScreen("home");
}

function writeHash() {
  if (!state.scan) return;
  const sp = new URLSearchParams({ scan: state.scan.scan_id });
  if (state.path) sp.set("path", state.path);
  if (state.view !== "map") sp.set("view", state.view);
  history.replaceState(null, "", `#${sp}`);
}

function readHash() {
  const sp = new URLSearchParams(location.hash.slice(1));
  return { scan: sp.get("scan"), path: sp.get("path"), view: sp.get("view") };
}

function newScan() {
  showScreen("home");
}

// --- keyboard ------------------------------------------------------------

const SHORTCUTS = [
  ["Double-click, Return", "Open the selected folder"],
  ["Backspace, Esc", "Go up a level (Esc first clears the selection)"],
  ["S", "Mark or unmark the selection for removal"],
  ["R", "Reveal the selection in Finder"],
  ["O", "Open the selection"],
  ["1, 2", "Map view, list view"],
  ["L", "Largest files under this folder"],
  ["/", "Filter the list by name"],
  ["⌘ Backspace", "Review and remove marked items"],
  ["?", "Show these shortcuts"],
];

function showShortcuts() {
  modal((close) => h("div", { class: "modal-inner" },
    h("h2", { class: "modal-title" }, "Keyboard shortcuts"),
    h("dl", { class: "shortcuts" }, SHORTCUTS.flatMap(([k, v]) => [h("dt", {}, ...k.split(", ").flatMap((x, i) => (i ? [" ", h("kbd", {}, x)] : [h("kbd", {}, x)]))), h("dd", {}, v)])),
    h("div", { class: "modal-actions" }, h("button", { type: "button", class: "btn btn-primary", onclick: () => close() }, "Done"))),
  { label: "Keyboard shortcuts" });
}

function onKey(e) {
  if (modalOpen()) return;
  if (menuOpen()) { if (e.key === "Escape") closeMenu(); return; }
  const t = e.target;
  const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  if (typing) return;
  if ($("#app").dataset.screen !== "browse") {
    if (e.key === "?") showShortcuts();
    return;
  }
  const sel = state.selection;
  const k = e.key;
  if ((e.metaKey || e.ctrlKey) && k === "Backspace") { e.preventDefault(); executeFlow(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (k === "Escape") {
    e.preventDefault();
    if (sel) select(null); else up();
  } else if (k === "Backspace") {
    e.preventDefault();
    up();
  } else if (k === "Enter" && sel?.is_dir && !sel.collapsed) {
    e.preventDefault();
    nav.drill(sel);
  } else if (k === "s" || k === "S") {
    const many = state.view === "list" ? multiSelection() : [];
    if (many.length > 1) {
      const unstaged = many.filter((n) => !isStaged(n.path));
      if (unstaged.length) stagePaths(unstaged.map((n) => n.path)); else unstagePaths(many.map((n) => n.path));
    } else if (sel && !sel.is_other) toggleStage(sel);
  } else if ((k === "r" || k === "R") && sel?.path) {
    reveal(sel);
  } else if ((k === "o" || k === "O") && sel?.path) {
    openItem(sel);
  } else if (k === "1") {
    setView("map");
  } else if (k === "2") {
    setView("list");
  } else if (k === "l" || k === "L") {
    setView("list");
    usePreset("files");
  } else if (k === "/") {
    e.preventDefault();
    setView("list");
    focusSearch();
  } else if (k === "?") {
    showShortcuts();
  }
}

// --- boot ------------------------------------------------------------------

async function boot() {
  try {
    const [cats, cfg] = await Promise.all([api.categories(), api.config()]);
    setCategories(cats);
    state.config = cfg;
  } catch (e) {
    $("#app").dataset.screen = "home";
    toast("Couldn't load app settings from the server.", { kind: "error", detail: e.message, timeout: 0 });
  }

  initScan({ openScan, showScreen, closeScan, newScan });
  initBrowse({ onLocation: writeHash });
  initInspector();
  initList();
  initTray();
  on("scan", renderSwitch);

  $("#scan-switch").addEventListener("click", scanSwitcher);
  $("#btn-new-scan").addEventListener("click", newScan);
  $("#btn-back").addEventListener("click", () => { showScreen("browse"); writeHash(); });
  $("#btn-settings").addEventListener("click", openSettings);
  document.addEventListener("keydown", onKey);

  const loc = readHash();
  if (loc.scan) return openScan(loc.scan, loc.path, loc.view);
  // Re-attach to a scan that's still running (e.g. after a page reload).
  try {
    const scans = await api.listScans();
    const running = scans.find((s) => s.status === "running");
    if (running) return watch(running.scan_id, running.roots);
  } catch { /* home will show the error */ }
  showScreen("home");
}

boot();
