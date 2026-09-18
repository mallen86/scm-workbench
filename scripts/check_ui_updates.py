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
UPDATER_UI = UI / "updater-ui.js"
PAGE = UI / "pages" / "settings.js"


def fail(message):
    print(f"FAIL: {message}")
    return 1


def s_count(haystack, needle):
    return haystack.count(needle)


def main():
    if not FACADE.is_file():
        return fail("updates transport facade is missing")
    source = FACADE.read_text(encoding="utf-8")
    updater_ui = UPDATER_UI.read_text(encoding="utf-8")
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
    if 'import { getUpdates, checkUpdates, getUpdateNotes } from "../updates-transport.js";' not in page:
        return fail("settings page does not import the updates facade")
    if 'import { refreshUpdateNotice, startUpdateInstall, updateInstallActive } from "../updater-ui.js";' not in page:
        return fail("settings page does not use the shared update-install controller")
    for marker in ("getUpdates()", "checkUpdates(true)", "getUpdateNotes(tag)", "startUpdateInstall()",
                   "let checkPending = false", "checkPending = true", "checkPending = false",
                   "if (checkPending || st.checking)", "if (updateInstallActive())",
                   'setBtn("Updating…", null, true)', "uBtn.disabled = true;",
                   'setBtn("Check for updates", doCheck)',
                   'id: "set-beta-updates"', '"Include beta releases"',
                   'if (!simple) betaI.onchange = changeUpdateChannel;',
                   'Switch to Advanced mode to change whether beta releases are included.',
                   'setSettings({ update_channel: channel })',
                   'st.prerelease ? "A newer beta version is available: "',
                   'r.install_mode === "manual"', 'r.package_format === "arch"',
                   'View ${latest} download', 'Download the ${manualPackage}',
                   'This Linux distribution does not have a supported update package.'):
        if marker not in page:
            return fail(f"settings page is missing {marker}")
    beta_input = page.find('const betaI = el("input"')
    advanced_guard = page.find("if (!simple) {", beta_input)
    beta_label = page.find('"Include beta releases"', beta_input)
    update_row = page.find('const uRow = el("div"', beta_input)
    if min(beta_input, advanced_guard, beta_label, update_row) < 0 or not (
            beta_input < advanced_guard < beta_label < update_row):
        return fail("the beta opt-in is not confined to Advanced Settings")
    if "startUpdateRequest" in page or "startUpdateStrip(job.id)" in page:
        return fail("settings bypasses the shared update-install controller")
    if "checkUpdates(!fresh)" in page:
        return fail("manual update checks still reuse the scheduled-check cache")
    for marker in ("async function finishUpdateStrip(job)", "await jobs.log(job.id)",
                   "await finishUpdateStrip(j)", 'head.textContent = failed ? "SCM Workbench update failed"',
                   'job.progress?.restart_at', 'Restarting in ${seconds}',
                   'setInterval(tick, 250)', "export function updateInstallActive()",
                   "export async function startUpdateInstall()", "UPDATE_ACTIVE_STATUSES",
                   "export function startAutomaticUpdateChecks()",
                   "AUTOMATIC_UPDATE_INTERVAL_MS = 24 * 60 * 60 * 1000",
                   "await checkUpdates(true)", "await refreshUpdateNotice()",
                   "_updRequestPending = true", "removeUpdateNotice();"):
        if marker not in updater_ui:
            return fail(f"update progress strip is missing {marker}")
    # The standing "an update is ready" notice: the scheduled check runs in the
    # background, so without this a release could sit unnoticed until Settings
    # was opened. It shows the action and a way to close it, in the shared box.
    for marker in ("export async function refreshUpdateNotice()",
                   'class: "repoprog sidebar-note", id: "updatenotice"',
                   'class: "rp-head rp-head-row"',
                   '"aria-label": "Dismiss the update notice"',
                   "S.updateNoticeDismissed = tag;",
                   "!tag || S.updateNoticeDismissed === tag",
                   "startUpdateStrip(result.job?.id);",
                   "if (_updStrip && _updStrip.isConnected) return;",
                   'beta ? "Beta update available" : "Update available"',
                   'state.install_mode === "manual"', 'state.package_format === "arch"',
                   'state.package_format = view?.package_format;',
                   '"View download"', 'Install the Arch package with pacman.',
                   'Install the Debian package with your software manager.',
                   'This Linux distribution does not have a supported update package.'):
        if marker not in updater_ui:
            return fail(f"the sidebar update notice is missing {marker}")
    # Only a genuinely newer release may raise the notice.
    if 'state.status === "update-available" ? String(state.latest || "") : ""' not in updater_ui:
        return fail("the update notice does not gate on an available release")
    css = (UI.parent / "theme.css").read_text(encoding="utf-8")
    if ".sidebar-note .rp-head-row" not in css:
        return fail("the update notice close control is unstyled")
    core_state = (UI / "core.js").read_text(encoding="utf-8")
    if "updateNoticeDismissed: null" not in core_state or "repoFailureDismissed: null" not in core_state:
        return fail("the sidebar notices have no session dismissal state")
    if s_count(page, "refreshUpdateNotice();") < 1:
        return fail("a manual check does not refresh the sidebar notice")
    app = (UI / "app.js").read_text(encoding="utf-8")
    for marker in ('import { getTauriInvoke } from "./transport.js";',
                   'import { startAutomaticUpdateChecks } from "./updater-ui.js";',
                   "if (getTauriInvoke()) Promise.resolve().then(() => startAutomaticUpdateChecks()).catch(() => {});"):
        if marker not in app:
            return fail(f"packaged startup update read is missing {marker}")
    if "getUpdates" in app or "checkUpdates" in app:
        return fail("startup bypasses the shared automatic-update controller")
    backend = (ROOT / "scm_workbench" / "server.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
    for marker in ('token = os.environ.get("SCM_WORKBENCH_UPDATE_TOKEN", "")',
                   'if not re.fullmatch(r"[0-9a-f]{64}", token):',
                   'os.environ.get("SCM_WORKBENCH_NO_UPDATE_CHECK") == "1"'):
        if marker not in backend:
            return fail(f"automatic update backend is missing {marker}")
    linux_smoke = (ROOT / "scripts" / "check_linux_package.sh").read_text(encoding="utf-8")
    if workflow.count("SCM_WORKBENCH_NO_UPDATE_CHECK=1") != 2 or \
            workflow.count('SCM_WORKBENCH_NO_UPDATE_CHECK = "1"') != 2 or \
            "SCM_WORKBENCH_NO_UPDATE_CHECK=1" not in linux_smoke:
        return fail("packaging lifecycle smokes do not disable release-network checks")
    if workflow.count("python scripts/check_ui_update_security.py") != 3:
        return fail("all three package targets must run the updater security contract")
    for marker in ("expected_prerelease=false",
                   'version_without_build=${GITHUB_REF_NAME%%+*}',
                   '[[ "$version_without_build" == *-* ]]',
                   "prerelease_args+=(--prerelease)", "--json isPrerelease",
                   '"$actual_prerelease" != "$expected_prerelease"'):
        if marker not in workflow:
            return fail(f"packaging prerelease contract is missing {marker}")
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
const updaterSource = fs.readFileSync(process.argv[3], "utf8");
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

// The global strip is Simple mode's only update transcript. A terminal update
// must stop polling but remain visible with its specific failure, and a later
// update must replace that retained result.
class Classes {
  constructor(value = "") { this.values = new Set(String(value).split(/\s+/).filter(Boolean)); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) { if (force === undefined ? !this.contains(name) : force) this.add(name); else this.remove(name); }
}
class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tag = tag; this.children = []; this.textContent = ""; this.isConnected = true;
    this.classList = new Classes(attrs.class); this.style = {}; this.dataset = {};
    this.append(...children); Object.assign(this, Object.fromEntries(Object.entries(attrs).filter(([key]) => key !== "class")));
  }
  append(...children) { for (const child of children.flat()) { if (child instanceof Element) this.children.push(child); else if (child != null) this.textContent += String(child); } }
  before(child) { globalThis.__insertedUpdateStrip = child; child.isConnected = true; }
  remove() { this.isConnected = false; if (this.id) globalThis.__updateNodes.delete("#" + this.id); }
  get firstElementChild() { return this.children[0] || null; }
}
globalThis.__updateNodes = new Map();
globalThis.__updateFoot = new Element("footer");
globalThis.__makeUpdateElement = (tag, attrs, children) => {
  const node = new Element(tag, attrs, children);
  if (node.id) globalThis.__updateNodes.set("#" + node.id, node);
  return node;
};
let listedJob = { id: "update-1", kind: "update", title: "Update to v2", status: "fail", progress: { stage: "extract" } };
globalThis.__updateJobs = {
  list: async () => ({ jobs: [listedJob] }),
  log: async () => ({ lines: ["Extracting the new app …", "    ! archive contained an unsafe path", "✕ exited with code 1"] }),
};
globalThis.__updateSharedState = { info: { settings: { ui_mode: "advanced" } } };
const updaterCore = dataUrl(`export const S = globalThis.__updateSharedState; export function $(selector) { return selector === ".sidebar-foot" ? globalThis.__updateFoot : globalThis.__updateNodes.get(selector) || null; } export function el(tag, attrs, ...children) { return globalThis.__makeUpdateElement(tag, attrs || {}, children); } export function ico(name) { return globalThis.__makeUpdateElement("span", { "data-ico": name }, []); } export function openUrl(url, label) { globalThis.__openedUpdateUrls.push({ url, label }); }`);
const updaterJobs = dataUrl(`export const jobs = globalThis.__updateJobs;`);
globalThis.__updateState = { state: {} };
globalThis.__openedUpdateUrls = [];
globalThis.__automaticChecks = [];
globalThis.__startUpdateRequest = async () => ({ ok: true, job: { id: "update-1" } });
const updaterUpdates = dataUrl(`
  export const getUpdates = async () => globalThis.__updateState;
  export const checkUpdates = async force => {
    globalThis.__automaticChecks.push(force);
    if (globalThis.__automaticCheckResult) globalThis.__updateState = globalThis.__automaticCheckResult;
    return globalThis.__updateState;
  };
  export const startUpdate = async () => globalThis.__startUpdateRequest();
`);
const loadedUpdaterSource = updaterSource
  .replace('from "./core.js"', `from "${updaterCore}"`)
  .replace('from "./jobs.js"', `from "${updaterJobs}"`)
  .replace('from "./updates-transport.js"', `from "${updaterUpdates}"`);
