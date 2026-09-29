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
INSTALL_STATE = UI / "postprocess-install-state.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if not FACADE.is_file() or not HIGHLIGHT.is_file() or not PAGE.is_file() or not INSTALL_STATE.is_file():
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
    server = (ROOT / "scm_workbench" / "server.py").read_text(encoding="utf-8")

    for required in (
        'nativeCall("postprocessors.list")',
        'nativeCall("postprocessors.guide")',
        'nativeCall("postprocessors.get", params)',
        'params.revision_hash = revisionHash',
        'nativeCall("postprocessors.save", params)',
        'nativeCall("postprocessors.duplicate", params)',
        'nativeCall("postprocessors.trust", params)',
        'nativeCall("postprocessors.delete", params)',
        'nativeCall("postprocessors.optional.remove", params)',
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
        'state.imageScope = first(detail.args?.scope) || "both"',
        'if (state.imageScope !== scope) state.imageCount = null',
        'image_count',
        'recognized image',
        'Connect an SCM checkout before processing images',
        'const host = $(".pp-run-card", root) || root',
        'host.append(status)',
        'class: "pp-progress"',
        'const result = await jobs.kill(jobId)',
        'if (uiMode() === "simple") panel.append(',
        'disabled: stoppingId === running.id',
        'Cancel processing',
        'Go to Create PDF',
        'function renderSimplePostprocess()',
        'state.processors.filter(canRun)',
        'p.ready_to_run === true',
        'class: "input pp-simple-select"',
        'class: "plugin-grid pp-simple-builtins"',
        'simpleProcessorChoices(state.processors, state.selected, lastCustomProcessorId)',
        'const { builtins, custom } = choices',
        'pp-custom-tile',
        'customPicker.hidden = !customSelected',
        'tile.disabled = isCustom && !custom.length',
        'if (grid.dataset.tilesKey !== tilesKey)',
        'if (select.dataset.optionsKey !== optionsKey)',
        'You can install the Advanced AI Upscaler before SCM is ready;',
        'The built-in Simple Upscaler is ready without any downloads.',
        'Remove model & libraries',
        'Cancel installation',
        'Follow the installation job in the sidebar.',
        'onError: showStartError',
        'loadInstallFailure(selectedInstall)',
        'jobs.log(job.id, 0, 4096)',
        'jobs.log(job.id, activity.after, 64)',
        'class: "small pp-model-status"',
        'class: "small pp-install-summary"',
        'state.installStartError = { processorId: p.id, at: Date.now(), message }',
        'postprocessors.removeOptional',
        'const result = await jobs.kill(state.installJob.id)',
        'JPEG and PNG output is always set to 1200 DPI; the source DPI is not multiplied.',
        'Built-in processors are read-only',
        'Duplicate Advanced Upscaler source?',
        "It does not inherit Workbench's installed AI model or libraries.",
        'supply your own model path in the source',
    ):
        if required not in page:
            return fail(f"post-processing page is missing {required}")
    if 'pp-custom-picker' not in page or 'Choose a custom processor…' not in page:
        return fail("Simple mode must show the dropdown only for the Custom tile")
    if 'No custom processors are ready. Create, install, and trust one in Advanced mode.' in page:
        return fail("Simple mode must not show the removed custom-processor setup hint")
    if not re.search(r'\.pp-custom-picker\[hidden\]\s*\{\s*display:\s*none;', css):
        return fail("Custom picker styling must not override its hidden state")
    if 'export function simpleProcessorChoices' not in page:
        return fail("Simple-mode processor selection helper is missing")
    if page.count('formCard("postprocess_images", { run: false, preview: "summary" })') != 2:
        return fail("image post-processing must hide technical commands in both modes while keeping preview validation events")
    if '["back", "Back only"]' not in server:
        return fail("manifest-driven post-processing scope is missing Back only")
    if 'with no download during processing' in page or page.count('Processing can take a while depending on your computer.') != 2:
        return fail("AI Upscaler description must describe realistic processing time without obsolete download wording")
    if any(text in page for text in ('model and inference libraries are optional', 'plus optional libraries',
                                     'One-time optional download:', 'Install optional upscaler')):
        return fail("AI Upscaler installation copy incorrectly describes required components as optional")
    for required in ('This upscaler requires its model and inference libraries to be installed before use.',
                     'plus inference libraries.', 'at least 2 GB free', 'Install upscaler'):
        if required not in page:
            return fail(f"AI Upscaler installation copy is missing {required}")
    if '150–250 MB' in page:
        return fail("AI Upscaler installation estimate must account for the Linux GPU wheel")
    if 'never the app bundle' in page:
        return fail("optional installation confirmation uses platform-specific app bundle jargon")
    if 'if (!p.optional_model) actions.append(' in page:
        return fail("the Advanced Upscaler is missing its source-only Duplicate action")
    simple_setup = page.split('el("div", { class: "pp-model-setup", hidden: true },', 1)[-1].split('const run = el("section",', 1)[0]
    if not re.search(r'class: "pp-model-copy".*class: "[^"]*pp-model-state".*class: "[^"]*pp-model-cost".*class: "pp-model-actions".*class: "[^"]*pp-model-install".*class: "[^"]*pp-model-remove"', simple_setup, re.S):
        return fail("Simple-mode model status and installation actions must share one row")
    if (not re.search(r'\.pp-model-copy\s*\{[^}]*flex:\s*1[^}]*\}', css) or
            not re.search(r'\.pp-model-actions\s*\{[^}]*justify-content:\s*flex-end;[^}]*margin-left:\s*auto;', css)):
        return fail("Simple-mode model actions must align to the right of their status text")
    remove_action = 'el("button", { class: "btn danger pp-model-remove", type: "button", hidden: true, onclick: removeOptionalModel }, "Remove model & libraries")'
    if page.count(remove_action) != 2 or '.btn.danger {' not in css:
        return fail("Remove model & libraries must use the danger style in Simple and Advanced modes")
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
    for marker in (".pp-editor", ".pp-source", ".pp-source-wrap", ".pp-source-highlight", ".py-keyword", ".py-string", ".py-comment", ".pp-run-status", ".pp-progress", ".pp-cancel", ".pp-lock", ".pp-simple-detail", ".pp-guide-modal", ".pp-guide-content", ".pp-library > .card-head {", ".pp-library > .card-head .actions", "@media (max-width: 760px)"):
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
    helper_match = re.search(r"export function simpleProcessorChoices\(.*?\n}\n(?=const selectedProcessor)", page, re.S)
    if not helper_match:
        return fail("could not extract the Simple-mode selection helper for behavioral coverage")
    predicates = page[page.index('const revision ='):page.index('export function simpleProcessorChoices')]
    helper = predicates + helper_match.group(0).replace("export function", "function", 1)
    behavior = helper + r'''
const ready = (id, bundled = false) => ({ id, bundled, ready_to_run: true });
const processors = [ready("builtin", true), ready("custom-a"), ready("custom-b"), { id: "untrusted", ready_to_run: false }];
let selection = simpleProcessorChoices(processors, "builtin");
if (selection.customSelected || selection.builtins.length !== 1 || selection.custom.map(p => p.id).join(",") !== "custom-a,custom-b") throw Error("initial built-in selection/filter failed");
selection = simpleProcessorChoices(processors, "custom-b", selection.rememberedCustomId);
if (!selection.customSelected || selection.rememberedCustomId !== "custom-b") throw Error("history-prefilled custom selection failed");
selection = simpleProcessorChoices(processors, "builtin", selection.rememberedCustomId);
if (selection.customSelected || selection.customSelection !== "custom-b") throw Error("built-in selection did not retain previous custom choice");
for (const installed of [false, true]) {
  const source = [{ id: "ai", bundled: true, optional_model: true, ready_to_run: installed }, ready("simple", true)];
  const ordered = simpleProcessorChoices(source, "ai");
  if (ordered.builtins.map(p => p.id).join(",") !== "simple,ai") throw Error("Simple Upscaler must precede the AI upscaler regardless of installation state");
  if (source[0].id !== "ai") throw Error("Simple-mode ordering must not mutate the processor library");
}
const empty = simpleProcessorChoices([ready("builtin", true)], "builtin");
if (empty.custom.length || empty.customSelection !== null) throw Error("empty custom list was not filtered");
'''
    repaint = page[page.index('function repaintSimplePicker()'):page.index('async function showGuide()')]
    behavior += r'''
// Exercise the actual tile handlers, not just the pure selection helper.
class Element {
  constructor(tag, attrs = {}, ...children) {
    this.tag = tag; this.children = children; this.dataset = {}; this.attrs = attrs;
    this.classList = { toggle: (name, enabled) => { this[name] = enabled; } };
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "data-processor-id") this.dataset.processorId = value;
      else this[key] = value;
    }
  }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  querySelectorAll() { return this.children; }
  querySelector(selector) { return this.children.find(child => child.class === selector.slice(1)); }
  setAttribute(key, value) { this.attrs[key] = value; }
}
const el = (...args) => new Element(...args);
const grid = el("div"), select = el("select"), customPicker = el("label");
const document = { querySelector: selector => ({
  ".pp-simple-builtins": grid, ".pp-simple-select": select, ".pp-custom-picker": customPicker,
}[selector] || null) };
const state = { processors, selected: "builtin" };
const selectedProcessor = () => state.processors.find(p => p.id === state.selected);
let lastCustomProcessorId = null, patched = null;
const patchRunForm = () => { patched = state.selected; };
''' + repaint + r'''
repaintSimplePicker();
const builtinTile = grid.children[0], customTile = grid.children.at(-1);
if (!customPicker.hidden || builtinTile.attrs["aria-pressed"] !== "true") throw Error("built-in must hide custom dropdown");
customTile.onclick();
if (state.selected !== "custom-a" || patched !== "custom-a" || customPicker.hidden) throw Error("Custom tile must reveal dropdown and select a ready processor");
state.selected = "custom-b"; // dropdown or history selection
repaintSimplePicker();
if (select.value !== "custom-b" || !customTile.active) throw Error("custom prefill must activate Custom tile");
builtinTile.onclick();
if (!customPicker.hidden) throw Error("built-in must hide dropdown again");
customTile.onclick();
if (state.selected !== "custom-b") throw Error("tile handler captured a stale custom selection");
if (grid.children.at(-1) !== customTile || grid.children[0] !== builtinTile) throw Error("repaint replaced focused tiles");
state.processors = [ready("builtin", true), ready("custom-c"), ready("custom-d")];
state.selected = "builtin";
repaintSimplePicker();
customTile.onclick();
if (state.selected !== "custom-c" || state.loaded.id !== "custom-c") throw Error("tile handler captured a stale processor list");
state.processors = [ready("builtin", true)]; state.selected = "builtin";
repaintSimplePicker();
if (!grid.children.at(-1).disabled || !customPicker.hidden) throw Error("empty Custom tile must be disabled with dropdown hidden");
'''
    behavior_result = subprocess.run([node, "-e", behavior], text=True, capture_output=True)
    if behavior_result.returncode:
        return fail(f"Simple-mode selection behavior failed: {behavior_result.stderr.strip()}")
    script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[1], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const transport = dataUrl(`export function getTauriInvoke() { return globalThis.nativeInvoke ? globalThis.nativeInvoke.bind(globalThis) : null; }`);
