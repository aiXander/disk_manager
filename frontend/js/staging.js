// Staging tray. The server owns the staged list; every change goes through
// the API and the tray renders whatever state comes back.
import { api } from "./api.js";
import { state, setStaging, emit, on, isStaged } from "./state.js";
import { bytes, items, tilde, basename, count, ltr } from "./format.js";
import { $, h, toast, toastError, modal } from "./ui.js";

let expanded = false;
let busy = false;

export async function loadStaging() {
  if (!state.scan) return;
  try {
    setStaging(await api.staging(state.scan.scan_id));
  } catch (e) {
    toastError(e, "Couldn't load the removal list");
  }
}

export async function stagePaths(paths) {
  paths = paths.filter(Boolean);
  if (!paths.length || !state.scan) return;
  try {
    setStaging(await api.stage(state.scan.scan_id, paths));
  } catch (e) {
    toastError(e, "Couldn't mark for removal");
  }
}

export async function unstagePaths(paths) {
  paths = paths.filter(Boolean);
  if (!paths.length || !state.scan) return;
  try {
    setStaging(await api.unstage(state.scan.scan_id, paths));
  } catch (e) {
    toastError(e, "Couldn't unmark");
  }
}

/** Stage or unstage a node (directly staged only; children of a staged dir are covered already). */
export function toggleStage(node) {
  if (!node?.path) return;
  if (state.stagedPaths.has(node.path)) return unstagePaths([node.path]);
  if (isStaged(node.path)) {
    toast("Its parent folder is already marked for removal.", { kind: "info" });
    return;
  }
  return stagePaths([node.path]);
}

export function initTray() {
  on("staging", render);
  render();
}

function render() {
  const tray = $("#tray");
  const s = state.staging;
  const n = s.item_count ?? s.items.length;
  tray.classList.toggle("has-items", n > 0);
  tray.classList.toggle("expanded", expanded && n > 0);
  tray.replaceChildren();

  const bar = h("div", { class: "tray-bar" },
    h("div", { class: "tray-summary" },
      h("span", { class: "hatch-swatch", "aria-hidden": "true" }),
      n
        ? h("button", { class: "tray-toggle", type: "button", "aria-expanded": String(expanded), onclick: () => { expanded = !expanded; render(); } },
          h("span", { class: "tray-total" }, bytes(s.total_size)),
          h("span", { class: "tray-count" }, `${items(n)} marked for removal`),
          h("svg", { class: "chev", viewBox: "0 0 10 10", width: 9, height: 9, html: '<path d="M2 6.5 5 3.5 8 6.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' }))
        : h("span", { class: "tray-empty" }, "Nothing marked for removal. Select something and press ", h("kbd", {}, "S"), ", or right-click it."),
    ),
    n ? h("div", { class: "tray-actions" },
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: clearAll, disabled: busy }, "Clear"),
      h("button", { class: "btn btn-mark btn-sm", type: "button", onclick: executeFlow, disabled: busy, title: "Review and remove (⌘⌫)" },
        busy ? "Removing…" : "Review and remove…"),
    ) : null,
  );
  tray.append(bar);

  if (expanded && n) {
    const list = h("ul", { class: "tray-list" });
    const sorted = [...s.items].sort((a, b) => (b.size || 0) - (a.size || 0));
    for (const it of sorted) {
      list.append(h("li", { class: `tray-item${it.covered ? " covered" : ""}`, title: it.covered ? "Inside a folder that's already marked, so it isn't counted twice" : null },
        h("span", { class: `tray-kind ${it.is_dir ? "dir" : "file"}`, "aria-hidden": "true" }),
        h("span", { class: "tray-name", title: it.path }, it.name || basename(it.path)),
        h("span", { class: "tray-path", title: it.path }, ltr(tilde(it.path))),
        h("span", { class: "tray-size" }, bytes(it.size)),
        h("button", { class: "icon-btn tray-remove", type: "button", "aria-label": `Unmark ${it.name}`, title: "Unmark", onclick: () => unstagePaths([it.path]) }, "×"),
      ));
    }
    tray.append(list);
  }
}

async function clearAll() {
  if (!state.scan) return;
  try {
    setStaging(await api.clearStaging(state.scan.scan_id));
  } catch (e) {
    toastError(e, "Couldn't clear the list");
  }
}