const realInterval = globalThis.setInterval, realClearInterval = globalThis.clearInterval;
let intervalCleared = false;
globalThis.setInterval = (fn, delay) => {
  if (delay === 24 * 60 * 60 * 1000) { globalThis.__automaticUpdateTick = fn; return 42; }
  globalThis.__updateTick = fn;
  return 41;
};
globalThis.clearInterval = id => { if (id === 41) intervalCleared = true; };
const updaterUi = await import(dataUrl(loadedUpdaterSource));
updaterUi.startUpdateStrip("update-1");
await new Promise(resolve => realTimeout(resolve, 0));
await new Promise(resolve => realTimeout(resolve, 0));
const failedStrip = globalThis.__insertedUpdateStrip;
const elementText = node => node.textContent + node.children.map(elementText).join(" ");
if (!failedStrip?.isConnected || !elementText(failedStrip).includes("update failed") ||
    !elementText(failedStrip).includes("archive contained an unsafe path") || !intervalCleared)
  fail("terminal update failure did not remain visible with its log detail");
const countdownStart = Date.now();
listedJob = { id: "update-2", kind: "update", title: "Update to v3", status: "handoff", progress: { stage: "handoff", restart_at: countdownStart / 1000 + 4 } };
updaterUi.startUpdateStrip("update-2");
await new Promise(resolve => realTimeout(resolve, 0));
await new Promise(resolve => realTimeout(resolve, 0));
const countdownStrip = globalThis.__insertedUpdateStrip;
if (failedStrip.isConnected || !countdownStrip?.isConnected || !elementText(countdownStrip).includes("Restarting in 4 seconds"))
  fail("the durable handoff did not replace progress with a visible restart countdown");
