# AGENTS.md

This file is the fast-start guide for coding agents working on SCM Workbench. Read it before changing code, then consult the linked documentation for the area you are touching.

## Start here

1. Run `git status --short --branch` and preserve all existing user changes.
2. Check whether the branch is behind its remote before planning work. Never reset, overwrite, or rebase a dirty checkout without explicit approval.
3. Read `TODO.md` for the active request. Mark an item `[x]` only after its implementation and regression coverage pass.
4. Read `CONTRIBUTING.md` for setup, verification, packaging, and release details.
5. For native/transport work, read `docs/native-ipc.md` before editing.
6. For macOS first-run or packaging behavior, also read `docs/macos-first-run.md` and `.github/workflows/package.yml`.

## What this repository is

SCM Workbench is a Python application with two frontends:

- **Packaged app:** a Rust/Tauri shell embeds `ui/`, starts a bundled Python worker, and communicates over JSON-lines native IPC.
- **Source development:** Python serves the same UI in a normal browser over loopback HTTP, normally at `http://127.0.0.1:8037`.

The app manages and launches tools from the sibling `silhouette-card-maker` and `scm-extras` repositories. Those repositories remain upstream owners of their scripts; do not copy their implementations into this repository or import their code into the Workbench process.

## Repository map

- `scm_workbench/server.py` — manifest, argument normalization, command construction, jobs, HTTP compatibility routes, and shared application behavior.
- `scm_workbench/ipc.py` — bounded native RPC validation and dispatch.
- `scm_workbench/repo_sync.py` — secure managed-repository download, verification, update, locking, and user-data preservation.
- `scm_workbench/updater.py` — release metadata, app download, extraction, and transactional update handoff.
- `scm_workbench/bootstrap.py` — packaged first-launch managed-repository setup.
- `ui/index.html`, `ui/theme.css` — app shell and design system.
- `ui/js/core.js` — DOM helpers, icons, shared state, API helpers, toasts, and modals.
- `ui/js/forms.js` — manifest-driven forms, previews, and job starts.
- `ui/js/jobs.js` — native/browser job transport facade and stream subscriptions.
- `ui/js/transport.js` — native capability selection and browser HTTP compatibility.
- `ui/js/nav.js` — routing plus Simple/Advanced mode behavior.
- `ui/js/pages/` — page renderers.
- `tauri/src/` — native shell, worker RPC, lifecycle, and update helper.
- `tests/` — Python unit/integration/security tests.
- `scripts/check_ui_*.py` — executable frontend and transport contracts, usually backed by Node.
- `.github/workflows/package.yml` — canonical packaging and release workflow.

## Core architecture rules

### Manifest and commands

`server.py` owns one manifest for job options. The frontend renders forms from that manifest, and the backend builds the exact command that runs. When adding or changing an option:

1. Update the manifest and command builder together.
2. Preserve preview/run parity.
3. Add or update Python tests and the relevant `scripts/check_ui_*.py` contract.
4. Check both Simple and Advanced modes.

Do not hard-code a second option schema in the UI.

### Native and browser transports

Packaged windows must use native RPC; source-browser mode retains HTTP compatibility.

- Put transport selection in the relevant facade (`jobs.js`, `*-transport.js`, or a new equivalent), not in page code.
- Once a callable native bridge is selected, a native failure is final. **Never silently retry it over HTTP.**
- Keep native and HTTP request validation, application errors, and result shapes equivalent.
- Pages should call transport facades rather than `/api/...` or `wb_rpc` directly.
- Native RPC operations must be bounded in input size, output size, counts, time, and polling retries.

### IPC stdout is reserved

The packaged Python worker uses stdout for JSON-lines protocol responses. Do not add ordinary prints to stdout on the IPC path. Route diagnostics through the existing logging/stderr mechanisms so one stray line cannot corrupt the native protocol.

### Security and filesystem boundaries

This app downloads archives, opens files, deletes images, exports artifacts, and replaces application bundles. Treat every path, URL, archive member, response header, and native parameter as untrusted.

Maintain the existing protections for:

- canonical path containment and symlink rejection;
- bounded reads, downloads, metadata, logs, and directory listings;
- approved HTTPS origins and redirect hosts;
- archive traversal, duplicate entries, special files, and platform path spelling;
- atomic staging/publication and rollback;
- managed-repository user data (`game/`, decklists, images, output, and offsets);
- capability/grant-based artifact export.

Do not weaken a validation check merely to make a fixture pass. Add a portable normalization at the correct trust boundary and test hostile cases.

