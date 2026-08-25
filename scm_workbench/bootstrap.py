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
import time
from pathlib import Path
from typing import Callable, List, Optional


def bootstrap_managed_repos(repo_sync, log: Optional[Callable[[str], None]] = None) -> None:
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
                log(f"\n[bootstrap] the managed {meta['name']} copy no longer matches its recorded "
                    f"state — re-deploying it now (your decklists, images and output are "
                    f"staged and restored around the swap, so nothing is lost) …")
                repo_sync.cmd_init(key, log=log, force_redeploy=True)
                log(f"[bootstrap] {meta['name']} re-synced — continuing.")
                continue
            except Exception as e:
                log(f"[bootstrap] {meta['name']} state check failed ({e}) — leaving the copy as-is.")
            continue
        log(f"\n[bootstrap] first launch — fetching the newest {meta['name']} "
            f"({meta['owner']}/{meta['repo']}) into the Workbench data area …")
        t0 = time.time()
        try:
            repo_sync.cmd_init(key, log=log)
            log(f"[bootstrap] done in {time.time() - t0:.0f}s — dashboard is ready.")
        except Exception as e:
            log(f"[bootstrap] could not fetch {meta['name']} yet ({e}).")
            log("[bootstrap] no problem — the app still works; open Settings → "
                "“Managed repo copies” and press “Download latest” when you're online.")
            log("[bootstrap] (existing sister folders next to this app are still detected normally)")


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
    bootstrap flag the UI polls, fetches the managed repo copies (with the
    transcript feeding the banner's live phase), then clears the flag.
    """
    if log is None:
        log = print
    from scm_workbench import repo_sync

    def blog(s: str = "") -> None:
        # every transcript line also refreshes the UI's status (the dashboard
        # banner polls /api/info, which reads this flag)
        line = str(s).strip()
        if line:
            _write_flag(data, ["scm", "extras"], [], line[:140])
        log(s)

    _write_flag(data, ["scm", "extras"], [], "getting ready …")
    try:
        bootstrap_managed_repos(repo_sync, log=blog)
    finally:
        _write_flag(data, [], ["scm", "extras"], "done")
        log("[bootstrap] first-launch preparation finished — the UI now sees the managed copies.")
