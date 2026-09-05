#!/usr/bin/env python3
"""Static and Node contract for the settings-write transport facade."""
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "settings-transport.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if not FACADE.is_file():
        return fail("settings transport facade is missing")
    facade = FACADE.read_text(encoding="utf-8")
    nav_path = UI / "nav.js"
    nav = nav_path.read_text(encoding="utf-8")
    settings = (UI / "pages" / "settings.js").read_text(encoding="utf-8")
    dashboard = (UI / "pages" / "dashboard.js").read_text(encoding="utf-8")

    for required in (
        'export async function setSettings(changes)',
        'method: "settings.set"',
        'params: { changes }',
        'fetch("/api/settings", {',
        'body: JSON.stringify(changes)',
        'if (!response.ok) throw new Error(errorMessage(data, response.status));',
        'data.ok === false',
        'data.errors',
    ):
        if required not in facade:
            return fail(f"settings facade is missing {required}")

    for path in sorted(UI.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        if path != FACADE and re.search(r'fetch\s*\(\s*["\']/api/settings', source):
            return fail(f"{path.relative_to(ROOT)} directly bypasses the settings facade")
        if path != FACADE and re.search(r'api\s*\(\s*["\']/api/settings["\']\s*,', source):
            return fail(f"{path.relative_to(ROOT)} uses api() for a settings write")

    for path, marker in (
        (nav_path, 'import { setSettings } from "./settings-transport.js";'),
        (UI / "pages" / "settings.js", 'import { setSettings } from "../settings-transport.js";'),
        (UI / "pages" / "dashboard.js", 'import { setSettings } from "../settings-transport.js";'),
    ):
        if marker not in path.read_text(encoding="utf-8"):
            return fail(f"{path.relative_to(ROOT)} does not import the settings facade")
    for source, marker in (
        (nav, 'await setSettings({ ui_mode: mode });'),
        (nav, 'then(() => setSettings({ theme }))'),
        (settings, 'setSettings({ defaults:'),
        (settings, 'setSettings({ scm_dir:'),
        (settings, 'setSettings({ python:'),
        (dashboard, 'setSettings({ onboarded: true })'),
        (dashboard, 'setSettings({ scm_dir:'),
    ):
        if marker not in source:
            return fail(f"settings caller is missing {marker}")
    for marker in (
        'let themeSelection = 0;',
        'let confirmedTheme = null;',
        'let themeWriteQueue = Promise.resolve();',
        'if (confirmedTheme === null) confirmedTheme = previous;',
        'const selection = ++themeSelection;',
        'themeWriteQueue = themeWriteQueue',
        'then(() => setSettings({ theme }))',
        'if (selection !== themeSelection) return;',
        'applyTheme(s, confirmedTheme || previous);',
        'confirmedTheme = theme;',
        'toast("err",',
    ):
        if marker not in nav:
            return fail(f"theme failure/race handling is missing {marker}")

    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the settings transport contract")
    node_script = r'''
import fs from "node:fs";
const facadeSource = fs.readFileSync(process.argv[2], "utf8");
const navSource = fs.readFileSync(process.argv[3], "utf8");
const dataUrl = source => `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;
const transportUrl = dataUrl(fs.readFileSync(process.argv[4], "utf8"));
const facade = await import(dataUrl(facadeSource.replace('from "./transport.js"', `from "${transportUrl}"`)));
const fail = message => { throw new Error(message); };
const changes = { theme: "light", nested: { value: 2 } };

// Native writes use the exact RPC method and params shape, and never fall back
// to HTTP if the selected bridge rejects.
const nativeCalls = [];
const internals = { invoke(method, rpc) {
  nativeCalls.push({ method, rpc, receiver: this });
  return Promise.resolve({ saved: true });
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return Promise.reject(new Error("HTTP fallback was used")); };
const nativeResult = await facade.setSettings(changes);
if (JSON.stringify(nativeResult) !== JSON.stringify({ saved: true }) ||
    JSON.stringify(nativeCalls[0]) !== JSON.stringify({
      method: "wb_rpc", rpc: { method: "settings.set", params: { changes } }, receiver: internals,
    }) || fetchCalls) fail("native settings payload or HTTP isolation is incorrect");

internals.invoke = () => Promise.reject(new Error("native down"));
let nativeRejected = false;
try { await facade.setSettings({ ui_mode: "simple" }); } catch (error) { nativeRejected = error.message === "native down"; }
if (!nativeRejected || fetchCalls) fail("native settings rejection silently fell back to HTTP");

// Browser mode posts the patch itself, and both HTTP and application-level
// failures become rejected promises.
delete globalThis.window;
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ saved: true }) };
};
const browserResult = await facade.setSettings(changes);
if (JSON.stringify(browserResult) !== JSON.stringify({ saved: true }) || requests.length !== 1 ||
    requests[0].url !== "/api/settings" || JSON.stringify(requests[0].options) !== JSON.stringify({
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
    })) fail("browser settings URL, method, or exact patch body is incorrect");

globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ error: "worker unavailable" }) });
let httpRejected = null;
try { await facade.setSettings({ port: 8038 }); } catch (error) { httpRejected = error; }
if (!httpRejected || httpRejected.message !== "worker unavailable") fail("HTTP settings error was not surfaced");

globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: false, errors: ["invalid port"] }) });
let appRejected = null;
try { await facade.setSettings({ port: -1 }); } catch (error) { appRejected = error; }
if (!appRejected || appRejected.message !== "invalid port") fail("application settings error was not surfaced");

// Exercise the real theme selection race. Writes are queued in click order,
// while visuals remain immediate. Use a fresh module/state for each scenario.
const consoleUrl = dataUrl("export const toggleConsole = () => {}; export const refreshJobs = () => {}; ");
const infoUrl = dataUrl("export const refreshInfo = () => Promise.resolve(); ");
const formsUrl = dataUrl("export const defaultArgs = () => ({}); ");
const settingsTransportUrl = dataUrl(`
  export function setSettings(changes) {
    return new Promise((resolve, reject) => globalThis.themeWrites.push({ changes, resolve, reject }));
  }
`);
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
let themeCase = 0;
async function freshTheme(initial) {
  const id = ++themeCase;
  const coreUrl = dataUrl(`
    // unique state for theme case ${id}
    export const PAGES = {};
    export const S = { info: { settings: { theme: ${JSON.stringify(initial)}, ui_mode: "advanced" } }, page: "dashboard" };
    export const $ = () => ({ textContent: "", innerHTML: "", firstElementChild: null });
    export const $$ = () => [];
    export const iconize = () => {};
    export const toast = (...args) => globalThis.themeToasts.push(args);
  `);
  const navCode = navSource
    .replace('from "./console.js"', `from "${consoleUrl}"`)
    .replace('from "./info.js"', `from "${infoUrl}"`)
    .replace('from "./core.js"', `from "${coreUrl}"`)
    .replace('from "./forms.js"', `from "${formsUrl}"`)
    .replace('from "./settings-transport.js"', `from "${settingsTransportUrl}"`);
  globalThis.window = { addEventListener() {} };
  globalThis.document = { documentElement: { dataset: { theme: initial } } };
  globalThis.themeWrites = [];
  globalThis.themeToasts = [];
  return { nav: await import(dataUrl(navCode) + `#${id}`), settings: globalThis.window, document: globalThis.document };
}

// A failed old write must not clobber a newer choice, and the second write
// must not start until the first one settles.
let test = await freshTheme("dark");
test.nav.setTheme("light");
test.nav.setTheme("dark");
await flush();
if (globalThis.themeWrites.length !== 1 || globalThis.themeWrites[0].changes.theme !== "light")
  fail("theme writes were not serialized in click order");
globalThis.themeWrites[0].reject(new Error("old write failed"));
await flush();
if (globalThis.themeWrites.length !== 2 || globalThis.themeWrites[1].changes.theme !== "dark" ||
    globalThis.document.documentElement.dataset.theme !== "dark" || globalThis.themeToasts.length)
  fail("stale theme failure clobbered a newer selection or queue did not continue");
globalThis.themeWrites[1].resolve({ ok: true });
await flush();

// If every write fails, revert to the initial confirmed theme, not the
// optimistic predecessor from the last click.
test = await freshTheme("light");
test.nav.setTheme("dark");
test.nav.setTheme("light");
await flush();
globalThis.themeWrites[0].reject(new Error("first write failed"));
await flush();
if (globalThis.themeWrites.length !== 2) fail("second failed theme write was not queued");
globalThis.themeWrites[1].reject(new Error("second write failed"));
await flush();
if (globalThis.document.documentElement.dataset.theme !== "light" || globalThis.themeToasts.length !== 1)
  fail("two theme failures did not restore the initial confirmed theme");

// A successful first write advances the confirmed rollback point for a later
// failed selection.
test = await freshTheme("light");
test.nav.setTheme("dark");
test.nav.setTheme("light");
await flush();
globalThis.themeWrites[0].resolve({ ok: true });
await flush();
if (globalThis.themeWrites.length !== 2) fail("second theme write did not follow a successful first write");
globalThis.themeWrites[1].reject(new Error("latest write failed"));
await flush();
if (globalThis.document.documentElement.dataset.theme !== "dark" || globalThis.themeToasts.length !== 1)
  fail("latest theme failure did not revert to the first confirmed theme");

if (navSource.includes('fetch("/api/settings"')) fail("nav still contains a direct settings write");
console.log("ok: native/browser settings payloads, no fallback, HTTP/application errors, ordered theme writes, and race-safe rollback pass");
'''.strip()
    result = subprocess.run(
        [node, "--input-type=module", "-", str(FACADE), str(nav_path), str(UI / "transport.js")],
        input=node_script,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return fail(f"Node settings contract failed: {detail}")
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
