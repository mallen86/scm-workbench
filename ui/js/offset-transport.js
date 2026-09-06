/* Offset mutation transport facade.
 *
 * Packaged Tauri windows write through the worker RPC. Browser mode keeps the
 * historical /api/offset request bodies. Once a native bridge is selected,
 * its result (including a rejection) owns the operation; it is never retried
 * over HTTP.
 */

import { getTauriInvoke } from "./transport.js";


function errorMessage(data, status) {
  if (typeof data?.error === "string" && data.error) return data.error;
  if (data?.error && typeof data.error.message === "string" && data.error.message) return data.error.message;
  if (Array.isArray(data?.errors) && data.errors.length) return data.errors.join("; ");
  return status ? `HTTP ${status}` : "Offset write failed";
}


function checkedResult(data, status = 0) {
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(errorMessage(data, status));
  if (data.ok === false || data.error || (Array.isArray(data.errors) && data.errors.length)) {
    throw new Error(errorMessage(data, status));
  }
  return data;
}


function finiteNumber(value, name) {
  if (value === null || value === undefined || String(value).trim() === "") {
    throw new Error(`${name} must be a finite number`);
  }
  const number = Number(typeof value === "string" ? value.trim() : value);
  if (!Number.isFinite(number)) throw new Error(`${name} must be a finite number`);
  return number;
}


function offsetValues(x, y, angle) {
  return {
    x: Math.trunc(finiteNumber(x, "X")),
    y: Math.trunc(finiteNumber(y, "Y")),
    angle: finiteNumber(angle, "Angle"),
  };
}


function offsetSize(size) {
  return size === null || size === undefined || size === "" ? null : String(size);
}


function nativeResult(invoke, method, params) {
  // There is deliberately no fetch fallback in this branch. A callable
  // native capability owns the write, including native rejection.
  return invoke("wb_rpc", { method, params });
}


/** Save a global or per-paper-size offset. */
export async function setOffset({ size = null, x, y, angle } = {}) {
  const values = offsetValues(x, y, angle);
  const nativeParams = { size: offsetSize(size), ...values };
  const invoke = getTauriInvoke();
  if (invoke) return checkedResult(await nativeResult(invoke, "offset.set", nativeParams));

  const requestBody = nativeParams.size === null
    ? values
    : { size: nativeParams.size, ...values };
  const response = await fetch("/api/offset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(requestBody),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return checkedResult(data, response.status);
}


/** Remove a per-paper-size offset row. */
export async function deleteOffset(size) {
  const invoke = getTauriInvoke();
  if (invoke) return checkedResult(await nativeResult(invoke, "offset.delete", { size }));

  const response = await fetch("/api/offset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ size, delete: true }),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return checkedResult(data, response.status);
}
