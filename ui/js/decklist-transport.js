/* Decklist import boundary.
 *
 * Packaged Tauri windows invoke the dedicated no-argument command. Browser
 * callers may use the old path-based compatibility helper explicitly, but no
 * browser picker is exposed by the UI.
 */
import { getTauriInvoke } from "./transport.js";

export function canImportDecklist(scope = typeof window === "undefined" ? null : window) {
  return !!getTauriInvoke(scope);
}

export function importDecklist(path = null) {
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_decklist_import", {});
  if (!path) return Promise.reject(new Error("native picker required"));
  return fetch("/api/decklists/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  }).then(async response => {
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error((result.errors || [result.error || "Import failed"])[0]);
    return result;
  });
}
