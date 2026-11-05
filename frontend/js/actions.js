// Node actions shared by the map, list, inspector, context menu and keyboard.
// browse.js fills in navigation (drill/showInList); the rest live here.
import { api } from "./api.js";
import { state, isStaged } from "./state.js";
import { tilde } from "./format.js";
import { toast, toastError, contextMenu } from "./ui.js";
import { toggleStage, stagePaths, unstagePaths } from "./staging.js";

export const nav = {
  drill: () => {},
  showInList: () => {},
};

export async function reveal(node) {
  if (!node?.path) return;
  try { await api.open(state.scan.scan_id, node.path, true); }
  catch (e) { toastError(e, "Couldn't reveal in Finder"); }
}

export async function openItem(node) {
  if (!node?.path) return;
  try { await api.open(state.scan.scan_id, node.path, false); }
  catch (e) { toastError(e, "Couldn't open"); }
}

export async function copyPath(node) {
  if (!node?.path) return;
  try {
    await navigator.clipboard.writeText(node.path);
    toast(`Copied ${tilde(node.path)}`);
  } catch {
    toast("Clipboard access was blocked by the browser.", { kind: "error" });
  }
}

export { toggleStage, stagePaths, unstagePaths };

/** Context menu for one node (or several, from the list's multi-select). */
export function nodeMenu(node, e, multi = null) {
  if (multi && multi.length > 1) {
    const anyUnstaged = multi.some((n) => !isStaged(n.path));
    return contextMenu(e.clientX, e.clientY, [
      anyUnstaged
        ? { label: `Mark ${multi.length} items for removal`, kbd: "S", run: () => stagePaths(multi.filter((n) => !isStaged(n.path)).map((n) => n.path)) }
        : { label: `Unmark ${multi.length} items`, kbd: "S", run: () => unstagePaths(multi.map((n) => n.path)) },
    ]);
  }
  if (node.is_other) {
    return contextMenu(e.clientX, e.clientY, [
      { label: "List these items", run: () => nav.showInList({ path: node._parent, is_dir: true }) },
    ]);
  }
  const staged = isStaged(node.path);
  const direct = state.stagedPaths.has(node.path);
  const canDrill = node.is_dir && !node.collapsed;
  contextMenu(e.clientX, e.clientY, [
    canDrill ? { label: "Open folder here", kbd: "↩", run: () => nav.drill(node) } : null,
    node.is_dir ? { label: "List contents", run: () => nav.showInList(node) } : null,
    { label: "Reveal in Finder", kbd: "R", run: () => reveal(node) },
    { label: node.is_dir ? "Open in Finder" : "Open with default app", kbd: "O", run: () => openItem(node) },
    { label: "Copy path", run: () => copyPath(node) },
    "-",
    direct
      ? { label: "Unmark", kbd: "S", run: () => toggleStage(node) }
      : { label: staged ? "Parent folder is marked" : "Mark for removal", kbd: "S", disabled: staged, run: () => toggleStage(node) },
  ].filter(Boolean));
}
