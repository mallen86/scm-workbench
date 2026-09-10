/* Cutting template mutation transport.
 *
 * Packaged windows use native RPC. Browser mode keeps an HTTP compatibility
 * route. A selected native bridge owns failures and never retries over HTTP.
 */

import { getTauriInvoke } from "./transport.js";


function errorMessage(data, status) {
  if (typeof data?.error === "string" && data.error) return data.error;
  if (data?.error && typeof data.error.message === "string" && data.error.message) return data.error.message;
  if (Array.isArray(data?.errors) && data.errors.length) return data.errors.join("; ");
  return status ? `HTTP ${status}` : "Template deletion failed";
}


function checkedResult(data, status = 0) {
  if (!data || typeof data !== "object" || Array.isArray(data) || data.ok !== true) {
    throw new Error(errorMessage(data, status));
  }
  return data;
}


/** Permanently delete one DXF cutting template. */
export async function deleteTemplate(path) {
  const params = { path };
  const invoke = getTauriInvoke();
  if (invoke) {
    return checkedResult(await invoke("wb_rpc", { method: "template.delete", params }));
  }

  const response = await fetch("/api/templates/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return checkedResult(data, response.status);
}
