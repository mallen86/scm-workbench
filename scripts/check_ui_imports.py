#!/usr/bin/env python3
"""
check_ui_imports.py — fail the build if any ES-module import in ui/js
cannot resolve to a real file, relative to the file that writes it.

This is the guard for the v0.3.5 blank-window bug: the page modules
imported their shared dependencies from the wrong directory ('./core.js'
from inside ui/js/pages/ points at a file that does not exist), the
worker 404'd them as JSON, WebKit refused the MIME type, and the whole
UI died after the nav shell painted. Every specifier is checked against
the real tree, and a specifier that could not resolve is printed with
both the written and the likely-correct form.
"""
import re
import sys
from pathlib import Path

UI_JS = Path(__file__).resolve().parent.parent / "ui" / "js"
IMPORT_RE = re.compile(r'''from\s+["\']([^"\']+)["\']''')


def main() -> int:
    if not UI_JS.is_dir():
        print(f"ui/js not found at {UI_JS} — nothing to check")
        return 0
    bad = 0
    for f in sorted(UI_JS.rglob("*.js")):
        for m in IMPORT_RE.finditer(f.read_text()):
            spec = m.group(1)
            if spec.startswith(("./", "../")):
                target = (f.parent / spec).resolve()
            else:
                continue  # bare specifiers are not used in this tree
            if not target.is_file():
                bad += 1
                # the likely correction, for the message:
                name = spec.rsplit("/", 1)[-1]
                fixes = [str((UI_JS / name).relative_to(f.parent)),
                         str((UI_JS / "pages" / name).relative_to(f.parent))]
                fix = next((x for x in fixes if (f.parent / x).is_file()), None)
                print(f"  {f.relative_to(UI_JS.parent.parent)}: "
                      f'import ... from "{spec}" -> no such file'
                      + (f"  (did you mean \"{fix}\"?)" if fix else ""))
    if bad:
        print(f"\nFAIL: {bad} import specifier(s) in ui/js do not resolve. "
              f"The window would ship broken again - fix the paths and re-run.")
        return 1
    print(f"ok: every relative import under ui/js resolves to a real file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
