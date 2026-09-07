#!/usr/bin/env python3
"""Static and Node contract for native artifact save.

The save dialog is intentionally a Tauri-only operation.  This check keeps the
browser compatibility surface fail-closed and verifies the small JavaScript
facade without requiring a WebView or a Tauri runtime.
"""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
FACADE = UI / "native-actions.js"
TAURI = ROOT / "tauri" / "src" / "main.rs"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    facade = FACADE.read_text(encoding="utf-8")
    tauri = TAURI.read_text(encoding="utf-8")
    required = (
        "export function saveArtifact(grantId, suggestedName)",
        'invoke("wb_save_artifact", { grantId, suggestedName })',
        'Promise.reject(new Error("artifact export requires the app window"))',
        ".dialog()",
        ".file()",
        ".set_parent(&window)",
        '.set_title("Export PDF")',
        '.add_filter("PDF", &["pdf"])',
        ".set_file_name(suggested_name.clone())",
        "spawn_blocking",
        "poll_artifact_export",
        "cancel_artifact_export",
        'return Ok(Value::Null);',
        'return Err("artifact export timed out".to_string());',
    )
    for marker in required:
        if marker not in (facade + "\n" + tauri):
            return fail(f"save implementation is missing {marker}")

    # There is no browser picker and no alternate UI save route.  The native
    # command owns destination selection; HTTP remains only for old callers in
    # standalone server mode, never for the app window.
    all_ui = "\n".join(p.read_text(encoding="utf-8") for p in UI.rglob("*.js"))
    for forbidden in ("/api/files/save", "nativePick", "pick_save", "plugin:dialog|save"):
        if forbidden in all_ui:
            return fail(f"UI retains a save bypass: {forbidden}")
    if 'invoke("wb_save_artifact", { grantId, suggestedName })' not in facade:
        return fail("native save arguments are not explicit")

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
const grantId = "a".repeat(64);
const suggestedName = "cards.pdf";
const calls = [];
const internals = { invoke(method, args) {
  calls.push({ method, args, receiver: this });
  return Promise.resolve({ ok: true, name: "cards.pdf", bytes: 7 });
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
const success = await actions.saveArtifact(grantId, suggestedName);
if (JSON.stringify(success) !== JSON.stringify({ ok: true, name: "cards.pdf", bytes: 7 }) ||
    calls.length !== 1 || calls[0].method !== "wb_save_artifact" ||
    JSON.stringify(calls[0].args) !== JSON.stringify({ grantId, suggestedName }) ||
    calls[0].receiver !== internals) fail("native save command or receiver binding is incorrect");

// Application rejection is a result, not an infrastructure exception.
internals.invoke = () => Promise.resolve({ ok: false, errors: ["destination rejected"] });
const appFailure = await actions.saveArtifact(grantId, suggestedName);
if (appFailure.ok !== false || appFailure.errors[0] !== "destination rejected") fail("application rejection was changed");

// Once native capability exists, its failure must not silently retry HTTP.
let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return Promise.reject(new Error("HTTP fallback")); };
internals.invoke = () => Promise.reject(new Error("worker unavailable"));
let infrastructureFailure = false;
try { await actions.saveArtifact(grantId, suggestedName); } catch (error) {
  infrastructureFailure = error.message === "worker unavailable";
}
if (!infrastructureFailure || fetchCalls !== 0) fail("native infrastructure failure used an HTTP fallback");

// Browser compatibility is deliberately fail-closed: it has no native picker
// and must not invent a destination or issue a direct save request.
delete globalThis.window;
let browserRejected = false;
try { await actions.saveArtifact(grantId, suggestedName); } catch (error) {
  browserRejected = error.message === "artifact export requires the app window";
}
if (!browserRejected || fetchCalls !== 0) fail("browser save did not reject without a picker");
console.log("ok: native save args, app/infrastructure failures, no fallback, and browser fail-closed behavior pass");
''',
        text=True,
        capture_output=True,
    )
    if node.returncode:
        detail = (node.stderr or node.stdout).strip()
        return fail(f"Node native-save contract failed: {detail}")
    print(node.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
