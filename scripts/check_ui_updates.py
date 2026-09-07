#!/usr/bin/env python3
"""Executable contract for the native/browser app-update transport."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "updates-transport.js"
PAGE = UI / "pages" / "settings.js"


def fail(message):
    print(f"FAIL: {message}")
    return 1


def main():
    if not FACADE.is_file():
        return fail("updates transport facade is missing")
    source = FACADE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    for marker in (
        "export function getUpdates()", "export function checkUpdates(force = false)",
        "export function getUpdateNotes(tag)", "export function startUpdate()",
        '"updates.get"', '"updates.check"', '"updates.notes"',
        '"updates.poll"', '"updates.start"',
        'params: { id }', "OPERATION_TIMEOUT_MS = 45000",
        "encodeURIComponent(tag)", "never retried over HTTP",
    ):
        if marker not in source:
            return fail(f"updates facade is missing {marker}")
    if 'import { getUpdates, checkUpdates, getUpdateNotes, startUpdate as startUpdateRequest } from "../updates-transport.js";' not in page:
        return fail("settings page does not import the updates facade")
    for marker in ("getUpdates()", "checkUpdates(!fresh)", "getUpdateNotes(tag)", "startUpdateRequest()",
                   "let checkPending = false", "checkPending = true", "checkPending = false",
                   "if (checkPending || st.checking)"):
        if marker not in page:
            return fail(f"settings page is missing {marker}")
    app = (UI / "app.js").read_text(encoding="utf-8")
    for marker in ('import { getTauriInvoke } from "./transport.js";',
                   'import { getUpdates } from "./updates-transport.js";',
                   "if (getTauriInvoke()) Promise.resolve().then(() => getUpdates()).catch(() => {});"):
        if marker not in app:
            return fail(f"packaged startup update read is missing {marker}")
    for path in UI.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        if path != FACADE and any(route in text for route in ("/api/updates", "/api/release-notes")):
            return fail(f"{path.relative_to(ROOT)} bypasses the updates facade")
        if path != FACADE and re.search(r'(?:api|fetch)\s*\(\s*["\x27`]\s*/api/(?:updates|release-notes)', text):
            return fail(f"{path.relative_to(ROOT)} directly calls an update HTTP route")
    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the updates transport contract")
    script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const transport = dataUrl(`export function getTauriInvoke() {
  const i = globalThis.window && globalThis.window.__TAURI_INTERNALS__;
  return i && typeof i.invoke === "function" ? i.invoke.bind(i) : null;
}`);
const updates = await import(dataUrl(source.replace('from "./transport.js"', `from "${transport}"`)));
const fail = message => { throw new Error(message); };
let now = 0;
const realNow = Date.now, realTimeout = globalThis.setTimeout;
Date.now = () => now;
globalThis.setTimeout = (fn, delay) => { now += delay; return realTimeout(fn, 0); };
const calls = [];
let polls = 0;
const internals = { invoke(method, rpc) {
  calls.push({ method, rpc });
  if (rpc.method === "updates.get") return Promise.resolve({ current: "1", repo: "o/r", packaged: true, bundle: "", state: {} });
  if (rpc.method === "updates.check") return Promise.resolve({ operation: { id: "check-id", status: "running" } });
  if (rpc.method === "updates.notes") return Promise.resolve({ operation_id: "notes-id" });
  if (rpc.method === "updates.start") return Promise.resolve({ ok: true, job: { id: "job" } });
  if (rpc.method === "updates.poll") {
    polls++;
    return Promise.resolve(polls === 1 ? { ok: true, status: "running" } : { ok: true, status: "done", result: rpc.params.id === "notes-id" ? { ok: true, tag: "v2", body: "" } : { ok: true, state: { status: "up-to-date" } } });
  }
  return Promise.reject(new Error("unexpected native call"));
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
let fetches = 0;
globalThis.fetch = () => { fetches++; return Promise.reject(new Error("HTTP fallback")); };
const a = updates.checkUpdates(false), b = updates.checkUpdates(false);
if (a !== b) fail("concurrent native checks were not memoized");
if (!(await a).state || polls !== 2 || fetches) fail("native check polling or HTTP isolation failed");
const notes = await updates.getUpdateNotes("v2");
if (!notes.ok || !calls.some(x => x.rpc.method === "updates.poll" && x.rpc.params.id === "notes-id")) fail("native notes polling failed");
if (!(await updates.getUpdates()).packaged || !(await updates.startUpdate()).ok) fail("native synchronous methods failed");
// Application-level busy is data, while invoke rejection is an exception; in
// both cases the native path must never turn into an HTTP retry.
internals.invoke = (method, rpc) => rpc.method === "updates.start"
  ? Promise.resolve({ ok: false, errors: ["busy"] })
  : Promise.reject(new Error("native down"));
const busy = await updates.startUpdate();
if (busy.ok !== false || busy.errors[0] !== "busy" || fetches) fail("native start application rejection was not preserved");
// Native rejection must not turn into a browser request.
internals.invoke = () => Promise.reject(new Error("native down"));
try { await updates.getUpdates(); fail("native error was swallowed"); } catch (error) { if (!error.message.includes("native down")) fail("native error changed"); }
try { await updates.startUpdate(); fail("native start error was swallowed"); } catch (error) { if (!error.message.includes("native down")) fail("native start error changed"); }
if (fetches) fail("native failure used HTTP fallback");
// Browser compatibility keeps exact routes and payloads.
delete globalThis.window; Date.now = realNow; globalThis.setTimeout = realTimeout;
const requests = [];
globalThis.fetch = async (url, options) => { requests.push({ url, options }); return { ok: true, status: 200, json: async () => ({ ok: true, state: {} }) }; };
await updates.getUpdates(); await updates.checkUpdates(true); await updates.getUpdateNotes("v 2"); await updates.startUpdate();
if (requests.length !== 4 || requests[0].url !== "/api/updates" || requests[1].options.body !== JSON.stringify({ force: true }) ||
    requests[2].url !== "/api/release-notes?tag=v%202" || requests[3].url !== "/api/updates/start") fail("browser update routes or payloads changed");
// Browser application-level busy is also returned as data for the settings
// warning branch, not converted into a transport exception.
globalThis.fetch = async (url, options) => ({ ok: false, status: 400, json: async () => ({ ok: false, errors: ["busy"] }) });
const browserBusy = await updates.startUpdate();
if (browserBusy.ok !== false || browserBusy.errors[0] !== "busy") fail("browser start application rejection changed");
console.log("ok: native/browser update transport, bounded polling, memoization, and failure isolation pass");
'''.strip()
    result = subprocess.run([node, "--input-type=module", "-", str(FACADE)], input=script,
                            text=True, capture_output=True)
    if result.returncode:
        return fail("Node updates transport contract failed: " + (result.stderr or result.stdout).strip())
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
