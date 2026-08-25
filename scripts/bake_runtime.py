#!/usr/bin/env python3
"""
bake_runtime.py — build the app's *bundled* private runtime (CI step).

Downloads the pinned python-build-standalone archive — the exact name the
launcher fetches on a first boot, imported from scm_workbench.launcher so
the two can never drift — unpacks it into <bundle>/runtime, then installs
the managed repos' job dependencies into it. The shipped bundle is thus
self-contained: first launch needs no downloads other than the managed repo
copies themselves.

Usage:
    python scripts/bake_runtime.py --bundle <dir>
    python scripts/bake_runtime.py --bundle <dir> --skip-fetch   # runtime already there
    python scripts/bake_runtime.py --bundle <dir> --pbs-archive <file>  # local archive

Notes:
  * Only the `scm` repo ships a requirements file (see repo_sync.REPOS); the
    bake installs it. The dependency *pins* go through the app's own
    _KNOWN_BAD_PINS rewrite so CI bakes exactly what a first boot would.
  * Unlike the user-facing sync (which is deliberately never fatal), the bake
    is strict: a dependency that cannot install fails the build.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = "20260814"  # keep in sync with scm_workbench/launcher.py
GITHUB = "https://github.com"


def pbs_archive_url() -> str:
    # the launcher knows the exact pinned archive name for this platform
    sys.path.insert(0, str(ROOT))
    from scm_workbench import launcher
    name = launcher._runtime_download_name()
    return f"{GITHUB}/astral-sh/python-build-standalone/releases/download/{RELEASE}/{name}"


def fetch_runtime(bundle: Path, archive: str | None, skip_fetch: bool) -> Path:
    rt = bundle / "runtime"
    py = rt / "python" / "install" / "python.exe"
    if skip_fetch and py.is_file():
        print(f"bake_runtime: using the runtime already at {py}")
        return py
    if archive:
        tgz = Path(archive)
        print(f"bake_runtime: using local pbs archive {tgz}")
    else:
        url = pbs_archive_url()
        tgz = bundle / ".runtime-download.tar.zst"
        print(f"bake_runtime: fetching the pinned runtime: {url}")
        t0 = time.time()
        urllib.request.urlretrieve(url, str(tgz))
        print(f"bake_runtime: {tgz.stat().st_size / 1e6:.0f} MB received in {time.time() - t0:.0f}s")
    rt.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["tar", "-xf", str(tgz), "-C", str(rt)], capture_output=True, text=True)
    if r.returncode != 0 or not py.is_file():
        raise SystemExit(f"bake_runtime: extracting the runtime failed: {(r.stderr or '').strip()[-300:]}")
    if not archive:
        tgz.unlink(missing_ok=True)
    print(f"bake_runtime: runtime ready at {py}")
    return py


def latest_release_ref(owner: str, repo: str) -> str:
    """Newest vX.Y.Z tag of an upstream repo (git ls-remote, no clone yet)."""
    r = subprocess.run(
        ["git", "ls-remote", "--tags", f"{GITHUB}/{owner}/{repo}.git"],
        capture_output=True, text=True, check=True)
    refs = [line.split()[-1] for line in r.stdout.splitlines()
            if line.endswith(("^{}",)) is False and "/tags/" in line]
    tags = [re.sub(r"^refs/tags/", "", t) for t in refs]
    tags = [t for t in tags if re.match(r"^v\d+(\.\d+)+$", t)]
    if not tags:
        raise SystemExit(f"bake_runtime: no version-shaped tags found on {owner}/{repo}")
    tags.sort(key=lambda t: [int(p) for p in t.lstrip("v").split(".")])
    return tags[-1]


def bake_deps(py: Path, bundle: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from scm_workbench import repo_sync

    work = bundle / ".bake"
    work.mkdir(exist_ok=True)
    for key, meta in repo_sync.REPOS.items():
        req_name = meta.get("requirements")
        if not req_name:
            print(f"bake_runtime: {meta['name']} ships no requirements file — nothing to bake")
            continue
        ref = latest_release_ref(meta["owner"], meta["repo"])
        clone = work / meta["name"]
        print(f"bake_runtime: cloning {meta['owner']}/{meta['repo']} @ {ref} (for its {req_name})")
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", ref,
             f"{GITHUB}/{meta['owner']}/{meta['repo']}.git", str(clone)],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"bake_runtime: cloning {meta['name']} failed: {(r.stderr or '').strip()[-200:]}")
        src = clone / req_name
        if not src.is_file():
            raise SystemExit(f"bake_runtime: {meta['name']} @ {ref} has no {req_name}")
        # the same pin rewrites the app applies at first-boot, so CI bakes the
        # exact set a user would end up with
        patched = repo_sync.apply_bad_pin_fixes(
            src.read_text(encoding="utf-8", errors="replace").splitlines())
        req = work / f"{key}-requirements.txt"
        req.write_text("\n".join(patched) + "\n", encoding="utf-8")
        run_kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        print(f"bake_runtime: installing {meta['name']}'s dependencies into the bundled runtime …")
        r = subprocess.run(
            [str(py), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", **run_kw)
        if r.returncode != 0:
            tail = " ".join((r.stderr or r.stdout or "").strip().split())[-400:]
            raise SystemExit(f"bake_runtime: dependency install for {meta['name']} failed:\n{tail}")
        print(f"bake_runtime: {meta['name']} dependencies baked")
    # Windows occasionally holds a handle in the scratch dir a moment after
    # the clone dies, so retry the cleanup instead of trusting one shot:
    for _ in range(3):
        shutil.rmtree(work, ignore_errors=True)
        if not work.exists():
            break
    if work.exists():
        print("bake_runtime: warning - could not fully remove the .bake scratch dir; "
              "the release pipeline removes it before zipping too")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", required=True, help="the bundle dir containing (or receiving) runtime/")
    ap.add_argument("--skip-fetch", action="store_true", help="reuse the runtime already in the bundle")
    ap.add_argument("--pbs-archive", help="local pbs archive to use instead of downloading")
    args = ap.parse_args()
    bundle = Path(args.bundle).resolve()
    bundle.mkdir(parents=True, exist_ok=True)
    py = fetch_runtime(bundle, args.pbs_archive, args.skip_fetch)
    bake_deps(py, bundle)
    print("bake_runtime: done — the bundled runtime is self-contained.")


if __name__ == "__main__":
    main()
