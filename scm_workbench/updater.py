#!/usr/bin/env python3
"""
updater.py — check for, and install, newer versions of the SCM Workbench app.

The app is packaged with Briefcase and shipped as a GitHub *release* zip
(macOS: "SCM Workbench.app", Windows: "SCM Workbench.exe" + src/). This
module talks to the releases of the Workbench's own repository:

  * fetch the newest release (the release repo is private, so the check
    can't see it until the repo is made public - no credentials anywhere
    in the meantime),
  * compare it with the running version,
  * on "update available" an in-process job downloads the right platform's
    zip, extracts it, swaps it over the current app folder, relaunches the
    new one and quits the old one.

The swap only touches the *app* folder; the data area (settings, job
history, managed repo copies, the private runtime) lives elsewhere and is
never part of the swap, so an update can never lose user data.

Standard library only — same rule as the rest of the Workbench.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

USER_AGENT = "scm-workbench-updater/0.1"
# overridable for tests (point the checker at a stub API)
API = os.environ.get("SCM_WORKBENCH_GITHUB_API") or "https://api.github.com"

# The repo this app's releases live in. Overridable for tests and for people
# running a fork (point it at the fork and the checker follows it).
UPDATE_REPO = os.environ.get("SCM_WORKBENCH_UPDATE_REPO") or "mallen86/scm-workbench"

APP_NAME = "SCM Workbench"


class UpdateError(Exception):
    """A check/install problem with a user-showable message."""


class AuthRequiredError(UpdateError):
    """The release repo can't be seen anonymously (it is still private -
    making it public is all that's needed)."""


# ----------------------------------------------------------------------------
# Versions
# ----------------------------------------------------------------------------

_VER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.\-]+))?$")


def parse_version(s) -> tuple:
    """'v0.1.0' / '1.2' / '2.0.0-rc1' -> a comparable tuple (None if unparseable).

    A prerelease suffix sorts *before* its final release, so 0.2.0-rc1 is
    offered for 0.1.0 but 0.2.0-rc1 is not an update over 0.2.0."""
    m = _VER_RE.match(str(s or "").strip())
    if not m:
        return None
    nums = tuple(int(m.group(i) or 0) for i in (1, 2, 3))
    return nums + ((-1 if m.group(4) else 0),)


def is_newer(latest, current) -> bool:
    try:
        a, b = parse_version(latest), parse_version(current)
    except Exception:
        return False
    if not a or not b:
        return False
    return a > b


# ----------------------------------------------------------------------------
# GitHub over plain HTTPS
# ----------------------------------------------------------------------------

