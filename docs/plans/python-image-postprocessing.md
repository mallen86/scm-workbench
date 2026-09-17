# Advanced Python image post-processing plan

## Status

Proposed and implemented on 2026-09-16. This document now serves as the design, trust-boundary, and verification record for the implemented feature. Final packaged-app smoke testing is tracked separately from the implementation status.

The workflow is intentionally Advanced-only. It lets a user save or import a Python image processor, install optional Python packages into Workbench-owned storage, and run the processor against fetched front and double-sided card images before Create PDF.

## Decision summary

Implement a managed **Image post-processing** workflow with these core decisions:

- SCM Workbench, not the user script, discovers and orders the images.
- Workbench starts **one isolated child process for the whole batch**, loads the selected module once, and calls one documented `process_image(...)` function once per staged image.
- Do not start a new Python interpreter for every image and do not require scripts to scan or loop over SCM folders.
- Give the callback only a private writable copy of one image and non-sensitive metadata. Do not pass the managed checkout path.
- Stage and validate every result before changing the real image folders. Publish the complete batch through a journaled replace/rollback transaction.
- Store script revisions and dependency environments below Workbench's writable data directory. Never install user packages into the app bundle, worker interpreter, SCM checkout, or the user's system Python.
- Treat scripts and packages as **trusted local code**. Python import restrictions, AST checks, audit hooks, and RestrictedPython are not security sandboxes. Process separation and resource limits protect Workbench and reduce accidental damage, but they cannot make hostile Python safe under the same desktop account.
- Require explicit trust for the exact script revision and dependency lock before it can run. Saving or changing either invalidates that trust.
- Keep execution manual in the first release. An Advanced-mode fetch completion action may take the user to post-processing, but a fetch must never silently execute custom code.

This design combines the safer ownership model of per-image invocation with the performance of one long-lived process.

## Why Workbench should own the image loop

There are three plausible execution models:

| Model | Benefit | Problems | Decision |
| --- | --- | --- | --- |
| User script receives a directory and loops | Smallest Workbench wrapper | Grants broad path authority; each script must rediscover SCM naming rules; progress, cancellation, bounds, and transactions are inconsistent; scripts can accidentally process placeholders or unrelated files | Reject |
| Workbench launches one Python process per image | Strong per-image crash isolation | Repeats interpreter startup, imports, model loading, and package initialization; makes ML-backed processors especially slow; complicates aggregate state | Reject |
| Workbench launches one process and invokes a callback per staged image | Centralized discovery, bounds, progress, cancellation, and commit; imports and models load once; module state may be reused | One bad callback ends the batch, so all outputs must remain staged until success | Select |

A local directional benchmark on 2026-09-16 used 20 tiny Pillow operations. Twenty interpreter launches and Pillow imports took 0.723 seconds in total, while one interpreter importing Pillow once and performing all 20 operations took 0.038 seconds, about 19 times faster for that deliberately tiny workload. Real image transforms will reduce that ratio, while processors that initialize a model may increase it. The benchmark supports avoiding per-image process startup; it is not a product performance guarantee.

The selected contract is therefore:

1. the trusted Workbench worker performs the bounded scan and creates private copies;
2. one child imports the processor module once;
3. the trusted runner loops over a deterministic manifest and invokes the callback for each copy;
4. the worker validates and commits only after the runner succeeds for every image.

The user writes the transformation, not traversal, locking, backup, or error-recovery logic.

## Security promise and trust boundary

### What the first release should protect

The implementation must protect these boundaries even for buggy scripts:

- The JSON-lines worker protocol never shares a process or stdin with user code.
- A syntax error, import failure, native-extension crash, timeout, or cancellation cannot crash the Workbench worker.
- A normal failed or cancelled run leaves every original image byte-for-byte unchanged.
- The callback receives a path below a private run directory, never a real `game/front` or `game/double_sided` path.
- Scripts cannot select their own command-line arguments, interpreter, working directory, package index, input directories, or output names.
- No shell parses any user-controlled value.
- Saved scripts, requirements, requests, responses, logs, directory scans, image sizes, dependency environments, run duration, and process output are explicitly bounded.
- Symlinks, reparse points, special files, path traversal, changed identities, and checkout swaps are rejected at each trust boundary.
- A package install or processor run is cancellable and its entire process tree is terminated.
- A processor cannot mutate Workbench's bundled runtime through the supported API.

### What it cannot safely promise

Arbitrary CPython running as the logged-in desktop user is not a secure sandbox. A deliberately malicious processor or imported package could still:

