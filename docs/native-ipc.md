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
reads, preview, packaged-Tauri job control/log operations, and the read-only
`template.resolve`/`file.list` metadata slice below; other surfaces remain on
their existing HTTP compatibility paths.

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
below, and `params` must be a JSON object. The bootstrap read methods take
`{}`; preview, job, and artifact metadata methods use the parameter contracts
below.

| HTTP compatibility route | Native method | Python implementation |
| --- | --- | --- |
| `GET /api/info` | `info` | `server.get_info()` |
| `GET /api/manifest` | `manifest` | `server.get_manifest()` |
| `GET /api/settings` | `settings.get` | `server.load_settings()` |
| `GET /api/jobs` | `jobs.list` | `server.list_jobs()` |
| `POST /api/jobs` | `jobs.start` | `server.start_job()` |
| `GET /api/jobs/<id>/log` | `jobs.log` | `server.get_job_log()` |
| `POST /api/jobs/<id>/kill` | `jobs.kill` | `server.kill_job()` |
| `GET /api/jobs/<id>/stream` | `jobs.poll` (packaged Tauri) | `server.poll_jobs()` |
| `GET /api/preview` | `preview` (packaged Tauri) | `server.build_preview()` |
| `GET /api/template` | `template.resolve` (packaged Tauri) | `server.resolve_template()` |
| `GET /api/file?...images_only=1` (directory metadata) | `file.list` (packaged Tauri) | `server.list_files()` |

Those HTTP routes remain served as compatibility endpoints; native selection is
a client transport choice, not their removal. The methods use these exact
parameter and result shapes inside the common RPC envelope:

* `preview`: params `{"kind":"<string>","args":{...}}` (exactly those two
  keys). `kind` is at most 128 UTF-8 bytes. The JSON encoding of the `args`
  object is at most 512 KiB. The result is the same object as `GET
  /api/preview` (`cmd`, `cwd`, `env`, `warnings`, `errors`, and
  `no_front_images`) and its encoded JSON is at most 512 KiB.
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
* `unknown_method` — a method outside the eleven-method allowlist.
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
allowlist before writing to the child. Python validates it again. This is a
transport/orchestration boundary, not a general native escape hatch.

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

## Browser fallback and migration boundary

The UI keeps its existing transport seams. In a packaged Tauri window,
`preview`, `jobs.list`, `jobs.start`, `jobs.log`, `jobs.kill`, aggregate
`jobs.poll`, `template.resolve`, and `file.list`, as well as the three
bootstrap reads, use native IPC when a callable Tauri `invoke` capability
exists. In a normal browser there is no Tauri capability: preview, template
resolution, directory metadata, and job list/start/log/kill use the existing
HTTP routes and live output uses the SSE stream. A native invocation failure is
reported to the UI; “browser fallback” means running without the Tauri bridge,
not silently hiding a failed worker call. Binary file reads, `/api/file` open
and reveal actions, and the state-changing update-start operation remain HTTP.

The current migration ledger is:

| Surface | Current transport | Status |
| --- | --- | --- |
| Bootstrap `info`, `manifest`, `settings.get` reads | Tauri → worker JSON-lines | **This first slice** |
| Static assets, `/`, `/up`, worker-origin navigation | HTTP | Compatibility path |
| Job list/start/kill, log reads, and packaged-Tauri aggregate polling | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP/SSE fallback remains |
| Preview | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains |
| `template.resolve` read-only template metadata | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains |
| `file.list` read-only directory/image metadata | Tauri → worker JSON-lines | **Migrated for packaged Tauri**; browser HTTP fallback remains |
| Binary artifacts/file reads and `/api/file` open/reveal actions | HTTP | Later: needs bounded artifact/path handling and OS-action capability design |
| Settings writes, repo sync/ref actions, and offsets | HTTP | Later: preserve atomic state writes and ref resolution |
| Updates and external filesystem/open actions | HTTP | Update-start remains HTTP; later: strict capability/root validation |

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
python scripts/check_ui_preview.py
python scripts/check_ui_artifacts.py
find ui/js -name '*.js' -print0 | xargs -0 -n1 node --check
(cd tauri && cargo fmt --check && cargo test && cargo check --features custom-protocol)
```

The packaged smoke checks use temporary data and
`SCM_WORKBENCH_NO_BOOTSTRAP=1`; they prove that the worker is live, the actual
webview loaded, all three bootstrap reads, `preview`, and `jobs.list` used
native IPC, no bootstrap or migrated route was fetched over HTTP by the
WebView, and the worker is reaped. They intentionally do not require
`template.resolve` or `file.list` markers: these methods are read-only metadata
facades and are not deterministically invoked during startup, so CI does not
add fake UI calls or claim WebView markers for them. The executable facade
contract, Python HTTP/native parity, and Rust real-worker coverage prove their
behavior; the runtime smoke guard still rejects a WebView HTTP request to the
migrated `/api/template` or `images_only=1` directory-list route after the
WebKit marker, while deliberately allowing the still-unmigrated `/api/file`
`open=1` action. The lower-layer Python, Rust, and Node contracts cover the
remaining operations. Build the shell with
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
