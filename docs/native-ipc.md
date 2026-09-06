# Native IPC: the first slice

This document describes the **current first native IPC slice**. It is a
transitional boundary, not a claim that the Workbench's HTTP transport has
been removed.

## Architecture

The packaged app has one Tauri shell and one supervised Python worker:

```text
SCM Workbench (Tauri native webview)
  └─ Python -m scm_workbench.server --ipc --no-browser
       ├─ stdin/stdout: bounded JSON-lines RPC
       └─ 127.0.0.1:8038: HTTP compatibility server, static assets, and
                           every endpoint not yet migrated
```

The worker is one process, not a native server plus a second Python server.
It continues to own Workbench behavior and binds its existing loopback HTTP
server so that the rest of the UI remains functional during migration. The
Tauri window currently navigates to that worker origin because unmigrated UI
calls use relative HTTP URLs. The native protocol covers the three bootstrap
reads, bounded settings and offset mutations, preview, packaged-Tauri job
control/log operations, the read-only `template.resolve`/`file.list` metadata
slice, bounded repository metadata operations, and the bounded OS-action methods
below. Other surfaces remain on their existing HTTP compatibility paths.

A source checkout still uses the browser development flow: `python -m
scm_workbench` (or `python -m scm_workbench.server`) starts the HTTP server and
opens or prints a local browser URL. The packaged Tauri shell does not open a
browser for its worker.

## JSON-lines protocol

Each request and response is one UTF-8 JSON object terminated by `\n`. The
worker flushes after every response. The input frame is limited to 1 MiB and
the complete response frame is limited to 8 MiB on the Tauri reader (the Python
worker keeps ordinary responses below 7 MiB). An oversized result becomes a
bounded error response rather than a truncated JSON frame. `preview` has a
stricter 512 KiB encoded result budget, and `jobs.poll` has a stricter 6 MiB
result budget.

A request has this exact shape:

```json
{"id":"rpc-1","method":"info","params":{}}
```

`id` must be a non-empty string. `method` must be a string in the allowlist
below, and `params` must be a JSON object. The exact allowlist contains
twenty-one methods: `info`, `manifest`, `settings.get`, `settings.set`,
`offset.set`, `offset.delete`, `jobs.list`, `jobs.start`, `jobs.log`,
`jobs.kill`, `jobs.poll`, `preview`, `template.resolve`, `file.list`,
`file.open`, `file.reveal`, `url.open`, `repos.refs`, `repos.source.set`,
`repos.check`, and `repos.poll`. The bootstrap read methods take `{}`;
preview, job, artifact metadata, offset, and repository methods use the
parameter contracts below.

| HTTP compatibility route | Native method | Python implementation |
| --- | --- | --- |
| `GET /api/info` | `info` | `server.get_info()` |
| `GET /api/manifest` | `manifest` | `server.get_manifest()` |
| `GET /api/settings` | `settings.get` | `server.load_settings()` |
| `POST /api/settings` | `settings.set` | `server.update_settings()` |
| `POST /api/offset` (global/per-size) | `offset.set` | `server.offset_set()` |
| `POST /api/offset` (per-size delete) | `offset.delete` | `server.offset_delete()` |
| `GET /api/jobs` | `jobs.list` | `server.list_jobs()` |
| `POST /api/jobs` | `jobs.start` | `server.start_job()` |
| `GET /api/jobs/<id>/log` | `jobs.log` | `server.get_job_log()` |
| `POST /api/jobs/<id>/kill` | `jobs.kill` | `server.kill_job()` |
| `GET /api/jobs/<id>/stream` | `jobs.poll` (packaged Tauri) | `server.poll_jobs()` |
| `GET /api/preview` | `preview` (packaged Tauri) | `server.build_preview()` |
| `GET /api/template` | `template.resolve` (packaged Tauri) | `server.resolve_template()` |
| `GET /api/file?...images_only=1` (directory metadata) | `file.list` (packaged Tauri) | `server.list_files()` |
| `GET /api/file?...open=1` (regular file action) | `file.open` (packaged Tauri) | `server.file_open_action()` |
| `POST /api/reveal` (file/directory action) | `file.reveal` (packaged Tauri) | `server.file_reveal_action()` |
| `GET /api/file?...url=` (external URL action) | `url.open` (packaged Tauri) | `server.url_open_action()` |
| `POST /api/repos/refs` | `repos.refs` (packaged Tauri) | `repo_sync.list_refs()` in a background operation |
| `POST /api/repos/save` | `repos.source.set` (packaged Tauri) | `repo_sync.resolve_target()` and the settings/state transaction |
| `POST /api/repos/check` | `repos.check` (packaged Tauri) | `run_repo_check()` in a background operation |
| (operation registry) | `repos.poll` (packaged Tauri) | poll a repository metadata operation |

