# SCM Workbench

A local web UI for **[silhouette-card-maker](https://github.com/Alan-Cha/silhouette-card-maker)** and **[scm-extras](https://github.com/Alan-Cha/scm-extras)** — the two Python repos that make card games for Silhouette cutting machines.

It wraps every script in a friendly, cross-platform interface: pick options in a form, watch a live console, open your results. No terminal, no memorized flags, no manual environment variable wiring.

```
┌────────────────────────────────────────────────────────────┐
│  SCM Workbench (this repo)                                  │
│  └─ shells out to →  ../silhouette-card-maker   (SCM)      │
│                     ../scm-extras            (extras)       │
└────────────────────────────────────────────────────────────┘
```

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
| **Utilities** | `clean_up.py` (with confirm), a size converter using the repo's own units, a "list all sizes" dump |

Everything runs as a tracked **job**: live log stream in the bottom console drawer, stop button, "reveal folder" (for your own folders) or **"Move to my files…"** (for Workbench-managed copies — the system save dialog carries the finished PDF / calibration sheets from the app's private working area out to wherever you point it), persistent history, and a **command preview** that shows the exact argv (and any auto-added env vars) before you run.

**Interface: Simple or Advanced** (Settings → Interface). Advanced is the full layout above. Simple trims the Create PDF page to the basics — card & paper size, registration marks, borderless, front-only and the saved-offset switch; directory fields, resolution/quality and the power sections stay one click away. The Fetch pages are already as simple as they get, so they're untouched; hidden options simply keep their defaults, so the command that runs is identical in both modes.

### The magic touch: extras auto-wiring

The scm-extras README asks you to export `SCM_EXTRA_LAYOUTS` by hand before you can use `standard_mtg` / `standard_sorcery` in SCM. The Workbench does it for you: the moment you pick an extra size (in Create PDF or template generation), it detects the size belongs to `scm-extras` and injects the env var for that job — with a note in the preview.

## Quick start

### macOS / Linux

```sh
# 1. put the three repos side by side (the Workbench auto-detects the sisters)
cd ~/projects            # or wherever you keep things
git clone --depth 1 https://github.com/Alan-Cha/silhouette-card-maker
git clone --depth 1 https://github.com/Alan-Cha/scm-extras
git clone --depth 1 https://github.com/mallen86/scm-workbench

# 2. one Python venv for the base repo's scripts (SCM targets 3.12+)
python3 -m venv venv
source venv/bin/activate
pip install -r silhouette-card-maker/requirements.txt

# 3. run the Workbench with that interpreter
./venv/bin/python scm-workbench/server.py
```

A browser tab opens automatically at `http://127.0.0.1:8037`.

> The clones are shallow (`--depth 1`) since you only need the latest state to run things — re-clone without it, or run `git fetch --unshallow`, if you ever want the full history (e.g. to contribute upstream).

> **WSL2 (Windows):** the same steps work, plus a bonus: the Workbench opens a tab in your *Windows* default browser when the server starts (it binds the WSL VM's interfaces, which only the host machine can reach). From inside WSL, the usual `http://127.0.0.1:8037` works as before.

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

# 3. go
.\venv\Scripts\python.exe scm-workbench\server.py
```

> If your repos aren't named exactly `silhouette-card-maker` / `scm-extras`, or aren't siblings, just paste the real paths into **Settings** once — it remembers.

> **PowerShell refuses `Activate.ps1` (“running scripts is disabled on this system”)?** That's Windows' default `Restricted` execution policy, not anything wrong with the venv. The one-time, non-admin fix:
>
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> .\venv\Scripts\Activate.ps1
> ```
>
> (`RemoteSigned` just means “locally-authored scripts run, downloaded ones need a signature” — the right level for dev venvs.) Or skip activation altogether: step 3 works fine calling `.\venv\Scripts\python.exe` directly.

### Server flags

```
python server.py [--port N] [--host 127.0.0.1] [--no-browser]
```

* `--port` — listen port (default 8037, or whatever you saved in Settings).
* `--no-browser` — don't auto-open a tab.
* The server always binds to **loopback only** — it is local tooling, never a network service. File access is sandboxed to the two repos + the Workbench's own data dir.

## No extra dependencies

`server.py` is pure Python standard library (3.10+, works under 3.12). The *base repos'* `requirements.txt` is what does the heavy lifting (Pillow, ezdxf, pypdfium2, the plugins' fetchers, …) — install it once into the interpreter you run the Workbench with, and both the scripts and the UI work.

> Silhouette Studio automation (`dxf_to_studio3.py`) is intentionally **not** wrapped: it drives a Windows-only GUI and lives in its own repo workflow. Use the `.studio3` files the Workbench generates/opens, or run that script in a terminal if you need the conversion itself.

## How it works

* **One manifest, two users.** `server.py` holds a single option manifest for every job (each option: type, choices, default, help). The browser renders forms *from it* and the server assembles argv *from it* — so the on-screen command preview is byte-identical to what runs.
* **Jobs** are `subprocess.Popen` children with UTF-8 forced (`PYTHONUTF8=1` — Windows codepages can't print some card names), `CREATE_NO_WINDOW` on Windows (no console pop-ups), and session/process-group isolation so *Stop* kills cleanly on both platforms.
* **Live logs** flow over a per-job SSE stream (`/api/jobs/<id>/stream`); history is persisted to `data/jobs.json` + `data/logs/`.
* **`data/`** (gitignored) holds `settings.json`, job history and logs. Delete it to factory-reset the Workbench.
* The Workbench never imports code from the base repos — it reads their JSON (`assets/layouts.json`, `assets/extra_layouts/`) and shells out, so it stays compatible with whatever version the repos are on.

## Notes

* **Offsets** — SCM keeps one *shared* X/Y/angle at `silhouette-card-maker/data/offset_data.json`, but the correction you need depends on the paper you feed. So the Workbench keeps a **per-paper-size table of its own** (`data/offsets_by_size.json`) and, before any run that consumes an offset (Create PDF with “Apply saved offset”, or Offset PDF with a size picked), **stages the matching row into that shared file** — the job then runs with exactly the value SCM's scripts always read, and no SCM change is needed. Saving a row from the *Offset* page stages it too; running Offset PDF with “Save” records the used values back into that row. When no row matches, the global value applies as before.
* **Front pages only** — `create_pdf.py` refuses `--only_fronts` while `game/double_sided/` still holds images. Flipping the toggle on with images present warns you that the option won't work and offers to remove them from the folder in one click (non-images like `README.md` are left alone).
* **Decklists** — pasted text is saved into `game/decklist/<name>` (letters, digits, spaces, `. - ( )` only). The app's window can also pick an existing decklist from anywhere on disk via the native file dialog (Settings-independent); it's copied into `game/decklist/` automatically.
* **Templates** — single-template generation defaults to the repo's own naming (`<paper>-<card>[-borderless]-v1.dxf`); tick *save* to register new sizes in `layouts.json`.
* **Extras DXFs** are generated into `scm-extras/cutting_templates/` exactly like the upstream `generate.py` (SCM is located as a sister folder).

## Packaging (self-contained app for macOS / Windows)

The repo doubles as its own packaging definition (see `pyproject.toml`):

```sh
pip install briefcase
briefcase build macos app      # → build/scm-workbench/macos/app/SCM Workbench.app
briefcase build windows app    # → build/scm-workbench/windows/app/ (zip it)
```

* **Entry point** is `scm_workbench.launcher`: it pins the app's data area to a
  writable per-user directory (`~/Library/Application Support/scm-workbench` on macOS,
  `%LOCALAPPDATA%\scm-workbench` on Windows — overridable with `SCM_WORKBENCH_DATA`),
  then hands over to the regular server. The dev flow (`python server.py`) is unchanged.
* **First launch** does three things automatically (transcript in `launcher.log` in the
  data area): it provisions a relocatable CPython runtime (GHCI python-build-standalone,
  pinned build in `launcher.py`) for *job* scripts — the bundle's own interpreter is only
  reachable through the app stub; it pointlessly-provisions nothing else: dependency sync
  only `pip install`s into that runtime when `SCM_WORKBENCH_PACKAGED=1`; and it fetches the
  newest managed copy of each sister repo (Settings → “Managed repo copies” offers the same
  on demand, per repo: track the latest release (the default for silhouette-card-maker, so
  unreleased changes on main can’t break a released version), the latest `main`, or any
  pinned tag/SHA; scm-extras publishes no releases and follows its main branch).
* **TLS** works without system configuration: `certifi` ships in the support packages and
  the launcher points `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` at it (bundled macOS Pythons
  can't see the OS trust store).
* **Signing** (optional, for a friction-free first launch): macOS — an Apple Developer ID
  plus notarization (`briefcase` passes both through once an identity is configured);
  Windows — an OV code-signing certificate. Unsigned builds run fine after the one-time
  Gatekeeper/SmartScreen exception.
* `.github/workflows/package.yml` builds both platforms (and, on a `v*` tag, publishes a
  GitHub release with the two archives).

The data area holds `settings.json`, job history/logs, the per-size offset table,
`repos-state.json`, the managed repo copies, and the provisioned runtime — delete it to
factory-reset. The bundle itself is never written to at runtime.
