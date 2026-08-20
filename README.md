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
| **Fetch card art** | all 22 game plugins (`plugins/*/fetch.py`) — decklist by file *or* pasted text, per-game formats, and the MTG plugin's 10 preference flags |
| **Create PDF** | `create_pdf.py` — all 28 options: sizes, registration, specialty layouts, fit/crop/extend, PPI, quality, skip-indexes, labels, outlines, borderless… |
| **Offset & calibration** | `offset_pdf.py`, `generate_calibration.py`, plus a small editor for the saved X/Y/angle offset (`data/offset_data.json`) |
| **Cutting templates** | `generate_dxf.py` — a single template (named *or* fully custom card/paper dimensions), batch (missing / all / re-optimize), and a gallery of every DXF + `.studio3` in the repo |
| **Extras: MTG & Sorcery** | `scm-extras/generate.py`, `generate_readme_tables.py`, and the extras template gallery |
| **Sizes & layouts** | every card/paper size as scaled silhouettes, the full cards-per-page matrix, specialty layouts |
| **Utilities** | `clean_up.py` (with confirm), a size converter using the repo's own units, a "list all sizes" dump |

Everything runs as a tracked **job**: live log stream in the bottom console drawer, stop button, "reveal folder", persistent history, and a **command preview** that shows the exact argv (and any auto-added env vars) before you run.

### The magic touch: extras auto-wiring

The scm-extras README asks you to export `SCM_EXTRA_LAYOUTS` by hand before you can use `standard_mtg` / `standard_sorcery` in SCM. The Workbench does it for you: the moment you pick an extra size (in Create PDF or template generation), it detects the size belongs to `scm-extras` and injects the env var for that job — with a note in the preview.

## Quick start

### macOS / Linux

```sh
# 1. put the three repos side by side (the Workbench auto-detects the sisters)
cd ~/projects            # or wherever you keep things
git clone https://github.com/Alan-Cha/silhouette-card-maker
git clone https://github.com/Alan-Cha/scm-extras
git clone <this-repo>   # scm-ui

# 2. one Python venv for the base repo's scripts (SCM targets 3.12+)
python3 -m venv venv
source venv/bin/activate
pip install -r silhouette-card-maker/requirements.txt

# 3. run the Workbench with that interpreter
./venv/bin/python scm-ui/server.py
```

A browser tab opens automatically at `http://127.0.0.1:8037`.

### Windows (PowerShell)

```powershell
# 1. sister folders, e.g. in Documents
cd Documents
git clone https://github.com/Alan-Cha/silhouette-card-maker
git clone https://github.com/Alan-Cha/scm-extras
git clone <this-repo>

# 2. venv + deps
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r silhouette-card-maker\requirements.txt

# 3. go
.\venv\Scripts\python.exe scm-ui\server.py
```

> If your repos aren't named exactly `silhouette-card-maker` / `scm-extras`, or aren't siblings, just paste the real paths into **Settings** once — it remembers.

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

* **Offsets** — the saved X/Y/angle lives at `silhouette-card-maker/data/offset_data.json`; the Workbench's *Offset* page edits the same file the scripts read, so `--load_offset` just works.
* **Decklists** — pasted text is saved into `game/decklist/<name>` (letters, digits, spaces, `. - ( )` only).
* **Templates** — single-template generation defaults to the repo's own naming (`<paper>-<card>[-borderless]-v1.dxf`); tick *save* to register new sizes in `layouts.json`.
* **Extras DXFs** are generated into `scm-extras/cutting_templates/` exactly like the upstream `generate.py` (SCM is located as a sister folder).