Those HTTP routes remain served as compatibility endpoints; native selection is
a client transport choice, not their removal. The methods use these exact
parameter and result shapes inside the common RPC envelope. For `settings.set`,
the worker holds the settings lock across load, schema validation, merge, and
atomic commit. It writes a sibling temporary file and replaces `settings.json`
with `os.replace`; a failed validation or write leaves the previous file intact.
After a successful commit it invalidates the manifest cache, so the returned
settings and the next `settings.get` agree. It never writes repos state or the
offset state. Offset mutations use their own serialized, atomic
state/projection transaction described below:

* `preview`: params `{"kind":"<string>","args":{...}}` (exactly those two
  keys). `kind` is at most 128 UTF-8 bytes. The JSON encoding of the `args`
  object is at most 512 KiB. The result is the same object as `GET
  /api/preview` (`cmd`, `cwd`, `env`, `warnings`, `errors`, and
  `no_front_images`) and its encoded JSON is at most 512 KiB.
* `settings.get`: params `{}`. Result is the complete merged settings object.
* `offset.set`: params exactly `{"size":null,"x":<integer>,"y":<integer>,"angle":<number>}`
  for the global baseline, or the same object with `size` set to a known SCM
  paper-size name for a per-size row. `size` is either JSON null or a non-empty
  UTF-8 string of at most 128 bytes with no C0 or DEL controls. `x` and `y`
  are non-boolean JSON integers in `-100000..100000`; `angle` is a non-boolean
  finite JSON number in `-360..360`. No other keys are accepted. Success is
  exactly `{"ok":true,"offset":{"x_offset":x,"y_offset":y,"angle_offset":angle}}`
  for the global baseline and adds `"size":"<name>","staged":true` for a
  per-size row. Validation failures are successful RPC results with
  `{"ok":false,"errors":["..."]}`; malformed parameter envelopes are
  `bad_request`.
* `offset.delete`: params exactly `{"size":"<known paper-size name>"}`.
  It removes that canonical per-size row; deletion is idempotent and returns
  exactly `{"ok":true,"removed":"<name>"}`. If the deleted row was staged,
  the global baseline is projected back to SCM's shared file (or the shared
  projection is removed when no baseline exists). Validation and filesystem
  failures use the same bounded `ok:false` result shape.
* `settings.set`: params exactly `{"changes":{...}}`. The `changes` object
  must be valid JSON, non-empty, and at most **64 KiB (65,536 bytes)** when
  encoded as compact UTF-8 JSON. Only these top-level keys are accepted:
  `scm_dir`, `extras_dir`, `python`, `port`, `theme`, `ui_mode`,
  `auto_open_browser`, `onboarded`, and `defaults`. Unknown keys,
  `repos`, and offset state are rejected. String paths/interpreters may be
  empty but are at most 4096 UTF-8 bytes and contain no C0 or DEL controls.
  `port` is a non-boolean integer in `1024..65535`; `theme` is `dark` or
  `light`; `ui_mode` is `simple` or `advanced`; and the two remaining scalar
  values are strict booleans. `defaults` must be a non-empty partial object
  containing only `card_size`, `paper_size`, `ppi`, and `quality`. Card and
  paper names are non-empty, at most 128 UTF-8 bytes, and contain no C0 or
  DEL controls; `ppi` is a non-boolean finite number in `0..10000`, and
  `quality` is a non-boolean finite number in `0..100`. The merged settings
  result is schema-checked and must also fit within 64 KiB encoded JSON.
  Success is exactly `{"ok":true,"settings":<complete merged settings>}`.
  Validation happens before any write, so a rejected change cannot partially
  update settings.