### Frontend conventions

The UI is vanilla JavaScript with native ES modules and no bundling step.

- Build DOM with `el()`, `$()`, and `$$()` from `core.js`; do not introduce a framework for a local change.
- Shared mutable UI state lives in `S`.
- Page renderers are registered in `PAGES` and are rebuilt by `go()`.
- If a page needs post-insertion DOM work, attach a `wrap.__patch` callback.
- Preserve form objects across refreshes when the user is editing (`refreshInfo({ keepForms: true })`).
- Long-running work belongs in jobs; keep page state correct across navigation and terminal job refreshes.
- In Simple mode, the job console is hidden, so any essential status or completion action needs an in-page equivalent.
- Test responsive layout and both themes when changing CSS.

## Local development

Use Python 3.13, Node.js, Rust, and `uv`.

```sh
uv venv
uv sync
python -m scm_workbench
```

A direct browser-server entry point is also available:

```sh
python -m scm_workbench.server --port 8037
```

To exercise the packaged asset origin and native IPC from a source checkout without bootstrapping large repositories:

```sh
(cd tauri && \
  SCM_WORKBENCH_DATA="$(mktemp -d /private/tmp/scm-workbench-source.XXXXXX)" \
  SCM_WORKBENCH_NO_BOOTSTRAP=1 \
  SCM_WORKBENCH_PYTHON="$(command -v python3)" \
  cargo run --locked --features custom-protocol)
```

The app auto-detects sibling folders named `silhouette-card-maker` and `scm-extras`. Other locations can be configured in Settings.

## Verification

Run focused tests while iterating. Before handing off a cross-cutting change, run the complete relevant suite.

### Python and frontend contracts

```sh
python -m unittest discover -s tests -v
python -m unittest tests.test_update_handoff -v

python scripts/check_ui_imports.py
python scripts/check_ui_transport.py
python scripts/check_ui_decklists.py
python scripts/check_ui_jobs.py
python scripts/check_ui_repos.py
python scripts/check_ui_onboarding.py
python scripts/check_ui_preview.py
python scripts/check_ui_artifacts.py
python scripts/check_ui_native_actions.py
python scripts/check_ui_save.py
python scripts/check_ui_settings.py
python scripts/check_ui_offsets.py
python scripts/check_ui_updates.py
python scripts/check_ui_fs_delete.py

find ui/js -name '*.js' -print0 | xargs -0 -n1 node --check
```

When adding a UI behavior, extend the closest existing `check_ui_*.py` contract rather than relying only on visual inspection. Backend behavior should also have a real unit/integration test where practical.

### Rust/Tauri

Keep Cargo output outside the checkout when possible:

```sh
(cd tauri && cargo fmt --check)
(cd tauri && CARGO_TARGET_DIR="${TMPDIR:-/tmp}/scm-workbench-tauri-target" cargo test)
(cd tauri && CARGO_TARGET_DIR="${TMPDIR:-/tmp}/scm-workbench-tauri-target" cargo check --features custom-protocol)
```

Run the release build only when the change warrants it:

```sh
(cd tauri && CARGO_TARGET_DIR="${TMPDIR:-/tmp}/scm-workbench-tauri-target" cargo build --release --features custom-protocol)
```

Always compare local verification with the current steps in `.github/workflows/package.yml`; the workflow is canonical.

## Packaging and releases

- Supported release targets are macOS ARM64 and Windows x64.
- macOS uses a drag-to-Applications DMG as both the installer and in-app updater payload; only Windows publishes a ZIP.
- Current artifacts are intentionally not Developer ID/notarized or OV-signed. Do not change that policy incidentally.
- A `v*` tag is the release version source of truth. `scripts/inject_version.py` synchronizes Python, Tauri, Cargo, and packaging metadata.
- `workflow_dispatch` runs packaging without creating a tagged release; tag pushes build and attach release assets.
- Do not hand-edit generated Tauri schemas unless the generator and workflow require it.

## Change discipline

- Keep changes narrow and preserve unrelated local modifications.
- Add regression coverage for every bug fix, especially if a TODO item was previously marked complete incorrectly.
- Prefer root-cause fixes over timing-only or display-only workarounds.
- Do not commit runtime state or build products from `data/`, `logs/`, `.venv/`, Cargo targets, caches, mounted DMGs, or generated app bundles.
- Update `README.md`, `CONTRIBUTING.md`, or `docs/` when behavior or architecture changes—not for implementation details that are already obvious from code.
- Do not commit, push, tag, publish, or dispatch package workflows unless the user requested those actions.
