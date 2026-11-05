// Formatting helpers. Sizes are decimal (1 kB = 1000 B), matching Finder.

const UNITS = ["B", "kB", "MB", "GB", "TB", "PB"];

export function bytes(n) {
  if (n == null || isNaN(n)) return "–";
  if (n < 1000) return `${n} B`;
  let i = 0;
  let v = n;
  while (v >= 1000 && i < UNITS.length - 1) { v /= 1000; i++; }
  const digits = v >= 100 ? 0 : 1;
  return `${v.toFixed(digits)} ${UNITS[i]}`;
}

const nf = new Intl.NumberFormat("en-US");
export const count = (n) => (n == null ? "–" : nf.format(n));

export function items(n, word = "item") {
  return `${count(n)} ${word}${n === 1 ? "" : "s"}`;
}

const dateFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const dateTimeFmt = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
});

export function date(ts) {
  if (!ts) return "–";
  return dateFmt.format(new Date(ts * 1000));
}
export function dateTime(ts) {
  if (!ts) return "–";
  return dateTimeFmt.format(new Date(ts * 1000));
}

/** "3 min ago", "5 days ago", or an absolute date once it's older than ~a month. */
export function ago(ts) {
  if (!ts) return "–";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) { const d = Math.floor(s / 86400); return `${d} day${d === 1 ? "" : "s"} ago`; }
  return date(ts);
}

/** Age as a compact span: "4 y", "7 mo", "12 d". */
export function age(ts) {
  if (!ts) return "–";
  const d = (Date.now() / 1000 - ts) / 86400;
  if (d >= 365) return `${(d / 365).toFixed(d >= 3650 ? 0 : 1)} y`;
  if (d >= 30) return `${Math.floor(d / 30)} mo`;
  if (d >= 1) return `${Math.floor(d)} d`;
  return "today";
}

export function duration(s) {
  if (s == null) return "–";
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

// --- paths -------------------------------------------------------------

let home = null;
/** Learn the home dir from any absolute path under /Users/<name>. */
export function learnHome(path) {
  if (home || !path) return;
  const m = /^(\/Users\/[^/]+)(\/|$)/.exec(path);
  if (m) home = m[1];
}
export function tilde(path) {
  if (!path) return "";
  if (home && (path === home || path.startsWith(home + "/"))) return "~" + path.slice(home.length);
  return path;
}

export function basename(path) {
  if (!path) return "";
  if (path === "/") return "/";
  const p = path.replace(/\/+$/, "");
  return p.slice(p.lastIndexOf("/") + 1) || p;
}

export function parent(path) {
  if (!path || path === "/") return null;
  const p = path.replace(/\/+$/, "");
  const i = p.lastIndexOf("/");
  return i <= 0 ? "/" : p.slice(0, i);
}

/** Path of `path` relative to `root`, or the tilde form if not under it. */
export function relative(path, root) {
  if (root && path.startsWith(root + "/")) return path.slice(root.length + 1);
  return tilde(path);
}

export function extOf(name) {
  if (!name) return "";
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i).toLowerCase() : "";
}

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Wrap in LRM marks so a path stays left-to-right inside a `direction: rtl` (ellipsis-at-start) box. */
export const ltr = (s) => `\u200E${s}\u200E`;

/** Tilde path, shortened to its last segments when longer than `max` characters. */
export function shortPath(path, max = 32) {
  const t = tilde(path);
  if (t.length <= max) return t;
  const parts = t.split("/").filter(Boolean);
  let out = parts.pop();
  while (parts.length && out.length + parts[parts.length - 1].length + 1 <= max - 2) out = `${parts.pop()}/${out}`;
  return `…/${out}`;
}
