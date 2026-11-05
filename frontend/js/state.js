// Shared app state + a tiny event bus. Staging lives on the server; `staging`
// here is only the last state the server returned.

const listeners = new Map();

export const state = {
  categories: {},      // name -> {color, extensions}
  otherColor: "#8a8f98",
  extToCat: new Map(),
  config: null,

  scan: null,          // current scan record from GET /scans/{id}
  path: null,          // folder currently shown
  view: "map",         // "map" | "list"
  selection: null,     // node-ish object {path, name, is_dir, size, ...}
  catFilter: new Set(),// categories highlighted / filtered

  staging: { items: [], total_size: 0, item_count: 0, dir_count: 0, file_count: 0 },
  stagedPaths: new Set(),
};

export function on(evt, fn) {
  if (!listeners.has(evt)) listeners.set(evt, new Set());
  listeners.get(evt).add(fn);
  return () => listeners.get(evt).delete(fn);
}

export function emit(evt, payload) {
  for (const fn of listeners.get(evt) || []) {
    try { fn(payload); } catch (e) { console.error(`listener for ${evt} failed`, e); }
  }
}

export function setCategories(json) {
  state.categories = json.categories || {};
  state.otherColor = json.other_color || state.otherColor;
  state.extToCat = new Map();
  for (const [cat, { extensions }] of Object.entries(state.categories)) {
    for (const ext of extensions) state.extToCat.set(ext, cat);
  }
}

export function categoryOfExt(ext) {
  return state.extToCat.get((ext || "").toLowerCase()) || "other";
}

export function colorOf(cat) {
  return state.categories[cat]?.color || state.otherColor;
}

export function setStaging(s) {
  state.staging = s;
  state.stagedPaths = new Set((s.items || []).map((i) => i.path));
  emit("staging", s);
}

/** True if `path` is staged directly or sits under a staged directory. */
export function isStaged(path) {
  if (!path) return false;
  if (state.stagedPaths.has(path)) return true;
  for (const p of state.stagedPaths) if (path.startsWith(p + "/")) return true;
  return false;
}

export function select(node) {
  state.selection = node;
  emit("selection", node);
}
