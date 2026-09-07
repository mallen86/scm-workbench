/* App-update transport facade.
 *
 * Packaged windows use the bounded Python update-operation registry. A normal
 * browser retains the compatibility HTTP routes. Once native invoke is
 * selected, failures stay native and are never retried over HTTP.
 */

import { getTauriInvoke } from "./transport.js";

const POLL_INTERVAL_MS = 150;
const OPERATION_TIMEOUT_MS = 45000;
const checks = new Map();
const notes = new Map();

function errorMessage(value, fallback = "Update operation failed") {
  if (value instanceof Error && value.message) return value.message;
  if (typeof value === "string" && value) return value;
  if (typeof value?.error === "string" && value.error) return value.error;
  if (value?.error && typeof value.error.message === "string" && value.error.message) return value.error.message;
  if (Array.isArray(value?.errors) && value.errors.length) return value.errors.join("; ");
  if (typeof value?.message === "string" && value.message) return value.message;
  return fallback;
}

function asError(value, fallback) {
  return value instanceof Error ? value : new Error(errorMessage(value, fallback));
}

function applicationResult(value, where) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`Malformed ${where} response`);
  if (value.ok === false || value.error || (Array.isArray(value.errors) && value.errors.length)) {
    throw new Error(errorMessage(value, `${where} failed`));
  }
  return value;
}

async function nativeOperation(invoke, method, params) {
  let started;
  try {
    started = await invoke("wb_rpc", { method, params });
  } catch (error) {
    throw asError(error, `${method} could not start`);
  }
  if (!started || typeof started !== "object" || Array.isArray(started)) throw new Error(`${method} start returned an invalid response`);
  if (started.ok === false || started.error || (Array.isArray(started.errors) && started.errors.length)) {
    throw new Error(errorMessage(started, `${method} could not start`));
  }
  const id = started.operation_id || started.operation?.id;
  if (typeof id !== "string" || !id) throw new Error(`${method} start did not return an operation id`);
  const deadline = Date.now() + OPERATION_TIMEOUT_MS;
  while (Date.now() <= deadline) {
    let polled;
    try {
      polled = await invoke("wb_rpc", { method: "updates.poll", params: { id } });
    } catch (error) {
      throw asError(error, "Update operation polling failed");
    }
    if (!polled || typeof polled !== "object" || Array.isArray(polled)) throw new Error("Malformed updates.poll response");
    if (polled.ok === false || polled.error || (Array.isArray(polled.errors) && polled.errors.length)) {
      throw new Error(errorMessage(polled, "Update operation polling failed"));
    }
    if (Date.now() > deadline) throw new Error(`Update operation timed out after ${OPERATION_TIMEOUT_MS / 1000} seconds`);
    if (polled.status === "running" || polled.status === "pending" || polled.status === "queued" ||
        polled.done === false || polled.complete === false) {
      await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
      continue;
    }
    if (polled.status !== "done" && !Object.prototype.hasOwnProperty.call(polled, "result")) throw new Error("Malformed updates.poll response");
    return applicationResult(Object.prototype.hasOwnProperty.call(polled, "result") ? polled.result : polled, "Update operation");
  }
  throw new Error(`Update operation timed out after ${OPERATION_TIMEOUT_MS / 1000} seconds`);
}

async function browserRequest(path, body) {
  let response;
  try {
    response = await fetch(path, body === undefined ? undefined : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (error) {
    throw asError(error, "Update request failed");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(data, `HTTP ${response.status}`));
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Malformed update response");
  return data;
}

async function browserStartRequest() {
  let response;
  try {
    response = await fetch("/api/updates/start", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
    });
  } catch (error) {
    throw asError(error, "Update start request failed");
  }
  const data = await response.json().catch(() => null);
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Malformed updates.start response");
  // The HTTP endpoint deliberately returns its application-level busy result
  // as data; keep that exact shape for native and browser callers alike.
  if (!response.ok && data.ok === false && Array.isArray(data.errors)) return data;
  if (!response.ok) throw new Error(errorMessage(data, `HTTP ${response.status}`));
  return data;
}

function memoized(map, key, work) {
  const existing = map.get(key);
  if (existing) return existing;
  const promise = work();
  map.set(key, promise);
  promise.finally(() => { if (map.get(key) === promise) map.delete(key); }).catch(() => {});
  return promise;
}

export function getUpdates() {
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_rpc", { method: "updates.get", params: {} })
    .then(value => applicationResult(value, "updates.get"))
    .catch(error => { throw asError(error, "updates.get failed"); });
  return browserRequest("/api/updates");
}

export function checkUpdates(force = false) {
  const value = !!force;
  const invoke = getTauriInvoke();
  if (invoke) return memoized(checks, String(value), () => nativeOperation(invoke, "updates.check", { force: value }));
  return browserRequest("/api/updates/check", { force: value });
}

export function getUpdateNotes(tag) {
  const invoke = getTauriInvoke();
  if (invoke) return memoized(notes, String(tag), () => nativeOperation(invoke, "updates.notes", { tag }));
  return browserRequest(`/api/release-notes?tag=${encodeURIComponent(tag)}`);
}

export function startUpdate() {
  const invoke = getTauriInvoke();
  if (invoke) return invoke("wb_rpc", { method: "updates.start", params: {} })
    .then(value => {
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Malformed updates.start response");
      if (value.ok === false && Array.isArray(value.errors)) return value;
      return applicationResult(value, "updates.start");
    })
    .catch(error => { throw asError(error, "updates.start failed"); });
  return browserStartRequest();
}

export { OPERATION_TIMEOUT_MS };