- import `os`, `socket`, `subprocess`, `ctypes`, or another powerful module;
- inspect or modify other files readable by the user's account if it guesses or discovers their paths;
- access the network;
- spawn subprocesses or native code;
- exfiltrate data available to that account;
- attempt denial of service outside the limits the operating system successfully enforces.

Do not claim that AST validation, a module allowlist, Python audit hooks, `-I`, a scrubbed environment, or RestrictedPython changes this fact. RestrictedPython's own documentation says it is not a sandbox or secured environment. Import allowlists are also incompatible with the requested ability to use relatively arbitrary third-party image libraries and are bypassable once native extensions or introspection are available.

The UI must say, before trust is granted:

> Python processors and their libraries run as your user account. Only use code and packages you trust. Workbench limits inputs, resources, and image publication, but it cannot safely sandbox arbitrary Python from your other files or network.

Trust is recorded for the content-addressed tuple `(processor revision, dependency lock, runner contract version)`, not merely for a friendly processor name. Any source edit, requirements edit, dependency update, or contract-version change makes the processor untrusted until the user approves it again.

If running genuinely untrusted code becomes a requirement, stop this design and evaluate an externally managed VM/container or a much narrower declarative image-operation language. That is a different product scope. Pyodide/WASM would also restrict many native image and ML wheels and should not be presented as a drop-in solution.

## User experience

Add an Advanced-only `Image post-processing` item in the Workflow section, after Fetch card art and before Create PDF.

The page has three areas.

### Processor library

A bounded list shows saved processors with:

- name;
- active revision abbreviation;
- trusted/untrusted state;
- dependencies ready or not ready;
- selection plus Duplicate and Delete actions; the selected item is edited, trusted, installed, or run in the adjacent panels.

Deletion needs confirmation and is refused while that processor revision or environment is in use. Deleting a processor removes its saved source after no active job references it; job history retains only bounded name/hash metadata, not source code.

### Editor

Use a native `<textarea>`-based monospace editor in the first release. This keeps the no-build vanilla-JavaScript frontend, avoids adding a large editor framework, and is enough for small processor functions. Add tab insertion, line/column status, `spellcheck=false`, and a clear dirty indicator. Source must always enter the DOM through the `value` property or text nodes, never `innerHTML`.

Fields and actions:

- processor name;
- Python source;
- optional requirements, one validated package request per line;
- Save revision;
- Import `.py`;
- Revert unsaved changes;
- Trust this revision;
- Install/Update libraries;
- Delete processor.

A new processor starts from a small no-op template and explanatory comments. Saving performs structural validation but never imports or executes the script. Navigating away with unsaved edits requires confirmation.

`Import .py` is a convenience, not a separate execution path:

- packaged windows use a parented native file picker and a private worker operation;
- the worker reads one stable regular UTF-8 file with the same source-size limit, rejects links/reparse points and controls, and creates an untrusted draft/revision;
- standalone-browser mode uses a normal file input and sends bounded UTF-8 source content, never a browser-supplied path;
- once a native bridge is selected, a native failure is final and never falls back to HTTP.

### Run panel

The run form is manifest-driven and contains:

- processor;
- scope: `Front and double-sided` (default), `Front only`, or `Double-sided only`;
- the bounded image count that will be processed;
- the normal command preview and Run action.

Run remains disabled until the exact revision is trusted, its dependency lock is ready for the selected interpreter/platform, the SCM checkout is connected, and the selected scope has recognized images.

During a run, show a real `current / total` progress bar and current basename. On completion, offer `Go to Create PDF`. On failure, cancellation, or rollback, state explicitly that original images were not changed. If failure happens after publication began, report whether recovery restored the originals; never show success until the durable transaction is complete.

In Advanced mode, a successful Fetch card art job may also show `Post-process images`. It navigates to this page with `Front and double-sided` selected. It must not automatically run a processor. If no processor exists, the action says `Set up post-processing` instead.

The page and nav item are absent in Simple mode. Direct navigation redirects as other hidden pages do. The backend also rejects new post-processing, package-install, trust, import, and edit operations while `ui_mode` is `simple`; changing mode while an existing job runs does not kill it, and the global job notification/history still reports its outcome.

## Processor contract

### Required callable

Each saved module must contain a normal synchronous function named `process_image`:

```python
from pathlib import Path


def process_image(image_path: Path, context: dict) -> None:
    """Modify the private working copy at image_path in place."""
    # Example: open image_path, transform it, and save back to image_path.
```

The runner passes:

```python
context = {
    "role": "front",                 # or "double_sided"
    "relative_path": "game/front/Card Name.jpg",
    "name": "Card Name.jpg",
    "index": 1,                      # one-based across this run
    "total": 73,
}
```