### Repository metadata operations

Repository metadata has an asynchronous native contract because refs and update
checks perform remote work. `repos.refs`, `repos.source.set`, and `repos.check`
are **start** methods: after validating their exact parameter object they return
immediately with `{"ok":true,"operation":{"id":"...","status":"running"}}`.
They do not wait for GitHub and do not hold the serialized RPC call for network
I/O. `repos.poll` is the only method that reads an operation:
`{"operation_id":"..."}`. It returns immediately with
`{"ok":true,"status":"running"}` or a terminal
`{"ok":true,"status":"done","result":{...}}`. A missing or expired ID is
an immediate bounded `worker error: bad_request` app failure; it never starts
work and never falls back to HTTP.

The exact start parameters are:

* `repos.refs`: `{"repo":"scm"}` or `{"repo":"extras"}`.
* `repos.source.set`: `{"repo":"scm","source":"<ref>"}`. `source` uses
  the same bounded ref validation as the HTTP save route.
* `repos.check`: `{"repo":"scm","force":false}`; `force` is required and
  boolean.

No other keys are accepted. Repository identity is fixed to the two entries in
`repo_sync.REPOS` (`scm` = `Alan-Cha/silhouette-card-maker`, `extras` =
`Alan-Cha/scm-extras`); callers cannot supply an owner, URL, path, or command.
Invalid repo keys are rejected before any remote request or settings/state
write. The in-memory operation registry has a queue of 16, at most 16 active
records, and at most 32 retained records; two daemon workers perform the remote
calls. A full registry returns an immediate bounded busy result. Terminal
records are retained for 300 seconds and then become missing; a restart drops
all records. Ref fetches also use a per-repository one-hour cache and a
single-flight event, so HTTP and native callers do not duplicate a concurrent
GitHub request.

The refs result is the existing `{"ok":true,"repo":"...","refs":{...}}`
shape and is capped at 512 KiB encoded JSON before entering the cache. Check
and source-set results retain their HTTP response fields and every repository
operation result is capped at 1 MiB; errors are bounded to eight sanitized
messages of at most 256 characters each. `repos.poll` itself is limited to one
operation ID. The 10-second `WorkerRpc` timeout therefore remains appropriate:
starts and polls are local, immediate operations; remote work runs in the
worker's two-thread background registry rather than occupying the serialized
request.

`repos-state.json` is the canonical repository source/deployment/check state;
`settings.json` contains the mirrored UI source choice. A successful source
operation resolves the fixed remote target first, then commits the canonical
state under the repository locks and mirrors the source into settings. If the
mirror fails, the operation still succeeds with `canonical:true` and a bounded
`warnings` list: state remains authoritative and the refreshed UI reports the
repair warning without claiming the source change failed. A failed resolution
leaves canonical state unchanged. Ref listing is read-only; a check updates
only its repository's check metadata. Neither method changes the managed tree.
`repo_init` and `repo_update` remain ordinary `jobs.start` kinds because they
copy/deploy large trees and stream progress; this IPC slice does not turn them
into metadata operations. These metadata starts also inherit the prior
`repo_sync` hardening: fixed GitHub API/raw hosts, bounded ref and JSON results,
validated ref/path forms, canonical no-symlink repository paths, per-repository
locks, stale-source checks, and transactional deployment journals. Metadata IPC
never accepts a local path or arbitrary remote URL and never bypasses the
transactional init/update publication boundary.

### Offset state, projection, and jobs

Offsets have one canonical Workbench state and one disposable upstream
projection. `data/offset_state.json` stores the global baseline, per-paper
rows, and the currently staged paper; SCM's `data/offset_data.json`
is only the projection consumed by `--load_offset`. A global `offset.set`
updates the baseline and projection. A per-size `offset.set` updates its row,
records it as staged, and projects that row. `offset.delete` removes the row
and, when it was staged, restores the baseline projection; with no baseline it
removes the upstream projection. The SCM repository code is never changed.

