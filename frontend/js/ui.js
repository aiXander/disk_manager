// Shared UI primitives: toasts, modal dialogs, context menu, tooltip.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// --- toasts ------------------------------------------------------------

export function toast(message, { kind = "info", detail, timeout = kind === "error" ? 8000 : 4000, action } = {}) {
  const host = $("#toasts");
  const el = h("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" },
    h("div", { class: "toast-body" },
      h("div", { class: "toast-msg" }, message),
      detail ? h("div", { class: "toast-detail" }, detail) : null),
    action ? h("button", { class: "btn btn-ghost btn-sm", onclick: () => { action.run(); dismiss(); } }, action.label) : null,
    h("button", { class: "toast-x", "aria-label": "Dismiss", onclick: () => dismiss() }, "×"),
  );
  host.append(el);
  requestAnimationFrame(() => el.classList.add("in"));
  let timer = timeout ? setTimeout(dismiss, timeout) : null;
  el.addEventListener("mouseenter", () => clearTimeout(timer));
  el.addEventListener("mouseleave", () => { if (timeout) timer = setTimeout(dismiss, timeout); });
  function dismiss() {
    clearTimeout(timer);
    el.classList.remove("in");
    setTimeout(() => el.remove(), 200);
  }
  return dismiss;
}

export const toastError = (err, prefix) =>
  toast(prefix ? `${prefix}` : (err?.message || String(err)), { kind: "error", detail: prefix ? err?.message || String(err) : undefined });

// --- modal -------------------------------------------------------------

let openModal = null;
export const modalOpen = () => !!openModal;

/**
 * Show a modal. `build(close)` returns the content node. Resolves with the
 * value passed to close(); Escape / backdrop click close with `undefined`.
 */
export function modal(build, { wide = false, label = "Dialog" } = {}) {
  return new Promise((resolve) => {
    const prevFocus = document.activeElement;
    const backdrop = h("div", { class: "modal-backdrop" });
    const box = h("div", { class: `modal${wide ? " modal-wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": label });
    backdrop.append(box);
    const close = (val) => {
      if (openModal !== ctrl) return;
      openModal = null;
      backdrop.classList.remove("in");
      setTimeout(() => backdrop.remove(), 160);
      document.removeEventListener("keydown", onKey, true);
      prevFocus?.focus?.();
      resolve(val);
    };
    const ctrl = { close };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(undefined); }
      if (e.key === "Tab") trapFocus(e, box);
    };
    box.append(build(close));
    backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(undefined); });
    document.addEventListener("keydown", onKey, true);
    $("#modal-root").append(backdrop);
    openModal = ctrl;
    requestAnimationFrame(() => {
      backdrop.classList.add("in");
      const target = ["[autofocus]", "input:not([type=checkbox])", "button.btn-primary, button.btn-mark, button.btn-danger", "button"]
        .map((sel) => [...box.querySelectorAll(sel)].find((el) => !el.disabled && el.offsetParent !== null)).find(Boolean);
      target?.focus();
    });
  });
}

function trapFocus(e, box) {
  const f = $$("button, input, select, textarea, a[href], [tabindex]:not([tabindex='-1'])", box).filter((x) => !x.disabled);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/** Simple confirm. Resolves true/false. */
export function confirmDialog({ title, body, confirmLabel = "OK", danger = false, cancelLabel = "Cancel" }) {
  return modal((close) =>
    h("div", { class: "modal-inner" },
      h("h2", { class: "modal-title" }, title),
      body ? h("div", { class: "modal-body" }, body) : null,
      h("div", { class: "modal-actions" },
        h("button", { class: "btn btn-ghost", onclick: () => close(false) }, cancelLabel),
        h("button", { class: danger ? "btn btn-danger" : "btn btn-primary", autofocus: true, onclick: () => close(true) }, confirmLabel),
      ),
    ), { label: title }).then((v) => !!v);
}

// --- context menu ------------------------------------------------------

let menuEl = null;
export function closeMenu() {
  menuEl?.remove();
  menuEl = null;
}
export const menuOpen = () => !!menuEl;

/** items: [{label, run, kbd?, danger?, disabled?} | "-"] */
export function contextMenu(x, y, items) {
  closeMenu();
  menuEl = h("div", { class: "ctx-menu", role: "menu" });
  for (const it of items) {
    if (it === "-") { menuEl.append(h("div", { class: "ctx-sep" })); continue; }
    menuEl.append(h("button", {
      class: `ctx-item${it.danger ? " danger" : ""}`, role: "menuitem", disabled: it.disabled,
      onclick: () => { closeMenu(); it.run(); },
    }, h("span", {}, it.label), it.kbd ? h("kbd", {}, it.kbd) : null));
  }
  document.body.append(menuEl);
  const r = menuEl.getBoundingClientRect();
  menuEl.style.left = `${Math.min(x, innerWidth - r.width - 6)}px`;
  menuEl.style.top = `${Math.min(y, innerHeight - r.height - 6)}px`;
  menuEl.querySelector("button:not([disabled])")?.focus();
  menuEl.addEventListener("keydown", (e) => {
    const btns = $$("button:not([disabled])", menuEl);
    const i = btns.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); btns[(i + 1) % btns.length]?.focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); btns[(i - 1 + btns.length) % btns.length]?.focus(); }
  });
}
document.addEventListener("mousedown", (e) => { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }, true);
window.addEventListener("blur", closeMenu);
window.addEventListener("resize", closeMenu);

// --- tooltip -----------------------------------------------------------

let tipEl = null;
export function showTip(x, y, html) {
  if (!tipEl) {
    tipEl = h("div", { class: "tip", role: "tooltip" });
    document.body.append(tipEl);
  }
  tipEl.innerHTML = html;
  tipEl.style.display = "block";
  const r = tipEl.getBoundingClientRect();
  let left = x + 14, top = y + 16;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 12;
  tipEl.style.left = `${Math.max(8, left)}px`;
  tipEl.style.top = `${Math.max(8, top)}px`;
}
export function hideTip() { if (tipEl) tipEl.style.display = "none"; }