const facade = await import(dataUrl(source.replace('from "./transport.js"', `from "${transport}"`)));
const highlighter = await import(dataUrl(fs.readFileSync(process.argv[2], "utf8")));
const installState = await import(dataUrl(fs.readFileSync(process.argv[3], "utf8")));
const fail = message => { throw new Error(message); };
const id = "629deb7c0e4b48968845537a28354d85";
const processor = { id, optional_model: true, ready_to_run: false };
const job = (status, ts = 1) => ({ id: `install-${ts}`, kind: "postprocess_dependencies", status, ts, args: { processor_id: id } });
const persisted = installState.latestInstallJob([job("fail", 1), job("running", 2)], id);
if (persisted?.id !== "install-2" || installState.latestInstallJob([job("ok")], "another-id"))
  fail("optional install status did not select the latest matching job");
if (installState.optionalInstallStatus(processor, job("running"), null, null).tone !== "running")
  fail("running installation is not visible");
const ongoing = job("running", Math.floor(Date.now() / 1000) - 610);
const progress = installState.optionalInstallStatus(processor, ongoing, null, null,
  { id: ongoing.id, step: "Downloading verified model (67 MB)" });
if (!progress.label.includes("10 min elapsed") || !progress.label.includes("Downloading verified model"))
  fail("long-running installation did not report elapsed time and the current installer step");
