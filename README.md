# SCM Workbench

<p align="center">
  <img src="screenshot.gif" alt="The Fetch card art page, Simple mode first, then Advanced - the switch is at the bottom of the sidebar" width="800">
</p>

A local interface for [silhouette-card-maker](https://github.com/Alan-Cha/silhouette-card-maker) and [scm-extras](https://github.com/Alan-Cha/scm-extras), the two Python repos for making card games on Silhouette cutting machines.

It wraps those repos' scripts in a normal app: pick options in a form, see the exact command that will run, press run, and watch the job in a live log. No terminal, no memorized flags, nothing to install.

## Getting the app (macOS / Windows)

Download the latest release from the [releases page](https://github.com/mallen86/scm-workbench/releases) — one archive per platform, both built by the repo's CI.

* **macOS** — open `SCM Workbench.app`. If the system warns that it is unsigned, right-click → *Open* once.
* **Windows** — unzip and run it (one-time SmartScreen prompt).

That's the whole install. The app puts nothing on your machine: no Python, no packages, no terminal. It keeps everything in its own per-user data folder (`~/Library/Application Support/scm-workbench` on macOS, `%LOCALAPPDATA%\scm-workbench` on Windows) — a private Python runtime it provisions itself, your settings, job history, and working copies of the two repos.

On first launch a window opens immediately, and the setup work (fetching the newest copy of each repo, installing the scripts' dependencies into the private runtime) proceeds in the background with live progress in the UI. silhouette-card-maker tracks its latest release; scm-extras tracks its main branch, since it publishes no releases. **Settings → “Managed repo copies”** redoes any of it on demand, or pins a copy to a specific tag or release. Prefer your own clones? Point **Settings → repos** at them and the managed copies simply stay unused.

**Updating the app** works the same way: it checks GitHub for a newer release at start-up and once a day (Settings → “App updates” shows the button, the last-checked time, and the outcome). When a new release is out, the button becomes *Download & install*. The install replaces the app only — your data folder (settings, images, decklists) is never touched — and the app reopens itself as the new version, keeping the old one as a backup. Until the release repo is made public the check can't see its releases, and the card says so plainly; once it's public, no setup is needed at all.

## What you get

| Page | What it does |
| --- | --- |
| **Dashboard** | repo status, quick actions, the cards-per-page matrix, recent jobs |
| **Fetch card art** | the card-art fetchers for every game: a decklist from a file (with the OS file picker), pasted text, or a URL — including the MTG preferences |
| **Create PDF** | print-ready PDFs from your card images: sizes, registration, specialty layouts, fit & crop, PPI, quality, borderless and more |
| **Offset & calibration** | calibration sheets, print-offset measurement, and a per-paper-size offset table |
| **Cutting templates** | DXF and `.studio3` templates, single or batch, plus a gallery of everything in the repos |
| **Extras: MTG & Sorcery** | scm-extras' extra card families and their template gallery |
| **Sizes & layouts** | every card and paper size as scaled silhouettes, and the cards-per-page matrix |
| **Utilities** | clear the card-image folders, a size converter, a list of all sizes |

Everything runs as a tracked job: a live log, a stop button, a persistent history, and — before you run — a **command preview** of the exact command that will run. The preview doubles as a sanity check: the Create PDF button stays disabled, with a reason, while the front-images folder is empty.

**Simple or Advanced** (the switch at the bottom of the sidebar, on every page). Advanced is the full set of options above. Simple is the essentials only: the navigation keeps Fetch card art, Create PDF and Settings, and the Create PDF form shrinks to the choices you actually use — card size and paper size on one row, the borderless / apply-saved-offset / front-only toggles on the next — with everything else at its defaults, so a simple run and an advanced run with the same visible settings produce the same command. The “Apply saved offset” switch is enabled only when an offset is saved for that paper size.

**Extras auto-wiring.** Using an scm-extras size such as `standard_mtg` normally requires exporting `SCM_EXTRA_LAYOUTS` by hand. The Workbench does it for you the moment you pick such a size, and the preview says so.

## Behaviors worth knowing

* **Offsets are per paper size.** silhouette-card-maker keeps one shared X/Y/angle correction, but the value you need depends on the paper you feed it. The Workbench keeps its own per-paper table and, before any run that uses an offset, stages the matching row into the repo's file — the repo's code is never modified.
* **Front pages only** refuses to work while the double-sided folder still holds images. Flipping the toggle with images present warns you and offers to clear the folder in one click (non-image files such as `README.md` are left alone).
* **Decklists.** Pasted text is saved into the repo's decklist folder under a sanitized name. You can also pick an existing decklist from anywhere on disk with the OS file dialog; it's copied in for you.
* **One deliberate gap.** The repos' Silhouette Studio automation script (`dxf_to_studio3.py`) drives a Windows-only GUI and is not wrapped. Open the `.studio3` files the app generates and runs instead.
* **Factory reset.** Delete the data folder and the Workbench is back to first launch.

## Development

Running from source, packaging the app, and cutting releases are documented in [CONTRIBUTING.md](CONTRIBUTING.md).
