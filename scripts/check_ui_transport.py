#!/usr/bin/env python3
"""Static guard for the first frontend native-transport slice.

The route selector is deliberately pure in ui/js/transport.js, so this check
can verify its complete allowlist and GET-only behavior without a DOM/Tauri
runtime.  The surrounding source checks ensure api() still has its HTTP
fallback and no bootstrap module bypasses it with a literal fetch().
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
EXPECTED = {
    "/api/info": "info",
    "/api/manifest": "manifest",
    "/api/settings": "settings.get",
}


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    transport = (UI / "transport.js").read_text(encoding="utf-8")
    core = (UI / "core.js").read_text(encoding="utf-8")

    # Match the complete literal inventory, not just the three expected
    # entries: adding a route requires an intentional update to this guard.
    entries = dict(re.findall(r'"(/api/[^"]+)"\s*:\s*"([^"]+)"', transport))
    if entries != EXPECTED:
        return fail(f"native bootstrap route inventory is {entries!r}, expected {EXPECTED!r}")
    if "if (body) return null;" not in transport:
        return fail("native bootstrap selector is not GET-only")
    if "return NATIVE_BOOTSTRAP_ROUTES[path] || null;" not in transport:
        return fail("native bootstrap selector does not return only its allowlist")

    # A marker object is not a Tauri capability; invoke must be callable.
    if 'typeof internals.invoke !== "function"' not in transport:
        return fail("Tauri capability check does not require a callable invoke")
    if "invoke(\"wb_rpc\", { method, params: {} })" not in transport:
        return fail("native bootstrap does not invoke wb_rpc with an empty params object")

    api_start = core.find("export async function api(path, body)")
    api_end = core.find("\n\n/* --------------------------------- toasts", api_start)
    if api_start < 0 or api_end < 0:
        return fail("could not locate api() transport boundary")
    api = core[api_start:api_end]
    for required in (
        "selectNativeBootstrapRoute(path, body)",
        "getTauriInvoke()",
        "invokeNativeBootstrap(nativeMethod, invoke)",
        "const r = await fetch(path, opts);",
    ):
        if required not in api:
            return fail(f"api() is missing {required}")

    # Bootstrap reads must go through api(); literal fetch calls for these
    # routes would bypass the native selection in a packaged window.
    direct = []
    for path in sorted(UI.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        # A literal no-options fetch is a GET bypass. Existing POST writes
        # intentionally remain direct HTTP calls and include a second options
        # argument, so they are not part of this guard.
        if re.search(r'fetch\s*\(\s*["\']/api/(?:info|manifest|settings)["\']\s*\)', source):
            direct.append(str(path.relative_to(ROOT)))
    if direct:
        return fail("bootstrap route has a direct fetch bypass: " + ", ".join(direct))

    if "/api/preview" in entries or "/api/jobs" in entries:
        return fail("non-migrated endpoint entered the native inventory")
    if "fetch(path, opts)" not in api:
        return fail("browser/non-migrated requests do not retain the HTTP fallback")

    # Run the actual pure ES-module functions under Node.  Keeping this as a
    # data-URL import avoids adding a package.json solely to mark this small
    # no-build UI tree as ESM.
    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute ui/js/transport.js")
    node_script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const encoded = Buffer.from(source, "utf8").toString("base64");
const transport = await import(`data:text/javascript;base64,${encoded}`);
const fail = message => { throw new Error(message); };
const selected = (path, body) => transport.selectNativeBootstrapRoute(path, body);
for (const [path, method] of Object.entries(%s)) {
  if (selected(path) !== method) fail(`GET ${path} did not select ${method}`);
  if (selected(path, {}) !== null || selected(path, { value: 1 }) !== null)
    fail(`${path} with a body selected native transport`);
}
if (selected("/api/other") !== null || selected("/api/jobs") !== null)
  fail("a non-migrated GET selected native transport");
const calls = [];
const scope = { __TAURI_INTERNALS__: {
  invoke(method, args) { calls.push({ method, args, receiver: this }); return "ok"; }
}};
const invoke = transport.getTauriInvoke(scope);
if (typeof invoke !== "function" || !transport.hasTauriInvoke(scope)) fail("callable Tauri capability was rejected");
if (transport.getTauriInvoke({ __TAURI_INTERNALS__: {} }) !== null) fail("non-callable invoke was accepted");
if (transport.getTauriInvoke(null) !== null) fail("missing Tauri capability was accepted");
if (transport.invokeNativeBootstrap("info", invoke) !== "ok") fail("native invoke result changed");
if (calls.length !== 1 || calls[0].method !== "wb_rpc" || calls[0].args.method !== "info" ||
    JSON.stringify(calls[0].args.params) !== "{}" || calls[0].receiver !== scope.__TAURI_INTERNALS__)
  fail("native invoke arguments or binding are incorrect");
'''.strip() % json.dumps(EXPECTED)
    result = subprocess.run(
        [node, "--input-type=module", "-", str(UI / "transport.js")],
        input=node_script,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return fail(f"Node transport contract failed: {detail}")

    print("ok: native transport selector and capability functions pass their Node contract; all other requests use HTTP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
