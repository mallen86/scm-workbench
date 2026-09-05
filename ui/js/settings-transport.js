/* Settings write transport facade.
 *
 * Packaged Tauri windows write through the worker RPC. Browser mode keeps the
 * compatibility HTTP route, but the request body remains the settings patch
 * itself (not an RPC-shaped wrapper). A selected native bridge owns the
 * operation: its rejection is never retried over HTTP.
 */

import { getTauriInvoke } from "./transport.js";


function errorMessage(data, status) {
  if (typeof data?.error === "string" && data.error) return data.error;
  if (data?.error && typeof data.error.message === "string" && data.error.message) return data.error.message;
  if (Array.isArray(data?.errors) && data.errors.length) return data.errors.join("; ");
  return status ? `HTTP ${status}` : "Settings write failed";
}


function checkedResult(data, status = 0) {
  if (!data || typeof data !== "object") throw new Error(errorMessage(data, status));
  if (data.ok === false || data.error || (Array.isArray(data.errors) && data.errors.length)) {
    throw new Error(errorMessage(data, status));
  }
  return data;
}


/** Persist a partial settings object through the native or browser boundary. */
export async function setSettings(changes) {
  const invoke = getTauriInvoke();
  if (invoke) {
    // Do not add an HTTP retry here. Once a native capability is present, its
    // result (including a rejection) is the result of the write operation.
    return checkedResult(await invoke("wb_rpc", { method: "settings.set", params: { changes } }));
  }

  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return checkedResult(data, response.status);
}
