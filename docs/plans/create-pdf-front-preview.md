# Create PDF representative front-page preview plan

## Status

Implemented in SCM Workbench. The completed design uses private bounded copies, the selected SCM interpreter for isolated Pillow work, asynchronous native and browser operations, cancellation and checkout coordination, and a representative first-front-page panel in both interface modes. Simple mode shows a concise readiness summary instead of the technical command preview; Advanced mode retains the command preview.

## Decision summary

Add a low-quality, representative preview to the Create PDF page using changes in SCM Workbench only. The preview will:

- show one image: the front of the first generated page;
- use a small, bounded sample of front card images;
- invoke the existing upstream `create_pdf.py` CLI unchanged;
- render against private temporary input and output directories;
- never import, copy, or reimplement the upstream PDF renderer;
- never modify source image folders, `game/output`, job history, or artifact grants;
- work through equivalent native IPC and standalone-browser transports.

This is a visual aid, not a print proof. It does not promise to contain every card that the real first page would contain or to reproduce final-resolution pixels.

## Why this approach

The existing command preview is intentionally side-effect free and only assembles the command. The upstream renderer has no first-page mode. It creates all pages in memory, writes them only after rendering the full input set, and deletes hidden files from its input directories before rendering.

Calling it directly against the user's folders for every form change would therefore be unsafe and wasteful. SCM Workbench can avoid both problems by staging a small sample in a private temporary tree, forcing low-resolution front-only image output, and displaying only `page1.png` after converting it to a bounded JPEG.

A local benchmark with the unchanged renderer at 150 PPI took about 1.26 seconds for 9 synthetic card images and 1.83 seconds for 90 images on the development Mac. A bounded sample, lower internal resolution, debouncing, cancellation, and single-flight admission should make a representative preview responsive without rendering the full deck.

## User experience

Add a preview panel to the Create PDF page near the validation or command summary and Run action.

The panel should:

- be titled `Representative front-page preview`;
- explain that it is low quality and uses up to 16 front images;
- preserve the previous preview, dimmed, while a replacement is rendering;
- show clear empty, loading, unavailable, cancelled, and failed states;
- update after relevant form changes settle;
- update when the page opens or the app regains focus, so external front-image changes are reflected;
- never imply that card-back settings are represented;
- remain usable in both Simple and Advanced modes and in both themes.

The preview should not refresh for settings that cannot change the first front page, such as back-only crop and fit controls. It is acceptable to refresh conservatively in the first implementation if filtering those fields would duplicate option knowledge.

Changes that should normally be visible include paper and card size, specialty layout, registration settings, borderless layout, front fit and crop, front edge and corner extensions, front bleed, skipped positions, labels, and cut outlines.

The following are intentionally not represented by a first-front-page preview:

- the card back image;
- back-specific fit, crop, edge, corner, and bleed settings;
- saved printer offset, which the upstream renderer applies to back pages;
- final PPI and compression quality;
- later pages or the complete deck order.

## Backend rendering pipeline

### 1. Reuse normal validation

Use the existing manifest, `normalize_args`, settings snapshot, repository snapshot, and `build_command` path. Do not create a second option schema.

Start rendering only when the ordinary command preview reports no errors and at least one recognized front image. A preview request must still repeat server-side validation because UI state is not trusted.

### 2. Restrict eligible source paths

Resolve the selected front directory against the request's pinned SCM checkout using a purpose-specific, read-only resolver. Relative paths remain below that checkout. Absolute paths outside the configured SCM checkout should make the preview unavailable even though an explicitly started upstream job may support them.

This narrower rule prevents an automatically triggered feature from scanning arbitrary paths while the user is typing. It does not change which paths a real Create PDF job can use.

Reject unsafe path components, links or reparse points, non-directories, excessive path lengths, and a changed checkout identity. Reuse existing stable-handle and containment patterns instead of relying on `Path.resolve()` alone.

### 3. Select and stage a bounded sample

Scan recognized front images with explicit limits. The initial target bounds are:

- at most 8,192 directory entries scanned;
- at most 1,024 recognized candidates considered;
- at most 16 images staged;
- at most 32 MiB per source image;
- at most 128 MiB copied in one operation;
- at most 4,096 UTF-8 bytes per path and 255 UTF-8 bytes per source name.

Choose up to 16 images using a deterministic natural-name order. The preview is representative, so exact parity with upstream's deck ordering is not required. Rename staged files to safe sequential names while retaining recognized suffixes.