Contract rules:

- `image_path` is a private writable regular-file copy below the run directory.
- The module is imported once per job. Module globals may cache a model or session across callbacks.
- Workbench calls the function sequentially in deterministic natural-name order: fronts first, then double-sided images.
- The function must return `None`; arbitrary return values are rejected to avoid an accidental second protocol.
- The function may modify or replace the staged file at the same path.
- It may not rename it, add a result, delete another result, or select a real destination.
- The output must remain a recognized, decodable image of the same file format and within output bounds. Dimensions may change within configured pixel limits.
- An exception fails the whole job. No real image is changed.
- Async callbacks, per-image custom options, batch directory callbacks, pre/post hooks, and parallel callback execution are deferred.

Module-level initialization is the first-release answer for expensive model loading. A later contract version may add explicit `start_batch(context)` and `finish_batch(context)` hooks, but only with versioning and tests. Scripts that require a global analysis pass over all image pixels need a future bounded batch API; they must not be given SCM directories as a shortcut.

### Save-time validation

Saving a revision performs only non-executing validation:

- valid UTF-8, no NUL, and at most 256 KiB;
- parse with `ast.parse`/`compile` without executing;
- exactly one top-level function definition named `process_image` with a supported signature;
- no duplicate processor ID or stale compare-and-set revision;
- bounded portable processor name and metadata.

Do not reject ordinary Python imports or function bodies under the pretense that this creates a sandbox. The required callable check is an API/usability rule, not a security boundary.

## Storage model

Keep all state under `DATA_DIR / "postprocessing"`, outside both managed repositories and outside the signed/bundled runtime:

```text
postprocessing/
  processors/
    <random-id>/
      metadata.json
      revisions/
        <revision-sha256>.py
        <revision-sha256>.json
  environments/
    <environment-fingerprint>/
      ready.json
      requirements.lock
      install-report.json
      site-packages/
  runs/
    <job-id>/
      manifest.json
      work/front/...
      work/double_sided/...
  transactions/
    <job-id>.json
```

Rules:

- IDs are server-generated opaque hex values, never names or paths supplied by the WebView.
- A revision hash covers normalized source bytes, normalized requested requirements, and the runner contract version.
- Revision files are immutable. `metadata.json` selects an active revision through a compare-and-set update.
- Every write uses bounded sibling-temp creation, flush/fsync where the existing durability model requires it, and atomic replacement.
- Reject symlinked/reparse-point components and unsafe permissions. Use private user-only permissions where the platform supports them.
- Initial bounds: 64 processors, 20 retained revisions per processor, 256 KiB source per revision, 8 MiB total saved source, and bounded 512 KiB list/get responses.
- Retain the active revision and revisions referenced by live jobs. Garbage-collect older unreferenced revisions deterministically.
- Runs and failed environment staging directories have a startup cleanup/recovery path and age/size limits.
- Do not put source code in `settings.json`, the global info payload, job records, command previews, or telemetry.

## Optional Python-library installation

### Environment choice

Use a Workbench-managed **target directory**, not a copied virtual environment and not the interpreter's own site-packages.

Reasons:

- Python documents virtual environments as disposable and inherently non-portable because installed scripts contain absolute interpreter paths.
- The packaged interpreter already lives in a signed/read-only app payload on macOS and a shipped runtime on Windows; modifying either makes updates and verification unsafe.
- `pip install --target` can publish packages into a data-directory staging tree.
- A target environment can be keyed to the exact interpreter/platform fingerprint and rebuilt when that fingerprint changes.

The fingerprint must include at least Python implementation/version/ABI, platform/architecture, runner contract version, normalized requested requirements, and the resolved lock hash. Identical locks may share one immutable environment across processors. A job pins the fingerprint for its lifetime, and garbage collection never removes an environment with a live reference.

### Accepted requirements

The UI does not accept an arbitrary pip command. Initially accept only these forms, one per line:

- `distribution-name`
- `distribution-name==version`
- `distribution-name[extra]`
- `distribution-name[extra]==version`

Normalize names and reject duplicate/conflicting requests. Reject URLs, local paths, VCS references, requirement-file includes, editable installs, environment markers, hashes supplied as command syntax, index options, control characters, and every line beginning with `-`. Initial limits are 32 direct requests, 256 UTF-8 bytes per line, and 8 KiB total.

A bare name means "resolve the current compatible release now". After resolution, Workbench stores exact transitive versions and SHA-256 hashes. Choosing the explicit `Install / update libraries` action again re-resolves the current compatible wheel set and creates a new untrusted processor/environment tuple if the lock changes.

