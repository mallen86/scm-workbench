#!/usr/bin/env python3
"""Static and Node contracts for offset transport and calibration inventory."""
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "offset-transport.js"
PAGE = UI / "pages" / "offset.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if not FACADE.is_file():
        return fail("offset transport facade is missing")
    facade = FACADE.read_text(encoding="utf-8")
    if not PAGE.is_file():
        return fail("offset page is missing")
    page = PAGE.read_text(encoding="utf-8")
    forms = (UI / "forms.js").read_text(encoding="utf-8")
    nav = (UI / "nav.js").read_text(encoding="utf-8")
    index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    for required in (
        "export async function setOffset",
        "export async function deleteOffset",
        '"offset.set"',
        '"offset.delete"',
        'fetch("/api/offset"',
        'body: JSON.stringify(requestBody)',
        'body: JSON.stringify({ size, delete: true })',
    ):
        if required not in facade:
            return fail(f"offset facade is missing {required}")
    for marker in (
        'import { setOffset, deleteOffset } from "../offset-transport.js";',
        'import { watchJobDone } from "./utilities.js";',
        "export function renderCalibrationFiles",
        "export async function regenerateCalibration",
        'refreshInfo({ keepForms: true })',
        "setOffset(",
        "deleteOffset(",
    ):
        if marker not in page:
            return fail(f"offset page is missing {marker}")
    if 'kind === "calibration" || kind === "dxf_batch"' in forms:
        return fail("calibration still uses the fixed-delay inventory refresh")
    for marker in (
        'import { go, uiMode } from "../nav.js";',
        'if (uiMode() !== "simple") wrap.append(formCard("offset_pdf"',
        'if (uiMode() !== "simple") patchOffsetForm()',
    ):
        if marker not in page:
            return fail(f"simple offset page contract is missing {marker}")
    simple_pages = re.search(r'export const SIMPLE_PAGES = \[([^\]]*)\]', nav)
    if not simple_pages or '"offset"' not in simple_pages.group(1):
        return fail("simple navigation does not allow the offset page")
    # The item must exist, stay visible in simple mode, and stay in the workflow
    # section (asserted structurally: the exact attribute string would break on
    # any unrelated markup change).
    offset_link = re.search(r'<a class="nav-item"([^>]*data-page="offset"[^>]*)>', index)
    if not offset_link:
        return fail("offset navigation item is missing")
    if "data-simple-hide" in offset_link.group(1):
        return fail("offset navigation item is still hidden in simple mode")
    if 'data-section="workflow"' not in offset_link.group(1):
        return fail("offset navigation item left the workflow section")
    for path in sorted(UI.rglob("*.js")):
        if path == FACADE:
            continue
        source = path.read_text(encoding="utf-8")
        if 'api("/api/offset"' in source or 'fetch("/api/offset"' in source:
            return fail(f"{path.relative_to(ROOT)} bypasses the offset transport facade")

    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the offset transport contract")
    node_script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const transportSource = fs.readFileSync(process.argv[3], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const transportUrl = dataUrl(transportSource);
const facade = await import(dataUrl(source.replace('from "./transport.js"', `from "${transportUrl}"`)));
const fail = message => { throw new Error(message); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const calls = [];
const internals = { invoke(method, rpc) {
  calls.push({ method, rpc, receiver: this });
  return Promise.resolve(rpc.method === "offset.set"
    ? { ok: true, offset: { x_offset: rpc.params.x, y_offset: rpc.params.y, angle_offset: rpc.params.angle },
        ...(rpc.params.size === null ? {} : { size: rpc.params.size, staged: true }) }
    : { ok: true, removed: rpc.params.size });
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return Promise.reject(new Error("HTTP fallback was used")); };
const globalResult = await facade.setOffset({ x: "11.9", y: -12.8, angle: 1.25 });
const sizeResult = await facade.setOffset({ size: "letter", x: -21, y: 22, angle: -2.5 });
const deleteResult = await facade.deleteOffset("letter");
if (!same(globalResult.offset, { x_offset: 11, y_offset: -12, angle_offset: 1.25 }) ||
    !same(sizeResult, { ok: true, offset: { x_offset: -21, y_offset: 22, angle_offset: -2.5 }, size: "letter", staged: true }) ||
    !same(deleteResult, { ok: true, removed: "letter" }) || fetchCalls || calls.length !== 3 ||
    calls[0].method !== "wb_rpc" || calls[0].rpc.method !== "offset.set" ||
    !same(calls[0].rpc.params, { size: null, x: 11, y: -12, angle: 1.25 }) ||
    !same(calls[1].rpc.params, { size: "letter", x: -21, y: 22, angle: -2.5 }) ||
    !same(calls[2].rpc, { method: "offset.delete", params: { size: "letter" } }))
  fail("native offset payload or result handling is incorrect");

internals.invoke = () => Promise.reject(new Error("native down"));
let nativeRejected = false;
try { await facade.setOffset({ x: 1, y: 2, angle: 3 }); } catch (error) { nativeRejected = error.message === "native down"; }
if (!nativeRejected || fetchCalls) fail("native offset rejection silently fell back to HTTP");

delete globalThis.window;
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ ok: true }) };
};
await facade.setOffset({ x: "11.9", y: -12.8, angle: 1.25 });
await facade.setOffset({ size: "letter", x: 1, y: 2, angle: 3 });
await facade.deleteOffset("letter");
if (requests.length !== 3 || requests[0].url !== "/api/offset" ||
    requests[0].options.body !== JSON.stringify({ x: 11, y: -12, angle: 1.25 }) ||
    requests[1].options.body !== JSON.stringify({ size: "letter", x: 1, y: 2, angle: 3 }) ||
    requests[2].options.body !== JSON.stringify({ size: "letter", delete: true }))
  fail("browser offset URL or exact compatibility body is incorrect");

