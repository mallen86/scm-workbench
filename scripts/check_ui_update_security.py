#!/usr/bin/env python3
"""Static and Node contract for the packaged update card's security boundary."""
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "ui" / "js" / "pages" / "settings.js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if not SETTINGS.is_file():
        return fail("settings page is missing")
    source = SETTINGS.read_text(encoding="utf-8")

    # The release-notes body is the only HTML supplied by the server.  The
    # other innerHTML writes are fixed-value DOM resets, never interpolations.
    assignments = re.findall(
        r"(?m)^\s*([A-Za-z_$][\w$]*)\.innerHTML\s*=\s*([^\n;]+)", source
    )
    allowed = {
        ("m", '""'),
        ("box", '""'),
        ("body", 'r.body || "<p>(no notes on this release)</p>"'),
    }
    if set(assignments) != allowed:
        return fail(f"unexpected innerHTML writes: {assignments!r}")
    if "uStatus.innerHTML" in source:
        return fail("update metadata is interpolated into innerHTML")
    if "body.innerHTML = r.body ||" not in source:
        return fail("the server-rendered release-notes body boundary is missing")

    # release_url is persisted remote metadata.  It must be constrained to
    # GitHub's release URL shape before either the link or OS-browser bridge
    # can use it.
    for marker in (
        'function serverReleaseUrl(value)',
        'u.protocol !== "https:"',
        'u.hostname !== "github.com"',
        'path[3] !== "releases"',
        'path[4] !== "tag"',
        'const releaseUrl = serverReleaseUrl(st.release_url);',
        'const safeReleaseUrl = serverReleaseUrl(releaseUrl);',
    ):
        if marker not in source:
            return fail(f"release URL validation is missing {marker}")

    # setBtn must be initialized before the checking fast path can call it.
    set_button = source.find("const setBtn =")
    checking = source.find("if (st.checking)")
    if set_button < 0 or checking < 0 or set_button > checking:
        return fail("checking state can reach setBtn before its initialization")
    if 'setBtn("Checking…", null, true);' not in source:
        return fail("checking state does not disable the update button")
    for marker in (
        'uStatus.replaceChildren("You\'re on the latest version — ", el("b", {}, latest)',
        'uStatus.replaceChildren(\n            "A newer version is out — ", el("b", {}, latest)',
    ):
        if marker not in source:
            return fail(f"remote release metadata is not rendered as text nodes: {marker}")

    node = shutil.which("node")
    if not node:
        return fail("Node.js is required to execute the update security contract")
    node_script = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const fail = message => { throw new Error(message); };

// Execute the actual URL guard and update-card render function in a tiny DOM
// double.  This exercises the production code without requiring a browser or
// adding a package solely to mark the no-build UI tree as ESM.
const urlStart = source.indexOf("function serverReleaseUrl(value)");
const urlEnd = source.indexOf("\n}\n\n/* In-app", urlStart) + 2;
if (urlStart < 0 || urlEnd < 2) fail("could not locate serverReleaseUrl");
const serverReleaseUrl = new Function(`${source.slice(urlStart, urlEnd)}; return serverReleaseUrl;`)();
if (serverReleaseUrl("javascript:alert(1)") !== null ||
    serverReleaseUrl("https://evil.example/owner/repo/releases/tag/v1") !== null ||
    serverReleaseUrl("https://github.com/owner/repo/releases/tag/v1?next=evil") !== null ||
    !serverReleaseUrl("https://github.com/owner/repo/releases/tag/v1"))
  fail("release URL guard accepted an unsafe URL or rejected a server-shaped URL");

const vvLine = source.match(/const vv = [^\n]+/);
if (!vvLine) fail("could not locate vv");
const vv = new Function(`${vvLine[0]}\nreturn vv;`)();
const renderStart = source.indexOf("    const render = async () => {");
const renderEnd = source.indexOf("\n    };\n\n    async function doCheck()", renderStart) + 7;
if (renderStart < 0 || renderEnd < 7) fail("could not locate update-card render function");

const nodeText = value => {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  return value.textContent || "";
};
const fakeNode = (tag, attrs = {}, ...initial) => {
  const n = { tag, attrs, children: [], _text: "", _html: "" };
  Object.defineProperty(n, "textContent", {
    get() { return n._text + n.children.map(nodeText).join(""); },
    set(value) { n._text = String(value); n.children = []; },
  });
  Object.defineProperty(n, "innerHTML", {
    get() { return n._html; },
    set(value) { n._html = String(value); },
  });
  n.append = (...values) => n.children.push(...values.flat(Infinity).filter(value => value !== null && value !== undefined && value !== false));
  n.replaceChildren = (...values) => { n._text = ""; n.children = values.flat(Infinity).filter(value => value !== null && value !== undefined && value !== false); };
  n.removeAttribute = () => {};
  n.append(...initial);
  return n;
};
const el = (tag, attrs = {}, ...kids) => fakeNode(tag, attrs, ...kids);
const ico = name => fakeNode("span", { class: `ic ${name}` });
const $$ = () => [];
const humanize = ts => ts ? "a persisted time" : "never";
const showWhatsNew = () => {};
const uLast = fakeNode("span");
const uBtn = fakeNode("button");
const uStatus = fakeNode("div");
let state;
const getUpdates = async () => ({ state, current: state.current });
const render = new Function(
  "getUpdates", "checkPending", "uLast", "uBtn", "uStatus", "humanize", "vv", "serverReleaseUrl", "el", "ico", "$$", "doCheck", "startUpdate", "showWhatsNew",
  `${source.slice(renderStart, renderEnd)}; return render;`
)(getUpdates, false, uLast, uBtn, uStatus, humanize, vv, serverReleaseUrl, el, ico, $$, () => {}, () => {}, showWhatsNew);

// A daily/background check must not hit the pre-declaration TDZ path.
state = { checking: true, status: "never", checked_at: null, current: "0.1.0" };
await render();
if (!uBtn.disabled || !uStatus.textContent.includes("Asking GitHub"))
  fail("checking state did not render a disabled in-flight status");

// Hostile release tags must remain text, including in the up-to-date and
// update-available states.  No fake DOM element may appear and no innerHTML
// sink may receive the tag.
const hostile = '<img onerror="alert(1)">';
state = { checking: false, status: "up-to-date", checked_at: Date.now() / 1000, latest: hostile, current: "0.1.0" };
await render();
if (uStatus._html || uStatus.children.some(child => child?.tag === "img") || !uStatus.textContent.includes(hostile))
  fail("hostile up-to-date tag reached HTML or disappeared from text output");
state = { checking: false, status: "update-available", checked_at: Date.now() / 1000,
  latest: hostile, current: "0.1.0", published: "2026-09-06T00:00:00Z",
  release_url: "https://github.com/owner/repo/releases/tag/v1" };
await render();
if (uStatus._html || uStatus.children.some(child => child?.tag === "img") || !uStatus.textContent.includes(hostile) || !uBtn.textContent.includes(hostile))
  fail("hostile update-available tag reached HTML or disappeared from text output");
console.log("ok: checking state, hostile release tags, text-only metadata rendering, and server-bound release URLs pass");
'''.strip()
    result = subprocess.run(
        [node, "--input-type=module", "-", str(SETTINGS)],
        input=node_script,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return fail(f"Node update security contract failed: {detail}")
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
