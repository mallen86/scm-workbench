"""The bounded JSON-lines protocol used by the native shell.

This module is deliberately a small adapter around the server's existing read
functions.  It does not expose arbitrary server callables: the allowlisted
methods below are the complete child-process RPC surface for the native slice.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import traceback
from typing import Any, BinaryIO, Dict, Optional, TextIO, Tuple


# A frame includes its trailing newline when one is present.  Keeping this
# limit here (rather than in the Tauri side only) means a directly launched
# worker has the same safety boundary.
MAX_LINE_SIZE = 1024 * 1024
# Responses are complete newline-delimited frames. Keep the limit safely below
# the native reader's 8 MiB ceiling while bounding accidental or hostile data.
MAX_RESPONSE_SIZE = 7 * 1024 * 1024
# Preview has a narrower budget than the general protocol so a large manifest
# value cannot monopolize the native request/response channel.
MAX_PREVIEW_ARGS_SIZE = 512 * 1024
MAX_PREVIEW_RESULT_SIZE = 512 * 1024
# Byte-oriented aliases make the unit explicit for callers and tests.
MAX_PREVIEW_ARGS_BYTES = MAX_PREVIEW_ARGS_SIZE
MAX_PREVIEW_RESULT_BYTES = MAX_PREVIEW_RESULT_SIZE
ALLOWED_METHODS = frozenset((
    "info", "manifest", "settings.get", "settings.set", "preview", "template.resolve", "file.list",
    "file.open", "file.reveal", "url.open",
    "jobs.list", "jobs.start", "jobs.log", "jobs.kill", "jobs.poll",
    "repos.refs", "repos.source.set", "repos.check", "repos.poll",
    "offset.set", "offset.delete",
))


def _bad_params(request_id: str, message: str) -> dict:
    return _error(request_id, "bad_request", message)


def _string(value: Any, name: str) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value


def _bounded_ipc_message(value: Any) -> str:
    text = " ".join(str(value or "bad request").split())
    return text[:256] or "bad request"


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _error(request_id: Any, code: str, message: str) -> dict:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def _success(request_id: str, result: Any) -> dict:
    return {"id": request_id, "ok": True, "result": result}


def dispatch(request: dict) -> dict:
    """Validate and execute one already-decoded request object."""
    request_id = request.get("id")
    if not isinstance(request_id, str) or not request_id:
        return _error(None, "bad_request", "request id must be a non-empty string")

    method = request.get("method")
    if not isinstance(method, str):
        return _error(request_id, "bad_request", "method must be a string")
    if method not in ALLOWED_METHODS:
        return _error(request_id, "unknown_method", f"unknown method: {method}")

    params = request.get("params")
    if not isinstance(params, dict):
        return _error(request_id, "bad_request", "params must be an object")

    # Import lazily so importing this adapter never creates a second server or
    # introduces an import cycle while server.py is being loaded.
    from scm_workbench import server

    try:
        if method in ("info", "manifest", "settings.get", "jobs.list") and params:
            return _bad_params(request_id, f"{method} does not accept parameters")
        if method == "info":
            result = server.get_info()
        elif method == "manifest":
            result = server.get_manifest()
        elif method == "settings.get":
            result = server.load_settings()
        elif method == "repos.refs":
            if set(params) != {"repo"} or not isinstance(params.get("repo"), str):
                return _bad_params(request_id, "repos.refs requires exactly repo")
            try:
                repo = server.repo_sync.validate_repo_key(params["repo"])
            except server.repo_sync.RepoError:
                return _bad_params(request_id, "unknown repository")
            result = server._start_repo_operation("refs", {"repo": repo})
        elif method == "repos.source.set":
            if set(params) != {"repo", "source"} or not isinstance(params.get("repo"), str):
                return _bad_params(request_id, "repos.source.set requires exactly repo and source")
            try:
                repo = server.repo_sync.validate_repo_key(params["repo"])
                source = server.repo_sync.validate_source(params["source"])
            except server.repo_sync.RepoError as exc:
                return _bad_params(request_id, _bounded_ipc_message(exc))
            result = server._start_repo_operation("source.set", {"repo": repo, "source": source})
        elif method == "repos.check":
            if set(params) != {"repo", "force"} or not isinstance(params.get("repo"), str):
                return _bad_params(request_id, "repos.check requires exactly repo and force")
            if not isinstance(params.get("force"), bool):
                return _bad_params(request_id, "repos.check force must be boolean")
            try:
                repo = server.repo_sync.validate_repo_key(params["repo"])
            except server.repo_sync.RepoError:
                return _bad_params(request_id, "unknown repository")
            result = server._start_repo_operation("check", {"repo": repo, "force": params["force"]})
        elif method == "repos.poll":
            if set(params) != {"operation_id"} or not isinstance(params.get("operation_id"), str) \
                    or not params["operation_id"] or len(params["operation_id"]) > 64:
                return _bad_params(request_id, "repos.poll requires exactly operation_id")
            result = server.poll_repo_operation(params["operation_id"])
            if not result.get("ok"):
                error = result.get("error") or {}
                return _error(request_id, "bad_request", _bounded_ipc_message(error.get("message", "operation not found")))
        elif method == "settings.set":
            if set(params) != {"changes"}:
                return _bad_params(request_id, "settings.set requires exactly changes")
            changes = params["changes"]
            if not isinstance(changes, dict):
                return _bad_params(request_id, "settings.set changes must be an object")
            # Value validation, the 64 KiB encoded budget, merge semantics, and
            # persistence are shared with HTTP /api/settings. Invalid values
            # are application results so both transports expose the same body.
            result = server.update_settings(changes)
        elif method == "jobs.list":
            result = server.list_jobs()
        elif method == "template.resolve":
            if set(params) != {"paper", "card", "borderless"}:
                return _bad_params(request_id, "template.resolve requires exactly paper, card, and borderless")
            paper = params["paper"]
            card = params["card"]
            if not isinstance(paper, str) or not paper or not isinstance(card, str) or not card:
                return _bad_params(request_id, "template.resolve paper and card must be non-empty strings")
            try:
                if len(paper.encode("utf-8")) > 128 or len(card.encode("utf-8")) > 128:
                    return _bad_params(request_id, "template.resolve paper and card exceed 128 UTF-8 bytes")
            except UnicodeEncodeError:
                return _bad_params(request_id, "template.resolve paper and card must be valid UTF-8")
            if not isinstance(params["borderless"], bool):
                return _bad_params(request_id, "template.resolve borderless must be boolean")
            result = server.resolve_template(paper, card, params["borderless"])
        elif method == "file.list":
            if set(params) != {"path", "images_only"}:
                return _bad_params(request_id, "file.list requires exactly path and images_only")
            path = params["path"]
            if not isinstance(path, str) or not path:
                return _bad_params(request_id, "file.list path must be a non-empty string")
            try:
                if len(path.encode("utf-8")) > 4096:
                    return _bad_params(request_id, "file.list path exceeds 4096 UTF-8 bytes")
            except UnicodeEncodeError:
                return _bad_params(request_id, "file.list path must be valid UTF-8")
            if not isinstance(params["images_only"], bool):
                return _bad_params(request_id, "file.list images_only must be boolean")
            try:
                result = server.list_files(path, params["images_only"])
            except server.FileListError as error:
                if error.code == "not_found":
                    result = {"exists": False, "items": [], "truncated": False,
                              "scanned": 0, "found": 0}
                else:
                    return _error(request_id, error.code, error.message)
        elif method in ("file.open", "file.reveal"):
            if set(params) != {"path"}:
                return _bad_params(request_id, f"{method} requires exactly path")
            path = params["path"]
            if not isinstance(path, str) or not path:
                return _bad_params(request_id, f"{method} path must be a non-empty string")
            try:
                if len(path.encode("utf-8")) > server.ACTION_PATH_MAX_BYTES:
                    return _bad_params(request_id, f"{method} path exceeds 4096 UTF-8 bytes")
            except UnicodeEncodeError:
                return _bad_params(request_id, f"{method} path must be valid UTF-8")
            if server.has_forbidden_action_controls(path):
                return _bad_params(request_id, f"{method} path contains control characters")
            if method == "file.open":
                result, _status = server.file_open_action(path)
            else:
                result, _status = server.file_reveal_action(path)
        elif method == "url.open":
            if set(params) != {"url"}:
                return _bad_params(request_id, "url.open requires exactly url")
            url = params["url"]
            if not isinstance(url, str) or not url:
                return _bad_params(request_id, "url.open url must be a non-empty string")
            try:
                if len(url.encode("utf-8")) > server.ACTION_URL_MAX_BYTES:
                    return _bad_params(request_id, "url.open url exceeds 8192 UTF-8 bytes")
            except UnicodeEncodeError:
                return _bad_params(request_id, "url.open url must be valid UTF-8")
            if server.has_forbidden_action_controls(url):
                return _bad_params(request_id, "url.open url contains control characters")
            result, _status = server.url_open_action(url)
        elif method == "preview":
            if set(params) != {"kind", "args"}:
                return _bad_params(request_id, "preview requires exactly string kind and object args")
            kind = params["kind"]
            if not isinstance(kind, str) or not kind:
                return _bad_params(request_id, "preview kind must be a non-empty string")
            try:
                kind_size = len(kind.encode("utf-8"))
            except UnicodeEncodeError:
                return _bad_params(request_id, "preview kind must be valid UTF-8")
            if kind_size > 128:
                return _bad_params(request_id, "preview kind exceeds 128 UTF-8 bytes")
            args = params["args"]
            if not isinstance(args, dict):
                return _bad_params(request_id, "preview args must be an object")
            try:
                args_size = len(json.dumps(
                    args, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8"))
            except (TypeError, ValueError, UnicodeError):
                return _bad_params(request_id, "preview args must be valid JSON")
            if args_size > MAX_PREVIEW_ARGS_SIZE:
                return _bad_params(request_id, "preview args exceed 512 KiB when encoded")
            try:
                result = server.build_preview(kind, args)
            except server.PreviewError as error:
                return _error(request_id, error.ipc_code, error.message)
        elif method == "offset.set":
            if set(params) != {"size", "x", "y", "angle"}:
                return _bad_params(request_id, "offset.set requires exactly size, x, y, and angle")
            size = params["size"]
            if size is not None and not isinstance(size, str):
                return _bad_params(request_id, "offset.set size must be null or a string")
            if size is not None:
                try:
                    size_bytes = len(size.encode("utf-8"))
                except UnicodeEncodeError:
                    return _bad_params(request_id, "offset.set size must be valid UTF-8")
                if server._offset_name(size) is None:
                    return _bad_params(request_id, "offset.set size is empty, too long, or contains controls")
            x, y, angle = params["x"], params["y"], params["angle"]
            if (not isinstance(x, int) or isinstance(x, bool) or not -100000 <= x <= 100000 or
                    not isinstance(y, int) or isinstance(y, bool) or not -100000 <= y <= 100000):
                return _bad_params(request_id, "offset.set x and y must be integers from -100000 through 100000")
            try:
                clean_angle = float(angle)
            except (TypeError, ValueError, OverflowError):
                clean_angle = math.nan
            if (not isinstance(angle, (int, float)) or isinstance(angle, bool) or
                    not math.isfinite(clean_angle) or not -360 <= clean_angle <= 360):
                return _bad_params(request_id, "offset.set angle must be finite and from -360 through 360")
            result = server.set_offset(size, x, y, angle)
        elif method == "offset.delete":
            if set(params) != {"size"}:
                return _bad_params(request_id, "offset.delete requires exactly size")
            size = params["size"]
            if not isinstance(size, str):
                return _bad_params(request_id, "offset.delete size must be a string")
            try:
                size_bytes = len(size.encode("utf-8"))
            except UnicodeEncodeError:
                return _bad_params(request_id, "offset.delete size must be valid UTF-8")
            if server._offset_name(size) is None:
                return _bad_params(request_id, "offset.delete size is empty, too long, or contains controls")
            result = server.delete_offset(size)
        elif method == "jobs.start":
            if set(params) != {"kind", "args"} or not _string(params.get("kind"), "kind"):
                return _bad_params(request_id, "jobs.start requires string kind and object args")
            if not isinstance(params.get("args"), dict):
                return _bad_params(request_id, "jobs.start args must be an object")
            job, errors = server.start_job(params["kind"], params["args"])
            if errors:
                result = {"ok": False, "errors": errors}
            else:
                result = {"ok": True, "job": {
                    "id": job["id"], "title": job["title"], "status": job["status"],
                    "cmd": job["cmd"], "warnings": job.get("warnings", []),
                }}
        elif method == "jobs.kill":
            if set(params) != {"job_id"} or not _string(params.get("job_id"), "job_id"):
                return _bad_params(request_id, "jobs.kill requires string job_id")
            result = {"ok": bool(server.kill_job(params["job_id"]))}
        elif method == "jobs.log":
            allowed = {"job_id", "after", "max_lines"}
            if not set(params).issubset(allowed) or not _string(params.get("job_id"), "job_id"):
                return _bad_params(request_id, "jobs.log requires string job_id")
            after = params.get("after", 0)
            max_lines = params.get("max_lines", server.JOB_LOG_MAX_LINES)
            if not _nonnegative_int(after) or not isinstance(max_lines, int) or isinstance(max_lines, bool) \
                    or not (1 <= max_lines <= server.JOB_LOG_MAX_LINES):
                return _bad_params(request_id, "jobs.log cursor or max_lines is out of range")
            result = server.get_job_log(params["job_id"], after, max_lines)
        else:  # jobs.poll
            allowed = {"cursors", "max_events"}
            if set(params) != allowed or not isinstance(params.get("cursors"), list):
                return _bad_params(request_id, "jobs.poll requires cursors and max_events")
            cursors = params["cursors"]
            max_events = params["max_events"]
            if len(cursors) > 32 or not isinstance(max_events, int) or isinstance(max_events, bool) \
                    or not (1 <= max_events <= 256):
                return _bad_params(request_id, "jobs.poll accepts at most 32 cursors and max_events 1..256")
            clean = []
            for cursor in cursors:
                if not isinstance(cursor, dict) or set(cursor) != {"job_id", "after"} \
                        or not _string(cursor.get("job_id"), "job_id") \
                        or not _nonnegative_int(cursor.get("after")):
                    return _bad_params(request_id, "each poll cursor requires job_id and nonnegative after")
                clean.append({"job_id": cursor["job_id"], "after": cursor["after"]})
            result = server.poll_jobs(clean, max_events)
    except Exception:
        # Keep exception details out of the wire contract.  The traceback is
        # useful to the supervising shell and belongs on stderr, not stdout.
        traceback.print_exc(file=sys.stderr)
        return _error(request_id, "internal", "request handler failed")
    if method == "preview":
        # This is deliberately outside the handler exception block: a genuine
        # build failure remains an ``internal`` error, never a size error.
        try:
            result_size = len(json.dumps(
                result, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return _error(request_id, "internal", "request handler failed")
        if result_size > MAX_PREVIEW_RESULT_SIZE:
            return _error(request_id, "result_too_large", "preview result exceeds 512 KiB")
    # stdout is exclusively the JSON-lines protocol.  This concise stderr
    # marker lets packaged smoke tests prove the native dispatch path without
    # corrupting a response frame.
    print(f"[ipc] served {method}", file=sys.stderr, flush=True)
    return _success(request_id, result)


def process_line(line: bytes | str, *, max_line_size: int = MAX_LINE_SIZE) -> dict:
    """Turn one input line into exactly one protocol response.

    This function is also useful for deterministic unit tests.  Framing (and
    draining the remainder of an overlong physical line) is handled by
    :func:`serve_stdio`; callers passing a complete line get the same size and
    UTF-8 validation here.
    """
    if isinstance(line, str):
        try:
            raw = line.encode("utf-8")
        except UnicodeEncodeError:
            return _error(None, "bad_request", "input is not valid UTF-8")
    else:
        raw = line
    if len(raw) > max_line_size:
        return _error(None, "bad_request", "input line exceeds 1 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _error(None, "bad_request", "input is not valid UTF-8")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _error(None, "bad_request", "input is not valid JSON")
    if not isinstance(value, dict):
        return _error(None, "bad_request", "request must be a JSON object")
    return dispatch(value)


def _read_physical_line(stream: BinaryIO | TextIO, max_line_size: int) -> Tuple[Optional[bytes], bool]:
    """Read one line and report whether it exceeded the bound.

    ``readline(size)`` may leave the rest of an overlong line buffered.  Drain
    that remainder so the next response still corresponds to the next input
    line rather than to a suffix of the rejected frame.
    """
    try:
        value = stream.readline(max_line_size + 1)
    except TypeError:  # a small test stream may not accept the size argument
        value = stream.readline()
    if value in (b"", ""):
        return None, False
    raw = value if isinstance(value, bytes) else value.encode("utf-8", "surrogatepass")
    oversized = len(raw) > max_line_size
    if oversized and b"\n" not in raw:
        while True:
            chunk = stream.readline()
            if chunk in (b"", ""):
                break
            chunk_bytes = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8", "surrogatepass")
            if b"\n" in chunk_bytes:
                break
    return raw, oversized


def _encode_response(response: dict, max_response_size: int) -> bytes:
    encoded = json.dumps(response, default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) <= max_response_size:
        return encoded
    # Do not truncate JSON: replace the entire result with a bounded error
    # frame so the next response remains aligned on a newline.
    fallback = _error(response.get("id"), "internal", "response exceeds configured limit")
    fallback_bytes = json.dumps(fallback, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(fallback_bytes) <= max_response_size:
        return fallback_bytes
    # An opaque request id is itself bounded only by the input-frame limit;
    # never let it defeat the response bound.
    minimal = b'{"id":null,"ok":false,"error":{"code":"internal","message":"response exceeds configured limit"}}\n'
    return minimal if len(minimal) <= max_response_size else b'{"id":null}\n'


def _write(output: BinaryIO | TextIO, response: dict,
           max_response_size: int = MAX_RESPONSE_SIZE) -> None:
    encoded = _encode_response(response, max_response_size)
    try:
        output.write(encoded)
    except TypeError:
        output.write(encoded.decode("utf-8"))
    output.flush()


def serve_stdio(input_stream: BinaryIO | TextIO = None,
                output_stream: BinaryIO | TextIO = None,
                *, max_line_size: int = MAX_LINE_SIZE,
                max_response_size: int = MAX_RESPONSE_SIZE,
                on_eof=None) -> None:
    """Serve requests from stdin until EOF, flushing one response per line."""
    source = input_stream if input_stream is not None else getattr(sys.stdin, "buffer", sys.stdin)
    sink = output_stream if output_stream is not None else getattr(sys.stdout, "buffer", sys.stdout)
    while True:
        try:
            line, oversized = _read_physical_line(source, max_line_size)
        except (OSError, ValueError):
            # A closed pipe is an ordinary shutdown path for a supervised
            # child.  Do not put diagnostics on the protocol stream.
            return
        if line is None:
            if on_eof is not None:
                on_eof()
            return
        if oversized:
            response = _error(None, "bad_request", "input line exceeds 1 MiB")
        else:
            response = process_line(line, max_line_size=max_line_size)
        try:
            _write(sink, response, max_response_size)
        except (BrokenPipeError, OSError, ValueError):
            return


def start_thread(*, input_stream: BinaryIO | TextIO = None,
                 output_stream: BinaryIO | TextIO = None,
                 max_line_size: int = MAX_LINE_SIZE,
                 max_response_size: int = MAX_RESPONSE_SIZE,
                 on_eof=None) -> threading.Thread:
    """Start the adapter in a daemon thread and return the thread object."""
    thread = threading.Thread(
        target=serve_stdio,
        kwargs={"input_stream": input_stream, "output_stream": output_stream,
                "max_line_size": max_line_size, "max_response_size": max_response_size,
                "on_eof": on_eof},
        daemon=True,
        name="ipc-reader",
    )
    thread.start()
    return thread


# Explicit aliases make the child entry point easy to discover for embedders
# without creating another protocol implementation.
run = serve_stdio
start = start_thread