Date.now = () => countdownStart + 5000;
globalThis.__updateTick();
if (!elementText(countdownStrip).includes("Restarting now"))
  fail("the restart countdown did not advance to its terminal message");
Date.now = realNow;
listedJob = { id: "update-3", kind: "update", title: "Update to v4", status: "running", progress: { stage: "download", done: 2, total: 10 } };
updaterUi.startUpdateStrip("update-3");
await new Promise(resolve => realTimeout(resolve, 0));
if (countdownStrip.isConnected || !globalThis.__insertedUpdateStrip?.isConnected || !updaterUi.updateInstallActive())
  fail("a new update did not replace the retained countdown strip or report active");
listedJob = { ...listedJob, status: "fail" };
globalThis.__updateTick();
await new Promise(resolve => realTimeout(resolve, 0));
if (updaterUi.updateInstallActive()) fail("a failed update left install actions disabled");

// Both the Settings action and the sidebar notice use one admission guard.
// A successful Settings-style start removes the standing notice immediately,
// rejects a concurrent second click, and stays active until terminal failure.
updaterUi.stopUpdateStrip();
globalThis.__updateState = { state: { status: "update-available", latest: "v5", channel: "beta", prerelease: true, published: "2026-01-02T00:00:00Z" } };
globalThis.__updateSharedState.info.settings.ui_mode = "simple";
await updaterUi.refreshUpdateNotice();
const standingNotice = globalThis.__updateNodes.get("#updatenotice");
if (!standingNotice?.isConnected || !elementText(standingNotice).includes("Beta update available"))
  fail("a saved beta preference did not remain visible in Simple mode");