Open each source through a stable regular-file handle, verify its identity and size before and after copying, and reject links, devices, FIFOs, directories, and files that change while being read. Copy into a private operation directory below Workbench data. Do not hardlink or symlink source files into the staging tree.

Create empty staged `back` and `double_sided` directories. This lets the unchanged upstream CLI use `--only_fronts` without reading card backs or rejecting real double-sided files. Its hidden-file cleanup then affects only the disposable staging tree.

### 4. Build the representative command

Clone the normalized Create PDF arguments and override only the preview-specific fields before passing them back through `build_command`:

- `front_dir`: private staged front directory;
- `back_dir`: private empty directory;
- `double_sided_dir`: private empty directory;
- `output_path`: private output directory;
- `output_images`: true;
- `only_fronts`: true;
- `ppi`: 75;
- `quality`: a low preview value;
- `load_offset`: false.

Keep the real front-page layout and finishing arguments, including card and paper size, specialty layout, registration, borderless mode, front fit and crop, front extensions, front bleed, skipped positions, label, and outline.

The operation must use the Python interpreter selected in Settings, set `stdin=subprocess.DEVNULL`, capture bounded output, and use the existing child process-group or Windows job containment behavior.

### 5. Bound paper geometry before launch

A malicious or accidental custom layout could request an enormous raster even at 75 PPI. Resolve the effective named paper from the same cached layout metadata used by the manifest, parse only the supported `mm` and `in` dimensions, and enforce a conservative maximum physical size and pixel area before starting the child.

Named specialty layouts may preview when they resolve to a bounded named paper. An inline specialty paper without trustworthy dimensions should show `Preview unavailable for this custom paper size` rather than launching an unbounded render.

These checks affect only preview availability. They must not alter real job validation or execution.

### 6. Produce one bounded image

The unchanged CLI may create more than one low-resolution staged page when the selected layout holds fewer than 16 cards. Read and display only `page1.png`; delete every other temporary page without publishing it.

Run a small Workbench-owned helper under the selected interpreter to load `page1.png` with Pillow, apply EXIF-safe conversion if needed, constrain the longest edge to 900 pixels, and encode RGB JPEG at approximately quality 60. The helper must not import upstream modules.

Reduce dimensions or JPEG quality until the encoded file is at most 512 KiB. Reject malformed output, excessive dimensions, or a file that changes while being read. Return bounded metadata plus base64 JPEG data, then remove the complete operation directory.

## Asynchronous operation model

Do not perform rendering inside the worker's serialized RPC call. Add a bounded operation registry and executor with:

- one active render globally;
- at most four retained terminal records;
- random 32-character lowercase hexadecimal operation IDs;
- a 15-second render deadline;
- a short terminal retention period, such as 60 seconds;
- cooperative cancellation plus forced process-tree termination;
- guaranteed temporary-directory cleanup after success, failure, cancellation, timeout, shutdown, and startup recovery.

Starting a newer preview should cancel an older preview that belongs to the same UI generation. Starting a real Create PDF job should cancel the live preview first. Preview admission should pause or reject while repository update, image fetch, image deletion, cleanup, or another Create PDF operation could change the relevant checkout.

A failed native operation is final. It must never retry over HTTP.

## Transport contract

Add exact logical operations:

- `pdf_preview.start` with `{args}`;
- `pdf_preview.poll` with `{operation_id}`;
- `pdf_preview.cancel` with `{operation_id}`.

Native packaged windows should use public `wb_rpc` allowlisted methods. Standalone browsers should use one purpose-specific `/api/pdf-preview` compatibility route with equivalent parameter validation, errors, and result shapes.

Suggested terminal result shape:

```json
{
  "ok": true,
  "mime": "image/jpeg",
  "data": "<bounded base64>",
  "width": 900,
  "height": 695,
  "sampled": 16,
  "available": 87
}
```

The response must have a dedicated result-size limit below the worker's general JSON-lines response ceiling. Polling returns the image only once the operation is terminal. No generic raw-file method, filesystem scope, asset protocol, or temporary path should be exposed to the WebView.

## Frontend scheduling

Add a dedicated transport facade and preview controller rather than calling native IPC or HTTP from `pages/pdf.js` directly.

The controller should:

1. wait for the ordinary command preview to validate the current form;
2. debounce a render for 750 to 1,000 ms;
3. snapshot the form arguments and assign a monotonically increasing generation;
4. cancel the previous operation when a newer generation starts;
5. poll at a bounded interval;
6. ignore every stale response;
7. stop timers and cancel active work when the page is removed;
8. refresh after focus or visibility returns;
9. retain and dim the last successful image while updating;
10. release any object URL or large data string when replaced.

