// Thin client for the /api/v1 HTTP API. Every call throws an Error whose
// message is the server's `detail` (or a readable fallback) on failure.

const BASE = "/api/v1";

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function qs(params = {}) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, Array.isArray(v) ? v.join(",") : String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

async function request(method, path, { params, body } = {}) {
  let res;
  try {
    res = await fetch(BASE + path + qs(params), {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("Can't reach the Disk Manager server. Is it still running?", 0);
  }
  if (!res.ok) {
    let msg = `${method} ${path} failed (${res.status})`;
    try {
      const j = await res.json();
      if (typeof j.detail === "string") msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    } catch { /* non-JSON error body */ }
    throw new ApiError(msg, res.status);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

const enc = encodeURIComponent;

export const api = {
  categories: () => request("GET", "/categories"),
  config: () => request("GET", "/config"),
  patchConfig: (patch) => request("PATCH", "/config", { body: patch }),

  listScans: () => request("GET", "/scans"),
  getScan: (id) => request("GET", `/scans/${enc(id)}`),
  startScan: (roots, max_depth) => request("POST", "/scans", { body: { roots, ...(max_depth ? { max_depth } : {}) } }),
  cancelScan: (id) => request("POST", `/scans/${enc(id)}/cancel`),
  deleteScan: (id) => request("DELETE", `/scans/${enc(id)}`),
  streamUrl: (id) => `${BASE}/scans/${enc(id)}/stream`,

  tree: (id, root, depth = 2, max_children = 60) =>
    request("GET", `/scans/${enc(id)}/tree`, { params: { root, depth, max_children } }),
  files: (id, params) => request("GET", `/scans/${enc(id)}/files`, { params }),
  summary: (id, under) => request("GET", `/scans/${enc(id)}/summary`, { params: { under } }),

  staging: (id) => request("GET", `/scans/${enc(id)}/staging`),
  stage: (id, paths) => request("POST", `/scans/${enc(id)}/staging/add`, { body: { paths } }),
  unstage: (id, paths) => request("POST", `/scans/${enc(id)}/staging/remove`, { body: { paths } }),
  clearStaging: (id) => request("DELETE", `/scans/${enc(id)}/staging`),
  execute: (id, permanent) => request("POST", `/scans/${enc(id)}/staging/execute`, { body: { permanent } }),

  thumbnailUrl: (id, path) => `${BASE}/scans/${enc(id)}/preview/thumbnail${qs({ path })}`,
  text: (id, path) => request("GET", `/scans/${enc(id)}/preview/text`, { params: { path } }),
  meta: (id, path) => request("GET", `/scans/${enc(id)}/preview/meta`, { params: { path } }),
  open: (id, path, reveal = true) => request("POST", `/scans/${enc(id)}/preview/open`, { body: { path, reveal } }),
};