### Install pipeline

Library installation is a normal cancellable background job, but it uses a package-install resource class rather than the SCM image lease.

The server alone builds the argv for the same interpreter `job_python(settings)` will use for execution. Use a private staging directory and an isolated pip configuration:

1. run pip in isolated, non-interactive mode with a fixed public HTTPS index policy;
2. resolve with `--dry-run --ignore-installed --only-binary=:all: --report`;
3. validate the report, selected hosts, names, versions, wheel compatibility, artifact sizes, and SHA-256 hashes;
4. generate a complete exact lock containing every transitive distribution and hash;
5. download only those wheels to a private cache and verify their hashes;
6. install offline from that cache with `--no-index`, `--require-hashes`, `--only-binary=:all:`, and `--target <staging>/site-packages`;
7. validate the installed tree, quotas, report, and absence of links/special files;
8. atomically publish the immutable environment plus a final `ready.json` marker.

Set `PIP_CONFIG_FILE` to the null device, clear inherited `PIP_*`, proxy, credential, and Python-path variables unless an explicitly reviewed certificate setting is needed, disable stdin and keyring prompts, and never pass user strings as pip options. Do not call `site.addsitedir` at runtime because it executes `.pth` files; insert the validated target directory directly into `sys.path` after the trusted runner has initialized.

Wheel-only installation avoids source builds and their build-time code, compilers, and unbounded temporary work. It means packages without a compatible wheel for the bundled Python/platform fail with an actionable message. Supporting source distributions, custom indexes, local wheels, system package managers, or external executables is deferred and must not be added as a silent fallback.

The package warning remains necessary: a wheel's Python/native code executes when imported, and a correctly hashed package can still be malicious.

### Package resource limits

Start with explicit, testable limits and tune them with real packages before release:

- 15-minute install wall-clock timeout and 5-minute idle/progress watchdog;
- 1 GiB total downloaded wheels per install;
- 2 GiB unpacked size per environment;
- 12 GiB aggregate environment cache before explicit or safe LRU cleanup;
- bounded file count, path depth, component length, and install-report size;
- bounded on-disk job log and bounded individual output records;
- minimum free-space reserve before and during installation.

A failed/cancelled install deletes staging and never changes the prior ready environment. An app or interpreter update marks incompatible environments stale rather than trying to copy or repair them in place.

## Backend execution pipeline

### 1. Validate and pin

At job start, under server-side validation:

- require Advanced mode;
- normalize the manifest arguments;
- load the processor by opaque ID;
- pin its immutable source revision, dependency lock, environment fingerprint, and trust record;
- require an existing compatible ready environment (or the explicit empty environment);
- pin the current SCM root and root identity;
- reject stale UI revisions rather than silently running newer code.

The command preview may show a display-safe command such as `python -I …/postprocess_runner.py --manifest <private>`, but it must not expose source, absolute managed paths, or package credentials. Actual argv remains an array and uses `shell=False`.

### 2. Acquire the correct resource lease

The current generic image-job lease admits real jobs concurrently. Post-processing needs a stronger rule because it writes both front and double-sided images.

Refactor job admission into explicit resource modes:

- image readers: Create PDF and read-only previews;
- image writers: fetch jobs, clear/delete, card-back mutations where applicable, and post-processing;
- repository writers: managed-repository switches/updates;
- package writers: dependency environment publication;
- unrelated jobs: no image lease.

Readers may share only when no image or repository writer exists. Image writers are exclusive against readers, other image writers, previews, deletion, and repository mutation. Package installation does not block SCM image work. Admission remains non-blocking and returns the existing clear busy response rather than waiting through an RPC deadline.

Hold the post-processing image-writer lease from source preflight until commit/cleanup completes. This prevents Workbench-mediated fetch, Create PDF, deletion, preview, or repo-switch races. External filesystem edits are still detected by identity revalidation.

### 3. Discover a bounded image set

Only scan the effective managed SCM checkout's immediate:

- `game/front` directory;
- `game/double_sided` directory when selected.

Do not include `game/back`, recurse, follow links, or accept arbitrary directories in the first release. Preserve non-image placeholders and unrelated files.

Reuse the existing magic-byte image recognition and stable-handle/path-containment patterns. Initial bounds should align with existing image operations unless tests justify lower values:

- at most 8,192 entries scanned per directory;
- at most 1,024 recognized images in one run;
- at most 64 MiB per input file;
- at most 8 GiB aggregate input bytes;
- at most 4,096 UTF-8 bytes per path and 255 per name;
- regular files only, with no symlink/reparse-point directory component;
- bounded decoded dimensions/pixel count, with decompression-bomb warnings treated as errors.

