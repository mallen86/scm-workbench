#!/usr/bin/env python3
"""Static and Node contract for the offset mutation transport facade."""
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
        "setOffset(",
        "deleteOffset(",
    ):
        if marker not in page:
            return fail(f"offset page is missing {marker}")
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
    offset_link = '<a class="nav-item" data-page="offset"><span class="nav-ico" data-ico="target"></span>Offset &amp; calibration</a>'
    if offset_link not in index:
        return fail("offset navigation item is still hidden in simple mode")
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
console.log("ok: native/browser offset.set and offset.delete payloads, truncation, and no-fallback contract pass");
'''.strip()
    result = subprocess.run(
        [node, "--input-type=module", "-", str(FACADE), str(UI / "transport.js")],
        input=node_script, text=True, capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return fail(f"Node offset transport contract failed: {detail}")
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
