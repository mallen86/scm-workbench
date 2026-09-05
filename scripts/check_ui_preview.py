#!/usr/bin/env python3
"""Static and Node contract for the frontend preview transport facade."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    preview = (UI / "preview.js").read_text(encoding="utf-8")
    forms = (UI / "forms.js").read_text(encoding="utf-8")

    if 'import { getTauriInvoke } from "./transport.js";' not in preview:
        return fail("preview facade does not import the shared Tauri capability")
    for required in (
        'export function preview(kind, args)',
        'invoke("wb_rpc", { method: "preview", params })',
        'fetch(path)',
        'the server answered ${status}',
    ):
        if required not in preview:
            return fail(f"preview facade is missing {required}")
    if 'import { preview } from "./preview.js";' not in forms:
        return fail("forms.js does not import the preview facade")
    if "/api/preview" in forms or "fetch(`/api/preview" in forms:
        return fail("forms.js still bypasses the preview facade")
    if 'preview(kind, S.forms[kind])' not in forms:
        return fail("forms.js does not pass the current form to the preview facade")
    for required in (
        "attempt < 5",
        "setTimeout(() => run(attempt + 1), 2000)",
        "const isCurrent = () =>",
        "if (!isCurrent()) return;",
        "renderPreview(box, d);",
    ):
        if required not in forms:
            return fail(f"preview retry/sequencing/rendering contract lost: {required}")
    for path in sorted(UI.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        if path.name != "preview.js" and "/api/preview" in source:
            return fail(f"{path.relative_to(ROOT)} contains a direct preview route bypass")

    node = subprocess.run(
        ["node", "--input-type=module", "-", str(UI / "preview.js"), str(UI / "forms.js")],
        input=r'''
import fs from "node:fs";
const previewSource = fs.readFileSync(process.argv[2], "utf8");
const formsSource = fs.readFileSync(process.argv[3], "utf8");
const dataUrl = source => `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;
const previewUrl = dataUrl(previewSource.replace('from "./transport.js"', `from "${dataUrl(`
  export function getTauriInvoke() {
    const internals = globalThis.window && globalThis.window.__TAURI_INTERNALS__;
    return internals && typeof internals.invoke === "function" ? internals.invoke.bind(internals) : null;
  }
`)}"`));
const { preview } = await import(previewUrl);
const fail = message => { throw new Error(message); };

const kind = "create_pdf";
const args = { paper_size: "letter", options: ["a&b"] };
const nativeCalls = [];
globalThis.window = { __TAURI_INTERNALS__: { invoke(method, rpc) {
  nativeCalls.push({ method, rpc, receiver: this });
  return Promise.resolve({ cmd: "native" });
} } };
const nativeResult = await preview(kind, args);
if (JSON.stringify(nativeResult) !== JSON.stringify({ cmd: "native" }) || nativeCalls.length !== 1 ||
    nativeCalls[0].method !== "wb_rpc" || nativeCalls[0].rpc.method !== "preview" ||
    nativeCalls[0].rpc.params.kind !== kind || nativeCalls[0].rpc.params.args !== args ||
    nativeCalls[0].receiver !== globalThis.window.__TAURI_INTERNALS__)
  fail("native preview payload or result is incorrect");

let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return Promise.reject(new Error("HTTP fallback was used")); };
globalThis.window.__TAURI_INTERNALS__.invoke = () => Promise.reject(new Error("native down"));
let nativeRejected = false;
try { await preview(kind, args); } catch (error) { nativeRejected = error.message === "native down"; }
if (!nativeRejected || fetchCalls) fail("native preview failure silently fell back to HTTP");

delete globalThis.window;
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ cmd: "browser" }) };
};
const browserResult = await preview(kind, args);
const expectedUrl = `/api/preview?kind=${encodeURIComponent(kind)}&args=${encodeURIComponent(JSON.stringify(args))}`;
if (JSON.stringify(browserResult) !== JSON.stringify({ cmd: "browser" }) || requests.length !== 1 ||
    requests[0].url !== expectedUrl || requests[0].options !== undefined)
  fail("browser preview URL or GET behavior changed");

globalThis.fetch = async () => ({ ok: false, status: 422, json: async () => ({ error: "bad preview" }) });
let browserError = null;
try { await preview(kind, args); } catch (error) { browserError = error; }
if (!browserError || browserError.message !== "bad preview") fail("browser preview error behavior changed");

// Resolve two updatePreview calls out of order.  The first response must not
// repaint the box after the second request has become current.  Small import
// stubs keep this a DOM-free test of forms.js's real sequencing branch.
const coreUrl = dataUrl(`
  export const S = { forms: { fixture: { value: 1 } }, info: null };
  export const $ = () => null;
  export const $$ = () => [];
  export const confirmModal = () => Promise.resolve(false);
  export const el = () => ({ append() {} });
  export const ico = () => ({ });
  export const toast = () => {};
`);
const jobsUrl = dataUrl("export const jobs = {}; ");
const prepUrl = dataUrl("export const repoReady = () => true; ");
const navUrl = dataUrl("export const uiMode = () => \"advanced\"; ");
globalThis.releases = [];
const previewStubUrl = dataUrl(`
  export function preview() {
    return new Promise(resolve => globalThis.releases.push(resolve));
  }
`);
const formsForTest = formsSource
  .replace('from "./core.js"', `from "${coreUrl}"`)
  .replace('from "./jobs.js"', `from "${jobsUrl}"`)
  .replace('from "./preview.js"', `from "${previewStubUrl}"`)
  .replace('from "./prep.js"', `from "${prepUrl}"`)
  .replace('from "./nav.js"', `from "${navUrl}"`);
const forms = await import(dataUrl(formsForTest));
const box = {
  dataset: { kind: "fixture" }, isConnected: true, innerHTML: "initial", paints: 0,
  append() { this.paints++; },
};
globalThis.document = {
  querySelector() { return box; },
  getElementById() { return null; },
};
forms.updatePreview("fixture");
forms.updatePreview("fixture");
if (globalThis.releases.length !== 2) fail("preview requests were not issued for both form states");
globalThis.releases[1]({ cmd: "new" });
await Promise.resolve();
const paintsAfterNew = box.paints;
globalThis.releases[0]({ cmd: "old" });
await Promise.resolve();
if (!paintsAfterNew || box.paints !== paintsAfterNew) fail("an out-of-order preview response repainted the box");

console.log("ok: native/browser preview payloads, failure isolation, HTTP errors, import resolution, and stale response sequencing pass");
''',
        text=True,
        capture_output=True,
    )
    if node.returncode:
        detail = (node.stderr or node.stdout).strip()
        return fail(f"Node preview contract failed: {detail}")
    print(node.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