Record a stable identity, size, timestamps/change time as appropriate for the platform, recognized format, dimensions, and cryptographic digest for every candidate. Sort deterministically.

### 4. Stage private working copies

Check free space, create a private `runs/<job-id>` tree, and copy each source from a stable open handle. Recheck identity before and after every copy. Never hardlink or symlink a real image into staging.

Keep the original basename under role-specific private directories so common image libraries infer the right format, but derive all paths from the trusted manifest. Set the runner's working directory, temporary directory, and synthetic home below this run tree. The manifest contains only private paths and bounded metadata.

### 5. Spawn one constrained runner

Launch the trusted `scm_workbench/postprocess_runner.py` with the pinned job interpreter:

```text
python -I -u -X utf8 <trusted-runner> --manifest <private-manifest>
```

Requirements:

- `stdin=DEVNULL`, `stdout` and `stderr` captured through a bounded reader, `shell=False`;
- a new process group/session so cancellation, timeout, app shutdown, and update handoff kill descendants;
- a minimal allowlisted environment with UTF-8 settings and no inherited secrets, `PYTHONPATH`, repository variables, cloud tokens, or proxy credentials;
- the immutable dependency target inserted only inside the child;
- no SCM checkout path in argv, cwd, environment, or callback context;
- wall-clock deadline and idle/progress watchdog;
- POSIX hard resource limits set by the trusted runner before importing user code, including CPU, address space where effective, file size, and open files; callback descendants remain in the job's process group and are terminated before validation;
- a per-job Windows Job Object with kill-on-close plus tested memory/process-count limits, nested safely under the shell's existing worker Job Object;
- continuous private-run size/free-space monitoring on both platforms.

Resource controls are defense in depth, not a sandbox. Some limits vary by OS and native libraries. Platform tests must prove the behavior actually enforced; unsupported limits must be disclosed rather than assumed.

The trusted runner imports the pinned module once, verifies the callable again, and invokes it sequentially for each manifest entry. It emits one runner-owned, machine-prefixed progress record after each successful callback and bounded human-readable errors. User output is drained without allowing an unterminated line or log spam to consume unbounded memory or disk.

### 6. Validate every result

A zero child exit code is necessary but not sufficient. Before publication, the worker independently verifies:

- every expected staged path exists exactly once;
- there are no missing, renamed, linked/reparse-point, directory, device, or unexpected result objects;
- each output is within per-file and aggregate bounds;
- magic bytes and a bounded Pillow decode/verify succeed;
- decoded dimensions/pixel count are within limits;
- output format matches that input's format;
- the runner did not alter its pinned source, environment, or manifest identities;
- every original and the SCM root still match their preflight identities.

Skip publication for byte-identical outputs so their timestamps and identities remain unchanged.

### 7. Publish transactionally

Multi-file replacement is not one filesystem-atomic operation, so use a durable journal and rollback design rather than claiming otherwise.

For each changed image:

1. create an unpredictable hidden quarantine directory on the same filesystem as its destination;
2. write/fsync the transaction journal before destructive steps;
3. revalidate the destination and parent by stable handles;
4. atomically move the original into quarantine;
5. atomically publish a validated sibling temporary copy of the staged output without following links;
6. update the journal phase.

Only after all replacements succeed should the transaction be marked committed and quarantined originals removed. If any publication fails, restore every moved original and remove every newly published output. Cancellation requested after publication starts is deferred until commit or rollback reaches a consistent terminal state.

Recover incomplete journals synchronously at worker startup, before accepting jobs, previews, deletion, or repository mutation. Recovery must be idempotent and identity-checked. Keep POSIX descriptor-relative operations and Windows stable-handle/reparse-point checks separate where platform semantics differ; include stale Windows directory timestamp cases in regression tests.

The job becomes `done` only after commit and cleanup. A runner success followed by validation/commit failure is a failed job, not a partial success.

## Job, history, and progress integration

Add two backend job kinds:

- `postprocess_images` for actual image processing;
- `postprocess_dependencies` for resolution/install/rebuild.

`postprocess_dependencies` may be an internal manifest-backed form action rather than a standalone nav workflow, but it must use normal job cancellation/logging and a non-image resource policy.

Persist only bounded metadata in job history:

- processor ID, display name, revision hash, environment fingerprint;
- scope and image total;
- status, duration, warnings, and ordinary bounded job fields.

