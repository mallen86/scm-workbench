#!/usr/bin/env python3
"""
launcher.py — entry point for the *packaged* SCM Workbench app.

In a normal dev checkout you run ``python -m scm_workbench.server`` directly;
this module is the *app's* entry point (the bundle's stub calls it), where it:

  1. pins the app's data area to a per-user, writable location that survives
     app updates (settings, job history, logs, and the managed repo copies),
  2. marks the run as packaged (dependency sync may then pip into the app's
     own Python — never the user's),
  3. bootstraps first launch: if no managed repo copy exists yet, fetch the
     newest one now (or report that the UI buttons can do it later),
  4. hands over to the regular server.

Nothing in here imports the sister repos — same rule as the Workbench itself.
"""

import json
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "scm-workbench"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "scm-workbench"
    return Path.home() / ".local" / "share" / "scm-workbench"


# The private CPython the job scripts run on (macOS only; on Windows the
# bundle's own Python serves). Its minor version must match the bundle's
# runtime — `python_version` in the briefcase config — because job scripts
# also import the bundle's own Resources/app_packages (cp3xx wheels), and a
# mismatched interpreter can't load their compiled extensions (PIL dies with
# "cannot import name '_imaging' from 'PIL'" in that case). A data area that
# still holds a mismatched runtime is re-provisioned automatically below.
RUNTIME_VERSION = "3.13.15"


