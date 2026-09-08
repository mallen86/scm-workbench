#!/usr/bin/env python3
"""Static contract checks for the packaged Tauri asset origin."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "tauri/tauri.conf.json").read_text(encoding="utf-8"))
MAIN = (ROOT / "tauri/src/main.rs").read_text(encoding="utf-8")
CAPABILITY = json.loads((ROOT / "tauri/capabilities/default.json").read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise SystemExit(f"embedded Tauri check failed: {message}")


if CONFIG["build"]["frontendDist"] != "../ui":
    fail("frontendDist must embed only the bounded UI tree")
if "devUrl" in CONFIG["build"]:
    fail("packaged/source Tauri must not retain a loopback devUrl")
if "remote" in CAPABILITY:
    fail("the packaged capability must not grant a worker HTTP remote origin")
for page in ("loading.html", "index.html"):
    if f'WebviewUrl::App("{page}"' not in MAIN:
        # loading_page is currently the only constructor; the index path is
        # checked separately because it is navigated after worker readiness.
        if page == "index.html":
            continue
        fail(f"{page} is not an embedded WebviewUrl::App")
if "{origin}/index.html" not in MAIN:
    fail("ready navigation does not target the embedded index")
if "window.location.replace" in MAIN or "window.location.href" in MAIN:
    fail("the shell must not navigate the WebView with window.location")
# Packaged workers are stdio-only.  Keeping these checks here prevents a
# future startup edit from reintroducing port reclamation (which could kill an
# unrelated process) or a second transport accidentally.
for forbidden in ("WORKER_PORT", "claim_worker_port", "port_open", "lsof", "netstat", "taskkill", ".arg(\"--port\")"):
    if forbidden in MAIN:
        fail(f"packaged shell must not reclaim or depend on a worker port: {forbidden}")
if ".arg(\"--ipc\")" not in MAIN:
    fail("worker is not started in IPC mode")
if ".on_navigation(is_embedded_url)" not in MAIN:
    fail("the WebView navigation policy is not asset-only")
try:
    js_start = MAIN.index("    let mut js = String::new();")
    js_end = MAIN.index("    let html = format!(", js_start)
    fragments = re.findall(r'js\.push_str\(("(?:[^"\\\\]|\\\\.)*")\);', MAIN[js_start:js_end])
    failure_js = "".join(json.loads(fragment) for fragment in fragments)
except (ValueError, json.JSONDecodeError) as error:
    fail(f"could not reconstruct startup failure handlers: {error}")
syntax = subprocess.run(["node", "--check"], input=failure_js, text=True,
                        capture_output=True)
if not fragments or syntax.returncode:
    fail("startup failure handlers are not valid JavaScript: " + syntax.stderr.strip())

# Every absolute asset reference in the two HTML entry documents must remain
# inside the bounded frontendDist tree.
for html_name in ("loading.html", "index.html"):
    html = (ROOT / "ui" / html_name).read_text(encoding="utf-8")
    for asset in re.findall(r'(?:src|href)="(/[^"?#]+)', html):
        if not (ROOT / "ui" / asset.lstrip("/")).is_file():
            fail(f"{html_name} references missing embedded asset {asset}")

nav = (ROOT / "ui/js/nav.js").read_text(encoding="utf-8")
for route in ("dashboard", "fetch", "pdf", "offset", "templates", "extras", "sizes", "utilities", "settings"):
    if route not in nav:
        fail(f"SPA route {route} is not present in the embedded UI")

print("ok: packaged Tauri embeds only the UI tree and SPA routes; no remote worker capability or WebView HTTP navigation")
