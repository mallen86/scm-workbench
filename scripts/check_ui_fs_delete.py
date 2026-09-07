#!/usr/bin/env python3
"""Check the packaged image-deletion facade and its no-fallback contract."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "fs-transport.js"
PDF = UI / "pages" / "pdf.js"


def main() -> int:
    source = FACADE.read_text(encoding="utf-8")
    pdf = PDF.read_text(encoding="utf-8")
    for marker in ('export function deleteImages(path)', '"fs.delete_images"',
                   '"/api/fs"', '"delete_images"', 'getTauriInvoke()'):
        if marker not in source:
            print(f"FAIL: fs facade is missing {marker}")
            return 1
    if 'from "../fs-transport.js"' not in pdf or 'fetch("/api/fs"' in pdf:
        print("FAIL: PDF page bypasses the fs facade")
        return 1
    node = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const transport = await import("data:text/javascript;base64," + Buffer.from(`
  export function getTauriInvoke() {
    const i = globalThis.window && globalThis.window.__TAURI_INTERNALS__;
    return i && typeof i.invoke === "function" ? i.invoke.bind(i) : null;
  }
`, "utf8").toString("base64"));
const facade = await import("data:text/javascript;base64," + Buffer.from(source.replace('from "./transport.js"', 'from "data:text/javascript;base64,' + Buffer.from(`
  export function getTauriInvoke() {
    const i = globalThis.window && globalThis.window.__TAURI_INTERNALS__;
    return i && typeof i.invoke === "function" ? i.invoke.bind(i) : null;
  }
`, "utf8").toString("base64") + '"'), "utf8").toString("base64"));
const path = "/checkout/game/double_sided";
let calls = 0;
const internals = { invoke(method, args) { calls++; if (method !== "wb_rpc" || args.method !== "fs.delete_images" || args.params.path !== path) throw new Error("bad native call"); return Promise.resolve({ok:true}); } };
globalThis.window = { __TAURI_INTERNALS__: internals };
if (!(await facade.deleteImages(path)).ok || calls !== 1) throw new Error("native facade failed");
let fetches = 0;
internals.invoke = () => Promise.reject(new Error("native down"));
globalThis.fetch = () => { fetches++; throw new Error("HTTP fallback"); };
let rejected = false;
try { await facade.deleteImages(path); } catch (e) { rejected = e.message === "native down"; }
if (!rejected || fetches !== 0) throw new Error("native rejection fell back to HTTP");
delete globalThis.window;
globalThis.fetch = async (url, opts) => { if (url !== "/api/fs" || opts.method !== "POST") throw new Error("bad browser route"); return {ok:true, json: async () => ({ok:true,deleted:0,names:[]})}; };
if (!(await facade.deleteImages(path)).ok) throw new Error("browser route failed");
'''
    # The facade has one relative import; replace it with a data URL module.
    result = subprocess.run(["node", "--input-type=module", "-", str(FACADE)], input=node,
                            text=True, capture_output=True)
    if result.returncode:
        print("FAIL: Node fs transport contract: " + (result.stderr or result.stdout).strip())
        return 1
    print("ok: packaged image deletion is native-only and browser mode uses POST /api/fs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
