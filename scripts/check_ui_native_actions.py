#!/usr/bin/env python3
"""Static and Node contract for the native OS-action facade."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "native-actions.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    facade = FACADE.read_text(encoding="utf-8")
    required = (
        'export function openFile(path)',
        'export function revealPath(path)',
        'export function openExternalUrl(url)',
        'export function saveArtifact(grantId, suggestedName)',
        'invoke("wb_rpc", { method, params })',
        '"file.open"',
        '"file.reveal"',
        '"url.open"',
        '{ path }',
        '{ url }',
        '`/api/file?path=${encodeURIComponent(path)}&open=1`',
        '"/api/reveal"',
        '`/api/file?url=${encodeURIComponent(url)}`',
    )
    for marker in required:
        if marker not in facade:
            return fail(f"native action facade is missing {marker}")

    # These routes are actions, not metadata reads. They must have one owner so
    # packaged windows cannot accidentally bypass the native boundary.
    for path in sorted(UI.rglob("*.js")):
        if path == FACADE:
            continue
        source = path.read_text(encoding="utf-8")
        if "/api/reveal" in source or "/api/file?path=" in source or "/api/file?url=" in source:
            return fail(f"{path.relative_to(ROOT)} directly bypasses native actions")
        if "&open=1" in source or "?url=" in source:
            return fail(f"{path.relative_to(ROOT)} contains an action URL bypass")

    # Metadata and destructive filesystem compatibility routes remain, but
    # artifact export must have one native owner and no UI HTTP fallback.
    artifacts = (UI / "artifacts.js").read_text(encoding="utf-8")
    pdf = (UI / "pages" / "pdf.js").read_text(encoding="utf-8")
    console = (UI / "console.js").read_text(encoding="utf-8")
    if "/api/file?${query}" not in artifacts or 'method: "file.list"' not in artifacts:
        return fail("raw file-list metadata HTTP route was removed")
    if any("/api/files/save" in p.read_text(encoding="utf-8") for p in UI.rglob("*.js")):
        return fail("UI retains a direct artifact save HTTP route")
    fs_facade = (UI / "fs-transport.js").read_text(encoding="utf-8")
    if 'from "../fs-transport.js"' not in pdf or '"fs.delete_images"' not in fs_facade:
        return fail("filesystem deletion did not use its transport facade")
    if 'fetch("/api/fs"' not in fs_facade:
        return fail("standalone browser filesystem compatibility route was removed")
    for path, marker in (
        (UI / "core.js", 'openExternalUrl(url)'),
        (UI / "native-actions.js", 'saveArtifact(grantId, suggestedName)'),
        (UI / "console.js", 'from "./native-actions.js";'),
        (UI / "pages" / "pdf.js", 'import { openFile } from "../native-actions.js";'),
        (UI / "pages" / "templates.js", 'import { openFile } from "../native-actions.js";'),
        (UI / "pages" / "offset.js", 'import { openFile } from "../native-actions.js";'),
        (UI / "pages" / "settings.js", 'import { revealPath } from "../native-actions.js";'),
    ):
        if marker not in path.read_text(encoding="utf-8"):
            return fail(f"{path.relative_to(ROOT)} did not migrate its action call site")
    if "split(/[\\\\/]/)" not in console:
        return fail("console basename does not handle both slash separators")
    all_ui = "\n".join(p.read_text(encoding="utf-8") for p in UI.rglob("*.js"))
    if "nativePick" in all_ui or "plugin:dialog|save" in all_ui or "pick_save" in all_ui:
        return fail("legacy direct picker/save bridge remains")

    node = subprocess.run(
        ["node", "--input-type=module", "-", str(FACADE)],
        input=r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const dataUrl = source => `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;
const transportUrl = dataUrl(`
  export function getTauriInvoke() {
    const internals = globalThis.window && globalThis.window.__TAURI_INTERNALS__;
    return internals && typeof internals.invoke === "function" ? internals.invoke.bind(internals) : null;
  }
`);
const actions = await import(dataUrl(source.replace('from "./transport.js"', `from "${transportUrl}"`)));
const fail = message => { throw new Error(message); };
const path = String.raw`C:\\Cards\\my card.pdf`;
const url = "https://example.test/a path?q=one&two=2";
const nativeCalls = [];
const internals = { invoke(method, rpc) {
  nativeCalls.push({ method, rpc, receiver: this });
  return Promise.resolve({ ok: true });
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
await actions.openFile(path);
await actions.revealPath(path);
await actions.openExternalUrl(url);
const expected = [
  { method: "wb_rpc", rpc: { method: "file.open", params: { path } } },
  { method: "wb_rpc", rpc: { method: "file.reveal", params: { path } } },
  { method: "wb_rpc", rpc: { method: "url.open", params: { url } } },
];
if (JSON.stringify(nativeCalls.map(({ method, rpc }) => ({ method, rpc }))) !== JSON.stringify(expected) ||
    nativeCalls.some(call => call.receiver !== internals)) fail("native method, params, or receiver binding is incorrect");

let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return Promise.reject(new Error("HTTP fallback was used")); };
internals.invoke = () => Promise.reject(new Error("native down"));
let rejected = false;
try { await actions.openFile(path); } catch (error) { rejected = error.message === "native down"; }
if (!rejected || fetchCalls) fail("native rejection silently fell back to HTTP");

delete globalThis.window;
const requests = [];
globalThis.fetch = async (request, options) => {
  requests.push({ request, options });
  return { ok: true, json: async () => ({ ok: true }) };
};
await actions.openFile(path);
await actions.revealPath(path);
await actions.openExternalUrl(url);
if (requests[0].request !== `/api/file?path=${encodeURIComponent(path)}&open=1` ||
    requests[0].options !== undefined ||
    requests[1].request !== "/api/reveal" ||
    JSON.stringify(requests[1].options) !== JSON.stringify({
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path })
    }) ||
    requests[2].request !== `/api/file?url=${encodeURIComponent(url)}` ||
    requests[2].options !== undefined) fail("browser action requests are not exact");

const basename = value => value.split(/[\\/]/).pop();
if (basename(String.raw`C:\\Users\\me\\output.pdf`) !== "output.pdf" || basename("/tmp/output.pdf") !== "output.pdf")
  fail("Windows or POSIX basename behavior is incorrect");
console.log("ok: native/browser OS-action payloads, receiver binding, no fallback, exact URLs, and path separators pass");
''',
        text=True,
        capture_output=True,
    )
    if node.returncode:
        detail = (node.stderr or node.stdout).strip()
        return fail(f"Node native-action contract failed: {detail}")
    print(node.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