def _runtime_download_name() -> str:
    """The pinned python-build-standalone archive for this machine."""
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

    The bundle's own interpreter is only reachable through the app stub - and
    on Windows that stub is a fixed "run the app" binary that ignores its
    arguments, so it can't even be re-run as a server - which is why job
    scripts (create_pdf.py & friends) and the UI-server child need their own
    real interpreter. We provision GHCI's python-build-standalone: a
    relocatable, pip-included CPython that runs from the data area with zero
    system prerequisites.
    """
    rt = data / "runtime"
    marker = rt / ".ready"
    expected = _runtime_python_path(rt)
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
    rt.mkdir(parents=True, exist_ok=True)  # the re-provision path wipes it
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


def bootstrap_managed_repos(repo_sync, log=None) -> None:
    """Fetch newest managed copies on first launch; never fatal."""
    if log is None:
        log = print
    state = repo_sync.load_state()
    for key, meta in repo_sync.REPOS.items():
        r = state.get(key) or {}
        if r.get("deployed"):
            # The state says “deployed” — but the state file and the tree can
            # drift (an interrupted update, a pre-lock era write). A cheap
            # offline probe decides; a mismatch triggers a *safe* re-deploy,
            # which stages the user's files before touching the tree, so this
            # path can never be a data-loss path again.
            try:
                if repo_sync.verify_deployed(key):
                    continue
                log(f"\n[launcher] the managed {meta['name']} copy no longer matches its recorded "
                    f"state — re-deploying it now (your decklists, images and output are "
                    f"staged and restored around the swap, so nothing is lost) …")
                repo_sync.cmd_init(key, log=log, force_redeploy=True)
                log(f"[launcher] {meta['name']} re-synced — continuing.")
                continue
            except Exception as e:
                log(f"[launcher] {meta['name']} state check failed ({e}) — leaving the copy as-is.")
            continue
        log(f"\n[launcher] first launch — fetching the newest {meta['name']} "
           f"({meta['owner']}/{meta['repo']}) into the Workbench data area …")
        t0 = time.time()
        try:
            repo_sync.cmd_init(key, log=log)
            log(f"[launcher] done in {time.time() - t0:.0f}s — dashboard is ready.")
        except Exception as e:
            log(f"[launcher] could not fetch {meta['name']} yet ({e}).")
            log("[launcher] no problem — the app still works; open Settings → "
                "“Managed repo copies” and press “Download latest” when you're online.")
            log("[launcher] (existing sister folders next to this app are still detected normally)")


def main() -> None:
    data = Path(os.environ.get("SCM_WORKBENCH_DATA") or str(default_data_dir())).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    os.environ["SCM_WORKBENCH_DATA"] = str(data)
    os.environ["SCM_WORKBENCH_PACKAGED"] = "1"

    # an in-place update keeps one backup of the previous app folder beside
    # the new one (`.old-<stamp>`); by the time a new instance reaches this
    # point it is provably alive, so the backup can go now
    bundle = _app_bundle()
    if bundle is not None:
        import shutil as _sh
        for old in bundle.parent.glob(bundle.name + ".old-*"):
            try:
                _sh.rmtree(old, ignore_errors=True)
            except Exception:
                pass

    # Packaged macOS/Windows Pythons sometimes can't see the OS trust store
    # ("unable to get local issuer certificate"). Bundle certifi and point the
    # default SSL context at it so every HTTPS call in the app works offline
    # of any system configuration.
    try:
        import certifi  # shipped in the support venv (see pyproject requires)
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except Exception:
        pass

    # make the app root the working directory (templates, relative paths, …)
    try:
        os.chdir(Path(__file__).resolve().parent.parent)
    except Exception:
        pass

    from scm_workbench import repo_sync, server

    settings_file = data / "settings.json"
    if not settings_file.exists():
        # first launch ever: nothing to configure — point the app at its own
        # managed copies by default (the user can switch to their own clones later)
        try:
            settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
            server.save_settings(settings)
        except Exception:
            pass

    # --- from here on: window mode (packaged) or classic server mode (dev) ---
    if os.environ.get("SCM_WORKBENCH_PACKAGED"):
        def log(s: str = "") -> None:
            print(s)
            try:
                with open(data / "launcher.log", "a", encoding="utf-8") as f:
                    f.write(str(s) + "\n")
            except Exception:
                pass
        _run_window(data, log, server)
        return

    # Dev flow (running the package from a checkout): classic CLI server.
    server.main()


# The UI server's child process (packaged window mode).
_ui_child = None


def _server_up(port: int) -> bool:
    """Plain-TCP probe: the port accepts connections == the UI server is up.
    Deliberately not HTTP — a TCP handshake has no origin/ATS subtleties."""
    import socket as _s
    try:
        s = _s.socket()
        s.settimeout(1.5)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def _app_bundle():
    """The app's own folder, when running inside one (the updater swaps it in
    place). The launcher is the only process that can see it: inside a macOS
    bundle the interpreter lives under `…/SCM Workbench.app/…`; on Windows the
    bundle root is the exe's own folder (the exe is a renamed Python)."""
    try:
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            for p in exe.parents:
                if p.name.endswith(".app"):
                    return p
        elif os.name == "nt" and exe.suffix.lower() == ".exe":
            # Shipped (flattened) layout: the exe sits at the bundle root, beside
            # app/, app_packages/ and the bundled runtime files.
            if (exe.parent / "app").is_dir():
                return exe.parent
            # Pre-flattening build tree: the exe lives in src/, the root is up.
            if exe.parent.name == "src" and (exe.parent.parent / "app").is_dir():
                return exe.parent.parent
    except Exception:
        pass
    return None


def _stop_leftover_server(data: Path, log) -> None:
    """Stop a UI server orphaned by an earlier launch (its window died but
    the child kept running, and keeps the configured port) — it would answer
    with stale state. Waits until the process is actually gone so the port is
    released before anything reserves it."""
    try:
        pf = data / "server.pid"
        if pf.exists():
            old = int(pf.read_text().strip() or 0)
            if old:
                try:
                    os.kill(old, 0)
                    log(f"[launcher] stopping leftover UI server (pid {old}) from a previous launch")
                    os.kill(old, signal.SIGTERM)
                    for _ in range(20):
                        time.sleep(0.25)
                        try:
                            os.kill(old, 0)
                        except OSError:
                            break
                    else:
                        os.kill(old, signal.SIGKILL)
                except OSError:
                    pass
            pf.unlink(missing_ok=True)
    except Exception:
        pass