Both files are committed with sibling temporary files and `os.replace` while
the offset lease is held. The canonical file is committed before projection;
if projection or replacement fails, the previous canonical bytes and previous
projection are restored (or the failed temporary files are removed), and the
RPC returns `ok:false`. Directory `fsync` is best effort on platforms including
Windows, so this is an atomic application-level transaction, not an absolute
power-loss guarantee. A corrupt or incomplete canonical file is rejected
without guessing or overwriting it. This makes restart recovery deterministic:
load canonical state, inspect `staged_size`, and repair only the projection
before serving jobs. The canonical baseline is authoritative; a staged row is
never folded into the baseline implicitly. There is no partial-success response.

The same lease is held while an offset-sensitive job stages its row and for the
child's lifetime. Offset mutations and offset-sensitive job starts fail fast
with the bounded busy result when the lease is held; they do not wait. A job
cannot restage a different row concurrently with a mutation. This prevents a
completed mutation from being silently overwritten by an older PDF process;
normal job process-group/setpgid teardown still applies.

An `offset_pdf --save` job records a durable pending-save intent before its
child starts. On startup, reconciliation adopts the intended upstream value
only when the child demonstrably wrote a valid value; otherwise it restores the
prior projection from canonical state. An ambiguous or corrupt intent/state
blocks reconciliation and reports an error rather than guessing. This keeps a
crash between the child write and the job completion from silently changing the
baseline or losing a user save.

* `jobs.list`: params `{}`. Result is `{"jobs":[...]}`. Each live row contains
  `id`, `ts`, `kind`, `title`, `status`, `exit_code`, `cmd`, `warnings`, and
  `outputs`; `progress` is present while progress is available. Persisted
  history rows retain the same metadata (and may carry older persisted fields).
* `jobs.start`: params `{"kind":"<string>","args":{...}}` (exactly those two
  keys). Success is `{"ok":true,"job":{"id":"...","title":"...",
  "status":"running","cmd":"...","warnings":[...]}}`. Rejected form
  arguments are a successful RPC containing `{"ok":false,"errors":["..."]}`.
* `jobs.log`: params `{"job_id":"<string>"}` with optional non-negative
  `after` (default `0`) and `max_lines` (default `4096`, range `1..4096`).
  Result is `{"lines":["..."],"status":"...","exit_code":...,
  "cmd":"...","first_seq":0,"next_seq":0,"truncated":false,
  "gap":false,"line_truncated":false}`. A missing job is a normal result
  with status `missing`, not an RPC error.
* `jobs.kill`: params `{"job_id":"<string>"}` (exactly that key). Result is
  `{"ok":true}` when a running job was found and asked to stop, otherwise
  `{"ok":false}`.
* `jobs.poll`: params `{"cursors":[{"job_id":"<string>","after":0},...],
  "max_events":256}` (both keys required). There may be at most 32 cursors;
  `max_events` is an integer from 1 through 256. Result is
  `{"jobs":[...]}` in the same cursor order. Each row is
  `{"job_id":"...","lines":[{"i":0,"s":"..."}],"next_seq":0,
  "status":"...","exit_code":...,"cmd":"...","complete":false,
  "truncated":false,"gap":false}`. Missing jobs are complete rows with an
  empty `lines` array and status `missing`.
* `template.resolve`: params `{"paper":"<string>","card":"<string>",
  "borderless":false}` (exactly those three keys; `paper` and `card` are
  non-empty strings, each at most 128 UTF-8 bytes). It returns the same
  metadata as the compatibility route:
  `{"ok":true,"name":"...","path":"...","repo":"..."}` when a
  matching `.studio3` exists, or `{"ok":false,"errors":["..."]}` when the
  card, repository, or template is missing. It reads directory entries only;
  it never returns template bytes and never opens the result. Its result is
  bounded to 512 KiB.
