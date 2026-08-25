#!/usr/bin/env python3
"""
repo_sync.py — keep the Workbench's *managed copies* of the sister repos current.

The Workbench can own its own copies of silhouette-card-maker and scm-extras
(living in the Workbench's data area). This module is how those copies are
created and updated:

  * GitHub's plain-HTTPS endpoints only (api.github.com + raw.githubusercontent.com)
    — no git binary, no credentials, nothing installed on the host machine.
  * "check" is metadata-only (a couple of small API calls); "update" fetches
    ONLY the files that changed between the deployed commit and the target
    (GitHub compare API + per-file raw fetch). A full tarball swap is the
    fallback when the diff is too large or the API misbehaves.
  * User-generated files (card art, decklists, printed PDFs, offsets, …) are
    all *untracked* in the repos, so an update never deletes or overwrites
    them. Locally-edited *tracked* files are reconciled against a stored hash
    manifest (which always records the PRISTINE upstream hash per path):
      - upstream-only change   -> new content applied
      - user-only change       -> local content kept
      - changed on both sides  -> local content KEPT, with a warning

Run as a module from the Workbench server (fast actions: check / refs / save)
or as a CLI (long actions, streamed into a Workbench job console: update / init):

    python -m scm_workbench.repo_sync init   --repo scm   [--tarball path/to/local.tar.gz]
    python -m scm_workbench.repo_sync update --repo scm   [--force-full]
    python -m scm_workbench.repo_sync check  --repo scm   [--json]
    python -m scm_workbench.repo_sync refs   --repo scm

Exit codes: 0 ok, 1 failure (message on stdout, or {"error": ...} with --json).
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "scm-workbench/1.1"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DIFF_FILE_CAP = 300          # GitHub's compare API returns at most this many files

REPOS = {
    "scm": {
        "owner": "Alan-Cha", "repo": "silhouette-card-maker",
        "name": "silhouette-card-maker", "rel": "repos/silhouette-card-maker",
        "requirements": "requirements.txt",
        # default to the latest *release* — unreleased main can carry breaking
        # changes between releases (scm-extras publishes no releases, so it
        # defaults to its main branch)
        "default_source": "latest-release",
    },
    "extras": {
        "owner": "Alan-Cha", "repo": "scm-extras",
        "name": "scm-extras", "rel": "repos/scm-extras",
        "requirements": None,
        "default_source": "main",
    },
}


class RepoError(Exception):
    pass


# ----------------------------------------------------------------------------
# Locations & state
# ----------------------------------------------------------------------------

def data_dir() -> Path:
    p = os.environ.get("SCM_WORKBENCH_DATA")
    if p:
        return Path(p).expanduser().resolve()
    # Dev checkout: the data area lives at the repo root (one level up from
    # this package). Inside an app bundle the launcher always sets the env var.
    return Path(__file__).resolve().parent.parent / "data"


def state_file() -> Path:
    return data_dir() / "repos-state.json"


def manifest_file(key: str) -> Path:
    return data_dir() / f"repos-manifest-{key}.json"


def load_state() -> dict:
    try:
        with open(state_file()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    # atomic: write a sibling temp file and rename over the real one, so a
    # crash (or a second process) can never leave a half-written state file
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, f)


@contextlib.contextmanager
def _state_lock():
    """Process-wide (and cross-process) mutex around state read-modify-write
    sections. The launcher and the UI server are separate processes that both
    write repos-state.json; without this, two writers can interleave and one
    loses the other's changes — which is exactly how a copy could lose its
    'deployed' record and get silently re-inited next launch."""
    lock_path = data_dir() / ".repos-lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write(" ")
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


# ----------------------------------------------------------------------------
# Live progress (the UI polls this through /api/info while a clone/update runs)
# progress.json: {"scm": {"stage": "download", "done": 123456, "total": 590000000}, …}
# ----------------------------------------------------------------------------

def progress_file() -> Path:
    return data_dir() / "progress.json"


def _read_progress() -> dict:
    try:
        with open(progress_file()) as f:
            return json.load(f)
    except Exception:
        return {}


def load_progress() -> dict:
    return _read_progress()


# Per-process rate state for the progress rows: speed/ETA are computed on
# every callback tick, even for ticks whose file write is throttled.
_prog_rate = {}


def _rate_for(key: str, done: int, total: int, stage):
    """Exponential-moving-average speed (bytes/s) + ETA (s) for one tick."""
    st = _prog_rate.setdefault(key, {})
    if stage is not None:
        st.clear()
    now = time.time()
    speed = st.get("speed")
    last_ts, last_done = st.get("ts"), st.get("done")
    if last_ts is not None and last_done is not None and now > last_ts and done > last_done:
        inst = (done - last_done) / (now - last_ts)
        if speed is not None:
            inst = 0.6 * inst + 0.4 * speed
        speed = int(inst)
    st["ts"], st["done"] = now, done
    if speed is not None:
        st["speed"] = speed
    eta = None
    if speed and total and total >= 1000 and total > done:
        eta = max(1, int((total - done) / speed))
    return speed, eta


def set_progress(key: str, **kw) -> None:
    """Record progress for one repo. Writes are throttled: while a download
    streams, a cb tick per 1 MB chunk only rewrites the file every ~2% of
    progress (or on a stage change), so the UI stays smooth, not chatty."""
    try:
        d = _read_progress()
        cur = d.get(key) or {}
        stage = kw.get("stage", cur.get("stage"))
        row = {**cur, **kw}
        if kw.get("done") is not None:
            speed, eta = _rate_for(key, int(kw["done"]), int(row.get("total") or 0), kw.get("stage"))
            if speed:
                row["speed"] = speed
            if eta is not None:
                row["eta"] = eta
        if kw.get("done") is not None and cur.get("total") and stage == cur.get("stage"):
            span = max(1, int(cur["total"] * 0.02))
            if abs(int(kw["done"]) - int(cur.get("done") or 0)) < span:
                return
        d[key] = row
        progress_file().write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def clear_progress(key: str) -> None:
    try:
        _prog_rate.pop(key, None)
        d = _read_progress()
        if key in d:
            del d[key]
            progress_file().write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def repo_dir(key: str) -> Path:
    return data_dir() / REPOS[key]["rel"]


def load_manifest(key: str) -> dict:
    try:
        with open(manifest_file(key)) as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(key: str, man: dict) -> None:
    with open(manifest_file(key), "w", encoding="utf-8") as f:
        json.dump(man, f)


def load_source(key: str) -> str:
    """Which ref the user asked for: 'main' | 'latest-release' | a tag/sha (pinned)."""
    st = load_state().get(key) or {}
    if st.get("source"):
        return str(st["source"])
    settings_file = data_dir() / "settings.json"
    try:
        settings = json.loads(settings_file.read_text())
        return str((settings.get("repos", {}).get(key) or {}).get("source")
                   or REPOS[key].get("default_source", "main"))
    except Exception:
        return REPOS[key].get("default_source", "main")


def set_source(key: str, source: str) -> None:
    with _state_lock():
        st = load_state()
        r = st.setdefault(key, {})
        r["source"] = source
        save_state(st)


# ----------------------------------------------------------------------------
# GitHub over plain HTTPS
# ----------------------------------------------------------------------------

def gh_json(path: str, params: dict = None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RepoError("GitHub API rate limit or permission error — try again shortly.")
        if e.code == 404:
            return None
        raise RepoError(f"GitHub API error {e.code} for {path}")
    except Exception as e:
        raise RepoError(f"could not reach GitHub ({path}): {e}")


def gh_get_bytes(url: str, timeout: int = 120, progress_cb=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except Exception as e:
        raise RepoError(f"download failed: {e}")
    try:
        if progress_cb is None:
            return r.read()
        total = int(r.headers.get("Content-Length") or 0)
        if total == 0:
            # Some of GitHub's download routes stream chunked (no
            # Content-Length) — a HEAD against the same URL usually still
            # carries the size, so ask for it before we lose the chance.
            try:
                hr = urllib.request.urlopen(
                    urllib.request.Request(url, method="HEAD",
                                           headers={"User-Agent": USER_AGENT}),
                    timeout=15)
                total = int(hr.headers.get("Content-Length") or 0)
                hr.close()
            except Exception:
                total = 0
        chunks, done = [], 0
        progress_cb(0, total)
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            chunks.append(b)
            done += len(b)
            progress_cb(done, total)
        return b"".join(chunks)
    finally:
        r.close()


def repo_api(key: str) -> dict:
    return gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}") or {}


def commit_info(key: str, ref: str):
    d = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/commits/{urllib.parse.quote(ref, safe='')}")
    if not d or not d.get("sha"):
        return None
    c = d.get("commit") or {}
    return {
        "sha": d["sha"],
        "ref": ref,
        "date": (c.get("committer") or {}).get("date") or (c.get("author") or {}).get("date"),
        "message": ((c.get("message") or "").splitlines() or [""])[0][:100],
    }


def _source_is_builtin_default(key: str) -> bool:
    """True when no user-chosen source is on record (state or settings)."""
    if (load_state().get(key) or {}).get("source"):
        return False
    try:
        settings = json.loads((data_dir() / "settings.json").read_text())
    except Exception:
        return True
    return not (settings.get("repos", {}).get(key) or {}).get("source")


def resolve_target(key: str, source: str) -> dict:
    """Turn the user's source choice into a concrete {sha, ref, date}."""
    if source == "latest-release":
        rel = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/releases/latest")
        if rel and rel.get("tag_name"):
            tag = rel["tag_name"]
            info = commit_info(key, f"refs/tags/{tag}") or commit_info(key, tag)
            if info:
                info["ref"] = tag
                info["release_name"] = rel.get("name")
                return info
        if _source_is_builtin_default(key):
            # built-in default but no releases published (yet) — track the
            # default branch instead so a first launch never dead-ends
            source = "main"
        else:
            raise RepoError(f"no GitHub releases are published for {REPOS[key]['name']} yet — use “Latest (main)” or a pinned ref.")
    ref = source if source != "main" else (repo_api(key).get("default_branch") or "main")
    info = commit_info(key, ref)
    if not info:
        raise RepoError(f"could not resolve ref “{ref}” for {REPOS[key]['name']}.")
    return info


