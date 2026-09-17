#!/usr/bin/env python3
"""Static and Node contracts for Advanced image post-processing UI transport."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "postprocess-transport.js"
PAGE = UI / "pages" / "postprocess.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if not FACADE.is_file() or not PAGE.is_file():
        return fail("post-processing transport or page module is missing")
    facade = FACADE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    nav = (UI / "nav.js").read_text(encoding="utf-8")
    app = (UI / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    forms = (UI / "forms.js").read_text(encoding="utf-8")
    css = (ROOT / "ui" / "theme.css").read_text(encoding="utf-8")
    fetch = (UI / "pages" / "fetch.js").read_text(encoding="utf-8")

    for required in (
        'nativeCall("postprocessors.list")',
        'nativeCall("postprocessors.get", { processor_id: id })',
        'nativeCall("postprocessors.save", params)',
        'nativeCall("postprocessors.duplicate", params)',
        'nativeCall("postprocessors.trust", params)',
        'nativeCall("postprocessors.delete", params)',
        'nativeCall("postprocessors.status", { processor_id: processorId })',
        'invoke("wb_postprocessor_import", {})',
        'file.text()',
        'new TextEncoder().encode(source).length > MAX_SOURCE_BYTES',
    ):
        if required not in facade:
            return fail(f"post-processing facade is missing {required}")

    for path in sorted(UI.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        if path != FACADE and re.search(r'["\']/api/postprocessors', source):
            return fail(f"{path.relative_to(ROOT)} bypasses the post-processing facade")

    for required in (
        'PAGES.postprocess = root =>',
        'if (uiMode() === "simple")',
        'source.value = d.source',
        'state.dirty',
        'preserveDirty',
        'const keepDraft = preserveDirty && state.dirty',
        '? state.selected : null',
        'p.id === state.selected',
        'pp-cursor',
        'Resolved library lock',
        'Trust this Python revision?',
        'cannot safely sandbox arbitrary Python',
        'doRun("postprocess_dependencies"',
        'doRun("postprocess_images"',
        'Original images were not changed',
        'COMMAND_PREVIEW_EVENT',
        'image_count',
        'recognized image',
        'Go to Create PDF',
    ):
        if required not in page:
            return fail(f"post-processing page is missing {required}")
    if ".innerHTML = d.source" in page or "innerHTML: d.source" in page:
        return fail("processor source is inserted as HTML")
    if ('import "./pages/postprocess.js";' not in app or 'postprocess: "Image post-processing"' not in nav or
            'data-page="postprocess"' not in index or 'data-page="postprocess" data-section="workflow" data-simple-hide' not in index):
        return fail("post-processing route or Advanced-only navigation item is missing")
    simple_pages = re.search(r'export const SIMPLE_PAGES\s*=\s*\[(.*?)\]', nav, re.S)
    if not simple_pages or "postprocess" in simple_pages.group(1):
        return fail("post-processing was added to Simple-mode routes")
    for marker in (".pp-editor", ".pp-source", ".pp-run-status", ".pp-lock", "@media (max-width: 760px)"):
        if marker not in css:
            return fail(f"responsive post-processing CSS is missing {marker}")
    if ('go("postprocess", { scope: "both" })' not in fetch or "postprocessPrefill" not in nav or
            "Post-process images" not in fetch):
        return fail("fetch completion does not offer manual post-processing navigation")
    if re.search(r'doRun\s*\(\s*["\']postprocess_images', fetch):
        return fail("fetch completion automatically runs a processor")
    if 'if (o.hidden) return false;' not in forms or '!o.hidden' not in forms:
        return fail("forms expose internal processor revision controls")

    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the post-processing transport contract")
    script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[1], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const transport = dataUrl(`export function getTauriInvoke() { return globalThis.nativeInvoke ? globalThis.nativeInvoke.bind(globalThis) : null; }`);
const facade = await import(dataUrl(source.replace('from "./transport.js"', `from "${transport}"`)));
const fail = message => { throw new Error(message); };
let fetchCalls = [];
globalThis.fetch = async (url, options) => {
  fetchCalls.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ ok: true, url }) };
};

// Once native transport is selected, every public method stays native and a
// rejection is final rather than silently retried over HTTP.
const nativeCalls = [];
globalThis.nativeInvoke = function(command, rpc) {
  nativeCalls.push({ command, rpc, receiver: this });
  return Promise.resolve({ ok: true, command, rpc });
};
await facade.list();
await facade.get("abc/def");
await facade.save({ processor_id: null, name: "Example", source: "def process_image(image_path, context):\n    pass\n", requirements: "" });
await facade.trust("abc", "f".repeat(64), null);
await facade.remove("abc", "f".repeat(64));
await facade.status("abc");
const methods = nativeCalls.map(call => call.rpc?.method);
if (JSON.stringify(methods) !== JSON.stringify([
  "postprocessors.list", "postprocessors.get", "postprocessors.save",
  "postprocessors.trust", "postprocessors.delete", "postprocessors.status",
])) fail("native post-processing method routing is incorrect");
if (nativeCalls.some(call => call.command !== "wb_rpc" || call.receiver !== globalThis))
  fail("native post-processing invoke binding is incorrect");
if (fetchCalls.length) fail("native operations used HTTP fallback");
globalThis.nativeInvoke = () => Promise.reject(new Error("native down"));
let rejected = false;
try { await facade.list(); } catch (error) { rejected = error.message === "native down"; }
if (!rejected || fetchCalls.length) fail("native rejection silently fell back to HTTP");

// Browser mode uses purpose-specific HTTP routes and encodes path components.
delete globalThis.nativeInvoke;
fetchCalls = [];
await facade.list();
await facade.get("abc/def");
await facade.save({ name: "Example", source: "def process_image(image_path, context):\n    pass\n", requirements: "" });
await facade.duplicate("abc", "Copy", "1".repeat(64));
await facade.trust("abc", "2".repeat(64), "3".repeat(64));
await facade.remove("abc", "4".repeat(64));
await facade.status("abc");
const routes = fetchCalls.map(call => `${call.options?.method || "GET"} ${call.url}`);
if (JSON.stringify(routes) !== JSON.stringify([
  "GET /api/postprocessors", "GET /api/postprocessors/abc%2Fdef",
  "POST /api/postprocessors", "POST /api/postprocessors/abc/duplicate",
  "POST /api/postprocessors/abc/trust", "DELETE /api/postprocessors/abc",
  "GET /api/postprocessors/abc/status",
])) fail(`browser post-processing routes are incorrect: ${JSON.stringify(routes)}`);

// Client-side caps reject oversized text before transport.
const beforeCap = fetchCalls.length;
let tooLarge = false;
try { await facade.save({ source: "x".repeat(facade.MAX_SOURCE_BYTES + 1), requirements: "" }); }
catch (error) { tooLarge = /too large/i.test(error.message); }
if (!tooLarge || fetchCalls.length !== beforeCap) fail("source cap did not fail closed before transport");

// Browser import reads bounded contents and never forwards a local path.
let imported = null;
globalThis.document = { createElement() {
  return {
    files: [{ name: "resize.py", size: 12, text: async () => "def process_image(image_path, context):\n    pass\n" }],
    click() { this.onchange(); },
  };
} };
const importedResult = await facade.importSource({ saveDraft: async payload => { imported = payload; return { ok: true }; } });
if (!importedResult?.ok || imported.name !== "resize" || "path" in imported || !imported.source.includes("process_image"))
  fail("browser import did not pass bounded source-only data");

// Native import uses only the private picker command and remains fail-closed.
delete globalThis.document;
const picker = [];
globalThis.nativeInvoke = (command, args) => { picker.push({ command, args }); return Promise.resolve({ ok: true }); };
await facade.importSource();
if (JSON.stringify(picker) !== JSON.stringify([{ command: "wb_postprocessor_import", args: {} }]))
  fail("native import did not use the private picker command");
const requestsBeforePickerFailure = fetchCalls.length;
globalThis.nativeInvoke = () => Promise.reject(new Error("picker down"));
rejected = false;
try { await facade.importSource(); } catch (error) { rejected = error.message === "picker down"; }
if (!rejected || fetchCalls.length !== requestsBeforePickerFailure)
  fail("native import rejection retried over HTTP");
'''
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, str(FACADE)],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return fail("post-processing transport contract failed")
    print("OK: Advanced image post-processing UI and transport contracts are intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
