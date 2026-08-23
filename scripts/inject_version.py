#!/usr/bin/env python3
"""
inject_version.py — pin one version string into every place a build needs it.

Usage:
    python scripts/inject_version.py            # auto (see below)
    python scripts/inject_version.py v0.1.1    # explicit (local builds)

Where the version comes from, in priority order:
  1. the argument,
  2. GITHUB_REF_NAME when it is version-shaped (i.e. on a `v*` tag push —
     this is the normal CI path: the TAG is the single human input),
  3. the [project] version currently in pyproject.toml (dev/checkout builds).

What it writes:
  * scm_workbench/_version.py — what the *running* app reports: the
    Data & about line, and the "current version" the updater compares
    the newest release against.
  * the [project] version in pyproject.toml — what briefcase stamps into
    the bundle at build time (the Info.plist "About" box, the
    dist-info record, and the Windows executable's version metadata all
    read this one value; the briefcase app section deliberately has no
    version of its own and inherits it).

The file is rewritten in place and is meant to run *before*
`briefcase build` (the workflow does this automatically; for a local
build run it yourself first). Nothing here is network access, and the
only things it can change are the two files named above.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# digits.digits[.digits], optional -rc1 / +local suffixes, optional v prefix
_VERSION_RE = re.compile(r"^v?(\d+)(\.\d+){0,2}([.\-+][0-9A-Za-z.\-]+)?$")


def normalize(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if not _VERSION_RE.match(v):
        return ""
    return v[1:] if v.startswith("v") and v[1].isdigit() else v


def resolve_version() -> str:
    if len(sys.argv) > 1:
        v = normalize(sys.argv[1])
        if not v:
            raise SystemExit(f"inject_version: “{sys.argv[1]}” is not a usable version "
                             "(expected e.g. 0.1.1 or v0.1.1)")
        return v
    v = normalize(os.environ.get("GITHUB_REF_NAME", ""))
    if v:
        return v
    # no tag in sight (local run / workflow_dispatch on a branch):
    # keep whatever the repo declares
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text[text.find("[project]"):])
    return m.group(1) if m else ""


def pin(version: str) -> None:
    # 1) the runtime constant (what the running app reports)
    vf = ROOT / "scm_workbench" / "_version.py"
    vf.write_text(
        '"""Build-pinned version — written by scripts/inject_version.py.\n'
        "\n"
        "Do not edit by hand: the value is the release tag the app was built\n"
        "from (or the [project] version for dev builds).\n"
        '"""\n__version__ = "' + version + '"\n',
        encoding="utf-8",
    )

    # 2) the briefcase-side value (what the bundle's Info.plist gets)
    pp = ROOT / "pyproject.toml"
    text = pp.read_text(encoding="utf-8")
    head, sep, tail = text.partition("[project]")
    if not sep:
        raise SystemExit("inject_version: pyproject.toml has no [project] section")
    new_tail, n = re.subn(r'(?m)^(version\s*=\s*")[^"]+(")',
                          lambda m: m.group(1) + version + m.group(2),
                          tail, count=1)
    if not n:
        raise SystemExit("inject_version: no version line under [project] in pyproject.toml")
    pp.write_text(head + sep + new_tail, encoding="utf-8")

    print(f"inject_version: pinned {version}")
    print(f"  {vf.relative_to(ROOT)}  ->  __version__ = \"{version}\"")
    print(f"  pyproject.toml [project] version -> \"{version}\" "
          "(briefcase inherits it for the bundle's Info.plist)")


def main() -> None:
    version = resolve_version()
    if not version:
        raise SystemExit("inject_version: no version found (no argument, no version-shaped "
                          "GITHUB_REF_NAME, and no [project] version in pyproject.toml)")
    pin(version)


if __name__ == "__main__":
    main()
