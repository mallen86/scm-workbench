#!/usr/bin/env python3
"""
launcher.py — a *small* dev hook for running SCM Workbench from a source
checkout, and the home of the private-runtime helpers the build still needs.

The packaged app does NOT open its window from here. The window is the
Tauri shell (``tauri/src/main.rs``): one native webview (WKWebView on macOS,
WebView2 on Windows) that spawns the bundled runtime running
``scm_workbench.server`` as a *child it reaps on any exit*. That shell is
self-contained — no pywebview, no .NET, no third-party runtime on the machine —
so there is exactly one window and one runtime, and a hard-quit of the app can
never leave an orphaned server holding the port (see the reap check in the
packaging workflow).

What lives here now is only:

  * the per-user data area (settings, job history, logs, managed repo copies);
  * the private python-build-standalone runtime the build bakes into the bundle
    (``provision_runtime`` / ``_runtime_download_name`` / ``RUNTIME_VERSION`` —
    imported by ``scripts/bake_runtime.py`` so the two can't drift);
  * a first-launch bootstrap that fetches the managed repo copies; and
  * ``main()`` — run the UI *server* from a checkout (the classic dev flow).
    It has no window: in a checkout you open the browser at the server's
    address, exactly as before.

Nothing in here imports the sister repos.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "scm-workbench"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "scm-workbench"
    return Path.home() / ".local" / "share" / "scm-workbench"


# The private CPython the job scripts and the UI server run on (macOS; on
# Windows the bundle's own Python serves). Its minor version must match the
# bundle's runtime because job scripts also import the bundle's own
# app_packages (cp3xx wheels); a mismatched interpreter can't load their
# compiled extensions (PIL dies with "cannot import name '_imaging'").
RUNTIME_VERSION = "3.13.15"


def _runtime_download_name() -> str:
    """The pinned python-build-standalone archive for this machine.

    ``scripts/bake_runtime.py`` imports this so the CI bake and a first-boot
    provisioning can never fetch different runtimes.
    """
    import platform as _plat
    release = "20260814"  # pinned pbs build carrying RUNTIME_VERSION; bump deliberately
    if sys.platform == "win32":
        arch = "aarch64" if _plat.machine() == "ARM64" else "x86_64"
        return f"cpython-{RUNTIME_VERSION}+{release}-{arch}-pc-windows-msvc-pgo-full.tar.zst"
    arch = "aarch64" if _plat.machine() == "arm64" else "x86_64"
    return f"cpython-{RUNTIME_VERSION}+{release}-{arch}-apple-darwin-pgo+lto-full.tar.zst"


def _runtime_python_path(rt: Path) -> Path:
    """Where the provisioned interpreter ends up, per platform."""
    if sys.platform == "win32":
        return rt / "python" / "install" / "python.exe"
    minor = ".".join(RUNTIME_VERSION.split(".")[:2])
    return rt / "python" / "install" / "bin" / f"python{minor}"


def provision_runtime(data: Path, log) -> str:
    """Ensure a real, relocatable CPython lives in the data area and jobs can use it.

    The private runtime is GHCI's python-build-standalone: a relocatable,
    pip-included CPython that runs from the data area with zero system
    prerequisites. One-time ~60 MB download; a marker makes repeats cheap.
    """
    rt = data / "runtime"
    marker = rt / ".ready"
    expected = _runtime_python_path(rt)
    minor = ".".join(RUNTIME_VERSION.split(".")[:2])
    if marker.is_file():
        have = str(marker.read_text(encoding="utf-8").strip())
        if have == str(expected) and Path(have).is_file():
            return have
        log(f"\n[launcher] the private runtime in the data area is out of date "
            f"({Path(have).name or '?'}, needs python{minor}) — re-provisioning …")
        shutil.rmtree(rt, ignore_errors=True)
    name = _runtime_download_name()
    release = "20260814"  # keep in sync with _runtime_download_name
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{name}"
    log(f"\n[launcher] provisioning a private CPython {RUNTIME_VERSION} runtime (~60 MB download, one-time) …")
    t0 = time.time()
    import urllib.request
    tgz = data / ".runtime-download.tar.zst"
    urllib.request.urlretrieve(url, str(tgz))
    log(f"[launcher] {tgz.stat().st_size / 1e6:.0f} MB received — extracting …")
    rt.mkdir(parents=True, exist_ok=True)
    # The stdlib tarfile learned zstd only in Python 3.14 — on an older
    # interpreter that attempt raises, so fall back to the system tar
    # (macOS bsdtar reads zstd via libarchive).
    ok = False
    try:
        import tarfile
        with tarfile.open(tgz, "r:*") as tf:
            tf.extractall(rt, filter="data")
        ok = expected.is_file()
    except Exception:
        ok = False
    if not ok:
        import subprocess as _sp
        # Windows' tar is a console app: without CREATE_NO_WINDOW the one-time
        # extraction would flash a terminal window open and closed.
        run_kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        r = _sp.run(["tar", "-xf", str(tgz), "-C", str(rt)],
                     capture_output=True, text=True,
                     encoding="utf-8", errors="replace", **run_kw)
        if r.returncode != 0:
            raise RuntimeError(f"extracting the runtime failed: {(r.stderr or '').strip()[-200:]}")
        ok = expected.is_file()
    if not ok:
        raise RuntimeError("the runtime archive did not unpack as expected — check the download URL")
    tgz.unlink(missing_ok=True)
    marker.write_text(str(expected), encoding="utf-8")
    log(f"[launcher] runtime ready in {time.time() - t0:.0f}s → {expected}")
    return str(expected)


def first_boot_bootstrap(data: Path, log) -> None:
    """First-launch prep, never fatal: fetch the managed repo copies into the
    data area (a copy the app owns, so an update can't touch a user clone).
    A failure just means the app runs with what it has and Settings offers a
    retry. Runs in the foreground of a dev launch; the packaged app runs the
    same work as a background thread of its server.
    """
    from scm_workbench import repo_sync
    from scm_workbench.bootstrap import bootstrap_managed_repos

    flag = data / "bootstrap.json"

    def set_flag(pending, done, failed=None, phase="…"):
        try:
            flag.write_text(
                json.dumps({"pending": pending, "done": done, "failed": failed or [], "phase": phase}),
                encoding="utf-8")
        except Exception:
            pass

    def blog(s: str = "") -> None:
        line = str(s).strip()
        if line:
            set_flag(["scm", "extras"], [])
        log(s)

    set_flag(["scm", "extras"], [])
    results = {key: True for key in repo_sync.REPOS}
    try:
        if not os.environ.get("SCM_WORKBENCH_NO_BOOTSTRAP"):
            results = bootstrap_managed_repos(repo_sync, log=blog)
    finally:
        done = [key for key in repo_sync.REPOS if results.get(key)]
        failed = [key for key in repo_sync.REPOS if results.get(key) is False]
        set_flag([], done, failed, "done" if not failed else "setup incomplete")
        if failed:
            log("[launcher] first-launch preparation incomplete — retry the failed managed copies.")
        else:
            log("[launcher] first-launch preparation finished — the UI now sees the managed copies.")


def main() -> None:
    """Run the UI *server* from a checkout — the classic dev flow.

    This deliberately opens no window and imports no webview: the packaged
    window belongs to the Tauri shell. From a source tree you point a browser
    at the printed address, and the server + its job children live and die
    with this process (Ctrl-C / a plain exit reaps them the ordinary way).
    """
    data = Path(os.environ.get("SCM_WORKBENCH_DATA") or str(default_data_dir())).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    os.environ["SCM_WORKBENCH_DATA"] = str(data)
    os.environ["SCM_WORKBENCH_PACKAGED"] = "1"

    def log(s: str = "") -> None:
        print(s)
        try:
            with open(data / "launcher.log", "a", encoding="utf-8") as f:
                f.write(str(s) + "\n")
        except Exception:
            pass

    # A data area that predates the in-bundle runtime (or holds a mismatched
    # one) is re-provisioned so job scripts run on the interpreter we expect.
    # In a dev checkout SCM_WORKBENCH_PYTHON is usually set by the caller; we
    # only provision when the caller hasn't already chosen an interpreter.
    if sys.platform in ("darwin", "win32") and not os.environ.get("SCM_WORKBENCH_PYTHON"):
        try:
            os.environ["SCM_WORKBENCH_PYTHON"] = provision_runtime(data, log)
        except Exception as e:
            log(f"[launcher] runtime provisioning failed ({e}) — jobs will wait for the next launch.")

    # make the checkout the working directory (templates, relative paths, …)
    try:
        os.chdir(Path(__file__).resolve().parent.parent)
    except Exception:
        pass

    first_boot_bootstrap(data, log)

    # Hand over to the server. The window (packaged) or the user's browser
    # (dev) drives it; this process is the server's lifecycle owner.
    from scm_workbench import server
    server.main()
