#!/usr/bin/env python3
"""Frontend contract for the Advanced image post-processing workflow.

The worker-side registry is intentionally not required by this check: these
assertions exercise the browser/native contract against mocked responses and
keep the UI safe while the backend implementation lands.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
JS = UI / "js"

def fail(message):
    print(f"FAIL: {message}")
    return 1

def main():
    files = {
        "index": UI / "index.html",
        "nav": JS / "nav.js",
        "app": JS / "app.js",
        "fetch": JS / "pages" / "fetch.js",
        "page": JS / "pages" / "postprocess.js",
        "transport": JS / "postprocess-transport.js",
        "css": UI / "theme.css",
    }
    for name, path in files.items():
        if not path.is_file():
            return fail(f"{path.relative_to(ROOT)} is missing")
    source = {name: path.read_text(encoding="utf-8") for name, path in files.items()}
    if 'data-page="postprocess"' not in source["index"] or 'data-simple-hide' not in source["index"]:
        return fail("post-processing nav item is not Advanced-only")
    if 'postprocess: "Image post-processing"' not in source["nav"]:
        return fail("post-processing route title is missing")
    if 'postprocess' in re.search(r'export const SIMPLE_PAGES\s*=\s*\[(.*?)\]', source["nav"], re.S).group(1):
        return fail("post-processing was added to SIMPLE_PAGES")
    if 'import "./pages/postprocess.js";' not in source["app"]:
        return fail("app.js does not import the post-processing page")
    if "PAGES.postprocess" not in source["page"] or 'uiMode() === "simple"' not in source["page"]:
        return fail("page is not registered or Simple mode is not guarded")
    if 'go("postprocess"' not in source["fetch"] or "Post-process images" not in source["fetch"]:
        return fail("fetch completion has no post-processing navigation action")
    if re.search(r'go\("postprocess"[^\n]*\)\s*;\s*[^\n]*doRun', source["fetch"]):
        return fail("fetch completion appears to auto-run processing")
    for marker in ("postprocessors.list", "postprocessors.get", "postprocessors.save", "postprocessors.duplicate", "postprocessors.trust", "postprocessors.delete", "postprocessors.status", "wb_postprocessor_import"):
        if marker not in source["transport"]:
            return fail(f"transport is missing {marker}")
    if 'invoke("wb_rpc"' not in source["transport"] or "fetch(" not in source["transport"]:
        return fail("transport does not expose native and browser operations")
    if "native.native ? native.value" not in source["transport"]:
        return fail("native operation does not remain authoritative after selection")
    if 'textarea' not in source["page"] or '.value' not in source["page"]:
        return fail("editor is not textarea/value based")
    if re.search(r'innerHTML\s*=\s*[^;]*(?:source|requirements)', source["page"]):
        return fail("source is assigned through HTML")
    for marker in ("Unsaved changes", "Discard unsaved changes", "Revert unsaved changes", "Save revision", "Trust this revision", "Install / rebuild libraries", "Original images were not changed", "Go to Create PDF"):
        if marker not in source["page"]:
            return fail(f"page is missing {marker}")
    for marker in (".pp-editor", ".pp-source", ".pp-run-status", "@media (max-width: 760px)"):
        if marker not in source["css"]:
            return fail(f"responsive post-processing CSS is missing {marker}")
    node = shutil.which("node")
    if not node:
        return fail("Node.js is required")
    # Mock native and browser calls. A rejected native call must be observable;
    # this deliberately installs a fetch that would fail the test if called.
    script = r'''
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
const data = s => `data:text/javascript;base64,${Buffer.from(s, "utf8").toString("base64")}`;
const transport = await import(data(src.replace('from "./transport.js"', `from "${data(`export const getTauriInvoke = () => globalThis.__invoke || null;`)}"`)));
const fail = m => { throw new Error(m); };
let calls = [];
globalThis.__invoke = (method, args) => { calls.push({method, args}); return Promise.resolve({ok:true, processors:[]}); };
const listed = await transport.list();
if (!listed.ok || calls[0].method !== "wb_rpc" || calls[0].args.method !== "postprocessors.list") fail("native list contract failed");
let httpCalls = 0;
globalThis.fetch = () => { httpCalls++; return Promise.reject(new Error("HTTP fallback")); };
globalThis.__invoke = () => Promise.reject(new Error("native failed"));
let rejected = false;
try { await transport.get("p1"); } catch (e) { rejected = e.message === "native failed"; }
if (!rejected || httpCalls) fail("native failure used HTTP fallback");
console.log("ok: Advanced post-processing wiring, editor, gating, terminal messaging, and mocked transport contract pass");
'''
    result = subprocess.run([node, "--input-type=module", "-", str(files["transport"])], input=script, text=True, capture_output=True)
    if result.returncode:
        return fail((result.stderr or result.stdout).strip())
    print(result.stdout.strip())
    return 0

if __name__ == "__main__":
    sys.exit(main())
