"""First-launch preparation for the packaged app.

Historically this lived in the stub launcher (the process that *opened the
window*), which prepared the data area in three steps: provision a private
runtime, start the UI server, then fetch the managed repo copies. With the
Tauri shell the window process *is* the app and the runtime ships inside the
bundle, so the only remaining first-launch work — fetching the managed repo
copies — now runs as a background thread of the UI server itself, exactly
where its effects belong:

  * bootstrap.json (data area) tells the UI whether prep is still going —
    the dashboard banner polls /api/info, which reads that flag;
  * the work is never fatal: a failed fetch leaves the app working with
    what it has, and Settings → "Managed repo copies" offers the retry.
"""

import json
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional


def _bootstrap_one(repo_sync, key, log) -> None:
    """One repo of a bootstrap pass: the deployed check, a *safe* re-deploy
    if the tree drifted, or the first-ever fetch. Never raises — a failure
    just means the app keeps working with what it has, and the user can
    retry it from Settings later."""
    meta = repo_sync.REPOS[key]
    r = (repo_sync.load_state().get(key) or {})
    if r.get("deployed"):
        # The state says “deployed” — but the state file and the tree can
        # drift (an interrupted update, a pre-lock era write). A cheap
        # offline probe decides; a mismatch triggers a *safe* re-deploy,
        # which stages the user's files before touching the tree, so this
        # path can never be a data-loss path again.
        try:
            if repo_sync.verify_deployed(key):
                return
            log(f"\n[bootstrap] the managed {meta['name']} copy no longer matches its recorded "
                f"state — re-deploying it now (your decklists, images and output are "
                f"staged and restored around the swap, so nothing is lost) …")
            repo_sync.cmd_init(key, log=log, force_redeploy=True)
            log(f"[bootstrap] {meta['name']} re-synced — continuing.")
            return
        except Exception as e:
            log(f"[bootstrap] {meta['name']} state check failed ({e}) — leaving the copy as-is.")
        return
    log(f"\n[bootstrap] first launch — fetching the newest {meta['name']} "
        f"({meta['owner']}/{meta['repo']}) into the Workbench data area …")
    t0 = time.time()
    try:
        repo_sync.cmd_init(key, log=log)
        log(f"[bootstrap] {meta['name']} done in {time.time() - t0:.0f}s.")
    except Exception as e:
        log(f"[bootstrap] could not fetch {meta['name']} yet ({e}).")
        log("[bootstrap] no problem — the app still works; open Settings → "
            "“Managed repo copies” and press “Download latest” when you're online.")
        log("[bootstrap] (existing sister folders next to this app are still detected normally)")


def bootstrap_managed_repos(repo_sync, log: Optional[Callable[[str], None]] = None) -> None:
    """Fetch newest managed copies on first launch; never fatal.

    The sequential pass — the legacy stub launcher's behaviour, unchanged."""
    if log is None:
        log = print
    for key in repo_sync.REPOS:
        _bootstrap_one(repo_sync, key, log)


_flag_lock = threading.Lock()


def _write_flag(data: Path, pending: List[str], done: List[str], phase: str) -> None:
    try:
        (data / "bootstrap.json").write_text(
            json.dumps({"pending": pending, "done": done, "phase": phase}),
            encoding="utf-8",
        )
    except Exception:
        pass


def run_first_boot(data: Path, log: Optional[Callable[[str], None]] = None) -> None:
    """The whole first-boot sequence, run by the app itself (packaged mode).

    Call this from a daemon thread once the UI is up: it writes the
    bootstrap flag the UI polls, fetches the managed repo copies *in
    parallel* (one thread per repo — each clone is independent, and the
    state file is mutex-locked per read-modify-write), with the transcript
    feeding the banner's live phase, then clears the flag.
    """
    if log is None:
        log = print
    from scm_workbench import repo_sync

    def blog(s: str = "") -> None:
        # every transcript line also refreshes the UI's status (the dashboard
        # banner polls /api/info, which reads this flag)
        line = str(s).strip()
        if line:
            with _flag_lock:
                _write_flag(data, keys, [], line[:140])
        log(s)

    keys = list(repo_sync.REPOS.keys())
    with _flag_lock:
        _write_flag(data, keys, [], "getting ready …")
    threads = [
        threading.Thread(
            target=_bootstrap_one, args=(repo_sync, key, blog),
            daemon=True, name=f"first-boot-{key}",
        )
        for key in keys
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with _flag_lock:
        _write_flag(data, [], keys, "done")
    log("[bootstrap] first-launch preparation finished — the UI now sees the managed copies.")
