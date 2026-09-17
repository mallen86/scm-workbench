"""One-process callback runner for the image post-processing contract.

The parent creates a private manifest and staged files, then invokes this file
in a separate process.  The module is imported once and its synchronous
``process_image(path, context)`` function is called once per entry.  This file
never knows the managed SCM destination and never publishes results.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import signal
import stat
import sys
import unicodedata
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


def _read_regular(path: Path, label: str, limit: int) -> tuple[bytes, tuple[int, int, int, int, int]]:
    try:
        before = os.lstat(path)
        if (stat.S_ISLNK(before.st_mode) or getattr(before, "st_reparse_tag", 0) or
                not stat.S_ISREG(before.st_mode) or getattr(before, "st_nlink", 1) != 1 or
                before.st_size > limit):
            raise RunnerError(f"{label} is not a bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                        getattr(opened, "st_nlink", 1))
            if identity != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                            getattr(before, "st_nlink", 1)):
                raise RunnerError(f"{label} changed while opening")
            chunks = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk); remaining -= len(chunk)
            raw = b"".join(chunks)
            after_read = os.fstat(fd)
        finally:
            os.close(fd)
        after = os.stat(path, follow_symlinks=False)
        if (len(raw) > limit or
                (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns,
                 getattr(after_read, "st_nlink", 1)) != identity or
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 getattr(after, "st_nlink", 1)) != identity):
            raise RunnerError(f"{label} changed while reading")
        return raw, identity
    except RunnerError:
        raise
    except OSError as exc:
        raise RunnerError(f"could not read {label}") from exc


def _load_manifest(path: Path) -> dict:
    if not path.is_absolute():
        raise RunnerError("manifest must be an absolute path")
    raw, _identity = _read_regular(path, "manifest", MAX_MANIFEST_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("manifest is invalid JSON") from exc
    required = {"source_path", "run_root", "entries", "environment", "environment_root",
                "revision", "requirements", "contract", "limits"}
    if not isinstance(value, dict) or set(value) != required or not isinstance(value.get("entries"), list):
        raise RunnerError("manifest has invalid shape")
    for key in ("source_path", "run_root", "environment", "environment_root", "revision", "contract"):
        if not isinstance(value.get(key), str) or len(value[key].encode("utf-8")) > 4096:
            raise RunnerError("manifest has invalid fields")
    requirements = value.get("requirements")
    if (not isinstance(requirements, list) or len(requirements) > 32 or
            any(not isinstance(item, str) or len(item.encode("utf-8")) > 256 for item in requirements) or
            sum(len(item.encode("utf-8")) + 1 for item in requirements) > 8192):
        raise RunnerError("manifest requirements are invalid")
    limits = value.get("limits")
    maximums = {"cpu_seconds": 3600, "address_space": 16 * 1024 * 1024 * 1024,
                "file_size": 1024 * 1024 * 1024, "open_files": 1024, "processes": 64}
    if (not isinstance(limits, dict) or set(limits) != set(maximums) or
            any(not isinstance(limits[key], int) or isinstance(limits[key], bool) or
                not (0 < limits[key] <= maximums[key]) for key in maximums)):
        raise RunnerError("manifest limits are invalid")
    return value


def _load_processor(manifest: dict):
    source_path = manifest["source_path"]
    path = Path(source_path)
    run_root = Path(manifest["run_root"])
    if (not path.is_absolute() or path != run_root / "processor.py"):
        raise RunnerError("processor source is not a regular file")
    raw, source_identity = _read_regular(path, "processor source", 256 * 1024)
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerError("processor source is not UTF-8") from exc
    raw_source = source.encode("utf-8")
    if "\x00" in source or len(raw_source) > 256 * 1024:
        raise RunnerError("processor source is invalid")
    expected_revision = manifest.get("revision")
    requirements = manifest.get("requirements")
    contract = manifest.get("contract")
    if not isinstance(expected_revision, str) or not isinstance(requirements, list) or not isinstance(contract, str):
        raise RunnerError("processor revision identity is missing")
    material = (raw_source + b"\n\0" +
                json.dumps(tuple(requirements), separators=(",", ":")).encode("utf-8") +
                b"\n\0" + contract.encode("utf-8"))
    if hashlib.sha256(material).hexdigest() != expected_revision:
        raise RunnerError("processor source does not match its approved revision")
    namespace = {"__name__": "scm_workbench_trusted_processor"}
    code = compile(source, source_path, "exec")
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


def _configure_environment(manifest: dict) -> None:
    """Expose only the selected private dependency directory to user imports."""
    raw = manifest.get("environment")
    if raw in (None, ""):
        return
    root_raw = manifest.get("environment_root")
    if not isinstance(raw, str) or not isinstance(root_raw, str):
        raise RunnerError("dependency environment is invalid")
    path = Path(raw)
    root = Path(root_raw)
    if not path.is_absolute() or not root.is_absolute():
        raise RunnerError("dependency environment is invalid")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise RunnerError("dependency environment is invalid") from exc
    if path.is_symlink() or not resolved.is_dir():
        raise RunnerError("dependency environment is invalid")
    # Inserting the directory directly intentionally does not process .pth
    # files.  Import hooks in a package remain trusted-code behavior, but an
    # environment cannot extend sys.path merely by existing.
    sys.path.insert(0, str(resolved))


_WINDOWS_JOB_HANDLE = None


def _set_windows_limits(limits: dict) -> bool:
    global _WINDOWS_JOB_HANDLE
    if _WINDOWS_JOB_HANDLE is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(f"value_{index}", ctypes.c_ulonglong) for index in range(6)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Io), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateJobObjectW(None, None)
        if not handle: return False
        info = Extended()
        info.basic.process_time = max(1, int(limits.get("cpu_seconds", 900))) * 10_000_000
        info.basic.active = max(1, int(limits.get("processes", 8)))
        info.basic.flags = 0x00000002 | 0x00000008 | 0x00000200 | 0x00002000
        info.job_memory = max(256 * 1024 * 1024, int(limits.get("address_space", 4 * 1024 * 1024 * 1024)))
        if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or
                not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
            kernel.CloseHandle(handle); return False
        _WINDOWS_JOB_HANDLE = handle
        return True
    except Exception:
        return False


def _set_limits(manifest: dict) -> None:
    """Apply best-effort OS limits before importing user code."""
    limits = manifest.get("limits") if isinstance(manifest.get("limits"), dict) else {}
    if os.name == "nt":
        if not _set_windows_limits(limits):
            raise RunnerError("Windows process limits could not be established")
        return
    if os.name != "posix":
        return
    try:
        import resource
    except ImportError:
        return
    for name, resource_name in (("cpu_seconds", "RLIMIT_CPU"), ("address_space", "RLIMIT_AS"), ("file_size", "RLIMIT_FSIZE"), ("open_files", "RLIMIT_NOFILE")):
        value = limits.get(name)
        if not isinstance(value, int) or value <= 0 or not hasattr(resource, resource_name):
            continue
        try:
            kind = getattr(resource, resource_name)
            _soft, hard = resource.getrlimit(kind)
            bounded = min(value, hard) if hard != resource.RLIM_INFINITY else value
            resource.setrlimit(kind, (bounded, bounded))
        except (OSError, ValueError):
            # Resource support differs between POSIX platforms. The parent
            # still owns wall-clock and process-tree termination.
            continue


def run(manifest_path: str | Path) -> int:
    manifest = _load_manifest(Path(manifest_path))
    _set_limits(manifest)
    _configure_environment(manifest)
    callback, source_identity = _load_processor(manifest)
    entries = manifest["entries"]
    if not (0 < len(entries) <= 1024):
        raise RunnerError("manifest image count is invalid")
    total = len(entries)
    raw_run_root = Path(manifest["run_root"])
    if (not raw_run_root.is_absolute() or len(str(raw_run_root).encode("utf-8")) > 4096):
        raise RunnerError("run root is invalid")
    run_root = raw_run_root.resolve()
    if not run_root.is_dir() or run_root != raw_run_root:
        raise RunnerError("run root is invalid")
    if Path(manifest_path).resolve() != run_root / "manifest.json":
        raise RunnerError("manifest is outside the private run")
    entry_keys = {"role", "relative_path", "name", "staged", "index", "total"}
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            raise RunnerError("manifest entry is invalid")
        role = entry.get("role"); name = entry.get("name"); relative = entry.get("relative_path")
        if (role not in {"front", "double_sided"} or not isinstance(name, str) or
                not name or name in {".", ".."} or "/" in name or "\\" in name or
                any(unicodedata.category(character).startswith("C") for character in name) or
                len(name.encode("utf-8")) > 255 or not isinstance(relative, str) or
                relative != f"game/{role}/{name}" or entry.get("index") != index or
                entry.get("total") != total or not isinstance(entry.get("staged"), str)):
            raise RunnerError("manifest entry is invalid")
        image = Path(entry["staged"])
        expected_image = run_root / "work" / role / name
        if not image.is_absolute() or image != expected_image:
            raise RunnerError("staged image escapes run root")
        try:
            observed = os.lstat(image)
        except OSError as exc:
            raise RunnerError("staged image is not a regular file") from exc
        if (stat.S_ISLNK(observed.st_mode) or getattr(observed, "st_reparse_tag", 0) or
                not stat.S_ISREG(observed.st_mode) or getattr(observed, "st_nlink", 1) != 1):
            raise RunnerError("staged image is not a private regular file")
        context = {
            "role": role, "relative_path": relative,
            "name": name, "index": index, "total": total,
        }
        result = callback(image, context)
        if result is not None:
            raise RunnerError("process_image must return None")
        _emit(PROGRESS_PREFIX, {"index": index, "total": total, "name": _bounded_text(entry.get("name", "")), "role": _bounded_text(entry.get("role", ""))})
    if source_identity is not None:
        path = Path(str(manifest["source_path"]))
        _raw, now_identity = _read_regular(path, "processor source", 256 * 1024)
        if now_identity != source_identity:
            raise RunnerError("processor source changed during execution")
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
        # Keep frame locations useful without copying processor source lines
        # into persisted job history.
        frames = []
        current = exc.__traceback__
        while current is not None:
            code = current.tb_frame.f_code
            frames.append(f"  at {Path(code.co_filename).name}:{current.tb_lineno} in {code.co_name}")
            current = current.tb_next
        if frames:
            print("Processor traceback (source text omitted):", file=sys.stderr)
            print("\n".join(frames[-8:]), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