Never persist source or full install reports in job records. Restoring an old history row opens the post-processing page. It may preselect only an existing exact revision; it must not silently substitute the processor's newer active revision. If that revision is gone/stale/untrusted, explain why it cannot be rerun.

Extend the progress parser for runner-owned records and keep ordinary user stdout as console text. Progress counts successful callbacks, not merely started callbacks. A validation/commit phase follows 100% processing and must have its own visible label so users do not see a false early completion.

Add job finalization hooks rather than allowing the user-code child to publish directly:

- prepare/stage before spawn;
- finalize/validate/commit in the worker's pump path after child exit;
- cleanup/release in one `finally` path for success, failure, cancellation, spawn failure, update quiescing, and app shutdown.

## Transport and IPC contract

Processor management needs a purpose-specific facade, for example `ui/js/postprocess-transport.js`. Page code must not call `/api/...`, `wb_rpc`, or native invoke directly.

Public native RPC and HTTP compatibility operations should be equivalent and bounded:

- list processor summaries;
- get one processor's source/requirements by ID;
- save a new compare-and-set revision;
- duplicate a processor;
- trust/untrust an exact revision/environment tuple;
- delete a processor;
- inspect compatible environment status and disk use.

Use exact parameter objects, opaque IDs, source/response caps below the global 1 MiB IPC line limit, and deterministic result shapes. Mutations require an expected active revision so two windows or stale page state cannot overwrite newer edits.

Actual processing and dependency installation continue through the existing `jobs.start`, poll/stream, and stop contracts. Add dynamic processor choices to the backend manifest/info response using summaries only; never send source globally. Saving/trusting/install completion invalidates the appropriate manifest/info caches while preserving active form objects.

The packaged import convenience is a separate no-argument Tauri command such as `wb_postprocessor_import`, analogous to decklist and card-back imports. It opens a parented `.py` picker and privately sends the selected path to a worker method that public `wb_rpc` rejects. Cancellation returns `null`; application failures are bounded; bridge failures reject without HTTP fallback.

Document all new methods, exact shapes, limits, private/public separation, and lifecycle behavior in `docs/native-ipc.md` during implementation.

## Manifest and frontend integration

Add `postprocess_images` to the server-owned manifest. The backend remains the source of option validation and argv construction. Mark the kind Advanced-only and enforce that server-side in both preview and start paths.

Frontend work should follow existing conventions:

- `ui/index.html`: Advanced-only nav item with `data-simple-hide`;
- `ui/js/nav.js`: title/route mapping, but do not add the page to `SIMPLE_PAGES`;
- `ui/js/app.js`: import the page module;
- `ui/js/pages/postprocess.js`: page renderer and `__patch` behavior;
- `ui/js/postprocess-transport.js`: native/browser selection and CRUD/import facade;
- `ui/js/forms.js`: only generic support needed for dynamic manifest choices/readiness, not a second option schema;
- `ui/js/pages/fetch.js`: Advanced-only completion navigation action;
- `ui/js/job-history.js`: exact revision-aware page restoration;
- `ui/theme.css`: responsive editor/list/progress layout in both themes.

Essential install/run status must remain in-page even though Advanced mode also has the console. Preserve editor objects and dirty source across unrelated `refreshInfo({keepForms: true})` updates. Terminal job refreshes must update environment readiness and image state without replacing unsaved text.

## Implementation phases

Do not expose a partially safe version. Keep the route behind a development feature flag until phases 1 through 5 and their security tests pass.

### Phase 0: threat model and executable contract

- Turn the security promise above into constants and code-level invariants.
- Prototype one-process callback loading with the packaged Python on macOS ARM64 and Windows x64.
- Verify Pillow, a pure-Python added package, and a compatible native-wheel package from a data-directory target.
- Verify process-tree cancellation and realistic memory/time controls on both release targets.
- Confirm environment publication does not modify any byte under the app/runtime bundle.
- Record benchmark methodology and choose tested initial quotas.

Exit criterion: supported and unsupported isolation guarantees are documented, and both platforms can enforce the minimum process lifecycle controls.

### Phase 1: processor registry, editor, and transports

- Add a focused `scm_workbench/postprocessing.py` module for storage, validation, trust, environment metadata, and recovery helpers rather than expanding every concern inline in `server.py`.
- Implement immutable revisions, compare-and-set metadata, caps, corruption handling, and cache invalidation.
- Add exact IPC/HTTP CRUD methods and the frontend transport facade.
- Add the Advanced-only page, editor, dirty-state protection, trust warning, and no-op template.
- Add native/browser `.py` import with purpose-specific validation.