* `file.list`: params `{"path":"<string>","images_only":true}` (exactly
  those two keys; `path` is a non-empty string of at most 4096 UTF-8 bytes).
  A successful directory result is
  `{"exists":true,"dir":"...","items":[],"truncated":false,
  "scanned":0,"found":0}`. `found` is always the numeric count of entries
  matching the filter during the bounded scan; it is never a boolean. Each
  item is metadata only: `{"name":"...","dir":false,"size":0,
  "path":"..."}`. `dir` is true for directories and their size is zero.
  `images_only:true` applies the same image-file predicate as Python. A
  native missing-directory result is successful metadata with
  `{"exists":false,"items":[],"truncated":false,"scanned":0,"found":0}`;
  it contains no path contents. The worker scans at most 8192 directory
  entries, returns at most 1024 matching items, and caps the encoded result
  at 512 KiB. `truncated:true` means one of those bounds stopped the listing;
  it does not mean an item was deleted or that a path may be opened. Items
  are the deterministic sorted prefix of the bounded scan. A non-directory,
  unreadable path, or sandbox escape is a structured RPC error
  (`not_directory`, `unreadable`, or `forbidden`), never a file read. The
  HTTP compatibility route retains its legacy 404 for a missing directory;
  the browser HTTP facade normalizes that response to
  `{exists:false,items:[],truncated:false,scanned:0,found:0}`.

### Native OS actions

The three OS actions are deliberately separate allowlisted methods, not an
arbitrary Python call or a path/command escape hatch. Each has an exact
parameter object and returns only the bounded action result (never file bytes):

* `file.open`: params exactly `{"path":"<string>"}`. `path` is a non-empty,
  valid UTF-8 string of at most **4096 UTF-8 bytes** with no C0 or DEL control
  characters. Python canonicalizes it and requires an existing regular file.
* `file.reveal`: params exactly `{"path":"<string>"}`, with the same 4096-byte
  and printable-value bounds. Python canonicalizes it and requires an existing
  file or directory. On Windows, revealing a file selects it with Explorer
  (`explorer /select,`); revealing a directory opens that directory in
  Explorer. Other platforms use their native file-manager reveal equivalent.
* `url.open`: params exactly `{"url":"<string>"}`. `url` is non-empty, valid
  UTF-8, has no C0 or DEL control characters, and is at most **8192 UTF-8
  bytes**. Its parsed scheme must be `http` or `https` (case-insensitive), it
  must have a hostname, must have a valid authority/port, and must not contain
  userinfo. `file:`, `javascript:`, `data:`, custom schemes, empty-host URLs,
  malformed authorities, and userinfo URLs are rejected.

The result is exactly `{"ok":true,"errors":[]}` on a launch request accepted
by the platform helper, or `{"ok":false,"errors":["<message>"]}` for a
bounded failure. Error text is sanitized and capped at **4096 UTF-8 bytes**;
no path or URL result is returned. The ordinary JSON-lines frame limits still
apply (7 MiB Python response ceiling and 8 MiB Tauri reader ceiling), but an
OS-action result is much smaller than either bound.

Python remains the owner of validation, canonicalization, allowed-root policy,
and OS launching. The allowed roots are the Workbench data directory, the UI
asset directory, and the effective configured SCM and extras repositories.
Relative paths retain the existing first-existing-root precedence; absolute
and relative paths are resolved canonically (including symlinks) before the
candidate is checked with the canonical `_inside` root test. A path that
resolves outside those roots is rejected, and no launcher is called. Tauri
only forwards these exact method names through the existing `wb_rpc` command;
there is no new capability, command, framing rule, or lifecycle behavior.

Native actions do not silently fail over to HTTP. A packaged WebView invokes
the native method and surfaces a native error; a normal browser, which has no
callable Tauri capability, uses the existing HTTP compatibility route instead.
This is a browser-versus-packaged transport choice, not a retry policy. The
Python helper owns the actual platform process launch, including Windows
Explorer selection; the Rust shell never launches an arbitrary executable.