export async function executeFlow() {
  const s = state.staging;
  const n = s.item_count ?? s.items.length;
  if (!n || busy || !state.scan) return;
  const permanent = await confirmRemoval(s, n);
  if (permanent === undefined) return;

  busy = true;
  render();
  const dismiss = toast(permanent ? `Deleting ${items(n)}…` : `Moving ${items(n)} to the Trash…`, { timeout: 0 });
  let result;
  try {
    result = await api.execute(state.scan.scan_id, permanent);
  } catch (e) {
    dismiss();
    busy = false;
    render();
    toastError(e, "Removal failed");
    return;
  }
  dismiss();
  busy = false;
  await loadStaging();
  const deleted = result.deleted || [];
  const failed = result.failed || [];
  emit("data-changed", { deleted });

  if (deleted.length) {
    const what = permanent ? `Deleted ${items(deleted.length)}` : `Moved ${items(deleted.length)} to the Trash`;
    toast(`${what}. Freed ${bytes(result.bytes_freed)}.`, { kind: "success", timeout: 6000 });
  }
  if (failed.length) showFailures(failed);
}

function confirmRemoval(s, n) {
  const defaultPermanent = !!state.config?.permanent_delete_default;
  return modal((close) => {
    const sorted = [...s.items].sort((a, b) => (b.size || 0) - (a.size || 0));
    const shown = sorted.slice(0, 6);
    const title = h("h2", { class: "modal-title" });
    const lede = h("p", { class: "modal-lede" });
    const permBox = h("input", { type: "checkbox", id: "perm-toggle" });
    permBox.checked = defaultPermanent;
    const typeInput = h("input", { type: "text", class: "input", id: "perm-type", placeholder: "delete", spellcheck: "false", autocomplete: "off" });
    const typeRow = h("div", { class: "perm-type" },
      h("label", { for: "perm-type" }, "Type ", h("b", {}, "delete"), " to confirm. Permanently deleted files can't be recovered."),
      typeInput);
    const go = h("button", { class: "btn", type: "button" });
    const sync = () => {
      const p = permBox.checked;
      title.textContent = p ? `Permanently delete ${items(n)}?` : `Move ${items(n)} to the Trash?`;
      lede.textContent = p
        ? `This frees ${bytes(s.total_size)} immediately and skips the Trash.`
        : `This frees ${bytes(s.total_size)} once you empty the Trash. Until then you can put anything back from the Trash in Finder.`;
      typeRow.hidden = !p;
      go.className = p ? "btn btn-danger" : "btn btn-mark";
      go.textContent = p ? "Delete permanently" : "Move to Trash";
      go.disabled = p && typeInput.value.trim().toLowerCase() !== "delete";
    };
    permBox.addEventListener("change", () => { sync(); if (permBox.checked) typeInput.focus(); });
    typeInput.addEventListener("input", sync);
    typeInput.addEventListener("keydown", (e) => { if (e.key === "Enter" && !go.disabled) close(true); });
    go.addEventListener("click", () => close(permBox.checked));
    sync();
    return h("div", { class: "modal-inner" },
      title, lede,
      h("ul", { class: "confirm-list" },
        shown.map((it) => h("li", {},
          h("span", { class: `tray-kind ${it.is_dir ? "dir" : "file"}`, "aria-hidden": "true" }),
          h("span", { class: "confirm-path", title: it.path }, ltr(tilde(it.path))),
          h("span", { class: "confirm-size" }, bytes(it.size)))),
        sorted.length > shown.length ? h("li", { class: "confirm-more" }, `and ${count(sorted.length - shown.length)} more`) : null),
      h("label", { class: "check-row" }, permBox, h("span", {}, "Delete permanently instead of moving to the Trash")),
      typeRow,
      h("div", { class: "modal-actions" },
        h("button", { class: "btn btn-ghost", type: "button", onclick: () => close(undefined) }, "Cancel"),
        go),
    );
  }, { label: "Confirm removal" });
}

function showFailures(failed) {
  modal((close) => h("div", { class: "modal-inner" },
    h("h2", { class: "modal-title" }, `${items(failed.length)} couldn't be removed`),
    failed.some((f) => state.stagedPaths.has(f.path))
      ? h("p", { class: "modal-lede" }, "Those still on disk stay marked for removal, so you can fix the cause and try again.")
      : null,
    h("ul", { class: "confirm-list fail-list" },
      failed.map((f) => h("li", {},
        h("span", { class: "confirm-path", title: f.path }, ltr(tilde(f.path))),
        h("span", { class: "fail-err" }, f.error)))),
    h("div", { class: "modal-actions" }, h("button", { class: "btn btn-primary", type: "button", onclick: () => close() }, "OK")),
  ), { label: "Removal failures" });
}
