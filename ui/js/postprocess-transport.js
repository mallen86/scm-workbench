/* Purpose-specific transport for the Advanced image post-processing registry.
 * A callable native bridge is authoritative for the whole operation. Native
 * failures are returned to the caller and are never retried over HTTP.
 */

import { getTauriInvoke } from "./transport.js";

const MAX_SOURCE_BYTES = 256 * 1024;
const MAX_REQUIREMENTS_BYTES = 8 * 1024;

function nativeCall(method, params = {}) {
  const invoke = getTauriInvoke();
  if (!invoke) return { native: false, value: null };
  return { native: true, value: invoke("wb_rpc", { method, params }) };
}

async function httpJson(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || (data.errors || []).join("; ") || `HTTP ${response.status}`);
  return data;
}

function jsonOptions(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function idPath(id, suffix = "") {
  return `/api/postprocessors/${encodeURIComponent(id)}${suffix}`;
}

export function list() {
  const native = nativeCall("postprocessors.list");
  return native.native ? native.value : httpJson("/api/postprocessors");
}

export function get(id) {
  const native = nativeCall("postprocessors.get", { processor_id: id });
  return native.native ? native.value : httpJson(idPath(id));
}

export function save(payload) {
  const source = String(payload?.source ?? "");
  const requirements = String(payload?.requirements ?? "");
  if (new TextEncoder().encode(source).length > MAX_SOURCE_BYTES) return Promise.reject(new Error("Processor source is too large."));
  if (new TextEncoder().encode(requirements).length > MAX_REQUIREMENTS_BYTES) return Promise.reject(new Error("Requirements are too large."));
  const params = { ...payload, source, requirements };
  const native = nativeCall("postprocessors.save", params);
  return native.native ? native.value : httpJson("/api/postprocessors", jsonOptions("POST", params));
}

export function duplicate(processorId, name, expectedRevision = null) {
  const params = { processor_id: processorId, name, expected_revision: expectedRevision };
  const native = nativeCall("postprocessors.duplicate", params);
  return native.native ? native.value : httpJson(idPath(processorId, "/duplicate"), jsonOptions("POST", params));
}

export function trust(processorId, revisionHash, environmentFingerprint = null) {
  const params = { processor_id: processorId, revision_hash: revisionHash, environment_fingerprint: environmentFingerprint };
  const native = nativeCall("postprocessors.trust", params);
  return native.native ? native.value : httpJson(idPath(processorId, "/trust"), jsonOptions("POST", params));
}

export function remove(processorId, expectedRevision = null) {
  const params = { processor_id: processorId, expected_revision: expectedRevision };
  const native = nativeCall("postprocessors.delete", params);
  return native.native ? native.value : httpJson(idPath(processorId), jsonOptions("DELETE", params));
}

export function status(processorId) {
  const native = nativeCall("postprocessors.status", { processor_id: processorId });
  return native.native ? native.value : httpJson(idPath(processorId, "/status"));
}

export function canImport() {
  return !!getTauriInvoke() || typeof document !== "undefined";
}

/* Native import is a Rust-owned picker. In browser mode the path is never
 * sent: the bounded UTF-8 contents are sent through the ordinary save API. */
export async function importSource({ saveDraft } = {}) {
  const invoke = getTauriInvoke();
  if (invoke) {
    return invoke("wb_postprocessor_import", {});
  }
  if (typeof document === "undefined" || typeof FileReader !== "function")
    throw new Error("Processor import requires a file picker.");
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".py,text/x-python,text/plain";
  const file = await new Promise((resolve, reject) => {
    input.onchange = () => resolve(input.files?.[0] || null);
    input.onerror = () => reject(new Error("Could not open the file picker."));
    input.click();
  });
  if (!file) return null;
  if (file.size > MAX_SOURCE_BYTES) throw new Error("Processor source is too large.");
  const source = await file.text();
  if (new TextEncoder().encode(source).length > MAX_SOURCE_BYTES) throw new Error("Processor source is too large.");
  const payload = { name: file.name.replace(/\.py$/i, "") || "Imported processor", source, requirements: "" };
  return typeof saveDraft === "function" ? saveDraft(payload) : save(payload);
}

export const postprocessors = Object.freeze({ list, get, save, duplicate, trust, delete: remove, status, canImport, importSource });
export { MAX_SOURCE_BYTES, MAX_REQUIREMENTS_BYTES };