A successful response has exactly this shape (with the method's JSON result):

```json
{"id":"rpc-1","ok":true,"result":{}}
```

A failed request has exactly this shape:

```json
{"id":"rpc-1","ok":false,"error":{"code":"unknown_method","message":"unknown method: jobs.start"}}
```

The defined error codes are:

* `bad_request` — invalid JSON or UTF-8, a non-object request, an invalid or
  missing ID/method/params, or an input line over 1 MiB. Malformed requests
  whose ID cannot be trusted use `"id":null`.
* `unknown_method` — a method outside the twenty-one-method allowlist.
* `not_directory`, `unreadable`, and `forbidden` — bounded `file.list`
  metadata resolution could not produce a listing. A missing directory is
  instead the successful `{exists:false,...}` result described above. These
  are structured read-only metadata failures; they never authorize a file
  read or OS action.
* `internal` — the existing handler failed or the response exceeded the
  configured limit. Handler details are written to stderr, not exposed on the
  wire.

There is no arbitrary Python callable or path dispatch in this protocol.
Protocol frames are the only data written to the worker's stdout; startup text
and diagnostics go to stderr (captured in the packaged worker log).

### Job polling bounds and cursors

Packaged Tauri job output uses one bounded aggregate `jobs.poll` request for all
currently displayed jobs. Each subscription owns an independent `after` cursor
(sequence number); rows are correlated with the requested `job_id` and cursors
advance from `next_seq`. The request accepts at most 32 cursors and at most 256
`max_events` per cursor. The aggregate response is capped at 6 MiB and is
allocated fairly across cursors, so one large transcript cannot starve the
others. Displayed lines are capped at 64 KiB of UTF-8, with a visible
`… [line truncated]` marker; complete transcripts remain in the worker's log
storage.

`truncated` means bounded pagination or display clipping, not missing sequence
numbers: continue with the returned `next_seq`. `gap` is different: the
requested cursor predates the retained `first_seq`, so earlier output is no
longer available and the UI warns. A terminal row sends its final lines before
`complete:true`/`onDone`. Native polling reports a temporary outage once, then
retries with bounded backoff and stops after six failed polls; it never retries
a failed native operation over HTTP.

## Tauri `WorkerRpc`

`WorkerRpc` is managed Tauri state installed with the already-spawned worker's
stdin and stdout pipes. It:

* assigns monotonically increasing correlation IDs (`rpc-1`, `rpc-2`, ...),
  writes one newline-terminated request, and flushes it;
* keeps a reader thread draining complete stdout frames, then matches each
  response by `id` to the pending call;
* serializes calls in this first slice (one in flight), while the reader keeps
  the pipe drained;
* validates response shape and correlation, rejects malformed, mismatched, or
  oversized frames, and maps unknown error codes safely; and
* applies a 10-second default call timeout. Worker EOF, a broken pipe, or a
  timeout marks the worker unavailable and wakes pending calls instead of
  hanging the webview.

The webview reaches one narrow Tauri command, `wb_rpc`. The Tauri capability
ACL grants `allow-wb-rpc` to the main window and permits the worker-served
origin `http://127.0.0.1:8038`; the command itself validates the same method
allowlist before writing to the child. Python validates it again, including
OS-action path and URL policy. This is a transport/orchestration boundary, not
a general native escape hatch.

The shell supervises the same single worker for its entire lifetime. Closing
the window or a hard shell exit reaps it and its descendants: macOS uses a
process group and Windows uses a kill-on-close job object. The worker is
started with `--ipc --no-browser`; it does not start another worker or browser.
When the IPC input reaches EOF, the worker's shutdown callback terminates every
active upstream job, waits/reaps each process, and joins every output-pump
thread before the HTTP worker exits; no upstream job is left behind.

Each installed pair of worker pipes has a session generation. EOF, malformed
output, and pending-call failure are applied only to the generation that saw
them, so a stale reader from an old worker cannot disable or deliver data to a
newly installed worker session.

### Stdout reservation rule

Every child process launched under an IPC worker must obey this rule: **the
worker's stdout is reserved exclusively for JSON protocol frames**. A child
must not inherit it. Send diagnostics to stderr or a log, use `DEVNULL` for
quiet external helpers, or pipe and consume job output inside the worker. This
also applies to future child-process additions; a stray print can corrupt
framing and deadlock or mis-correlate the native caller.

## Update hardening prerequisite

Updates still use HTTP during this migration phase, but their remote-input boundary is
hardened before native exposure. Release metadata is capped at 2 MiB and validated
against the configured repository, fixed GitHub URL shapes, exact supported artifact
names, and bounded asset fields. Concurrent checks share one locked lookup and publish
strict, atomic state. Release notes are tag-bound, size-limited, HTML-escaped Markdown;
the WebView treats that renderer as its sole remote HTML boundary and renders all other
release metadata as text nodes. Downloads have a 60-second total deadline, a 1 GiB
ceiling, exact declared/received-size checks, an exact GitHub release-CDN host allowlist,
and SHA-256 verification when GitHub supplies a digest. A completed download is
published from a unique temporary file only after validation. ZIP extraction now preflights bounded names, sizes, types, collisions, local-record
ranges, and symlink targets, then securely stages and atomically publishes only an
absent destination with a platform no-replace rename. Release shapes are checked
against the macOS `.app` and Windows `exe`/`app`/`runtime` workflows; unsupported
permissions, links, compression, and encrypted or ambiguous records fail closed.
HTTP update-start accepts only `{}` and admits one canonical newer state/token for the
entire updater-thread lifetime, including failure cleanup; replacement, restart
recovery, and the external Windows helper/journal remain the next prerequisite before
update-start can move to native IPC.

## Browser fallback and migration boundary

The UI keeps its existing transport seams. In a packaged Tauri window,
`preview`, `settings.set`, `offset.set`, `offset.delete`, `jobs.list`,
`jobs.start`, `jobs.log`, `jobs.kill`, aggregate `jobs.poll`, `template.resolve`,
`file.list`, `file.open`, `file.reveal`, `url.open`, and the asynchronous
`repos.refs`, `repos.source.set`, `repos.check`, and `repos.poll` methods, as
well as the three bootstrap reads, use native IPC when a callable Tauri
`invoke` capability exists. In a normal browser there is no Tauri capability:
preview, template resolution, directory metadata, OS actions, repo metadata,
and job list/start/log/kill use the existing HTTP routes and live output uses
the SSE stream. A native invocation failure is reported to the UI; “browser
fallback” means running without the Tauri bridge, not silently hiding a failed
worker call. Binary file reads, save/copy, image deletion, and the
state-changing update-start operation remain HTTP. The repository HTTP routes
remain the browser fallback only; a packaged native failure never retries them.
The packaged smoke test rejects WebView `POST /api/settings` and
`POST /api/offset` after the WebKit marker; browser-mode HTTP fallback remains
allowed, and native failure never retries over HTTP.

The current migration ledger is (21 native methods; the four repository
methods are the latest entries):

| Surface | Current transport | Status |
| --- | --- | --- |
| Bootstrap `info`, `manifest`, `settings.get` reads | Tauri → worker JSON-lines | **This first slice** |
| Static assets, `/`, `/up`, worker-origin navigation | HTTP | Compatibility path |
| Job list/start/kill, log reads, and packaged-Tauri aggregate polling | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP/SSE fallback remains |
| Preview | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains |
| `template.resolve` read-only template metadata | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains |
| `file.list` read-only directory/image metadata | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains |
| `file.open`, `file.reveal`, and `url.open` OS actions | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains; strict roots/URL policy above |
| Binary artifacts/raw file reads | HTTP | Deferred: later bounded artifact/path handling; native actions never return bytes |
| Save/copy and image deletion (`/api/files/save`, `/api/fs`) | HTTP | Deferred: preserve user-selected destinations and destructive-operation guards |
| Settings bootstrap reads and bounded `settings.set` writes | Tauri → worker JSON-lines | **`settings.set` migrated for packaged Tauri**; browser HTTP GET/POST fallback remains; `repos` and unknown schema keys are excluded |
| Repo refs, source selection, check, and poll | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains; remote work is backgrounded and `repo_init`/`repo_update` remain jobs |
| Global and per-size offsets (`offset.set`, `offset.delete`) | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains; canonical state/projection lease is preserved |
| Updates and update-start | HTTP | Deferred: update lifecycle and replacement remain HTTP |
| Static assets, worker-origin navigation, and other compatibility routes | HTTP | Compatibility path; not an OS-action capability |

This ledger is a follow-up checklist, not permission to expand the current
slice. New UI features must go through the transport adapter (`api()` and its
native route selection), not add a direct `fetch()` that bypasses the boundary.
When an endpoint group is migrated, it needs parity coverage against HTTP,
Python and Tauri allowlist updates, UI selection tests, and a packaged smoke
check before its HTTP path is removed. Keep HTTP and the worker-origin
navigation until every relative call has a native transport or an explicit
compatibility proxy. Do not infer HTTP removal from this first slice.

## Upstream ownership

`silhouette-card-maker` and `scm-extras` are always authoritative for
fetching, PDF generation, DXF generation, card layouts, and other repo-specific
functionality. Workbench code only wraps and orchestrates those repositories:
it validates options, persists Workbench state, supervises processes, and
presents their outputs. Native `jobs.start` and `jobs.kill` call the same
Workbench orchestration and upstream child processes as HTTP; native IPC must
not reimplement or fork upstream behavior. The upstream operations remain the
authoritative work.

## Local verification and packaging

From the repository root, the first-slice checks are:

```sh
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
find ui/js -name '*.js' -print0 | xargs -0 -n1 node --check
(cd tauri && cargo fmt --check && cargo test && cargo check --features custom-protocol)
```

The packaged smoke checks use temporary data and
`SCM_WORKBENCH_NO_BOOTSTRAP=1`; they prove that the worker is live, the actual
webview loaded, all three bootstrap reads, `preview`, and `jobs.list` used
native IPC, no bootstrap or migrated route (including repository metadata) was
fetched over HTTP by the WebView, and the worker is reaped. The five existing
startup markers remain
exactly the contract (`info`, `manifest`, `settings.get`, `jobs.list`, and
`preview`); settings and offset writes do not add a startup mutation or fake
marker.
They intentionally do not require
`template.resolve` or `file.list` markers: these methods are read-only metadata
facades and are not deterministically invoked during startup, so CI does not
add fake UI calls or claim WebView markers for them. The executable facade
contract, Python HTTP/native parity, and Rust real-worker coverage prove their
behavior; the runtime smoke guard still rejects WebView HTTP requests to the
migrated metadata routes and, after the WebKit marker, rejects `POST
/api/settings`, `POST /api/offset`, `POST /api/repos/refs`,
`POST /api/repos/save`, `POST /api/repos/check`, `/api/reveal`, plus
`/api/file` action queries containing the exact `open=1`, `reveal=1`, or `url=`
keys in any reasonable query order. It deliberately allows raw `/api/file`
reads, `images_only=1` metadata, and file save/delete compatibility requests.
No repository startup marker is added: the five existing startup IPC markers
remain the complete packaged smoke contract. The lower-layer Python, Rust, and
Node contracts cover the asynchronous repository operations.
Build the shell with
`cargo build --release --features custom-protocol`. `scripts/build.sh macos`
then assembles the local macOS bundle; the packaging workflow is the canonical
assembly path for both platforms.

The supported release matrix is deliberately narrow:

* **macOS ARM64 only** (`macos-14`, `uname -m` must be `arm64`). There is no
  Intel or universal macOS artifact.
* **Windows x64 only** (`AMD64`/MSVC). There is no ARM Windows artifact.

The current signing tradeoff is explicitly accepted for this first slice:
macOS releases are ad-hoc signed, not Developer ID signed or notarized, so a
download may require **Open Anyway** in Privacy & Security. Current Windows
releases are unsigned and may require the one-time SmartScreen prompt. No new
certificate, notarization, or signing work is part of this slice; those are
future release work, not prerequisites for the current artifacts.