The ordinary command preview should publish its validated result through a narrow callback or event so the visual preview does not send a duplicate validation request. Avoid a circular import between the generic form system and the PDF page.

## Expected files

Likely additions and changes:

- `scm_workbench/pdf_preview.py`: bounds, staging, operation registry, child control, result validation, and cleanup;
- `scm_workbench/pdf_preview_helper.py`: Pillow-only JPEG conversion in a child process;
- `scm_workbench/server.py`: shared normalization and command construction integration plus browser compatibility route;
- `scm_workbench/ipc.py`: exact native request dispatch and size validation;
- `tauri/src/ipc.rs`: public method allowlist and response validation;
- `ui/js/pdf-preview-transport.js`: native/browser facade with no fallback after native selection;
- `ui/js/pdf-page-preview.js`: lifecycle, sequencing, cancellation, rendering, and UI states;
- `ui/js/pages/pdf.js`, `ui/js/forms.js`, and `ui/theme.css`: page integration and validated-preview notification;
- focused Python, JavaScript contract, Rust allowlist, and browser tests;
- packaging smoke checks to prove the selected bundled interpreter can run the helper and Pillow.

Keep staging and destructive filesystem logic out of page code. Keep the worker's stdout reserved for JSON-lines responses.

## Test plan

### Python and security tests

Cover:

- the normal one-page result and base64/JPEG bounds;
- deterministic sampling and the 16-image cap;
- empty, missing, unsafe, external, linked, reparse, special-file, oversized, and changing sources;
- scan, path, name, source-byte, paper-geometry, subprocess-output, JPEG, and response limits;
- staged hidden files cannot cause deletion from the real source directory;
- source images, placeholders, unrelated files, `game/output`, jobs, logs, and artifact grants remain byte-for-byte unchanged;
- preview overrides cannot affect the real form arguments or command preview;
- custom and specialty paper admission;
- timeout, cancellation, process-tree termination, executor rejection, shutdown, startup cleanup, and retained-record expiry;
- an image or checkout changing between validation and staging is rejected safely;
- active repository or image operations block preview admission.

### Native and browser contract tests

Cover:

- exact method and parameter allowlists;
- malformed and oversized request rejection;
- bounded terminal response validation;
- native start, poll, cancel, and failure behavior;
- no HTTP request after a callable native bridge rejects;
- equivalent HTTP result and application-error shapes;
- packaged HTTP rejection for routes that remain browser-only.

### Frontend tests

Cover:

- initial rendering and the full empty/loading/success/error state machine;
- debounce behavior under rapid field changes;
- cancellation and stale-response suppression;
- cleanup on navigation;
- focus and visibility refresh;
- no rendering when validation is blocked or no fronts exist;
- only the first front-page image is displayed;
- representative/sampled wording remains visible;
- Simple and Advanced mode placement;
- dark and light theme layout with no desktop overflow.

### End-to-end fixture

Use an isolated SCM checkout containing recognizable numbered front images. Verify that changing paper, card size, borderless mode, crop, skip, label, and outline eventually changes the displayed image while the source tree and real output directory remain unchanged. Verify that card-back-only changes do not claim to be represented.

Run the complete Python, frontend contract, JavaScript syntax, Rust formatting/test/check, packaged embedded-asset, and release-workflow-equivalent gates before completion.

## Acceptance criteria

- Create PDF shows one clearly labeled low-quality preview of the first front page.
- The preview uses no more than 16 staged fronts and never renders the full user deck.
- The unchanged upstream CLI remains the sole layout renderer.
- Preview generation never mutates user or managed-repository files.
- Preview activity never appears in job history or publishes an artifact.
- Rapid edits cannot create unbounded work or display stale output.
- Navigation, cancellation, timeout, shutdown, and update handoff leave no child process or temporary file behind.
- Native and browser modes behave equivalently, and native failures never retry over HTTP.
- Unsupported or unsafe inputs produce a bounded unavailable state without affecting the real Create PDF job.

## Deferred possibilities

Do not include these in the first implementation:

- back-page preview;
- page navigation;
- full-deck thumbnails;
- final-resolution rendering;
- embedded PDF or PDF.js viewer;
- arbitrary external source-folder preview;
- persistent preview cache;
- using a completed job artifact as the live form preview.
