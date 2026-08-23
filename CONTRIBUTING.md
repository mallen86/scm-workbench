# Working on the SCM Workbench

The packaged app is the intended way to *use* the Workbench; this file is the way to *work on* it — running it from a checkout, packaging the bundle, and cutting releases.

## Running from source

A plain venv and a browser tab — the same engine the bundle runs, no packaging involved.

### macOS / Linux

```sh
# 1. put the three repos side by side (the Workbench auto-detects the sisters)
cd ~/projects            # or wherever you keep things
git clone --depth 1 https://github.com/Alan-Cha/silhouette-card-maker
git clone --depth 1 https://github.com/Alan-Cha/scm-extras
git clone --depth 1 https://github.com/mallen86/scm-workbench

# 2. one venv carrying the base repo's script dependencies (SCM targets 3.12+)
python3 -m venv venv
source venv/bin/activate
pip install -r silhouette-card-maker/requirements.txt

# 3. run the Workbench from its own checkout
cd scm-workbench
./../venv/bin/python -m scm_workbench.server
```

A browser tab opens automatically at `http://127.0.0.1:8037`.

> The clones are shallow (`--depth 1`) since you only need the latest state to run things — re-clone without it, or run `git fetch --unshallow`, if you ever want the full history (e.g. to contribute upstream).

> **WSL2 (Windows):** the same steps work, plus a bonus: the server binds the WSL VM's interfaces, so the Workbench also opens a tab in your *Windows* default browser. From inside WSL, the usual `http://127.0.0.1:8037` works as before.

### Windows (PowerShell)

```powershell
# 1. sister folders, e.g. in Documents
cd Documents
git clone --depth 1 https://github.com/Alan-Cha/silhouette-card-maker
git clone --depth 1 https://github.com/Alan-Cha/scm-extras
git clone --depth 1 https://github.com/mallen86/scm-workbench

# 2. venv + deps
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r silhouette-card-maker\requirements.txt

# 3. go (from inside the scm-workbench folder)
cd scm-workbench
.\..\venv\Scripts\python.exe -m scm_workbench.server
```

> If your repos aren't named exactly `silhouette-card-maker` / `scm-extras`, or aren't siblings, paste the real paths into **Settings** once — it remembers.

> **PowerShell refuses `Activate.ps1` ("running scripts is disabled on this system")?** That's Windows' default `Restricted` execution policy, not anything wrong with the venv. The one-time, non-admin fix:
>
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> .\venv\Scripts\Activate.ps1
> ```
>
> (`RemoteSigned` just means "locally-authored scripts run, downloaded ones need a signature" — the right level for dev venvs.) Or skip activation altogether: step 3 works fine calling `.\..\venv\Scripts\python.exe` directly.

### Server flags

```
python -m scm_workbench.server [--port N] [--host 127.0.0.1] [--no-browser]
```

* `--port` — listen port (default 8037, or whatever you saved in Settings).
* `--no-browser` — don't auto-open a tab.
* The server always binds to **loopback only** (except on WSL2, where it binds the VM's interfaces so the Windows host can reach it) — it is local tooling, never a network service. File access is sandboxed to the two repos + the Workbench's own data dir.

**No extra dependencies** on the Workbench itself: it's pure Python standard library (3.10+). The *base repos'* `requirements.txt` is what does the heavy lifting (Pillow, ezdxf, pypdfium2, the plugins' fetchers, …) — install it once into the interpreter you run the Workbench with, and both the scripts and the UI work.

### Other entry points

* `python -m scm_workbench` — the app's own entry point (the one the bundle's stub binary calls): native window, private runtime, managed-copy bootstrap. In a plain dev checkout it behaves like the app, not like the classic dev server above.
* `python -m scm_workbench.repo_sync {check|refs|update|init} --repo scm|extras` — the repo-sync CLI behind the "Managed repo copies" page, if you like terminals.
* Silhouette Studio automation (`dxf_to_studio3.py`) is intentionally **not** wrapped: it drives a Windows-only GUI and lives in its own repo workflow. Use the `.studio3` files the Workbench generates/opens, or run that script in a terminal if you need the conversion itself.

## How it works

* **One manifest, two users.** The server holds a single option manifest for every job (each option: type, choices, default, help). The UI renders forms *from it* and the server assembles argv *from it* — so the on-screen command preview is byte-identical to what runs.
* **Jobs** are `subprocess.Popen` children with UTF-8 forced (`PYTHONUTF8=1` — Windows codepages can't print some card names), `CREATE_NO_WINDOW` on Windows (no console pop-ups), and session/process-group isolation so *Stop* kills cleanly on both platforms.
* **Live logs** flow over a per-job SSE stream (`/api/jobs/<id>/stream`); history is persisted to `data/jobs.json` + `data/logs/` (or the app data folder, when packaged).
* The Workbench never imports code from the base repos — it reads their JSON (`assets/layouts.json`, `assets/extra_layouts/`) and shells out, so it stays compatible with whatever version the repos are on.

The data area holds `settings.json`, job history/logs, the per-size offset table, `repos-state.json`, the managed repo copies, and the provisioned runtime — delete it to factory-reset. The bundle itself is never written to at runtime.

## Packaging the app

The repo doubles as its own [briefcase](https://briefcase.readthedocs.io) definition (see `pyproject.toml`):

```sh
pip install briefcase
briefcase build macos app      # → build/scm-workbench/macos/app/SCM Workbench.app
briefcase build windows app    # → build/scm-workbench/windows/app/ (zip it)
```

* **Entry point** is `scm_workbench.launcher`: it pins the app's data area to the writable per-user directory, marks the run as packaged (dependency sync may then `pip` into the app's own private runtime — never anything of the user's), bootstraps first launch, and hands over to the server, which the launcher runs as a **separate child process** with its own log (`server.log`) so the window can never take it down with it.
* **First launch** provisions the relocatable CPython runtime (GHCI python-build-standalone, pinned build in `launcher.py`) into the data area, then fetches the newest managed copy of each sister repo. The transcript goes to `launcher.log` in the data area; if a fetch can't finish (offline first run), the app still works and **Settings → "Managed repo copies"** retries on demand.
* **TLS** works without system configuration: `certifi` ships in the support packages and the launcher points `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` at it (bundled macOS Pythons can't see the OS trust store).
* **Signing** (optional, for a friction-free first launch): macOS — an Apple Developer ID plus notarization (`briefcase` passes both through once an identity is configured); Windows — an OV code-signing certificate. Unsigned builds run fine after the one-time Gatekeeper/SmartScreen exception.
* `.github/workflows/package.yml` builds both platforms, and on a `v*` tag publishes a GitHub release with the two archives.

## Releasing a new version

**The tag is the only version input.** On a `v*` tag push the workflow runs `scripts/inject_version.py`, which pins the tag (minus its `v`) into the two places a build consumes it: `scm_workbench/_version.py` (what the running app reports — the "Data & about" line and the version the update checker compares against the newest release) and the `[project]` version in `pyproject.toml` (what briefcase stamps into the bundle — the macOS About box, the dist-info record, the Windows executable's version metadata). The briefcase app section has no version of its own by design, so there is nothing left to keep in sync:

```bash
git tag v0.1.1
git push origin v0.1.1        # CI builds both archives and publishes the release
```

(For a local build, run `python scripts/inject_version.py v0.1.1` before `briefcase build`; with no tag in sight it keeps the version the repo declares.)
