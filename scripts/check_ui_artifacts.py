#!/usr/bin/env python3
"""Static and Node contract for the read-only artifact transport facade."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    artifacts_path = UI / "artifacts.js"
    template_transport_path = UI / "template-transport.js"
    pdf_path = UI / "pages" / "pdf.js"
    templates_path = UI / "pages" / "templates.js"
    theme_path = ROOT / "ui" / "theme.css"
    artifacts = artifacts_path.read_text(encoding="utf-8")
    template_transport = template_transport_path.read_text(encoding="utf-8")
    pdf = pdf_path.read_text(encoding="utf-8")
    templates = templates_path.read_text(encoding="utf-8")
    theme = theme_path.read_text(encoding="utf-8")

    if 'import { getTauriInvoke } from "./transport.js";' not in artifacts:
        return fail("artifact facade does not use the shared callable Tauri capability")
    for required in (
        'export function resolveTemplate(paper, card, borderless = false)',
        'method: "template.resolve"',
        'params = { paper, card, borderless: !!borderless }',
        'export async function listFiles(path, imagesOnly = false)',
        'method: "file.list"',
        'params = { path, images_only: !!imagesOnly }',
        'fetch(path)',
        'images_only=1',
        'exists: false',
        'truncated: false',
        'scanned: 0',
        'found: 0',
        'if (result.exists === false)',
    ):
        if required not in artifacts:
            return fail(f"artifact facade is missing {required}")
    if 'import { listFiles, resolveTemplate } from "../artifacts.js";' not in pdf:
        return fail("pdf page does not import the artifact facade")
    if "/api/template" in pdf or "images_only=1" in pdf:
        return fail("pdf page still directly bypasses the artifact facade")
    if "resolveTemplate(f.paper_size, f.card_size, !!f.borderless)" not in pdf:
        return fail("pdf template lookup does not use the artifact facade")
    if "listFiles(dir, true)" not in pdf:
        return fail("pdf image listing does not use the artifact facade")
    if "listing.truncated" not in pdf or "Never offer a destructive action" not in pdf:
        return fail("pdf does not guard destructive deletion on truncated listings")
    if 'import { deleteImages } from "../fs-transport.js";' not in pdf:
        return fail("pdf confirmation/deletion behavior disappeared")
    for required in ('export async function deleteTemplate(path)',
                     'method: "template.delete"', 'fetch("/api/templates/delete"'):
        if required not in template_transport:
            return fail(f"template deletion facade is missing {required}")
    if 'import { deleteTemplate } from "../template-transport.js";' not in templates:
        return fail("templates page does not import the deletion facade")
    for required in ('class: "fileitem-delete"', 'await confirmModal({',
                     'await deleteTemplate(path)', 'This cannot be undone.'):
        if required not in templates:
            return fail(f"templates page is missing {required}")
    for required in ('.fileitem-wrap { position: relative;',
                     '.fileitem-delete {', 'position: absolute; top: 5px; right: 5px;'):
        if required not in theme:
            return fail(f"template delete control CSS is missing {required}")

    # The facades own browser compatibility routes; packaged callers take the
    # native branches above.
    for path in sorted(UI.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        if path != artifacts_path and ("/api/template?" in source or "images_only=1" in source):
            return fail(f"{path.relative_to(ROOT)} contains a direct artifact metadata bypass")
        if path != template_transport_path and "/api/templates/delete" in source:
            return fail(f"{path.relative_to(ROOT)} contains a direct template deletion bypass")

    imports = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_ui_imports.py")],
        text=True,
        capture_output=True,
    )
    if imports.returncode:
        return fail("import resolution check failed: " + (imports.stdout + imports.stderr).strip())

    node = subprocess.run(
        ["node", "--input-type=module", "-", str(artifacts_path), str(template_transport_path)],
        input=r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const templateMutationSource = fs.readFileSync(process.argv[3], "utf8");
const dataUrl = source => `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;
const transportUrl = dataUrl(`
  export function getTauriInvoke() {
    const internals = globalThis.window && globalThis.window.__TAURI_INTERNALS__;
    return internals && typeof internals.invoke === "function" ? internals.invoke.bind(internals) : null;
  }
`);
const artifacts = await import(dataUrl(source.replace('from "./transport.js"', `from "${transportUrl}"`)));
const fail = message => { throw new Error(message); };

const nativeCalls = [];
globalThis.window = { __TAURI_INTERNALS__: { invoke(method, rpc) {
  nativeCalls.push({ method, rpc, receiver: this });
  if (rpc.method === "template.resolve") return Promise.resolve({ ok: true, name: "x.studio3", path: "/x", repo: "repo" });
  if (rpc.method === "file.list") return Promise.resolve({ exists: true, found: 1, items: [{ name: "a.png" }], truncated: false, scanned: 1 });
  return Promise.reject(new Error("unexpected method"));
} } };
const template = await artifacts.resolveTemplate("A paper", "A/card", true);
const listing = await artifacts.listFiles("double sided", true);
if (JSON.stringify(template) !== JSON.stringify({ ok: true, name: "x.studio3", path: "/x", repo: "repo" }) ||
    listing.exists !== true || listing.found !== 1 || JSON.stringify(listing.items) !== JSON.stringify([{ name: "a.png" }]) ||
    JSON.stringify(nativeCalls.map(x => x.rpc)) !== JSON.stringify([
      { method: "template.resolve", params: { paper: "A paper", card: "A/card", borderless: true } },
      { method: "file.list", params: { path: "double sided", images_only: true } },
    ]) || nativeCalls.some(x => x.method !== "wb_rpc" || x.receiver !== globalThis.window.__TAURI_INTERNALS__))
  fail("native artifact payload or result is incorrect");

let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return Promise.reject(new Error("HTTP fallback was used")); };
globalThis.window.__TAURI_INTERNALS__.invoke = () => Promise.reject(new Error("native down"));
let nativeRejected = false;
try { await artifacts.listFiles("folder", true); } catch (error) { nativeRejected = error.message === "native down"; }
if (!nativeRejected || fetchCalls) fail("native failure silently fell back to HTTP");

globalThis.window.__TAURI_INTERNALS__.invoke = () => Promise.resolve({ exists: false, found: 0, items: [{ name: "must not survive" }], truncated: true, scanned: 99 });
const nativeMissing = await artifacts.listFiles("missing", true);
const emptyListing = { exists: false, items: [], truncated: false, scanned: 0, found: 0 };
if (JSON.stringify(nativeMissing) !== JSON.stringify(emptyListing)) fail("native missing listing was not normalized safely");

delete globalThis.window;
const requests = [];
globalThis.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, status: 200, json: async () => url.startsWith("/api/template")
    ? { ok: false, errors: ["not found"] }
    : { exists: true, dir: "/d", found: 1, items: [{ name: "a.png" }], truncated: false, scanned: 1 } };
};
const browserTemplate = await artifacts.resolveTemplate("letter size", "card/name", false);
const browserListing = await artifacts.listFiles("double sided", true);
if (JSON.stringify(browserTemplate) !== JSON.stringify({ ok: false, errors: ["not found"] }) ||
    browserListing.exists !== true || browserListing.found !== 1 ||
    JSON.stringify(browserListing.items) !== JSON.stringify([{ name: "a.png" }]) ||
    requests[0].url !== "/api/template?paper=letter%20size&card=card%2Fname&borderless=0" ||
    requests[1].url !== "/api/file?path=double%20sided&images_only=1" ||
    requests.some(x => x.options !== undefined))
  fail("browser artifact URLs, fallback, or template failure result changed");

globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ dir: "/legacy", items: [] }) });
const legacyBrowserListing = await artifacts.listFiles("legacy", true);
if (legacyBrowserListing.exists !== true || !Array.isArray(legacyBrowserListing.items))
  fail("legacy browser listing was not upgraded to an explicit exists flag");

globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({ error: "not found" }) });
const browserMissing = await artifacts.listFiles("gone", true);
if (JSON.stringify(browserMissing) !== JSON.stringify(emptyListing)) fail("missing browser directory was not normalized");

globalThis.fetch = async () => ({ ok: false, status: 500, json: async () => ({ error: "server broke" }) });
let httpRejected = false;
try { await artifacts.listFiles("broken", true); } catch (error) { httpRejected = error.message === "server broke"; }
if (!httpRejected) fail("non-missing browser failure was swallowed");

globalThis.window = { __TAURI_INTERNALS__: { invoke() { return Promise.resolve({ exists: true, items: [] }); } } };
let nativeMalformed = false;
try { await artifacts.listFiles("malformed", true); } catch { nativeMalformed = true; }
if (!nativeMalformed) fail("malformed native listing was accepted");

const templateMutations = await import(dataUrl(templateMutationSource.replace('from "./transport.js"', `from "${transportUrl}"`)));
const deleteCalls = [];
globalThis.window = { __TAURI_INTERNALS__: { invoke(method, rpc) {
  deleteCalls.push({ method, rpc });
  return Promise.resolve({ ok: true, errors: [], name: "custom.dxf" });
} } };
globalThis.fetch = () => { throw new Error("native template deletion used HTTP"); };
const nativeDelete = await templateMutations.deleteTemplate("/repo/custom.dxf");
if (nativeDelete.name !== "custom.dxf" || JSON.stringify(deleteCalls) !== JSON.stringify([{
  method: "wb_rpc", rpc: { method: "template.delete", params: { path: "/repo/custom.dxf" } }
}])) fail("native template deletion payload changed");
globalThis.window.__TAURI_INTERNALS__.invoke = () => Promise.reject(new Error("native delete failed"));
let deleteRejected = false;
try { await templateMutations.deleteTemplate("/repo/custom.dxf"); } catch (error) { deleteRejected = error.message === "native delete failed"; }
if (!deleteRejected) fail("native template deletion failure was hidden");

delete globalThis.window;
let browserDelete = null;
globalThis.fetch = async (url, options) => {
  browserDelete = { url, options };
  return { ok: true, status: 200, json: async () => ({ ok: true, errors: [], name: "custom.dxf" }) };
};
await templateMutations.deleteTemplate("/repo/custom.dxf");
if (browserDelete.url !== "/api/templates/delete" || browserDelete.options.method !== "POST" ||
    browserDelete.options.headers["Content-Type"] !== "application/json" ||
    browserDelete.options.body !== JSON.stringify({ path: "/repo/custom.dxf" }))
  fail("browser template deletion payload changed");

console.log("ok: native/browser artifact metadata and template deletion contracts pass");
''',
        text=True,
        capture_output=True,
    )
    if node.returncode:
        detail = (node.stderr or node.stdout).strip()
        return fail(f"Node artifact contract failed: {detail}")
    print(node.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
