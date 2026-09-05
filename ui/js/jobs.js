/* Shared job transport facade.
 *
 * Job operations are native whenever a callable Tauri bridge is present.  A
 * native error is deliberately not retried over HTTP: packaged windows must
 * never accidentally operate on a different worker.  The browser keeps the
 * existing HTTP API and uses its SSE stream for live output.
 */

import { getTauriInvoke } from "./transport.js";

const DEFAULT_MAX_LINES = 4096;
const NATIVE_POLL_MS = 250;
const RETRY_BASE_MS = 250;
const RETRY_MAX_MS = 5000;
const RETRY_LIMIT = 6;

function nativeCall(method, params) {
  const invoke = getTauriInvoke();
  if (!invoke) return { native: false, value: null };
  // Do not put a fetch fallback in this function.  Once this branch is
  // selected, its rejection is the operation's result.
  return { native: true, value: invoke("wb_rpc", { method, params }) };
}

async function httpJson(path, options, { allowResultError = false } = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  // The start endpoint intentionally returns {ok:false, errors:[...]} for
  // rejected form arguments; callers display that result in the normal UI.
  if (!response.ok && !(allowResultError && data && data.ok === false)) {
    const message = data.error || (data.errors || []).join("; ") || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return data;
}

export function list() {
  const native = nativeCall("jobs.list", {});
  if (native.native) return native.value;
  return httpJson("/api/jobs");
}

export function start(kind, args = {}) {
  const params = { kind, args };
  const native = nativeCall("jobs.start", params);
  if (native.native) return native.value;
  return httpJson("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  }, { allowResultError: true });
}

export function log(jobId, after = 0, maxLines = DEFAULT_MAX_LINES) {
  if (after && typeof after === "object") {
    maxLines = after.max_lines ?? DEFAULT_MAX_LINES;
    after = after.after ?? 0;
  }
  const params = { job_id: jobId, after, max_lines: maxLines };
  const native = nativeCall("jobs.log", params);
  if (native.native) return native.value;
  const query = [];
  if (after) query.push(`after=${encodeURIComponent(after)}`);
  if (maxLines !== DEFAULT_MAX_LINES) query.push(`max_lines=${encodeURIComponent(maxLines)}`);
  return httpJson(`/api/jobs/${encodeURIComponent(jobId)}/log${query.length ? "?" + query.join("&") : ""}`);
}

export function kill(jobId) {
  const native = nativeCall("jobs.kill", { job_id: jobId });
  if (native.native) return native.value;
  return httpJson(`/api/jobs/${encodeURIComponent(jobId)}/kill`, { method: "POST" });
}

function callHandler(sub, name, value) {
  const fn = sub.handlers && sub.handlers[name];
  if (typeof fn === "function") {
    try { fn(value); } catch { /* a UI callback must not stop the poller */ }
  }
}

function makeSubscription(jobId, handlers, after) {
  return {
    jobId,
    handlers: handlers || {},
    cursor: Math.max(0, Number(after) || 0),
    seen: new Set(),
    active: true,
    done: false,
    outageNotified: false,
  };
}

function remember(sub, index) {
  sub.seen.add(index);
  if (sub.seen.size <= 2048) return;
  for (const old of sub.seen) {
    if (old < sub.cursor - 1024) sub.seen.delete(old);
    if (sub.seen.size <= 1024) break;
  }
}

function consumeNativeRow(sub, row) {
  if (!sub.active || !row) return;
  // Report loss before delivering the first available line.  The server's
  // next_seq is authoritative when a bounded response skipped old output.
  if (row.gap) {
    callHandler(sub, "onGap", {
      job_id: sub.jobId,
      after: sub.cursor,
      next_seq: Number(row.next_seq) || sub.cursor,
      gap: true,
      truncated: !!row.truncated,
    });
  }
  for (const line of row.lines || []) {
    const index = Number(line && line.i);
    if (!Number.isInteger(index) || index < sub.cursor || sub.seen.has(index)) continue;
    remember(sub, index);
    sub.cursor = Math.max(sub.cursor, index + 1);
    callHandler(sub, "onLine", { i: index, s: String(line.s ?? "") });
  }
  const next = Number(row.next_seq);
  if (Number.isInteger(next) && next >= sub.cursor) sub.cursor = next;
  if (row.complete && !sub.done) {
    sub.done = true;
    sub.active = false;
    callHandler(sub, "onDone", {
      status: row.status,
      exit_code: row.exit_code,
      cmd: row.cmd,
    });
  }
}

class NativePollHub {
  constructor() {
    this.subscriptions = new Set();
    this.timer = null;
    this.inFlight = null;
    this.retry = 0;
  }

  add(sub) {
    this.subscriptions.add(sub);
    this.schedule(0);
  }

  remove(sub) {
    sub.active = false;
    this.subscriptions.delete(sub);
    if (!this.subscriptions.size && this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  schedule(delay) {
    if (!this.subscriptions.size || this.timer !== null) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.poll();
    }, delay);
  }

  poll() {
    if (this.inFlight || !this.subscriptions.size) return;
    const active = [...this.subscriptions].filter(sub => sub.active);
    if (!active.length) return;
    const cursors = active.map(sub => ({ job_id: sub.jobId, after: sub.cursor }));
    // Assign the promise before invoking so even a synchronously-resolved
    // mock cannot create a second outstanding aggregate request.
    this.inFlight = Promise.resolve().then(() => {
      const native = nativeCall("jobs.poll", { cursors, max_events: 256 });
      if (!native.native) throw new Error("Native job polling is unavailable");
      return native.value;
    }).then(result => {
      if (!result || !Array.isArray(result.jobs) || result.jobs.length !== active.length)
        throw new Error("Invalid native jobs.poll response: row count mismatch");
      result.jobs.forEach((row, index) => {
        const cursor = active[index].cursor;
        if (!row || row.job_id !== active[index].jobId)
          throw new Error("Invalid native jobs.poll response: job order mismatch");
        if (!Array.isArray(row.lines) || !Number.isInteger(row.next_seq) || row.next_seq < cursor)
          throw new Error("Invalid native jobs.poll response: cursor data");
        if (row.lines.some(line => !line || !Number.isInteger(line.i) || typeof line.s !== "string"))
          throw new Error("Invalid native jobs.poll response: line data");
      });
      this.retry = 0;
      for (const sub of active) sub.outageNotified = false;
      result.jobs.forEach((row, index) => {
        const sub = active[index];
        if (sub && this.subscriptions.has(sub)) consumeNativeRow(sub, row);
        if (sub && !sub.active) this.remove(sub);
      });
      this.schedule(NATIVE_POLL_MS);
    }).catch(error => {
      const failure = error instanceof Error ? error : new Error(String(error || "Native job polling failed"));
      const current = [...this.subscriptions].filter(sub => sub.active);
      for (const sub of current) {
        if (!sub.outageNotified) {
          sub.outageNotified = true;
          callHandler(sub, "onError", new Error("Job output is temporarily unavailable; retrying"));
        }
      }
      this.retry++;
      if (this.retry >= RETRY_LIMIT) {
        for (const sub of [...this.subscriptions]) {
          if (!sub.active) continue;
          callHandler(sub, "onError", new Error(`Job output stopped after ${RETRY_LIMIT} failed polls: ${failure.message}`));
          this.remove(sub);
        }
      } else {
        const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** (this.retry - 1)));
        this.schedule(delay);
      }
    }).finally(() => {
      this.inFlight = null;
      if (!this.subscriptions.size && this.timer !== null) {
        clearTimeout(this.timer);
        this.timer = null;
      }
    });
  }
}