def _ensure_server(data: Path, log, url: str, port: int, server) -> None:
    """Run the UI server as a separate child process: the same classic server
    the dev flow uses, but with its own interpreter, its own lifecycle and a
    log file of its own (<data>/server.log) — so its behaviour is always
    inspectable afterwards and it can never share fate with the window.
    """
    global _ui_child
    import subprocess as _sp
    import urllib.request as _ur

    # Defensive: the normal stop happens in _run_window before the port is
    # reserved; this covers direct/older calls where it did not.
    _stop_leftover_server(data, log)

    py = os.environ.get("SCM_WORKBENCH_PYTHON") or str(server.bundled_python())
    # Never Popen the app's own entry stub. On Windows it is a fixed
    # "run the app" binary that ignores its arguments, so re-launching it as
    # the "server child" would start a brand-new copy of this app - which
    # starts another, and so on (a fork bomb of identical processes). While
    # the private runtime isn't ready, the UI simply waits: the server starts
    # on the next launch, once provisioning has written SCM_WORKBENCH_PYTHON.
    if (os.name == "nt" and not os.environ.get("SCM_WORKBENCH_PYTHON")
            and Path(py).resolve() == Path(sys.executable).resolve()):
        log("[launcher] the private runtime isn't ready yet, so the UI server will not run as its "
            "own process (the app stub can only re-launch the app, not serve). It starts on the next "
            "launch - the UI's prep banner tracks the provisioning.")
        return
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["SCM_WORKBENCH_DATA"] = str(data)
    if bundle := _app_bundle():
        env["SCM_WORKBENCH_BUNDLE"] = str(bundle)
    # The child's own stdio must be UTF-8. The server prints a banner containing
    # box-drawing glyphs; a freshly spawned interpreter on Windows defaults to
    # the machine's ANSI codepage (e.g. cp1252), which cannot encode them, so
    # v0.2.2's server child died with UnicodeEncodeError before ever binding its
    # port (a terminal window flashed up and closed, and the UI never appeared).
    # PEP 540/528: UTF-8 mode, and a matching explicit stdio encoding for the
    # case an older interpreter ignores one of the two.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        # UTF-8 log file: the child writes UTF-8 bytes (see above), and this is
        # the file you read afterwards to inspect what the server did.
        logf = open(data / "server.log", "a", buffering=1, encoding="utf-8", errors="replace")
        logf.write("\n===== UI server start %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        # Never let the child allocate a console window — the app is "no
        # terminal"; everything it prints ends up in server.log anyway.
        popen_kw = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {}  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        _ui_child = _sp.Popen(
            [str(py), "-m", "scm_workbench.server",
             "--port", str(port), "--host", "127.0.0.1", "--no-browser"],
            cwd=str(root), env=env, stdout=logf, stderr=_sp.STDOUT, **popen_kw)
        log("[launcher] UI server process started (pid %d, interpreter %s)" % (_ui_child.pid, py))
    except Exception as e:
        log(f"[launcher] could not start the UI server: {e}")
        return
    for _ in range(90):
        try:
            _ur.urlopen(url, timeout=2).read()
            log(f"[launcher] UI server is answering at {url} (details in server.log)")
            return
        except Exception:
            time.sleep(1)
    log("[launcher] the UI server has not come up yet — the window keeps retrying on its own.")


def _stop_server(log, data=None) -> None:
    global _ui_child
    if _ui_child is not None and _ui_child.poll() is None:
        _ui_child.terminate()
        try:
            _ui_child.wait(timeout=5)
        except Exception:
            _ui_child.kill()
    if data is not None:
        try:
            pf = data / "server.pid"
            if pf.exists() and pf.read_text().strip() == str(getattr(_ui_child, "pid", -1)):
                pf.unlink()
        except Exception:
            pass
    _ui_child = None


def _background_bootstrap(data: Path, log, url: str, port: int, server) -> None:
    """First-launch prep, off the critical path: the window is already up
    (showing its “starting” page), so this daemon thread only has to be
    eventually consistent. bootstrap.json tells the UI (via /api/info) whether
    prep is still going.
    """
    from scm_workbench import repo_sync
    flag = data / "bootstrap.json"
    phase = {"v": "starting up …"}

    def set_flag(pending, done):
        try:
            flag.write_text(json.dumps({"pending": pending, "done": done, "phase": phase["v"]}), encoding="utf-8")
        except Exception:
            pass

    def blog(s: str = "") -> None:
        # every transcript line also refreshes the UI's status (the dashboard
        # banner polls /api/info, which reads this flag)
        line = str(s).strip()
        if line:
            phase["v"] = line[:140]
            set_flag(["scm", "extras"], [])
        log(s)

    set_flag(["scm", "extras"], [])
    try:
        # 1) the private runtime for job scripts and the UI-server child
        #    (macOS + Windows; one-time per machine)
        if sys.platform in ("darwin", "win32") and not os.environ.get("SCM_WORKBENCH_PYTHON"):
            try:
                os.environ["SCM_WORKBENCH_PYTHON"] = provision_runtime(data, blog)
            except Exception as e:
                blog(f"[launcher] runtime provisioning failed ({e}) - the UI server and jobs will wait for the next launch.")
        # 2) the UI server itself (child process — see _ensure_server)
        _ensure_server(data, blog, url, port, server)
        # 3) the managed repo copies (idempotent: a repo already at the wanted
        #    source is reported up to date and skipped)
        if not os.environ.get("SCM_WORKBENCH_NO_BOOTSTRAP"):
            bootstrap_managed_repos(repo_sync, log=blog)
    finally:
        set_flag([], ["scm", "extras"])
        log("[launcher] first-launch preparation finished — the UI now sees the managed copies.")


def _configure_dotnet(data: Path, log) -> str:
    """Windows: make the *window* work, when this machine lets it.

    pywebview's Windows platform hosts the UI in a WinForms window, which
    needs a machine .NET (Core) runtime; the bundled pythonnet ships only a
    netstandard 2.0 (Core) build of Python.Runtime, so pythonnet's "default on
    Windows" (.NET Framework) loader cannot run it - the winforms import dies
    with "Failed to resolve Python.Runtime.Loader.Initialize", and pywebview's
    own coreclr retry is defeated by pythonnet's sticky runtime global. In
    v0.2.3 that sent every Windows launch into the plain-browser fallback.

    If a .NET runtime with the Windows Desktop (WinForms) component is
    installed, we select coreclr *before* the first `import clr` and pin a
    runtimeconfig that also pulls in the Windows Desktop framework, so
    System.Windows.Forms and the WebView2 host resolve. The config lives in
    the data area: it must be writable, and the updater swaps the app folder,
    never the data. Returns "ok" or a short reason the UI can show.
    """
    def versions(frame: str) -> list:
        d = dotnet_root / "shared" / frame
        out = []
        if d.is_dir():
            for p in d.iterdir():
                m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", p.name)
                if p.is_dir() and m:
                    out.append((int(m.group(1)), int(m.group(2))))
        return out

    root = os.environ.get("DOTNET_ROOT")
    if not root:
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        root = str(Path(pf) / "dotnet")
    dotnet_root = Path(root)
    if not (dotnet_root / "shared").is_dir():
        return ("no .NET runtime found (looked for %s) — install the .NET 8 Desktop "
                "Runtime for the native window; the UI runs in your browser meanwhile" % dotnet_root)
    common = sorted(set(versions("Microsoft.NETCore.App")) & set(versions("Microsoft.WindowsDesktop.App")), reverse=True)
    if not common:
        return ("no .NET *Windows Desktop* (WinForms) runtime is installed — install the .NET 8 "
                "Desktop Runtime for the native window; the UI runs in your browser meanwhile")
    major, minor = common[0]
    v = f"{major}.{minor}.0"
    net = data / ".net"
    net.mkdir(parents=True, exist_ok=True)
    cfg = net / "scm-workbench.runtimeconfig.json"
    opts = {
        # canonical TFM is major.minor only ("net10.0") - the host rejects
        # three-part forms, and clr_loader's own generated configs use this
        # exact shape.
        "tfm": f"net{major}.{minor}",
        "frameworks": [
            {"name": "Microsoft.NETCore.App", "version": v},
            {"name": "Microsoft.WindowsDesktop.App", "version": v},
        ],
    }
    # The bundled Python.Runtime (netstandard 2.0) resolves its facade
    # assemblies from pythonnet's own runtime folder; offer it as a flat
    # probing path (harmless if the frameworks already provide them).
    if bundle := _app_bundle():
        rt = bundle / "app_packages" / "pythonnet" / "runtime"
        if rt.is_dir():
            opts["additionalProbingPaths"] = [str(rt)]
    cfg.write_text(json.dumps({"runtimeOptions": opts}, indent=2), encoding="utf-8")
    os.environ["PYTHONNET_RUNTIME"] = "coreclr"
    os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"] = str(cfg)
    os.environ["PYTHONNET_CORECLR_DOTNET_ROOT"] = str(dotnet_root)
    log(f"[launcher] .NET {major}.{minor} desktop runtime found — the app window will run on it "
        f"(WinForms + WebView2); config: {cfg}")
    return "ok"


def _patch_winforms_coreclr(log) -> None:
    """Windows: make the bundled pywebview's winforms platform importable under
    the .NET (Core) runtime.

    The platform does `from Microsoft.Win32 import SystemEvents` — correct on
    .NET Framework, where that type sits in the Microsoft.Win32 *namespace* of
    the monolithic System assembly. .NET (Core) moved the type into its own
    assembly/namespace (Microsoft.Win32.SystemEvents), so the bare import
    raises ImportError and the whole platform (and hence the window) dies.
    Rewriting that single line to a try/except that falls back to the Core
    spelling is the entire fix; done in place in the bundle at launch
    (idempotent — a re-shipped bundle just gets patched again).
    """
    if os.name != "nt":
        return
    if bundle := _app_bundle():
        wf = bundle / "app_packages" / "webview" / "platforms" / "winforms.py"
        if not wf.is_file():
            return
        try:
            src = wf.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log(f"[launcher] could not read the winforms platform to patch it ({e})")
            return
        marker = "clr.AddReference('Microsoft.Win32.SystemEvents')"
        if marker in src:
            return  # already at the current patch level
        if "Microsoft.Win32.SystemEvents" in src:
            # an earlier patch level: the Core import exists but never references
            # its assembly, which pythonnet cannot find (it only resolves against
            # loaded assemblies). Insert the reference right before that import.
            patched = src.replace(
                "    from Microsoft.Win32.SystemEvents import SystemEvents",
                "    " + marker + "\n"
                "    from Microsoft.Win32.SystemEvents import SystemEvents")
            if patched != src:
                try:
                    wf.write_text(patched, encoding="utf-8")
                    log("[launcher] upgraded the winforms SystemEvents patch to reference the "
                        "assembly (one-time, in the bundle copy)")
                except Exception as e:
                    log(f"[launcher] could not upgrade the winforms patch ({e})")
                return
        patched = src.replace(
            "from Microsoft.Win32 import SystemEvents  # noqa: E402",
            "try:\n"
            "    from Microsoft.Win32 import SystemEvents  # .NET Framework\n"
            "except ImportError:\n"
            "    clr.AddReference('Microsoft.Win32.SystemEvents')  # .NET (Core) moved the type to its own assembly, and pythonnet only sees loaded ones\n"
            "    from Microsoft.Win32.SystemEvents import SystemEvents")
        if patched == src:
            # tolerate a differently-commented original line
            patched = src.replace(
                "from Microsoft.Win32 import SystemEvents",
                "try:\n"
                "    from Microsoft.Win32 import SystemEvents  # .NET Framework\n"
                "except ImportError:\n"
                "    clr.AddReference('Microsoft.Win32.SystemEvents')  # .NET (Core)\n"
                "    from Microsoft.Win32.SystemEvents import SystemEvents")
        if patched == src:
            log("[launcher] the winforms platform's SystemEvents import was not the known "
                "line - leaving it untouched")
            return
        try:
            wf.write_text(patched, encoding="utf-8")
            log("[launcher] patched pywebview's winforms SystemEvents import for the .NET (Core) "
                "runtime (one-time, in the bundle copy)")
        except Exception as e:
            log(f"[launcher] could not patch the winforms platform ({e}) - the native window "
                "may not start")


def _run_window(data: Path, log, server) -> None:
    """Packaged mode: the app's own window is the interface.

    A native webview (system WKWebView on macOS, WinForms + WebView2 on
    Windows — the latter needs a machine .NET (Core) runtime, which
    _configure_dotnet points the CLR loader at) shows the UI. The UI server
    runs as a separate child process (logged to <data>/server.log), so the
    window process can never drag it down with it. Closing the window stops
    the server and quits the app. If no webview is available we fall back to
    the system browser, the way the dev flow works — with the reason in
    <data>/window.json, which the dashboard shows.
    """
    import socket as _sock
    import threading
    import webbrowser

    settings = server.load_settings()
    wanted = int(settings.get("port") or server.DEFAULT_PORT)
    # A previously orphaned UI server may still hold the configured port —
    # stop it *before* reserving, or the probe below falls back to a random
    # port and every relaunch moves the UI to a new address.
    _stop_leftover_server(data, log)
    probe = _sock.socket()
    # SO_REUSEADDR like the real HTTP server uses — a freshly-stopped server
    # leaves the port in TIME_WAIT, which must not force a fallback port.
    probe.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    try:
        for _ in range(20):  # up to ~5 s for a just-stopped server to let go
            try:
                probe.bind(("127.0.0.1", wanted))
                break
            except OSError:
                time.sleep(0.25)
        else:
            probe.close()
            probe = _sock.socket()
            probe.bind(("127.0.0.1", 0))
            log(f"[launcher] port {wanted} is busy — the UI server will use a free port instead.")
    except Exception:
        probe.close()
        probe = _sock.socket()
        probe.bind(("127.0.0.1", 0))
        log(f"[launcher] port {wanted} is busy — the UI server will use a free port instead.")
    port = probe.getsockname()[1]
    probe.close()
    url = f"http://127.0.0.1:{port}"
    log(f"[launcher] opening the app window (UI server will listen at {url}) …")

    def prepare() -> None:
        threading.Thread(target=_background_bootstrap, args=(data, log, url, port, server),
                         daemon=True, name="prepare").start()

    prepare()
    # The window opens on the REAL UI — so first wait for the server to accept
    # connections (a couple of seconds on normal launches; longer on a true
    # first launch, while the private runtime downloads). No loading page.
    waited = 0.0
    while not _server_up(port) and waited < 180:
        time.sleep(0.3)
        waited += 0.3
    if _server_up(port):
        log(f"[launcher] UI server is up — opening the app window at {url}")
    else:
        log(f"[launcher] the UI server is not up after {waited:.0f}s — opening the window anyway; "
            "it will show the UI as soon as the server starts.")

    # Windows: the WinForms host of the webview needs a machine .NET (Core)
    # runtime — select it before the first `import clr` below, or the
    # bundled netstandard 2.0 Python.Runtime cannot be loaded by the
    # .NET-Framework loader and the window silently dies into the browser.
    dotnet_note = _configure_dotnet(data, log) if os.name == "nt" else None
    if os.name == "nt":
        _patch_winforms_coreclr(log)

    try:
        # Route every pywebview log record (including the full traceback of a
        # failing platform import - the logger.exception of its try_import)
        # into launcher.log, so a fallback is always diagnosable afterwards.
        import logging as _logging
        import traceback as _tb

        class _LogSink(_logging.Handler):
            def emit(self, record):
                try:
                    msg = record.getMessage()
                    if record.exc_info:
                        msg += "\n" + "".join(_tb.format_exception(*record.exc_info))
                    log("[launcher] webview: %s" % msg)
                except Exception:
                    pass

        _pv = _logging.getLogger("pywebview")
        _pv.setLevel(_logging.DEBUG)
        _pv.handlers = [_LogSink()]
        _pv.propagate = False

        # On macOS the bundled stub's Python sees sys.argv[0] as a *relative*
        # build path, and pywebview's app-root heuristic resolves it against the
        # current working directory — so from most launch paths (double-click,
        # `open`) that path doesn't exist and the embedded window dies into the
        # browser fallback. Pinning RESOURCEPATH gives pywebview a stable absolute
        # app root no matter how (or where) this process was started.
        if sys.platform == "darwin":
            if bundle := _app_bundle():
                os.environ["RESOURCEPATH"] = str(bundle / "Contents" / "Resources")
        import webview
        # min_size: the UI's sidebar collapses to an icon rail below 860px
        # (a layout we don't care about), so keep the window out of it - and
        # give the page area a usable floor. Applied to the NSWindow on macOS
        # (pywebview 6.x); note the Windows driver ignores min_size.
        window = webview.create_window(
            "SCM Workbench", url,
            width=1280, height=860, min_size=(900, 640), text_select=True)

        def pick_file():
            """Native OS file chooser (single file) for the UI's "Browse…"
            buttons. Returns the chosen path, or None when cancelled. No
            type restriction — the app decides what a file is for."""
            try:
                res = window.create_file_dialog(webview.FileDialog.OPEN, "", False, "", ())
            except Exception:
                return None
            return res[0] if res else None

        def pick_save(filename: str = ""):
            """Native OS save panel (single file) for “Move to my files…”.
            Returns the chosen destination path, or None when cancelled."""
            try:
                res = window.create_file_dialog(webview.FileDialog.SAVE, "", False, filename or "", ())
            except Exception:
                return None
            return res[0] if res else None

        window.expose(pick_file)
        window.expose(pick_save)
        webview.start()
        log("[launcher] window closed — stopping the UI server, bye.")
        _stop_server(log, data)
        return
    except Exception as e:
        import traceback as _tb
        detail = "".join(_tb.format_exception(type(e), e, e.__traceback__)).strip()
        reason = f"embedded window unavailable ({e})"
        if dotnet_note and dotnet_note != "ok":
            reason += f"; {dotnet_note}"
        try:
            # the full traceback (with the .NET hresult, e.g. 0x80008093) is
            # what actually diagnoses the failure - keep it, never truncate it
            (data / "window-error.log").write_text(detail, encoding="utf-8")
            # the UI (in the browser) explains itself on the dashboard card
            (data / "window.json").write_text(
                json.dumps({"mode": "browser", "reason": str(e), "detail": detail[:2000]}, indent=2),
                encoding="utf-8")
        except Exception:
            pass
        log(f"[launcher] {reason} — opening your browser instead "
            f"(full error in {data / 'window-error.log'}).")
    try:
        if settings.get("auto_open_browser", True):
            webbrowser.open(url, new=2)
        else:
            print(f"\n  UI:  {url}\n  Local only — not exposed to your network. Ctrl+C to stop.\n")
        for _ in range(3600):
            if _ui_child is not None and _ui_child.poll() is not None:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        _stop_server(log, data)


if __name__ == "__main__":
    main()
