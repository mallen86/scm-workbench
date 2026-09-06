/* Repository operation transport facade.
 *
 * Repository ref and state operations are asynchronous in native windows. The
 * native worker returns an operation id first; this facade owns polling that
 * operation to completion. Browser mode retains the existing HTTP routes and
 * response shapes. A selected native bridge is authoritative: native errors
 * are never retried over HTTP.
 */

import { getTauriInvoke } from "./transport.js";

const POLL_INTERVAL_MS = 150;
const OPERATION_TIMEOUT_MS = 100000;
const refsPromises = new Map();

function errorMessage(value, fallback = "Repository operation failed") {
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
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Malformed ${where} response`);
  }
  if (value.ok === false || value.error || (Array.isArray(value.errors) && value.errors.length)) {
    throw new Error(errorMessage(value, `${where} failed`));
  }
  return value;
}

function nativeStart(invoke, method, params) {
  // There is intentionally no HTTP branch here. Once a native capability is
  // selected, this worker operation owns the request and its failures.
  return invoke("wb_rpc", { method, params });
}

function pendingPoll(value) {
  return value && (value.done === false || value.complete === false ||
    value.status === "pending" || value.status === "running" || value.status === "queued");
}

function finishedPoll(value) {
  return value && (value.done === true || value.complete === true ||
    value.status === "done" || value.status === "complete" || value.status === "completed" ||
    Object.prototype.hasOwnProperty.call(value, "result"));
}

async function nativeOperation(invoke, method, params) {
  let started;
  try {
    started = await nativeStart(invoke, method, params);
  } catch (error) {
    throw asError(error, `${method} could not start`);
  }
  if (!started || typeof started !== "object" || Array.isArray(started)) {
    throw new Error(`${method} start returned an invalid response`);
  }
  if (started.ok === false || started.error || (Array.isArray(started.errors) && started.errors.length)) {
    throw new Error(errorMessage(started, `${method} could not start`));
  }
  // The worker's operation envelope carries the id as operation.id; accept
  // the flat form too so the facade remains tolerant of the documented start
  // response while still requiring a real operation identifier.
  const operationId = started.operation_id || started.operation?.id;
  if (typeof operationId !== "string" || !operationId) {
    throw new Error(`${method} start did not return an operation id`);
  }

  const deadline = Date.now() + OPERATION_TIMEOUT_MS;
  while (Date.now() <= deadline) {
    let polled;
    try {
      polled = await invoke("wb_rpc", { method: "repos.poll", params: { operation_id: operationId } });
    } catch (error) {
      throw asError(error, "Repository operation polling failed");
    }
    if (!polled || typeof polled !== "object" || Array.isArray(polled)) {
      throw new Error("Malformed repos.poll response");
    }
    if (Date.now() > deadline) {
      throw new Error(`Repository operation timed out after ${OPERATION_TIMEOUT_MS / 1000} seconds`);
    }
    if (polled.ok === false || polled.error || (Array.isArray(polled.errors) && polled.errors.length)) {
      throw new Error(errorMessage(polled, "Repository operation polling failed"));
    }
    if (pendingPoll(polled)) {
      await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
      continue;
    }
    if (!finishedPoll(polled) && polled.ok !== true) {
      throw new Error("Malformed repos.poll response");
    }
    const result = Object.prototype.hasOwnProperty.call(polled, "result") ? polled.result : polled;
    return applicationResult(result, "Repository operation");
  }
  throw new Error(`Repository operation timed out after ${OPERATION_TIMEOUT_MS / 1000} seconds`);
}

async function browserOperation(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw asError(error, "Repository request failed");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(data, `HTTP ${response.status}`));
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("Malformed repository response");
  }
  // Keep the compatibility route's final body shape, including {ok:false},
  // for callers that display its application-level validation errors.
  return data;
}

function operation(method, params, path, body) {
  const invoke = getTauriInvoke();
  if (invoke) return nativeOperation(invoke, method, params);
  return browserOperation(path, body);
}

/** List tags and releases for one repository. */
export function listRepoRefs(repo) {
  const existing = refsPromises.get(repo);
  if (existing) return existing;
  const promise = operation("repos.refs", { repo }, "/api/repos/refs", { repo });
  refsPromises.set(repo, promise);
  // A failed refs operation must not poison later attempts. Successful
  // promises remain memoized so concurrent/repeated renders do not duplicate
  // the expensive remote ref lookup.
  promise.catch(() => {
    if (refsPromises.get(repo) === promise) refsPromises.delete(repo);
  });
  return promise;
}

/** Persist the selected repository source/ref. */
export function setRepoSource(repo, source) {
  return operation("repos.source.set", { repo, source }, "/api/repos/save", { repo, source });
}

/** Check a repository for a newer source revision. */
export function checkRepo(repo, force = false) {
  return operation("repos.check", { repo, force: !!force }, "/api/repos/check", { repo, force: !!force });
}
