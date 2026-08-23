# SCM Workbench

A local UI for **[silhouette-card-maker](https://github.com/Alan-Cha/silhouette-card-maker)** and **[scm-extras](https://github.com/Alan-Cha/scm-extras)** — the two Python repos that make card games for Silhouette cutting machines.

It wraps every script in a friendly, cross-platform interface: pick options in a form, watch a live console, open your results. No terminal, no memorized flags, no manual environment variable wiring.

```
┌──────────────────────────────────────────────────────────────────┐
│  SCM Workbench (this repo — the UI and the engine behind it)     │
│  └─ drives →  silhouette-card-maker   (SCM)                       │
│                scm-extras                                               │
│      from its own managed copies (latest release / latest main)      │
│      in the app's data folder — or from your own clones, which it    │
│      detects when they sit next to it                                │
└──────────────────────────────────────────────────────────────────┘
```

## Getting the app (macOS / Windows)

Download the latest release from the [GitHub releases page](https://github.com/mallen86/scm-workbench/releases) — one archive per platform, both built by the repo's CI whenever a `v*` tag is pushed.

* **macOS** — open `SCM Workbench.app` (double-click; if it's unsigned, right-click → *Open* once).
* **Windows** — unzip and run the app (one-time SmartScreen prompt).

That's the whole install. **Nothing is put on your machine** — no Python, no packages, no terminal:

* The app keeps everything in its own per-user data folder (`~/Library/Application Support/scm-workbench` on macOS, `%LOCALAPPDATA%\scm-workbench` on Windows): a **private Python runtime** it provisions itself, the pinned job dependencies, settings, job history, and the managed repo copies.
* On **first launch** a native window opens at once — no waiting, no installer — while the background work proceeds with live progress in the UI: the runtime is provisioned, dependencies installed into it, and the newest copy of each sister repo is fetched (SCM tracks its **latest release**, scm-extras its **main** branch, since it publishes no releases).
* **Settings → “Managed repo copies”** does the same on demand: download latest, re-check, or pin to any tag/SHA — so a future upstream release can never drag a broken `main` state into a working version.
* Prefer your own clones? Point **Settings → repos** at them once (sibling folders next to the app are auto-detected) — the managed copies simply stay unused.

**Updating the app itself** works the same way: it checks GitHub for a newer release at start-up and once a day while open (**Settings → “App updates”** shows the button, the “last checked” time, and the outcome). When a new release is out, the button becomes *Download & install* — the install swaps the app folder only (your data folder, with settings, images and decklists, is never part of it), the app reopens itself as the new version, and the previous one is kept as a backup until the next launch. The release repo is private, so checks need a **GitHub token** with read access to it (paste it in the same card; it is stored only in the local `settings.json`).

## What you get

| Page | Wraps |
| --- | --- |
| **Dashboard** | repo status, quick actions, layout matrix, recent jobs |
| **Fetch card art** | all 22 game plugins (`plugins/*/fetch.py`) — decklist by file, a file picked from disk with the native OS file dialog, or pasted text; per-game formats; the MTG plugin's 10 preference flags |
| **Create PDF** | `create_pdf.py` — all 28 options: sizes, registration, specialty layouts, fit/crop/extend, PPI (defaults to 1200), quality, skip-indexes, labels, outlines, borderless… |
| **Offset & calibration** | `offset_pdf.py`, `generate_calibration.py`, plus a per-paper-size offset table and an editor for SCM's shared X/Y/angle offset (`data/offset_data.json`) |
| **Cutting templates** | `generate_dxf.py` — a single template (named *or* fully custom card/paper dimensions), batch (missing / all / re-optimize), and a gallery of every DXF + `.studio3` in the repo |
| **Extras: MTG & Sorcery** | `scm-extras/generate.py`, `generate_readme_tables.py`, and the extras template gallery |
| **Sizes & layouts** | every card/paper size as scaled silhouettes, the full cards-per-page matrix, specialty layouts |
| **Utilities** | clear the card-image folders (with confirm — the folder README placeholders are kept), a size converter using the repo's own units, a “list all sizes” dump |

Everything runs as a tracked **job**: live log stream in the bottom console drawer, stop button, "reveal folder" (for your own folders) or **"Move to my files…"** (for Workbench-managed copies — the system save dialog carries the finished PDF / calibration sheets from the app's private working area out to wherever you point it), persistent history, and a **command preview** that shows the exact argv (and any auto-added env vars) before you run. The preview doubles as a sanity check: the Create PDF run button stays disabled — with a message — while the front directory has no images to make pages from.

**Interface: Simple or Advanced** (the switch in the top bar, next to the Console button — it applies to every page). Advanced is the full layout above. Simple is about not being overwhelmed: the navigation collapses to just **Fetch card art**, **Create PDF** and Settings, and the Create PDF form becomes a single plain section — no group headers, no collapsible power sections, no card title — holding only the everyday choices, in two rows: the card-size and paper-size dropdowns alone up top, and the three toggles (borderless, apply saved offset, front-only) together below. The rest of the form is still there in advanced mode; every option not shown in simple mode keeps its default, so a simple-mode run and an advanced-mode run with the same visible settings produce the same command. In that section the “Apply saved offset” switch is only live when an offset exists for the form’s paper size (its per-size row, or the global value) — otherwise it sits disabled, pointing at the Offset & calibration page. In simple mode the command preview drops the flags that would only repeat SCM's own defaults (the folder paths, 3-mark registration, stretch fit, and quality while the global quality setting is 100) — a plain run reads `create_pdf.py --output_path … --card_size … --paper_size … --ppi 1200`, and any value you deliberately change (or a non-100 global quality) reappears in the command. The Fetch pages are already as simple as they get, so they're untouched in either mode.

### The magic touch: extras auto-wiring

The scm-extras README asks you to export `SCM_EXTRA_LAYOUTS` by hand before you can use `standard_mtg` / `standard_sorcery` in SCM. The Workbench does it for you: the moment you pick an extra size (in Create PDF or template generation), it detects the size belongs to `scm-extras` and injects the env var for that job — with a note in the preview.

## How it works

* **One manifest, two users.** The server holds a single option manifest for every job (each option: type, choices, default, help). The UI renders forms *from it* and the server assembles argv *from it* — so the on-screen command preview is byte-identical to what runs.
* **Jobs** are `subprocess.Popen` children with UTF-8 forced (`PYTHONUTF8=1` — Windows codepages can't print some card names), `CREATE_NO_WINDOW` on Windows (no console pop-ups), and session/process-group isolation so *Stop* kills cleanly on both platforms.
* **Live logs** flow over a per-job SSE stream (`/api/jobs/<id>/stream`); history is persisted to `data/jobs.json` + `data/logs/` (or the app data folder, when packaged).
* Delete that data area to factory-reset the Workbench.
* The Workbench never imports code from the base repos — it reads their JSON (`assets/layouts.json`, `assets/extra_layouts/`) and shells out, so it stays compatible with whatever version the repos are on.

## Running from source (developers & contributors)

The app is the intended way to *use* the Workbench; this is the way to *work on* it — a plain venv and a browser tab, same engine as the bundle.

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

> If your repos aren't named exactly `silhouette-card-maker` / `scm-extras`, or aren't siblings, just paste the real paths into **Settings** once — it remembers.

> **PowerShell refuses `Activate.ps1` (“running scripts is disabled on this system”)?** That's Windows' default `Restricted` execution policy, not anything wrong with the venv. The one-time, non-admin fix:
>
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> .\venv\Scripts\Activate.ps1
> ```
>
> (`RemoteSigned` just means “locally-authored scripts run, downloaded ones need a signature” — the right level for dev venvs.) Or skip activation altogether: step 3 works fine calling `.\..\venv\Scripts\python.exe` directly.

### Server flags

```
python -m scm_workbench.server [--port N] [--host 127.0.0.1] [--no-browser]
```

* `--port` — listen port (default 8037, or whatever you saved in Settings).
* `--no-browser` — don't auto-open a tab.
* The server always binds to **loopback only** (except on WSL2, where it binds the VM's interfaces so the Windows host can reach it) — it is local tooling, never a network service. File access is sandboxed to the two repos + the Workbench's own data dir.

**No extra dependencies** on the Workbench itself: it's pure Python standard library (3.10+). The *base repos'* `requirements.txt` is what does the heavy lifting (Pillow, ezdxf, pypdfium2, the plugins' fetchers, …) — install it once into the interpreter you run the Workbench with, and both the scripts and the UI work.

Two other entry points, for completeness:

* `python -m scm_workbench` — the app's own entry point (the one the bundle's stub binary calls): native window, private runtime, managed-copy bootstrap. In a plain dev checkout it behaves like the app, not like the classic dev server above.
* `python -m scm_workbench.repo_sync {check|refs|update|init} --repo scm|extras` — the repo-sync CLI behind the “Managed repo copies” page, if you like terminals.

> Silhouette Studio automation (`dxf_to_studio3.py`) is intentionally **not** wrapped: it drives a Windows-only GUI and lives in its own repo workflow. Use the `.studio3` files the Workbench generates/opens, or run that script in a terminal if you need the conversion itself.

## Notes

* **Offsets** — SCM keeps one *shared* X/Y/angle at `silhouette-card-maker/data/offset_data.json`, but the correction you need depends on the paper you feed. So the Workbench keeps a **per-paper-size table of its own** and, before any run that consumes an offset (Create PDF with “Apply saved offset”, or Offset PDF with a size picked), **stages the matching row into that shared file** — the job then runs with exactly the value SCM's scripts always read, and no SCM change is needed. Saving a row from the *Offset* page stages it too; running Offset PDF with “Save” records the used values back into that row. When no row matches, the global value applies as before.
* **Front pages only** — `create_pdf.py` refuses `--only_fronts` while `game/double_sided/` still holds images. Flipping the toggle on with images present warns you that the option won't work and offers to remove them from the folder in one click (non-images like `README.md` are left alone).
* **Decklists** — pasted text is saved into `game/decklist/<name>` (letters, digits, spaces, `. - ( )` only). The UI can also pick an existing decklist from anywhere on disk via the native file dialog (Settings-independent); it's copied into `game/decklist/` automatically.
* **Templates** — single-template generation defaults to the repo's own naming (`<paper>-<card>[-borderless]-v1.dxf`); tick *save* to register new sizes in `layouts.json`.
* **Extras DXFs** are generated into `scm-extras/cutting_templates/` exactly like the upstream `generate.py` (SCM is located as a sister folder).

## Packaging the app

The repo doubles as its own [briefcase](https://briefcase.readthedocs.io) definition (see `pyproject.toml`):

```sh
pip install briefcase
briefcase build macos app      # → build/scm-workbench/macos/app/SCM Workbench.app
briefcase build windows app    # → build/scm-workbench/windows/app/ (zip it)
```

* **Entry point** is `scm_workbench.launcher`: it pins the app's data area to the writable per-user directory, marks the run as packaged (dependency sync may then `pip` into the app's own private runtime — never anything of the user's), bootstraps first launch, and hands over to the server, which the launcher runs as a **separate child process** with its own log (`server.log`) so the window can never take it down with it.
* **First launch** provisions the relocatable CPython runtime (GHCI python-build-standalone, pinned build in `launcher.py`) into the data area, then fetches the newest managed copy of each sister repo. The transcript goes to `launcher.log` in the data area; if a fetch can't finish (offline first run), the app still works and **Settings → “Managed repo copies”** retries on demand.
* **TLS** works without system configuration: `certifi` ships in the support packages and the launcher points `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` at it (bundled macOS Pythons can't see the OS trust store).
* **Signing** (optional, for a friction-free first launch): macOS — an Apple Developer ID plus notarization (`briefcase` passes both through once an identity is configured); Windows — an OV code-signing certificate. Unsigned builds run fine after the one-time Gatekeeper/SmartScreen exception.
* `.github/workflows/package.yml` builds both platforms, and on a `v*` tag publishes a GitHub release with the two archives.

### Releasing a new version

**The tag is the only version input.** On a `v*` tag push the workflow runs
`scripts/inject_version.py`, which pins the tag (minus its `v`) into the two
places a build consumes it: `scm_workbench/_version.py` (what the running app
reports — the “Data & about” line and the version the update checker compares
against the newest release) and the `[project]` version in `pyproject.toml`
(what briefcase stamps into the bundle — the macOS About box, the dist-info
record, the Windows executable’s version metadata). The briefcase app section
has no version of its own by design, so there is nothing left to keep in sync:

```bash
git tag v0.1.1
git push origin v0.1.1        # CI builds both archives and publishes the release
```

(For a local build, run `python scripts/inject_version.py v0.1.1` before
`briefcase build`; with no tag in sight it keeps the version the repo declares.)

The data area holds `settings.json`, job history/logs, the per-size offset table, `repos-state.json`, the managed repo copies, and the provisioned runtime — delete it to factory-reset. The bundle itself is never written to at runtime.