// The calibration card must render only the backend's current file inventory,
// then re-read and repaint that inventory after generation actually finishes.
const pageState = {
  S: {
    info: {
      scm: {
        calibration: [{ name: "custom-wide", path: "/scm/calibration/custom-wide.pdf", size: 17 }],
        paper_sizes: [{ name: "legal" }, { name: "phantom" }],
      },
    },
    forms: {},
  },
  watch: null,
  refreshOptions: null,
  currentGrid: null,
  doRunCalls: [],
  async doRun(kind, button) {
    this.doRunCalls.push({ kind, button });
    return { id: "calibration-job" };
  },
  watchJobDone(id, callback) { this.watch = { id, callback }; },
  async refreshInfo(options) {
    this.refreshOptions = options;
    this.S.info.scm.calibration = [
      { name: "letter", path: "/scm/calibration/letter-calibration.pdf", size: 21 },
      { name: "legal", path: "/scm/calibration/legal-calibration.pdf", size: 22 },
    ];
  },
  toasts: [],
};
globalThis.offsetPageTest = pageState;
const coreStub = `
export const S = globalThis.offsetPageTest.S;
export const PAGES = {};
export const $ = selector => selector === ".calibration-files" ? globalThis.offsetPageTest.currentGrid : null;
export const $$ = () => [];
export function el(tag, attrs = {}, ...children) {
  const node = {
    tag, children: [], isConnected: true,
    append(...items) { this.children.push(...items.filter(item => item !== null && item !== undefined)); },
    replaceChildren(...items) { this.children = items.filter(item => item !== null && item !== undefined); },
  };
  Object.assign(node, attrs || {});
  node.append(...children);
  return node;
}
export const fmtBytes = value => String(value);
export const ico = name => ({ icon: name });
export const pageHead = () => ({});
export const toast = (...args) => globalThis.offsetPageTest.toasts.push(args);
`;
const stubs = {
  "../core.js": coreStub,
  "../native-actions.js": `export const openFile = async () => ({ ok: true });`,
  "../offset-transport.js": `export const setOffset = async () => ({}); export const deleteOffset = async () => ({});`,
  "../forms.js": `
    export const afterFormChange = () => {};
    export const defaultArgs = () => ({});
    export const doRun = (...args) => globalThis.offsetPageTest.doRun(...args);
    export const formCard = () => ({});
    export const numSteppers = () => ({});
  `,
  "../info.js": `export const refreshInfo = options => globalThis.offsetPageTest.refreshInfo(options);`,
  "../nav.js": `export const go = () => {}; export const uiMode = () => "advanced";`,
  "../repo-setup.js": `export const connectCardNeeded = () => false; export const repoSetupCard = () => ({});`,
  "./utilities.js": `export const watchJobDone = (...args) => globalThis.offsetPageTest.watchJobDone(...args);`,
};
let pageSource = fs.readFileSync(process.argv[4], "utf8");
for (const [specifier, stub] of Object.entries(stubs)) {
  const quoted = `"${specifier}"`;
  if (!pageSource.includes(quoted)) fail(`offset page stopped importing ${specifier}`);
  pageSource = pageSource.replaceAll(quoted, `"${dataUrl(stub)}"`);
}
const page = await import(dataUrl(pageSource));
const grid = {
  children: [], isConnected: true,
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = [...items]; },
};
const textOf = value => typeof value === "string" ? value
  : value && Array.isArray(value.children) ? value.children.map(textOf).join(" ") : "";
page.renderCalibrationFiles(grid);
let labels = textOf(grid);
if (grid.children.length !== 1 || !labels.includes("custom-wide") ||
    labels.includes("legal") || labels.includes("phantom"))
  fail("calibration buttons were not enumerated exclusively from the current file inventory");
const generated = await page.regenerateCalibration(grid);
if (generated?.id !== "calibration-job" || pageState.doRunCalls.length !== 1 ||
    pageState.doRunCalls[0].kind !== "calibration" || pageState.watch?.id !== "calibration-job")
  fail("calibration regeneration did not register a terminal-job refresh");
// Simulate leaving and returning to the page while generation runs. The job's
// watcher must repaint the currently connected card, not its detached grid.
grid.isConnected = false;
const liveGrid = {
  children: [], isConnected: true,
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = [...items]; },
};
pageState.currentGrid = liveGrid;
await pageState.watch.callback({ status: "ok" });
labels = textOf(liveGrid);
if (!same(pageState.refreshOptions, { keepForms: true }) || liveGrid.children.length !== 2 ||
    !liveGrid.children.every(child => child.class === "fileitem") ||
    !labels.includes("letter") || !labels.includes("legal") || labels.includes("custom-wide"))
  fail("terminal calibration refresh did not repaint the actual generated files");
console.log("ok: offset transport and completion-based calibration inventory contracts pass");
'''.strip()
    result = subprocess.run(
        [node, "--input-type=module", "-", str(FACADE), str(UI / "transport.js"), str(PAGE)],
        input=node_script, text=True, capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return fail(f"Node offset transport contract failed: {detail}")
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
