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
calls use relative HTTP URLs. The native protocol is used only for the three
bootstrap reads below.

A source checkout still uses the browser development flow: `python -m
scm_workbench` (or `python -m scm_workbench.server`) starts the HTTP server and
opens or prints a local browser URL. The packaged Tauri shell does not open a
browser for its worker.

## JSON-lines protocol

Each request and response is one UTF-8 JSON object terminated by `\n`. The
worker flushes after every response. The input frame is limited to 1 MiB and
the complete response frame is limited to 8 MiB; an oversized result becomes a
bounded error response rather than a truncated JSON frame.

A request has this exact shape:

```json
{"id":"rpc-1","method":"info","params":{}}
```

`id` must be a non-empty string. `method` must be a string in the allowlist
below, and `params` must be a JSON object. The first slice has no method
parameters, so callers send `{}`.

| HTTP compatibility read | Native method | Python implementation |
| --- | --- | --- |
| `GET /api/info` | `info` | `server.get_info()` |
| `GET /api/manifest` | `manifest` | `server.get_manifest()` |
| `GET /api/settings` | `settings.get` | `server.load_settings()` |

Those three HTTP routes remain served as compatibility endpoints; native
selection is a client transport choice, not their removal.

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
* `unknown_method` — a method outside the three-method allowlist.
* `internal` — the existing handler failed or the response exceeded the
  configured limit. Handler details are written to stderr, not exposed on the
  wire.

There is no arbitrary Python callable, path dispatch, or job operation in
this protocol. Protocol frames are the only data written to the worker's
stdout; startup text and diagnostics go to stderr (captured in the packaged
worker log).

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

### Stdout reservation rule

Every child process launched under an IPC worker must obey this rule: **the
worker's stdout is reserved exclusively for JSON protocol frames**. A child
must not inherit it. Send diagnostics to stderr or a log, use `DEVNULL` for
quiet external helpers, or pipe and consume job output inside the worker. This
also applies to future child-process additions; a stray print can corrupt
framing and deadlock or mis-correlate the native caller.

## Browser fallback and migration boundary

The UI keeps its existing `api(path, body)` seam. In a Tauri window, only a
body-less `GET` for the three routes in the table is selected for native IPC
when a callable Tauri `invoke` capability exists. In a normal browser there is
no Tauri capability, so those reads use the existing HTTP endpoint. All other
requests use HTTP in both environments. A native invocation failure is
reported to the UI; “browser fallback” means running without the Tauri bridge,
not silently hiding a failed worker call.

The current migration ledger is:

| Surface | Current transport | Status |
| --- | --- | --- |
| Bootstrap `info`, `manifest`, `settings.get` reads | Tauri → worker JSON-lines | **This first slice** |
| Static assets, `/`, `/up`, worker-origin navigation | HTTP | Compatibility path |
| Jobs, job logs, and SSE streams | HTTP | Later: needs streaming, events, and backpressure |
| Preview, generated artifacts, templates, and file access | HTTP | Later: needs bounded artifact/path handling |
| Settings writes, repo sync/ref actions, and offsets | HTTP | Later: preserve atomic state writes and ref resolution |
| Updates and external filesystem/open actions | HTTP | Later: strict capability/root validation |

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
presents their outputs. Native IPC must not reimplement or fork that behavior.

## Local verification and packaging

From the repository root, the first-slice checks are:

```sh
python -m unittest discover -s tests -v
python scripts/check_ui_imports.py
python scripts/check_ui_transport.py
find ui/js -name '*.js' -print0 | xargs -0 -n1 node --check
(cd tauri && cargo fmt --check && cargo test && cargo check --features custom-protocol)
```

The packaged smoke checks use temporary data and
`SCM_WORKBENCH_NO_BOOTSTRAP=1`; they prove that the worker is live, the actual
webview loaded, all three bootstrap reads used native IPC, no bootstrap route
was fetched over HTTP, and the worker is reaped. Build the shell with
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