const nativeHub = new NativePollHub();

function subscribeNative(jobId, handlers, after) {
  const sub = makeSubscription(jobId, handlers, after);
  nativeHub.add(sub);
  const close = () => nativeHub.remove(sub);
  close.close = close;
  return close;
}

function subscribeBrowser(jobId, handlers, after) {
  const sub = makeSubscription(jobId, handlers, after);
  let source = null;
  let timer = null;
  let retry = 0;

  const close = () => {
    if (!sub.active) return;
    sub.active = false;
    if (source) { source.close(); source = null; }
    if (timer !== null) { clearTimeout(timer); timer = null; }
  };
  close.close = close;

  const open = () => {
    if (!sub.active) return;
    if (typeof EventSource !== "function") {
      callHandler(sub, "onError", new Error("EventSource is unavailable"));
      close();
      return;
    }
    const cursor = sub.cursor;
    const current = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream?after=${encodeURIComponent(cursor)}`);
    source = current;
    current.addEventListener("line", event => {
      if (!sub.active) return;
      let line;
      try { line = JSON.parse(event.data); } catch (error) { callHandler(sub, "onError", error); return; }
      const index = Number(line && line.i);
      if (!Number.isInteger(index) || index < sub.cursor || sub.seen.has(index)) return;
      remember(sub, index);
      sub.cursor = Math.max(sub.cursor, index + 1);
      retry = 0;
      sub.outageNotified = false;
      callHandler(sub, "onLine", { i: index, s: String(line.s ?? "") });
    });
    current.addEventListener("done", event => {
      if (!sub.active || sub.done || source !== current) return;
      let done;
      try { done = JSON.parse(event.data); } catch { done = {}; }
      sub.done = true;
      sub.active = false;
      current.close();
      if (source === current) source = null;
      if (timer !== null) { clearTimeout(timer); timer = null; }
      callHandler(sub, "onDone", done);
    });
    current.onerror = () => {
      if (!sub.active || source !== current) return;
      current.close(); source = null;
      if (!sub.outageNotified) {
        sub.outageNotified = true;
        callHandler(sub, "onError", new Error("Job output is temporarily unavailable; reconnecting"));
      }
      retry++;
      if (retry >= RETRY_LIMIT) {
        callHandler(sub, "onError", new Error(`Job output stopped after ${RETRY_LIMIT} failed reconnects`));
        close();
        return;
      }
      const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** (retry - 1)));
      if (timer === null) timer = setTimeout(() => { timer = null; open(); }, delay);
    };
    current.onopen = () => { retry = 0; sub.outageNotified = false; };
  };
  open();
  return close;
}

export function subscribe(jobId, handlers = {}, after = 0) {
  // The documented forms are subscribe(id, {after, onLine, ...}) and the
  // optional shorthand subscribe(id, onLine, after).  Do not reinterpret a
  // numeric second argument as a different positional API.
  if (typeof handlers === "function") handlers = { onLine: handlers };
  if (!handlers || typeof handlers !== "object") throw new TypeError("job subscription handlers must be an object or function");
  const cursor = handlers.after !== undefined ? handlers.after : after;
  return getTauriInvoke() ? subscribeNative(jobId, handlers, cursor) : subscribeBrowser(jobId, handlers, cursor);
}

export const jobs = Object.freeze({ list, start, log, kill, subscribe });
