/* Card-back import boundary.
 *
 * Packaged windows use the dedicated no-argument parented picker command.
 * Browser compatibility accepts an explicit source path only. A selected
 * native bridge is authoritative and never retries through HTTP.
 */
import { getTauriInvoke } from "./transport.js";

export function canImportBackImage(scope = typeof window === "undefined" ? null : window) {
  return !!getTauriInvoke(scope);
}

export function importBackImage(path = null) {
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_back_image_import", {});
  if (!path) return Promise.reject(new Error("native picker required"));
  return fetch("/api/back-images/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  }).then(async response => {
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error((result.errors || [result.error || "Import failed"])[0]);
    return result;
  });
}
