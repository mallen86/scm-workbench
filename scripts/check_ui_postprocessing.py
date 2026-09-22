#!/usr/bin/env python3
"""Static and Node contracts for Simple and Advanced image post-processing UI."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "postprocess-transport.js"
HIGHLIGHT = UI / "python-highlight.js"
PAGE = UI / "pages" / "postprocess.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if not FACADE.is_file() or not HIGHLIGHT.is_file() or not PAGE.is_file():
        return fail("post-processing transport, highlighter, or page module is missing")
    facade = FACADE.read_text(encoding="utf-8")
    highlighter = HIGHLIGHT.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    nav = (UI / "nav.js").read_text(encoding="utf-8")
    app = (UI / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    forms = (UI / "forms.js").read_text(encoding="utf-8")
    css = (ROOT / "ui" / "theme.css").read_text(encoding="utf-8")
    fetch = (UI / "pages" / "fetch.js").read_text(encoding="utf-8")

    for required in (
        'nativeCall("postprocessors.list")',
        'nativeCall("postprocessors.guide")',
        'nativeCall("postprocessors.get", params)',
        'params.revision_hash = revisionHash',
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
        'postprocessors.guide()',
        'body.innerHTML = result.body',
        'backdrop.onclick = close',
        'Bundled with SCM Workbench v',
        'aria-label": "Open the image post-processing guide',
        'pp-guide-modal',
        'import { renderPythonHighlight } from "../python-highlight.js";',
        'source.value = d.source',
        'state.dirty',
        'preserveDirty',
        'const keepDraft = !simple && preserveDirty && state.dirty',
        'const prefillId = S.postprocessPrefill?.processor_id || null',
        'const requested = prefillId || selectId',
        'The processor used by this job is no longer available to run.',
        '? state.selected : null',
        'p.id === state.selected',
        'pp-cursor',
        'pp-source-highlight',
        'pp-revision-picker',
        'Saved processor revision',
        'Loading an older revision does not make it active until you save it.',
        'postprocessors.get(state.loaded.id, chosen)',
        'paintSourceHighlight(source)',
        'syncSourceHighlightScroll(source)',
        'Resolved library lock',
        'const isStale = p =>',
        'Libraries need reinstall',
        'The selected Python runtime changed.',
        'Reinstall libraries for this Python first',
        'Trust this Python revision?',
        'cannot safely sandbox arbitrary Python',
        'doRun("postprocess_dependencies"',
        'doRun("postprocess_images"',
        'Original images were not changed',
        'COMMAND_PREVIEW_EVENT',
        'image_count',
        'recognized image',
        'const host = $(".pp-run-card", root) || root',
        'host.append(status)',
        'class: "pp-progress"',
        'Go to Create PDF',
        'function renderSimplePostprocess()',
        'state.processors.filter(canRun)',
        'p.ready_to_run === true',
        'class: "input pp-simple-select"',
        'Simple mode only shows processors that are already installed and trusted.',
        'The built-in Simple Upscaler is ready without any downloads.',
        'JPEG and PNG output is always set to 1200 DPI; the source DPI is not multiplied.',
        'Built-in processors are read-only',
    ):
        if required not in page:
            return fail(f"post-processing page is missing {required}")
    if ".innerHTML = d.source" in page or "innerHTML: d.source" in page or "innerHTML" in highlighter:
        return fail("processor source highlighting uses unsafe HTML insertion")
    for required in ("pythonHighlightTokens", "renderPythonHighlight", "createTextNode", "textContent"):
        if required not in highlighter:
            return fail(f"Python source highlighter is missing {required}")
    if ('import "./pages/postprocess.js";' not in app or 'postprocess: "Image post-processing"' not in nav or
            'data-page="postprocess" data-section="workflow"' not in index or
            'data-page="postprocess" data-section="workflow" data-simple-hide' in index):
        return fail("post-processing route is not visible in both interface modes")
    simple_pages = re.search(r'export const SIMPLE_PAGES\s*=\s*\[(.*?)\]', nav, re.S)
    if not simple_pages or "postprocess" not in simple_pages.group(1):
        return fail("post-processing is missing from Simple-mode routes")
    for marker in (".pp-editor", ".pp-source", ".pp-source-wrap", ".pp-source-highlight", ".py-keyword", ".py-string", ".py-comment", ".pp-run-status", ".pp-progress", ".pp-lock", ".pp-guide-modal", ".pp-guide-content", ".pp-library > .card-head {", ".pp-library > .card-head .actions", "@media (max-width: 760px)"):
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
const highlighter = await import(dataUrl(fs.readFileSync(process.argv[2], "utf8")));
const fail = message => { throw new Error(message); };

const pythonSample = '@cached\nasync def resize(value: int = 0x10):\n    """Docstring"""\n    # note\n    return str(value) + f"{value}"\n';
const pythonTokens = highlighter.pythonHighlightTokens(pythonSample);
if (pythonTokens.map(token => token.text).join("") !== pythonSample)
  fail("Python highlighting changed source text");
const tokenTypes = new Set(pythonTokens.map(token => token.type));
for (const expected of ["decorator", "keyword", "definition", "builtin", "number", "string", "comment", "operator"])
  if (!tokenTypes.has(expected)) fail(`Python highlighting missed ${expected}`);
const markupSource = '<script>alert("x")</script>';
if (highlighter.pythonHighlightTokens(markupSource).map(token => token.text).join("") !== markupSource)
  fail("Python highlighting did not preserve markup-like source as text");
const oversized = "x".repeat(highlighter.PYTHON_HIGHLIGHT_MAX_CHARS + 1);
const oversizedTokens = highlighter.pythonHighlightTokens(oversized);
if (oversizedTokens.length !== 1 || oversizedTokens[0].type !== "plain" || oversizedTokens[0].text !== oversized)
  fail("oversized Python highlighting did not fall back to plain text");
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
await facade.guide();
await facade.get("abc/def");
await facade.get("abc", "f".repeat(64));
await facade.save({ processor_id: null, name: "Example", source: "def process_image(image_path, context):\n    pass\n", requirements: "" });
await facade.trust("abc", "f".repeat(64), null);
await facade.remove("abc", "f".repeat(64));
await facade.status("abc");
const methods = nativeCalls.map(call => call.rpc?.method);
if (JSON.stringify(methods) !== JSON.stringify([
  "postprocessors.list", "postprocessors.guide", "postprocessors.get", "postprocessors.get",
  "postprocessors.save", "postprocessors.trust", "postprocessors.delete", "postprocessors.status",
])) fail("native post-processing method routing is incorrect");
if (nativeCalls[3].rpc.params.revision_hash !== "f".repeat(64))
  fail("native historical revision was not bound to the request");
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
await facade.guide();
await facade.get("abc/def");
await facade.get("abc", "e".repeat(64));
await facade.save({ name: "Example", source: "def process_image(image_path, context):\n    pass\n", requirements: "" });
await facade.duplicate("abc", "Copy", "1".repeat(64));
await facade.trust("abc", "2".repeat(64), "3".repeat(64));
await facade.remove("abc", "4".repeat(64));
await facade.status("abc");
const routes = fetchCalls.map(call => `${call.options?.method || "GET"} ${call.url}`);
if (JSON.stringify(routes) !== JSON.stringify([
  "GET /api/postprocessors", "GET /api/postprocessors/guide", "GET /api/postprocessors/abc%2Fdef",
  `GET /api/postprocessors/abc?revision=${"e".repeat(64)}`,
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
        [node, "--input-type=module", "-e", script, str(FACADE), str(HIGHLIGHT)],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return fail("post-processing transport contract failed")
    print("OK: Simple and Advanced image post-processing UI and transport contracts are intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