def list_refs(key: str) -> dict:
    meta = {
        "default_branch": repo_api(key).get("default_branch") or "main",
        "tags": [], "releases": [],
    }
    tags = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/tags", {"per_page": 100}) or []
    for t in tags:
        meta["tags"].append({"name": t.get("name"), "sha": (t.get("commit") or {}).get("sha")})
    rels = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/releases", {"per_page": 30}) or []
    for r in rels:
        meta["releases"].append({
            "tag": r.get("tag_name"), "name": r.get("name"),
            "date": r.get("published_at"), "prerelease": bool(r.get("prerelease")),
        })
    return meta


def compare(key: str, base_sha: str, head_sha: str) -> dict:
    o, r = REPOS[key]["owner"], REPOS[key]["repo"]
    d = gh_json(f"/repos/{o}/{r}/compare/{base_sha}...{head_sha}")
    if not d:
        raise RepoError("compare API returned nothing — falling back to a full sync.")
    files = [{
        "path": f.get("filename"),
        "status": f.get("status"),
        "previous": f.get("previous_filename"),
    } for f in d.get("files", [])]
    return {"files": files, "too_many": len(files) >= DIFF_FILE_CAP,
            "commits": d.get("total_commits", 0), "status": d.get("status")}


def download_to(key: str, sha: str, path: str, dest: Path, log=print) -> int:
    data = gh_get_bytes(f"{RAW}/{REPOS[key]['owner']}/{REPOS[key]['repo']}/{sha}/{urllib.parse.quote(path)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    n = len(data)
    if n >= 1024 * 1024:
        log(f"    ↓ {path} ({n / 1e6:.1f} MB)")
    elif n >= 10 * 1024:
        log(f"    ↓ {path} ({n // 1024} KB)")
    return n


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_members(members: list):
    """Strip the {owner}-{repo}-{sha}/ wrapper so the tree extracts flat. Returns the wrapper's name."""
    root = None
    for m in members:
        if root is None:
            root = m.name.split("/", 1)[0] + "/"
            continue
        if not m.name.startswith(root):
            raise RepoError(f"unexpected tarball layout: {m.name}")
        rel = m.name[len(root):]
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            raise RepoError(f"unsafe path in tarball: {m.name}")
        m.name = rel  # strip the {owner}-{repo}-{sha}/ prefix
    return root


def extract_tarball(tar_path: Path, dest: Path, log=print):
    """Extract a GitHub tarball into dest (flat tree, wrapper dir dropped)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(exist_ok=True)
    with tarfile.open(tar_path, "r:*") as tf:
        members = tf.getmembers()
        safe_members(members)
        # drop the (now-empty) wrapper dir — only if it really is a directory entry
        if members and (members[0].isdir() or members[0].name.endswith("/")):
            members = members[1:]
        tf.extractall(dest, members=members, filter="data")


def tracked_paths(tree_dir: Path) -> list:
    out = []
    for p in sorted(tree_dir.rglob("*")):
        if p.is_file():
            out.append(str(p.relative_to(tree_dir)))
    return out


# ----------------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------------
# manifest[path] is always the PRISTINE (upstream) sha256 of that path at the
# currently deployed commit.  local hash != manifest hash  ==  "user-edit".
#
#   apply:  local pristine  -> replace with new pristine
#           local user-edited, new pristine == local -> no-op
#           local user-edited, new pristine differs   -> KEEP local, warn
#           local missing                             -> place new pristine
#   delete: local pristine  -> remove
#           local user-edited -> KEEP local, warn (it becomes the user's file)

def _decide(local: Path, old_hash, new_pristine: Path):
    if not local.exists():
        return "place"
    new_hash = sha256_file(new_pristine) if new_pristine is not None else None
    cur = sha256_file(local)
    if new_hash is not None and cur == new_hash:
        return "noop"
    if old_hash is not None and cur == old_hash:
        return "replace"
    # local differs from the recorded pristine (a user edit):
    if new_hash is not None and old_hash is not None and old_hash == new_hash:
        return "keep-user"      # upstream didn't change it -> keep silently
    return "keep-conflict"       # changed on both sides


def apply_changes(key: str, man: dict, target: dict,
                  apply_ops: dict, delete_paths: list,
                  pristine_for, log=print) -> dict:
    """apply_ops: {path: prev_path_or_None} ; pristine_for(path)->Path|None resolves
    the new pristine content (a staging file in full mode, a temp download in diff)."""
    repo = repo_dir(key)
    new_manifest = dict(man.get("files") or {})
    old_files = man.get("files") or {}
    applied = replaced = deleted = 0
    conflicts = []

    for path, prev in apply_ops.items():
        if prev and prev != path:
            # upstream rename: drop the old local slot when it's still pristine,
            # warn when the user edited it (their copy would otherwise strand silently)
            old_local = repo / prev
            if old_local.exists():
                if old_files.get(prev) and sha256_file(old_local) == old_files[prev]:
                    old_local.unlink()
                else:
                    conflicts.append(prev)
            new_manifest.pop(prev, None)
            old_files.pop(prev, None)
        pristine = pristine_for(path)
        local = repo / path
        d = _decide(local, old_files.get(path), pristine)
        if pristine is None:
            if d in ("place", "replace"):
                log(f"    ! skipped {path} (new content could not be fetched)")
            continue
        if d in ("place", "replace"):
            shutil.copy2(pristine, local)
            new_manifest[path] = sha256_file(local)
            if d == "replace":
                replaced += 1
            else:
                applied += 1
        elif d == "keep-conflict":
            conflicts.append(path)
            new_manifest[path] = sha256_file(pristine)
        else:  # noop or keep-user: local already matches (or is the user's own edit of)
            new_manifest[path] = sha256_file(pristine)

    for path in delete_paths:
        local = repo / path
        if not local.exists():
            new_manifest.pop(path, None)
            continue
        if old_files.get(path) and sha256_file(local) == old_files[path]:
            local.unlink()
            deleted += 1
        else:
            conflicts.append(path)
        new_manifest.pop(path, None)

    man2 = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date"),
            "files": new_manifest}
    return {"applied": applied + replaced, "deleted": deleted, "conflicts": conflicts, "manifest": man2}


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def check_repo(key: str, force: bool = False) -> dict:
    """Resolve the asked-for ref and compare it with the deployed one. Caches the
    remote answer for an hour (rate-limit friendly); force re-queries."""
    st = load_state().get(key) or {}
    deployed = st.get("deployed")
    lc = st.get("last_check") or {}
    if not force and lc.get("checked") and time.time() - lc.get("checked_at", 0) < 3600:
        return {"repo": key, "ok": True, "cached": True, "deployed": deployed,
                "target": lc["checked"].get("target"), "up_to_date": lc["checked"].get("up_to_date")}
    try:
        target = resolve_target(key, load_source(key))
    except RepoError as e:
        return {"repo": key, "ok": False, "error": str(e), "deployed": deployed}
    res = {"repo": key, "ok": True, "cached": False, "target": target, "deployed": deployed,
           "up_to_date": bool(deployed and deployed.get("sha") == target["sha"])}
    if not deployed:
        res["note"] = "no managed copy yet — run “Download latest” first."
    state = load_state()
    r = state.setdefault(key, {})
    r["last_check"] = {"checked": res, "checked_at": time.time()}
    save_state(state)
    return res


def cmd_check(key: str, as_json: bool = False):
    res = check_repo(key, force=True)
    if as_json:
        print(json.dumps(res))
        return res
    t, d = res.get("target"), res.get("deployed")
    if res.get("ok") and res["up_to_date"] and t:
        print(f"{REPOS[key]['name']}: up to date at {t['ref']} ({t['sha'][:7]}, {(t.get('date') or '?')[:10]})")
    elif res.get("ok") and d and t:
        print(f"{REPOS[key]['name']}: update available — {t['ref']} ({t['sha'][:7]}, {(t.get('date') or '?')[:10]}) "
              f"is newer than deployed {d.get('ref')} ({d['sha'][:7]})")
    elif res.get("ok"):
        print(f"{REPOS[key]['name']}: {res.get('note', 'no managed copy yet')}")
    else:
        print(f"check {key}: {res.get('error')}")
    return res


def cmd_refs(key: str, as_json: bool = False):
    meta = list_refs(key)
    if as_json:
        print(json.dumps(meta))
    else:
        print(f"{REPOS[key]['name']}: default branch “{meta['default_branch']}”, "
              f"{len(meta['tags'])} tag(s), {len(meta['releases'])} release(s)")
        for t in meta["tags"][:10]:
            print(f"  tag {t['name']} @ {t['sha'][:7] if t['sha'] else '?'}")
    return meta


_KNOWN_BAD_PINS = {
    # Upstream pins that were never published to PyPI (or yank-rot): rewrite
    # them to the newest resolvable release so the whole requirements file can
    # install atomically.
    ("filetype", "1.2.1"): "filetype==1.2.0",
    ("pypdfium2", "5.12.1"): "pypdfium2==5.9.0",
}


def apply_bad_pin_fixes(lines) -> list:
    """Rewrite the requirements pins that can never install as written.

    Shared by the user-facing first-boot dependency sync and the CI runtime
    bake (scripts/bake_runtime.py) so both apply exactly the same rewrites.
    """
    patched = []
    for line in lines:
        s = line.strip()
        if "==" in s and not s.startswith(("-", "#")):
            parts = s.split("==", 1)
            fix = _KNOWN_BAD_PINS.get((parts[0].strip().lower(), parts[1].strip()))
            if fix:
                tail = line.split("==", 1)[1]
                comment = "  # " + tail.split("#", 1)[1].strip() if "#" in tail else ""
                s = fix + comment
        patched.append(s)
    return patched


def _sync_deps(key: str, log=print) -> None:
    # Only when the app launcher says it's a real packaged app: then the
    # interpreter belongs to the app, so pip-ing into it is safe and keeps
    # the managed copy runnable. Never in a dev checkout — that would touch
    # the user's own Python environment.
    if not os.environ.get("SCM_WORKBENCH_PACKAGED"):
        return
    req = REPOS[key]["requirements"]
    repo = repo_dir(key)
    if not req or not (repo / req).is_file():
        return
    # The job scripts run on the provisioned relocatable runtime, so its
    # site-packages — not this process's interpreter — is the target.
    py = os.environ.get("SCM_WORKBENCH_PYTHON")
    interpreter = [py, "-m", "pip"] if py else [sys.executable, "-m", "pip"]
    src = (repo / req).read_text(encoding="utf-8", errors="replace")
    patched = apply_bad_pin_fixes(src.splitlines())
    work = repo / ".wb-requirements.txt"
    work.write_text("\n".join(patched) + "\n", encoding="utf-8")
    run_kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        # pip is a console app on Windows: CREATE_NO_WINDOW keeps the one-time
        # dependency sync from flashing a terminal (its output is captured anyway).
        r = subprocess.run([*interpreter, "install", "--disable-pip-version-check", "-q",
                            "-r", str(work)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", **run_kw)
    finally:
        work.unlink(missing_ok=True)
    if r.returncode == 0:
        log("  dependencies ok (installed into the app's private runtime)")
    else:
        tail = " ".join((r.stderr or r.stdout or "").strip().split())[-160:]
        hint = (" — " + tail) if tail else " — check the repo's requirements.txt pins"
        log(f"  ! dependency sync: some pinned requirements could not be installed (continuing){hint}")


# User data lives in these repo subfolders (decklists, fetched card images,
# generated output, calibration data). A full re-deploy must never lose it:
# copies are staged before the tree is replaced and put back afterwards.
# Upstream placeholder files (README/EMPTY) are not user data.
USER_DATA_PATHS = ("data", "game/front", "game/back", "game/double_sided",
                   "game/decklist", "game/output")
_PRISTINE_NAMES = {"README.md", "EMPTY.md"}


def stash_user_data(repo: Path, dest: Path, log=print) -> list:
    """Copy user files out of a tree that is about to be replaced.
    Returns [(relpath, staged_path), …]."""
    saved = []
    for rel in USER_DATA_PATHS:
        d = repo / rel
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.name in _PRISTINE_NAMES:
                continue
            sp = dest / str(f.relative_to(repo))
            sp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, sp)
            saved.append((str(f.relative_to(repo)), sp))
    if saved:
        log(f"[repos] staged {len(saved)} user file(s) from the old tree "
            f"(decklists/images/output/offsets) — they will be restored after the re-deploy")
    return saved


def restore_user_data(saved: list, repo: Path, log=print) -> None:
    """Put staged user files back into (a freshly replaced) tree. User data
    wins over any same-named upstream file."""
    for rel, sp in saved:
        rp = repo / rel
        rp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, rp)
    if saved:
        log(f"[repos] restored {len(saved)} user file(s) into the new tree")


def verify_deployed(key: str) -> bool:
    """Cheap offline spot check: does the deployed tree still match the
    recorded state (same sha in state/manifest, probed file hashes intact)?
    The launcher uses this to decide when a managed copy needs a safe
    re-deploy; the update flow itself repairs drift, so this mostly catches
    interrupted work."""
    st = load_state().get(key) or {}
    deployed = st.get("deployed") or {}
    if not deployed.get("sha"):
        return False
    man = load_manifest(key)
    files = man.get("files") or {}
    if not files:
        return False
    if man.get("sha") and man["sha"] != deployed["sha"]:
        return False
    repo = repo_dir(key)
    probe = sorted(files)[0]
    p = repo / probe
    if not p.is_file():
        return False
    try:
        return sha256_file(p) == files[probe]
    except Exception:
        return False


def cmd_init(key: str, tarball: str = None, log=print, force_redeploy: bool = False):
    meta = REPOS[key]
    st = load_state()
    rstate = st.get(key) or {}
    source = load_source(key)
    if rstate.get("deployed") and not tarball and not force_redeploy:
        log(f"[init {key}] already deployed at {rstate['deployed']['ref']} — nothing to do.")
        return {"ok": True, "noop": True}
    log(f"[init {key}] resolving target “{source}” …")
    target = resolve_target(key, source)
    log(f"[init {key}] target: {target['ref']} @ {target['sha'][:7]}")
    repo = repo_dir(key)
    stash_dir = data_dir() / f".repos-stash-{key}-{int(time.time())}"
    saved = []
    try:
        if repo.exists():
            log(f"[init {key}] replacing existing copy at {repo}")
            saved = stash_user_data(repo, stash_dir, log)
            shutil.rmtree(repo, ignore_errors=True)
        if tarball:
            tp = Path(tarball)
            log(f"[init {key}] extracting local tarball {tp.name} ({tp.stat().st_size / 1e6:.0f} MB) …")
            extract_tarball(tp, repo, log)
        else:
            tmp = data_dir() / f".repos-download-{key}.tar.gz"
            set_progress(key, stage="download", done=0, total=0, label="full snapshot")
            log(f"[init {key}] downloading full snapshot from GitHub …")
            data = gh_get_bytes(f"{API}/repos/{meta['owner']}/{meta['repo']}/tarball/{target['sha']}",
                                  timeout=1800, progress_cb=lambda d, t: set_progress(key, done=d, total=t))
            tmp.write_bytes(data)
            log(f"[init {key}] {len(data) / 1e6:.0f} MB received — extracting to {repo} …")
            set_progress(key, stage="extract", done=0, total=0)
            extract_tarball(tmp, repo, log)
            tmp.unlink(missing_ok=True)
            set_progress(key, stage="fingerprint", done=0, total=0)
        restore_user_data(saved, repo, log)
        man = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date"), "files": {}}
        for p in tracked_paths(repo):
            man["files"][p] = sha256_file(repo / p)
        save_manifest(key, man)
        with _state_lock():
            # reload inside the lock: another process (the UI server) may have
            # written state while we were downloading
            st = load_state()
            rstate = st.get(key) or {}
            rstate["deployed"] = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date")}
            rstate["mode"] = "bundled"
            rstate["source"] = source
            st[key] = rstate
            save_state(st)
        log(f"[init {key}] {REPOS[key]['name']} deployed at {target['ref']} ({target['sha'][:7]}) — "
            f"{len(man['files'])} tracked files fingerprinted")
        try:
            _sync_deps(key, log)
        except Exception as e:
            log(f"  ! dependency sync failed: {e}")
    finally:
        try:
            if stash_dir.exists():
                shutil.rmtree(stash_dir, ignore_errors=True)
        except Exception:
            pass
        clear_progress(key)
    return {"ok": True, "files": len(man["files"])}


def cmd_update(key: str, force_full: bool = False, log=print):
    meta = REPOS[key]
    st = load_state()
    rstate = st.get(key) or {}
    deployed = rstate.get("deployed")
    source = load_source(key)
    if not deployed:
        raise RepoError("no managed copy of this repo yet — run “Download latest” (init) first.")
    log(f"[update {key}] resolving target “{source}” …")
    target = resolve_target(key, source)
    log(f"[update {key}] target: {target['ref']} @ {target['sha'][:7]} ({(target.get('date') or '?')[:10]})")
    if deployed["sha"] == target["sha"]:
        log(f"[update {key}] already at {target['ref']} ({target['sha'][:7]}) — nothing to do.")
        return {"ok": True, "noop": True}

    return _run_update(key, meta, st, rstate, deployed, source, target, force_full, log)

def _run_update(key, meta, st, rstate, deployed, source, target, force_full, log):
    t0 = time.time()
    try:
        man = load_manifest(key)
        old_files = man.get("files") or {}
        repo = repo_dir(key)
        mode = "full" if force_full else "diff"

        if mode == "diff":
            try:
                cmp = compare(key, deployed["sha"], target["sha"])
                if cmp["status"] != "ahead":
                    # the target is older than (behind) or unrelated to (diverged) the deployed
                    # commit — GitHub's merge-base diff can't express that, so swap full snapshots
                    log(f"[update {key}] moving to an older/diverged ref — using full-tarball sync.")
                    mode = "full"
                elif cmp["too_many"]:
                    log(f"[update {key}] {DIFF_FILE_CAP}+ files changed — switching to full-tarball sync.")
                    mode = "full"
                else:
                    log(f"[update {key}] {len(cmp['files'])} file(s) changed across {cmp['commits']} commit(s)")
            except RepoError as e:
                log(f"[update {key}] diff unavailable ({e}) — switching to full-tarball sync.")
                mode = "full"

        if mode == "diff":
            staging = data_dir() / f".repos-diff-{key}-{int(time.time())}"
            staging.mkdir(parents=True, exist_ok=True)

            set_progress(key, stage="update", done=0, total=len(cmp["files"]))

            _count = [0]

            def pristine_for(path):
                """Fetch the new pristine content into staging; return its path (or None on failure)."""
                sp = staging / path
                try:
                    download_to(key, target["sha"], path, sp, log)
                    _count[0] += 1
                    set_progress(key, done=_count[0])
                    return sp
                except RepoError as e:
                    log(f"    ! could not fetch {path}: {e}")
                    return None

            apply_ops, delete_paths = {}, []
            for f in cmp["files"]:
                if f["status"] == "removed":
                    delete_paths.append(f["path"])
                else:
                    apply_ops[f["path"]] = f.get("previous")
            res = apply_changes(key, man, target, apply_ops, delete_paths, pristine_for, log)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            log(f"[update {key}] downloading full snapshot of {target['ref']} …")
            set_progress(key, stage="download", done=0, total=0, label="full snapshot")
            tmp = data_dir() / f".repos-download-{key}.tar.gz"
            data = gh_get_bytes(f"{API}/repos/{meta['owner']}/{meta['repo']}/tarball/{target['sha']}",
                                  timeout=1800, progress_cb=lambda d, t: set_progress(key, done=d, total=t))
            tmp.write_bytes(data)
            log(f"[update {key}] {len(data) / 1e6:.0f} MB received — extracting & reconciling …")
            staging = data_dir() / f".repos-staging-{key}-{int(time.time())}"
            set_progress(key, stage="extract", done=0, total=0)
            extract_tarball(tmp, staging, log)
            set_progress(key, stage="apply", done=0, total=0)
            new_tree = {p: staging / p for p in tracked_paths(staging)}
            apply_ops = {p: None for p in new_tree}
            delete_paths = [p for p in old_files if p not in new_tree]
            res = apply_changes(key, man, target, apply_ops, delete_paths,
                                 lambda p: new_tree.get(p), log)
            shutil.rmtree(staging, ignore_errors=True)
            tmp.unlink(missing_ok=True)

        # state write under the lock, with a fresh reload (the UI server's
        # check/update handlers and the launcher both write this file)
        new_manifest = res["manifest"]
        with _state_lock():
            st = load_state()
            rstate = st.get(key) or {}
            rstate["deployed"] = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date")}
            rstate["last_update"] = time.time()
            rstate["last_update_secs"] = round(time.time() - t0, 1)
            # re-record the check against the *new* deployed commit — we just moved, so
            # by construction the target is current (avoids a stale “update available” line)
            rstate["last_check"] = {"checked": {"repo": key, "ok": True, "cached": False, "target": target,
                                                "deployed": rstate["deployed"], "up_to_date": True},
                                   "checked_at": time.time()}
            st[key] = rstate
            save_state(st)
        save_manifest(key, new_manifest)

        for p in res["conflicts"][:10]:
            log(f"  ! kept your local version of {p} (upstream also changed it — merge manually if needed)")
        log(f"[update {key}] done in {res['applied']} applied, {res['deleted']} removed, "
            f"{len(res['conflicts'])} conflict(s) kept — {meta['name']} now at "
            f"{target['ref']} ({target['sha'][:7]})")
        try:
            _sync_deps(key, log)
        except Exception as e:
            log(f"  ! dependency sync failed: {e}")
        return {"ok": True, "applied": res["applied"], "deleted": res["deleted"],
                 "conflicts": res["conflicts"]}
    finally:
        clear_progress(key)



def main():
    ap = argparse.ArgumentParser(description="Workbench repo sync")
    ap.add_argument("cmd", choices=["init", "update", "check", "refs"])
    ap.add_argument("--repo", required=True, choices=list(REPOS))
    ap.add_argument("--tarball", default=None,
                    help="local tarball to extract instead of downloading (init only)")
    ap.add_argument("--force-full", action="store_true",
                    help="update via full tarball instead of the file diff")
    ap.add_argument("--json", action="store_true", help="machine-readable output for check/refs")
    a = ap.parse_args()
    try:
        if a.cmd == "init":
            out = cmd_init(a.repo, tarball=a.tarball)
        elif a.cmd == "update":
            out = cmd_update(a.repo, force_full=a.force_full)
        elif a.cmd == "check":
            out = cmd_check(a.repo, as_json=a.json)
        else:
            out = cmd_refs(a.repo, as_json=a.json)
        if isinstance(out, dict) and out.get("ok") is False:
            sys.exit(1)
    except RepoError as e:
        if a.json:
            print(json.dumps({"repo": a.repo, "ok": False, "error": str(e)}))
        else:
            print(f"error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