let resolveStart, startCalls = 0;
globalThis.__startUpdateRequest = () => {
  startCalls++;
  return new Promise(resolve => { resolveStart = resolve; });
};
const firstStart = updaterUi.startUpdateInstall();
if (!updaterUi.updateInstallActive()) fail("an update request did not disable install actions immediately");
const duplicateStart = await updaterUi.startUpdateInstall();
if (duplicateStart.ok !== false || startCalls !== 1) fail("concurrent update clicks reached the transport");
listedJob = { id: "update-4", kind: "update", title: "Update to v5", status: "running", progress: { stage: "download", done: 1, total: 10 } };
resolveStart({ ok: true, job: { id: "update-4" } });
const acceptedStart = await firstStart;
await new Promise(resolve => realTimeout(resolve, 0));
if (!acceptedStart.ok || standingNotice.isConnected || globalThis.__updateNodes.has("#updatenotice"))
  fail("an accepted update did not clear the standing sidebar notice");
if (!updaterUi.updateInstallActive()) fail("an accepted update re-enabled install actions while running");
listedJob = { ...listedJob, status: "fail" };
globalThis.__updateTick();
await new Promise(resolve => realTimeout(resolve, 0));
if (updaterUi.updateInstallActive()) fail("install actions did not re-enable after update failure");

// Linux exposes the exact release without admitting a self-update into /usr.
updaterUi.stopUpdateStrip();
globalThis.__updateState = { install_mode: "manual", package_format: "deb", state: {
  status: "update-available", latest: "v5.1", channel: "stable", prerelease: false,
  release_url: "https://github.com/mallen86/scm-workbench/releases/tag/v5.1",
} };
await updaterUi.refreshUpdateNotice();
const manualNotice = globalThis.__updateNodes.get("#updatenotice");
const descendants = node => node ? [node, ...node.children.flatMap(descendants)] : [];
const manualButton = descendants(manualNotice).find(node =>
  node.tag === "button" && elementText(node).includes("View download"));
