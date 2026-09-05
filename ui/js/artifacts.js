/* Read-only artifact metadata transport.
 *
 * Packaged Tauri windows use the worker RPC for metadata reads. A browser has
 * the same request through the existing HTTP compatibility routes. Once the
 * native capability is selected, a rejection is the operation's result: it
 * must not be hidden by an HTTP retry.
 */

import { getTauriInvoke } from "./transport.js";


function errorMessage(data, status) {
  return data?.error || (Array.isArray(data?.errors) ? data.errors.join("; ") : "") || `HTTP ${status}`;
}


const EMPTY_FILE_LIST = Object.freeze({
  exists: false,
  items: [],
  truncated: false,
  scanned: 0,
  found: 0,
});


function emptyFileList() {
  return { ...EMPTY_FILE_LIST, items: [] };
}


async function httpJson(path, { missing = null } = {}) {
  const response = await fetch(path);
  const data = await response.json().catch(() => ({}));
  if (response.status === 404 && missing !== null) return missing;
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return data;
}


/** Resolve the cutting template for a paper/card/borderless form state. */
export function resolveTemplate(paper, card, borderless = false) {
  const params = { paper, card, borderless: !!borderless };
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_rpc", { method: "template.resolve", params });

  const path = `/api/template?paper=${encodeURIComponent(paper)}&card=${encodeURIComponent(card)}&borderless=${params.borderless ? 1 : 0}`;
  return httpJson(path);
}


function normalizeListing(result, allowLegacyBrowserShape = false) {
  if (!result || typeof result !== "object") throw new Error("Invalid file listing response");
  if (result.ok === false) throw new Error(errorMessage(result, 0));
  if (result.exists === false) return emptyFileList();
  if (result.exists !== true) {
    if (!allowLegacyBrowserShape || !Array.isArray(result.items)) throw new Error("Invalid file listing response");
    return { ...result, exists: true };
  }
  if (!Array.isArray(result.items)) throw new Error("Invalid file listing response");
  if (result.found !== undefined && (!Number.isInteger(result.found) || result.found < 0)) {
    throw new Error("Invalid file listing response");
  }
  if (!allowLegacyBrowserShape && result.found === undefined) throw new Error("Invalid file listing response");
  return result;
}


/** List directory entries, optionally restricted to image files. */
export async function listFiles(path, imagesOnly = false) {
  const params = { path, images_only: !!imagesOnly };
  const invoke = getTauriInvoke();
  if (invoke) return normalizeListing(await invoke("wb_rpc", { method: "file.list", params }));

  const query = `path=${encodeURIComponent(path)}${imagesOnly ? "&images_only=1" : ""}`;
  const result = await httpJson(`/api/file?${query}`, { missing: emptyFileList() });
  return normalizeListing(result, true);
}
