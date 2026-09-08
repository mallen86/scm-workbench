#!/usr/bin/env python3
"""Executable contract for the repository-operation transport facade."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "repos-transport.js"
PAGE = UI / "pages" / "settings.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if not FACADE.is_file():
        return fail("repository transport facade is missing")
    facade = FACADE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    for marker in (
        "export function listRepoRefs(repo)",
        "export function setRepoSource(repo, source)",
        "export function checkRepo(repo, force = false)",
        '"repos.refs"',
        '"repos.source.set"',
        '"repos.check"',
        'method: "repos.poll"',
        'params: { operation_id: operationId }',
        'setTimeout(resolve, POLL_INTERVAL_MS)',
        "OPERATION_TIMEOUT_MS = 100000",
        'fetch(path, {',
        'body: JSON.stringify(body)',
    ):
        if marker not in facade:
            return fail(f"repository facade is missing {marker}")
    if 'import { listRepoRefs, setRepoSource, checkRepo } from "../repos-transport.js";' not in page:
        return fail("settings page does not import the repository facade")
    for marker in ("listRepoRefs(row.key)", "setRepoSource(row.key, v)", "checkRepo(row.key, true)"):
        if marker not in page:
            return fail(f"settings page is missing {marker}")
    if "finally" not in page or "checkBtn.disabled = false" not in page:
        return fail("repository check button is not restored in finally")
    for marker in ('j.kind === "repo_init" && j.status === "running"',
                   "repoInitPending || S.repoInitPending || dlBtn.disabled",
                   "repoInitPending = true; S.repoInitPending = true; dlBtn.disabled = true;"):
        if marker not in page:
            return fail(f"managed-copy download admission is missing {marker}")
    for path in sorted(UI.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        if path != FACADE and re.search(r'(?:api|fetch)\s*\(\s*["\']/api/repos/', source):
            return fail(f"{path.relative_to(ROOT)} bypasses the repository facade")
    if "render().catch" not in page:
        return fail("settings repository render failures are not caught")
    for marker in (
        "const warnings = Array.isArray(r.warnings)",
        'for (const warning of warnings) toast("warn", warning, 5200);',
        'toast("ok", `Tracking “${r.target ? r.target.ref : v}” for ${row.name}.`);',
    ):
        if marker not in page:
            return fail(f"repository source warning handling is missing {marker}")
    refresh_at = page.find("await refreshInfo({ keepForms: true })", page.find("const pickSource"))
    warning_at = page.find("const warnings = Array.isArray(r.warnings)", refresh_at)
    if refresh_at < 0 or warning_at < 0 or warning_at < refresh_at:
        return fail("source warnings are shown before canonical state refresh")

    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the repository transport contract")
    script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const transportUrl = dataUrl(`
  export function getTauriInvoke() {
    const internals = globalThis.window && globalThis.window.__TAURI_INTERNALS__;
    return internals && typeof internals.invoke === "function" ? internals.invoke.bind(internals) : null;
  }
`);
const repos = await import(dataUrl(source.replace('from "./transport.js"', `from "${transportUrl}"`)));
const fail = message => { throw new Error(message); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
let now = 0;
const realNow = Date.now;
Date.now = () => now;
let delays = [];
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, delay) => { delays.push(delay); now += delay; return realSetTimeout(fn, 0); };
let calls = [];
let pollCount = 0;
const internals = { invoke(method, rpc) {
  calls.push({ method, rpc, receiver: this });
  if (rpc.method === "repos.refs") return Promise.resolve({ operation_id: "refs-1" });
  if (rpc.method === "repos.poll") {
    pollCount++;
    return pollCount === 1
      ? Promise.resolve({ done: false })
      : Promise.resolve({ done: true, result: { ok: true, repo: "scm", refs: { tags: [], releases: [] } } });
  }
  if (rpc.method === "repos.source.set") return Promise.resolve({ operation_id: "set-1" });
  if (rpc.method === "repos.check") return Promise.resolve({ operation_id: "check-1" });
  return Promise.reject(new Error("unexpected native method"));
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return Promise.reject(new Error("HTTP fallback was used")); };
const first = repos.listRepoRefs("scm");
const second = repos.listRepoRefs("scm");
if (first !== second) fail("refs calls were not memoized per repository");
const refs = await first;
if (!refs.ok || pollCount !== 2 || fetchCalls || delays[0] !== 150) fail("delayed native refs polling or HTTP isolation failed");
if (!same(calls[0].rpc, { method: "repos.refs", params: { repo: "scm" } }) ||
    !same(calls[1].rpc, { method: "repos.poll", params: { operation_id: "refs-1" } }) ||
    calls.some(x => x.method !== "wb_rpc" || x.receiver !== internals)) fail("native refs payload or receiver is incorrect");
await repos.setRepoSource("scm", "main");
await repos.checkRepo("scm", true);
if (!same(calls.find(x => x.rpc.method === "repos.source.set").rpc,
          { method: "repos.source.set", params: { repo: "scm", source: "main" } }) ||
    !same(calls.find(x => x.rpc.method === "repos.check").rpc,
          { method: "repos.check", params: { repo: "scm", force: true } })) fail("native operation payload changed");

// Native app errors, malformed operation ids, and native rejection are all
// surfaced without attempting a browser retry.
internals.invoke = (method, rpc) => {
  if (rpc.method === "repos.source.set") return Promise.resolve({ operation_id: "bad-set" });
  if (rpc.method === "repos.check") return Promise.resolve({ ok: true });
  if (rpc.method === "repos.poll") return Promise.resolve({ done: true, result: { ok: false, errors: ["source rejected"] } });
  return Promise.reject(new Error("native down"));
};
let appError = null;
try { await repos.setRepoSource("scm", "bad"); } catch (error) { appError = error; }
if (!appError || appError.message !== "source rejected" || fetchCalls) fail("native application error was not surfaced");
let malformed = null;
try { await repos.checkRepo("malformed", false); } catch (error) { malformed = error; }
if (!malformed || !malformed.message.includes("operation id") || fetchCalls) fail("malformed operation id was accepted");

// Failed refs promises are evicted so a later render can retry.
let refsAttempt = 0;
internals.invoke = (method, rpc) => {
  if (rpc.method === "repos.refs") {
    refsAttempt++;
    return refsAttempt === 1 ? Promise.resolve({ operation_id: "retry" }) : Promise.resolve({ operation_id: "retry-2" });
  }
  if (rpc.method === "repos.poll") return Promise.resolve({ done: true, result: { ok: false, errors: ["temporary"] } });
  return Promise.reject(new Error("unexpected"));
};
try { await repos.listRepoRefs("retry"); } catch {}
try { await repos.listRepoRefs("retry"); } catch {}
if (refsAttempt !== 2) fail("failed refs promise was not cleared");

// Bounded timeout: advancing the mocked clock through the polling sleep must
// stop the operation rather than polling forever.
now = 0;
internals.invoke = (method, rpc) => rpc.method === "repos.check"
  ? Promise.resolve({ operation_id: "never" })
  : Promise.resolve({ done: false });
let timeout = null;
try { await repos.checkRepo("timeout", false); } catch (error) { timeout = error; }
if (!timeout || !timeout.message.includes("timed out") || now < 100000) fail("native polling deadline is not bounded");

// Browser compatibility retains the old POST bodies and final result shapes.
delete globalThis.window;
Date.now = realNow;
globalThis.setTimeout = realSetTimeout;
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ ok: true, repo: "browser", target: { ref: "main" }, refs: { tags: [], releases: [] } }) };
};
const bRefs = await repos.listRepoRefs("browser");
const bSource = await repos.setRepoSource("browser", "latest-release");
const bCheck = await repos.checkRepo("browser", false);
if (!bRefs.ok || !bSource.target || !bCheck.ok || requests.length !== 3 ||
    requests[0].url !== "/api/repos/refs" || requests[1].url !== "/api/repos/save" || requests[2].url !== "/api/repos/check" ||
    requests.some(x => x.options.method !== "POST") ||
    requests[0].options.body !== JSON.stringify({ repo: "browser" }) ||
    requests[1].options.body !== JSON.stringify({ repo: "browser", source: "latest-release" }) ||
    requests[2].options.body !== JSON.stringify({ repo: "browser", force: false })) fail("browser repository payloads or result shapes changed");
globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ errors: ["worker unavailable"] }) });
let browserError = null;
try { await repos.checkRepo("browser-error", true); } catch (error) { browserError = error; }
if (!browserError || browserError.message !== "worker unavailable") fail("browser HTTP error was not surfaced");
console.log("ok: native repository operations, polling deadline, memoization, failure isolation, and browser payloads pass");
'''.strip()
    result = subprocess.run([node, "--input-type=module", "-", str(FACADE)], input=script,
                            text=True, capture_output=True)
    if result.returncode:
        return fail("Node repository contract failed: " + (result.stderr or result.stdout).strip())
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