const failure = installState.optionalInstallStatus(processor, job("fail"), { id: "install-1", message: "Model hash mismatch" }, null);
if (failure.tone !== "fail" || !failure.label.includes("Model hash mismatch"))
  fail("a terminal installation failure did not retain its cause");
if (!installState.optionalInstallStatus({ ...processor, ready_to_run: true }, job("fail"), null, null).label.includes("Previous installed profile remains ready") ||
    !installState.optionalInstallStatus({ ...processor, ready_to_run: true }, job("killed"), null, null).label.includes("Previous installed profile remains ready"))
  fail("a failed/cancelled CUDA switch hid the old ready installation");
const noJob = installState.optionalInstallStatus(processor, null, null,
  { processorId: id, at: Date.now(), message: "SCM repo is not ready" });
if (noJob.tone !== "fail" || !noJob.label.includes("did not start") || !noJob.label.includes("SCM repo"))
  fail("failure before job creation did not remain visible");
if (installState.optionalInstallStatus({ ...processor, ready_to_run: true }, job("ok"), null, null).tone !== "ok")
  fail("completed installation is not shown as ready");

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
await facade.removeOptional("abc", "a".repeat(64));
await facade.status("abc");
const methods = nativeCalls.map(call => call.rpc?.method);
if (JSON.stringify(methods) !== JSON.stringify([
  "postprocessors.list", "postprocessors.guide", "postprocessors.get", "postprocessors.get",
  "postprocessors.save", "postprocessors.trust", "postprocessors.delete", "postprocessors.optional.remove", "postprocessors.status",
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
await facade.removeOptional("abc", "5".repeat(64));
await facade.status("abc");
const routes = fetchCalls.map(call => `${call.options?.method || "GET"} ${call.url}`);
if (JSON.stringify(routes) !== JSON.stringify([
  "GET /api/postprocessors", "GET /api/postprocessors/guide", "GET /api/postprocessors/abc%2Fdef",
  `GET /api/postprocessors/abc?revision=${"e".repeat(64)}`,
  "POST /api/postprocessors", "POST /api/postprocessors/abc/duplicate",
  "POST /api/postprocessors/abc/trust", "DELETE /api/postprocessors/abc",
  "POST /api/postprocessors/abc/optional-remove", "GET /api/postprocessors/abc/status",
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
        [node, "--input-type=module", "-e", script, str(FACADE), str(HIGHLIGHT), str(INSTALL_STATE)],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return fail("post-processing transport contract failed")

    start_error_script = r'''
import fs from "node:fs";
const encode = text => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
let source = fs.readFileSync(process.argv[1], "utf8");
const errors = [], toasts = [];
const S = { manifest: { postprocess_dependencies: { needs: ["scm"] } },
  info: { server: { is_packaged: false }, scm: { found: true } }, forms: {}, jobs: [] };
globalThis.__installStart = { S, errors, toasts, reject: false };
const stubs = {
  "./core.js": `const v=globalThis.__installStart; export const S=v.S; export const $=()=>null;
    export const $$=()=>[]; export const el=()=>({}); export const ico=()=>"";
    export const toast=(...args)=>v.toasts.push(args); export const confirmModal=async()=>true;`,
  "./job-events.js": `export const publishJobsUpdated=()=>{};`,
  "./job-notices.js": `export const syncJobNotices=()=>{};`,
  "./jobs.js": `export const jobs={start:async()=>{if(globalThis.__installStart.reject)throw Error("native down");
    return {ok:false,errors:["could not prepare installation"]};}};`,
  "./preview.js": `export const preview=()=>{};`,
  "./prep.js": `export const repoReady=()=>true;`,
  "./nav.js": `export const uiMode=()=>"simple";`,
  "./settings-transport.js": `export const canPickDirectory=()=>false; export const pickDirectory=()=>{};`,
};
for (const [path, stub] of Object.entries(stubs)) {
  const needle = `from "${path}"`;
  if (!source.includes(needle)) throw Error(`missing import ${path}`);
  source = source.replace(needle, `from "${encode(stub)}"`);
}
const { doRun } = await import(encode(source));
const onError = message => errors.push(message);
let result = await doRun("postprocess_dependencies", null, { args: {}, onError });
if (result !== null || errors.at(-1) !== "could not prepare installation" || toasts.at(-1)?.[0] !== "err")
  throw Error("rejected installer start was not retained for in-page status");
globalThis.__installStart.reject = true;
result = await doRun("postprocess_dependencies", null, { args: {}, onError });
if (result !== null || errors.at(-1) !== "native down")
  throw Error("native failure before job creation was not retained");
S.info.scm.found = false;
result = await doRun("postprocess_dependencies", null, { args: {}, onError });
if (result !== null || !errors.at(-1)?.includes("SCM repo not found"))
  throw Error("preflight rejection before job creation was not retained");
'''
    result = subprocess.run([node, "--input-type=module", "-e", start_error_script,
                             str(UI / "forms.js")], cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        return fail("post-processing start rejection contract failed")

    cancel_script = r'''
import fs from "node:fs";
const encode = text => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
let source = fs.readFileSync(process.argv[1], "utf8");
const fail = message => { throw new Error(message); };
let mode = "simple", killResult, tick;
const calls = [], toasts = [], notices = [];
const S = { jobs: [{ id: "job-one", kind: "postprocess_images", status: "running", progress: { current: 1, total: 3 } }] };
const el = (tag, attrs = {}, ...children) => ({
  tag, className: attrs.class || "", hidden: !!attrs.hidden, disabled: !!attrs.disabled,
  onclick: attrs.onclick, children,
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; },
});
const buttons = node => [node, ...(node.children || []).flatMap(child => typeof child === "object" && child ? buttons(child) : [])]
  .filter(child => child.tag === "button" && child.className.includes("pp-cancel"));
globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector() { return null; } };
globalThis.setInterval = fn => { tick = fn; return 1; };
globalThis.clearInterval = () => {};
globalThis.__cancelTest = { S, el, calls, toasts, notices, mode: () => mode, kill: id => { calls.push(id); return killResult(id); } };
const modules = {
  "../core.js": `const x = globalThis.__cancelTest; export const PAGES = {}; export const S = x.S;
    export const $ = (_selector, root) => root; export const el = x.el;
    export const toast = (...args) => x.toasts.push(args);
    export const confirmModal = () => {}; export const ico = () => ""; export const pageHead = () => {};`,
  "../forms.js": `export const afterFormChange = () => {}; export const COMMAND_PREVIEW_EVENT = "preview";
    export const doRun = () => {}; export const formArgs = () => null; export const formCard = () => {};`,
  "../jobs.js": `export const jobs = { list: async () => ({ jobs: globalThis.__cancelTest.S.jobs }),
    kill: id => globalThis.__cancelTest.kill(id) };`,
  "../nav.js": `export const go = () => {}; export const uiMode = () => globalThis.__cancelTest.mode();`,
  "../python-highlight.js": `export const renderPythonHighlight = () => {};`,
  "../postprocess-transport.js": `export const postprocessors = {};`,
  "../postprocess-install-state.js": fs.readFileSync(process.argv[2], "utf8"),
  "./utilities.js": `export const watchJobDone = () => {};`,
  "../job-notices.js": `export const syncJobNotices = jobs => globalThis.__cancelTest.notices.push(jobs.map(job => job.status));`,
  "../job-notice-progress.js": `export const jobNoticeProgress = job => ({fraction: job.progress?.current / job.progress?.total || 0, text: "Processing", warning: ""});`,
};
for (const [path, stub] of Object.entries(modules)) {
  const needle = `from "${path}"`;
  if (!source.includes(needle)) fail(`post-processing import changed: ${path}`);
  source = source.replace(needle, `from "${encode(stub)}"`);
}
const { attachRunStatus } = await import(encode(source + "\nexport { attachRunStatus };"));
const root = el("div");
attachRunStatus(root);
await new Promise(resolve => setImmediate(resolve));
let cancel = buttons(root);
if (cancel.length !== 1 || cancel[0].disabled || cancel[0].children.at(-1) !== "Cancel processing")
  fail("Simple running processor job has no enabled cancel button");
let complete;
killResult = () => new Promise(resolve => { complete = resolve; });
const pending = cancel[0].onclick();
cancel = buttons(root);
if (calls.length !== 1 || calls[0] !== "job-one" || !cancel[0].disabled || cancel[0].children.at(-1) !== "Stopping…")
  fail("cancellation did not target the active job and disable repeated clicks");
await cancel[0].onclick();
if (calls.length !== 1) fail("repeated cancel click issued a second request");
complete({ ok: true });
await pending;
S.jobs[0].status = "killed";
await tick();
if (notices.at(-1)?.[0] !== "killed") fail("processor completion left the sidebar notice running");
if (buttons(root).length || !root.children[0].children[0].children[0].children[0].includes("Original images were not changed."))
  fail("terminal processor job still offers cancellation or lost its safe-outcome message");
mode = "advanced";
S.jobs = [{ id: "job-two", kind: "postprocess_images", status: "running" }];
await tick();
if (buttons(root).length) fail("Advanced processor status gained a Simple-only cancel button");
mode = "simple";
killResult = () => Promise.reject(new Error("transport failed"));
await tick();
cancel = buttons(root);
await cancel[0].onclick();
if (buttons(root)[0].disabled || !toasts.some(([kind, text]) => kind === "err" && text === "transport failed"))
  fail("failed cancellation did not restore the action and report the error");
root.__dispose();
'''
    result = subprocess.run(
        [node, "--input-type=module", "-e", cancel_script, str(PAGE), str(INSTALL_STATE)],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return fail("Simple post-processing cancellation contract failed")
    duplicate_script = r'''
import fs from "node:fs";
const encode = text => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
let source = fs.readFileSync(process.argv[1], "utf8");
const fail = message => { throw new Error(message); };
let allow = false;
const confirmations = [], calls = [], toasts = [];
const original = { id: "advanced", name: "Advanced Upscaler", revision: "a".repeat(64),
  optional_model: true, bundled: true, trusted: true, requirements: ["onnxruntime==1.30.0"] };
const copy = { id: "source-copy", name: "Advanced Upscaler copy", revision: "b".repeat(64),
  optional_model: false, bundled: false, trusted: false, requirements: original.requirements,
  source: "def process_image(image_path, context):\n    pass\n" };
const library = { children: [], append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; } };
const el = (tag, attrs = {}, ...children) => ({
  tag, onclick: attrs.onclick, children,
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; },
});
const buttons = node => [node, ...(node.children || []).flatMap(child => typeof child === "object" && child ? buttons(child) : [])]
  .filter(child => child.tag === "button" && child.children.includes("Duplicate"));
globalThis.document = { querySelector: selector => selector === ".pp-library-list" ? library : null };
const data = { calls, toasts, el, confirm: async spec => { confirmations.push(spec); return allow; },
  list: async () => ({ processors: calls.length ? [original, copy] : [original] }),
  get: async id => id === copy.id ? copy : original,
  duplicate: async (...args) => { calls.push(args); return { ok: true, processor: copy }; } };
globalThis.__duplicateTest = data;
const modules = {
  "../core.js": `const x = globalThis.__duplicateTest; export const PAGES = {}; export const S = {jobs: []};
    export const $ = () => null; export const el = x.el; export const toast = (...args) => x.toasts.push(args);
    export const confirmModal = x.confirm; export const ico = () => ""; export const pageHead = () => {};`,
  "../forms.js": `export const afterFormChange = () => {}; export const COMMAND_PREVIEW_EVENT = "preview";
    export const doRun = () => {}; export const formArgs = () => null; export const formCard = () => {};`,
  "../jobs.js": `export const jobs = {};`,
  "../nav.js": `export const go = () => {}; export const uiMode = () => "advanced";`,
  "../python-highlight.js": `export const renderPythonHighlight = () => {};`,
  "../postprocess-transport.js": `const x = globalThis.__duplicateTest;
    export const postprocessors = { list: x.list, get: x.get, duplicate: x.duplicate };`,
  "../postprocess-install-state.js": fs.readFileSync(process.argv[2], "utf8"),
  "./utilities.js": `export const watchJobDone = () => {};`,
  "../job-notices.js": `export const syncJobNotices = () => {};`,
  "../job-notice-progress.js": `export const jobNoticeProgress = () => ({fraction: 0, text: "Processing", warning: ""});`,
};
for (const [path, stub] of Object.entries(modules)) {
  const needle = `from "${path}"`;
  if (!source.includes(needle)) fail(`post-processing import changed: ${path}`);
  source = source.replace(needle, `from "${encode(stub)}"`);
}
const { state, repaintLibrary, duplicateProcessor } = await import(encode(source + "\nexport { state, repaintLibrary, duplicateProcessor };"));
state.processors = [original]; state.selected = original.id; state.loaded = original;
repaintLibrary();
if (buttons(library).length !== 1) fail("Advanced Upscaler has no Duplicate action in the library");
await duplicateProcessor(original);
if (calls.length || confirmations.length !== 1 || !confirmations[0].text.includes("does not inherit"))
  fail("source-only copy needs an explicit warning before creation");
allow = true;
await duplicateProcessor(original);
if (calls.length !== 1 || calls[0][0] !== original.id || calls[0][2] !== original.revision)
  fail("source-only Duplicate did not use the approved built-in revision");
if (state.selected !== copy.id || state.loaded?.bundled || state.loaded?.optional_model || buttons(library).length !== 2)
  fail("the editable source copy was not selected and shown next to the built-in");
if (!toasts.some(([kind, text]) => kind === "ok" && text.includes("Configure its model and libraries")))
  fail("source-only duplication did not explain the next steps");
'''
    result = subprocess.run(
        [node, "--input-type=module", "-e", duplicate_script, str(PAGE), str(INSTALL_STATE)],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        sys.stderr.write(result.stdout[-2000:])
        stderr = re.sub(r"data:text/javascript;base64,[A-Za-z0-9+/=]+", "<postprocess-page>", result.stderr)
        sys.stderr.write(stderr[-3000:])
        return fail("Advanced Upscaler source-only duplication contract failed")
    if ('import { jobNoticeProgress } from "../job-notice-progress.js";' not in page or
            'const progress = jobNoticeProgress(running);' not in page or
            'progress.fraction ?? 0' not in page or 'progress.text' not in page or
            'syncJobNotices(S.jobs)' not in page or
            'if (uiMode() === "simple") panel.append(' not in page):
        return fail("Simple and Advanced processing status must share the job notice progress view")
    # The same install selector is rendered in both modes. Ensure its label,
    # select and helper have explicit vertical rhythm instead of touching.
    cuda_rule = re.search(r'\.pp-cuda-install\s*\{([^}]+)\}', css)
    if (page.count('cudaInstallControl(),') != 2 or not cuda_rule or
            not re.search(r'display:\s*grid', cuda_rule.group(1)) or
            not re.search(r'gap:\s*(?:[6-9]|1[0-9])px', cuda_rule.group(1)) or
            not re.search(r'margin:\s*(?:1[0-9]|2[0-9])px\s+0', cuda_rule.group(1)) or
            '.pp-cuda-install, .pp-editor .pp-cuda-install {' not in css or
            '.pp-cuda-install[hidden] { display: none; }' not in css or
            '.pp-cuda-profile { width: 100%; min-width: 0; }' not in css):
        return fail("Simple and Advanced CUDA install labels/selectors/helpers need responsive spacing")
    activity_script = r'''import fs from "node:fs";
const url = `data:text/javascript;base64,${Buffer.from(fs.readFileSync(process.argv[1])).toString("base64")}`;
const {jobNoticeProgress: view} = await import(url);
const job = {kind:"postprocess_images", status:"running", postprocess_cpu_warning:"cuda",
  progress:{current:0,total:2,activity:{index:1,total:2,name:"First.png",role:"front",
    phase:"tile",provider:"CPUExecutionProvider",tile:3,tiles:12}}};
let result = view(job);
if (result.fraction !== 0 || !result.text.includes("CUDA 12.x and cuDNN 9") ||
    !result.text.includes("First.png on CPU, tile 3 / 12"))
  throw new Error("CPU fallback/tile status missing before first completed image");
job.progress.current = 1;
job.progress.activity = {index:2,total:2,name:"Second.png",role:"back",
  phase:"initializing",provider:"CUDAExecutionProvider",tile:0,tiles:0};
result = view(job);
if (result.fraction !== 0.5 || !result.text.includes("Initializing Second.png on CUDA"))
  throw new Error("second image initialization lost monotonic image progress");
job.progress.activity = {index:1,total:2,name:"stale",phase:"tile",provider:"CPUExecutionProvider",tile:12,tiles:12};
if (view(job).text.includes("stale")) throw new Error("stale activity displayed");
job.postprocess_cpu_warning = "cuda13";
if (!view(job).text.includes("CUDA 13.x and cuDNN 9") || view(job).text.includes("CUDA 12.x"))
  throw new Error("CUDA 13 CPU fallback used CUDA 12 requirements");
job.postprocess_cpu_warning = "cuda";
if (!view(job).text.includes("CUDA 12.x and cuDNN 9"))
  throw new Error("legacy CUDA history lost its CUDA 12 meaning");
job.postprocess_cpu_warning = "cuda13";
job.postprocess_cpu_reason = "missing_cudnn";
job.progress.current = 0;
if (!view(job).text.includes("cuDNN 9 (libcudnn.so) is missing") ||
    !view(job).text.includes("using CPU instead of CUDA 13") ||
    view(job).text.includes("CUDA 12.x"))
  throw new Error("missing cuDNN at lazy CUDA inference was not reported in the running notice");
job.status = "ok";
if (!view(job).text.includes("cuDNN 9 (libcudnn.so) missing; CPU fallback used"))
  throw new Error("missing cuDNN was lost on successful terminal notice");
job.status = "fail";
if (!view(job).text.includes("cuDNN 9 (libcudnn.so) missing; CPU fallback attempted"))
  throw new Error("failed CPU retry was mislabeled successful or lost missing cuDNN reason");
job.postprocess_cpu_reason = "unrelated";
if (view(job).text.includes("libcudnn.so"))
  throw new Error("unrelated failure was mislabeled missing cuDNN");
'''
    result = subprocess.run([node, "--input-type=module", "-e", activity_script,
                             str(UI / "job-notice-progress.js")], cwd=ROOT,
                            text=True, capture_output=True)
    if result.returncode:
        return fail("shared CPU/tile progress contract failed: " + result.stderr[-1200:])
    profile_confirmation_script = r'''import fs from "node:fs";
const encode = text => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
let source = fs.readFileSync(process.argv[1], "utf8");
const confirmations = [], launches = [];
let mode = "simple", allow = false;
const S = {jobs: []};
globalThis.document = {querySelector: () => null, querySelectorAll: () => []};
globalThis.__profileTest = {S, confirmations, launches, getMode: () => mode, confirm: async value => {
  confirmations.push(value); return allow;
}, run: async (_kind, _button, options) => { launches.push(options.args); return {id: "fixture"}; }};
const modules = {
  "../core.js": `const x=globalThis.__profileTest; export const PAGES={}; export const S=x.S;
    export const $=()=>null; export const confirmModal=x.confirm; export const el=()=>({});
    export const ico=()=>""; export const pageHead=()=>{}; export const toast=()=>{};`,
  "../forms.js": `export const afterFormChange=()=>{}; export const COMMAND_PREVIEW_EVENT="preview";
    export const doRun=(...args)=>globalThis.__profileTest.run(...args);
    export const formArgs=()=>null; export const formCard=()=>{};`,
  "../jobs.js": `export const jobs={};`,
  "../nav.js": `export const go=()=>{}; export const uiMode=()=>globalThis.__profileTest.getMode();`,
  "../python-highlight.js": `export const renderPythonHighlight=()=>{};`,
  "../postprocess-transport.js": `export const postprocessors={};`,
  "../postprocess-install-state.js": fs.readFileSync(process.argv[2], "utf8"),
  "./utilities.js": `export const watchJobDone=()=>{};`,
  "../job-notices.js": `export const syncJobNotices=()=>{};`,
  "../job-notice-progress.js": `export const jobNoticeProgress=()=>({});`,
};
for (const [path, stub] of Object.entries(modules)) {
  const needle=`from "${path}"`;
  if (!source.includes(needle)) throw new Error(`missing import ${path}`);
  source=source.replace(needle, `from "${encode(stub)}"`);
}
const {state, installLibraries} = await import(encode(source+"\nexport {state, installLibraries};"));
const id="629deb7c0e4b48968845537a28354d85";
const installed={id,revision:"a".repeat(64),optional_model:true,ready_to_run:true,cuda_profile:"cuda12"};
for (const candidateMode of ["simple", "advanced"]) {
  mode=candidateMode;
  state.loaded={...installed,cuda_detection:{recommended:"cuda13",reason:"CUDA 13 runtime visible"}};
  state.selected=id; state.processors=[state.loaded]; state.dirty=false; state.installing=false;
  state.cudaChoice="auto";
  allow=false;
  await installLibraries();
  const linux=confirmations.at(-1).text;
  if (!linux.includes("Install CUDA 13 profile") || !linux.includes("Current installed profile: CUDA 12") ||
      !linux.includes("cuDNN 9") || !linux.includes("CPU fallback"))
    throw new Error(`${candidateMode} mode did not explain the resolved Linux switch`);
  state.cudaChoice="cuda12";
  allow=true;
  await installLibraries();
  if (launches.at(-1)?.cuda_profile!=="cuda12" || launches.at(-1)?.requirements!=="")
    throw new Error(`${candidateMode} mode failed to submit its manual override`);
  state.loaded={...installed}; state.processors=[state.loaded]; state.installing=false;
  allow=false;
  await installLibraries();
  const other=confirmations.at(-1).text;
  if (!other.includes("compatible ONNX Runtime") || /CUDA|cuDNN|NVIDIA/.test(other))
    throw new Error(`${candidateMode} mode leaked Linux requirements into other platforms`);
  allow=true;
  await installLibraries();
  if (Object.hasOwn(launches.at(-1), "cuda_profile"))
    throw new Error(`${candidateMode} mode submitted a CUDA override outside Linux`);
}
'''
    result = subprocess.run([node, "--input-type=module", "-e", profile_confirmation_script,
                             str(PAGE), str(INSTALL_STATE)], cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        sys.stderr.write(re.sub(r"data:text/javascript;base64,[A-Za-z0-9+/=]+", "<postprocess-page>", result.stderr)[-2500:])
        return fail("Linux and non-Linux install confirmation behavior diverged")
    if (page.count('cudaInstallControl(),') != 2 or
            'option?.choices || []' not in page or
            'state.cudaChoice === "auto" ? detected.recommended' not in page or
            'Current installed profile:' not in page or
            'cuda_profile: cudaProfile' not in page or
            'Matching system CUDA, cuDNN 9' not in page or
            'running.postprocess_cpu_reason === "missing_cudnn"' not in page):
        return fail("both modes must offer backend-driven CUDA profiles with an explicit resolved switch")
    print("OK: Simple and Advanced image post-processing UI and transport contracts are intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
