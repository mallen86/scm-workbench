"""One-process callback runner for the image post-processing contract.

The parent creates a private manifest and staged files, then invokes this file
in a separate process.  The module is imported once and its synchronous
``process_image(path, context)`` function is called once per entry.  This file
never knows the managed SCM destination and never publishes results.
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any

MAX_MANIFEST_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_MESSAGE_BYTES = 4096
PROGRESS_PREFIX = "WB_POSTPROCESS_PROGRESS "
ERROR_PREFIX = "WB_POSTPROCESS_ERROR "


class RunnerError(Exception):
    pass


def _bounded_text(value: Any, limit: int = MAX_MESSAGE_BYTES) -> str:
    text = str(value).replace("\x00", " ")
    return " ".join(text.split())[:limit]


def _emit(prefix: str, value: dict) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raw = json.dumps({"error": "runner message too large"})
    print(prefix + raw, flush=True)


def _load_manifest(path: Path) -> dict:
    if not path.is_absolute():
        raise RunnerError("manifest must be an absolute path")
    if path.is_symlink() or not path.is_file():
        raise RunnerError("manifest is not a regular file")
    raw = path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RunnerError("manifest is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("manifest is invalid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise RunnerError("manifest has invalid shape")
    return value


def _load_processor(manifest: dict):
    source = manifest.get("source")
    source_path = manifest.get("source_path")
    if isinstance(source_path, str):
        path = Path(source_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise RunnerError("processor source is not a regular file")
        source = path.read_text(encoding="utf-8")
        source_identity = (path.stat().st_dev, path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns)
    elif isinstance(source, str):
        source_identity = None
    else:
        raise RunnerError("processor source is missing")
    if "\x00" in source or len(source.encode("utf-8")) > 256 * 1024:
        raise RunnerError("processor source is invalid")
    namespace = {"__name__": "scm_workbench_trusted_processor"}
    code = compile(source, str(source_path or "<processor>"), "exec")
    exec(code, namespace, namespace)
    callback = namespace.get("process_image")
    if not callable(callback) or getattr(callback, "__code__", None) is None:
        raise RunnerError("processor does not define process_image")
    try:
        signature = inspect.signature(callback)
        parameters = list(signature.parameters.values())
        if len(parameters) != 2 or any(parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD, parameter.KEYWORD_ONLY) for parameter in parameters) or any(parameter.default is not parameter.empty for parameter in parameters):
            raise RunnerError("process_image must accept exactly (image_path, context)")
    except (TypeError, ValueError) as exc:
        raise RunnerError("could not inspect process_image") from exc
    return callback, source_identity


def _set_limits(manifest: dict) -> None:
    """Apply best-effort POSIX limits before importing user code."""
    if os.name != "posix":
        return
    try:
        import resource
        limits = manifest.get("limits") if isinstance(manifest.get("limits"), dict) else {}
        for name, resource_name in (("cpu_seconds", "RLIMIT_CPU"), ("address_space", "RLIMIT_AS"), ("file_size", "RLIMIT_FSIZE"), ("open_files", "RLIMIT_NOFILE"), ("processes", "RLIMIT_NPROC")):
            value = limits.get(name)
            if isinstance(value, int) and value > 0 and hasattr(resource, resource_name):
                kind = getattr(resource, resource_name)
                soft, hard = resource.getrlimit(kind)
                new_soft = min(value, hard) if hard != resource.RLIM_INFINITY else value
                resource.setrlimit(kind, (new_soft, hard))
    except (ImportError, OSError, ValueError):
        # Resource support differs between macOS, Linux, and Windows. The
        # parent still owns wall-clock and process-tree termination.
        pass


def run(manifest_path: str | Path) -> int:
    manifest = _load_manifest(Path(manifest_path))
    _set_limits(manifest)
    callback, source_identity = _load_processor(manifest)
    entries = manifest["entries"]
    total = len(entries)
    run_root = Path(str(manifest.get("run_root", ""))).resolve()
    if not run_root.is_absolute() or not run_root.is_dir():
        raise RunnerError("run root is invalid")
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise RunnerError("manifest entry is invalid")
        image = Path(str(entry.get("staged", "")))
        try:
            image.resolve().relative_to(run_root)
        except ValueError as exc:
            raise RunnerError("staged image escapes run root") from exc
        if image.is_symlink() or not image.is_file():
            raise RunnerError("staged image is not a regular file")
        context = {
            "role": entry.get("role"), "relative_path": entry.get("relative_path"),
            "name": entry.get("name"), "index": index, "total": total,
        }
        result = callback(image, context)
        if result is not None:
            raise RunnerError("process_image must return None")
        _emit(PROGRESS_PREFIX, {"index": index, "total": total, "name": _bounded_text(entry.get("name", "")), "role": _bounded_text(entry.get("role", ""))})
    if source_identity is not None:
        path = Path(str(manifest["source_path"]))
        try:
            now = path.stat()
            if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != source_identity:
                raise RunnerError("processor source changed during execution")
        except OSError as exc:
            raise RunnerError("processor source could not be rechecked") from exc
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.manifest)
    except KeyboardInterrupt:
        _emit(ERROR_PREFIX, {"error": "cancelled"})
        return 130
    except Exception as exc:
        _emit(ERROR_PREFIX, {"error": _bounded_text(exc)})
        # Detailed traceback is intentionally bounded and goes to stderr only.
        traceback.print_exc(limit=8, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