def gh_request(path: str, method: str = "GET", timeout: int = 30,
              stream_to=None, progress=None):
    """One API/download request. Returns (status, headers, body_bytes).

    stream_to: write the response body to this file instead of reading it
    all into memory (big asset downloads); progress gets (done, total).
    """
    url = API + path if path.startswith("/") else path
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, e.headers, b""
    except Exception as e:
        raise UpdateError(f"could not reach GitHub ({urllib.parse.urlsplit(url).netloc}): {e}")
    try:
        if stream_to is not None:
            total = int(r.headers.get("Content-Length") or 0)
            if total == 0:
                # chunked download routes: a HEAD usually still carries the size
                try:
                    hr = urllib.request.urlopen(urllib.request.Request(url, method="HEAD",
                                                                         headers=headers), timeout=15)
                    total = int(hr.headers.get("Content-Length") or 0)
                    hr.close()
                except Exception:
                    total = 0
            done = 0
            if progress is not None:
                progress(0, total)
            with open(stream_to, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    if progress is not None:
                        progress(done, total)
            return r.status, r.headers, None
        data = r.read()
        return r.status, r.headers, data
    finally:
        r.close()


def latest_release(timeout: int = 25) -> dict:
    """The newest release of the Workbench repo, as a plain dict.

    Raises AuthRequiredError when the release repo can't be seen anonymously
    (it is still private) - once it is made public, the same call just works.
    """
    status, headers, body = gh_request(f"/repos/{UPDATE_REPO}/releases/latest", timeout=timeout)
    if status == 200:
        rel = json.loads(body.decode("utf-8"))
        if not rel.get("tag_name"):
            raise UpdateError("GitHub answered, but the release data is empty.")
        assets = [{
            "name": a.get("name") or "",
            "url": a.get("browser_download_url") or "",
            "size": a.get("size") or 0,
        } for a in (rel.get("assets") or [])]
        return {
            "tag": rel["tag_name"],
            "name": rel.get("name") or rel["tag_name"],
            "body": rel.get("body") or "",
            "published": rel.get("published_at") or "",
            "url": rel.get("html_url") or "",
            "assets": assets,
        }
    if status == 404:
        raise AuthRequiredError(
            "the release repo can't be seen — it is still private; "
            "making it public is all that's needed for checks to work")
    if status in (403, 429):
        raise UpdateError("GitHub rate-limited the check — try again in a few minutes")
    raise UpdateError(f"GitHub API error {status} on the releases lookup")


def pick_asset(release: dict, platform: str = None) -> dict:
    """The zip for this platform among a release's assets ("" / None if absent)."""
    platform = platform or (sys.platform if os.name != "nt" else "win32")
    want = re.compile(r"macos|darwin" if platform == "darwin" else r"windows" if platform == "win32" else r"")
    zips = [a for a in release.get("assets", []) if a["url"] and a["url"].lower().endswith(".zip")]
    for a in zips:
        if not want or want.search(a["name"].lower()):
            return a
    if zips and not want:
        return zips[0]
    names = ", ".join(a["name"] for a in release.get("assets", [])) or "none"
    raise UpdateError(f"the release has no {platform} zip to install (assets: {names})")


# ----------------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------------

def download(url: str, dest: Path, progress=None, timeout: int = 60) -> int:
    """Stream a release asset to dest (via dest.part), returning its size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            raise UpdateError("the download can't see the asset — the release "
                               "repo is still private; once it is made public "
                               "this works")
        raise UpdateError(f"download failed with HTTP {e.code}")
    except Exception as e:
        raise UpdateError(f"download failed: {e}")
    try:
        total = int(r.headers.get("Content-Length") or 0)
        if total == 0:
            try:
                hr = urllib.request.urlopen(urllib.request.Request(url, method="HEAD",
                                                                     headers=headers), timeout=15)
                total = int(hr.headers.get("Content-Length") or 0)
                hr.close()
            except Exception:
                total = 0
        done = 0
        if progress is not None:
            progress(0, total)
        with open(part, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if progress is not None:
                    progress(done, total)
        r.close()
        os.replace(str(part), str(dest))
        return done
    except BaseException:
        try:
            r.close()
        except Exception:
            pass
        part.unlink(missing_ok=True)
        raise


def extract_app(zip_path: Path, dest_dir: Path, log=print) -> Path:
    """Unzip a release into dest_dir and return the app folder it contains.

    macOS releases hold "SCM Workbench.app"; Windows releases hold
    "SCM Workbench.exe" (+ src/) directly at the archive top level — the
    returned path is the folder to swap in place of the current one.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        # path-traversal guard, same policy as the repo sync
        for m in zf.namelist():
            if m.startswith("/") or ".." in m.split("/"):
                raise UpdateError(f"the release archive has an unsafe entry ({m}) — not installing it")
        zf.extractall(dest_dir)

    top = sorted(p for p in dest_dir.iterdir() if not p.name.startswith("."))
    # 1) the normal shape: the bundle itself at the archive top level
    for p in top:
        if p.is_dir() and p.name.endswith(".app"):
            if not (p / "Contents" / "MacOS").is_dir():
                raise UpdateError(f"the archive’s “{p.name}” is not a macOS app bundle")
            return p
    for p in top:
        if p.is_file() and p.suffix.lower() == ".exe":
            if not (dest_dir / "src").is_dir():
                raise UpdateError(f"the archive’s “{p.name}” is missing its src/ runtime — not installing it")
            return dest_dir
    # 2) a flat archive (GitHub-tarball style: the bundle's contents at top level)
    if (dest_dir / "Contents" / "MacOS").is_dir():
        return dest_dir
    raise UpdateError("could not find the app inside the release archive")


def swap_bundle(new_bundle: Path, old_bundle: Path, log=print) -> Path:
    """Replace old_bundle with new_bundle (same parent), keeping the old one
    as a `.old-<stamp>` sibling until the next launch sweeps it. Returns the
    backup path. Rolls the move back if the second one fails."""
    parent = old_bundle.parent
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = parent / (old_bundle.name + f".old-{stamp}")
    os.replace(str(old_bundle), str(backup))
    try:
        shutil.move(str(new_bundle), str(old_bundle))
    except BaseException:
        try:
            os.replace(str(backup), str(old_bundle))
        except Exception:
            log(f"    ! could not roll the swap back — the old app is at {backup}")
        raise
    return backup


def _ancestors_to_stop() -> list:
    """The window-hosting process(es) above us — on both platforms the chain
    is exactly two deep: the bundle's launcher runs the UI *in* the stub/exe
    process, and the server (us) is the child it spawned. Killing the parent
    closes the window; we stay up to finish the install."""
    p = os.getppid()
    return [] if p <= 1 else [p]


def stop_ancestors(log=print) -> None:
    """Stop the window host (never the data — that lives in the data area)."""
    for p in _ancestors_to_stop():
        try:
            if os.name == "nt":
                # taskkill is a console app: CREATE_NO_WINDOW, no flash
                subprocess.Popen(["taskkill", "/F", "/PID", str(p)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=0x08000000)
            else:
                os.kill(p, 15)  # SIGTERM — the window host quits cleanly
            log(f"    (stopped the window host, pid {p})")
        except Exception:
            pass
    time.sleep(1.2)


def relaunch_detached(bundle: Path, log=print, delay: float = 2.5) -> None:
    """(Re)start the app a moment after this process has gone — a detached
    one-liner is the parent-agnostic way: the new instance wants the port
    the old one (us) still holds, so the relaunch must land after our exit."""
    if sys.platform == "darwin":
        cmd = ["/bin/sh", "-c", f"sleep {delay:.0f} && open '{bundle}'"]
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        exe = next((p for p in bundle.iterdir() if p.suffix.lower() == ".exe"), None)
        target = exe or bundle
        cmd = ["cmd", "/c", f"timeout /t {int(delay)} >nul && start '' \"{target}\""]
        DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, creationflags=DETACHED,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(bundle.parent if target == bundle else target.parent))
    else:
        raise UpdateError("automatic relaunch is only supported on the packaged macOS/Windows apps")


def clean_old_bundles(bundle: Path, log=print) -> None:
    """Drop `.old-<stamp>` backup bundles left by previous updates (called at
    launch: by then the new app is provably healthy enough to start)."""
    try:
        parent = bundle.parent
        for old in parent.glob(bundle.name + ".old-*"):
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
                log(f"(removed the previous version kept at {old.name})")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# The install job (run in a server thread; `job` is the server's job dict)
# ----------------------------------------------------------------------------

def run_job(job: dict, plan: dict, log_f) -> None:
    """Download + install a newer release, then hand over to the new app.

    plan keys: repo, current, latest, asset {name,url,size},
    bundle (the app folder to replace, None when not packaged), work (the
    scratch dir), force (allow same-version reinstalls).
    """
    def emit(s: str) -> None:
        job["log_lines"].append(s)
        log_f.write(s + "\n")
        log_f.flush()
        for q in list(job["subs"]):
            try:
                q.put(("line", s))
            except Exception:
                pass

    def fail(msg: str) -> None:
        emit(f"    ! {msg}")
        job["status"] = "fail"
        job["exit_code"] = 1
        job["ended"] = time.time()
        job["duration"] = round(job["ended"] - job["started"], 2)
        for q in list(job["subs"]):
            try:
                q.put(("done", "fail", 1))
            except Exception:
                pass

    def finish(ok: bool, final: str) -> None:
        emit(final)
        job["status"] = "ok" if ok else "fail"
        job["exit_code"] = 0 if ok else 1
        job["ended"] = time.time()
        job["duration"] = round(job["ended"] - job["started"], 2)
        for q in list(job["subs"]):
            try:
                q.put(("done", job["status"], job["exit_code"]))
            except Exception:
                pass

    try:
        emit(f"Update to {plan.get('latest') or 'the latest release'} — repo {plan.get('repo')}")
        # 1) re-verify (the state that started the job can be a few minutes old)
        rel = latest_release()
        if not is_newer(rel["tag"], plan.get("current")) and not plan.get("force"):
            finish(True, f"Nothing to do — v{plan.get('current')} is still the latest release ({rel['tag']}).")
            return
        # 2) the right zip
        try:
            asset = pick_asset({"assets": rel["assets"]})
        except UpdateError:
            asset = plan.get("asset")
        if not asset:
            fail("no installable zip is attached to the newest release")
            return
        size_mb = asset.get("size", 0) / 1e6
        emit(f"Downloading {asset['name']} ({size_mb:.0f} MB) from the release …")
        # 3) download (progress line, throttled to ~1/s)
        work = Path(plan["work"]); work.mkdir(parents=True, exist_ok=True)
        dest = work / asset["name"]
        last = {"t": 0.0}

        def progress(done: int, total: int) -> None:
            if total and (time.time() - last["t"]) > 1.0:
                last["t"] = time.time()
                job["progress"] = {"stage": "download", "done": done, "total": total}
                emit(f"    ↓ {done / 1e6:.1f} / {total / 1e6:.1f} MB")
        download(asset["url"], dest, progress=progress)
        emit(f"    downloaded {dest.stat().st_size / 1e6:.1f} MB")
        # 4) extract + verify
        job["progress"] = {"stage": "extract", "done": 1, "total": 1}
        emit("Extracting the new app …")
        new_bundle = extract_app(dest, work / "staging", log=emit)
        emit(f"    ready: {new_bundle}")
        # 5) no bundle of our own (a dev run) — hand the files over instead
        bundle = plan.get("bundle")
        if not bundle:
            finish(True,
                   "Done — the new build is unpacked at\n    "
                   f"    {new_bundle}\n"
                   "    This server has no app folder to swap (running from a source\n"
                   "    checkout), so move/copy it over your existing install manually.")
            return
        old_bundle = Path(bundle)
        if not old_bundle.exists():
            fail(f"the current app folder is gone ({old_bundle}) — not swapping")
            return
        # 6) the swap. The data area is a sibling of all this, never inside it.
        emit(f"Installing over {old_bundle} — the app will close and reopen by itself in a few seconds.\n"
             "    (Your data folder is not part of the app folder and stays as-is.)")
        backup = swap_bundle(new_bundle, old_bundle, log=emit)
        emit(f"    swapped — the previous version is kept at “{backup.name}” until the next launch")
        # 7) relaunch the new one (detached, lands after we exit), then quit the
        #    window host and ourselves — in that order on purpose.
        relaunch_detached(old_bundle, log=emit)
        stop_ancestors(log=emit)
        finish(True, f"New version is starting. If the window doesn't reopen within ~10 s, "
                     f"launch {old_bundle} yourself — everything is already in place.")
    except UpdateError as e:
        fail(str(e))
    except Exception as e:
        import traceback
        emit("    " + traceback.format_exc(limit=3).replace("\n", "\n    "))
        fail(f"the update failed: {e}")
