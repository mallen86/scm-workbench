#!/usr/bin/env python3
"""Static contract for the dedicated packaged decklist import command."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"


def fail(message):
    print(f"FAIL: {message}")
    return 1


def main():
    transport_path = UI / "decklist-transport.js"
    transport = transport_path.read_text(encoding="utf-8")
    fetch = (UI / "pages" / "fetch.js").read_text(encoding="utf-8")
    core = (UI / "core.js").read_text(encoding="utf-8")
    other_ui = "\n".join(
        path.read_text(encoding="utf-8")
        for path in UI.rglob("*.js")
        if path != transport_path
    )
    if 'invoke("wb_decklist_import", {})' not in transport:
        return fail("packaged decklist import is not a no-argument native command")
    if "native picker required" not in transport:
        return fail("browser import does not fail closed without an explicit path")
    if "importDecklist()" not in fetch or "canImportDecklist()" not in fetch:
        return fail("fetch page does not use the decklist transport facade")
    if re.search(r"nativePick\.(?:pickFile|canPick)", fetch + core):
        return fail("legacy native file picker remains in the UI")
    if "plugin:dialog|open" in transport + other_ui:
        return fail("UI directly invokes the dialog open plugin")
    if "/api/decklists/import" in other_ui:
        return fail("packaged UI can bypass the decklist transport facade")

    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the decklist transport contract")
    node_script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const transportSource = fs.readFileSync(process.argv[3], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const transportUrl = dataUrl(transportSource);
const facade = await import(dataUrl(source.replace('from "./transport.js"', `from "${transportUrl}"`)));
const fail = message => { throw new Error(message); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
let fetchCalls = [];
const calls = [];
let nativeMode = "success";
const internals = { invoke(method, params) {
  calls.push({ method, params, receiver: this });
  if (nativeMode === "reject") return Promise.reject(new Error("native down"));
  if (nativeMode === "cancel") return Promise.resolve(null);
  return Promise.resolve({ ok: true, name: "picked.txt", decklists: [] });
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
globalThis.fetch = (...args) => { fetchCalls.push(args); return Promise.reject(new Error("HTTP fallback was used")); };
if (!facade.canImportDecklist() || !same(await facade.importDecklist("ignored"),
    { ok: true, name: "picked.txt", decklists: [] }) || calls.length !== 1 ||
    calls[0].method !== "wb_decklist_import" || !same(calls[0].params, {}) ||
    calls[0].receiver !== internals || fetchCalls.length) fail("native decklist command or binding is wrong");
nativeMode = "cancel";
if (await facade.importDecklist() !== null) fail("native picker cancellation was not preserved");
nativeMode = "reject";
let rejected = false;
try { await facade.importDecklist(); } catch (error) { rejected = error.message === "native down"; }
if (!rejected || fetchCalls.length) fail("native rejection silently fell back to HTTP");
delete globalThis.window;
if (facade.canImportDecklist()) fail("browser was reported as native-capable");
let browserError = false;
try { await facade.importDecklist(); } catch (error) { browserError = error.message === "native picker required"; }
if (!browserError || fetchCalls.length) fail("browser no-argument import exposed a picker or HTTP call");
let browserRequest;
globalThis.fetch = async (...args) => {
  browserRequest = args;
  return { ok: true, json: async () => ({ ok: true }) };
};
await facade.importDecklist("/tmp/selected.txt");
if (!browserRequest || browserRequest[0] !== "/api/decklists/import" ||
    browserRequest[1].method !== "POST" ||
    browserRequest[1].body !== JSON.stringify({ path: "/tmp/selected.txt" }))
  fail("explicit browser compatibility request changed");
console.log("ok: native decklist command payload, cancellation, rejection isolation, and browser picker boundary pass");
'''.strip()
    result = subprocess.run(
        [node, "--input-type=module", "-", str(UI / "decklist-transport.js"),
         str(UI / "transport.js")],
        input=node_script, text=True, capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return fail(f"Node decklist transport contract failed: {detail}")
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
