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

* `python -m scm_workbench` — the source-checkout entry point: it prepares the dev data area and runs the HTTP server for a browser. The packaged app is the Tauri shell in `tauri/`, which spawns the bundled Python worker.
* `python -m scm_workbench.repo_sync {check|refs|update|init} --repo scm|extras` — the repo-sync CLI behind the "Managed repo copies" page, if you like terminals.
* Silhouette Studio automation (`dxf_to_studio3.py`) is intentionally **not** wrapped: it drives a Windows-only GUI and lives in its own repo workflow. Use the `.studio3` files the Workbench generates/opens, or run that script in a terminal if you need the conversion itself.

## How it works

* **One manifest, two users.** The server holds a single option manifest for every job (each option: type, choices, default, help). The UI renders forms *from it* and the server assembles argv *from it* — so the on-screen command preview is byte-identical to what runs.
* **Jobs** are `subprocess.Popen` children with UTF-8 forced (`PYTHONUTF8=1` — Windows codepages can't print some card names), `CREATE_NO_WINDOW` on Windows (no console pop-ups), and session/process-group isolation so *Stop* kills cleanly on both platforms.
* **Live logs** use native aggregate polling for packaged Tauri windows and a
  per-job SSE stream (`/api/jobs/<id>/stream`) in the browser; history is
  persisted to `data/jobs.json` + `data/logs/` (or the app data folder, when
  packaged).
* The Workbench never imports code from the base repos — it reads their JSON and shells out, so it stays compatible with whatever version the repos are on. `silhouette-card-maker` and `scm-extras` are always authoritative for fetching, PDF/DXF generation, and layouts; Workbench only wraps and orchestrates them.
* **Native boundary (current migration wave).** The packaged Tauri window supervises one Python worker. That worker serves both the existing HTTP compatibility server and bounded JSON-lines RPC over stdin/stdout. Bootstrap reads, bounded `settings.set`, `offset.set`/`offset.delete`, preview, packaged-Tauri job list/start/log/kill/poll, read-only `template.resolve`/`file.list` metadata, bounded `file.open`/`file.reveal`/`url.open` OS actions, a Rust-owned no-argument decklist picker/import command backed by private `decklists.import_selected` IPC, asynchronous repository metadata (`repos.refs`, `repos.source.set`, `repos.check`, `repos.poll`), and asynchronous update/release-notes operations (`updates.get`, `updates.check`, `updates.notes`, `updates.poll`, `updates.start`) use native IPC; browser HTTP/SSE fallback, raw bytes, save/delete, and static/worker-origin navigation remain HTTP. Update checks/notes and repository network work are backgrounded so starts and polls remain immediate; `repo_init` and `repo_update` remain `jobs.start` operations. Offset state is canonical in Workbench data and is projected atomically into SCM under a serialized offset/job lease; repository source settings and repository check/deployment metadata retain their separate canonical mirrors. The upstream repo is never modified by metadata IPC. Native actions and mutations never silently retry over HTTP. `settings.set` accepts only its bounded settings schema and offsets have their own exact numeric and paper-size bounds. See [docs/native-ipc.md](docs/native-ipc.md) for the exact schema, bounds, ACL, rollout rule, and verification commands.

The data area holds `settings.json`, job history/logs, canonical `offset_state.json`, `repos-state.json`, the managed repo copies, and the provisioned runtime — delete it to factory-reset. The bundle itself is never written to at runtime.

## Packaging the app

The packaged app is a Tauri native webview plus one bundled, supervised Python
worker. It is not a Briefcase/pywebview window. The worker serves the UI's
transitional HTTP origin and the first-slice JSON-lines IPC described in
[docs/native-ipc.md](docs/native-ipc.md).

Use a Python 3.13 development environment, Rust, and Node.js for the local
checks and build:

```sh
uv venv && uv sync
python -m unittest discover -s tests -v
python scripts/check_ui_imports.py
python scripts/check_ui_transport.py
python scripts/check_ui_jobs.py
python scripts/check_ui_repos.py
python scripts/check_ui_preview.py
python scripts/check_ui_artifacts.py
python scripts/check_ui_native_actions.py
python scripts/check_ui_settings.py
python scripts/check_ui_offsets.py
python scripts/check_ui_updates.py
find ui/js -name '*.js' -print0 | xargs -0 -n1 node --check
(cd tauri && cargo fmt --check && cargo test && cargo check --features custom-protocol)
(cd tauri && cargo build --release --features custom-protocol)
```

`PIP_FIND_LINKS=file://$(pwd)/ciwheels` is used by CI and by
`scripts/build.sh` when baking the bundled runtime. On an ARM64 Mac,
`scripts/build.sh macos` also assembles the local `.app`. The complete
platform-specific assembly and smoke checks live in
`.github/workflows/package.yml`; use that workflow (or a matching local copy
of its steps) for the Windows bundle.

The supported release matrix is **macOS ARM64 only** and **Windows x64 only**:
there is no Intel/universal macOS artifact and no ARM Windows artifact. The
macOS workflow uses the current ad-hoc signature and no notarization; current
Windows artifacts are unsigned. Those signing tradeoffs, including the
expected macOS **Open Anyway** and Windows SmartScreen prompts, are explicitly
accepted for the current first slice. Do not add Developer ID/notarization or
an OV certificate as part of this work.

## Releasing a new version

**The tag is the only version input.** On a `v*` tag push the workflow runs `scripts/inject_version.py`, which pins the tag (minus its `v`) into the Python version, Tauri config/Cargo metadata, and the project metadata consumed during packaging. The running app, native shell, and release metadata therefore share one version. The workflow then builds the macOS ARM64 and Windows x64 archives and attaches them to the GitHub release.

```bash
git tag v0.1.1
git push origin v0.1.1        # CI builds both archives and publishes the release
```

For a local build, run `python scripts/inject_version.py v0.1.1` before building; with no tag in sight it keeps the version the repository declares.
