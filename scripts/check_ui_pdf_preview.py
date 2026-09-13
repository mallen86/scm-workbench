#!/usr/bin/env python3
"""Executable contracts for the representative Create PDF front-page preview."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    transport = (UI / "pdf-preview-transport.js").read_text(encoding="utf-8")
    controller = (UI / "pdf-front-preview.js").read_text(encoding="utf-8")
    forms = (UI / "forms.js").read_text(encoding="utf-8")
    page = (UI / "pages" / "pdf.js").read_text(encoding="utf-8")
    nav = (UI / "nav.js").read_text(encoding="utf-8")
    css = (ROOT / "ui" / "theme.css").read_text(encoding="utf-8")

    for required in (
        'import { getTauriInvoke } from "./transport.js";',
        'export function startPdfPreview(args, signal)',
        'export function pollPdfPreview(operationId, signal)',
        'export function cancelPdfPreview(operationId, signal)',
        'invoke("wb_rpc", { method, params })',
        'fetch("/api/pdf-preview"',
        'method: "POST"',
        'operation_id: operationId',
    ):
        if required not in transport:
            return fail(f"PDF preview transport is missing {required}")
    for method in ("pdf_preview.start", "pdf_preview.poll", "pdf_preview.cancel"):
        if method not in transport:
            return fail(f"PDF preview transport does not select {method}")
    for path in sorted(UI.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        if path.name != "pdf-preview-transport.js" and "/api/pdf-preview" in source:
            return fail(f"{path.relative_to(ROOT)} bypasses the PDF preview transport")

    for required in (
        'export const COMMAND_PREVIEW_EVENT = "wb:command-preview";',
        "publishCommandPreview(kind, requestArgs, d);",
        "pending: true",
        "Couldn't validate these settings",
        "runBtn.disabled = true",
        "box.isConnected",
    ):
        if required not in forms:
            return fail(f"validated command-preview hook is missing {required}")
    for required in (
        "PREVIEW_DEBOUNCE_MS = 750",
        "POLL_MAX = 100",
        "RETRY_MAX = 3",
        "cancelPdfPreview(operationId)",
        "generation !== state.generation",
        "document.removeEventListener(COMMAND_PREVIEW_EVENT",
        "image.removeAttribute(\"src\")",
        'updatePreview("create_pdf")',
        "Representative front-page preview",
        "Card backs and final print quality are not shown.",
        "This is not a print proof.",
        "pdf-preview-refreshing",
        'document.addEventListener("visibilitychange"',
        "sampled > 16",
        "data.length > 700000",
    ):
        if required not in controller:
            return fail(f"PDF preview lifecycle contract is missing {required}")
    for required in (
        "pdfFrontPreviewPanel()",
        "commandPreview.before(visualPreview)",
        'preview: simple ? "summary" : true',
        "pdfValidationSummaryPanel()",
        "mountPdfFrontPreview(visualPreview, validationSummary)",
        "backImageControl.dispose()",
    ):
        if required not in page:
            return fail(f"Create PDF page integration is missing {required}")
    if "previous.__dispose()" not in nav or nav.index("previous.__dispose()") > nav.index('pageEl.innerHTML = ""'):
        return fail("page disposal does not run before detached DOM replacement")
    if 'class: `cmdbox${validationOnly ? " validation-only" : ""}`' not in forms:
        return fail("simple validation keeps a visible command-preview box")
    for selector in (
        ".pdf-front-preview", ".pdf-preview-stage", ".pdf-preview-image",
        ".pdf-preview-loading", ".pdf-preview-error", ".pdf-validation-summary",
    ):
        if selector not in css:
            return fail(f"PDF preview styling is missing {selector}")

    node = subprocess.run(
        ["node", "--input-type=module", "-", str(UI / "pdf-preview-transport.js")],
        input=r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value).toString("base64")}`;
const capabilityUrl = dataUrl(`
  export function getTauriInvoke() {
    const invoke = globalThis.window?.__TAURI_INTERNALS__?.invoke;
    return typeof invoke === "function" ? invoke.bind(globalThis.window.__TAURI_INTERNALS__) : null;
  }
`);
const moduleUrl = dataUrl(source.replace('from "./transport.js"', `from "${capabilityUrl}"`));
const api = await import(moduleUrl);
const fail = message => { throw new Error(message); };

const calls = [];
let fetches = 0;
globalThis.window = { __TAURI_INTERNALS__: { invoke(command, payload) {
  calls.push({ command, payload, receiver: this });
  return Promise.resolve({ ok: true });
} } };
globalThis.fetch = () => { fetches++; throw new Error("HTTP fallback"); };
await api.startPdfPreview({ paper_size: "letter" });
await api.pollPdfPreview("a".repeat(32));
await api.cancelPdfPreview("a".repeat(32));
if (calls.length !== 3 || calls.some(call => call.command !== "wb_rpc") ||
    calls.map(call => call.payload.method).join(",") !==
      "pdf_preview.start,pdf_preview.poll,pdf_preview.cancel" || fetches)
  fail("native PDF preview dispatch is incorrect");
if (calls[0].payload.params.args.paper_size !== "letter" ||
    calls[1].payload.params.operation_id !== "a".repeat(32) ||
    calls.some(call => call.receiver !== globalThis.window.__TAURI_INTERNALS__))
  fail("native PDF preview parameters or receiver are incorrect");

globalThis.window.__TAURI_INTERNALS__.invoke = () => Promise.reject(new Error("native down"));
let rejected = false;
try { await api.startPdfPreview({}); } catch (error) { rejected = error.message === "native down"; }
if (!rejected || fetches) fail("native PDF preview failure silently retried over HTTP");

delete globalThis.window;
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ ok: true }) };
};
const controller = new AbortController();
await api.startPdfPreview({ card_size: "standard" }, controller.signal);
await api.pollPdfPreview("b".repeat(32), controller.signal);
await api.cancelPdfPreview("b".repeat(32), controller.signal);
const bodies = requests.map(request => JSON.parse(request.options.body));
if (requests.length !== 3 || requests.some(request => request.url !== "/api/pdf-preview") ||
    requests.some(request => request.options.method !== "POST" ||
      request.options.headers["Content-Type"] !== "application/json" ||
      request.options.signal !== controller.signal) ||
    bodies[0].op !== "start" || bodies[0].args.card_size !== "standard" ||
    bodies[1].op !== "poll" || bodies[2].op !== "cancel")
  fail("browser PDF preview POST contract is incorrect");

globalThis.fetch = async () => ({ ok: false, status: 413, json: async () => ({ error: "too large" }) });
let httpError = false;
try { await api.startPdfPreview({}); } catch (error) { httpError = error.message === "too large"; }
if (!httpError) fail("browser PDF preview errors are not surfaced");
console.log("ok: PDF preview native/browser transport selection, payloads, abort signals, and failure isolation pass");
''',
        text=True,
        capture_output=True,
    )
    if node.returncode:
        return fail(f"Node PDF preview transport contract failed: {(node.stderr or node.stdout).strip()}")
    print(node.stdout.strip())

    lifecycle_node = subprocess.run(
        ["node", "--input-type=module", "-", str(UI / "pdf-front-preview.js")],
        input=r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value).toString("base64")}`;
class FakeNode {
  constructor(tag = "div", attrs = {}) {
    this.tag = tag; this.attrs = attrs; this.children = []; this.nodeType = 1;
    this.isConnected = true; this._html = ""; this.src = "";
  }
  append(...children) { this.children.push(...children.flat(Infinity).filter(Boolean)); }
  set innerHTML(value) { this._html = value; if (value === "") this.children = []; }
  get innerHTML() { return this._html; }
  querySelector(selector) {
    if (selector === "img" && this.tag === "img") return this;
    for (const child of this.children) {
      if (child?.querySelector) { const found = child.querySelector(selector); if (found) return found; }
    }
    return null;
  }
  removeAttribute(name) { if (name === "src") this.src = ""; delete this.attrs[name]; }
}
const coreUrl = dataUrl(`
  export function el(tag, attrs = {}, ...kids) {
    const node = new globalThis.FakeNode(tag, attrs);
    Object.assign(node, attrs);
    node.append(...kids);
    return node;
  }
  export function ico(name) { return new globalThis.FakeNode("icon", {name}); }
`);
const formsUrl = dataUrl('export const COMMAND_PREVIEW_EVENT = "wb:command-preview"; export function updatePreview() { globalThis.formRefreshes++; }');
const transportUrl = dataUrl(`
  export function startPdfPreview(args) {
    return new Promise(resolve => globalThis.previewStarts.push({args, resolve}));
  }
  export function pollPdfPreview(id) {
    globalThis.previewPolls.push(id);
    return Promise.resolve({ok:true,status:"done",result:{ok:true,mime:"image/jpeg",data:"/9j/",width:10,height:12,sampled:1,available:1}});
  }
  export function cancelPdfPreview(id) { globalThis.previewCancels.push(id); return Promise.resolve({ok:true}); }
`);
globalThis.FakeNode = FakeNode;
globalThis.previewStarts = [];
globalThis.previewPolls = [];
globalThis.previewCancels = [];
globalThis.formRefreshes = 0;
const listeners = new Map();
globalThis.document = {
  visibilityState: "visible",
  addEventListener(name, listener) { listeners.set(name, listener); },
  removeEventListener(name, listener) { if (listeners.get(name) === listener) listeners.delete(name); },
};
let rewritten = source
  .replace('from "./core.js"', `from "${coreUrl}"`)
  .replace('from "./forms.js"', `from "${formsUrl}"`)
  .replace('from "./pdf-preview-transport.js"', `from "${transportUrl}"`);
const api = await import(dataUrl(rewritten));
const fail = message => { throw new Error(message); };
const stage = new FakeNode("stage");
const panel = { isConnected: true, querySelector(selector) {
  return selector === "[data-pdf-preview-stage]" ? stage : null;
} };
const dispose = api.mountPdfFrontPreview(panel);
listeners.get("visibilitychange")?.();
await new Promise(resolve => setTimeout(resolve, 130));
if (formRefreshes !== 1) fail("visibility return did not refresh authoritative form validation");
const emitResult = (value, result) => listeners.get("wb:command-preview")?.({detail:{
  kind:"create_pdf", args:value, result
}});
const emit = value => emitResult(value, {
  cmd:"python create_pdf.py",errors:[],warnings:[],no_front_images:false
});
emit({ppi:100});
emit({ppi:200});
await new Promise(resolve => setTimeout(resolve, 800));
if (previewStarts.length !== 1 || previewStarts[0].args.ppi !== 200)
  fail("rapid validated changes were not debounced to the newest arguments");
previewStarts[0].resolve({ok:true,operation:{id:"d".repeat(32),status:"running"}});
await new Promise(resolve => setTimeout(resolve, 230));
previewStarts.length = 0; previewPolls.length = 0; previewCancels.length = 0;
emit({ppi:300});
if (!String(stage.children[0]?.class || "").includes("pdf-preview-refreshing"))
  fail("the previous successful image was not retained while refreshing");
await new Promise(resolve => setTimeout(resolve, 800));
emit({ppi:600});
await new Promise(resolve => setTimeout(resolve, 800));
if (previewStarts.length !== 2) fail("settled validated form events did not start both generations");
previewStarts[0].resolve({ok:true,operation:{id:"a".repeat(32),status:"running"}});
await Promise.resolve(); await Promise.resolve();
if (!previewCancels.includes("a".repeat(32))) fail("stale start response was not cancelled");
previewStarts[1].resolve({ok:true,operation:{id:"b".repeat(32),status:"running"}});
await new Promise(resolve => setTimeout(resolve, 230));
if (previewPolls.join() !== "b".repeat(32) || stage.children[0]?.tag !== "figure")
  fail("current operation was not polled and painted");
const startsBeforeBlock = previewStarts.length;
emitResult({ppi:700}, {cmd:"python create_pdf.py",errors:[],warnings:["No fronts"],no_front_images:true});
await new Promise(resolve => setTimeout(resolve, 800));
if (previewStarts.length !== startsBeforeBlock)
  fail("blocked validation started a representative render");
emit({ppi:900});
await new Promise(resolve => setTimeout(resolve, 800));
previewStarts[2].resolve({ok:true,operation:{id:"c".repeat(32),status:"running"}});
await Promise.resolve(); await Promise.resolve();
dispose();
if (!previewCancels.includes("c".repeat(32)) || listeners.has("wb:command-preview"))
  fail("page disposal did not cancel and remove the validated-preview listener");
console.log("ok: PDF preview controller cancels stale starts, paints only the current result, and disposes active work");
''',
        text=True,
        capture_output=True,
    )
    if lifecycle_node.returncode:
        return fail(f"Node PDF preview lifecycle contract failed: {(lifecycle_node.stderr or lifecycle_node.stdout).strip()}")
    print(lifecycle_node.stdout.strip())
    print("ok: validated trigger, lifecycle bounds, simple readiness summary, page mounting, and desktop styling are wired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
