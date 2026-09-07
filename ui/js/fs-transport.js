/* Destructive image-deletion transport.
 *
 * The native bridge is authoritative in packaged windows: a rejected native
 * call is returned to the caller and is never retried over HTTP. Standalone
 * browsers retain the explicit POST /api/fs compatibility route.
 */

import { getTauriInvoke } from "./transport.js";


export function deleteImages(path) {
  const params = { path };
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_rpc", { method: "fs.delete_images", params });
  return fetch("/api/fs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ op: "delete_images", path }),
  }).then(async response => {
    const result = await response.json().catch(() => ({}));
    if (!response.ok && result.ok !== false) {
      return { ok: false, errors: [result.error || `HTTP ${response.status}`], deleted: 0, names: [] };
    }
    return result;
  });
}