Exit criterion: users can safely save/import/edit/version/delete processors, but no custom code can execute yet.

### Phase 2: staged runner and transactional image publication

- Add `scm_workbench/postprocess_runner.py` as a stdlib-only trusted bootstrap until it imports the chosen environment and processor.
- Add bounded discovery, stable-copy staging, independent output validation, resource classification, progress, cancellation, and finalization hooks.
- Add durable multi-directory publication journals, rollback, and startup recovery.
- Integrate `postprocess_images` into manifest, jobs, history, and the page.

Exit criterion: processors using bundled libraries can run; every injected failure/cancel/crash test leaves originals intact or is recovered on restart.

### Phase 3: dependency resolution and environments

- Implement requirement normalization, dry-run reports, full hash locks, wheel download verification, offline target install, immutable publication, quotas, and cleanup.
- Add `postprocess_dependencies` job behavior and environment readiness UI.
- Pin environments to interpreter/platform and invalidate them after relevant changes.
- Add explicit update/rebuild/remove actions and trust invalidation.

Exit criterion: optional packages work without mutating the worker/app/system environment, and hostile requirements/pip configuration cannot become CLI options or alternate code sources.

### Phase 4: workflow integration and polish

- Add fetch completion navigation, Create PDF completion navigation, responsive/themed UI, actionable errors, and job-history restoration.
- Preserve unsaved editor state across background refreshes.
- Add docs and user-facing examples for Pillow, OpenCV-style APIs when available, and model caching through module globals.
- Add disk-use visibility and safe environment cleanup.

Exit criterion: the complete workflow is understandable without the console and remains absent from Simple mode.

### Phase 5: adversarial review and release gating

- Run hostile path, archive/wheel, output-spam, fork, resource exhaustion, symlink/reparse race, checkout-switch, crash-recovery, and rollback fault injection tests.
- Run packaged macOS and Windows smoke tests using the actual bundled Python.
- Review dependency supply-chain messaging and confirm no security text overstates isolation.
- Remove the feature flag only after all acceptance criteria pass.

## Expected files

Likely new files:

- `scm_workbench/postprocessing.py`
- `scm_workbench/postprocess_runner.py`
- `ui/js/postprocess-transport.js`
- `ui/js/pages/postprocess.js`
- `tests/test_postprocessors.py`
- `tests/test_postprocess_jobs.py`
- `tests/test_ipc_postprocessors.py`
- `scripts/check_ui_postprocessing.py`

Likely modified files:

- `scm_workbench/server.py`
- `scm_workbench/ipc.py`
- `ui/index.html`
- `ui/js/app.js`
- `ui/js/nav.js`
- `ui/js/forms.js`
- `ui/js/jobs.js`
- `ui/js/job-history.js`
- `ui/js/pages/fetch.js`
- `ui/theme.css`
- `tauri/src/main.rs`
- `tauri/capabilities/default.json`
- `docs/native-ipc.md`
- `README.md` or user documentation describing trusted custom code
- `.github/workflows/package.yml` for packaged smoke coverage if needed

Do not hand-edit generated Tauri schemas except through the repository's normal generation/workflow requirements.

## Test plan

### Registry and source validation

Cover:

- UTF-8/NUL/source/name/count/response limits;
- syntax errors, missing/duplicate/unsupported callable signatures;
- portable ID/name handling and case collisions;
- stale expected revisions and concurrent saves;
- atomic-write failures and corrupt/truncated metadata;
- symlink/reparse-point processor directories;
- trust invalidation on every source, requirement, lock, interpreter, or contract change;
- no source in info, history, logs, or command previews.

### Requirements and installer

Cover:

- accepted bare/exact names and extras;
- rejection of `-r`, indexes, URLs, VCS, paths, markers, controls, conflicting duplicates, and oversized requests;
- inherited pip config/environment isolation;
- wheel-only resolution, incompatible-wheel failure, report caps, host validation, and SHA-256 mismatch;
- complete transitive hash lock generation and offline `--require-hashes` install;
- cancel/timeout/output-spam/disk-quota cleanup;
- no partial ready marker and atomic environment publication;
- interpreter/platform invalidation and live-reference-safe garbage collection;
- app/runtime tree digest unchanged before and after install.

Use local fixture indexes/wheels in tests. Unit and CI tests must not depend on live PyPI.

### Runner and image transaction

Cover:

- module imported exactly once and callback invoked once per image in deterministic order;
- front-only, double-sided-only, combined, empty, and maximum-count scopes;
- module-global model/session reuse;
- no-op and byte-identical output skipping;
- callback exception, non-`None` return, missing/deleted/renamed output, wrong format, corrupt image, decompression bomb, oversized output, extra files, and native crash;
- timeout, cancellation, child/grandchild termination, stdout without newlines, log spam, and staging quota;
- input/root identity changes and external edits;
- POSIX symlink swaps and Windows reparse/component swaps;
- failure injected before and after every journal/rename/publication step;
- app termination at every transaction phase followed by idempotent startup recovery;
- rollback preserves exact original bytes and unrelated files;
- writer lease conflicts with fetch, Create PDF, preview, deletion, back-image mutation, and repo update while package install remains independent.

### IPC, HTTP, and native boundary

Cover:

- exact request keys/types and all size/count limits;
- equivalent public native and browser result/error shapes;
- private import method rejected by public `wb_rpc`;
- parented picker cancellation and path cap;
- stable regular-file import and source replacement races;
- native failure never retries over HTTP;
- simple-mode mutation/preview/start rejection;
- update quiescing and worker shutdown clean every process/stage/lease.

### Frontend contracts

Extend a focused `scripts/check_ui_postprocessing.py` contract to prove:

- route/module/nav wiring and Simple-mode hiding/redirect;
- all data access goes through the transport facade;
- source is assigned as textarea value, never HTML;
- dirty edits survive background info/job refreshes;
- save/import creates an untrusted revision;
- trust and environment changes correctly gate Run;
- processing and install terminal states repaint without navigation;
- fetch completion navigates but never auto-runs;
- completion, failure, cancellation, validation, and rollback messaging;
- history never substitutes a different revision;
- responsive layout and both themes.

### Full verification

Run the repository's complete Python/frontend suite, JavaScript syntax checks, Rust formatting/tests/checks, and package-workflow-equivalent macOS/Windows smoke tests described in `AGENTS.md` and `.github/workflows/package.yml`.

## Acceptance criteria

The feature is complete only when all of the following are true:

- It is absent from Simple-mode navigation and cannot start or mutate through stale Simple-mode requests.
- A user can create or import a bounded processor, inspect/edit it, save an immutable revision, and explicitly trust that exact revision.
- Optional compatible PyPI wheel packages install into a hashed, interpreter-specific environment below Workbench data without modifying the bundled/system Python.
- Workbench discovers the images and calls the processor once per staged image in one batch process; scripts do not need directory loops.
- Expensive module initialization happens once per job.
- Scripts never receive real managed image paths through the supported contract.
- Failed, cancelled, timed-out, crashed, invalid, or partially published runs do not leave a mixed image set; restart recovery is proven.
- Successful runs replace only recognized selected images, preserve names and unrelated files, and refresh Create PDF state.
- All processes, logs, requests, responses, files, scans, downloads, environments, and times have tested bounds.
- Native and browser transports are equivalent, with no native-to-HTTP fallback.
- The UI clearly states that processors and packages are trusted code, not securely sandboxed code.
- Packaging smoke tests pass on macOS ARM64 and Windows x64 with the actual bundled runtime.

## Deferred possibilities

Do not include these in the first implementation:

- automatic execution immediately after every fetch;
- untrusted-code claims or an import/module allowlist marketed as a sandbox;
- source distributions, custom/private package indexes, VCS/local requirements, or system package installation;
- arbitrary source/output directories or card-back processing;
- directory-level batch callbacks;
- parallel callbacks;
- per-image custom UI parameters;
- processor sharing/marketplace or downloading scripts from URLs;
- secrets injection;
- notebook support, interactive stdin, debugging, or a full IDE editor;
- output renaming, adding/removing cards, format conversion, or partial-success commits.

A future automatic mode may be considered only as an explicit opt-in pinned to a trusted revision/environment. Any edit must disable that opt-in until re-approved.

## Research basis

The design relies on these externally documented constraints:

- RestrictedPython explicitly says it is not a sandbox or secured environment: <https://restrictedpython.readthedocs.io/en/latest/>
- pip's secure-install guidance says normal installs may involve arbitrary distribution code and recommends complete hashes plus `--only-binary :all:` for stronger installs: <https://pip.pypa.io/en/stable/topics/secure-installs/>
- pip documents `--target`, `--report`, dry-run resolution, and binary-only controls: <https://pip.pypa.io/en/stable/cli/pip_install/>
- Python documents virtual environments as disposable, not movable/copyable, and inherently non-portable in the general case: <https://docs.python.org/3/library/venv.html>
- Python packaging documents normal plugin discovery through naming, namespace packages, or entry-point metadata. This plan deliberately uses an explicit Workbench registry instead of auto-discovering every installed package: <https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/>
