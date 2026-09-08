#!/usr/bin/env python3
"""Static contracts for simple PDF rendering and packaged first-boot setup."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
JS = UI / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    forms = (JS / "forms.js").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")
    core = (JS / "core.js").read_text(encoding="utf-8")
    nav = (JS / "nav.js").read_text(encoding="utf-8")
    prep = (JS / "prep.js").read_text(encoding="utf-8")
    page = (JS / "pages" / "preparing.js").read_text(encoding="utf-8")
    css = (UI / "theme.css").read_text(encoding="utf-8")

    # Calling visible() without its option caused optVisible(undefined, spec)
    # to throw and left the simple Create PDF route entirely blank.
    if forms.count("if (o && visible(o))") != 2:
        return fail("flat form rows do not pass each option to the visibility predicate")
    if "if (o && visible())" in forms:
        return fail("the blank simple-PDF runtime exception is still reachable")
    for marker in (
        'if (kind === "create_pdf" && rows.length)',
        "flatMap(g => g.options || [])",
        "for (const keys of rows) placeGroup(simpleGroup, keys)",
    ):
        if marker not in forms:
            return fail(f"simple PDF does not follow its cross-group row plan: {marker}")

    for marker in (
        'import "./pages/preparing.js";',
        'if (firstBootPageNeeded()) go("preparing", null, { push: false });',
    ):
        if marker not in app:
            return fail(f"startup does not select the transient preparation page: {marker}")
    if "firstBootDismissed: false" not in core:
        return fail("first-boot dismissal is not session-local shared state")
    if 'if (p === "preparing") return null;' not in nav:
        return fail("the transient preparation page became bookmarkable")
    for marker in (
        "export function firstBootPageNeeded()",
        "!S.firstBootDismissed",
        "S.info?.server?.is_packaged",
        "some(r => !r.deployed)",
        'native?.dataset.prepAll === "true"',
        'ready ? "ready"',
        'ready ? "Downloaded and ready"',
        "const indeterminate = !det && !ready",
        'bar.classList.toggle("indet", indeterminate)',
        'fill.style.width = indeterminate ? "" : pct + "%"',
        "setTimeout(tick, 750)",
    ):
        if marker not in prep:
            return fail(f"preparation state contract is missing: {marker}")
    for marker in (
        "PAGES.preparing",
        "S.firstBootDismissed = true",
        "bootPage();",
        'id: "repoprog"',
        '"data-prep-all": "true"',
        "wrap.__patch = updatePrepRows",
        "Explore while setup continues",
        "Setup needs your attention",
        "async function retrySetup(button)",
        'j.kind === "repo_init" || j.kind === "repo_update"',
        'doRun("repo_init", null',
        '"Retry setup"',
        'startPrepWatcher();',
        'go("preparing", null, { push: false, anim: false });',
    ):
        if marker not in page:
            return fail(f"first-boot page is missing: {marker}")
    if ".first-boot-page" not in css or ".first-boot-close" not in css:
        return fail("first-boot page presentation or close control is unstyled")
    if "/api/" in page or "wb_rpc" in page or "fetch(" in page:
        return fail("first-boot page bypasses the shared info/native transport path")

    print("ok: simple PDF visibility and transient packaged first-boot setup contracts pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
