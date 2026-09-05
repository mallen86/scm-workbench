"""The bounded JSON-lines protocol used by the native shell.

This module is deliberately a small adapter around the server's existing read
functions.  It does not expose arbitrary server callables: the three methods
below are the complete child-process RPC surface for the first native slice.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import Any, BinaryIO, Dict, Optional, TextIO, Tuple


# A frame includes its trailing newline when one is present.  Keeping this
# limit here (rather than in the Tauri side only) means a directly launched
# worker has the same safety boundary.
MAX_LINE_SIZE = 1024 * 1024
# Responses are complete newline-delimited frames.  8 MiB is intentionally
# above the current real manifest size while still bounding accidental or
# hostile results before they reach the native reader.
MAX_RESPONSE_SIZE = 8 * 1024 * 1024
ALLOWED_METHODS = frozenset(("info", "manifest", "settings.get"))


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
        if method == "info":
            result = server.get_info()
        elif method == "manifest":
            result = server.get_manifest()
        else:  # settings.get
            result = server.load_settings()
    except Exception:
        # Keep exception details out of the wire contract.  The traceback is
        # useful to the supervising shell and belongs on stderr, not stdout.
        traceback.print_exc(file=sys.stderr)
        return _error(request_id, "internal", "request handler failed")
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
    encoded = json.dumps(response, default=str, separators=(",", ":")).encode("utf-8") + b"\n"
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
