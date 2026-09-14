# SCM Workbench

<p align="center">
  <img src="screenshot.gif" alt="The Fetch card art page, Simple mode first, then Advanced - the switch is at the bottom of the sidebar" width="800">
</p>

A local interface for [silhouette-card-maker](https://github.com/Alan-Cha/silhouette-card-maker) and [scm-extras](https://github.com/Alan-Cha/scm-extras), the two Python repos for making card games on Silhouette cutting machines.

It wraps those repos' scripts in a normal app: pick options in a form, see the exact command that will run, press run, and watch the job in a live log. No terminal, no memorized flags, nothing to install.

## Getting the app (macOS / Windows)

Download the latest release from the [releases page](https://github.com/mallen86/scm-workbench/releases). CI builds a macOS ARM64 installer and a portable Windows x64 package.

* **macOS ARM64** — open `scm-workbench-macos.dmg`, then drag **SCM Workbench** onto the **Applications** icon in the installer window. Eject the disk image and open the installed app from Applications. The app is ad-hoc signed rather than notarized, so the first launch is gated by macOS: if it says the app *couldn't be opened* or is *from an unknown developer*, go to **System Settings → Privacy & Security**, scroll to the bottom, and click **Open Anyway**. That's it — the allow is remembered by the machine, though each new release gets its own (one more *Open Anyway*). If the window opens but stays on *Starting the SCM Workbench…* or shows a worker error, follow the diagnostic steps in [docs/macos-first-run.md](docs/macos-first-run.md). The embedded WebView no longer needs Local Network permission.
* **Windows x64** — unzip and run it (the current build is unsigned, so Windows may show a one-time SmartScreen prompt).

That's the whole install. The app puts nothing on your machine: no Python, no packages, no terminal. It keeps everything in its own per-user data folder (`~/Library/Application Support/scm-workbench` on macOS, `%LOCALAPPDATA%\scm-workbench` on Windows) — a private Python runtime it provisions itself, your settings, job history, and working copies of the two repos.

On first launch a window opens immediately, and the setup work (fetching the newest copy of each repo, installing the scripts' dependencies into the private runtime) proceeds in the background with live progress in the UI. silhouette-card-maker tracks its latest release; scm-extras tracks its main branch, since it publishes no releases. **Settings → “Managed repo copies”** redoes any of it on demand, or pins a copy to a specific tag or release. Prefer your own clones? Point **Settings → repos** at them and the managed copies simply stay unused.

**Updating the app** works the same way: it checks GitHub for a newer release at start-up and once a day (Settings → “App updates” shows the button, the last-checked time, and the outcome). When a new release is out, the button becomes *Download & install*. The install replaces the app only — your data folder (settings, images, decklists) is never touched — and the app reopens itself as the new version, keeping the old one as a backup. Until the release repo is made public the check can't see its releases, and the card says so plainly; once it's public, no setup is needed at all.

## What you get

| Page | What it does |
| --- | --- |
| **Job history** | every run, live and from earlier sessions; click one to reopen its page with the exact settings it ran with |
| **Fetch card art** | the card-art fetchers for every game, with recently used games kept at the top and the full catalog tucked away after the first run: use a decklist file, pasted text, or a URL |
| **Create PDF** | print-ready PDFs from your card images, with a low quality representative first-page preview before you run: sizes, registration, specialty layouts, fit and crop, PPI, quality, borderless and more |
| **Offset & calibration** | calibration sheets, print-offset measurement, and a per-paper-size offset table |
| **Cutting templates** | DXF and `.studio3` templates, single or batch, plus a gallery of everything in the repos |
| **Extras: MTG & Sorcery** | scm-extras' extra card families and their template gallery |
| **Sizes & layouts** | every card and paper size as scaled silhouettes, and the cards-per-page matrix |
| **Utilities** | clear the card-image folders, a size converter, a list of all sizes |

Everything you run becomes a tracked job with a live log, a stop button, and persistent history. Advanced mode also shows a **command preview** of the exact command that will run. Simple Create PDF replaces that technical box with a concise readiness summary, so warnings, validation errors, and the reason a run is blocked remain visible. The sidebar carries a **Documentation** link, marked with the opens-in-your-browser arrow, to the silhouette-card-maker docs site in both interface modes.

**Simple or Advanced** (the switch at the bottom of the sidebar, on every page). Advanced is the full set of options above. Simple is the essentials only: the navigation keeps Job history, Fetch card art, Create PDF, Offset & calibration and Settings, and the Create PDF form shrinks to the choices you actually use, with everything else at its defaults. The “Apply saved offset” switch is enabled only when an offset is saved for that paper size.

**Extras auto-wiring.** Using an scm-extras size such as `standard_mtg` normally requires exporting `SCM_EXTRA_LAYOUTS` by hand. The Workbench does it for you the moment you pick such a size, and the preview says so.

## Behaviors worth knowing

* **Offsets are per paper size.** silhouette-card-maker keeps one shared X/Y/angle correction, but the value you need depends on the paper you feed it. The Workbench keeps a canonical global baseline plus per-paper rows in its data folder and projects the matching row into the repo's `data/offset_data.json` under the serialized offset lease — the repo's code is never modified. Atomic replacement and rollback recover the projection after a crash; deleting the staged row restores the global baseline.
* **Repository metadata and app updates are asynchronous in the packaged app.** Ref lists, source selection, repository checks, release checks, and release notes use native IPC starts followed by bounded polls. Remote GitHub work runs in bounded worker registries instead of blocking the serialized transport; downloading/updating a managed copy and transactional self-install remain their existing jobs. A browser uses the existing HTTP routes, while a packaged native failure is shown rather than silently retried over HTTP.
* **Front pages only** refuses to work while the double-sided folder still holds images. Flipping the toggle with images present warns you and offers to clear the folder in one click (non-image files such as `README.md` are left alone).
* **The Create PDF image is a representative preview, not a print proof.** Workbench samples at most 16 fronts, renders only private low resolution copies with the unchanged upstream `create_pdf.py`, and displays the first front page as a bounded JPEG. Preview work is cancellable and disposable. It does not modify card folders, normal output, jobs, history, logs, or export permissions.
* **Decklists, card backs, and generated PDFs.** Pasted text is saved into the repo's decklist folder under a sanitized name. You can also pick an existing decklist from anywhere on disk with the OS file dialog. The Create PDF form keeps card-back status and actions beside the selected back folder in Advanced mode and immediately before the run action in Simple mode. Its picker imports one recognized image into `game/back`, replacing prior recognized backs while preserving placeholders and other files; another managed folder selected in Advanced mode is inspected and revealed without pretending that the default-folder picker writes there. Returning to the app rechecks the selected folder, so an image removed in Finder or Explorer disappears from the status. The default-folder control can also remove recognized card backs after confirmation while leaving placeholders and other files alone. In the packaged app, dedicated native commands own the decklist and card-back import dialogs and the “Move to my files…” save dialog. PDF export uses short-lived opaque grants tied to successful job snapshots, so the WebView cannot choose a source path or reuse the dialog as a general filesystem capability.
* **Card art progress shows real numbers only when the fetcher reports them.** The MTG fetcher over an MPCFill XML decklist announces its own prefetch total, so that run counts up through two named stages. Every other fetcher prints one line per decklist entry without ever printing a total, so the bar stays indeterminate rather than guessing a percentage from a decklist it did not parse. Create PDF always shows real numbers because the app counts the front images itself.
* **One deliberate gap.** The repos' Silhouette Studio automation script (`dxf_to_studio3.py`) drives a Windows-only GUI and is not wrapped. Open the `.studio3` files the app generates and runs instead.
* **Factory reset.** Delete the data folder and the Workbench is back to first launch.

## Development

Running from source, packaging the app, and cutting releases are documented in [CONTRIBUTING.md](CONTRIBUTING.md). Packaged Tauri embeds only the bounded `ui/` tree and serves `index.html` plus its root-relative assets from the app origin; its WebView never navigates to the worker's loopback HTTP listener. The packaged worker uses only native JSON-lines IPC; standalone browser flows retain HTTP when launched without `--ipc`. The current native boundary, including bounded `settings.set`, offsets, OS actions, secure image deletion, decklist and card-back import, artifact export, asynchronous repository metadata, and app update/release-notes IPC, is documented in [docs/native-ipc.md](docs/native-ipc.md). Browser fallback and raw bytes remain standalone-browser HTTP compatibility; packaged `/api/file` is unavailable because the UI has no raw-file caller. Standalone browsers retain POST `/api/fs` while packaged image deletion uses native IPC only. The packaged smoke guard uses native IPC markers as rendered-UI evidence, rejects WebView HTTP requests, and asserts port 8038 is closed; standalone browser HTTP remains available when launched without `--ipc`.