if (!manualNotice?.isConnected || !elementText(manualNotice).includes("Install the Debian package") || !manualButton?.onclick)
  fail(`the Linux notice did not expose a manual Debian download: ${manualNotice ? elementText(manualNotice) : "missing notice"}`);
manualButton.onclick();
if (globalThis.__openedUpdateUrls.length !== 1 || !globalThis.__openedUpdateUrls[0].url.endsWith("/releases/tag/v5.1"))
  fail("the Linux notice did not open its server-validated release page");

globalThis.__updateState = { install_mode: "manual", package_format: "arch", state: {
  status: "update-available", latest: "v5.2", channel: "stable", prerelease: false,
  release_url: "https://github.com/mallen86/scm-workbench/releases/tag/v5.2",
} };
await updaterUi.refreshUpdateNotice();
const archNotice = globalThis.__updateNodes.get("#updatenotice");
const archButton = descendants(archNotice).find(node =>
  node.tag === "button" && elementText(node).includes("View download"));
if (!archNotice?.isConnected || !elementText(archNotice).includes("Install the Arch package with pacman") ||
    elementText(archNotice).includes("Debian") || !archButton?.onclick)
  fail(`the Arch notice did not expose the target-bound manual package: ${archNotice ? elementText(archNotice) : "missing notice"}`);

globalThis.__updateState = { install_mode: "manual", package_format: "unexpected", state: {
  status: "update-available", latest: "v5.3", channel: "stable", prerelease: false,
  release_url: "https://github.com/mallen86/scm-workbench/releases/tag/v5.3",
} };
await updaterUi.refreshUpdateNotice();
const unsupportedNotice = globalThis.__updateNodes.get("#updatenotice");
const unsupportedButton = descendants(unsupportedNotice).find(node =>
  node.tag === "button" && elementText(node).includes("View download"));
if (!elementText(unsupportedNotice).includes("does not have a supported update package") ||
    !unsupportedButton?.disabled || unsupportedButton?.onclick)
  fail("malformed Linux package metadata did not disable the manual action");

// Packaged startup forces a fresh check before painting its result. Its daily
// timer repeats that flow without allowing duplicate scheduler installation.
updaterUi.stopUpdateStrip();
globalThis.__updateState = { state: { status: "up-to-date", latest: "v5" } };
globalThis.__automaticCheckResult = { state: { status: "update-available", latest: "v6", published: "2026-01-03T00:00:00Z" } };
await updaterUi.startAutomaticUpdateChecks();
if (globalThis.__automaticChecks.length !== 1 || globalThis.__automaticChecks[0] !== true ||
    !globalThis.__updateNodes.get("#updatenotice")?.isConnected || !globalThis.__automaticUpdateTick)
  fail("packaged startup did not force a fresh check and paint its resulting notice");
await updaterUi.startAutomaticUpdateChecks();
if (globalThis.__automaticChecks.length !== 1)
  fail("automatic update scheduling was installed more than once");
globalThis.__automaticUpdateTick();
await new Promise(resolve => realTimeout(resolve, 0));
await new Promise(resolve => realTimeout(resolve, 0));
if (globalThis.__automaticChecks.length !== 2 || globalThis.__automaticChecks[1] !== true)
  fail("the daily automatic update timer did not perform a fresh check");
globalThis.setInterval = realInterval; globalThis.clearInterval = realClearInterval;
console.log("ok: update transport, automatic checks, shared install state, notices, persistent failures, and restart countdown pass");
'''.strip()
    result = subprocess.run([node, "--input-type=module", "-", str(FACADE), str(UPDATER_UI)], input=script,
                            text=True, capture_output=True)
    if result.returncode:
        return fail("Node updates transport contract failed: " + (result.stderr or result.stdout).strip())
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
