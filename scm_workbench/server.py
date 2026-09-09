#!/usr/bin/env python3
"""
SCM Workbench — a local web UI for silhouette-card-maker and scm-extras.

Zero dependencies beyond the Python standard library. Run:

    python -m scm_workbench.server             # starts on http://127.0.0.1:8037
    python -m scm_workbench.server --port 9000  # different port
    python -m scm_workbench.server --no-browser  # don't auto-open a browser tab
    python -m scm_workbench.server --ipc --no-browser  # supervised native worker, no HTTP

The UI shells out to the Python scripts in the two sister repos (auto-detected
next to this folder, overridable in Settings) and streams their output live in
a job console. Every exposed CLI option is driven from a single manifest, so
the on-screen command preview always matches the command actually run.

Binds to 127.0.0.1 only — this is local tooling, not a network service.
Requires Python 3.10+ (3.12+ recommended to match silhouette-card-maker).
"""

import argparse
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import queue
import secrets
import tempfile
import threading
import time
import uuid
import webbrowser
import unicodedata
from collections import OrderedDict
import copy
import errno
import html
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse, urlsplit
from typing import Any, Dict, List, Optional, Tuple

# `python -m scm_workbench.server` executes this file as `__main__`. Register
# that live module under its package name before importing the IPC adapter, so
# the adapter cannot create a second server module with independent jobs,
# settings caches, readiness state, or shutdown hooks.
if __name__ == "__main__":
    sys.modules["scm_workbench.server"] = sys.modules[__name__]

from scm_workbench import repo_sync, updater

# The one version constant the whole app reports (About-card line, banner,
# and the updater's notion of "what am I running"). It is pinned per build
# by scripts/inject_version.py from the release tag, so a build from v0.1.1
# says 0.1.1 everywhere and can never offer to install itself.
from scm_workbench._version import __version__

SERVER_VERSION = __version__
DEFAULT_PORT = 8037
# True only while this process owns the native child JSON-lines transport.
# stdout is reserved for protocol frames in that mode.
_IPC_MODE = False
_IPC_PROCESS_GROUP_READY = False

# Native job responses and SSE frames share these conservative wire limits.
# Log files remain complete on disk; only transmitted lines are clipped.
JOB_LINE_MAX_BYTES = 64 * 1024
JOB_LOG_MAX_LINES = 4096
SSE_QUEUE_SIZE = 128


class _WakeQueue(queue.Queue):
    """Queue compatible with legacy producers but never blocks their pump."""
    def put(self, item, block=True, timeout=None):  # noqa: D401
        return super().put(item, block=False)

    def put_nowait(self, item):
        return super().put(item, block=False)


IPC_POLL_MAX_BYTES = 6 * 1024 * 1024  # safely below the 8 MiB frame ceiling


def _diag(message: str = "", *, error: bool = False) -> None:
    """Write human diagnostics without contaminating IPC stdout."""
    print(message, file=sys.stderr if (_IPC_MODE or error) else sys.stdout)

# The package lives one level down from the repo root in a dev checkout, and
# next to a `ui/` folder inside an app bundle; accept either layout.
_HERE = Path(__file__).resolve().parent

# When the app runs from a bundle (packaged with Briefcase/py2app), the
# launcher points SCM_WORKBENCH_DATA at a writable per-user area, so settings,
# job history, logs, and the managed repo copies survive app updates. In a dev
# checkout (env var unset) everything stays at the repo root, as before.
_env_data = os.environ.get("SCM_WORKBENCH_DATA")
DATA_DIR = Path(_env_data).expanduser().resolve() if _env_data else _HERE.parent / "data"
WB_ROOT = _HERE.parent

UI_DIR = next((c for c in (_HERE / "ui", _HERE.parent / "ui") if (c / "index.html").is_file()),
              _HERE.parent / "ui")
SETTINGS_FILE = DATA_DIR / "settings.json"
JOBS_FILE = DATA_DIR / "jobs.json"
LOGS_DIR = DATA_DIR / "logs"
PER_SIZE_OFFSETS_FILE = DATA_DIR / "offsets_by_size.json"
UPDATE_STATE_FILE = DATA_DIR / "update-state.json"
UPDATE_CHECK_INTERVAL = 86400          # re-check for a newer release at most once a day

# ============================================================================
# Repo detection & plain-JSON readers (no imports from the base repos)
# ============================================================================

def _try_read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _sibling(name: str, marker: str) -> Optional[Path]:
    base = Path(__file__).resolve().parent
    # walk up: the package dir, the repo root, and the folder holding the repo
    # (dev checkouts keep the sister repos side by side with the Workbench root)
    for _ in range(3):
        p = base / name
        if (p / marker).is_file():
            return p
        base = base.parent
    return None


def find_scm_repo() -> Optional[Path]:
    return _sibling("silhouette-card-maker", "create_pdf.py")


def find_extras_repo() -> Optional[Path]:
    return _sibling("scm-extras", "generate.py")


# Decklists cross a trust boundary: the native dialog supplies only a path,
# while the worker owns the copy into the managed repository.  Keep these
# limits independent of the JSON-lines frame limits.
DECKLIST_SOURCE_MAX_BYTES = 8 * 1024 * 1024
DECKLIST_PATH_MAX_BYTES = 4096
DECKLIST_NAME_MAX_BYTES = 255
DECKLIST_SCAN_MAX_SCANNED = 8192
DECKLIST_SCAN_MAX_ITEMS = 1024
DECKLIST_SCAN_MAX_RESULT_BYTES = 512 * 1024
DECKLIST_IO_CHUNK = 64 * 1024
DECKLIST_PLACEHOLDERS = frozenset(("README.md", "EMPTY.md"))
DECKLIST_TEMP_PREFIX = ".wb-decklist-import-"

# Artifact export is a deliberately separate trust boundary from the legacy
# browser compatibility route.  Grants contain no paths in the WebView; they
# are short-lived handles to immutable records owned by this worker.
ARTIFACT_MAX_BYTES = 4 * 1024 * 1024 * 1024
ARTIFACT_NAME_MAX_BYTES = 255
ARTIFACT_PATH_MAX_BYTES = 4096
ARTIFACT_IO_CHUNK = 64 * 1024
ARTIFACT_GRANT_TTL = 300.0
ARTIFACT_GRANT_MAX = 32
ARTIFACT_EXPORT_MAX_ACTIVE = 8
ARTIFACT_EXPORT_MAX_RETAINED = 32
ARTIFACT_EXPORT_TTL = 600.0
ARTIFACT_EXPORT_PREFIX = ".wb-artifact-export-"


class ArtifactExportError(Exception):
    def __init__(self, message: str):
        super().__init__(" ".join(str(message).split())[:256])
        self.message = str(self) or "artifact export failed"


class DecklistImportError(Exception):
    """Bounded application failure for a decklist import or scan."""

    def __init__(self, message: str):
        super().__init__(" ".join(str(message).split())[:256])
        self.message = str(self) or "decklist operation failed"


def _utf8_size(value: str, label: str, limit: int) -> int:
    if not isinstance(value, str) or not value:
        raise DecklistImportError(f"{label} must be a non-empty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DecklistImportError(f"{label} must be valid UTF-8") from exc
    if size > limit:
        raise DecklistImportError(f"{label} exceeds {limit} UTF-8 bytes")
    return size


def _decklist_name(name: str) -> str:
    _utf8_size(name, "decklist name", DECKLIST_NAME_MAX_BYTES)
    if name in (".", "..") or not name.strip():
        raise DecklistImportError("invalid decklist name")
    if any(ord(c) < 0x20 or ord(c) == 0x7f or unicodedata.category(c) == "Cc"
           for c in name):
        raise DecklistImportError("decklist name contains control characters")
    if any(c in name for c in ("/", "\\", ":")):
        raise DecklistImportError("decklist name contains a separator or ADS character")
    if name.endswith((".", " ")):
        raise DecklistImportError("decklist name must not end with dot or space")
    stem = name.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]", stem) or re.fullmatch(r"LPT[1-9]", stem):
        raise DecklistImportError("decklist name is reserved on Windows")
    if name in DECKLIST_PLACEHOLDERS or name.startswith(DECKLIST_TEMP_PREFIX):
        raise DecklistImportError("reserved decklist name")
    return name


def _is_reparse_or_symlink(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & reparse)


def _decklist_components(path: Path, *, create: bool = False) -> Path:
    """Check every destination component without resolving symlinks."""
    path = Path(os.path.abspath(os.fspath(path)))
    # The effective SCM path is the trust anchor. Check it and the two fixed
    # children, rather than platform ancestors such as macOS's /var alias.
    base = path.parent.parent
    components = (base, path.parent, path)
    for current in components:
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise DecklistImportError("decklist directory does not exist")
            try:
                current.mkdir()
            except FileExistsError:
                pass
            st = os.lstat(current)
        except OSError as exc:
            raise DecklistImportError("could not inspect decklist directory") from exc
        if _is_reparse_or_symlink(st) or not stat.S_ISDIR(st.st_mode):
            raise DecklistImportError("decklist destination contains a symlink or non-directory")
    return path


def _open_posix_decklist_directory(scm: Path, *, create: bool = True) -> Tuple[Path, int]:
    """Open SCM/game/decklist without following a path component."""
    if os.name == "nt" or not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise DecklistImportError("secure decklist directory handles are unavailable")
    root = Path(os.path.abspath(os.fspath(scm)))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    opened: List[int] = []
    try:
        current = os.open(root, flags)
        opened.append(current)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise DecklistImportError("decklist destination is not a directory")
        for component in ("game", "decklist"):
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise DecklistImportError("could not create decklist directory") from exc
            try:
                child = os.open(component, flags, dir_fd=current)
            except OSError as exc:
                raise DecklistImportError(
                    "decklist destination contains a symlink or non-directory") from exc
            opened.append(child)
            current = child
        final_fd = opened.pop()
        return root / "game" / "decklist", final_fd
    except DecklistImportError:
        raise
    except OSError as exc:
        raise DecklistImportError("could not open decklist directory") from exc
    finally:
        for opened_fd in opened:
            try:
                os.close(opened_fd)
            except OSError:
                pass


def _decklist_scan(directory: Path, directory_fd: Optional[int] = None) -> dict:
    """Bounded deterministic listing shared by info, manifest, and import."""
    try:
        directory_stat = os.fstat(directory_fd) if directory_fd is not None else os.lstat(directory)
    except OSError as exc:
        raise DecklistImportError("could not inspect decklist directory") from exc
    if _is_reparse_or_symlink(directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
        raise DecklistImportError("decklist directory is not a regular directory")
    candidates: List[dict] = []
    scanned = found = 0
    truncated = scan_limited = False
    try:
        iterator = os.scandir(directory_fd if directory_fd is not None else directory)
        with iterator:
            for entry in iterator:
                if scanned >= DECKLIST_SCAN_MAX_SCANNED:
                    truncated = scan_limited = True
                    break
                scanned += 1
                name = entry.name
                if name in DECKLIST_PLACEHOLDERS or name.startswith(DECKLIST_TEMP_PREFIX):
                    continue
                try:
                    encoded_name = name.encode("utf-8")
                except UnicodeEncodeError:
                    truncated = True
                    continue
                if (not encoded_name or len(encoded_name) > DECKLIST_NAME_MAX_BYTES or
                        any(ord(c) < 0x20 or ord(c) == 0x7f for c in name)):
                    truncated = True
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    truncated = True
                    continue
                if _is_reparse_or_symlink(st) or not stat.S_ISREG(st.st_mode):
                    continue
                found += 1
                candidates.append({"name": name, "size": int(st.st_size)})
    except OSError as exc:
        raise DecklistImportError("could not scan decklist directory") from exc

    # Once enumeration itself hits the cap, returning an OS-order-dependent
    # subset would make the result nondeterministic. Fail closed with an empty
    # partial list; the caller still gets explicit truncation and counts.
    candidates = [] if scan_limited else sorted(candidates, key=lambda item: item["name"])
    if len(candidates) > DECKLIST_SCAN_MAX_ITEMS:
        truncated = True
    items = candidates[:DECKLIST_SCAN_MAX_ITEMS]
    # Bound the complete scan object, not just its item array; import adds a
    # small envelope around this same listing before it crosses IPC.
    while items and len(json.dumps(
            {"items": items, "scanned": scanned, "found": found, "truncated": truncated},
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > DECKLIST_SCAN_MAX_RESULT_BYTES:
        items.pop()
        truncated = True
    return {"items": items, "scanned": scanned, "found": found, "truncated": bool(truncated)}


def _decklist_lock():
    """Acquire the same per-SCM lock as repo_sync, without an unbounded wait."""
    import contextlib
    @contextlib.contextmanager
    def locked():
        lock_path = repo_sync.data_dir() / ".repos-scm-lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        acquired = False
        deadline = time.monotonic() + 7.5
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0, 2)
                if fh.tell() == 0:
                    fh.write(" ")
                    fh.flush()
                contention_errno = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                contention_winerror = {32, 33, 36, 170, 212}
                while time.monotonic() < deadline:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError as exc:
                        if (getattr(exc, "errno", None) not in contention_errno and
                                getattr(exc, "winerror", None) not in contention_winerror):
                            raise DecklistImportError("could not acquire repository lock") from exc
                        time.sleep(0.05)
            else:
                import fcntl
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
            if not acquired:
                raise DecklistImportError("repository is busy")
            yield
        finally:
            if acquired:
                try:
                    if os.name == "nt":
                        fh.seek(0)
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh, fcntl.LOCK_UN)
                finally:
                    fh.close()
            else:
                fh.close()
    return locked()


def _open_decklist_source(source_path: str):
    _utf8_size(source_path, "source path", DECKLIST_PATH_MAX_BYTES)
    if "\x00" in source_path or any(ord(c) < 0x20 or ord(c) == 0x7f for c in source_path):
        raise DecklistImportError("source path contains control characters")
    name = os.path.basename(source_path)
    if not name or name != source_path.rstrip("/\\").split("/")[-1].split("\\")[-1]:
        # The basename is still used for the destination, but an empty leaf is
        # never a valid selected file.
        raise DecklistImportError("source path has no file name")
    name = _decklist_name(name)
    if os.name == "nt":
        # Open the final object itself and inspect reparse attributes before
        # converting the stable Win32 handle to a CRT fd for bounded reads.
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(source_path, 0x80000000, 0x00000007, None, 3,
                                      0x00200000, None)  # GENERIC_READ, OPEN_EXISTING, OPEN_REPARSE_POINT
        raw_handle = getattr(handle, "value", handle)
        invalid = ctypes.c_void_p(-1).value
        if raw_handle == invalid:
            raise DecklistImportError("could not open selected decklist")
        handle_info = _ByHandleFileInformation()
        if (not kernel32.GetFileInformationByHandle(handle, ctypes.byref(handle_info))
                or handle_info.dwFileAttributes & 0x400):
            kernel32.CloseHandle(handle)
            raise DecklistImportError("selected source is a reparse point or unavailable")
        try:
            import msvcrt
            fd = msvcrt.open_osfhandle(
                raw_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except Exception as exc:
            kernel32.CloseHandle(handle)
            raise DecklistImportError("could not open selected decklist") from exc
    else:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(source_path, flags)
        except OSError as exc:
            raise DecklistImportError("could not open selected decklist") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise DecklistImportError("could not open selected decklist") from exc
    if _is_reparse_or_symlink(st) or not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise DecklistImportError("selected source is not a regular file")
    if st.st_size > DECKLIST_SOURCE_MAX_BYTES:
        os.close(fd)
        raise DecklistImportError("selected decklist exceeds 8 MiB")
    return fd, st, name


def import_decklist(source_path: str) -> dict:
    """Copy one selected file through stable source/destination handles."""
    fd, source_stat, name = _open_decklist_source(source_path)
    directory_fd = None
    temp_name = None
    temp_path = None
    try:
        scm, _ = effective_dirs(load_settings())
        if not scm:
            raise DecklistImportError("no copy of silhouette-card-maker is connected yet")
        with _decklist_lock():
            if os.name == "nt":
                directory = _decklist_components(Path(scm) / "game" / "decklist", create=True)
            else:
                directory, directory_fd = _open_posix_decklist_directory(Path(scm))

            def destination_exists(candidate_name: str) -> bool:
                try:
                    if directory_fd is None:
                        os.lstat(directory / candidate_name)
                    else:
                        os.stat(candidate_name, dir_fd=directory_fd, follow_symlinks=False)
                    return True
                except FileNotFoundError:
                    return False

            target_name = name
            stem, extension = os.path.splitext(name)
            for suffix in range(1000):
                candidate_name = target_name if suffix == 0 else f"{stem} ({suffix + 1}){extension}"
                _utf8_size(candidate_name, "decklist name", DECKLIST_NAME_MAX_BYTES)
                if not destination_exists(candidate_name):
                    target_name = candidate_name
                    break
            else:
                raise DecklistImportError("too many decklist name collisions")

            # O_EXCL makes the temporary file private to this operation. It is
            # never removed unless this operation created it.
            for _ in range(16):
                candidate_name = f"{DECKLIST_TEMP_PREFIX}{uuid.uuid4().hex}.tmp"
                candidate_path = directory / candidate_name
                try:
                    if directory_fd is None:
                        temp_fd = os.open(
                            candidate_path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                            getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0),
                            0o600,
                        )
                        temp_path = candidate_path
                    else:
                        temp_fd = os.open(
                            candidate_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                            getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0),
                            0o600,
                            dir_fd=directory_fd,
                        )
                    temp_name = candidate_name
                    break
                except FileExistsError:
                    continue
            else:
                raise DecklistImportError("could not create temporary decklist")

            total = 0
            try:
                while total < source_stat.st_size:
                    chunk = os.read(fd, min(DECKLIST_IO_CHUNK, source_stat.st_size - total))
                    if not chunk:
                        raise DecklistImportError("selected source changed while reading")
                    written = 0
                    while written < len(chunk):
                        count = os.write(temp_fd, chunk[written:])
                        if count <= 0:
                            raise DecklistImportError("could not write temporary decklist")
                        written += count
                    total += len(chunk)
                    if total > DECKLIST_SOURCE_MAX_BYTES:
                        raise DecklistImportError("selected decklist exceeds 8 MiB")
                if os.read(fd, 1):
                    raise DecklistImportError("selected source grew while reading")
                end_stat = os.fstat(fd)

                def source_identity(value):
                    return (
                        value.st_dev,
                        value.st_ino,
                        value.st_size,
                        getattr(value, "st_mtime_ns", value.st_mtime),
                        getattr(value, "st_ctime_ns", value.st_ctime),
                    )

                if source_identity(end_stat) != source_identity(source_stat):
                    raise DecklistImportError("selected source changed while reading")
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)

            def publish(candidate_name: str) -> None:
                if directory_fd is None:
                    os.link(temp_path, directory / candidate_name)
                else:
                    os.link(
                        temp_name,
                        candidate_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )

            try:
                publish(target_name)
            except FileExistsError:
                # The repository lock excludes Workbench writers, but an
                # external writer can still win. No-replace publication then
                # selects a fresh bounded suffix without overwriting it.
                for suffix in range(1, 1000):
                    candidate_name = f"{stem} ({suffix + 1}){extension}"
                    _utf8_size(candidate_name, "decklist name", DECKLIST_NAME_MAX_BYTES)
                    try:
                        publish(candidate_name)
                        target_name = candidate_name
                        break
                    except FileExistsError:
                        continue
                else:
                    raise DecklistImportError("too many decklist name collisions")

            if directory_fd is None:
                os.unlink(temp_path)
            else:
                os.unlink(temp_name, dir_fd=directory_fd)
            temp_name = temp_path = None
            try:
                if directory_fd is not None:
                    os.fsync(directory_fd)
                else:
                    flush_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(flush_fd)
                    finally:
                        os.close(flush_fd)
            except OSError:
                pass
            listing = _decklist_scan(directory, directory_fd)

        result = {"ok": True, "name": target_name, "decklists": listing["items"],
                  "truncated": listing["truncated"]}
        while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > DECKLIST_SCAN_MAX_RESULT_BYTES and result["decklists"]:
            result["decklists"].pop()
            result["truncated"] = True
        invalidate_manifest_cache()
        return result
    except DecklistImportError:
        raise
    except (OSError, ValueError) as exc:
        raise DecklistImportError("decklist import failed") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if temp_name is not None:
            try:
                if directory_fd is not None:
                    os.unlink(temp_name, dir_fd=directory_fd)
                elif temp_path is not None:
                    os.unlink(temp_path)
            except OSError:
                pass
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def read_scm_info(scm: Optional[Path], extras: Optional[Path]) -> dict:
    """Display info for the SCM repo, read from plain JSON files only."""
    info = {
        "found": bool(scm and scm.is_dir()),
        "path": str(scm) if scm else None,
        "version": None,
        "ppi": 300,
        "card_radius": "3mm",
        "card_sizes": [],
        "paper_sizes": [],
        "layouts": {},
        "specialty": [],
        "templates": {"dxf": [], "borderless_dxf": [], "studio3": [], "borderless_studio3": []},
        "calibration": [],
        "saved_offset": None,
        "decklists": [],
        "output_pdfs": [],
    }
    if not info["found"]:
        return info

    try:
        m = re.search(r'version\s*=\s*"([^"]+)"', (scm / "pyproject.toml").read_text(encoding="utf-8"))
        if m:
            info["version"] = m.group(1)
    except Exception:
        pass

    layouts = _try_read_json(scm / "assets" / "layouts.json") or {}
    info["ppi"] = layouts.get("ppi", 300)
    info["card_radius"] = layouts.get("defaults", {}).get("card_radius", "3mm")

    extra: dict = {}
    if extras:
        extra = _try_read_json(extras / "assets" / "layouts_extra.json") or {}

    for name, d in (layouts.get("card_sizes") or {}).items():
        info["card_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "radius": d.get("radius"), "aliases": d.get("aliases") or [], "source": "core",
        })
    for name, d in (extra.get("card_sizes") or {}).items():
        info["card_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "radius": d.get("radius"), "aliases": d.get("aliases") or [], "source": "extras",
        })
    for name, d in (layouts.get("paper_sizes") or {}).items():
        info["paper_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "aliases": d.get("aliases") or [], "source": "core",
        })
    for name, d in (extra.get("paper_sizes") or {}).items():
        info["paper_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "aliases": d.get("aliases") or [], "source": "extras",
        })

    merged: Dict[str, Any] = {}
    for paper, cards in (layouts.get("layouts") or {}).items():
        for card, variants in cards.items():
            for variant, defn in variants.items():
                merged.setdefault(paper, {}).setdefault(card, {})[variant] = defn
    for paper, cards in (extra.get("layouts") or {}).items():
        for card, variants in cards.items():
            for variant, defn in variants.items():
                merged.setdefault(paper, {}).setdefault(card, {})[variant] = defn
    info["layouts"] = merged

    for name, d in (layouts.get("specialty_layouts") or {}).items():
        cs = d.get("card_size") or {}
        info["specialty"].append({
            "name": name,
            "paper": (d.get("paper_size") or {}).get("name"),
            "width": cs.get("width"), "height": cs.get("height"),
            "rows": d.get("num_rows"), "cols": d.get("num_cols"),
        })

    ct = scm / "cutting_templates"
    info["templates"] = {
        "dxf": _sorted_dir(ct / "dxf"),
        "borderless_dxf": _sorted_dir(ct / "borderless" / "dxf"),
        "studio3": _glob_dir(ct, "*.studio3"),
        "borderless_studio3": _glob_dir(ct / "borderless", "*.studio3"),
    }

    cal = scm / "calibration"
    if cal.is_dir():
        info["calibration"] = [
            {"name": p.stem.replace("-calibration", ""), "path": str(p), "size": p.stat().st_size}
            for p in sorted(cal.glob("*.pdf"))
        ]

    offset = None
    try:
        _, data_dir, path_error = _safe_offset_paths(scm)
        if not path_error:
            raw_offset = _try_read_json(data_dir / "offset_data.json")
            if isinstance(raw_offset, dict):
                offset = _offset_row({"x": raw_offset.get("x_offset"),
                                      "y": raw_offset.get("y_offset"),
                                      "angle": raw_offset.get("angle_offset", 0.0)})
    except Exception:
        offset = None
    if offset:
        info["saved_offset"] = offset
    # A canonical state, when present, is authoritative for the baseline;
    # never let a staged upstream projection masquerade as that baseline.
    try:
        state_path = _offset_state_path()
        if state_path.exists() or state_path.is_symlink():
            try:
                info["saved_offset"] = _load_offset_state_strict().get("global")
            except OffsetStateError:
                info["saved_offset"] = None
    except Exception:
        pass

    dl = scm / "game" / "decklist"
    scan_fd = None
    try:
        if os.name == "nt":
            if dl.exists() and not dl.is_symlink():
                _decklist_components(dl, create=False)
                info["decklists"] = _decklist_scan(dl)["items"]
        else:
            dl, scan_fd = _open_posix_decklist_directory(scm, create=False)
            info["decklists"] = _decklist_scan(dl, scan_fd)["items"]
    except (DecklistImportError, OSError):
        info["decklists"] = []
    finally:
        if scan_fd is not None:
            try:
                os.close(scan_fd)
            except OSError:
                pass
    outdir = scm / "game" / "output"
    if outdir.is_dir():
        info["output_pdfs"] = [p.name for p in sorted(outdir.glob("*.pdf"))]
    return info


def _sorted_dir(d: Path) -> List[str]:
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())


def _glob_dir(d: Path, pattern: str) -> List[str]:
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob(pattern))


def read_extras_info(extras: Optional[Path]) -> dict:
    info = {
        "found": bool(extras and extras.is_dir()),
        "path": str(extras) if extras else None,
        "card_sizes": [],
        "layouts": {},
        "templates": {"dxf": [], "borderless_dxf": [], "studio3": [], "borderless_studio3": []},
    }
    if not info["found"]:
        return info
    extra = _try_read_json(extras / "assets" / "layouts_extra.json") or {}
    for name, d in (extra.get("card_sizes") or {}).items():
        info["card_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "radius": d.get("radius"), "aliases": d.get("aliases") or [],
        })
    info["layouts"] = extra.get("layouts", {})
    ct = extras / "cutting_templates"
    info["templates"] = {
        "dxf": _sorted_dir(ct / "dxf"),
        "borderless_dxf": _sorted_dir(ct / "borderless" / "dxf"),
        "studio3": _glob_dir(ct, "*.studio3"),
        "borderless_studio3": _glob_dir(ct / "borderless", "*.studio3"),
    }
    return info


# ============================================================================
# Game plugins (mirrors silhouette-card-maker/plugins/*/fetch.py)
# ============================================================================

PLUGINS: Dict[str, dict] = {
    "mtg": {
        "title": "Magic: The Gathering",
        "formats": [
            ("archidekt", "Archidekt"), ("cubecobra_csv", "CubeCobra CSV"),
            ("deckstats", "Deckstats"), ("moxfield", "Moxfield"),
            ("mpcfill_xml", "MPCFill XML"), ("mtga", "MTG Arena"),
            ("mtgo", "MTGO"), ("scryfall_json", "Scryfall JSON"),
            ("simple", "Simple (name list)"), ("url", "URL"),
        ],
    },
    "yugioh": {"title": "Yu-Gi-Oh!", "formats": [("ydke", "YDKE"), ("ydk", "YDK")]},
    "pokemon": {"title": "Pokémon", "formats": [("limitless", "Limitless TCG")]},
    "altered": {"title": "Altered", "formats": [("ajordat", "Ajordat")]},
    "arkham_horror_lcg": {
        "title": "Arkham Horror LCG",
        "formats": [("arkhamdb_json", "ArkhamDB JSON"), ("arkhamdb_url", "ArkhamDB URL")],
    },
    "ashes_reborn": {
        "title": "Ashes: Reborn",
        "formats": [("ashes_share_url", "Ashes share URL"), ("ashesdb_share_url", "AshesDB share URL")],
    },
    "bushiroad": {"title": "Bushiroad", "formats": [("bushiroad_url", "Bushiroad URL")]},
    "digimon": {
        "title": "Digimon Card Game",
        "formats": [
            ("digimoncardapp", "Digimon Card App"), ("digimoncarddev", "Digimon Card Dev"),
            ("digimoncardio", "DigimonCard.io"), ("digimonmeta", "Digimon Meta"),
            ("tts", "Tabletop Simulator"), ("untap", "Untap"),
        ],
    },
    "echoes_of_astra": {"title": "Echoes of Astra", "formats": [("astrabuilder_url", "Astra Builder URL")]},
    "elestrals": {"title": "Elestrals", "formats": [("elestrals", "Elestrals")]},
    "final_fantasy": {
        "title": "Final Fantasy TCG",
        "formats": [("octgn_xml", "OctGN XML"), ("tts", "Tabletop Simulator"), ("untap", "Untap")],
    },
    "flesh_and_blood": {"title": "Flesh & Blood", "formats": [("fabrary", "Fabrary")]},
    "grand_archive": {"title": "Grand Archive", "formats": [("omnideck", "OmniDeck")]},
    "gundam": {
        "title": "Gundam Card Game",
        "formats": [
            ("deckplanet", "Deckplanet"), ("egman", "EGM Events"),
            ("exburst", "Exburst"), ("limitless", "Limitless TCG"),
        ],
    },
    "keyforge": {
        "title": "KeyForge",
        "formats": [
            ("archon_arcana", "Archon Arcana"), ("master_vault_url", "Master Vault URL"),
            ("decks_of_keyforge_url", "Decks of Keyforge URL"),
        ],
    },
    "lorcana": {"title": "Disney Lorcana", "formats": [("dreamborn", "Dreamborn")]},
    "lotr_lcg": {
        "title": "Lord of the Rings LCG",
        "formats": [
            ("ringsdb_url", "RingsDB URL"), ("ringsdb_fellowship_url", "RingsDB Fellowship URL"),
            ("ringsdb_scenario_url", "RingsDB Scenario URL"), ("hallofbeorn_url", "Hall of Beorn URL"),
        ],
    },
    "netrunner": {
        "title": "Netrunner",
        "formats": [
            ("bbcode", "BBCode"), ("jinteki", "Jinteki"), ("markdown", "Markdown"),
            ("plain_text", "Plain text"), ("text", "Text"),
        ],
    },
    "one_piece": {
        "title": "One Piece Card Game",
        "formats": [("egman", "EGM Events"), ("optcgsim", "OPTCG Simulator")],
    },
    "riftbound": {
        "title": "Riftbound",
        "formats": [
            ("piltover_archive", "Piltover Archive"), ("pixelborn", "Pixelborn"),
            ("tts", "Tabletop Simulator"),
        ],
    },
    "sorcery_contested_realm": {
        "title": "Sorcery: Contested Realm",
        "formats": [("curiosa_url", "Curiosa URL")],
    },
    "star_wars_unlimited": {
        "title": "Star Wars Unlimited",
        "formats": [("melee", "Melee"), ("picklist", "Picklist"), ("swudb_json", "SWUDB JSON")],
    },
}

MTG_LANGS = ["en", "sp", "fr", "de", "it", "pt", "jp", "kr", "ru", "cs", "ct", "ag", "ph"]

# ============================================================================
# Job manifest — single source of truth for every exposed option.
# Served to the browser (forms are rendered from it) and consumed by the
# argv builders below, so the command preview always matches the command run.
# ============================================================================

def _opt(key, label, type_, **kw) -> dict:
    d = {"key": key, "label": label, "type": type_}
    d.update(kw)
    return d


def build_manifest(info: dict) -> dict:
    """Assemble the job manifest. `info` = output of get_info()."""
    scm, extras = info["scm"], info["extras"]

    card_choices = [["", "— pick a card size —"]]
    for c in scm["card_sizes"]:
        tag = "   ✦ extras" if c["source"] == "extras" else ""
        card_choices.append([c["name"], f"{c['name']} — {c.get('width') or '?'} × {c.get('height') or '?'}{tag}"])
    paper_choices = [["", "— pick a paper size —"]]
    for p in scm["paper_sizes"]:
        paper_choices.append([p["name"], f"{p['name']} — {p.get('width') or '?'} × {p.get('height') or '?'}"])
    specialty_choices = [["", "None"]] + [
        [s["name"], f"{s['name']} ({s['paper']})"] for s in scm["specialty"]
    ]

    def fetch_groups(slug: str) -> List[dict]:
        groups = [
            {
                "title": "Decklist",
                # Keep the source selector above its conditional picker/editor
                # when the fetch form uses the flat simple-mode layout.
                "simple_rows": [
                    ["deck_source"],
                    ["deck_file", "deck_name", "deck_text", "deck_url"],
                ],
                "options": [
                    _opt("deck_source", "Source", "segment",
                         choices=[["file", "Existing file"], ["paste", "Paste text"], ["url", "URL"]], default="file"),
                    _opt("deck_file", "Decklist file", "select",
                         choices=[["", "— pick a file —"]] + [[d["name"], d["name"]] for d in scm["decklists"]],
                         default=""),
                    _opt("deck_name", "Save pasted decklist as", "text", placeholder="my_deck.txt", default=""),
                    _opt("deck_text", "Decklist text", "textarea", default=""),
                    _opt("deck_url", "URL", "text", placeholder="https://archidekt.io/deck/…", default="",
                         help="For URL-based formats (MTG “url”, any *_url) the decklist is the URL itself."),
                ],
            },
            {
                "title": "Format",
                "options": [
                    _opt("format", "Decklist format", "select",
                         choices=[["", "— pick a format —"]] + [list(f) for f in PLUGINS[slug]["formats"]],
                         default=""),
                ],
            },
        ]
        if slug == "mtg":
            groups.append({
                "title": "MTG card preferences",
                "collapsible": True,
                "options": [
                    _opt("prefer_set", "Prefer sets", "chips", placeholder="e.g. ONE, M25", default=[]),
                    _opt("ignore_set", "Exclude sets", "chips", default=[]),
                    _opt("prefer_lang", "Preferred languages (printed code)", "choice_chips",
                         choices=[[l, l.upper()] for l in MTG_LANGS], default=[]),
                    _opt("prefer_older_sets", "Prefer older sets", "toggle", default=False, width="quarter"),
                    _opt("prefer_showcase", "Prefer showcase art", "toggle", default=False, width="quarter"),
                    _opt("prefer_extra_art", "Prefer full / borderless / extended art", "toggle", default=False, width="quarter"),
                    _opt("prefer_ub", "Prefer Universe Beyond", "toggle", default=False, width="quarter"),
                    _opt("ignore_ub", "Exclude Universe Beyond", "toggle", default=False, width="quarter"),
                    _opt("tokens", "Also fetch related tokens", "toggle", default=False, width="quarter"),
                    _opt("ignore_set_and_collector_number", "Ignore set & collector numbers", "toggle", default=False, width="quarter"),
                ],
            })
        return groups

    kinds: Dict[str, dict] = {}

    # ------------------------------------------------------------------ PDF
    kinds["create_pdf"] = {
        "title": "Create PDF", "page": "pdf", "needs": ["scm"], "cwd": "scm",
        # the simple-mode layout: rows of the flat section — the two
        # dropdowns alone up top, the four toggles together below
        "simple_rows": [
            ["card_size", "paper_size"],
            ["borderless", "load_offset", "only_fronts", "mpcfill_crop"],
        ],
        "description": "Lays out card images into a print-ready PDF with registration marks that match the cutting templates.",
        "groups": [
            {
                "title": "Sources & output",
                "options": [
                    _opt("front_dir", "Front images folder", "path", default="game/front", width="half",
                         help="Directory containing the card front images."),
                    _opt("back_dir", "Card back folder", "path", default="game/back", width="half",
                         help="Directory with one or more card back images."),
                    _opt("double_sided_dir", "Double-sided folder", "path", default="game/double_sided", width="half",
                         help="Cards that have different front and back art."),
                    _opt("output_path", "Output PDF", "path", default="game/output/game.pdf", width="full"),
                    _opt("output_images", "Output images instead of a PDF", "toggle", default=False, width="third"),
                    _opt("only_fronts", "Front pages only", "toggle", default=False, width="third", simple=True),
                ],
            },
            {
                "title": "Card, paper & registration",
                "options": [
                    _opt("card_size", "Card size", "select", choices=card_choices, default="standard", width="third", simple=True),
                    _opt("paper_size", "Paper size", "select", choices=paper_choices, default="letter", width="third", simple=True),
                    _opt("registration", "Registration marks", "segment",
                         choices=[["3", "3 marks"], ["4", "4 marks"]], default="3", width="third"),
                    _opt("specialty", "Specialty layout", "select", choices=specialty_choices, default="", width="third",
                         help="Overrides card size, paper size, and registration."),
                    _opt("registration_orientation", "Registration orientation", "select",
                         choices=[["", "Auto (follow layout)"], ["portrait", "Portrait"], ["landscape", "Landscape"]],
                         default="", width="third"),
                    _opt("borderless", "Borderless (tighter inset)", "toggle", default=False, width="third", simple=True,
                         help="Fits more cards per page by using a smaller inset."),
                ],
            },
            {
                "title": "Quality",
                "options": [
                    _opt("ppi", "Resolution (PPI)", "range", default=1200, min=150, max=1200, step=10, width="third"),
                    _opt("quality", "Compression quality", "range", default=100, min=0, max=100, step=1, width="third"),
                    _opt("load_offset", "Apply saved offset", "toggle", default=False, width="third", simple=True,
                         help="Applies the saved X / Y / angle printer offset — the matching per-paper-size row when one is saved, else the global value."),
                ],
            },
            {
                "title": "Fit & edge finishing",
                "collapsible": True,
                "options": [
                    _opt("fit", "Fit front images", "segment",
                         choices=[["stretch", "Stretch"], ["crop", "Center crop"]], default="stretch", width="third",
                         help="Stretch allows distortion; crop preserves aspect ratio."),
                    _opt("fit_backs", "Fit back images", "segment",
                         choices=[["", "Auto (like fronts)"], ["stretch", "Stretch"], ["crop", "Center crop"]],
                         default="", width="third"),
                    _opt("mpcfill_crop", "MPCFill Crop", "toggle", default=False, width="third", simple=True, simple_only=True,
                         help="Applies a 3mm crop to the front images to fix MPCFill's padding — the art it fetches ships with its own print-bleed margin. A value typed in “Crop edges (fronts)” wins over this toggle."),
                    _opt("crop", "Crop edges (fronts)", "text", placeholder="3mm · 0.125in", width="third"),
                    _opt("crop_backs", "Crop edges (backs)", "text", placeholder="3mm · 0.125in", width="third"),
                    _opt("extend_edges", "Extend edges (fronts)", "text", placeholder="3mm", width="third"),
                    _opt("extend_edges_backs", "Extend edges (backs)", "text", placeholder="3mm", width="third"),
                    _opt("extend_corners", "Extend rounded corners (fronts)", "text", placeholder="3mm", width="third"),
                    _opt("extend_corners_backs", "Extend rounded corners (backs)", "text", placeholder="3mm", width="third"),
                    _opt("extend_bleed", "Extend outer bleed (front pages)", "text", placeholder="3mm", width="third"),
                    _opt("extend_bleed_backs", "Extend outer bleed (back pages)", "text", placeholder="3mm", width="third"),
                ],
            },
            {
                "title": "Advanced",
                "collapsible": True,
                "options": [
                    _opt("skip", "Skip card indexes", "chips", int=True, placeholder="0, 4", width="half",
                         help="0-based indexes of cards to skip (works around a bad registration)."),
                    _opt("label", "Custom page label", "text", width="half"),
                    _opt("show_outline", "Show white cut outline", "toggle", default=False, width="half"),
                ],
            },
        ],
    }

    # --------------------------------------------------------------- Offset
    kinds["offset_pdf"] = {
        "title": "Offset PDF", "page": "offset", "needs": ["scm"], "cwd": "scm",
        "description": "Shifts a printed PDF by an X/Y offset and rotation angle, then re-assembles it. Used to correct printer misalignment.",
        "groups": [
            {
                "title": "Source PDF",
                "options": [
                    _opt("pdf_path", "Input PDF", "select",
                         choices=[["", "— pick a PDF —"]] + [[p, p] for p in scm["output_pdfs"]]
                               + [["game/output/game.pdf", "game/output/game.pdf (default)"]],
                         default="game/output/game.pdf", width="half"),
                    _opt("output_pdf_path", "Output PDF (blank = auto)", "path", width="half",
                         help="Defaults to <input>_offset.pdf next to the input file."),
                ],
            },
            {
                "title": "Offset values",
                "options": [
                    _opt("paper_size", "Paper size (per-size row)", "select",
                         choices=[["", "— global only —"]] + [
                             [p["name"], f"{p['name']} — {p.get('width') or '?'} × {p.get('height') or '?'}"]
                             for p in scm["paper_sizes"]],
                         default="", width="third",
                         help="Which per-size row to work with: it prefills the fields below and is what “Save” records into. Blank = the single global offset."),
                    _opt("x_offset", "X offset (px, right +)", "number", default="", width="quarter"),
                    _opt("y_offset", "Y offset (px, up +)", "number", default="", width="quarter"),
                    _opt("angle", "Angle (deg, clockwise +)", "number", step=0.1, default="", width="quarter"),
                    _opt("ppi", "PPI", "range", default=1200, min=150, max=1200, step=10, width="half"),
                    _opt("save", "Save these as the new offset", "toggle", default=False, width="half"),
                    _opt("use_saved", "Prefill fields from the saved offset", "toggle", default=True, width="half"),
                ],
            },
        ],
    }

    # ------------------------------------------------------------ Calibration
    kinds["calibration"] = {
        "title": "Calibration sheets", "page": "offset", "needs": ["scm"], "cwd": "scm",
        "description": "Generates a two-page alignment sheet for every paper size. Print double-sided (long-edge flip), compare the dot grids, and measure misalignment.",
        "groups": [],
    }

    # ------------------------------------------------------------- Templates
    kinds["dxf_single"] = {
        "title": "Generate a cutting template (DXF)", "page": "templates", "needs": ["scm"], "cwd": "scm",
        "description": "Creates one DXF cutting template for a card-size × paper-size combination.",
        "groups": [
            {
                "title": "Card size",
                "options": [
                    _opt("card_mode", "Use", "segment", choices=[["named", "A named size"], ["custom", "Custom dimensions"]], default="named", width="half"),
                    _opt("card_size", "Named card size", "select",
                         choices=[["", "— pick —"]] + [[c["name"], f"{c['name']} — {c.get('width') or '?'} × {c.get('height') or '?'}"] for c in scm["card_sizes"]],
                         default="standard", width="half"),
                    _opt("card_width", "Custom width", "text", placeholder="63mm · 2.5in", width="quarter"),
                    _opt("card_height", "Custom height", "text", placeholder="88mm · 3.5in", width="quarter"),
                    _opt("card_radius", "Custom corner radius", "text", placeholder="3mm", width="quarter"),
                    _opt("card_name", "Card label (for filename)", "text", width="quarter",
                         help="Optional; only used for the output file name."),
                ],
            },
            {
                "title": "Paper size",
                "options": [
                    _opt("paper_mode", "Use", "segment", choices=[["named", "A named size"], ["custom", "Custom dimensions"]], default="named", width="half"),
                    _opt("paper_size", "Named paper size", "select",
                         choices=[["", "— pick —"]] + [[p["name"], f"{p['name']} — {p.get('width') or '?'} × {p.get('height') or '?'}"] for p in scm["paper_sizes"]],
                         default="letter", width="half"),
                    _opt("paper_width", "Custom width (shorter side)", "text", placeholder="8.5in · 210mm", width="quarter"),
                    _opt("paper_height", "Custom height (longer side)", "text", placeholder="11in · 297mm", width="quarter"),
                    _opt("paper_name", "Paper label (for filename)", "text", width="quarter",
                         help="Optional; only used for the output file name."),
                ],
            },
            {
                "title": "Layout & output",
                "options": [
                    _opt("variant", "Variant", "segment", choices=[["default", "Default"], ["borderless", "Borderless"]], default="default", width="third"),
                    _opt("orientation", "Orientation", "segment",
                         choices=[["optimize", "Optimize"], ["landscape", "Landscape"], ["portrait", "Portrait"]],
                         default="optimize", width="third"),
                    _opt("output_path", "Output file (blank = auto)", "path", width="full",
                         help="Defaults to cutting_templates/dxf/<paper>-<card>-v1.dxf (…/borderless/dxf/ for borderless)."),
                    _opt("save", "Save new size / layout to layouts.json", "toggle", default=True, width="half"),
                ],
            },
        ],
    }

    kinds["dxf_batch"] = {
        "title": "Batch generate DXF templates", "page": "templates", "needs": ["scm"], "cwd": "scm",
        "description": "Generates DXF templates for the standard paper × card size matrix in the repo.",
        "groups": [
            {
                "title": "Mode",
                "options": [
                    _opt("mode", "Mode", "segment",
                         choices=[["missing", "Missing only"], ["all", "Regenerate all"], ["optimize", "Re-optimize orientations"]],
                         default="missing"),
                ],
            },
        ],
    }

    kinds["dxf_list"] = {
        "title": "List available sizes", "page": "utilities", "needs": ["scm"], "cwd": "scm",
        "description": "Prints every card and paper size known to the repo (including extras).",
        "groups": [],
    }

    kinds["clean_up"] = {
        "title": "Clear card image folders", "page": "utilities", "needs": ["scm"], "cwd": "scm",
        "description": "Deletes every image in game/front/ and game/double_sided/ so you can start a new game fresh. The folder README placeholders are kept, and the card back folder is left untouched.",
        "groups": [],
    }

    # ------------------------------------------------------------- Repo copies
    kinds["repo_update"] = {
        "title": "Update a managed repo", "page": "settings", "needs": [], "cwd": "wb",
        "description": "Moves a Workbench-managed copy of a sister repo to the chosen ref. Forward moves fetch only the changed files; rollbacks and large jumps take a full snapshot. Your images, decklists, and local edits are preserved.",
        "groups": [
            {
                "title": "Target",
                "options": [
                    _opt("repo", "Repo", "segment",
                         choices=[["scm", "silhouette-card-maker"], ["extras", "scm-extras"]],
                         default="scm", width="half"),
                    _opt("force_full", "Force full snapshot", "toggle", default=False, width="half",
                         help="Skip the changed-files diff and swap the whole tree (use if a diff misbehaves)."),
                ],
            },
        ],
    }
    kinds["repo_init"] = {
        "title": "Download a managed repo copy", "page": "settings", "needs": [], "cwd": "wb",
        "description": "Fetches a complete copy of a sister repo into the Workbench's own data area, so the app never needs a system Python or a hand-rolled clone.",
        "groups": [
            {
                "title": "Target",
                "options": [
                    _opt("repo", "Repo", "segment",
                         choices=[["scm", "silhouette-card-maker"], ["extras", "scm-extras"]],
                         default="scm", width="half"),
                ],
            },
        ],
    }

    # ---------------------------------------------------------------- Extras
    kinds["extras_generate"] = {
        "title": "Generate extras DXF templates", "page": "extras", "needs": ["extras"], "cwd": "extras",
        "description": "Generates the DXF cutting templates for the extra card sizes (MTG, Sorcery) into scm-extras/cutting_templates/. Finds Silhouette Card Maker automatically as a sister folder and wires SCM_EXTRA_LAYOUTS for you.",
        "groups": [
            {
                "title": "Mode",
                "options": [
                    _opt("mode", "Mode", "segment", choices=[["missing", "Missing only"], ["all", "Regenerate all"]], default="missing"),
                ],
            },
        ],
    }

    kinds["extras_tables"] = {
        "title": "Extras README tables", "page": "extras", "needs": ["extras"], "cwd": "extras",
        "description": "Renders the markdown size tables for the extra card sizes (paste into the README when they change).",
        "groups": [],
    }

    # --------------------------------------------------------------- Plugins
    for slug, meta in PLUGINS.items():
        kinds[f"fetch:{slug}"] = {
            "title": "Fetch Card Art",
            "game": meta["title"],
            # jobs (console tabs, recent jobs) run per game — keep them
            # distinguishable even though the button/heading title is generic
            "job_title": f"Fetch Card Art ({meta['title']})",
            "page": "fetch", "needs": ["scm"], "cwd": "scm", "slug": slug,
            "description": f"Downloads card images for {meta['title']} from a decklist into the game/ folders.",
            # Simple mode lays the form out as one flat row per group — the
            # standard 3-per-row rhythm the PDF page uses (create_pdf's
            # `simple_rows` does it in the manifest because the PDF groups are
            # static; here the groups are built per game, so the rows are added
            # below for the one game whose groups exceed the default shape).
            "groups": fetch_groups(slug),
            # Every game's fetch form: the decklist group (source + file/name/
            # text/URL), then the format group, then each preferences group in
            # its own row (MTG's has 3 toggle-per-row sub-rows inside it).
            "simple_rows": [[o["key"] for o in g["options"]] for g in fetch_groups(slug) if not g.get("collapsible")],
            # URL-based formats (consumed by the same rule the command builder
            # uses): the client auto-selects one of these when the source is URL.
            "url_formats": [f for f, _ in meta["formats"] if f == "url" or f.endswith("_url")],
            # XML-based formats: the client auto-selects one of these when the
            # picked existing decklist file is an .xml (e.g. MTG's MPCFill XML).
            "xml_formats": [f for f, _ in meta["formats"] if f.endswith("_xml")],
        }
        # MTG alone has a preferences group (7 toggles): give it the standard
        # 3-per-row flat layout — the group-level rows above become sub-rows
        # rendered inside the group's own row.
        if slug == "mtg":
            kinds[f"fetch:{slug}"]["groups"][2]["simple_rows"] = [
                ["prefer_set", "ignore_set", "prefer_lang"],
                ["prefer_older_sets", "prefer_showcase", "prefer_extra_art"],
                ["prefer_ub", "ignore_ub", "tokens"],
                ["ignore_set_and_collector_number"],
            ]

    return kinds


# ============================================================================
# Settings
# ============================================================================

DEFAULT_SETTINGS: Dict[str, Any] = {
    "scm_dir": "",
    "extras_dir": "",
    "python": "",
    "port": DEFAULT_PORT,
    "theme": "dark",
    "ui_mode": "simple",
    "auto_open_browser": True,
    "onboarded": False,
    "defaults": {
        "card_size": "standard",
        "paper_size": "letter",
        "ppi": 1200,
        "quality": 100,
    },
    "repos": {
        "scm": {"source": "latest-release", "pin": ""},
        "extras": {"source": "main", "pin": ""},
    },
}


_GIF_1PX = bytes.fromhex("474946383961010001000000000021ff0b4e65747363617065000000003b")


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    data = _try_read_json(SETTINGS_FILE)
    if data:
        for k, v in data.items():
            if k in ("defaults", "repos") and isinstance(v, dict):
                s.setdefault(k, {})
                if isinstance(s.get(k), dict):
                    for k2, v2 in v.items():
                        if k2 in s[k] and isinstance(s[k][k2], dict) and isinstance(v2, dict):
                            s[k][k2].update(v2)
                        else:
                            s[k][k2] = v2
            else:
                s[k] = v
    return s


def save_settings(s: dict) -> None:
    # Share a stable cross-process source lock with repo_sync publication.
    # The caller's in-process _SETTINGS_LOCK still protects read/modify/write
    # callers; this lock protects the file against a deployment in another
    # process.
    with repo_sync._settings_source_lock():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_FILE)
            try:
                fd = os.open(DATA_DIR, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


# The server is threaded; two settings writers in flight would otherwise
# interleave their read-modify-write and one change would be lost.  Both the
# HTTP settings endpoint and native settings.set use this lock.
_SETTINGS_LOCK = threading.Lock()

SETTINGS_CHANGES_MAX_BYTES = 64 * 1024
_SETTINGS_PATH_FIELDS = frozenset(("scm_dir", "extras_dir", "python"))
_SETTINGS_FIELDS = frozenset((
    "scm_dir", "extras_dir", "python", "port", "theme", "ui_mode",
    "auto_open_browser", "onboarded", "defaults",
))
_DEFAULT_FIELDS = frozenset(("card_size", "paper_size", "ppi", "quality"))


def _setting_string(value: Any, *, name: str, max_bytes: int,
                    nonempty: bool = False) -> Optional[str]:
    """Validate a user-controlled UTF-8 string and return an error, if any."""
    if not isinstance(value, str):
        return f"{name} must be a string"
    if nonempty and not value:
        return f"{name} must be non-empty"
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return f"{name} must be valid UTF-8"
    if size > max_bytes:
        return f"{name} exceeds {max_bytes} UTF-8 bytes"
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        return f"{name} contains C0/DEL control characters"
    return None


def validate_settings_changes(changes: Any) -> List[str]:
    """Return bounded, application-level errors for a settings patch.

    This is deliberately shared by the HTTP and native transports.  The
    native method validates its exact ``{changes: ...}`` envelope separately;
    values and the resulting merge have one semantic implementation here.
    """
    if not isinstance(changes, dict):
        return ["changes must be an object"]
    if not changes:
        return ["changes must contain at least one setting"]
    try:
        encoded = json.dumps(changes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return ["changes must be valid JSON"]
    if len(encoded) > SETTINGS_CHANGES_MAX_BYTES:
        return ["changes exceed 64 KiB when encoded"]

    errors: List[str] = []
    for key in changes:
        if not isinstance(key, str) or key not in _SETTINGS_FIELDS:
            errors.append(f"unknown setting: {key}")
    for key, value in changes.items():
        if key in _SETTINGS_PATH_FIELDS:
            error = _setting_string(value, name=key, max_bytes=4096)
            if error:
                errors.append(error)
        elif key == "port":
            if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65535:
                errors.append("port must be an integer from 1024 through 65535")
        elif key == "theme":
            if value not in ("dark", "light") or not isinstance(value, str):
                errors.append("theme must be dark or light")
        elif key == "ui_mode":
            if value not in ("simple", "advanced") or not isinstance(value, str):
                errors.append("ui_mode must be simple or advanced")
        elif key in ("auto_open_browser", "onboarded"):
            if not isinstance(value, bool):
                errors.append(f"{key} must be boolean")
        elif key == "defaults":
            if not isinstance(value, dict):
                errors.append("defaults must be an object")
                continue
            if not value:
                errors.append("defaults must contain at least one setting")
                continue
            for nested_key in value:
                if not isinstance(nested_key, str) or nested_key not in _DEFAULT_FIELDS:
                    errors.append(f"unknown default: {nested_key}")
            for nested_key, nested_value in value.items():
                label = f"defaults.{nested_key}"
                if nested_key in ("card_size", "paper_size"):
                    error = _setting_string(nested_value, name=label, max_bytes=128, nonempty=True)
                    if error:
                        errors.append(error)
                elif nested_key in ("ppi", "quality"):
                    if (isinstance(nested_value, bool) or
                            not isinstance(nested_value, (int, float)) or
                            (isinstance(nested_value, float) and not math.isfinite(nested_value))):
                        errors.append(f"{label} must be a finite number")
                    elif nested_key == "ppi" and not 0 <= nested_value <= 10000:
                        errors.append(f"{label} must be from 0 through 10000")
                    elif nested_key == "quality" and not 0 <= nested_value <= 100:
                        errors.append(f"{label} must be from 0 through 100")
    return errors


def update_settings(changes: Any) -> dict:
    """Atomically validate and apply a settings patch.

    Invalid patches are application results (the HTTP transport sends them as
    400, while native settings.set returns this same result in its ``result``
    field).  The lock covers validation, load, merge, and atomic commit so this
    helper is also safe against callers changing a patch concurrently.  A
    successful save invalidates both manifest and repo snapshots so path/default
    changes are visible immediately.  ``repos`` is intentionally not accepted
    here; /api/repos/save remains its separate locked writer.
    """
    with _SETTINGS_LOCK:
        errors = validate_settings_changes(changes)
        if errors:
            return {"ok": False, "errors": errors}
        settings = load_settings()
        for key, value in changes.items():
            if key == "defaults":
                settings.setdefault("defaults", {}).update(value)
            else:
                settings[key] = value
        try:
            encoded_size = len(json.dumps(
                settings, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            return {"ok": False, "errors": ["merged settings must be valid JSON"]}
        if encoded_size > SETTINGS_CHANGES_MAX_BYTES:
            return {"ok": False, "errors": ["merged settings exceed 64 KiB when encoded"]}
        save_settings(settings)
        invalidate_manifest_cache()
        _INFO_SNAP.clear()
        _REPOS_MTIME.clear()
    return {"ok": True, "settings": settings}

# ============================================================================
# App updates (see updater.py)
# ============================================================================

# One in-flight release check at a time.  The condition and generation are
# deliberately coupled: waiters receive the exact result of the check they
# waited for, and an older worker cannot publish over a newer generation.
_UPDATE_CHECK_CONDITION = threading.Condition()
_UPDATE_CHECKING = False
_UPDATE_CHECK_GENERATION = 0
_UPDATE_CHECK_RESULT = None
_UPDATE_STATE_LOCK = threading.Lock()
_UPDATE_STATE_MAX_BYTES = 128 * 1024
# Separate from job status: run_job publishes a terminal status before its
# worker has finished closing/persisting, so status alone is not an admission
# lease. Both this token and JOBS are guarded by JOBS_LOCK.
_UPDATE_ADMISSION = False
_UPDATE_ADMISSION_JOB = None
# Set only after the candidate has been validated and the update transaction
# is being handed to the external helper.  Ordinary jobs are admitted under
# JOBS_LOCK, so this fence closes the last start-vs-handoff race.
_UPDATE_QUIESCING = False
_UPDATE_QUIESCING_JOB = None


def _default_update_state() -> dict:
    return {"status": "never", "current": SERVER_VERSION, "checked_at": None,
            "latest": None, "asset": None, "reason": None, "release_url": None,
            "published": None}


def _valid_update_state(st: Any) -> bool:
    if not isinstance(st, dict):
        return False
    fields = {"status", "current", "checked_at", "latest", "asset", "reason",
              "release_url", "published"}
    if set(st) != fields or st.get("status") not in {
            "never", "up-to-date", "update-available", "auth-required", "error"}:
        return False
    try:
        for field, maximum in (("current", 128), ("reason", 4096),
                               ("release_url", 2048), ("published", 64)):
            value = st[field]
            if value is not None:
                if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
                    return False
                if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
                    return False
        if not isinstance(st["current"], str) or not st["current"]:
            return False
        if st["checked_at"] is not None and (
                isinstance(st["checked_at"], bool) or
                not isinstance(st["checked_at"], (int, float)) or
                not math.isfinite(float(st["checked_at"])) or
                not 0 <= st["checked_at"] <= 1_000_000_000_000):
            return False
        latest = st["latest"]
        if latest is not None:
            if not isinstance(latest, str) or len(latest.encode("utf-8")) > 128:
                return False
            updater._tag(latest, "latest")
        asset = st["asset"]
        if asset is not None:
            if not isinstance(asset, dict) or set(asset) != {"id", "tag", "name", "url", "size", "digest"}:
                return False
            if (isinstance(asset["id"], bool) or not isinstance(asset["id"], int) or
                    asset["id"] <= 0 or asset["tag"] != latest):
                return False
            name = updater._asset_name(asset["name"])
            if (isinstance(asset["size"], bool) or not isinstance(asset["size"], int) or
                    not 1 <= asset["size"] <= updater.ASSET_MAX_BYTES):
                return False
            digest = asset["digest"]
            if digest is not None and (not isinstance(digest, str) or
                                       not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)):
                return False
            owner, repo = updater._repo_parts()
            updater._asset_url(asset["url"], owner, repo, latest, name)
        if st["release_url"]:
            owner, repo = updater._repo_parts()
            updater._github_release_url(st["release_url"], owner, repo, latest or "")
        if st["status"] == "update-available" and (not latest or asset is None):
            return False
    except Exception:
        return False
    return True


def _invalid_update_state() -> dict:
    st = _default_update_state()
    st.update(status="error", reason="saved update state is invalid")
    return st


def _bundle_shape(path: Path) -> bool:
    """Accept only a complete release bundle, never a source checkout."""
    try:
        if path.is_symlink():
            return False
        path = path.resolve(strict=True)
        def directory(value: Path) -> bool:
            return value.is_dir() and not value.is_symlink()
        def regular(value: Path) -> bool:
            return value.is_file() and not value.is_symlink()
        if sys.platform == "darwin":
            return (path.suffix == ".app" and directory(path) and
                    regular(path / "Contents/MacOS/SCM Workbench") and
                    directory(path / "Contents/app/scm_workbench") and
                    directory(path / "Contents/app/ui") and
                    directory(path / "Contents/runtime"))
        if os.name == "nt":
            return (directory(path) and regular(path / "SCM Workbench.exe") and
                    directory(path / "app/scm_workbench") and
                    directory(path / "app/ui") and directory(path / "runtime"))
    except (OSError, RuntimeError):
        pass
    return False


def _own_bundle() -> Optional[str]:
    """Find the canonical complete release bundle (None in development)."""
    if os.environ.get("SCM_WORKBENCH_BUNDLE"):
        hinted = Path(os.environ["SCM_WORKBENCH_BUNDLE"])
        return str(hinted.resolve()) if _bundle_shape(hinted) else None
    if not os.environ.get("SCM_WORKBENCH_PACKAGED"):
        return None
    exe = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if exe:
        for parent in (exe, *exe.parents):
            if ((sys.platform == "darwin" and parent.suffix == ".app") or
                    (os.name == "nt" and _bundle_shape(parent))):
                if _bundle_shape(parent):
                    return str(parent.resolve())
    return None


def load_update_state() -> dict:
    """Read only a complete, bounded state document; never repair it in place."""
    try:
        if UPDATE_STATE_FILE.is_symlink() or not UPDATE_STATE_FILE.is_file():
            return _default_update_state()
        if UPDATE_STATE_FILE.stat().st_size > _UPDATE_STATE_MAX_BYTES:
            return _invalid_update_state()
        with open(UPDATE_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if _valid_update_state(st) else _invalid_update_state()
    except Exception:
        return _invalid_update_state()


def save_update_state(st: dict) -> None:
    """Publish state with a unique fsynced temporary file and symlink guard."""
    if not _valid_update_state(st):
        raise ValueError("invalid update state")
    with _UPDATE_STATE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if UPDATE_STATE_FILE.is_symlink():
            raise OSError("refusing to replace symlinked update state")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{UPDATE_STATE_FILE.name}.",
                                        suffix=".tmp", dir=str(DATA_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(st, f, separators=(",", ":"), ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, UPDATE_STATE_FILE)
            try:
                dir_fd = os.open(DATA_DIR, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def run_update_check() -> dict:
    """Run one lookup; concurrent callers share one network result."""
    global _UPDATE_CHECKING, _UPDATE_CHECK_GENERATION, _UPDATE_CHECK_RESULT
    with _UPDATE_CHECK_CONDITION:
        if _UPDATE_CHECKING:
            generation = _UPDATE_CHECK_GENERATION
            while _UPDATE_CHECKING and generation == _UPDATE_CHECK_GENERATION:
                _UPDATE_CHECK_CONDITION.wait()
            if _UPDATE_CHECK_RESULT is not None:
                return copy.deepcopy(_UPDATE_CHECK_RESULT)
        _UPDATE_CHECKING = True
        generation = _UPDATE_CHECK_GENERATION

    st = _default_update_state()
    try:
        try:
            rel = updater.latest_release()
        except updater.AuthRequiredError as e:
            st.update(status="auth-required", reason=str(e), checked_at=time.time())
        except updater.UpdateError as e:
            st.update(status="error", reason=str(e), checked_at=time.time())
        else:
            if rel.get("tag") and (updater.is_newer(rel["tag"], SERVER_VERSION) or
                                    rel["tag"].lstrip("v") == SERVER_VERSION):
                try:
                    asset = updater.pick_asset(rel)
                except updater.UpdateError as e:
                    st.update(status="error", checked_at=time.time(),
                              reason=f"{rel['tag']} is out, but: {e}")
                else:
                    st.update(status="update-available", latest=rel["tag"], asset=asset,
                              release_url=rel.get("url"), published=rel.get("published"),
                              checked_at=time.time())
            else:
                st.update(status="up-to-date", latest=rel.get("tag") or None,
                          checked_at=time.time())
    except Exception as exc:
        st = _default_update_state()
        st.update(status="error", reason=f"release lookup failed: {exc}",
                  checked_at=time.time())
    with _UPDATE_CHECK_CONDITION:
        # Publish only if this worker still owns the generation it started.
        # Keeping the compare-and-set next to the atomic file replacement also
        # prevents a future overlapping implementation from clobbering newer
        # state with a slow, older lookup.
        if generation == _UPDATE_CHECK_GENERATION:
            try:
                save_update_state(st)
            except Exception as exc:
                st = _default_update_state()
                st.update(status="error", reason=f"could not save update state: {exc}",
                          checked_at=time.time())
            _UPDATE_CHECK_GENERATION += 1
            _UPDATE_CHECK_RESULT = copy.deepcopy(st)
        else:
            st = copy.deepcopy(_UPDATE_CHECK_RESULT or st)
        _UPDATE_CHECKING = False
        _UPDATE_CHECK_CONDITION.notify_all()
        return copy.deepcopy(st)


def _poll_update_result(timeout: float = 60.0) -> None:
    """Bounded startup poll: helper completion can race worker startup."""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        try:
            if reconcile_update_result():
                return
        except Exception:
            pass
        if not (DATA_DIR / ".update-result.json").exists():
            time.sleep(0.5)
        else:
            time.sleep(0.1)


def _update_daemon() -> None:
    """Check at server start, then once a day while the app is open."""
    time.sleep(1)
    _poll_update_result()
    while True:
        try:
            reconcile_update_result()
            st = load_update_state()
            age = None if st.get("checked_at") is None else time.time() - float(st["checked_at"])
            if st.get("status") == "never" or age is None or age > UPDATE_CHECK_INTERVAL:
                out = run_update_check()
                tag = out.get("latest") or ""
                _diag(f"[updater] release check: {out.get('status')}" + (f" → {tag}" if tag else ""))
        except Exception as e:
            _diag(f"[updater] check failed: {e}")
        time.sleep(1800)


def start_update_job(*_ignored, **_ignored_kwargs) -> Tuple[Optional[dict], List[str]]:
    """Admit exactly one install from the canonical, checked update state."""
    global _UPDATE_ADMISSION, _UPDATE_ADMISSION_JOB

    def error_text(exc: Exception) -> str:
        text = str(exc).replace("\x00", " ").replace("\n", " ").strip()
        return (text or exc.__class__.__name__)[:256]

    # State validation, admission, log creation, insertion, and thread start
    # are serialized. Caller values are intentionally ignored.
    with JOBS_LOCK:
        # A test/process teardown or recovery may have removed an abandoned
        # record. Do not let that orphaned token permanently deny admission.
        if _UPDATE_ADMISSION and _UPDATE_ADMISSION_JOB not in JOBS:
            _UPDATE_ADMISSION = False
            _UPDATE_ADMISSION_JOB = None
        if _UPDATE_ADMISSION or _UPDATE_QUIESCING:
            return None, ["an update is already running; try again later"]
        st = load_update_state()
        if (not _valid_update_state(st) or st.get("status") != "update-available" or
                st.get("current") != SERVER_VERSION or
                not updater.is_newer(st.get("latest"), SERVER_VERSION) or
                not isinstance(st.get("asset"), dict) or
                st["asset"].get("tag") != st.get("latest")):
            return None, ["No newer update is available from the current checked state."]
        latest = st["latest"]
        if any(j.get("kind") == "update" and j.get("status") == "running"
               for j in JOBS.values()):
            return None, ["an update is already running; try again later"]
        job_id = uuid.uuid4().hex[:10]
        log_f = None
        log_created = False
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            job = {
                "id": job_id, "ts": time.time(), "kind": "update",
                "title": f"Update the app to {latest}",
                "cmd": f"workbench: self-update → {latest}", "args": {},
                "status": "running", "exit_code": None,
                "log_file": str(LOGS_DIR / f"{job_id}.log"), "log_lines": [],
                "first_seq": 0, "subs": [], "warnings": [],
                "started": time.time(), "ended": None, "duration": None,
                "proc": None, "progress": None,
            }
            # Exclusive creation prevents a fixed sibling path from replacing
            # a symlink or a prior log if an ID collision ever occurs.
            log_f = open(job["log_file"], "x", encoding="utf-8")
            log_created = True
            header = f"$ {job['cmd']}"
            log_f.write(header + "\n\n")
            log_f.flush()
            job["log_lines"] = [header]
            JOBS[job_id] = job
            _UPDATE_ADMISSION = True
            _UPDATE_ADMISSION_JOB = job_id
        except Exception as exc:
            if log_f is not None:
                try:
                    log_f.close()
                except Exception:
                    pass
            if log_created:
                try:
                    (LOGS_DIR / f"{job_id}.log").unlink()
                except OSError:
                    pass
            return None, [f"could not start update: {error_text(exc)}"]

        plan = {
            "repo": updater.UPDATE_REPO,
            "current": SERVER_VERSION,
            "latest": st["latest"],
            "asset": copy.deepcopy(st["asset"]),
            "bundle": _own_bundle(),
            "work": DATA_DIR / "update",
        }

        def worker():
            global _UPDATE_ADMISSION, _UPDATE_ADMISSION_JOB
            try:
                updater.run_job(job, plan, log_f)
            except Exception as exc:
                # Keep the failure bounded and do not expose a traceback or an
                # arbitrary exception string to the job protocol.
                message = f"    ! update worker failed: {error_text(exc)}"
                try:
                    log_f.write(message + "\n")
                    log_f.flush()
                except Exception:
                    pass
                with JOBS_LOCK:
                    job["log_lines"].append(message)
                    job["status"] = "fail"
                    job["exit_code"] = 1
                    job["ended"] = time.time()
                    job["duration"] = round(job["ended"] - job["started"], 2)
                    subscribers = list(job.get("subs", []))
                for subscriber in subscribers:
                    try:
                        subscriber.put(("done", "fail", 1))
                    except Exception:
                        pass
            finally:
                try:
                    log_f.close()
                except Exception:
                    pass
                try:
                    _persist_jobs()
                except Exception:
                    pass
                # Release only after updater.run_job, failure handling, log
                # close, and persistence have all completed.
                with JOBS_LOCK:
                    if _UPDATE_ADMISSION_JOB == job_id:
                        _UPDATE_ADMISSION = False
                        _UPDATE_ADMISSION_JOB = None

        try:
            threading.Thread(target=worker, daemon=True, name="update-install").start()
        except Exception as exc:
            # Thread construction/start failed: rollback every admission side
            # effect while still holding JOBS_LOCK.
            JOBS.pop(job_id, None)
            _UPDATE_ADMISSION = False
            _UPDATE_ADMISSION_JOB = None
            try:
                log_f.close()
            except Exception:
                pass
            if log_created:
                try:
                    (LOGS_DIR / f"{job_id}.log").unlink()
                except OSError:
                    pass
            return None, [f"could not start update: {error_text(exc)}"]
    return job, []



# ============================================================================
# Managed repo copies (see repo_sync.py)
# ============================================================================

# Ref metadata is shared by HTTP and native callers.  A per-key in-flight
# event prevents two simultaneous requests from issuing duplicate GitHub calls.
_refs_cache = {}
_REFS_CACHE_LOCK = threading.Lock()
_REFS_INFLIGHT = {}
_REFS_CACHE_TTL = 3600

# Native repository control operations are deliberately independent of the job
# system: they have a tiny bounded registry, two daemon workers, and no
# cancellation/timeout promise to expose to the shell.
_REPO_OP_LOCK = threading.Lock()
_REPO_OPS = {}
_REPO_OP_QUEUE = queue.Queue(maxsize=16)
_REPO_OP_WORKERS_STARTED = False
_REPO_OP_ACTIVE_MAX = 16
_REPO_OP_TOTAL_MAX = 32
_REPO_OP_RESULT_MAX = 1024 * 1024
_REPO_OP_TTL = 300


def _bounded_error(value: Any) -> str:
    text = " ".join(str(value or "operation failed").split())
    return text[:256] or "operation failed"


def _bounded_errors(errors: Any) -> list:
    if isinstance(errors, (str, bytes)):
        errors = [errors]
    if not isinstance(errors, (list, tuple)):
        errors = [errors]
    return [_bounded_error(error) for error in list(errors)[:8]] or ["operation failed"]


def _repo_result_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _prune_repo_operations_locked(now=None):
    now = time.time() if now is None else now
    for operation_id, operation in list(_REPO_OPS.items()):
        if operation.get("status") == "done" and now - operation.get("ended", now) >= _REPO_OP_TTL:
            _REPO_OPS.pop(operation_id, None)


def _repo_operation_worker():
    while True:
        operation_id = _REPO_OP_QUEUE.get()
        try:
            with _REPO_OP_LOCK:
                operation = _REPO_OPS.get(operation_id)
            if operation is None:
                continue
            try:
                result = operation["call"]()
                if not isinstance(result, dict):
                    result = {"ok": False, "errors": ["operation returned an invalid result"]}
                if "errors" in result:
                    result["errors"] = _bounded_errors(result["errors"])
                if _repo_result_size(result) > _REPO_OP_RESULT_MAX:
                    result = {"ok": False, "errors": ["operation result exceeds 1 MiB"]}
            except Exception:
                # Never put traceback/network details on the protocol and never
                # let one worker exception kill the bounded operation service.
                result = {"ok": False, "errors": ["operation failed"]}
            with _REPO_OP_LOCK:
                current = _REPO_OPS.get(operation_id)
                if current is not None:
                    current["status"] = "done"
                    current["result"] = result
                    current["ended"] = time.time()
        finally:
            _REPO_OP_QUEUE.task_done()


def _ensure_repo_operation_workers_locked():
    global _REPO_OP_WORKERS_STARTED
    if _REPO_OP_WORKERS_STARTED:
        return
    _REPO_OP_WORKERS_STARTED = True
    for number in range(2):
        threading.Thread(target=_repo_operation_worker, daemon=True,
                         name=f"repo-operation-{number + 1}").start()


def _start_repo_operation(kind: str, args: dict) -> dict:
    with _REPO_OP_LOCK:
        _prune_repo_operations_locked()
        active = sum(op.get("status") == "running" for op in _REPO_OPS.values())
        if active >= _REPO_OP_ACTIVE_MAX or len(_REPO_OPS) >= _REPO_OP_TOTAL_MAX:
            return {"ok": False, "errors": ["too many repository operations"]}
        operation_id = secrets.token_urlsafe(24)[:64]
        while operation_id in _REPO_OPS:
            operation_id = secrets.token_urlsafe(24)[:64]
        calls = {
            "refs": lambda: repo_refs_result(args["repo"]),
            "source.set": lambda: repo_source_result(args["repo"], args["source"]),
            "check": lambda: repo_check_result(args["repo"], args["force"]),
        }
        operation = {"status": "running", "call": calls[kind], "result": None}
        _REPO_OPS[operation_id] = operation
        _ensure_repo_operation_workers_locked()
        try:
            _REPO_OP_QUEUE.put_nowait(operation_id)
        except queue.Full:
            _REPO_OPS.pop(operation_id, None)
            return {"ok": False, "errors": ["too many repository operations"]}
    return {"ok": True, "operation": {"id": operation_id, "status": "running"}}


def poll_repo_operation(operation_id: str) -> dict:
    with _REPO_OP_LOCK:
        _prune_repo_operations_locked()
        operation = _REPO_OPS.get(operation_id)
        if operation is None:
            return {"ok": False, "error": {"code": "bad_request", "message": "operation not found"}}
        if operation["status"] == "running":
            return {"ok": True, "status": "running"}
        return {"ok": True, "status": "done", "result": operation["result"]}


def repos_view(settings: dict) -> list:
    """One display row per sister repo: where it lives, what it's at, what's asked."""
    st = repo_sync.load_state()
    prog = repo_sync.load_progress()
    # A row is only “live” while its writer is: accepted writes stamp ts (a
    # download tick every ~2% of transfer, a fingerprint heartbeat every ~2%
    # of files). A row older than 300 s is a leftover from a pass that never
    # reached its clear — a first boot that died mid-clone, for example — and
    # it must not masquerade as in-flight work on an already-deployed repo.
    # Rows without a stamp (written before stamps existed) are stale by the
    # same rule, which is what heals a first boot stuck on the old race.
    now = time.time()
    for k, v in list(prog.items()):
        if float(v.get("ts") or 0) <= 0 or now - float(v["ts"]) > 300:
            prog.pop(k, None)
    scm, extras = effective_dirs(settings)
    rows = []
    for key, meta in repo_sync.REPOS.items():
        r = st.get(key) or {}
        deployed = r.get("deployed")
        managed = bool(deployed) and r.get("mode") == "bundled"
        cfg = (settings.get("repos", {}) or {}).get(key) or {}
        source = r.get("source") or cfg.get("source") or meta.get("default_source", "main")
        pin = r.get("pin") or cfg.get("pin") or ""
        if source == "pinned" and pin:
            source = pin
        ext = scm if key == "scm" else extras
        if managed:
            path, mode = DATA_DIR / meta["rel"], "managed"
        elif ext:
            path, mode = ext, "external"
        else:
            path, mode = None, "missing"
        rows.append({
            "key": key, "name": meta["name"], "mode": mode, "path": str(path) if path else None,
            "source": source, "deployed": deployed, "last_check": r.get("last_check"),
            "progress": (prog.get(key) or None),
        })
    return rows


def run_repo_check(key: str, force: bool = False) -> dict:
    """In-process check implementation shared by HTTP and native control."""
    try:
        return repo_sync.check_repo(key, force=force)
    except repo_sync.RepoError as e:
        return {"repo": key, "ok": False, "error": _bounded_error(e)}
    except Exception:
        return {"repo": key, "ok": False, "error": "check failed"}


def _invalidate_repo_views() -> None:
    invalidate_manifest_cache()
    _INFO_SNAP.clear()
    _REPOS_MTIME.clear()


def repo_check_result(key: str, force: bool = False) -> dict:
    """Return the complete browser response body for a repository check."""
    res = run_repo_check(key, force=force)
    if res.get("ok"):
        res["last_check"] = repo_sync.load_state().get(key, {}).get("last_check")
        _invalidate_repo_views()
    body = {"ok": bool(res.get("ok")), **(
        res if res.get("ok") else {"errors": _bounded_errors(res.get("error", "check failed"))}
    ), "repos": repos_view(load_settings())}
    return body


def _repo_source_settings_mirror(key: str, source: str) -> None:
    # This is intentionally a second phase.  State has already been committed
    # as the canonical source, so a disk/settings failure cannot leave the
    # effective source ambiguous.
    with _SETTINGS_LOCK:
        settings = load_settings()
        repos = settings.setdefault("repos", {})
        if not isinstance(repos, dict):
            repos = {}
            settings["repos"] = repos
        config = repos.setdefault(key, {})
        if not isinstance(config, dict):
            config = {}
            repos[key] = config
        config["source"] = source if source in ("main", "latest-release") else "pinned"
        config["pin"] = source if source not in ("main", "latest-release") else ""
        save_settings(settings)


def repo_source_result(key: str, source: str) -> dict:
    """Set a source and return the complete browser response body.

    The target resolution and canonical state publication are serialized with
    init/update/check.  Settings are only a mirror and are written after the
    state commit; if that mirror fails, state remains authoritative.
    """
    try:
        key = repo_sync.validate_repo_key(key)
        source = repo_sync.validate_source(source)
        with repo_sync._repo_lock(key):
            # Keep the settings-backed source stable while resolving it, and
            # retain the established repo -> settings-source -> state order.
            with repo_sync._settings_source_lock():
                target = repo_sync.resolve_target(key, source)
                repo_sync._record_source_and_check_locked(key, source, target)
        _invalidate_repo_views()
        try:
            _repo_source_settings_mirror(key, source)
        except Exception as exc:
            # The state/check commit is authoritative and must not be reported
            # as a failed source change merely because its optional settings
            # mirror could not be persisted.  Keep the warning bounded; the
            # browser and native transports receive this exact same body.
            return {"ok": True, "repo": key, "source": source, "target": target,
                    "canonical": True,
                    "warnings": [f"source mirror failed: {_bounded_error(exc)}"],
                    "repos": repos_view(load_settings())}
        settings = load_settings()
        return {"ok": True, "repo": key, "source": source, "target": target,
                "repos": repos_view(settings)}
    except repo_sync.RepoError as exc:
        return {"ok": False, "repo": key, "errors": [_bounded_error(exc)],
                "repos": repos_view(load_settings())}
    except Exception:
        return {"ok": False, "repo": key, "errors": ["source update failed"],
                "repos": repos_view(load_settings())}


def repo_refs_result(key: str) -> dict:
    """Return refs through a bounded, per-repository single-flight cache."""
    try:
        key = repo_sync.validate_repo_key(key)
        while True:
            now = time.time()
            with _REFS_CACHE_LOCK:
                cached = _refs_cache.get(key)
                if cached and now - cached[0] < _REFS_CACHE_TTL:
                    refs = cached[1]
                    return {"ok": True, "repo": key, "refs": refs}
                flight = _REFS_INFLIGHT.get(key)
                if flight is None:
                    flight = threading.Event()
                    _REFS_INFLIGHT[key] = flight
                    leader = True
                else:
                    leader = False
            if not leader:
                # HTTP callers may wait for the one network request. Native
                # callers are already off the IPC reader in an operation.
                flight.wait()
                continue
            try:
                refs = repo_sync.list_refs(key)
                if _repo_result_size(refs) > repo_sync.REFS_RESULT_CAP:
                    raise repo_sync.RepoError("GitHub refs result is too large")
                with _REFS_CACHE_LOCK:
                    _refs_cache[key] = (time.time(), refs)
                return {"ok": True, "repo": key, "refs": refs}
            except repo_sync.RepoError as exc:
                return {"ok": False, "repo": key, "errors": [_bounded_error(exc)]}
            finally:
                with _REFS_CACHE_LOCK:
                    event = _REFS_INFLIGHT.pop(key, None)
                    if event is not None:
                        event.set()
    except Exception:
        return {"ok": False, "repo": key, "errors": ["could not load repository refs"]}


def effective_dirs(settings: dict) -> Tuple[Optional[Path], Optional[Path]]:
    def resolve(p: str) -> Optional[Path]:
        if not p:
            return None
        pp = Path(p)
        if not pp.is_absolute():
            pp = Path(__file__).resolve().parent / pp
        return pp if pp.is_dir() else None

    scm = resolve(settings.get("scm_dir") or "")
    extras = resolve(settings.get("extras_dir") or "")
    # A managed copy (downloaded by the Workbench itself) counts as the repo
    # when no explicit path is set — that's what makes a packaged app fully
    # self-contained. An explicit user path always wins; in a bare dev checkout
    # (no managed copies) the old sibling auto-detect applies.
    st = repo_sync.load_state()
    if not scm:
        r = st.get("scm") or {}
        if r.get("deployed"):
            p = repo_sync.repo_dir("scm")
            if p.is_dir():
                scm = p
    if not extras:
        r = st.get("extras") or {}
        if r.get("deployed"):
            p = repo_sync.repo_dir("extras")
            if p.is_dir():
                extras = p
    if not scm:
        scm = find_scm_repo()
    if not extras:
        extras = find_extras_repo()
    return scm, extras


# ============================================================================
# Offsets
#
# silhouette-card-maker keeps ONE global printer offset per repo
# (data/offset_data.json, read by create_pdf --load_offset and offset_pdf.py).
# The required correction, however, depends on the paper you feed — so the
# Workbench keeps a per-paper-size table of its own and *stages* the matching
# row into that shared file right before a run. SCM's code never changes;
# it just reads the one file it always knew about.
# ============================================================================

# Offset state is Workbench-owned.  The upstream file is only a projection
# because SCM itself has one (global) offset slot.  Keep the old name around as
# a migration source for installations made before this state file existed.
OFFSET_STATE_FILE = DATA_DIR / "offset_state.json"
_INITIAL_OFFSET_DATA_DIR = DATA_DIR

def _offset_state_path() -> Path:
    # Tests and embedders historically replace DATA_DIR without knowing about
    # this newer file; keep that isolation while honoring an explicit path.
    if OFFSET_STATE_FILE == _INITIAL_OFFSET_DATA_DIR / "offset_state.json" and DATA_DIR != _INITIAL_OFFSET_DATA_DIR:
        return DATA_DIR / "offset_state.json"
    return OFFSET_STATE_FILE

OFFSET_STAGE_LOCK = threading.Lock()       # compatibility for embedders
OFFSET_LEASE = threading.Lock()
OFFSET_NAME_MAX_BYTES = 128
OFFSET_MAX_ERRORS = 8


def _finite_angle(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        angle = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return angle if math.isfinite(angle) and -360 <= angle <= 360 else None


def _offset_row(value: Any, *, strict: bool = False) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    if strict and set(value) != {"x", "y", "angle"}:
        return None
    x, y, angle = value.get("x"), value.get("y"), value.get("angle")
    clean_angle = _finite_angle(angle)
    if (not isinstance(x, int) or isinstance(x, bool) or not -100000 <= x <= 100000 or
            not isinstance(y, int) or isinstance(y, bool) or not -100000 <= y <= 100000 or
            clean_angle is None):
        return None
    return {"x": x, "y": y, "angle": clean_angle}


def _offset_name(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value.encode("utf-8")) > OFFSET_NAME_MAX_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    if any(unicodedata.category(c) == "Cc" for c in value):
        return None
    return value


def _offset_errors(errors: Any) -> list:
    return [str(e)[:256] for e in list(errors)[:OFFSET_MAX_ERRORS]]


def _safe_offset_paths(scm: Optional[Path]) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Return root/data/file only when the projection cannot escape root."""
    if scm is None:
        return None, None, "SCM repo not found — set it in Settings."
    try:
        root = Path(scm)
        if not root.is_dir() or root.is_symlink():
            return None, None, "SCM repo is not a safe directory."
        root_real = root.resolve(strict=True)
        data = root / "data"
        if data.is_symlink():
            return None, None, "SCM data directory is a symlink."
        if data.exists() and not data.is_dir():
            return None, None, "SCM data path is not a directory."
        data_real = data.resolve(strict=False)
        data_real.relative_to(root_real)
        target = data / "offset_data.json"
        if target.is_symlink():
            return None, None, "SCM offset file is a symlink."
        target.resolve(strict=False).relative_to(root_real)
        return root, data, None
    except (OSError, ValueError):
        return None, None, "SCM offset paths are not safe."


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError:
            pass
    except Exception:
        try: os.unlink(name)
        except OSError: pass
        raise


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


def _legacy_rows() -> dict:
    raw = _try_read_json(PER_SIZE_OFFSETS_FILE)
    if not isinstance(raw, dict):
        return {}
    rows = {}
    for name, value in raw.items():
        clean_name, row = _offset_name(name), _offset_row(value)
        if clean_name and row:
            rows[clean_name] = row
    return rows


class OffsetStateError(Exception):
    """A present canonical state file failed its structural safety contract."""


class OffsetRecoveryError(Exception):
    """A pending upstream save cannot be reconciled without guessing."""


def _read_upstream_offset(scm: Optional[Path]) -> Optional[dict]:
    _, _, error = _safe_offset_paths(scm)
    if error:
        return None
    try:
        o = json.loads((Path(scm) / "data" / "offset_data.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    # Older SCM versions omitted angle_offset when it was zero.  That is a
    # valid legacy baseline, not malformed state.
    return _offset_row({"x": o.get("x_offset"), "y": o.get("y_offset"),
                        "angle": o.get("angle_offset", 0.0)})


def _validate_offset_state(raw: Any) -> dict:
    """Validate canonical state without repairing or dropping any bytes."""
    if not isinstance(raw, dict):
        raise OffsetStateError("canonical offset state must be an object")
    required = {"version", "global", "rows", "staged_size"}
    if not required.issubset(raw) or set(raw) - required - {"pending"}:
        raise OffsetStateError("canonical offset state has an invalid schema")
    if raw.get("version") != 1 or isinstance(raw.get("version"), bool):
        raise OffsetStateError("canonical offset state version is unsupported")
    global_row = raw.get("global")
    if global_row is not None:
        global_row = _offset_row(global_row, strict=True)
        if global_row is None:
            raise OffsetStateError("canonical global offset is malformed")
    raw_rows = raw.get("rows")
    if not isinstance(raw_rows, dict):
        raise OffsetStateError("canonical offset rows must be an object")
    rows = {}
    for name, value in raw_rows.items():
        clean_name = _offset_name(name)
        row = _offset_row(value, strict=True)
        if clean_name is None or row is None:
            raise OffsetStateError("canonical offset row is malformed")
        rows[clean_name] = row
    staged = raw.get("staged_size")
    if staged is not None and (_offset_name(staged) is None or staged not in rows):
        raise OffsetStateError("canonical staged offset name is invalid")
    pending = raw.get("pending")
    clean_pending = None
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {"target", "intended", "prior_projection"}:
            raise OffsetStateError("canonical pending offset record is malformed")
        target = pending.get("target")
        if target != "global" and (_offset_name(target) is None):
            raise OffsetStateError("canonical pending offset target is invalid")
        intended = _offset_row(pending.get("intended"), strict=True)
        prior = pending.get("prior_projection")
        if intended is None or (prior is not None and _offset_row(prior, strict=True) is None):
            raise OffsetStateError("canonical pending offset values are malformed")
        clean_pending = {"target": target, "intended": intended,
                         "prior_projection": _offset_row(prior, strict=True) if prior is not None else None}
    out = {"version": 1, "global": global_row, "rows": rows, "staged_size": staged}
    if clean_pending is not None:
        out["pending"] = clean_pending
    return out


def _load_offset_state_strict() -> dict:
    state_path = _offset_state_path()
    if state_path.exists() or state_path.is_symlink():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OffsetStateError(f"canonical offset state is unreadable: {exc}")
        return _validate_offset_state(raw)
    return {"version": 1, "global": _read_upstream_offset(effective_dirs(load_settings())[0]),
            "rows": _legacy_rows(), "staged_size": None}


def load_offset_state() -> dict:
    """Read state safely; direct inspection sanitizes, mutations use strict load."""
    try:
        return _load_offset_state_strict()
    except OffsetStateError:
        raw = _try_read_json(_offset_state_path()) or {}
        # Inspection is allowed to omit unusable rows, but this repaired view
        # is never written back and all mutation paths use the strict loader.
        global_row = _offset_row(raw.get("global")) if isinstance(raw, dict) else None
        rows = {}
        raw_rows = raw.get("rows") if isinstance(raw, dict) else {}
        if isinstance(raw_rows, dict):
            for name, value in raw_rows.items():
                clean_name, row = _offset_name(name), _offset_row(value)
                if clean_name and row:
                    rows[clean_name] = row
        staged = raw.get("staged_size") if isinstance(raw, dict) else None
        if not isinstance(staged, str) or staged not in rows:
            staged = None
        return {"version": 1, "global": global_row, "rows": rows, "staged_size": staged}


def load_per_size_offsets() -> dict:
    try:
        return dict(load_offset_state().get("rows") or {})
    except OffsetStateError:
        return {}


def save_per_size_offsets(table: dict) -> None:
    # Compatibility helper: new callers should use offset_set.  It still writes
    # the canonical file, never the old table, and sanitizes untrusted rows.
    state = _load_offset_state_strict()
    rows = {}
    for name, value in (table or {}).items():
        clean_name, row = _offset_name(name), _offset_row(value)
        if clean_name and row:
            rows[clean_name] = row
    state["rows"] = rows
    _atomic_json(_offset_state_path(), state)


def write_global_offset(scm: Optional[Path], x: int, y: int, angle: float) -> None:
    """Atomically project the shared SCM offset file after safety checks."""
    _, data, error = _safe_offset_paths(scm)
    if error:
        raise OSError(error)
    _atomic_json(data / "offset_data.json", {"x_offset": int(x), "y_offset": int(y), "angle_offset": float(angle)})


def read_global_offset(scm: Optional[Path]) -> Optional[dict]:
    return _read_upstream_offset(scm)


def _offset_projection(state: dict) -> Optional[dict]:
    staged = state.get("staged_size")
    row = state.get("rows", {}).get(staged) if staged else None
    return row or state.get("global")


def _read_upstream_projection(scm: Optional[Path]) -> Tuple[Optional[dict], Optional[str]]:
    target, _, error = _projection_snapshot(scm)
    if error:
        return None, error
    if not target.is_file():
        return None, None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"upstream offset projection is unreadable: {exc}"
    if not isinstance(raw, dict):
        return None, "upstream offset projection is malformed"
    # Legacy files may omit angle_offset; normalize that historical shape.
    row = _offset_row({"x": raw.get("x_offset"), "y": raw.get("y_offset"),
                       "angle": raw.get("angle_offset", 0.0)})
    return (row, None) if row is not None else (None, "upstream offset projection is malformed")


def _reconcile_pending_locked(scm: Optional[Path], state: dict) -> Optional[str]:
    pending = state.get("pending")
    if pending is None:
        return None
    current, error = _read_upstream_projection(scm)
    if error:
        return error
    intended = pending["intended"]
    prior = pending["prior_projection"]
    if current == intended:
        target = pending["target"]
        if target == "global":
            state["global"], state["staged_size"] = intended, None
        else:
            state["rows"][target], state["staged_size"] = intended, target
        state.pop("pending", None)
        return _commit_offset_state(state, scm, project=True)
    if current == prior:
        state.pop("pending", None)
        # The upstream file already contains the old projection, so clearing
        # the durable intent does not need to rewrite it.
        return _commit_offset_state(state, scm, project=False)
    return "pending offset save cannot be reconciled safely; upstream offset changed unexpectedly"


def _projection_snapshot(scm: Optional[Path]) -> Tuple[Optional[Path], Optional[bytes], Optional[str]]:
    _, data, error = _safe_offset_paths(scm)
    if error:
        return None, None, error
    target = data / "offset_data.json"
    try:
        return target, target.read_bytes() if target.is_file() else None, None
    except OSError as exc:
        return None, None, f"could not read SCM offset file: {exc}"


def _restore_file(path: Path, content: Optional[bytes]) -> None:
    if content is None:
        try: path.unlink()
        except FileNotFoundError: pass
    else:
        _atomic_bytes(path, content)


def _commit_offset_state(state: dict, scm: Optional[Path], *, project: bool = True) -> Optional[str]:
    """Canonical-first transaction with projection rollback on failure."""
    try:
        state = _validate_offset_state(state)
    except OffsetStateError as exc:
        return str(exc)[:256]
    state_path = _offset_state_path()
    old_state_exists = state_path.is_file()
    try:
        old_state_bytes = state_path.read_bytes() if old_state_exists else None
    except OSError as exc:
        return f"could not read offset state: {exc}"
    target = old_projection = None
    if project:
        target, old_projection, error = _projection_snapshot(scm)
        if error:
            return error
    try:
        _atomic_json(state_path, state)
        if project:
            projection = _offset_projection(state)
            if projection is None:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_json(target, {"x_offset": projection["x"], "y_offset": projection["y"],
                                      "angle_offset": projection["angle"]})
    except Exception as exc:
        # Best effort rollback is important here: canonical-first is safe on a
        # crash, but a normal second-write failure must be invisible to callers.
        try:
            _restore_file(state_path, old_state_bytes)
            if project:
                _restore_file(target, old_projection)
        except Exception:
            pass
        return f"could not save offset: {exc}"
    _INFO_SNAP.clear()
    MANIFEST_CACHE.clear()
    return None


def _known_paper_names() -> set:
    try:
        scm, extras = effective_dirs(load_settings())
        if not scm:
            return set()
        info = read_scm_info(scm, extras)
        return {p["name"] for p in info.get("paper_sizes", []) if _offset_name(p.get("name"))}
    except Exception:
        return set()


def _recover_offset_projection_locked(scm: Optional[Path]) -> Optional[str]:
    if not (_offset_state_path().exists() or _offset_state_path().is_symlink()):
        return None
    try:
        state = _load_offset_state_strict()
    except OffsetStateError as exc:
        return str(exc)[:256]
    had_pending = state.get("pending") is not None
    pending_error = _reconcile_pending_locked(scm, state)
    if pending_error:
        return str(pending_error)[:256]
    if had_pending:
        return None
    projection = _offset_projection(state)
    target, _, error = _projection_snapshot(scm)
    if error:
        return error
    try:
        if projection is None:
            try: target.unlink()
            except FileNotFoundError: pass
        else:
            _atomic_json(target, {"x_offset": projection["x"], "y_offset": projection["y"],
                                  "angle_offset": projection["angle"]})
    except Exception as exc:
        return f"could not recover offset projection: {exc}"
    return None


def recover_offset_projection(scm: Optional[Path] = None) -> Optional[str]:
    """Roll forward a canonical commit left ahead of its SCM projection."""
    with OFFSET_LEASE:
        if scm is None:
            scm = effective_dirs(load_settings())[0]
        return _recover_offset_projection_locked(scm)


def _busy_offset_result() -> dict:
    return {"ok": False, "errors": ["offset operations are busy; try again after the running offset job finishes"]}


def _validate_offset_domain(size: Any, x: Any, y: Any, angle: Any, *, require_size: bool = False) -> list:
    errors = []
    if require_size and _offset_name(size) is None:
        errors.append("paper size must be a non-empty valid UTF-8 name")
    if size is not None and _offset_name(size) is None:
        errors.append("paper size must be a non-empty valid UTF-8 name")
    if not isinstance(x, int) or isinstance(x, bool) or not -100000 <= x <= 100000:
        errors.append("x must be an integer from -100000 through 100000")
    if not isinstance(y, int) or isinstance(y, bool) or not -100000 <= y <= 100000:
        errors.append("y must be an integer from -100000 through 100000")
    if _finite_angle(angle) is None:
        errors.append("angle must be finite and from -360 through 360")
    return _offset_errors(errors)


def offset_set(size: Any, x: Any, y: Any, angle: Any) -> dict:
    """Set one global or named offset and atomically project its selection."""
    errors = _validate_offset_domain(size, x, y, angle)
    if errors:
        return {"ok": False, "errors": errors}
    if size is not None and size not in _known_paper_names():
        return {"ok": False, "errors": [f"unknown paper size: “{size}”"]}
    if not OFFSET_LEASE.acquire(blocking=False):
        return _busy_offset_result()
    try:
        settings = load_settings()
        scm, _ = effective_dirs(settings)
        if not scm:
            return {"ok": False, "errors": ["SCM repo not found — set it in Settings."]}
        error = _recover_offset_projection_locked(scm)
        if error:
            return {"ok": False, "errors": _offset_errors([error])}
        state = _load_offset_state_strict()
        row = {"x": x, "y": y, "angle": float(angle)}
        if size is None:
            state["global"], state["staged_size"] = row, None
            body = {"ok": True, "offset": {"x_offset": x, "y_offset": y, "angle_offset": float(angle)}}
        else:
            state["rows"][size], state["staged_size"] = row, size
            body = {"ok": True, "size": size, "staged": True,
                    "offset": {"x_offset": x, "y_offset": y, "angle_offset": float(angle)}}
        error = _commit_offset_state(state, scm)
        return body if error is None else {"ok": False, "errors": _offset_errors([error])}
    except OffsetStateError as exc:
        return {"ok": False, "errors": _offset_errors([str(exc)])}
    finally:
        OFFSET_LEASE.release()


def offset_delete(size: Any) -> dict:
    """Delete a named row; deleting the selected row restores the baseline."""
    if _offset_name(size) is None:
        return {"ok": False, "errors": ["paper size must be a non-empty valid UTF-8 name"]}
    if not OFFSET_LEASE.acquire(blocking=False):
        return _busy_offset_result()
    try:
        state = _load_offset_state_strict()
        if size not in _known_paper_names() and size not in state.get("rows", {}):
            return {"ok": False, "errors": [f"unknown paper size: “{size}”"]}
        if size not in state["rows"]:
            return {"ok": True, "removed": size}
        was_staged = state.get("staged_size") == size
        del state["rows"][size]
        if was_staged:
            state["staged_size"] = None
        # A non-selected delete changes no upstream bytes and deliberately does
        # not require an SCM checkout (useful while a managed repo is absent).
        project = was_staged
        scm = effective_dirs(load_settings())[0] if project else None
        if project:
            error = _recover_offset_projection_locked(scm)
            if error:
                return {"ok": False, "errors": _offset_errors([error])}
        error = _commit_offset_state(state, scm, project=project)
        return {"ok": True, "removed": size} if error is None else {"ok": False, "errors": _offset_errors([error])}
    except OffsetStateError as exc:
        return {"ok": False, "errors": _offset_errors([str(exc)])}
    finally:
        OFFSET_LEASE.release()


# Public names used by both transports; aliases keep the domain API pleasant
# for embedders that do not know the wire method names.
def set_offset(size: Any, x: Any, y: Any, angle: Any) -> dict:
    return offset_set(size, x, y, angle)


def delete_offset(size: Any) -> dict:
    return offset_delete(size)


def stage_per_size_offset(scm: Optional[Path], paper: Optional[str]) -> Optional[dict]:
    """Compatibility staging API; callers that need errors use the lease path."""
    if not paper or not scm:
        return None
    acquired = OFFSET_LEASE.acquire(blocking=False)
    if not acquired:
        return None
    try:
        if _recover_offset_projection_locked(scm):
            return None
        try:
            state = _load_offset_state_strict()
        except OffsetStateError:
            return None
        entry = state.get("rows", {}).get(paper)
        desired = paper if entry else None
        if state.get("staged_size") != desired:
            state["staged_size"] = desired
            if _commit_offset_state(state, scm):
                return None
        return {"size": paper, **entry} if entry else None
    finally:
        OFFSET_LEASE.release()


def effective_paper(info: dict, kind: str, args: dict, settings: dict) -> Optional[str]:
    """The paper size a job would actually print on (a specialty layout wins over the pick)."""
    if kind == "create_pdf":
        d = settings.get("defaults", {})
        paper = str(args.get("paper_size") or d.get("paper_size") or "letter")
        sp = next((s for s in info.get("scm", {}).get("specialty", []) if s.get("name") == args.get("specialty")), None)
        if sp and sp.get("paper"):
            paper = sp["paper"]
        return paper or None
    if kind == "offset_pdf":
        return str(args.get("paper_size") or "") or None
    return None


def _bootstrap_state() -> dict:
    """Live status of the launcher's first-launch preparation (flag file in
    the data area; absence = packaged-and-ready or dev checkout)."""
    try:
        f = DATA_DIR / "bootstrap.json"
        if f.is_file():
            d = json.loads(f.read_text(encoding="utf-8"))
            return {"active": bool(d.get("pending")), "phase": d.get("phase") or ""}
    except Exception:
        pass
    return {"active": False, "phase": ""}


def _bootstrapping() -> bool:
    return _bootstrap_state()["active"]


_RELEASE_NOTES_CACHE = OrderedDict()
_RELEASE_NOTES_CACHE_LOCK = threading.Lock()
_RELEASE_NOTES_CACHE_TTL = 300
_RELEASE_NOTES_CACHE_MAX = 8
_RELEASE_NOTES_SOURCE_MAX = 256 * 1024
_RELEASE_NOTES_RENDERED_MAX = 512 * 1024


def _render_release_notes(src: str) -> str:
    """Render a deliberately tiny Markdown subset; never pass source HTML through."""
    if not isinstance(src, str):
        raise updater.UpdateError("release notes are invalid")
    try:
        source_size = len(src.encode("utf-8"))
    except UnicodeEncodeError:
        raise updater.UpdateError("release notes are invalid")
    if source_size > _RELEASE_NOTES_SOURCE_MAX:
        raise updater.UpdateError("release notes are too large")
    token = re.compile(
        r"`([^`\n]{1,4096})`|\[([^\]\n]{1,4096})\]\(([^)\s]{1,2048})\)"
        r"|\*\*([^*\n]{1,4096})\*\*|(?<![\w*])\*([^*\n]{1,4096})\*(?![\w*])")

    def inline(value: str) -> str:
        out, pos = [], 0
        for match in token.finditer(value):
            out.append(html.escape(value[pos:match.start()], quote=True))
            if match.group(1) is not None:
                out.append("<code>" + html.escape(match.group(1), quote=True) + "</code>")
            elif match.group(2) is not None:
                try:
                    link = urlsplit(match.group(3))
                    valid = (link.scheme.lower() == "https" and bool(link.hostname) and
                             link.username is None and link.password is None)
                    if valid:
                        _ = link.port  # reject malformed ports
                except ValueError:
                    valid = False
                # Links are inert text.  In particular, never interpolate an
                # attacker-controlled destination into HTML attributes.
                out.append(html.escape(match.group(2), quote=True) if valid else
                           html.escape(match.group(0), quote=True))
            elif match.group(4) is not None:
                out.append("<b>" + html.escape(match.group(4), quote=True) + "</b>")
            else:
                out.append("<i>" + html.escape(match.group(5), quote=True) + "</i>")
            pos = match.end()
        out.append(html.escape(value[pos:], quote=True))
        return "".join(out)

    out, para, items, code = [], [], [], []
    fence = False

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_list():
        if items:
            out.append("<ul>" + "".join("<li>" + inline(x) + "</li>" for x in items) + "</ul>")
            items.clear()

    for raw in src.splitlines():
        line = raw.rstrip(); stripped = line.strip()
        if stripped.startswith("```"):
            if fence:
                out.append("<pre><code>" + html.escape("\n".join(code), quote=True) + "</code></pre>")
                code.clear(); fence = False
            else:
                flush_para(); flush_list(); fence = True
            continue
        if fence:
            code.append(line); continue
        if not stripped:
            flush_para(); flush_list(); continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush_para(); flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>" + inline(heading.group(2)) + f"</h{level}>")
            continue
        item = re.match(r"^[-*+]\s+(.*)$", stripped)
        if item:
            flush_para(); items.append(item.group(1)); continue
        flush_list(); para.append(stripped)
    if fence:
        out.append("<pre><code>" + html.escape("\n".join(code), quote=True) + "</code></pre>")
    flush_para(); flush_list()
    rendered = "".join(out)
    if len(rendered.encode("utf-8")) > _RELEASE_NOTES_RENDERED_MAX:
        raise updater.UpdateError("rendered release notes are too large")
    return rendered


def release_notes_view(expected_tag: Optional[str] = None) -> dict:
    """Return safe notes for the tag recorded by the completed update check."""
    st = load_update_state()
    bound_tag = st.get("latest")
    if expected_tag is not None and expected_tag != bound_tag:
        return {"ok": False, "error": "release tag is not the checked release"}
    if not isinstance(bound_tag, str) or not bound_tag:
        return {"ok": False, "error": "no release is known yet"}
    now = time.monotonic()
    with _RELEASE_NOTES_CACHE_LOCK:
        cached = _RELEASE_NOTES_CACHE.get(bound_tag)
        if cached and now - cached[0] < _RELEASE_NOTES_CACHE_TTL:
            _RELEASE_NOTES_CACHE.move_to_end(bound_tag)
            return copy.deepcopy(cached[1])
        if cached:
            _RELEASE_NOTES_CACHE.pop(bound_tag, None)

    body, url = "", st.get("release_url") or ""
    published, name = st.get("published") or "", bound_tag
    try:
        rel = updater.latest_release(timeout=15)
        if rel.get("tag") != bound_tag:
            raise updater.UpdateError("release changed while loading notes")
        body = rel.get("body") or ""
        url = rel.get("url") or url
        published = rel.get("published") or published
        name = rel.get("name") or bound_tag
    except Exception:
        body = ""  # state metadata is safe, but its fallback note body is empty
    try:
        rendered = _render_release_notes(body)
    except updater.UpdateError:
        rendered = ""
    when = ""
    if published:
        try:
            p = published.split("T"); d = p[0].split("-")
            t = p[1][:2].lstrip("0") or "0"
            when = time.strftime("%b %-d, %Y", (int(d[0]), int(d[1]), int(d[2]), int(t), 0, 0, 0, 0, 0))
        except Exception:
            when = html.escape(str(published), quote=True)
    result = {"ok": True, "tag": bound_tag, "name": name, "published": when,
              "url": url, "body": rendered}
    with _RELEASE_NOTES_CACHE_LOCK:
        _RELEASE_NOTES_CACHE[bound_tag] = (time.monotonic(), copy.deepcopy(result))
        _RELEASE_NOTES_CACHE.move_to_end(bound_tag)
        while len(_RELEASE_NOTES_CACHE) > _RELEASE_NOTES_CACHE_MAX:
            _RELEASE_NOTES_CACHE.popitem(last=False)
    return result


# Updates use a registry separate from repository metadata operations.  The
# serialized IPC reader only admits work here; network calls run on two daemon
# workers and are therefore never allowed to hold the reader open.
_UPDATE_OP_LOCK = threading.Lock()
_UPDATE_OPS = {}
_UPDATE_OP_QUEUE = queue.Queue(maxsize=16)
_UPDATE_OP_WORKERS_STARTED = False
_UPDATE_OP_ACTIVE_MAX = 16
_UPDATE_OP_TOTAL_MAX = 32
_UPDATE_OP_RESULT_MAX = 1024 * 1024
_UPDATE_OP_ERROR_MAX = 256
_UPDATE_OP_TTL = 300


def updates_view() -> dict:
    """The shared, synchronous GET /api/updates representation.

    ``checking`` is deliberately a response-only projection.  The persisted
    update-state schema remains strict and never receives this transient key.
    Snapshot operation activity before reading state so this helper cannot
    participate in a registry/state lock inversion.
    """
    with _UPDATE_OP_LOCK:
        checking = any(op.get("kind") == "check" and
                       op.get("status") in ("running", "queued")
                       for op in _UPDATE_OPS.values())
    state = copy.deepcopy(load_update_state())
    state["checking"] = checking
    return {"current": SERVER_VERSION, "repo": updater.UPDATE_REPO,
            "packaged": os.environ.get("SCM_WORKBENCH_PACKAGED") == "1",
            "bundle": os.environ.get("SCM_WORKBENCH_BUNDLE") or "",
            "state": state}


def _update_check_result(force: bool) -> dict:
    """Return the browser-compatible final body for an update check."""
    if not force:
        st = load_update_state()
        if st.get("status") in ("up-to-date", "update-available") and st.get("checked_at") is not None:
            try:
                age = time.time() - float(st["checked_at"])
            except Exception:
                age = None
            if age is not None and age < UPDATE_CHECK_INTERVAL:
                cached = copy.deepcopy(st)
                cached["cached"] = True
                return {"ok": True, "state": cached}
    return {"ok": True, "state": run_update_check()}


def _checked_update_tag(tag: str) -> bool:
    st = load_update_state()
    return bool(_valid_update_state(st) and st.get("status") in ("up-to-date", "update-available")
                and st.get("checked_at") is not None and st.get("latest") == tag)


def _update_notes_result(tag: str) -> dict:
    """Load notes only while the canonical checked release is still `tag`."""
    if not _checked_update_tag(tag):
        return {"ok": False, "error": "release tag is not the checked release"}
    try:
        result = release_notes_view(expected_tag=tag)
    except Exception:
        return {"ok": False, "errors": ["operation failed"]}
    # A check can publish a newer canonical release while notes are fetched.
    # Never return the body from the old release in that case.
    if not _checked_update_tag(tag):
        return {"ok": False, "error": "release tag is not the checked release"}
    return result


def _bounded_update_errors(value: Any) -> list:
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for item in list(value)[:8]:
        text = " ".join(str(item or "operation failed").split())
        out.append(text[:_UPDATE_OP_ERROR_MAX] or "operation failed")
    return out or ["operation failed"]


def _update_result_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _prune_update_operations_locked(now=None):
    now = time.monotonic() if now is None else now
    for operation_id, operation in list(_UPDATE_OPS.items()):
        if operation.get("status") == "done" and now - operation.get("ended", now) >= _UPDATE_OP_TTL:
            _UPDATE_OPS.pop(operation_id, None)


def _update_operation_worker():
    while True:
        operation_id = _UPDATE_OP_QUEUE.get()
        try:
            with _UPDATE_OP_LOCK:
                operation = _UPDATE_OPS.get(operation_id)
            if operation is None:
                continue
            try:
                result = operation["call"]()
                if not isinstance(result, dict):
                    result = {"ok": False, "errors": ["operation returned an invalid result"]}
                if "errors" in result:
                    result["errors"] = _bounded_update_errors(result["errors"])
                if _update_result_size(result) > _UPDATE_OP_RESULT_MAX:
                    result = {"ok": False, "errors": ["operation result exceeds 1 MiB"]}
            except Exception:
                result = {"ok": False, "errors": ["operation failed"]}
            with _UPDATE_OP_LOCK:
                current = _UPDATE_OPS.get(operation_id)
                if current is not None:
                    current["status"] = "done"
                    current["result"] = result
                    current["ended"] = time.monotonic()
        finally:
            _UPDATE_OP_QUEUE.task_done()


def _ensure_update_operation_workers_locked():
    global _UPDATE_OP_WORKERS_STARTED
    if _UPDATE_OP_WORKERS_STARTED:
        return
    _UPDATE_OP_WORKERS_STARTED = True
    for number in range(2):
        threading.Thread(target=_update_operation_worker, daemon=True,
                         name=f"update-operation-{number + 1}").start()


def _start_update_operation(kind: str, args: dict) -> dict:
    with _UPDATE_OP_LOCK:
        _prune_update_operations_locked()
        active = sum(op.get("status") == "running" for op in _UPDATE_OPS.values())
        if active >= _UPDATE_OP_ACTIVE_MAX or len(_UPDATE_OPS) >= _UPDATE_OP_TOTAL_MAX:
            return {"ok": False, "errors": ["too many update operations"]}
        operation_id = secrets.token_hex(16)
        while operation_id in _UPDATE_OPS:
            operation_id = secrets.token_hex(16)
        if kind == "check":
            call = lambda: _update_check_result(args["force"])
        elif kind == "notes":
            # Recheck while holding the registry admission lock: validation in
            # the IPC adapter can race a newly published update state.
            if not _checked_update_tag(args["tag"]):
                return {"ok": False, "errors": ["release tag is not the checked release"]}
            call = lambda: _update_notes_result(args["tag"])
        else:
            return {"ok": False, "errors": ["unknown update operation"]}
        now = time.monotonic()
        _UPDATE_OPS[operation_id] = {
            "kind": kind, "status": "running", "call": call, "result": None,
            "started": now, "ended": None,
        }
        _ensure_update_operation_workers_locked()
        try:
            _UPDATE_OP_QUEUE.put_nowait(operation_id)
        except queue.Full:
            _UPDATE_OPS.pop(operation_id, None)
            return {"ok": False, "errors": ["too many update operations"]}
    return {"ok": True, "operation": {"id": operation_id, "status": "running"}}


def poll_update_operation(operation_id: str) -> dict:
    with _UPDATE_OP_LOCK:
        _prune_update_operations_locked()
        operation = _UPDATE_OPS.get(operation_id)
        if operation is None:
            return {"ok": False, "error": {"code": "bad_request", "message": "operation not found"}}
        if operation["status"] == "running":
            return {"ok": True, "status": "running"}
        return {"ok": True, "status": "done", "result": copy.deepcopy(operation["result"])}


def update_start_result() -> dict:
    job, errors = start_update_job()
    if errors:
        return {"ok": False, "errors": _bounded_update_errors(errors)}
    return {"ok": True, "job": {
        "id": job["id"], "title": job["title"], "status": job["status"],
        "cmd": job["cmd"],
    }}


def get_info() -> dict:

    settings = load_settings()
    scm, extras = effective_dirs(settings)
    scm_info = read_scm_info(scm, extras)
    # Once offset state exists, its baseline is authoritative even while a
    # per-paper row is projected into SCM's single shared file.
    canonical_exists = _offset_state_path().exists() or _offset_state_path().is_symlink()
    try:
        offset_state = load_offset_state()
    except OffsetStateError:
        offset_state = {"global": None, "rows": {}, "staged_size": None}
    if canonical_exists:
        # None is meaningful: a per-size-only state must not expose its staged
        # projection as the global baseline.
        scm_info["saved_offset"] = offset_state.get("global")
    elif offset_state.get("global") is not None:
        scm_info["saved_offset"] = offset_state["global"]
    return {
        "server": {
            "version": SERVER_VERSION,
            "python": sys.version.split()[0],
            "python_path": str(Path(sys.executable).resolve()),
            "platform": sys.platform,
            "is_windows": os.name == "nt",
            "is_packaged": os.environ.get("SCM_WORKBENCH_PACKAGED") == "1",
            **_bootstrap_state(),
            "runtime_ready": bool(os.environ.get("SCM_WORKBENCH_PYTHON")),
            "data_dir": str(DATA_DIR),
        },
        # the window host's state, when it is not the app's own window
        # (browser fallback): <data>/window.json, written by the launcher
        "window": _try_read_json(DATA_DIR / "window.json") or {},
        "scm": scm_info,
        "extras": read_extras_info(extras),
        "per_size_offsets": load_per_size_offsets(),
        "repos": repos_view(settings),
        "settings": settings,
    }


def _repos_signal_mtime() -> float:
    now = 0.0
    for p in (repo_sync.state_file(), DATA_DIR / "repos-manifest-scm.json"):
        try:
            now = max(now, p.stat().st_mtime)
        except OSError:
            pass
    try:
        scm, _ = effective_dirs(load_settings())
        if scm:
            dl = scm / "game" / "decklist"
            try:
                now = max(now, dl.stat().st_mtime)
            except OSError:
                pass
    except Exception:
        pass
    return now


def extras_card_names(info: dict) -> set:
    names = set()
    for c in info.get("extras", {}).get("card_sizes", []):
        names.add(c["name"].lower())
        for a in c.get("aliases", []):
            names.add(a.lower())
    return names


# ============================================================================
# Jobs
# ============================================================================

JOBS: Dict[str, dict] = {}
# Re-entrant because the handoff commits its durable job record while holding
# the same admission lock that ordinary jobs use.
JOBS_LOCK = threading.RLock()


def _line_wire(line: Any) -> Tuple[str, bool]:
    """Return a UTF-8 bounded line and whether it had to be clipped."""
    text = str(line)
    raw = text.encode("utf-8", "replace")
    if len(raw) <= JOB_LINE_MAX_BYTES:
        return text, False
    # Decode a byte prefix without splitting a code point and make the loss
    # visible to native callers rather than silently changing a transcript.
    marker = "… [line truncated]"
    room = max(1, JOB_LINE_MAX_BYTES - len(marker.encode("utf-8")))
    clipped = raw[:room].decode("utf-8", "ignore")
    return clipped + marker, True


def _job_lines_locked(job: dict) -> Tuple[int, List[str]]:
    """Copy transcript state while JOBS_LOCK is already held."""
    lines = list(job.get("log_lines") or [])
    return int(job.get("first_seq", 0) or 0), lines


def _job_lines_snapshot(job: dict) -> Tuple[int, List[str]]:
    """Copy transcript state without exposing a mutating list to readers."""
    with JOBS_LOCK:
        return _job_lines_locked(job)


def _append_job_line(job: dict, line: Any, *, log_f=None) -> int:
    """Append a complete line and wake subscribers without ever blocking."""
    text = str(line)
    if log_f is not None:
        log_f.write(text + "\n")
        log_f.flush()
    with JOBS_LOCK:
        lines = job.setdefault("log_lines", [])
        first = int(job.get("first_seq", 0) or 0)
        seq = first + len(lines)
        lines.append(text)
        subscribers = list(job.get("subs", []))
    _notify_subscribers(job, subscribers, ("line", seq, text))
    return seq


def _notify_subscribers(job: dict, subscribers: list, message: tuple, *, terminal: bool = False) -> None:
    """Non-blocking subscriber wakeups; a slow SSE client gets a replay gap."""
    for q in subscribers:
        try:
            q.put_nowait(message)
        except queue.Full:
            # Drop queued wakes, not the transcript.  The consumer will replay
            # from log_lines after the explicit gap marker.  A terminal marker
            # is forced in below so completion can never be lost.
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(("gap",))
            except queue.Full:
                pass
            if terminal:
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(message)
                except queue.Full:
                    pass


def _persist_jobs(*, strict: bool = False, finalized: Optional[list] = None) -> bool:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        rows = sorted(JOBS.values(), key=lambda j: j.get("ts", 0), reverse=True)[:100]
    def slim_row(j: dict) -> dict:
        return ({k: j[k] for k in ("id", "ts", "kind", "title", "cmd", "args", "status", "exit_code", "log_file", "duration", "scm_path", "artifact_snapshots")
                 if k in j}
                | {k: j[k] for k in ("update_token", "expected_version", "result_message") if k in j})

    slim = [
        slim_row(j) for j in rows
        if (j["status"] != "running" and j.get("duration") is not None)
        or (j.get("kind") == "update" and j.get("status") == "handoff")
    ]
    # A freshly launched worker may have no live JOBS entry.  Preserve its
    # finalized persisted row explicitly, while giving that row precedence by
    # ID and retaining every unrelated live job in the same atomic merge.
    if finalized:
        final_rows = [slim_row(j) for j in finalized if isinstance(j, dict)]
        final_ids = {j.get("id") for j in final_rows}
        slim = [j for j in slim if j.get("id") not in final_ids] + final_rows
    # Merge with rows persisted by earlier sessions: the in-memory map only
    # knows about *this* process's jobs, and rewriting the file from it alone
    # would silently erase the user's job history on every relaunch.
    try:
        old = _try_read_json(JOBS_FILE) or []
    except Exception:
        old = []
    ids = {s["id"] for s in slim}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if JOBS_FILE.is_symlink():
            if strict:
                raise OSError("refusing to replace symlinked jobs file")
            return False
        fd, tmp_name = tempfile.mkstemp(prefix=f".{JOBS_FILE.name}.", dir=str(DATA_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump((slim + [o for o in old if o.get("id") not in ids])[:100], f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, JOBS_FILE)
            try:
                dir_fd = os.open(DATA_DIR, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return True
    except Exception:
        if strict:
            raise
        return False


def read_persisted_jobs() -> list:
    data = _try_read_json(JOBS_FILE)
    return data or []


def _update_result_record(path: Path) -> Optional[dict]:
    """Read the helper's one-shot result without following links."""
    try:
        meta = os.lstat(path)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > 64 * 1024:
            return None
        with open(path, "rb") as f:
            raw = f.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return None
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"version", "token", "success", "expected_version", "message"}:
            return None
        token = value["token"]
        expected = value["expected_version"]
        message = value["message"]
        if (value["version"] != 1 or not isinstance(token, str) or
                not re.fullmatch(r"[0-9a-f]{64}", token) or
                not isinstance(value["success"], bool) or
                not isinstance(expected, str) or not expected or len(expected.encode()) > 128 or
                updater.canonical_version(expected) != expected or
                any(ord(c) < 0x20 or ord(c) == 0x7f for c in expected) or
                not isinstance(message, str) or not message or len(message.encode()) > 256 or
                any(ord(c) < 0x20 or ord(c) == 0x7f for c in message)):
            return None
        return value
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def reconcile_update_result() -> bool:
    """Durably consume a matching helper result, if one is present.

    The new shell has the token in its environment.  A rollback relaunch is
    intentionally scrubbed of that environment, so the persisted handoff job
    is the fallback lookup.  Invalid or stale records are left untouched.
    """
    global _UPDATE_QUIESCING, _UPDATE_QUIESCING_JOB
    result_path = DATA_DIR / ".update-result.json"
    result = _update_result_record(result_path)
    if result is None:
        return False
    env_token = os.environ.get("SCM_WORKBENCH_UPDATE_TOKEN")
    with JOBS_LOCK:
        rows = list(JOBS.values()) + read_persisted_jobs()
        matches = [j for j in rows if j.get("kind") == "update" and
                   j.get("status") == "handoff" and
                   j.get("update_token") == result["token"] and
                   j.get("expected_version") == result["expected_version"]]
        if env_token and env_token != result["token"]:
            matches = []
        current_version = updater.canonical_version(SERVER_VERSION)
        if result["success"] and (current_version is None or
                                   result["expected_version"] != current_version):
            matches = []
        if not matches:
            return False
        now = time.time()
        for job in matches:
            job["status"] = "ok" if result["success"] else "fail"
            job["exit_code"] = 0 if result["success"] else 1
            job["ended"] = now
            job["duration"] = round(max(0.0, now - float(job.get("started", now))), 2)
            job["result_message"] = result["message"]
        if result["success"]:
            state = _default_update_state()
            state.update(status="up-to-date", current=SERVER_VERSION,
                         latest=result["expected_version"], checked_at=now)
        else:
            state = _default_update_state()
            state.update(status="error", current=SERVER_VERSION, checked_at=now,
                         reason=result["message"])
        try:
            save_update_state(state)
            _persist_jobs(strict=True, finalized=matches)
        except Exception:
            return False
        expected_status = "ok" if result["success"] else "fail"
        durable = next((j for j in read_persisted_jobs()
                        if j.get("id") == matches[0].get("id") and
                        j.get("status") == expected_status and
                        j.get("update_token") == result["token"] and
                        j.get("expected_version") == result["expected_version"]), None)
        if durable is None:
            return False
        # Removing the marker is deliberately last: a crash before this point
        # replays an idempotent terminalization on the next launch. The
        # admission fence can be released once durable terminal state is
        # verified, even if unlink itself is temporarily unavailable.
        _UPDATE_QUIESCING = False
        _UPDATE_QUIESCING_JOB = None
        try:
            os.unlink(result_path)
            try:
                fd = os.open(DATA_DIR, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        except OSError:
            return False
        return True


def list_jobs() -> dict:
    """The one authoritative shape used by HTTP and native callers."""
    with JOBS_LOCK:
        live = [dict(j) for j in sorted(JOBS.values(), key=lambda x: x["ts"], reverse=True)[:50]]
    running = []
    for j in live:
        row = {"id": j["id"], "ts": j["ts"], "kind": j["kind"], "title": j["title"],
               "status": j["status"], "exit_code": j.get("exit_code"), "cmd": j["cmd"]}
        if j.get("progress"):
            row["progress"] = j["progress"]
        row.update(warnings=j.get("warnings", []), outputs=job_outputs(j),
                   save_grants=_grants_for_job(j))
        running.append(row)
    ids = {r["id"] for r in running}
    history = []
    for old in read_persisted_jobs():
        if old.get("id") in ids:
            continue
        row = dict(old)
        row.setdefault("outputs", job_outputs(row))
        # Persisted immutable snapshots mint fresh process-local grants after
        # restart; historical rows without snapshots remain display-only.
        row["save_grants"] = _grants_for_job(row)
        history.append(row)
    return {"jobs": running + history[:200]}


def _job_record(job_id: str) -> Optional[dict]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return None
        snapshot = dict(job)
        snapshot["log_lines"] = list(job.get("log_lines") or [])
        return snapshot


def _persisted_record(job_id: str) -> Optional[dict]:
    return next((dict(j) for j in read_persisted_jobs() if j.get("id") == job_id), None)


def get_job_log(job_id: str, after: int = 0, max_lines: int = JOB_LOG_MAX_LINES,
                *, byte_limit: int = IPC_POLL_MAX_BYTES) -> dict:
    """Read a bounded cursor window shared by HTTP and native IPC."""
    job = _job_record(job_id) or _persisted_record(job_id)
    if not job:
        return {"lines": [], "status": "missing", "exit_code": None, "cmd": "",
                "first_seq": 0, "next_seq": 0, "truncated": False, "gap": False}
    first = int(job.get("first_seq", 0) or 0)
    lines = list(job.get("log_lines") or [])
    if not lines and job.get("log_file"):
        try:
            with open(job["log_file"], encoding="utf-8", errors="replace") as f:
                lines = [line.rstrip("\r\n") for line in f]
        except OSError:
            pass
    available_next = first + len(lines)
    requested = max(0, int(after))
    start = max(requested, first)
    truncated = requested < first
    selected = lines[start - first:]
    if len(selected) > max_lines:
        selected = selected[:max_lines]
        truncated = True
    wire, clipped = [], False
    # Add lines incrementally so a hostile transcript never causes an
    # quadratic serialize-and-pop loop. The one-byte list comma is included.
    base_size = len(json.dumps({"lines": [], "status": job.get("status", "missing"),
                                "exit_code": job.get("exit_code"), "cmd": job.get("cmd", ""),
                                "first_seq": first, "next_seq": start,
                                "truncated": False, "gap": bool(requested < first),
                                "line_truncated": False}, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"))
    used = base_size
    for line in selected:
        bounded, was_clipped = _line_wire(line)
        item_size = len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) + (1 if wire else 0)
        if wire and used + item_size > byte_limit:
            truncated = True
            break
        wire.append(bounded)
        used += item_size
        clipped = clipped or was_clipped
    next_seq = max(start + len(wire), min(requested, available_next))
    return {"lines": wire, "status": job.get("status", "missing"),
            "exit_code": job.get("exit_code"), "cmd": job.get("cmd", ""),
            "first_seq": first, "next_seq": next_seq,
            "truncated": bool(truncated or clipped or len(wire) < len(selected)),
            "gap": bool(requested < first),
            "line_truncated": bool(clipped)}


def poll_jobs(cursors: list, max_events: int) -> dict:
    """Return independent bounded windows for native job polling."""
    result = []
    # Keep a little room for the IPC envelope. This is the complete frame
    # budget for the result, not a decrementing per-row budget.
    frame_budget = IPC_POLL_MAX_BYTES - 64 * 1024

    def encoded_size(rows: list) -> int:
        return len(json.dumps({"jobs": rows}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))

    for index, cursor in enumerate(cursors):
        jid, after = cursor["job_id"], cursor["after"]
        # Allocate the remaining frame budget fairly before reading any lines;
        # each helper also stops incrementally at this per-job byte budget.
        remaining = max(1, len(cursors) - index)
        used = encoded_size(result)
        remaining_budget = max(0, frame_budget - used)
        per_cursor = max(4096, remaining_budget // remaining)
        job = _job_record(jid) or _persisted_record(jid)
        if not job:
            row = {"job_id": jid, "lines": [], "next_seq": after,
                   "status": "missing", "exit_code": None, "cmd": "",
                   "complete": True, "truncated": False, "gap": False}
            result.append(row)
            continue
        log = get_job_log(jid, after, max_events, byte_limit=per_cursor)
        # get_job_log returns strings; native batches retain the established
        # SSE indexes and independently advance each cursor.
        first = log["first_seq"]
        indexed = [{"i": first + max(0, after - first) + i, "s": line}
                   for i, line in enumerate(log["lines"])]
        row = {"job_id": jid, "lines": indexed, "next_seq": log["next_seq"],
               "status": log["status"], "exit_code": log["exit_code"],
               "cmd": _line_wire(log["cmd"])[0], "complete": log["status"] != "running",
               "truncated": log["truncated"], "gap": log["gap"]}
        # A long line or many jobs can otherwise make max_events exceed the
        # frame budget. Remove tail events (never another job's events) and
        # report the resulting gap through truncated while preserving cursor.
        while indexed and encoded_size(result + [row]) > frame_budget:
            indexed.pop()
            row["lines"] = indexed
            row["truncated"] = True
            row["next_seq"] = max(first + len(indexed), min(after, log["next_seq"]))
        result.append(row)
    return {"jobs": result}


def _artifact_identity(st: os.stat_result) -> dict:
    return {"dev": int(getattr(st, "st_dev", 0)), "ino": int(getattr(st, "st_ino", 0)),
            "size": int(st.st_size), "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
            "ctime_ns": int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9)))}


def _artifact_name(value: str) -> str:
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ArtifactExportError("artifact name is invalid")
    try:
        if len(value.encode("utf-8")) > ARTIFACT_NAME_MAX_BYTES:
            raise ArtifactExportError("artifact name exceeds 255 UTF-8 bytes")
    except UnicodeEncodeError as exc:
        raise ArtifactExportError("artifact name must be valid UTF-8") from exc
    if any(ord(c) < 0x20 or ord(c) == 0x7f or unicodedata.category(c) == "Cc" for c in value):
        raise ArtifactExportError("artifact name contains control characters")
    if any(c in value for c in ("/", "\\", ":")):
        raise ArtifactExportError("artifact name must not contain path separators")
    # Keep the destination contract portable even when the worker is running
    # on POSIX and the selected path will later be used on Windows.
    stem = value.rstrip(" .").split(".", 1)[0].upper()
    if value != value.rstrip(" .") or not value.strip() or stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem or ""):
        raise ArtifactExportError("artifact name is not portable")
    return value


def _artifact_path_parts(path: Path) -> list:
    if not path.is_absolute():
        raise ArtifactExportError("artifact path must be absolute")
    try:
        raw = str(path)
        if len(raw.encode("utf-8")) > ARTIFACT_PATH_MAX_BYTES:
            raise ArtifactExportError("artifact path exceeds 4096 UTF-8 bytes")
    except UnicodeEncodeError as exc:
        raise ArtifactExportError("artifact path must be valid UTF-8") from exc
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw):
        raise ArtifactExportError("artifact path contains control characters")
    parts = list(path.parts)
    if any(part in ("", ".", "..") for part in parts[1:]):
        raise ArtifactExportError("artifact path contains an unsafe component")
    return parts


def _artifact_inside(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
        return bool(relative.parts) and all(part not in ("", ".", "..")
                                            for part in relative.parts)
    except ValueError:
        return False


def _artifact_expected_paths(job: dict) -> list:
    """Compute only the output families owned by a terminal artifact job."""
    kind, args = job.get("kind"), job.get("args") or {}
    root = Path(str(job.get("scm_path") or ""))
    if not root.is_absolute() or kind not in ("create_pdf", "offset_pdf", "calibration"):
        return []
    if kind == "calibration":
        directory = root / "calibration"
        try:
            found = []
            for p in directory.iterdir():
                if p.name.lower().endswith(".pdf"):
                    found.append(p)
                    if len(found) >= 64:
                        break
            return found
        except OSError:
            return []
    if kind == "create_pdf":
        if args.get("output_images"):
            return []
        raw = str(args.get("output_path") or "game/output/game.pdf")
        return [Path(raw) if Path(raw).is_absolute() else root / raw]
    raw = str(args.get("output_pdf_path") or "")
    src = Path(str(args.get("pdf_path") or "game/output/game.pdf"))
    src = src if src.is_absolute() else root / src
    return [Path(raw) if raw and Path(raw).is_absolute() else (root / raw if raw else src.with_name(src.stem + "_offset.pdf"))]


def _snapshot_artifacts(job: dict) -> list:
    if job.get("status") != "ok":
        return []
    raw_root = Path(str(job.get("scm_path") or ""))
    root = raw_root
    try:
        if not raw_root.is_absolute() or _is_reparse_or_symlink(os.lstat(raw_root)):
            return []
        root = raw_root.resolve(strict=True)
        if len(str(root).encode("utf-8")) > ARTIFACT_PATH_MAX_BYTES:
            return []
        root_stat = os.stat(root, follow_symlinks=False)
        if not root.is_dir() or _is_reparse_or_symlink(os.lstat(root)) or not stat.S_ISDIR(root_stat.st_mode):
            return []
    except OSError:
        return []
    result = []
    for expected_path in _artifact_expected_paths(job):
        try:
            # Rebase the job's lexical path onto the canonical pinned root
            # before inspecting every component. Resolving the candidate first
            # would erase evidence that an expected artifact was a symlink.
            relative = expected_path.relative_to(raw_root)
            if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
                continue
            candidate = root.joinpath(*relative.parts)
            if len(str(candidate).encode("utf-8")) > ARTIFACT_PATH_MAX_BYTES:
                continue
            if not _artifact_inside(root, candidate) or candidate.suffix.lower() != ".pdf":
                continue
            probe = root
            safe = True
            for part in relative.parts:
                probe /= part
                component_stat = os.lstat(probe)
                if _is_reparse_or_symlink(component_stat):
                    safe = False
                    break
            if not safe:
                continue
            if os.name == "nt":
                # Opening the handle first makes NTFS finalize a newly-created
                # file's change timestamp before the immutable snapshot is
                # recorded. A path stat immediately after the producer closes
                # can otherwise carry a transient ctime that changes on the
                # grant's first verification open.
                snapshot_fd, st = _open_windows_regular_file(candidate)
                os.close(snapshot_fd)
            else:
                st = os.stat(candidate, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_size < 0 or st.st_size > ARTIFACT_MAX_BYTES:
                continue
            identity = _artifact_identity(st)
            before = (job.get("artifact_before") or {}).get(str(candidate))
            if before is not None and before == identity:
                continue
            result.append({"path": str(candidate), "root": str(root), "name": _artifact_name(candidate.name),
                           "root_identity": _artifact_identity(root_stat), **identity})
        except (OSError, ValueError, ArtifactExportError):
            continue
    return result[:16]


def job_outputs(job: dict) -> list:
    """Return snapshotted outputs, never outputs from current Settings."""
    snapshots = job.get("artifact_snapshots")
    if isinstance(snapshots, list):
        return [str(x.get("path")) for x in snapshots if isinstance(x, dict) and isinstance(x.get("path"), str)]
    kind = job.get("kind")
    if kind not in ("create_pdf", "offset_pdf", "calibration"):
        return []
    args = job.get("args") or {}
    scm = Path(str(job.get("scm_path") or ""))
    # Historical rows without a pinned checkout are display-only and must not
    # be redirected through current Settings.
    if not scm.is_absolute():
        return []
    if kind == "create_pdf":
        if args.get("output_images"):
            return []
        p = Path(str(args.get("output_path") or "game/output/game.pdf"))
        return [str(p if p.is_absolute() else scm / p)]
    if kind == "offset_pdf":
        src = Path(str(args.get("pdf_path") or "game/output/game.pdf"))
        if not src.is_absolute():
            src = scm / src
        out = str(args.get("output_pdf_path") or "")
        if not out:
            out = str(src.with_name(src.stem + "_offset.pdf"))
        p = Path(out)
        return [str(p if p.is_absolute() else scm / p)]
    cdir = scm / "calibration"
    return [str(p) for p in sorted(cdir.glob("*.pdf"))] if cdir.is_dir() else []


# Grants and exports are process-local by design.  Persisted snapshots survive
# restart for audit/display, but a new worker must not inherit a usable grant.
ARTIFACT_GRANTS: "OrderedDict[str, dict]" = OrderedDict()
ARTIFACT_GRANTS_LOCK = threading.RLock()
EXPORTS: "OrderedDict[str, dict]" = OrderedDict()
EXPORTS_LOCK = threading.RLock()
EXPORT_EXECUTOR = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(max_workers=2, thread_name_prefix="artifact-export")


def _grant_error(message: str) -> dict:
    return {"ok": False, "errors": [" ".join(str(message).split())[:256]]}


def _artifact_snapshot_bound_to_job(snapshot: dict, job: dict) -> bool:
    """A persisted snapshot may name only its job's immutable SCM checkout."""
    try:
        raw_root = Path(str(job.get("scm_path") or ""))
        if not raw_root.is_absolute() or _is_reparse_or_symlink(os.lstat(raw_root)):
            return False
        job_root = raw_root.resolve(strict=True)
        snapshot_root = Path(str(snapshot.get("root") or ""))
        return snapshot_root == job_root
    except (OSError, ValueError):
        return False


def _grants_for_job(job: dict) -> Optional[list]:
    snapshots = job.get("artifact_snapshots")
    if (job.get("status") != "ok" or
            job.get("kind") not in ("create_pdf", "offset_pdf", "calibration") or
            not isinstance(snapshots, list) or not snapshots):
        return None

    # Once newer rows have filled the bounded registry, avoid filesystem work
    # for older history entries which cannot receive grants in this response.
    with ARTIFACT_GRANTS_LOCK:
        has_existing = any(grant.get("job_id") == job.get("id")
                           for grant in ARTIFACT_GRANTS.values())
        if not has_existing and len(ARTIFACT_GRANTS) >= ARTIFACT_GRANT_MAX:
            return None

    # Revalidate each immutable snapshot independently. A later calibration
    # file must not invalidate an earlier job, while a rename, symlink swap, or
    # mutation of that job's actual artifact must.
    checked = []
    for snapshot in snapshots[:16]:
        if (not isinstance(snapshot, dict) or
                not _artifact_snapshot_bound_to_job(snapshot, job)):
            return None
        try:
            fd, current_stat = _open_artifact_source(snapshot)
            os.close(fd)
        except (ArtifactExportError, OSError):
            return None
        expected = {key: snapshot.get(key) for key in
                    ("dev", "ino", "size", "mtime_ns", "ctime_ns")}
        if _artifact_identity(current_stat) != expected:
            return None
        checked.append(snapshot)

    now = time.time()
    job_id = job.get("id")
    with ARTIFACT_GRANTS_LOCK:
        # Process-local memoization is keyed by immutable snapshot contents,
        # not by the copied row returned from list_jobs().
        existing = []
        for grant_id, grant in list(ARTIFACT_GRANTS.items()):
            if grant.get("expires", 0) <= now:
                ARTIFACT_GRANTS.pop(grant_id, None)
                continue
            if grant.get("job_id") == job_id:
                existing.append((grant_id, grant))
        if len(existing) == len(checked) and all(
                grant.get("snapshot") == snapshot
                for (_, grant), snapshot in zip(existing, checked)):
            return [grant_id for grant_id, _ in existing]
        for grant_id, _ in existing:
            ARTIFACT_GRANTS.pop(grant_id, None)
        # Do not evict grants already returned for newer jobs in this same
        # jobs.list response. Older jobs simply have no native grant until
        # capacity becomes available.
        if len(ARTIFACT_GRANTS) + len(checked) > ARTIFACT_GRANT_MAX:
            return None
        ids = []
        for snapshot in checked:
            grant_id = secrets.token_hex(32)
            ARTIFACT_GRANTS[grant_id] = {
                "job_id": job_id,
                "terminal": True,
                "snapshot": dict(snapshot),
                "expires": now + ARTIFACT_GRANT_TTL,
                "busy": False,
            }
            ids.append(grant_id)
        return ids or None


def _expire_artifacts() -> None:
    now = time.time()
    with ARTIFACT_GRANTS_LOCK:
        for key in list(ARTIFACT_GRANTS):
            if ARTIFACT_GRANTS[key].get("expires", 0) <= now:
                ARTIFACT_GRANTS.pop(key, None)
    with EXPORTS_LOCK:
        for key, op in list(EXPORTS.items()):
            if op.get("expires", 0) <= now and op.get("done"):
                EXPORTS.pop(key, None)


def _open_windows_regular_file(path: Path) -> Tuple[int, os.stat_result]:
    """Open one Windows file handle without following a final reparse point."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x00000007, None, 3,
                                  0x00200000, None)
    raw_handle = getattr(handle, "value", handle)
    if raw_handle == ctypes.c_void_p(-1).value:
        raise ArtifactExportError("artifact source could not be opened")
    info = _ByHandleFileInformation()
    if (not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info))
            or info.dwFileAttributes & 0x400):
        kernel32.CloseHandle(handle)
        raise ArtifactExportError("artifact source is a reparse point or unavailable")
    try:
        fd = msvcrt.open_osfhandle(raw_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception as exc:
        kernel32.CloseHandle(handle)
        raise ArtifactExportError("artifact source could not be opened") from exc
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if _is_reparse_or_symlink(st) or not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise ArtifactExportError("artifact source is not a stable regular file")
    return fd, st


def _validate_windows_artifact_components(root: Path, parts: tuple, snapshot: dict) -> None:
    """Validate the pinned root and each Windows component before CreateFileW."""
    try:
        root_stat = os.stat(root, follow_symlinks=False)
        if (_is_reparse_or_symlink(os.lstat(root)) or
                not stat.S_ISDIR(root_stat.st_mode) or
                (snapshot.get("root_identity") and
                 _artifact_identity(root_stat) != snapshot.get("root_identity"))):
            raise ArtifactExportError("artifact root changed")
        probe = root
        for part in parts:
            probe /= part
            if _is_reparse_or_symlink(os.lstat(probe)):
                raise ArtifactExportError("artifact source contains a reparse point")
    except OSError as exc:
        raise ArtifactExportError("artifact source is unavailable") from exc


def _open_artifact_source(snapshot: dict) -> Tuple[int, os.stat_result]:
    root = Path(str(snapshot.get("root") or ""))
    source = Path(str(snapshot.get("path") or ""))
    if not root.is_absolute() or not source.is_absolute() or not _artifact_inside(root, source):
        raise ArtifactExportError("artifact grant is invalid")
    relative = source.relative_to(root)
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ArtifactExportError("artifact source is invalid")

    if os.name == "nt":
        # Python has no dir_fd traversal on Windows. Check the pinned root and
        # every lexical component for reparse points, then use one stable file
        # handle; the immutable identity check rejects a concurrent swap.
        _validate_windows_artifact_components(root, parts, snapshot)
        return _open_windows_regular_file(source)

    # Open every component below the pinned root without following links.
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    fd = root_fd
    try:
        root_stat = os.fstat(root_fd)
        if (snapshot.get("root_identity") and
                _artifact_identity(root_stat) != snapshot.get("root_identity")):
            raise ArtifactExportError("artifact root changed")
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ArtifactExportError("artifact source is not a regular file")
        return fd, st
    except Exception:
        os.close(fd)
        raise


def _open_windows_artifact_parent(parent: Path) -> list:
    """Pin every Windows destination component against rename/reparse races."""
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handles = []
    try:
        parts = parent.parts
        if not parts:
            raise ArtifactExportError("destination parent is invalid")
        current = Path(parts[0])
        for index, part in enumerate(parts[1:]):
            current /= part
            # DELETE access on the final directory makes the omitted
            # FILE_SHARE_DELETE meaningful on current Windows: an
            # attributes-only handle does not prevent the directory rename.
            desired_access = 0x00010080 if index == len(parts[1:]) - 1 else 0x00000080
            handle = kernel32.CreateFileW(
                str(current),
                desired_access,  # FILE_READ_ATTRIBUTES, plus DELETE on parent
                0x00000001 | 0x00000002,  # share read/write, deliberately not delete
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            raw_handle = getattr(handle, "value", handle)
            if raw_handle == ctypes.c_void_p(-1).value:
                raise ArtifactExportError("destination parent could not be pinned")
            info = _ByHandleFileInformation()
            if (not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)) or
                    info.dwFileAttributes & 0x400 or
                    not info.dwFileAttributes & 0x10):
                kernel32.CloseHandle(handle)
                raise ArtifactExportError("destination parent contains a reparse point")
            handles.append(handle)
        return handles
    except Exception:
        for handle in reversed(handles):
            kernel32.CloseHandle(handle)
        raise


def _close_windows_handles(handles: list) -> None:
    if not handles:
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    for handle in reversed(handles):
        kernel32.CloseHandle(handle)


def _open_artifact_parent(destination: Path) -> Tuple[int, str, list]:
    _artifact_path_parts(destination)
    if destination.name != _artifact_name(destination.name):
        raise ArtifactExportError("destination name is invalid")
    parent = destination.parent
    if not parent.is_absolute():
        raise ArtifactExportError("destination parent is invalid")
    if os.name == "nt":
        if not parent.is_dir() or parent.is_symlink():
            raise ArtifactExportError("destination parent must already exist")
        return -1, destination.name, _open_windows_artifact_parent(parent)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for part in parent.parts[1:]:
            if not part or part == ".":
                continue
            if part == "..":
                raise ArtifactExportError("destination parent is invalid")
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd, destination.name, []
    except Exception:
        os.close(fd)
        raise


def _copy_artifact(snapshot: dict, destination: str, cancelled=None) -> dict:
    def check_cancelled():
        if cancelled is not None and cancelled():
            raise ArtifactExportError("export cancelled")

    try:
        check_cancelled()
        _artifact_path_parts(Path(destination))
        dest_path = Path(destination)
        if dest_path.parent.is_symlink():
            raise ArtifactExportError("destination parent must not be a symlink")
        # POSIX aliases such as macOS /tmp are canonicalized before dirfd
        # traversal. Windows keeps the original path so the retained Win32
        # component handles can detect and pin every junction/reparse boundary.
        if os.name != "nt":
            dest_path = dest_path.parent.resolve(strict=True) / dest_path.name
        src_fd, start = _open_artifact_source(snapshot)
        try:
            parent_fd, name, parent_handles = _open_artifact_parent(dest_path)
        except Exception:
            os.close(src_fd)
            raise
        temp_name = None
        temp_fd = None
        try:
            expected = {k: snapshot.get(k) for k in ("dev", "ino", "size", "mtime_ns", "ctime_ns")}
            if _artifact_identity(start) != expected or start.st_size > ARTIFACT_MAX_BYTES:
                raise ArtifactExportError("artifact changed before export")
            # Select a collision-free final name while holding the parent fd.
            stem, ext = os.path.splitext(name)
            final = name
            for suffix in range(0, 1000):
                candidate = name if suffix == 0 else f"{stem} ({suffix + 1}){ext}"
                _artifact_name(candidate)
                try:
                    if parent_fd >= 0:
                        os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
                    elif Path(destination if suffix == 0 else dest_path.with_name(candidate)).exists():
                        pass
                    else:
                        raise FileNotFoundError
                except FileNotFoundError:
                    final = candidate
                    break
            else:
                raise ArtifactExportError("too many destination name collisions")
            for _ in range(16):
                temp_name = f"{ARTIFACT_EXPORT_PREFIX}{secrets.token_hex(12)}.tmp"
                try:
                    if parent_fd >= 0:
                        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0), 0o600, dir_fd=parent_fd)
                    else:
                        temp_fd = os.open(str(dest_path.parent / temp_name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                    break
                except FileExistsError:
                    continue
            if temp_fd is None:
                raise ArtifactExportError("could not create temporary destination")
            total = 0
            while total < start.st_size:
                check_cancelled()
                block = os.read(src_fd, min(ARTIFACT_IO_CHUNK, start.st_size - total))
                if not block:
                    raise ArtifactExportError("artifact changed while exporting")
                view = memoryview(block)
                while view:
                    count = os.write(temp_fd, view)
                    if count <= 0: raise ArtifactExportError("could not write destination")
                    view = view[count:]
                total += len(block)
            if os.read(src_fd, 1):
                raise ArtifactExportError("artifact grew while exporting")
            end = os.fstat(src_fd)
            if _artifact_identity(end) != expected or total != expected["size"]:
                raise ArtifactExportError("artifact changed while exporting")
            os.fsync(temp_fd)
            check_cancelled()
            os.close(temp_fd)
            temp_fd = None
            # Hard-link publication is no-replace on POSIX.  On Windows the
            # fallback is still exclusive because the destination was checked
            # and this path is only used by the compatibility implementation.
            if parent_fd >= 0:
                published = False
                for suffix in range(0, 1000):
                    candidate = name if suffix == 0 else f"{stem} ({suffix + 1}){ext}"
                    try:
                        os.link(temp_name, candidate, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                        final = candidate
                        published = True
                        break
                    except FileExistsError:
                        continue
                if not published:
                    raise ArtifactExportError("too many destination name collisions")
                os.unlink(temp_name, dir_fd=parent_fd)
                try: os.fsync(parent_fd)
                except OSError: pass
            else:
                published = False
                for suffix in range(0, 1000):
                    candidate = name if suffix == 0 else f"{stem} ({suffix + 1}){ext}"
                    try:
                        os.link(dest_path.parent / temp_name, dest_path.parent / candidate)
                        final = candidate
                        published = True
                        break
                    except FileExistsError:
                        continue
                if not published:
                    raise ArtifactExportError("too many destination name collisions")
                (dest_path.parent / temp_name).unlink()
            return {"ok": True, "dest": str(dest_path.parent / final), "name": final, "bytes": total}
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_name:
                try:
                    if parent_fd >= 0: os.unlink(temp_name, dir_fd=parent_fd)
                    else: (dest_path.parent / temp_name).unlink()
                except OSError: pass
            if parent_fd >= 0:
                os.close(parent_fd)
            _close_windows_handles(parent_handles)
            os.close(src_fd)
    except ArtifactExportError as exc:
        return _grant_error(exc.message)
    except (OSError, ValueError) as exc:
        return _grant_error(f"could not export artifact: {exc}")


def _resolve_artifact_grant(grant_id: str) -> dict:
    if not isinstance(grant_id, str) or not re.fullmatch(r"[0-9a-f]{64}", grant_id):
        raise ArtifactExportError("invalid save grant")
    with ARTIFACT_GRANTS_LOCK:
        grant = ARTIFACT_GRANTS.get(grant_id)
        if not grant or grant.get("expires", 0) <= time.time():
            ARTIFACT_GRANTS.pop(grant_id, None)
            raise ArtifactExportError("save grant expired")
        job = _job_record(grant.get("job_id")) or _persisted_record(grant.get("job_id"))
        if (job and
                (job.get("status") != "ok" or
                 not _artifact_snapshot_bound_to_job(grant.get("snapshot") or {}, job))):
            ARTIFACT_GRANTS.pop(grant_id, None)
            raise ArtifactExportError("save grant is no longer valid")
        if not job and not grant.get("terminal"):
            ARTIFACT_GRANTS.pop(grant_id, None)
            raise ArtifactExportError("save grant is no longer valid")
        snapshot = grant.get("snapshot") or {}
        try:
            fd, current_stat = _open_artifact_source(snapshot)
            os.close(fd)
        except (ArtifactExportError, OSError):
            ARTIFACT_GRANTS.pop(grant_id, None)
            raise ArtifactExportError("artifact changed before export")
        if _artifact_identity(current_stat) != {k: snapshot.get(k) for k in ("dev", "ino", "size", "mtime_ns", "ctime_ns")}:
            ARTIFACT_GRANTS.pop(grant_id, None)
            raise ArtifactExportError("artifact changed before export")
        return grant


def export_selected(grant_id: str, destination: str) -> dict:
    """Validate/admit a grant, then schedule the potentially long copy."""
    _expire_artifacts()
    grant = _resolve_artifact_grant(grant_id)
    if not isinstance(destination, str):
        raise ArtifactExportError("destination must be a string")
    _artifact_path_parts(Path(destination))
    with ARTIFACT_GRANTS_LOCK:
        if grant.get("busy"):
            raise ArtifactExportError("save grant is already being used")
        grant["busy"] = True
    with EXPORTS_LOCK:
        if sum(1 for op in EXPORTS.values() if not op.get("done")) >= ARTIFACT_EXPORT_MAX_ACTIVE:
            with ARTIFACT_GRANTS_LOCK: grant["busy"] = False
            raise ArtifactExportError("too many artifact exports are active")
        operation_id = secrets.token_hex(16)
        operation = {"done": False, "result": None, "expires": time.time() + ARTIFACT_EXPORT_TTL,
                     "grant_id": grant_id, "cancelled": False}
        EXPORTS[operation_id] = operation
        while len(EXPORTS) > ARTIFACT_EXPORT_MAX_RETAINED:
            old_id = next((key for key, value in EXPORTS.items() if value.get("done")), None)
            if old_id is None:
                break
            EXPORTS.pop(old_id, None)
    def run():
        def cancelled():
            with EXPORTS_LOCK:
                current = EXPORTS.get(operation_id)
                return current is None or bool(current.get("cancelled"))

        result = _copy_artifact(grant["snapshot"], destination, cancelled)
        with EXPORTS_LOCK:
            current = EXPORTS.get(operation_id)
            if current:
                current["done"], current["result"] = True, result
                # One-use only after successful publication; failed copies may retry.
                if result.get("ok"):
                    with ARTIFACT_GRANTS_LOCK: ARTIFACT_GRANTS.pop(grant_id, None)
                else:
                    with ARTIFACT_GRANTS_LOCK:
                        if grant_id in ARTIFACT_GRANTS: ARTIFACT_GRANTS[grant_id]["busy"] = False
    try:
        EXPORT_EXECUTOR.submit(run)
    except RuntimeError as exc:
        with EXPORTS_LOCK:
            EXPORTS.pop(operation_id, None)
        with ARTIFACT_GRANTS_LOCK:
            if grant_id in ARTIFACT_GRANTS:
                ARTIFACT_GRANTS[grant_id]["busy"] = False
        raise ArtifactExportError("artifact export worker is unavailable") from exc
    return {"operation_id": operation_id}


def export_poll(operation_id: str) -> dict:
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ArtifactExportError("invalid export operation")
    _expire_artifacts()
    with EXPORTS_LOCK:
        op = EXPORTS.get(operation_id)
        if not op:
            raise ArtifactExportError("export operation not found")
        if not op.get("done"):
            return {"done": False}
        return {"done": True, "result": op.get("result") or _grant_error("export failed")}


def export_cancel(operation_id: str) -> dict:
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ArtifactExportError("invalid export operation")
    with EXPORTS_LOCK:
        op = EXPORTS.get(operation_id)
        if not op: raise ArtifactExportError("export operation not found")
        if op.get("done"): return {"done": True, "result": op.get("result")}
        op["cancelled"] = True
        op["result"] = _grant_error("export cancelled")
        return {"done": False}


def _utf8_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # scripts that print progress in a loop (the fetch plugins print a line
    # per batch of cards) otherwise sit in Python's 8 KB pipe buffer until the
    # process exits, so the UI sees the whole transcript as one chunk at the
    # end. Line-buffered stdout makes each line reach the console as it's made.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _proc_kwargs() -> dict:
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _external_proc_kwargs() -> dict:
    """Detach UI-launched helpers from the worker's protocol stdio."""
    return {**_proc_kwargs(), "shell": False,
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def _fmt_argv(argv: List[str]) -> str:
    return " ".join(shlex.quote(p) if " " in p else p for p in argv)


def bundled_python() -> Path:
    """The interpreter job scripts should run with.

    In a dev checkout that's simply sys.executable. Inside an app bundle,
    however, sys.executable is the launcher *stub* (on macOS a dylib that
    can't be exec'd; on Windows an executable that only re-launches the app,
    ignoring its arguments), so the launcher provisions a relocatable CPython
    in the data area and tells us where it is via SCM_WORKBENCH_PYTHON.
    """
    env_py = os.environ.get("SCM_WORKBENCH_PYTHON")
    if env_py:
        p = Path(env_py)
        if p.is_file():
            return p
    exe = Path(sys.executable)
    name = exe.name.lower()
    if "python" in name:
        return exe
    # Fallbacks: the in-bundle framework / a runtime beside the stub
    app = next((p for p in exe.parents if p.suffix == ".app" or p.name.endswith(".app")), None)
    if app is not None:
        cand = app / "Contents" / "Frameworks" / "Python.framework" / "Versions" / "Current" / "Python"
        if cand.is_file():
            return cand
    for cand in (exe.parent / "pythonw.exe", exe.parent / "python.exe"):
        if cand.is_file():
            return cand
    return exe


def app_packages_dir() -> Optional[Path]:
    """The bundle's support site-packages (Resources/app_packages), if any."""
    base = Path(__file__).resolve()
    for p in base.parents:
        if p.name.endswith(".app"):
            d = p / "Contents" / "Resources" / "app_packages"
            return d if d.is_dir() else None
    return None


def build_command(kind: str, args: dict, settings: dict, info: dict, write_deck: bool = True) -> Tuple[list, Optional[Path], dict, str, list, list]:
    """Assemble (argv, cwd, env, title, warnings, errors) for a job kind.

    The browser sends structured values only; argv is assembled here, in one
    place, which keeps command previews and real runs identical.
    """
    warnings: List[str] = []
    if os.environ.get("SCM_WORKBENCH_PACKAGED") and not os.environ.get("SCM_WORKBENCH_PYTHON"):
        warnings.append(
            "First launch: the app's private Python runtime is still being prepared in the "
            "background, so this job runs without the repo's packages and may fail on import. "
            "Give it a minute and try again — or watch the dashboard banner.")
    errors: List[str] = []
    scm, extras = effective_dirs(settings)
    python = bundled_python()
    if settings.get("python"):
        p = Path(settings["python"])
        p = p if p.is_absolute() else Path(__file__).resolve().parent / p
        if p.exists():
            python = p
        else:
            warnings.append(f"Configured python not found ({p}); using {python.name}.")
    # A job can never run on the app's own stub: on Windows the stub is a
    # fixed "run the app" binary, so Popen'ing it would launch another copy
    # of this app (which launches another, …). Until the private runtime is
    # provisioned, jobs decline to start and say why.
    if (os.environ.get("SCM_WORKBENCH_PACKAGED") and not os.environ.get("SCM_WORKBENCH_PYTHON")
            and os.name == "nt" and Path(str(python)).resolve() == Path(sys.executable).resolve()):
        errors.append(
            "the app's private Python runtime isn't ready yet (first launch) — and a job can't "
            "run on the app's own stub, because that would just launch another copy of the app. "
            "Try again in a minute; the dashboard banner tracks provisioning.")

    manifest = get_manifest()
    spec = manifest.get(kind) or {}
    title = spec.get("job_title") or spec.get("title") or kind
    env = _utf8_env()
    argv = [str(python)]

    def require_repo(name: str, path: Optional[Path], hint: str = "") -> bool:
        if path is None or not Path(path).is_dir():
            errors.append(f"{name} repo not found — set its path in Settings{'. ' + hint if hint else '.'}")
            return False
        return True

    d = settings.get("defaults", {})

    # Packaged apps: the job's (provisioned) interpreter isn't the one that
    # installed the support packages, so point it at them explicitly.
    if os.environ.get("SCM_WORKBENCH_PACKAGED"):
        pkgs = app_packages_dir()
        if pkgs:
            env["PYTHONPATH"] = str(pkgs)

    if kind in ("repo_update", "repo_init"):
        cwd = WB_ROOT
        a = args
        argv += ["-m", "scm_workbench.repo_sync", kind.split("_")[-1], "--repo", str(a.get("repo") or "scm")]
        if kind == "repo_update" and a.get("force_full"):
            argv += ["--force-full"]
        env["SCM_WORKBENCH_DATA"] = str(DATA_DIR)

    elif kind == "create_pdf":
        if not require_repo("SCM", scm, "e.g. the silhouette-card-maker folder."):
            return argv, None, env, title, warnings, errors
        cwd = scm
        a = args
        # In simple mode the command shows only what deviates from the
        # defaults: SCM's own defaults (the folder paths, 3-mark
        # registration, stretch fit) never need to appear in the command a
        # user reads — unless the value actually deviates (edited in
        # advanced mode, say) or, for quality, the global quality setting
        # is other than 100.
        simple = str(settings.get("ui_mode", "advanced")) == "simple"

        def emit(key, *flag, default=None):
            nonlocal argv
            v = a.get(key)
            if v in (None, ""):
                return
            if simple and default is not None and str(v) == default:
                return
            argv += list(flag) + [str(v)]

        argv += ["create_pdf.py"]
        emit("front_dir", "--front_dir_path", default="game/front")
        emit("back_dir", "--back_dir_path", default="game/back")
        emit("double_sided_dir", "--double_sided_dir_path", default="game/double_sided")
        argv += ["--output_path", str(a.get("output_path") or "game/output/game.pdf")]
        if a.get("output_images"): argv += ["--output_images"]
        card = str(a.get("card_size") or d.get("card_size") or "standard")
        paper = str(a.get("paper_size") or d.get("paper_size") or "letter")
        argv += ["--card_size", card, "--paper_size", paper]
        emit("registration", "--registration", default="3")
        if a.get("registration_orientation"): argv += ["--registration_orientation", str(a["registration_orientation"])]
        if a.get("specialty"): argv += ["--specialty", str(a["specialty"])]
        if a.get("only_fronts"):
            argv += ["--only_fronts"]
            ds = str(a.get("double_sided_dir") or "")
            if ds:
                ds_dir = (cwd / ds) if not Path(ds).is_absolute() else Path(ds)
                if ds_dir.is_dir():
                    n = sum(1 for c in ds_dir.iterdir() if c.is_file() and is_image_file(c))
                    if n:
                        warnings.append(
                            f"Double-sided folder “{ds}” still has {n} image{'s' if n == 1 else 's'} — "
                            "create_pdf.py refuses --only_fronts while those exist; remove them first or uncheck the option.")
        emit("fit", "--fit", default="stretch")
        if a.get("fit_backs"): argv += ["--fit_backs", str(a["fit_backs"])]
        for key in ("crop", "crop_backs", "extend_edges", "extend_edges_backs",
                    "extend_corners", "extend_corners_backs", "extend_bleed", "extend_bleed_backs"):
            v = a.get(key)
            if key == "crop" and not v and a.get("mpcfill_crop") and simple:
                # the simple-mode “MPCFill Crop” toggle is shorthand for a 3mm
                # crop: MPCFill's fetched art carries its own print-bleed
                # padding. In advanced mode the toggle is not offered at all -
                # the Crop boxes are the direct control - so a leftover value
                # can't silently crop a PDF, and a typed value always wins.
                v = "3mm"
            if v: argv += ["--" + key, str(v)]
        ppi = a.get("ppi")
        ppi = int(ppi) if ppi not in (None, "") else int(d.get("ppi", 1200))
        quality = a.get("quality")
        quality = int(quality) if quality not in (None, "") else int(d.get("quality", 100))
        if simple and quality == 100:
            # the form sat at the manifest default — the global quality
            # setting is the preference that governs
            quality = int(d.get("quality", 100))
        argv += ["--ppi", str(ppi)]
        if not (simple and quality == 100):
            argv += ["--quality", str(quality)]
        for idx in a.get("skip") or []:
            argv += ["--skip", str(idx)]
        if a.get("label"): argv += ["--label", str(a["label"])]
        if a.get("show_outline"): argv += ["--show_outline"]
        if a.get("borderless"): argv += ["--borderless"]
        if a.get("load_offset"):
            argv += ["--load_offset"]
            paper_eff = effective_paper(info, "create_pdf", a, settings)
            entry = load_per_size_offsets().get(paper_eff or "")
            g = info["scm"].get("saved_offset")
            if entry:
                warnings.append(
                    f"Per-size offset for “{paper_eff}” (x {entry['x']}, y {entry['y']}, {entry['angle']}°) "
                    "will be staged into data/offset_data.json before the run — SCM keeps one shared offset file, "
                    "so this job prints with that row.")
            elif g:
                warnings.append(f"No per-size offset saved for “{paper_eff}” — the global saved offset (x {g['x']}, y {g['y']}, {g['angle']}°) applies.")
            else:
                warnings.append("No offset saved (global or per-size) — “--load_offset” has nothing to apply.")
        known_extra = extras_card_names(info)
        for v in (card, paper):
            if v and v.lower() in known_extra:
                extra_file = (extras / "assets" / "layouts_extra.json") if extras else None
                if extra_file and extra_file.is_file():
                    env["SCM_EXTRA_LAYOUTS"] = str(extra_file)
                else:
                    errors.append(f"“{v}” comes from scm-extras, but its layouts file can’t be found.")
                break

    elif kind == "offset_pdf":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        a = args
        src = a.get("pdf_path") or "game/output/game.pdf"
        argv += ["offset_pdf.py", "--pdf_path", str(src)]
        if a.get("output_pdf_path"):
            argv += ["--output_pdf_path", str(a["output_pdf_path"])]
        gave_any = False
        for k, f in (("x_offset", "-x"), ("y_offset", "-y"), ("angle", "-a")):
            if a.get(k) not in (None, ""):
                argv += [f, str(a[k])]
                gave_any = True
        if not gave_any:
            errors.append("Provide at least one of X, Y, or angle.")
        argv += ["--ppi", str(int(a.get("ppi") or 1200))]
        if a.get("save"):
            argv += ["-s"]
        if a.get("paper_size"):
            entry = load_per_size_offsets().get(str(a["paper_size"]))
            if entry:
                tail = " “Save” (−s) records the used values back into that row." if a.get("save") else ""
                warnings.append(
                    f"Per-size offset for “{a['paper_size']}” (x {entry['x']}, y {entry['y']}, {entry['angle']}°) is staged in "
                    f"before the run, so any field left blank falls back to that row instead of the global value.{tail}")

    elif kind == "calibration":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        argv += ["generate_calibration.py"]

    elif kind == "dxf_single":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        a = args
        argv += ["generate_dxf.py", "single"]
        if (a.get("card_mode") or "named") == "named":
            card = str(a.get("card_size") or "standard")
            argv += ["--card_size", card]
            if a.get("card_name"):
                argv += ["--card_name", str(a["card_name"])]
        else:
            if not a.get("card_width") or not a.get("card_height"):
                errors.append("Custom card size: both width and height are required (e.g. 63mm and 88mm).")
                card = None
            else:
                card = f"{a['card_width']}x{a['card_height']}"
                argv += ["--card_width", str(a["card_width"]), "--card_height", str(a["card_height"])]
                if a.get("card_radius"):
                    argv += ["--card_radius", str(a["card_radius"])]
        if (a.get("paper_mode") or "named") == "named":
            paper = str(a.get("paper_size") or "letter")
            argv += ["--paper_size", paper]
            if a.get("paper_name"):
                argv += ["--paper_name", str(a["paper_name"])]
        else:
            if not a.get("paper_width") or not a.get("paper_height"):
                errors.append("Custom paper size: both width and height are required (e.g. 8.5in and 11in).")
                paper = None
            else:
                paper = f"{a['paper_width']}x{a['paper_height']}"
                argv += ["--paper_width", str(a["paper_width"]), "--paper_height", str(a["paper_height"])]
        variant = str(a.get("variant") or "default")
        argv += ["--variant", variant]
        argv += ["--orientation", str(a.get("orientation") or "optimize")]
        out = a.get("output_path")
        if not out:
            sub = "borderless/dxf" if variant == "borderless" else "dxf"
            card_lbl = card or "?"
            paper_lbl = paper or "?"
            vtag = "" if variant == "default" else f"-{variant}"
            out = f"cutting_templates/{sub}/{paper_lbl}-{card_lbl}{vtag}-v1.dxf"
        argv += [str(out)]
        if a.get("save"):
            argv += ["--save"]
        known_extra = extras_card_names(info)
        for v in (card, paper):
            if v and str(v).lower() in known_extra:
                extra_file = (extras / "assets" / "layouts_extra.json") if extras else None
                if extra_file and extra_file.is_file():
                    env["SCM_EXTRA_LAYOUTS"] = str(extra_file)
                    warnings.append(f"Extras size “{v}” detected — SCM_EXTRA_LAYOUTS is set automatically.")
                else:
                    errors.append(f"“{v}” comes from scm-extras, but its layouts file can’t be found.")
                break

    elif kind == "dxf_batch":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        argv += ["generate_dxf.py", "batch"]
        mode = str(args.get("mode") or "missing")
        if mode == "all":
            argv += ["--all"]
        elif mode == "optimize":
            argv += ["--optimize"]

    elif kind == "dxf_list":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        argv += ["generate_dxf.py", "list"]

    elif kind == "clean_up":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        # the Workbench's own variant of SCM's clean_up.py (which would also
        # delete the README.md placeholders the current repo versions ship):
        # same clears, placeholders survive
        argv += [str(Path(__file__).resolve().parent / "clear_images.py")]

    elif kind == "extras_generate":
        if not require_repo("scm-extras", extras):
            return argv, None, env, title, warnings, errors
        cwd = extras
        argv += ["generate.py"]
        if str(args.get("mode") or "missing") == "all":
            argv += ["--all"]

    elif kind == "extras_tables":
        if not require_repo("scm-extras", extras):
            return argv, None, env, title, warnings, errors
        cwd = extras
        argv += ["generate_readme_tables.py"]

    elif kind.startswith("fetch:"):
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        slug = kind.split(":", 1)[1]
        a = args
        fmt = str(a.get("format") or "")
        if not fmt:
            errors.append("Pick a decklist format.")
        deck_source = str(a.get("deck_source") or "file")
        deck = ""
        is_url_format = fmt == "url" or fmt.endswith("_url")
        if deck_source == "paste":
            content = str(a.get("deck_text") or "").strip()
            if not content:
                errors.append("Paste the decklist text.")
            name = str(a.get("deck_name") or "").strip() or f"{slug}_deck.txt"
            if not re.fullmatch(r"[\w .\-(\)]+", name):
                errors.append("Decklist file name may only contain letters, numbers, spaces and . - ( )")
            else:
                deckdir = cwd / "game" / "decklist"
                if write_deck:
                    deckdir.mkdir(parents=True, exist_ok=True)
                    (deckdir / name).write_text(content + "\n", encoding="utf-8")
                deck = f"game/decklist/{name}"
        elif deck_source == "url":
            deck = str(a.get("deck_url") or "").strip()
            if not re.fullmatch(r"https?://\S+", deck):
                errors.append("Enter a URL starting with http:// or https://.")
            elif not is_url_format:
                warnings.append(f"“{fmt}” reads a decklist file — a URL only works with URL-based formats (e.g. “url”, “*_url”).")
        else:
            name = str(a.get("deck_file") or "").strip()
            if not name:
                errors.append("Pick an existing decklist file (or use paste / URL).")
            elif "/" in name or "\\" in name:
                errors.append("Decklist names come from the game/decklist/ list — they can't contain path separators.")
            elif not (cwd / "game" / "decklist" / name).is_file():
                errors.append(f"Decklist file “{name}” is not in game/decklist/ — reopen the form to refresh the list.")
            else:
                # the plugin opens its argument relative to the repo root, so the
                # file source gets the same prefixed path as the paste source
                deck = f"game/decklist/{name}"
            if deck and is_url_format:
                warnings.append("URL-based formats use the URL itself, not a file — switch the source to “URL”.")
        fetch_script = cwd / "plugins" / slug / "fetch.py"
        if not fetch_script.is_file():
            errors.append(f"plugins/{slug}/fetch.py is not present in your SCM checkout — the job cannot run.")
        argv += [f"plugins/{slug}/fetch.py", deck, fmt]
        if slug == "mtg":
            if a.get("ignore_set_and_collector_number"): argv += ["-i"]
            if a.get("prefer_older_sets"): argv += ["--prefer_older_sets"]
            for s in a.get("prefer_set") or []: argv += ["--prefer_set", str(s)]
            for s in a.get("ignore_set") or []: argv += ["--ignore_set", str(s)]
            if a.get("prefer_showcase"): argv += ["--prefer_showcase"]
            if a.get("prefer_extra_art"): argv += ["--prefer_extra_art"]
            for l in a.get("prefer_lang") or []: argv += ["--prefer_lang", str(l)]
            if a.get("prefer_ub"): argv += ["--prefer_ub"]
            if a.get("ignore_ub"): argv += ["--ignore_ub"]
            if a.get("tokens"): argv += ["--tokens"]

        # The plugins never delete what they find in game/front/: re-fetching a
        # *different* deck silently leaves the old images behind, and the next
        # Create PDF would mix them into the layout. Say so before the run.
        front = cwd / "game" / "front"
        if front.is_dir():
            n = sum(1 for c in front.iterdir() if c.is_file() and is_image_file(c))
            if n:
                warnings.append(
                    f"The front folder already holds {n} image{'s' if n != 1 else ''} from a previous fetch. "
                    "Fetching overwrites matching files but never deletes anything — if this is a different deck, "
                    "clear the folder first (the “Clear card images” button on this page) or the old art ends up "
                    "in your next PDF."
                )

    else:
        errors.append(f"Unknown job kind: {kind}")
        return argv, None, env, title, warnings, errors

    return argv, cwd, env, title, warnings, errors


MANIFEST_CACHE: Dict[str, dict] = {}
MANIFEST_LOCK = threading.Lock()
_REPOS_MTIME: Dict[str, float] = {}


def _repos_changed() -> bool:
    """True when a repo update touched files the manifest reads (layouts.json etc.)."""
    now = None
    for p in (repo_sync.state_file(), DATA_DIR / "repos-manifest-scm.json"):
        try:
            now = max(now or 0, p.stat().st_mtime)
        except OSError:
            pass
    # the decklist folder feeds the deck_file choices — a file added or removed
    # there (Finder, paste-save, import) must invalidate the cached manifest
    try:
        scm, _ = effective_dirs(load_settings())
        if scm:
            dl = scm / "game" / "decklist"
            try:
                now = max(now or 0, dl.stat().st_mtime)
            except OSError:
                pass
    except Exception:
        pass
    if now is None:
        return False
    return now > _REPOS_MTIME.get("t", 0)


# The manifest and the preview share ONE repo snapshot: boot pays for the one
# full get_info() scan, and every keystroke-driven preview after that reads the
# same cached dict (30 s TTL, invalidating early on the same repo-change
# signals as the manifest) instead of re-walking both repos. The lock makes a
# burst of concurrent previews wait for one build rather than each scanning.
_INFO_SNAP: Dict[str, Any] = {}


def _repos_changed_since(t: float) -> bool:
    return _repos_signal_mtime() > t


def _get_info_locked() -> dict:
    """Build or return the shared repo snapshot (caller holds MANIFEST_LOCK)."""
    now = time.time()
    c = _INFO_SNAP
    if c.get("v") and now - c.get("t", 0) < 30 and not _repos_changed_since(c.get("t", 0)):
        return c["v"]
    v = get_info()
    c.clear()
    c.update(t=now, v=v)
    return v


def get_info_cached() -> dict:
    """The snapshot the preview endpoint runs against (see _INFO_SNAP)."""
    with MANIFEST_LOCK:
        return _get_info_locked()


def get_manifest() -> dict:
    with MANIFEST_LOCK:
        # The mtime signal alone can never fire again once the first build
        # happens after the last state write (exactly what a first boot looks
        # like: the cache is built empty at startup, the bootstrap then writes
        # state, and no file ever changes again) — so the cache also carries
        # the same 30 s TTL as the shared repo snapshot. Worst case a stale
        # manifest is visible for half a minute; rebuilding is cheap because it
        # rides on the snapshot.
        now = time.time()
        if (not MANIFEST_CACHE or now - _REPOS_MTIME.get("t", 0) > 30 or _repos_changed()):
            MANIFEST_CACHE.clear()
            MANIFEST_CACHE.update(build_manifest(_get_info_locked()))
            _REPOS_MTIME["t"] = now
    return MANIFEST_CACHE


def invalidate_manifest_cache() -> None:
    with MANIFEST_LOCK:
        MANIFEST_CACHE.clear()
        _INFO_SNAP.clear()
        _REPOS_MTIME.clear()


class PreviewError(Exception):
    """A user-facing preview request error shared by HTTP and native IPC."""

    def __init__(self, *, status: int, http_body: dict, ipc_code: str, message: str):
        super().__init__(message)
        self.status = status
        self.http_body = http_body
        self.ipc_code = ipc_code
        self.message = message


def normalize_args(spec: dict, raw: dict) -> Tuple[dict, List[str], List[str]]:
    """Coerce/validate raw client values against the manifest. Returns (args, errors, warnings)."""
    errors: List[str] = []
    warns: List[str] = []
    args: Dict[str, Any] = {}
    for g in spec.get("groups", []):
        for o in g["options"]:
            key, t = o["key"], o["type"]
            v = raw.get(key)
            if t == "chips":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                v = v if isinstance(v, list) else []
                if o.get("int"):
                    clean = []
                    for x in v:
                        if re.fullmatch(r"\d+", str(x)):
                            clean.append(int(x))
                        else:
                            errors.append(f"{o['label']}: “{x}” is not a valid index.")
                    v = clean
                args[key] = v
            elif t == "choice_chips":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                v = v if isinstance(v, list) else []
                valid = [c[0] for c in o.get("choices", [])]
                if valid:
                    v = [x for x in v if x in valid]
                args[key] = v
            elif t == "toggle":
                args[key] = bool(v)
            elif t == "textarea":
                args[key] = "" if v is None else str(v)
            elif t in ("number",):
                if v in (None, ""):
                    args[key] = o.get("default")
                else:
                    try:
                        f = float(v)
                        args[key] = int(f) if (o.get("step") in (None, 1) or f == int(f)) else round(f, 2)
                    except (TypeError, ValueError):
                        errors.append(f"{o['label']}: “{v}” is not a number.")
                        args[key] = o.get("default")
            elif t == "range":
                try:
                    f = float(v)
                    lo, hi = o.get("min", -1e9), o.get("max", 1e9)
                    if not (lo <= f <= hi):
                        warns.append(f"{o['label']} {int(round(f)) if (o.get('step') or 1) >= 1 else f} is outside the slider range ({lo:g}–{hi:g}) — the slider shows the nearest value, but the number you typed is used.")
                    args[key] = int(round(f)) if (o.get("step") or 1) >= 1 else round(f, 2)
                except (TypeError, ValueError):
                    args[key] = o.get("default")
            elif t in ("select", "segment"):
                sv = "" if v is None else str(v).strip()
                if not sv:
                    args[key] = o.get("default")
                else:
                    valid = [c[0] for c in o.get("choices", [])]
                    if valid and sv not in valid:
                        errors.append(f"{o['label']}: unknown value “{sv}”.")
                        args[key] = o.get("default")
                    else:
                        args[key] = sv
            else:  # text / path
                args[key] = "" if v is None else str(v).strip()
    return args, errors, warns


def build_preview(kind: str, raw_args: dict) -> dict:
    """Build the command preview used by both HTTP and native IPC.

    This deliberately shares the manifest, argument normalization, command
    builder, and cached repo snapshot used by jobs.  It only assembles a
    command: ``write_deck=False`` keeps preview requests side-effect free.
    """
    manifest = get_manifest()
    if kind not in manifest:
        raise PreviewError(
            status=404, http_body={"error": "unknown kind"},
            ipc_code="bad_request", message="unknown kind",
        )
    if not isinstance(raw_args, dict):
        raise PreviewError(
            status=400, http_body={"error": "bad args"},
            ipc_code="bad_request", message="preview args must be an object",
        )

    normalized, errors, norm_warns = normalize_args(manifest[kind], raw_args)
    settings = load_settings()
    argv, cwd, env, title, warnings, errs = build_command(
        kind, normalized, settings, get_info_cached(), write_deck=False,
    )
    # Create PDF needs card images to work with — the front directory
    # (SCM's own default when the form leaves it empty) empty means the
    # job would produce nothing, so the client keeps the run button
    # disabled until it has images.
    no_front = False
    if kind == "create_pdf" and not errs and cwd:
        front = normalized.get("front_dir") or "game/front"
        fd = Path(front) if os.path.isabs(front) else (Path(cwd) / front)
        n = sum(1 for c in fd.iterdir() if c.is_file() and is_image_file(c)) if fd.is_dir() else 0
        if n == 0:
            no_front = True
            # the alternate tip only exists in advanced mode — simple mode
            # can't change the front directory, so fetching is the only way
            # to run
            tip = "" if str(settings.get("ui_mode", "advanced")) == "simple" else " or point the form at a folder that has images."
            warnings.append(f"No images in the front directory ({front}). Use the fetch card art workflow first{tip or '.'}")
    return {
        # Always show the command that was built: validation problems are
        # already visible in the notes below, and a (partial or
        # default-substituted) command is the most useful thing on screen.
        "cmd": _fmt_argv(argv),
        "cwd": str(cwd) if cwd else None,
        "env": {k: v for k, v in env.items()
                if k.startswith("SCM_") or k in ("PYTHONIOENCODING", "PYTHONUTF8")},
        "warnings": warnings + norm_warns + errors,
        "errors": errs,
        "no_front_images": no_front,
    }


def _offset_sensitive_job(kind: str, args: dict) -> bool:
    return (kind == "offset_pdf") or (kind == "create_pdf" and bool(args.get("load_offset")))


def _offset_save_intended(args: dict, prior: Optional[dict]) -> Optional[dict]:
    base = prior or {"x": 0, "y": 0, "angle": 0.0}
    values = {
        "x": base["x"] if args.get("x_offset") in (None, "") else args.get("x_offset"),
        "y": base["y"] if args.get("y_offset") in (None, "") else args.get("y_offset"),
        "angle": base["angle"] if args.get("angle") in (None, "") else args.get("angle"),
    }
    return _offset_row(values)


def start_job(kind: str, raw_args: dict) -> Tuple[Optional[dict], List[str]]:
    with JOBS_LOCK:
        if _UPDATE_QUIESCING:
            return None, ["the app update is being handed off; try again after it restarts"]
    spec = get_manifest().get(kind)
    if not spec:
        return None, [f"Unknown job kind “{kind}”."]

    # client may send hidden selections outside the manifest (e.g. dxf_single's
    # card_mode-paired select) — keep them if present
    args, errors, norm_warns = normalize_args(spec, raw_args)

    info = get_info()
    argv, cwd, env, title, warnings, errs = build_command(kind, args, load_settings(), info)
    warnings += norm_warns
    errors += errs
    if errors:
        return None, errors

    # The lease is deliberately acquired only after validation/building and
    # immediately before staging/spawn.  Never wait here: callers have a finite
    # RPC timeout and a deterministic busy result is safer than a stuck call.
    offset_lease = False
    staged = None
    if _offset_sensitive_job(kind, args):
        if not OFFSET_LEASE.acquire(blocking=False):
            return None, ["offset operations are busy; try again after the running offset job finishes"]
        offset_lease = True
        try:
            error = _recover_offset_projection_locked(cwd)
            if error:
                OFFSET_LEASE.release(); offset_lease = False
                return None, _offset_errors([error])
            paper = effective_paper(info, kind, args, load_settings())
            if paper:
                state = _load_offset_state_strict()
                entry = state.get("rows", {}).get(paper)
                desired = paper if entry else None
                if state.get("staged_size") != desired:
                    state["staged_size"] = desired
                    error = _commit_offset_state(state, cwd)
                    if error:
                        OFFSET_LEASE.release(); offset_lease = False
                        return None, _offset_errors([error])
                if entry:
                    staged = {"size": paper, **entry}
            elif kind == "offset_pdf":
                # Blank paper means global, never "whatever row happened to be
                # projected by the previous job". Commit this selection before
                # reading the prior value used by pending-save reconciliation.
                state = _load_offset_state_strict()
                state["staged_size"] = None
                error = _commit_offset_state(state, cwd)
                if error:
                    OFFSET_LEASE.release(); offset_lease = False
                    return None, _offset_errors([error])
            if kind == "offset_pdf" and args.get("save"):
                state = _load_offset_state_strict()
                prior, error = _read_upstream_projection(cwd)
                if error:
                    OFFSET_LEASE.release(); offset_lease = False
                    return None, _offset_errors([error])
                intended = _offset_save_intended(args, prior)
                if intended is None:
                    OFFSET_LEASE.release(); offset_lease = False
                    return None, ["offset save values are outside the allowed bounds"]
                target = str(args["paper_size"]) if args.get("paper_size") else "global"
                state["pending"] = {"target": target, "intended": intended,
                                    "prior_projection": prior}
                error = _commit_offset_state(state, cwd, project=False)
                if error:
                    OFFSET_LEASE.release(); offset_lease = False
                    return None, _offset_errors([error])
        except OffsetStateError as exc:
            OFFSET_LEASE.release(); offset_lease = False
            return None, _offset_errors([str(exc)])
        except Exception as exc:
            OFFSET_LEASE.release(); offset_lease = False
            return None, _offset_errors([f"could not prepare offset job: {exc}"])

    job_id = uuid.uuid4().hex[:10]
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        if offset_lease:
            OFFSET_LEASE.release()
        return None, _offset_errors([f"could not create job log: {exc}"])
    job: dict = {
        "id": job_id,
        "ts": time.time(),
        "kind": kind,
        "title": title,
        "cmd": _fmt_argv(argv),
        "args": args,
        "status": "running",
        "exit_code": None,
        "log_file": str(LOGS_DIR / f"{job_id}.log"),
        "log_lines": [],
        "first_seq": 0,
        "subs": [],
        "warnings": warnings,
        "started": time.time(),
        "ended": None,
        "duration": None,
        "proc": None,
        "proc_lock": threading.Lock(),
        "proc_reaped": False,
        "pump_thread": None,
        "scm_path": str(cwd) if cwd else None,
        "offset_lease": offset_lease,
    }
    # Record terminal candidates before the child starts. This prevents a
    # successful calibration run from granting an unrelated old PDF merely
    # because it happens to share the expected family.
    before = {}
    for candidate in _artifact_expected_paths(job):
        try:
            if os.name == "nt":
                # Take the pre-run baseline from an open handle: a path stat
                # on Windows can report the volume's cached directory-entry
                # timestamps, which lag the file's true change time after a
                # fresh write and would make every unmodified output look new.
                base_fd, st = _open_windows_regular_file(candidate)
                os.close(base_fd)
            else:
                st = os.stat(candidate, follow_symlinks=False)
                if _is_reparse_or_symlink(os.lstat(candidate)):
                    continue
            if stat.S_ISREG(st.st_mode):
                before[str(candidate.resolve())] = _artifact_identity(st)
        except (OSError, ArtifactExportError):
            pass
    if before:
        job["artifact_before"] = before
    if kind == "offset_pdf" and args.get("save"):
        # SCM's own -s writes the shared file with the values just used;
        # _pump mirrors them back into the selected row (or global baseline).
        job["offset_sync"] = str(args["paper_size"]) if args.get("paper_size") else None
        job["offset_save"] = True
    try:
        log_f = open(job["log_file"], "w", encoding="utf-8")
    except Exception as exc:
        if offset_lease:
            OFFSET_LEASE.release()
        return None, _offset_errors([f"could not create job log: {exc}"])
    header = [f"$ {job['cmd']}", f"(cwd: {cwd})",
              f"(started {time.strftime('%Y-%m-%d %H:%M:%S')})"]
    if staged:
        header.append(f"(offset: staged “{staged['size']}” — x {staged['x']}, y {staged['y']}, {staged['angle']}° → data/offset_data.json)")
    try:
        log_f.write("\n".join(header) + "\n\n")
        log_f.flush()
        job["log_lines"] = header
    except Exception as exc:
        try: log_f.close()
        except Exception: pass
        if offset_lease:
            OFFSET_LEASE.release()
        return None, _offset_errors([f"could not write job log: {exc}"])
    if not _acquire_image_job_lease():
        try:
            log_f.close()
        except Exception:
            pass
        if offset_lease:
            OFFSET_LEASE.release()
        try:
            Path(job["log_file"]).unlink()
        except OSError:
            pass
        return None, ["image deletion is using the SCM checkout; try again when it finishes"]
    job["image_lease"] = True

    proc = None
    try:
        # Keep the final admission check and publication under the same lock as
        # update handoff.  An update can therefore never quiesce after this
        # job passed the check but before it entered JOBS.
        with JOBS_LOCK:
            if _UPDATE_QUIESCING:
                raise RuntimeError("the app update is being handed off; try again after it restarts")
            proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **_proc_kwargs())
            job["proc"] = proc
            pump_thread = threading.Thread(
                target=_pump, args=(job, proc, log_f), daemon=True,
                name=f"job-pump-{job_id}",
            )
            job["pump_thread"] = pump_thread
            JOBS[job_id] = job
        pump_thread.start()
    except Exception as e:
        if isinstance(e, RuntimeError) and str(e).startswith("the app update is being handed off"):
            try:
                log_f.close()
            except Exception:
                pass
            if offset_lease:
                try:
                    OFFSET_LEASE.release()
                except RuntimeError:
                    pass
            if job.get("image_lease"):
                job["image_lease"] = False
                try:
                    _release_image_job_lease()
                except RuntimeError:
                    pass
            try:
                Path(job["log_file"]).unlink()
            except OSError:
                pass
            return None, [str(e)]
        if proc is not None:
            _terminate_and_reap(proc, job.get("proc_lock"))
            job["proc_reaped"] = proc.poll() is not None
        if job.get("offset_save"):
            try:
                state = _load_offset_state_strict()
                state.pop("pending", None)
                _commit_offset_state(state, Path(job["scm_path"]), project=False)
            except Exception:
                pass
        log_f.write(f"failed to start: {e}\n")
        log_f.close()
        job["status"] = "fail"
        job["log_lines"].append(f"failed to start: {e}")
        if offset_lease:
            OFFSET_LEASE.release()
            job["offset_lease"] = False
        if job.get("image_lease"):
            job["image_lease"] = False
            try:
                _release_image_job_lease()
            except RuntimeError:
                pass
        with JOBS_LOCK:
            JOBS[job_id] = job
    return job, []


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Force the exact managed job tree; never leave its grandchildren."""
    if os.name != "nt":
        try:
            # _proc_kwargs creates every managed job with PGID == PID. Kill
            # the group in one operation instead of waiting on its leader;
            # a TERM-resistant grandchild must not escape when the leader exits.
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, OSError):
            pass
    else:
        # Never pass a reaped/reusable PID to taskkill. During packaged app
        # shutdown the outer Tauri job object remains the descendant backstop.
        if proc.poll() is not None:
            return
        # taskkill targets this exact still-managed PID and its descendants;
        # shell=False and CREATE_NO_WINDOW avoid a command shell or console.
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
                timeout=3,
                check=False,
                shell=False,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    proc.kill()


def _terminate_and_reap(proc: subprocess.Popen, proc_lock=None) -> None:
    """Stop/reap a job while excluding every competing PID signal path."""
    lock_context = proc_lock if proc_lock is not None else contextlib.nullcontext()
    with lock_context:
        try:
            if proc.poll() is None:
                _kill_process_group(proc)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                _kill_process_group(proc)
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass


def _wait_job_process(job: dict, proc: subprocess.Popen) -> int:
    """Reap under the per-job signal lock so PID reuse cannot race kill_job."""
    proc_lock = job.setdefault("proc_lock", threading.Lock())
    poll = getattr(proc, "poll", None)
    if not callable(poll):
        # Deterministic test/embedding doubles may expose only wait(). Real
        # subprocess.Popen instances always take the nonblocking path below.
        with proc_lock:
            rc = proc.wait()
            job["proc_reaped"] = True
            return rc
    while True:
        with proc_lock:
            rc = poll()
            if rc is not None:
                job["proc_reaped"] = True
                return rc
        time.sleep(0.02)


def _pump(job: dict, proc: subprocess.Popen, log_f) -> None:
    rc = 1
    status = "fail"
    try:
        for line in iter(proc.stdout.readline, b""):
            s = line.decode("utf-8", "replace").rstrip("\r\n")
            _append_job_line(job, s, log_f=log_f)
        rc = _wait_job_process(job, proc)
        with JOBS_LOCK:
            kill_requested = bool(job.get("kill_requested"))
            pump_lines = list(job.get("log_lines") or [])
        if kill_requested:
            status = "killed"
        elif rc == 0 and any(re.search(r"is not a valid file", l, re.IGNORECASE) for l in pump_lines):
            status = "fail"
        elif rc == 0:
            status = "ok"

        # SCM writes its shared file before rendering.  A nonzero render exit
        # therefore does not discard a valid -s result; only malformed data is
        # ignored.  Use the job's snapshotted checkout, never current Settings.
        if job.get("offset_save"):
            scm_path = Path(job["scm_path"])
            state = _load_offset_state_strict()
            pending = state.get("pending")
            error = _reconcile_pending_locked(scm_path, state)
            if error:
                status = "fail"
                _append_job_line(job, f"(offset: could not reconcile saved values: {error})", log_f=log_f)
            else:
                current = _read_upstream_offset(scm_path)
                # Jobs created before durable pending records still get the
                # historical save behavior; new jobs always take the guarded
                # reconciliation branch above.
                if pending is None and current is not None:
                    paper = job.get("offset_sync")
                    if paper:
                        state["rows"][paper], state["staged_size"] = current, paper
                    else:
                        state["global"], state["staged_size"] = current, None
                    error = _commit_offset_state(state, scm_path)
                    if error:
                        status = "fail"
                        _append_job_line(job, f"(offset: could not record saved values: {error})", log_f=log_f)
                if current is not None:
                    paper = job.get("offset_sync")
                    target = f"the “{paper}” row" if paper else "the global baseline"
                    _append_job_line(job, f"(offset: recorded the saved values in {target} — x {current['x']}, y {current['y']}, {current['angle']}°)", log_f=log_f)

        with JOBS_LOCK:
            job["status"] = status
            job["exit_code"] = rc
            job["ended"] = time.time()
            job["duration"] = round(job["ended"] - job["started"], 2)
            # Capture once, at terminal success, against the job's immutable
            # checkout snapshot. This must happen before persistence and never
            # consult current Settings.
            if status == "ok":
                snapshots = _snapshot_artifacts(job)
                if snapshots:
                    job["artifact_snapshots"] = snapshots
                else:
                    job.pop("artifact_snapshots", None)
            else:
                job.pop("artifact_snapshots", None)
    except Exception as exc:
        # A logging/decoding/persistence failure must never release the lease
        # while the child can still read or write the shared SCM projection.
        _terminate_and_reap(proc, job.get("proc_lock"))
        job["proc_reaped"] = proc.poll() is not None
        try:
            _append_job_line(job, f"job pump failed: {exc}", log_f=log_f)
        except Exception:
            pass
        with JOBS_LOCK:
            job["status"] = "fail"
            job["exit_code"] = rc
            job["ended"] = time.time()
            job["duration"] = round(job["ended"] - job["started"], 2)
            job.pop("artifact_snapshots", None)
        status = "fail"
    finally:
        try:
            log_f.close()
        except Exception:
            pass
        with JOBS_LOCK:
            subscribers = list(job.get("subs", []))
        _notify_subscribers(job, subscribers, ("done", status, rc), terminal=True)
        if job.get("kind", "").startswith("fetch:"):
            invalidate_manifest_cache()
        if job.get("offset_lease"):
            job["offset_lease"] = False
            try:
                OFFSET_LEASE.release()
            except RuntimeError:
                pass
        if job.get("image_lease"):
            job["image_lease"] = False
            try:
                _release_image_job_lease()
            except RuntimeError:
                pass
        _persist_jobs()


def kill_job(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("status") != "running" or job.get("proc") is None:
            return False
        proc = job["proc"]
        proc_lock = job.setdefault("proc_lock", threading.Lock())

    # The pump uses this same lock for its only poll/reap operation. Therefore
    # a PID/PGID cannot become reusable between this liveness check and the
    # exact managed-tree signal below.
    with proc_lock:
        if job.get("proc_reaped") or proc.poll() is not None:
            job["proc_reaped"] = True
            return False
        with JOBS_LOCK:
            if job.get("status") != "running" or job.get("proc") is not proc:
                return False
            job["kill_requested"] = True
        try:
            _kill_process_group(proc)
        except Exception:
            pass
    return True


def stop_all_jobs(timeout: float = 2.0) -> None:
    """Terminate/reap children and join pumps before a transport exits.

    The lock is used only to take the work list. Waiting while holding it would
    deadlock a pump trying to publish its terminal status.
    """
    with JOBS_LOCK:
        active = []
        for job in JOBS.values():
            # Handoff is owned by the external helper.  Killing or deleting
            # this record here would strand its durable journal/result.
            if job.get("status") == "handoff":
                continue
            pump = job.get("pump_thread")
            if job.get("status") == "running" or (pump is not None and getattr(pump, "is_alive", lambda: False)()):
                active.append(job)
    for job in active:
        if job.get("status") == "running":
            kill_job(job.get("id", ""))

    deadline = time.monotonic() + max(0.0, timeout)
    for job in active:
        proc = job.get("proc")
        if proc is None or not hasattr(proc, "wait"):
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                _kill_process_group(proc)
            except Exception:
                pass
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
        except Exception:
            pass

    # Reaping closes the child's stdout, allowing _pump to finish its final
    # log write, subscriber wake, and persistence update. Join outside the
    # global lock so those operations can acquire it freely.
    pump_deadline = time.monotonic() + max(0.0, timeout)
    for job in active:
        pump = job.get("pump_thread")
        if pump is None or pump is threading.current_thread() or not hasattr(pump, "join"):
            continue
        try:
            pump.join(timeout=max(0.0, pump_deadline - time.monotonic()))
        except Exception:
            pass


def sse_stream(job_id: str, after: int):
    """Yield (event, data) tuples for one SSE subscriber of a job."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        yield "done", json.dumps({"status": "missing"})
        return
    q = _WakeQueue(maxsize=SSE_QUEUE_SIZE)
    with JOBS_LOCK:
        job.setdefault("subs", []).append(q)
        first, initial = _job_lines_locked(job)
        status = job.get("status", "missing")
        exit_code = job.get("exit_code")
    sent = after - 1
    try:
        for offset, line in enumerate(initial):
            seq = first + offset
            if seq >= after:
                bounded, _ = _line_wire(line)
                yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                sent = seq
        if status != "running":
            yield "done", json.dumps({"status": status, "exit_code": exit_code})
            return
        while True:
            try:
                msg = q.get(timeout=15)
            except queue.Empty:
                with JOBS_LOCK:
                    status = job.get("status", "missing")
                    exit_code = job.get("exit_code")
                yield "ping", "{}"
                if status != "running":
                    # The terminal notification may have raced this timeout;
                    # replay from the authoritative transcript before done.
                    first, lines = _job_lines_snapshot(job)
                    for n, line in enumerate(lines):
                        seq = first + n
                        if seq > sent:
                            bounded, _ = _line_wire(line)
                            yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                            sent = seq
                    yield "done", json.dumps({"status": status, "exit_code": exit_code})
                    return
                continue
            if msg[0] == "gap":
                first, lines = _job_lines_snapshot(job)
                for n, line in enumerate(lines):
                    seq = first + n
                    if seq > sent:
                        bounded, _ = _line_wire(line)
                        yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                        sent = seq
            elif msg[0] == "line":
                # Legacy in-process update jobs still emit (line, text),
                # while subprocess jobs carry an authoritative sequence.
                if len(msg) >= 3:
                    seq, line = msg[1], msg[2]
                else:
                    seq, line = sent + 1, msg[1]
                if seq > sent:
                    bounded, _ = _line_wire(line)
                    yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                    sent = seq
            elif msg[0] == "done":
                first, lines = _job_lines_snapshot(job)
                for n, line in enumerate(lines):
                    seq = first + n
                    if seq > sent:
                        bounded, _ = _line_wire(line)
                        yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                        sent = seq
                with JOBS_LOCK:
                    final_status = job.get("status")
                    final_exit_code = job.get("exit_code")
                yield "done", json.dumps({"status": final_status, "exit_code": final_exit_code})
                return
    finally:
        with JOBS_LOCK:
            try:
                job["subs"].remove(q)
            except (ValueError, KeyError):
                pass


# ============================================================================
# File sandbox
# ============================================================================

# Native OS actions accept only bounded, printable values. Keep these limits
# independent of the JSON-lines frame limit and the HTTP request parser.
ACTION_PATH_MAX_BYTES = 4096
ACTION_URL_MAX_BYTES = 8192
ACTION_ERROR_MAX_BYTES = 4096


def has_forbidden_action_controls(value: str) -> bool:
    """Whether *value* contains a C0 (or DEL) control character."""
    return any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)


def _bounded_action_error(error: Any) -> str:
    """Make launcher errors safe to put in either action response surface."""
    text = str(error).replace("\r", " ").replace("\n", " ")
    # HTTP action responses use the default JSON ASCII escaping. Restricting
    # diagnostics to printable ASCII makes the byte bound hold on both HTTP
    # and native serialization paths, including Unicode OSError messages.
    text = "".join(char if 0x20 <= ord(char) < 0x7f else " " for char in text)
    if not text:
        text = "action failed"
    encoded = text.encode("ascii")
    if len(encoded) <= ACTION_ERROR_MAX_BYTES:
        return text
    return encoded[:ACTION_ERROR_MAX_BYTES].decode("ascii") or "action failed"


def _action_response(error: Optional[Any] = None) -> dict:
    if error is None:
        return {"ok": True, "errors": []}
    return {"ok": False, "errors": [_bounded_action_error(error)]}


class _ActionFailure(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _validate_action_value(value: Any, name: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise _ActionFailure(f"{name} must be a non-empty string", 400)
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _ActionFailure(f"{name} must be valid UTF-8", 400) from error
    if encoded_size > max_bytes:
        raise _ActionFailure(f"{name} exceeds {max_bytes} UTF-8 bytes", 400)
    if has_forbidden_action_controls(value):
        raise _ActionFailure(f"{name} contains control characters", 400)
    return value


def _action_path(raw: Any, operation: str, settings: Optional[dict] = None) -> Path:
    value = _validate_action_value(raw, "path", ACTION_PATH_MAX_BYTES)
    roots = allowed_roots(settings if settings is not None else load_settings())
    try:
        # _managed_path retains the HTTP route's existing relative precedence;
        # canonicalize immediately afterwards so the launcher never receives a
        # traversal or a symlink that resolves outside the managed roots.
        candidate = _managed_path(value, roots)
        path = candidate.resolve(strict=False)
    except FileListError as error:
        raise _ActionFailure(error.message, 403 if error.code == "forbidden" else 400) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise _ActionFailure("could not resolve path", 400) from error
    if not _inside(path, roots):
        raise _ActionFailure("path is outside the allowed repos", 403)
    if not path.exists():
        raise _ActionFailure("path does not exist", 200)
    if operation == "open" and not path.is_file():
        raise _ActionFailure("path must be an existing regular file", 200)
    if operation == "reveal" and not (path.is_file() or path.is_dir()):
        raise _ActionFailure("path must be an existing file or directory", 200)
    return path


def file_open_action(raw: Any, settings: Optional[dict] = None) -> Tuple[dict, int]:
    try:
        return _action_response(open_path(_action_path(raw, "open", settings))), 200
    except _ActionFailure as error:
        return _action_response(error.message), error.status
    except Exception as error:
        return _action_response(error), 200


def file_reveal_action(raw: Any, settings: Optional[dict] = None) -> Tuple[dict, int]:
    try:
        return _action_response(reveal_path(_action_path(raw, "reveal", settings))), 200
    except _ActionFailure as error:
        return _action_response(error.message), error.status
    except Exception as error:
        return _action_response(error), 200


def _validate_action_url(raw: Any) -> str:
    value = _validate_action_value(raw, "url", ACTION_URL_MAX_BYTES)
    try:
        parsed = urlsplit(value)
        # Accessing hostname and port performs urllib's strict authority
        # checks (including malformed brackets and out-of-range ports).
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError as error:
        raise _ActionFailure("URL has a malformed port or authority", 400) from error
    if parsed.scheme.lower() not in ("http", "https"):
        raise _ActionFailure("only http(s) URLs can be opened", 400)
    if any(char.isspace() for char in value):
        raise _ActionFailure("URL must not contain whitespace", 400)
    if not hostname:
        raise _ActionFailure("URL must have a hostname", 400)
    if "@" in parsed.netloc:
        raise _ActionFailure("URL userinfo is not allowed", 400)
    # urllib treats a trailing colon as an absent port, but it is not a valid
    # authority for this action and is commonly an accidental malformed port.
    if parsed.netloc.endswith(":"):
        raise _ActionFailure("URL has a malformed port or authority", 400)
    return value


def url_open_action(raw: Any) -> Tuple[dict, int]:
    try:
        return _action_response(open_url(_validate_action_url(raw))), 200
    except _ActionFailure as error:
        return _action_response(error.message), error.status
    except Exception as error:
        return _action_response(error), 200


# Magic-byte signatures for the image formats silhouette-card-maker accepts
# (its `valid_mimetypes` list in utilities.py). The Workbench is stdlib-only,
# so this sniffs the file header instead of the `filetype` package — keeping
# "is this an image?" consistent with what create_pdf.py itself counts.
def is_image_file(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(16)
    except Exception:
        return False
    return _image_header_is_image(head)


# Image deletion is a separate destructive operation.  It intentionally has
# tighter bounds and a narrower root than the read-only file sandbox above.
IMAGE_DELETE_MAX_SCANNED = 8192
IMAGE_DELETE_MAX_CANDIDATES = 1024
IMAGE_DELETE_MAX_RESULT_BYTES = 512 * 1024
IMAGE_DELETE_MAX_PATH_BYTES = 4096
IMAGE_DELETE_MAX_NAME_BYTES = 255
_IMAGE_DELETE_LOCK = threading.Lock()
_IMAGE_JOB_STATE_LOCK = threading.Lock()
_IMAGE_JOB_USERS = 0


def _acquire_image_job_lease() -> bool:
    """Admit jobs concurrently, but never while image deletion owns the fence."""
    global _IMAGE_JOB_USERS
    with _IMAGE_JOB_STATE_LOCK:
        if _IMAGE_DELETE_LOCK.locked():
            return False
        _IMAGE_JOB_USERS += 1
        return True


def _release_image_job_lease() -> None:
    global _IMAGE_JOB_USERS
    with _IMAGE_JOB_STATE_LOCK:
        if _IMAGE_JOB_USERS <= 0:
            raise RuntimeError("image job lease is not held")
        _IMAGE_JOB_USERS -= 1


class ImageDeleteError(Exception):
    def __init__(self, message: str, status: int = 400, *, deleted=None, names=None, directory=None):
        super().__init__(message)
        self.message = " ".join(str(message).split())[:256] or "image deletion failed"
        self.status = status
        self.deleted = int(deleted or 0)
        self.names = list(names or [])[:IMAGE_DELETE_MAX_CANDIDATES]
        self.directory = directory

    def body(self) -> dict:
        result = {"ok": False, "errors": [self.message], "deleted": self.deleted,
                  "names": self.names}
        if self.directory is not None:
            result["dir"] = str(self.directory)
        return result


def _image_delete_error(message: str, status: int = 400, **kw):
    return ImageDeleteError(message, status, **kw)


def _image_delete_lock():
    """Take both mutation fences without waiting on either one.

    The lock file is the same SCM repository lock used by repo_sync.  A
    deletion is small and bounded, so waiting here would only turn a user
    action into an unbounded serialized IPC request; contention is reported as
    a normal application result instead.
    """
    @contextlib.contextmanager
    def locked():
        if not _IMAGE_DELETE_LOCK.acquire(blocking=False):
            raise _image_delete_error("image deletion is busy", 409)
        with _IMAGE_JOB_STATE_LOCK:
            jobs_active = _IMAGE_JOB_USERS > 0
        if jobs_active:
            _IMAGE_DELETE_LOCK.release()
            raise _image_delete_error("a job is using the SCM checkout", 409)
        fh = None
        acquired = False
        try:
            lock_path = repo_sync.data_dir() / ".repos-scm-lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "a+")
            if os.name == "nt":
                import msvcrt
                fh.seek(0, 2)
                if fh.tell() == 0:
                    fh.write(" ")
                    fh.flush()
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as exc:
                    if getattr(exc, "errno", None) in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or \
                            getattr(exc, "winerror", None) in {32, 33, 36, 170, 212}:
                        raise _image_delete_error("repository is busy", 409) from exc
                    raise _image_delete_error("could not acquire repository lock", 400) from exc
            else:
                import fcntl
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (BlockingIOError, OSError) as exc:
                    if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN):
                        raise _image_delete_error("repository is busy", 409) from exc
                    raise _image_delete_error("could not acquire repository lock", 400) from exc
            yield
        finally:
            if fh is not None:
                try:
                    if acquired:
                        if os.name == "nt":
                            import msvcrt
                            fh.seek(0)
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fh, fcntl.LOCK_UN)
                finally:
                    fh.close()
            _IMAGE_DELETE_LOCK.release()
    return locked()


def _image_header_is_image(head: bytes) -> bool:
    if len(head) < 4:
        return False
    return (head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or
            head[:4] == b"GIF8" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or
            head[:4] in (b"II\x2a\x00", b"MM\x00\x2a") or head[:2] == b"BM" or
            (head[4:8] == b"ftyp" and head[8:12] in (b"av01", b"avif", b"heif", b"hevc", b"mif1")) or
            head[:4] == b"qoif" or head[:8] == b"DDS <wal" or
            head[:12] == b"\x00\x00\x00\x0cJP\x20\x31\x31\x0a\x0d\x08")


def _image_delete_result_size(directory: Path, names: list) -> int:
    try:
        value = {"ok": True, "deleted": len(names), "names": names, "dir": str(directory)}
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, UnicodeError, ValueError) as exc:
        raise _image_delete_error("could not encode deletion result") from exc


def _delete_images_target(raw: str, settings: Optional[dict]) -> Tuple[Path, Path, dict]:
    try:
        encoded = raw.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise _image_delete_error("path must be valid UTF-8") from exc
    if not isinstance(raw, str) or not raw or len(encoded) > IMAGE_DELETE_MAX_PATH_BYTES:
        raise _image_delete_error("path must be a non-empty string of at most 4096 UTF-8 bytes")
    if has_forbidden_action_controls(raw):
        raise _image_delete_error("path contains control characters")
    settings = settings if settings is not None else load_settings()
    scm, _extras = effective_dirs(settings)
    if not scm:
        raise _image_delete_error("SCM checkout is not configured", 403)
    root_alias = Path(os.path.abspath(os.fspath(scm)))
    try:
        root = root_alias.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _image_delete_error("SCM checkout is unavailable", 403) from exc
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise _image_delete_error("SCM checkout is unavailable", 403) from exc
    if _is_reparse_or_symlink(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise _image_delete_error("SCM checkout is not a safe directory", 403)
    # Deletion is intentionally narrower than read-only file listing:
    # relative paths always bind to the initiating settings snapshot's SCM
    # checkout, never DATA_DIR, UI_DIR, or the extras checkout.
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root_alias / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    try:
        # Keep the lexical components below the configured SCM alias so a
        # symlink there is still rejected by dirfd traversal.  Only the fixed
        # platform alias above the checkout (notably macOS /var -> /private/var)
        # is replaced with the pinned canonical root.
        if os.path.commonpath((os.fspath(root_alias), os.fspath(candidate))) == os.fspath(root_alias):
            candidate = root / candidate.relative_to(root_alias)
        elif os.path.commonpath((os.fspath(root), os.fspath(candidate))) != os.fspath(root):
            resolved_candidate = candidate.resolve(strict=False)
            if os.path.commonpath((os.fspath(root), os.fspath(resolved_candidate))) != os.fspath(root):
                raise _image_delete_error("path is outside the SCM checkout", 403)
            candidate = resolved_candidate
    except ValueError as exc:
        raise _image_delete_error("path is outside the SCM checkout", 403) from exc
    if candidate == root:
        raise _image_delete_error("the SCM checkout itself cannot be deleted", 403)
    try:
        if len(os.fspath(candidate).encode("utf-8")) > IMAGE_DELETE_MAX_PATH_BYTES:
            raise _image_delete_error("path exceeds 4096 UTF-8 bytes")
    except UnicodeEncodeError as exc:
        raise _image_delete_error("path must be valid UTF-8") from exc
    return root, candidate, _artifact_identity(root_stat)


def _open_delete_directory_posix(root: Path, candidate: Path, root_identity_expected: dict):
    if os.name == "nt" or not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise _image_delete_error("secure image deletion is unavailable on this platform")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    root_fd = None
    current_fd = None
    try:
        root_fd = os.open(os.fspath(root), flags)
        current_fd = root_fd
        root_identity = os.fstat(root_fd)
        if (_is_reparse_or_symlink(root_identity) or
                not stat.S_ISDIR(root_identity.st_mode) or
                _artifact_identity(root_identity) != root_identity_expected):
            raise _image_delete_error("SCM checkout changed before deletion", 403)
        relative = candidate.relative_to(root)
        parts = relative.parts
        if not parts:
            raise _image_delete_error("the SCM checkout itself cannot be deleted", 403)
        for index, part in enumerate(parts):
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if index == len(parts) - 1:
                    if current_fd != root_fd:
                        os.close(current_fd)
                    return root_fd, None, candidate
                raise _image_delete_error("path contains a missing intermediate directory")
            except NotADirectoryError:
                try:
                    leaf = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if index == len(parts) - 1:
                        if current_fd != root_fd:
                            os.close(current_fd)
                        return root_fd, None, candidate
                    raise _image_delete_error("path contains a missing intermediate directory")
                if _is_reparse_or_symlink(leaf):
                    raise _image_delete_error(
                        "path is outside the SCM checkout or contains a symlink/reparse point", 403)
                if index == len(parts) - 1:
                    if current_fd != root_fd:
                        os.close(current_fd)
                    return root_fd, None, candidate
                raise _image_delete_error("path contains a non-directory component")
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.ELOOP:
                    raise _image_delete_error(
                        "path is outside the SCM checkout or contains a symlink/reparse point", 403) from exc
                if index == len(parts) - 1 and getattr(exc, "errno", None) in (errno.ENOENT, errno.ENOTDIR):
                    try:
                        leaf = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        if current_fd != root_fd:
                            os.close(current_fd)
                        return root_fd, None, candidate
                    if _is_reparse_or_symlink(leaf):
                        raise _image_delete_error(
                            "path is outside the SCM checkout or contains a symlink/reparse point", 403) from exc
                    if current_fd != root_fd:
                        os.close(current_fd)
                    return root_fd, None, candidate
                raise _image_delete_error("path contains an unsafe component") from exc
            st = os.fstat(next_fd)
            if _is_reparse_or_symlink(st) or not stat.S_ISDIR(st.st_mode):
                os.close(next_fd)
                if index == len(parts) - 1 and stat.S_ISREG(st.st_mode):
                    if current_fd != root_fd:
                        os.close(current_fd)
                    return root_fd, None, candidate
                raise _image_delete_error("path contains a symlink or non-directory component", 403)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        return root_fd, current_fd, candidate
    except Exception:
        if current_fd is not None and current_fd != root_fd:
            try: os.close(current_fd)
            except OSError: pass
        if root_fd is not None:
            try: os.close(root_fd)
            except OSError: pass
        raise


def _close_delete_fds(root_fd, directory_fd):
    if directory_fd is not None and directory_fd != root_fd:
        try: os.close(directory_fd)
        except OSError: pass
    if root_fd is not None:
        try: os.close(root_fd)
        except OSError: pass


def _check_image_delete_cancelled(cancelled, *, deleted=0, names=None,
                                  directory=None) -> None:
    if cancelled is not None and cancelled():
        raise _image_delete_error("image deletion cancelled", deleted=deleted,
                                  names=names or [], directory=directory)


def _scan_delete_posix(directory_fd: int, directory: Path, cancelled=None) -> list:
    candidates = []
    scanned = 0
    iterator = None
    try:
        iterator = os.scandir(directory_fd)
        for entry in iterator:
            _check_image_delete_cancelled(cancelled, directory=directory)
            if scanned >= IMAGE_DELETE_MAX_SCANNED:
                raise _image_delete_error("directory scan is too large; no images were deleted")
            scanned += 1
            name = entry.name
            try:
                if len(name.encode("utf-8")) > IMAGE_DELETE_MAX_NAME_BYTES:
                    raise _image_delete_error("directory entry name is too long; no images were deleted")
            except UnicodeEncodeError as exc:
                raise _image_delete_error("directory entry name is not valid UTF-8") from exc
            st = entry.stat(follow_symlinks=False)
            if _is_reparse_or_symlink(st):
                # A reparse/symlink candidate is never treated as an absent
                # image.  Failing the preflight also prevents partial deletion.
                raise _image_delete_error("directory contains a symlink or reparse point")
            if not stat.S_ISREG(st.st_mode):
                continue
            fd = None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
                stable = os.fstat(fd)
                if _is_reparse_or_symlink(stable) or not stat.S_ISREG(stable.st_mode):
                    raise _image_delete_error("directory entry changed to a symlink or non-file")
                if stable.st_dev != st.st_dev or stable.st_ino != st.st_ino:
                    raise _image_delete_error("directory entry changed during scan")
                if not _image_header_is_image(os.read(fd, 16)):
                    continue
                candidates.append({"name": name, "dev": stable.st_dev, "ino": stable.st_ino,
                                   "size": stable.st_size, "mtime_ns": stable.st_mtime_ns,
                                   "ctime_ns": stable.st_ctime_ns})
                if len(candidates) > IMAGE_DELETE_MAX_CANDIDATES:
                    raise _image_delete_error("too many image candidates; no images were deleted")
            except FileNotFoundError:
                raise _image_delete_error("directory changed during preflight; no images were deleted")
            finally:
                if fd is not None:
                    os.close(fd)
        if scanned >= IMAGE_DELETE_MAX_SCANNED:
            raise _image_delete_error("directory scan is too large; no images were deleted")
    except StopIteration:
        pass
    finally:
        if iterator is not None:
            iterator.close()
    candidates.sort(key=lambda item: item["name"])
    if _image_delete_result_size(directory, [item["name"] for item in candidates]) > IMAGE_DELETE_MAX_RESULT_BYTES:
        raise _image_delete_error("deletion result is too large; no images were deleted")
    return candidates


def _rename_delete_candidate(directory_fd: int, source: str, destination: str) -> None:
    """Atomically quarantine one POSIX name without replacing another name."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    source_raw = os.fsencode(source)
    destination_raw = os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        rename = libc.renameatx_np
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(directory_fd, source_raw, directory_fd,
                        destination_raw, 0x00000004)  # RENAME_EXCL
    elif hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(directory_fd, source_raw, directory_fd,
                        destination_raw, 0x00000001)  # RENAME_NOREPLACE
    else:
        raise _image_delete_error("secure no-replace deletion is unavailable")
    if result != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value), destination)


def _delete_images_posix(root: Path, directory_fd: int, directory: Path,
                         cancelled=None) -> dict:
    candidates = _scan_delete_posix(directory_fd, directory, cancelled)
    deleted = []
    for item in candidates:
        fd = None
        quarantine_fd = None
        quarantine_name = None
        moved = False
        try:
            _check_image_delete_cancelled(cancelled, deleted=len(deleted), names=deleted,
                                          directory=directory)
            fd = os.open(item["name"], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
            stable = os.fstat(fd)
            if (_is_reparse_or_symlink(stable) or not stat.S_ISREG(stable.st_mode) or
                    stable.st_dev != item["dev"] or stable.st_ino != item["ino"] or
                    stable.st_size != item["size"] or stable.st_mtime_ns != item["mtime_ns"] or
                    stable.st_ctime_ns != item["ctime_ns"] or
                    not _image_header_is_image(os.read(fd, 16))):
                raise _image_delete_error("an image changed before deletion", deleted=len(deleted), names=deleted, directory=directory)
            # Move the checked name to an unpredictable private name with an
            # atomic no-replace primitive. Verify that the moved object is the
            # already-open inode before unlinking the quarantine name.
            for _ in range(16):
                quarantine_name = f".wb-image-delete-{secrets.token_hex(16)}.tmp"
                try:
                    _rename_delete_candidate(directory_fd, item["name"], quarantine_name)
                    moved = True
                    break
                except FileExistsError:
                    continue
            if not moved:
                raise _image_delete_error("could not reserve image quarantine", deleted=len(deleted), names=deleted, directory=directory)
            quarantine_fd = os.open(quarantine_name, os.O_RDONLY | os.O_NOFOLLOW |
                                    getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd)
            quarantined = os.fstat(quarantine_fd)
            if (quarantined.st_dev != item["dev"] or quarantined.st_ino != item["ino"] or
                    quarantined.st_size != item["size"] or
                    not _image_header_is_image(os.read(quarantine_fd, 16))):
                try:
                    _rename_delete_candidate(directory_fd, quarantine_name, item["name"])
                    moved = False
                except Exception as restore_error:
                    raise _image_delete_error("an image changed and could not be restored", deleted=len(deleted), names=deleted, directory=directory) from restore_error
                raise _image_delete_error("an image changed before deletion", deleted=len(deleted), names=deleted, directory=directory)
            os.unlink(quarantine_name, dir_fd=directory_fd)
            moved = False
            deleted.append(item["name"])
        except ImageDeleteError:
            raise
        except FileNotFoundError as exc:
            raise _image_delete_error("an image disappeared before deletion", deleted=len(deleted), names=deleted, directory=directory) from exc
        except OSError as exc:
            raise _image_delete_error("could not delete image", deleted=len(deleted), names=deleted, directory=directory) from exc
        finally:
            if moved and quarantine_name is not None:
                try:
                    _rename_delete_candidate(directory_fd, quarantine_name, item["name"])
                except Exception:
                    pass
            if quarantine_fd is not None:
                os.close(quarantine_fd)
            if fd is not None:
                os.close(fd)
    return {"ok": True, "deleted": len(deleted), "names": deleted, "dir": str(directory)}


def _delete_images_windows(root: Path, candidate: Path, root_identity_expected: dict,
                           cancelled=None) -> dict:
    """Delete through pinned Win32 handles; there is intentionally no unlink fallback."""
    if os.name != "nt":
        raise _image_delete_error("secure image deletion is unavailable on this platform")
    import ctypes
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):
        _fields_ = [("attrs", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    class _DispositionEx(ctypes.Structure):
        _fields_ = [("flags", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    # READ_ATTRIBUTES/LIST_DIRECTORY, DELETE; share delete so disposition is
    # permitted even while an opened handle pins the object.
    access_dir = 0x0080 | 0x0001
    access_file = 0x0080 | 0x0001 | 0x00010000
    # Parent directory handles deliberately do not share DELETE, pinning every
    # component against rename while the operation uses path spelling to open
    # the next stable handle. File handles still delete by disposition.
    share = 0x00000001 | 0x00000002
    flags_dir = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
    flags_file = 0x02000000 | 0x00200000

    def raw(handle):
        return getattr(handle, "value", handle)

    def info(handle):
        record = _FileInfo()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(record)):
            raise _image_delete_error("could not inspect a stable Windows handle")
        return record

    def identity(record):
        return (int(record.volume), int(record.index_high), int(record.index_low),
                (int(record.size_high) << 32) | int(record.size_low))

    def open_handle(path: Path, access: int, flags: int):
        handle = kernel32.CreateFileW(str(path), access, share, None, 3, flags, None)
        if raw(handle) == invalid:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        return handle

    def read_head(handle):
        data = ctypes.create_string_buffer(16)
        count = wintypes.DWORD()
        if not kernel32.ReadFile(handle, data, 16, ctypes.byref(count), None):
            raise _image_delete_error("could not inspect image contents")
        return data.raw[:count.value]

    handles = []
    try:
        _check_image_delete_cancelled(cancelled, directory=candidate)
        root_handle = open_handle(root, access_dir, flags_dir)
        handles.append(root_handle)
        root_info = info(root_handle)
        root_key = (int(root_info.volume), int(root_info.index_high), int(root_info.index_low))
        try:
            current_root_stat = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise _image_delete_error("SCM checkout changed before deletion", 403) from exc
        # The retained root handle denies rename/delete sharing, so this path
        # stat identifies that same pinned object. CPython's Windows st_dev is
        # not the Win32 volume serial and must not be compared to root_key.
        if (root_info.attrs & 0x400 or not root_info.attrs & 0x10 or
                _artifact_identity(current_root_stat) != root_identity_expected):
            raise _image_delete_error("SCM checkout changed before deletion", 403)
        # Canonical lexical containment is checked again immediately before
        # opening components; every opened component is independently checked.
        if os.path.commonpath((str(root), str(candidate))) != str(root):
            raise _image_delete_error("path is outside the SCM checkout", 403)
        relative = candidate.relative_to(root)
        parts = relative.parts
        if not parts:
            raise _image_delete_error("the SCM checkout itself cannot be deleted", 403)
        for index, part in enumerate(parts):
            current = root.joinpath(*parts[:index + 1])
            try:
                handle = open_handle(current, access_dir, flags_dir)
            except OSError as exc:
                if index == len(parts) - 1:
                    if getattr(exc, "winerror", None) in (2, 3) or exc.errno in (2, 3):
                        return {"ok": True, "deleted": 0, "names": [], "dir": str(candidate)}
                    # A regular file cannot be opened with LIST_DIRECTORY;
                    # inspect the final object with an attributes-only handle
                    # so non-directories retain the successful zero result.
                    try:
                        leaf_handle = open_handle(current, 0x0080, flags_file)
                    except OSError:
                        raise _image_delete_error("path contains an unsafe component") from exc
                    handles.append(leaf_handle)
                    leaf = info(leaf_handle)
                    if leaf.attrs & 0x400:
                        raise _image_delete_error(
                            "path is outside the SCM checkout or contains a symlink/reparse point",
                            403) from exc
                    return {"ok": True, "deleted": 0, "names": [], "dir": str(candidate)}
                raise _image_delete_error("path contains an unsafe component") from exc
            handles.append(handle)
            record = info(handle)
            if record.attrs & 0x400:
                raise _image_delete_error(
                    "path is outside the SCM checkout or contains a symlink/reparse point", 403)
            if not record.attrs & 0x10:
                if index == len(parts) - 1:
                    return {"ok": True, "deleted": 0, "names": [], "dir": str(candidate)}
                raise _image_delete_error("path contains a non-directory component")
        directory_handle = handles[-1]
        current_root = info(root_handle)
        if (int(current_root.volume), int(current_root.index_high), int(current_root.index_low)) != root_key:
            raise _image_delete_error("SCM checkout changed during deletion", 403)
        # A bounded directory enumeration is done only after the stable target
        # handle exists.  os.scandir is used for names; each candidate is then
        # reopened by exact path and verified by its stable handle.
        candidates = []
        scanned = 0
        for entry in os.scandir(candidate):
            _check_image_delete_cancelled(cancelled, directory=candidate)
            if scanned >= IMAGE_DELETE_MAX_SCANNED:
                raise _image_delete_error("directory scan is too large; no images were deleted")
            scanned += 1
            if len(entry.name.encode("utf-8")) > IMAGE_DELETE_MAX_NAME_BYTES:
                raise _image_delete_error("directory entry name is too long; no images were deleted")
            child = candidate / entry.name
            probe_handle = open_handle(child, 0x0080, flags_file)
            try:
                probe = info(probe_handle)
                if probe.attrs & 0x400:
                    raise _image_delete_error("directory contains a reparse point")
                if probe.attrs & (0x10 | 0x40):  # DIRECTORY or DEVICE
                    continue
            finally:
                kernel32.CloseHandle(probe_handle)
            child_handle = open_handle(child, access_file, flags_file)
            try:
                record = info(child_handle)
                if record.attrs & 0x400:
                    raise _image_delete_error("directory contains a reparse point")
                if record.attrs & (0x10 | 0x40) or identity(record) != identity(probe):
                    raise _image_delete_error("directory entry changed during preflight")
                if not _image_header_is_image(read_head(child_handle)):
                    continue
                candidates.append({"name": entry.name, "identity": identity(record)})
                if len(candidates) > IMAGE_DELETE_MAX_CANDIDATES:
                    raise _image_delete_error("too many image candidates; no images were deleted")
            finally:
                kernel32.CloseHandle(child_handle)
        if scanned >= IMAGE_DELETE_MAX_SCANNED:
            raise _image_delete_error("directory scan is too large; no images were deleted")
        candidates.sort(key=lambda item: item["name"])
        if _image_delete_result_size(candidate, [item["name"] for item in candidates]) > IMAGE_DELETE_MAX_RESULT_BYTES:
            raise _image_delete_error("deletion result is too large; no images were deleted")
        deleted = []
        for item in candidates:
            _check_image_delete_cancelled(cancelled, deleted=len(deleted), names=deleted,
                                          directory=candidate)
            child_handle = open_handle(candidate / item["name"], access_file, flags_file)
            try:
                current_root = info(root_handle)
                if (int(current_root.volume), int(current_root.index_high), int(current_root.index_low)) != root_key:
                    raise _image_delete_error("SCM checkout changed during deletion", 403,
                                              deleted=len(deleted), names=deleted, directory=candidate)
                record = info(child_handle)
                if identity(record) != item["identity"] or not _image_header_is_image(read_head(child_handle)):
                    raise _image_delete_error("an image changed before deletion", deleted=len(deleted), names=deleted, directory=candidate)
                disposition = _DispositionEx(1 | 2)  # DELETE | POSIX_SEMANTICS
                # FileDispositionInfoEx is class 21 (22 is FileRenameInfoEx).
                # Fall back to the original disposition class for filesystems
                # which do not implement POSIX deletion; both paths delete the
                # exact object represented by this verified handle.
                deleted_by_handle = kernel32.SetFileInformationByHandle(
                    raw(child_handle), 21, ctypes.byref(disposition), ctypes.sizeof(disposition))
                if not deleted_by_handle:
                    class _Disposition(ctypes.Structure):
                        _fields_ = [("delete_file", wintypes.BOOL)]
                    legacy = _Disposition(True)
                    deleted_by_handle = kernel32.SetFileInformationByHandle(
                        raw(child_handle), 4, ctypes.byref(legacy), ctypes.sizeof(legacy))
                if not deleted_by_handle:
                    raise _image_delete_error("stable handle deletion is unavailable", deleted=len(deleted), names=deleted, directory=candidate)
                deleted.append(item["name"])
            finally:
                kernel32.CloseHandle(child_handle)
        return {"ok": True, "deleted": len(deleted), "names": deleted, "dir": str(candidate)}
    finally:
        for handle in reversed(handles):
            kernel32.CloseHandle(handle)


_IMAGE_DELETE_OP_LOCK = threading.Lock()
_IMAGE_DELETE_OPS = {}
_IMAGE_DELETE_OP_MAX = 4
_IMAGE_DELETE_OP_TTL = 600.0


def _prune_image_delete_operations_locked(now=None):
    now = time.time() if now is None else now
    for operation_id, operation in list(_IMAGE_DELETE_OPS.items()):
        if operation.get("done") and now - operation.get("ended", now) >= _IMAGE_DELETE_OP_TTL:
            _IMAGE_DELETE_OPS.pop(operation_id, None)


def _run_image_delete_operation(operation_id: str, raw: str, settings: dict):
    with _IMAGE_DELETE_OP_LOCK:
        operation = _IMAGE_DELETE_OPS.get(operation_id)
        if operation is None:
            return
        deadline = operation["deadline"]
    cancelled = lambda: time.monotonic() >= deadline
    try:
        # Bind relative paths to the settings snapshot taken by the initiating
        # RPC rather than whichever checkout happens to be current later.
        result = _delete_images_impl(raw, settings, cancelled)
    except ImageDeleteError as error:
        result = error.body()
    except Exception:
        result = {"ok": False, "errors": ["image deletion failed"], "deleted": 0, "names": []}
    with _IMAGE_DELETE_OP_LOCK:
        operation = _IMAGE_DELETE_OPS.get(operation_id)
        if operation is not None:
            operation["done"] = True
            operation["result"] = result
            operation["ended"] = time.time()


def start_image_delete_operation(raw: str) -> dict:
    with _IMAGE_DELETE_OP_LOCK:
        _prune_image_delete_operations_locked()
        active = sum(not operation.get("done") for operation in _IMAGE_DELETE_OPS.values())
        if active >= _IMAGE_DELETE_OP_MAX or len(_IMAGE_DELETE_OPS) >= _IMAGE_DELETE_OP_MAX * 2:
            return {"ok": False, "errors": ["too many image deletion operations"]}
        operation_id = secrets.token_hex(16)
        _IMAGE_DELETE_OPS[operation_id] = {
            "done": False,
            "result": None,
            "deadline": time.monotonic() + 300.0,
        }
        try:
            settings = load_settings()
            threading.Thread(target=_run_image_delete_operation,
                             args=(operation_id, raw, settings), daemon=True,
                             name="image-delete-operation").start()
        except Exception:
            _IMAGE_DELETE_OPS.pop(operation_id, None)
            return {"ok": False, "errors": ["could not start image deletion"]}
    return {"operation_id": operation_id}


def poll_image_delete_operation(operation_id: str) -> dict:
    with _IMAGE_DELETE_OP_LOCK:
        _prune_image_delete_operations_locked()
        operation = _IMAGE_DELETE_OPS.get(operation_id)
        if operation is None:
            return {"ok": False, "error": {"code": "bad_request", "message": "operation not found"}}
        if not operation.get("done"):
            return {"done": False}
        return {"done": True, "result": operation["result"]}


def _delete_images_impl(raw: str, settings: Optional[dict] = None,
                        cancelled=None) -> dict:
    root, candidate, root_identity = _delete_images_target(raw, settings)
    with _image_delete_lock():
        _check_image_delete_cancelled(cancelled, directory=candidate)
        if os.name == "nt":
            return _delete_images_windows(root, candidate, root_identity, cancelled)
        root_fd, directory_fd, directory = _open_delete_directory_posix(
            root, candidate, root_identity)
        try:
            if directory_fd is None:
                _check_image_delete_cancelled(cancelled, directory=candidate)
                return {"ok": True, "deleted": 0, "names": [], "dir": str(candidate)}
            return _delete_images_posix(root, directory_fd, candidate, cancelled)
        finally:
            _close_delete_fds(root_fd, directory_fd)


def delete_images(raw: str, settings: Optional[dict] = None) -> dict:
    try:
        return _delete_images_impl(raw, settings)
    except ImageDeleteError as exc:
        return exc.body()
    except Exception:
        return {"ok": False, "errors": ["image deletion failed"], "deleted": 0, "names": []}


def allowed_roots(settings: dict) -> List[Path]:
    roots = [DATA_DIR, UI_DIR]
    for p in effective_dirs(settings):
        if p:
            roots.append(p)
    return [r for r in roots if r]


def _inside(path: Path, roots: List[Path]) -> bool:
    try:
        rp = path.resolve()
    except Exception:
        return False
    return any(rp == r or r in rp.parents for r in (x.resolve() for x in roots))


# Native artifact reads are deliberately bounded independently of the JSON-lines
# frame limit. A directory iterator is never materialized before the scan
# limit is applied.
FILE_LIST_MAX_SCANNED = 8192
FILE_LIST_MAX_ITEMS = 1024
FILE_LIST_MAX_RESULT_BYTES = 512 * 1024


class FileListError(Exception):
    """A safe, user-facing failure while resolving a managed directory."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _managed_path(raw: Path | str, roots: List[Path]) -> Path:
    """Resolve a managed path without following it outside the sandbox."""
    p = Path(raw)
    if not p.is_absolute():
        # Preserve the HTTP route's precedence for relative paths: the first
        # existing root wins, otherwise paths are relative to the first root.
        candidate = next((root / p for root in roots if (root / p).exists()), None)
        p = candidate or (roots[0] / p if roots else p)
    if not _inside(p, roots):
        raise FileListError("forbidden", "path is outside the allowed repos")
    return p


def list_files(path: Path | str, images_only: bool = False,
               settings: Optional[dict] = None) -> dict:
    """Return a bounded, read-only listing from the managed-path sandbox.

    ``scanned`` is the number of directory entries inspected and ``found`` is
    the number matching the filter during that scan. ``truncated`` is
    conservative when a bound is reached; callers must not use a truncated
    listing as the basis for a destructive follow-up.
    """
    roots = allowed_roots(settings if settings is not None else load_settings())
    p = _managed_path(path, roots)
    if not p.exists():
        raise FileListError("not_found", "path does not exist")
    if not p.is_dir():
        raise FileListError("not_directory", "path is not a directory")

    items = []
    scanned = 0
    found = 0
    truncated = False
    # Account for each encoded item once instead of serializing the growing
    # result for every entry. Reserve the maximum digit width for counters and
    # the larger boolean token so the final result remains within the bound.
    try:
        dir_encoded_size = len(json.dumps(str(p), ensure_ascii=False).encode("utf-8"))
    except (TypeError, UnicodeError, ValueError) as error:
        raise FileListError("unreadable", "could not encode directory listing") from error
    counter_width = len(str(max(FILE_LIST_MAX_SCANNED, FILE_LIST_MAX_ITEMS)))
    fixed_size = (len(b'{"dir":') + dir_encoded_size
                  + len(b',"exists":true,"items":[')
                  + len(b'],"truncated":false,"scanned":') + counter_width
                  + len(b',"found":') + counter_width + len(b'}'))
    item_bytes = 0
    try:
        iterator = iter(p.iterdir())
        for _ in range(FILE_LIST_MAX_SCANNED):
            try:
                child = next(iterator)
            except StopIteration:
                break
            scanned += 1
            # Never disclose an entry whose symlink resolves outside the
            # managed roots, even when the directory itself is safe.
            if not _inside(child, roots):
                truncated = True
                continue
            try:
                is_dir = child.is_dir()
                if images_only and (is_dir or not child.is_file() or not is_image_file(child)):
                    continue
                size = 0 if is_dir else child.stat().st_size
            except (OSError, ValueError):
                # The caller cannot safely treat an unreadable entry as absent,
                # especially when the listing gates a destructive follow-up.
                truncated = True
                continue
            found += 1
            if len(items) >= FILE_LIST_MAX_ITEMS:
                truncated = True
                continue
            item = {"name": child.name, "dir": is_dir, "size": size, "path": str(child)}
            try:
                encoded_item_size = len(json.dumps(
                    item, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8"))
            except (TypeError, UnicodeError, ValueError):
                truncated = True
                continue
            projected = fixed_size + item_bytes + encoded_item_size + (1 if items else 0)
            if projected > FILE_LIST_MAX_RESULT_BYTES:
                truncated = True
                break
            items.append(item)
            item_bytes += encoded_item_size + (1 if len(items) > 1 else 0)
    except (OSError, ValueError) as error:
        raise FileListError("unreadable", "could not read directory") from error

    items.sort(key=lambda item: item["name"])
    # Reaching the scan ceiling is reported as truncated even for exactly that
    # many entries: proving completeness requires one extra unbounded probe.
    if scanned >= FILE_LIST_MAX_SCANNED:
        truncated = True
    result = {"dir": str(p), "exists": True, "items": items,
              "truncated": bool(truncated), "scanned": scanned, "found": found}
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise FileListError("unreadable", "could not encode directory listing") from error
    if len(encoded) > FILE_LIST_MAX_RESULT_BYTES:
        # Keep the final guard for unusual filesystem changes or counter widths;
        # it is not part of the normal per-item accounting path.
        result["items"] = []
        result["truncated"] = True
    return result


# Descriptive alias retained for callers that used the HTTP helper name while
# the native contract identifies this operation as file.list.
list_managed_directory = list_files


def reveal_path(path: Path) -> Optional[str]:
    """Reveal a path in the platform file manager. Returns an error string or None."""
    if not path.exists():
        return "path does not exist"
    try:
        if os.name == "nt":
            if path.is_dir():
                subprocess.Popen(["explorer", str(path)], **_external_proc_kwargs())
            else:
                # Explorer's /select, action reveals the file without opening
                # it in whatever application is associated with its suffix.
                subprocess.Popen(["explorer", f"/select,{path}"],
                                 **_external_proc_kwargs())
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if not path.is_dir() else "", str(path)] if not path.is_dir()
                             else ["open", str(path)], **_external_proc_kwargs())
        else:
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)],
                             **_external_proc_kwargs())
        return None
    except Exception as e:
        return str(e)


def open_path(path: Path) -> Optional[str]:
    """Open a file in the platform's default application (double-click semantics).

    Unlike reveal_path this launches the file itself — e.g. a .studio3 cutting
    template opens in Silhouette Studio if it is installed. Returns an error
    string or None."""
    if not path.exists():
        return "path does not exist"
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], **_external_proc_kwargs())
        else:
            subprocess.Popen(["xdg-open", str(path)], **_external_proc_kwargs())
        return None
    except Exception as e:
        return str(e)


def open_url(url: str) -> Optional[str]:
    """Open a URL in the platform's default browser (the link twin of
    open_path). The UI's webview can't window.open, so its link buttons go
    through here instead. Returns an error string or None."""
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], **_external_proc_kwargs())
        else:
            subprocess.Popen(["xdg-open", url], **_external_proc_kwargs())
        return None
    except Exception as e:
        return str(e)


# ============================================================================
# HTTP layer
# ============================================================================

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".dxf": "application/dxf",
    ".studio3": "application/octet-stream",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}


def _content_disposition(filename: str) -> str:
    """Return a header-safe legacy name plus the original RFC 5987 name."""
    # The quoted fallback is deliberately ASCII-only and excludes the header
    # delimiters most likely to be interpreted by old clients.  The extended
    # parameter preserves the user-visible name without putting controls in a
    # response header.
    fallback = "".join(
        char if 0x21 <= ord(char) <= 0x7E and char not in {'"', "\\", ";"}
        else "_"
        for char in filename
    ) or "download"
    encoded = quote(filename, safe="!#$&+-.^_`|~", encoding="utf-8", errors="replace")
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def resolve_template(paper: str, card: str, borderless: bool,
                     settings: Optional[dict] = None) -> dict:
    """Resolve one upstream cutting template for both HTTP and native IPC."""
    settings = settings if settings is not None else load_settings()
    paper = paper.strip().lower()
    card = card.strip().lower()
    if not (paper and card):
        return {"ok": False, "errors": ["needs both a card size and a paper size"]}

    # get_info remains authoritative for card ownership and source precedence;
    # this helper only locates the file selected by that upstream metadata.
    info = get_info()
    scm_root, extras_root = effective_dirs(settings)
    cards = {}
    for c in info.get("scm", {}).get("card_sizes", []):
        cards.setdefault(c["name"].lower(), c)
    for c in info.get("extras", {}).get("card_sizes", []):
        cards[c["name"].lower()] = c
    c = cards.get(card)
    if c is None:
        return {"ok": False, "errors": [f"no card size named “{card}” in either repo"]}

    sub = "borderless" if borderless else ""
    if c.get("source") == "extras":
        probes = [("scm-extras", extras_root and extras_root / "cutting_templates" / sub),
                  ("silhouette-card-maker", scm_root and scm_root / "cutting_templates" / sub)]
    else:
        probes = [("silhouette-card-maker", scm_root and scm_root / "cutting_templates" / sub),
                  ("scm-extras", extras_root and extras_root / "cutting_templates" / sub)]
    roots = allowed_roots(settings)
    fam = " (borderless)" if borderless else ""
    infix = "-borderless" if borderless else ""
    for repo, directory in probes:
        if not directory or not directory.is_dir() or not _inside(directory, roots):
            continue
        best, best_v = None, -1
        pat = re.compile(rf"^{re.escape(paper)}-{re.escape(card)}{re.escape(infix)}-v(\d+)\.studio3$")
        try:
            for candidate in directory.iterdir():
                # A matching symlink is not a safe template unless it resolves
                # within one of the managed roots.
                if not _inside(candidate, roots) or not candidate.is_file():
                    continue
                m = pat.fullmatch(candidate.name)
                if m and int(m.group(1)) > best_v:
                    best, best_v = candidate, int(m.group(1))
        except OSError:
            continue
        if best is not None:
            return {"ok": True, "name": best.name, "path": str(best), "repo": repo}
    return {"ok": False, "errors": [
        f"no cutting template for {paper} + {card}{fam} in either repo "
        f"(looked for {paper}-{card}{'-borderless' if borderless else ''}-v*.studio3)"]}


class Handler(BaseHTTPRequestHandler):
    server_version = f"SCMWorkbench/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[workbench] %s\n" % (fmt % args))

    # ---- plumbing ----

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: list = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200):
        self._send(code, json.dumps(obj, default=str).encode("utf-8"))

    def _body(self, *, strict: bool = False):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return None if strict else {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None if strict else {}

    # ---- GET ----

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        q = parse_qs(url.query)
        try:
            if path == "/smoke":
                # Build/CI proof that a real WKWebView can reach this server.
                # curl reaching /api/info proves the worker; this proves the
                # webview (the piece three consecutive releases shipped
                # broken because nothing in CI ever looked at it): the Tauri
                # shell points its window at this origin, and this endpoint
                # answers only a connection the webview itself opens.
                return self._json({
                    "smoke": "ok",
                    "version": SERVER_VERSION,
                    "client": self.headers.get("User-Agent", ""),
                })
            if path in ("/", "/index.html"):
                ua = self.headers.get("User-Agent", "")
                if "AppleWebKit" in ua:
                    # A WebKit session on the index: the worker says so at
                    # request time. This is the marker the CI smoke test
                    # greps for, and on a real machine it is the answer to
                    # "is the window on the UI?" - the app's own webview,
                    # not curl.
                    sys.stderr.write("[workbench] webview session on / (WebKit)\n")
                return self._static("index.html")
            if path.startswith("/ui/"):
                return self._static(path[4:])
            # Embedded Tauri assets live at the app-origin root. Serve those
            # same bounded paths in standalone-browser mode so one HTML tree
            # works in both environments; retain /ui/* for old bookmarks.
            if path.startswith("/js/") or path in (
                    "/favicon.svg", "/logo.svg", "/theme.css"):
                return self._static(path[1:])
            if path == "/up":
                # Liveness probe: the app window's “starting” page polls this
                # until the server is ready, then navigates to the real UI. The
                # permissive CORS header keeps that fetch reliable in WKWebView.
                return self._send(200, _GIF_1PX, ctype="image/gif",
                                  extra=[("Access-Control-Allow-Origin", "*")])
            if path == "/api/info":
                return self._json(get_info())
            if path == "/api/release-notes":
                tags = parse_qs(url.query, keep_blank_values=True).get("tag")
                if tags is not None and (len(tags) != 1 or not tags[0] or
                        len(tags[0].encode("utf-8")) > 128):
                    return self._json({"ok": False, "error": "invalid release tag"}, 400)
                if tags is None:
                    result = release_notes_view()
                else:
                    result = _update_notes_result(tags[0])
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/repos":
                return self._json({"repos": repos_view(load_settings())})
            if path == "/api/manifest":
                return self._json(get_manifest())
            if path == "/api/jobs":
                return self._json(list_jobs())
            m = re.fullmatch(r"/api/jobs/([\w-]+)/log", path)
            if m:
                jid = m.group(1)
                if not (_job_record(jid) or _persisted_record(jid)):
                    return self._json({"error": "job not found"}, 404)
                try:
                    after = int((q.get("after") or ["0"])[0])
                    max_lines = int((q.get("max_lines") or [str(JOB_LOG_MAX_LINES)])[0])
                    if after < 0 or not (1 <= max_lines <= JOB_LOG_MAX_LINES):
                        raise ValueError
                except (TypeError, ValueError):
                    return self._json({"error": "invalid log cursor"}, 400)
                return self._json(get_job_log(jid, after, max_lines))
            m = re.fullmatch(r"/api/jobs/([\w-]+)/stream", path)
            if m:
                after = int((q.get("after") or ["0"])[0])
                return self._sse(m.group(1), after)
            if path == "/api/file":
                return self._file(q)
            if path == "/api/template":
                return self._template(q)
            if path == "/api/preview":
                return self._preview(q)
            if path == "/api/settings":
                return self._json(load_settings())
            if path == "/api/updates":
                return self._json(updates_view())
            if path.startswith("/api/"):
                return self._json({"error": f"no such route: {path}"}, 404)
            # SPA routes (/pdf, /settings, ...): serve the app shell and let
            # the client pick the page from the URL, so a refresh stays put.
            return self._static("index.html")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    # ---- POST ----

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/api/jobs":
                body = self._body()
                job, errors = start_job(str(body.get("kind", "")), body.get("args") or {})
                if errors:
                    return self._json({"ok": False, "errors": errors}, 400)
                return self._json({"ok": True, "job": {
                    "id": job["id"], "title": job["title"], "status": job["status"],
                    "cmd": job["cmd"], "warnings": job.get("warnings", []),
                }})
            m = re.fullmatch(r"/api/jobs/([\w-]+)/kill", path)
            if m:
                return self._json({"ok": kill_job(m.group(1))})
            if path == "/api/updates/check":
                body = self._body(strict=True)
                if not isinstance(body, dict) or set(body) != {"force"} or not isinstance(body["force"], bool):
                    return self._json({"ok": False, "errors": [
                        "update check requires exactly boolean force"]}, 400)
                return self._json(_update_check_result(body["force"]))
            if path == "/api/updates/start":
                body = self._body(strict=True)
                if not isinstance(body, dict) or body != {}:
                    return self._json({"ok": False, "errors": [
                        "update start requires exactly an empty object"]}, 400)
                result = update_start_result()
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/settings":
                # HTTP keeps its historical direct-patch body shape, while the
                # native method wraps the same patch as {changes: ...}.  Invalid
                # patches are application results with HTTP 400, matching the
                # native result exactly; no invalid patch reaches save_settings.
                body = self._body()
                result = update_settings(body)
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/repos/save":
                body = self._body()
                key = body.get("repo") if isinstance(body, dict) else None
                source = body.get("source") if isinstance(body, dict) else None
                if not isinstance(key, str) or key not in repo_sync.REPOS:
                    return self._json({"ok": False, "errors": ["unknown repository"]}, 400)
                try:
                    source = repo_sync.validate_source(source)
                except repo_sync.RepoError as exc:
                    return self._json({"ok": False, "errors": [_bounded_error(exc)]}, 400)
                result = repo_source_result(key, source)
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/repos/check":
                body = self._body()
                key = body.get("repo") if isinstance(body, dict) else None
                force = body.get("force") if isinstance(body, dict) else None
                if not isinstance(key, str) or key not in repo_sync.REPOS:
                    return self._json({"ok": False, "errors": ["unknown repository"]}, 400)
                if not isinstance(force, bool):
                    return self._json({"ok": False, "errors": ["force must be boolean"]}, 400)
                result = repo_check_result(key, force)
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/repos/refs":
                body = self._body()
                key = body.get("repo") if isinstance(body, dict) else None
                if not isinstance(key, str) or key not in repo_sync.REPOS:
                    return self._json({"ok": False, "errors": ["unknown repository"]}, 400)
                result = repo_refs_result(key)
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/decklists/import":
                # A packaged worker must only accept the path delivered over
                # the native command. Standalone browser mode keeps this
                # compatibility route for callers which already have a path;
                # the UI never exposes a browser picker.
                if _IPC_MODE:
                    return self._json({"ok": False, "errors": [
                        "native picker required for packaged decklist import"]}, 400)
                body = self._body()
                src = body.get("path") if isinstance(body, dict) else None
                try:
                    result = import_decklist(src)
                except DecklistImportError as exc:
                    return self._json({"ok": False, "errors": [exc.message]}, 400)
                return self._json(result)
            if path == "/api/offset":
                body = self._body()
                if not isinstance(body, dict):
                    return self._json({"ok": False, "errors": ["offset body must be an object"]}, 400)
                size = body.get("size") if "size" in body else None
                if body.get("delete"):
                    if set(body) != {"size", "delete"} or not isinstance(size, str) or not size.strip():
                        return self._json({"ok": False, "errors": ["no paper size to remove"]}, 400)
                    result = delete_offset(size.strip())
                else:
                    if set(body) - {"size", "x", "y", "angle"} or not {"x", "y", "angle"}.issubset(body):
                        return self._json({"ok": False, "errors": ["offset requires exactly size, x, y, and angle"]}, 400)
                    def http_number(value, integer=False):
                        if isinstance(value, bool):
                            raise ValueError
                        if isinstance(value, (int, float)):
                            number = value
                        elif isinstance(value, str) and value.strip():
                            number = float(value.strip())
                        else:
                            number = 0
                        if integer:
                            if not math.isfinite(float(number)) or float(number) != int(float(number)):
                                raise ValueError
                            return int(number)
                        number = float(number)
                        if not math.isfinite(number):
                            raise ValueError
                        return number
                    try:
                        x = http_number(body.get("x", 0), integer=True)
                        y = http_number(body.get("y", 0), integer=True)
                        angle = http_number(body.get("angle", 0))
                    except (TypeError, ValueError, OverflowError):
                        return self._json({"ok": False, "errors": ["offset values must be numbers"]}, 400)
                    result = set_offset(size.strip() if isinstance(size, str) else size, x, y, angle)
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/files/save":
                if _IPC_MODE:
                    return self._json({"ok": False, "errors": [
                        "native artifact export is required in packaged mode"]}, 403)
                # Standalone browser compatibility is deliberately separate
                # from grants: the browser has no picker and may only use this
                # explicit legacy route. It still rejects symlink sources,
                # refuses mkdir/overwrite, and bounds the copy.
                body = self._body()
                src = os.path.expanduser(str(body.get("src") or "").strip())
                dest = os.path.expanduser(str(body.get("dest") or "").strip())
                errors = []
                if not src or not os.path.isfile(src):
                    errors.append("The file to move doesn't exist (yet).")
                if not dest:
                    errors.append("No destination chosen.")
                if errors:
                    return self._json({"ok": False, "errors": errors}, 400)
                # the source must live in a workbench-managed location; the
                # destination is anywhere the user pointed the save panel at.
                try:
                    if not _inside(Path(src), allowed_roots(load_settings())):
                        return self._json({"ok": False, "errors": ["that file isn't in a workbench-managed location"]}, 403)
                except Exception:
                    return self._json({"ok": False, "errors": ["could not validate the source location"]}, 403)
                try:
                    source_path = Path(src)
                    st = os.stat(source_path, follow_symlinks=False)
                    if _is_reparse_or_symlink(os.lstat(source_path)) or not stat.S_ISREG(st.st_mode):
                        return self._json({"ok": False, "errors": ["that source is not a stable regular file"]}, 400)
                    if st.st_size > ARTIFACT_MAX_BYTES:
                        return self._json({"ok": False, "errors": ["the source is too large"]}, 400)
                    if os.name == "nt":
                        # A path stat on Windows can report the volume's cached
                        # directory-entry timestamps, which lag the file's true
                        # MFT times after a fresh write.  The export guard
                        # compares identities taken from an open handle, so take
                        # this snapshot the same way to keep the two sides in
                        # the same timestamp space.
                        probe_fd, st = _open_windows_regular_file(source_path)
                        os.close(probe_fd)
                        if st.st_size > ARTIFACT_MAX_BYTES:
                            return self._json({"ok": False, "errors": ["the source is too large"]}, 400)
                    parent = Path(dest).parent
                    if not parent.is_dir() or parent.is_symlink():
                        return self._json({"ok": False, "errors": ["destination folder must already exist"]}, 400)
                    result = _copy_artifact({"path": str(source_path.resolve()), "root": str(source_path.parent.resolve()),
                                             "name": source_path.name, **_artifact_identity(st)}, dest)
                    return self._json(result, 200 if result.get("ok") else 400)
                except Exception as e:
                    return self._json({"ok": False, "errors": [f"Could not copy the file: {e}"]}, 400)
            if path == "/api/reveal":
                body = self._body()
                result, status = file_reveal_action(body.get("path"), load_settings())
                return self._json(result, status)
            if path == "/api/fs":
                # Packaged content has one owner for this destructive action.
                # Reject before reading or resolving its path so HTTP cannot be
                # used as a native fallback or as a second validation route.
                if _IPC_MODE:
                    return self._json({"ok": False, "errors": [
                        "native image deletion is required in packaged mode"]}, 403)
                body = self._body()
                if not isinstance(body, dict) or body.get("op") != "delete_images":
                    return self._json({"ok": False, "errors": [
                        f"unknown op \u201c{body.get('op') if isinstance(body, dict) else None}\u201d"]}, 400)
                raw = body.get("path")
                try:
                    result = _delete_images_impl(raw, load_settings())
                except ImageDeleteError as error:
                    return self._json(error.body(), error.status)
                return self._json(result)
            return self._json({"error": f"no such route: {path}"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    # ---- handlers ----

    def _static(self, rel: str):
        p = (UI_DIR / rel).resolve()
        if not _inside(p, [UI_DIR]):
            return self._json({"error": "forbidden"}, 403)
        if not p.is_file():
            return self._json({"error": "not found"}, 404)
        data = p.read_bytes()
        ctype = MIME.get(p.suffix.lower(), "application/octet-stream")
        if rel == "index.html":
            # Version the asset URLs (?v=<mtime>): the app window's webview keeps a
            # persistent URL cache, and bare /ui/... paths can hand a stale
            # theme.css/app.js to a window long after a redeploy. A new mtime on
            # deploy = a new URL = a guaranteed cache miss, so any window that
            # (re)loads after an update is guaranteed to see the new UI.
            def _v(name: str) -> str:
                try:
                    return str(int((UI_DIR / name).stat().st_mtime))
                except Exception:
                    return "0"
            text = data.decode("utf-8")
            for name in ("theme.css", "js/app.js", "favicon.svg"):
                text = text.replace(f"/ui/{name}", f"/ui/{name}?v={_v(name)}")
            data = text.encode("utf-8")
        self._send(200, data, ctype)

    def _file(self, q):
        # Packaged content has native owners for every file surface.  Reject
        # before settings, path resolution, action dispatch, or file reads so
        # an unrecognised query cannot become a filesystem capability.
        if _IPC_MODE:
            return self._json({"ok": False, "errors": [
                "native file access is required in packaged mode"]}, 403)
        url = (q.get("url") or [""])[0]
        if url:
            # Same open semantics as open=1, aimed at a link: the server
            # opens it in the default browser. Validation is shared with the
            # native RPC action.
            result, status = url_open_action(url)
            return self._json(result, status)
        settings = load_settings()
        rel = (q.get("path") or [""])[0]
        reveal = (q.get("reveal") or ["0"])[0] == "1"
        do_open = (q.get("open") or ["0"])[0] == "1"
        if not rel:
            return self._json({"error": "no path"}, 400)
        # Action requests use the same canonical resolver as native RPC. Keep
        # this ahead of the legacy raw-read path so action errors retain the
        # established {ok, errors} shape instead of the read route's {error}.
        if do_open:
            result, status = file_open_action(rel, settings)
            return self._json(result, status)
        if reveal:
            result, status = file_reveal_action(rel, settings)
            return self._json(result, status)

        roots = allowed_roots(settings)
        p = Path(rel)
        if not p.is_absolute():
            cand = next((r / rel for r in roots if (r / rel).exists()), None)
            p = cand or (roots[0] / rel if roots else rel)
        if not _inside(p, roots):
            return self._json({"error": "path outside sandbox"}, 403)
        if p.is_dir():
            try:
                listing = list_files(
                    p, (q.get("images_only") or [""])[0] == "1", settings)
            except FileListError as error:
                status = 403 if error.code == "forbidden" else 404
                return self._json({"error": "path outside sandbox" if status == 403 else "not found"}, status)
            return self._json(listing)
        if not p.is_file():
            return self._json({"error": "not found"}, 404)
        data = p.read_bytes()
        self._send(200, data, MIME.get(p.suffix.lower(), "application/octet-stream"),
                   [("Content-Disposition", _content_disposition(p.name))])

    def _template(self, q):
        """Resolve a cutting template using the shared HTTP/native helper."""
        settings = load_settings()
        paper = (q.get("paper") or [""])[0]
        card = (q.get("card") or [""])[0]
        borderless = (q.get("borderless") or ["0"])[0] == "1"
        return self._json(resolve_template(paper, card, borderless, settings))

    def _preview(self, q):
        kind = (q.get("kind") or [""])[0]
        args_json = (q.get("args") or ["{}"])[0]
        # A wiped form slot serializes as the literal string "undefined" from
        # a stale client; treat that and the old literal "null" as defaults.
        if args_json in ("undefined", "null"):
            args_json = "{}"
        try:
            args = json.loads(args_json)
        except Exception:
            return self._json({"error": "bad args"}, 400)
        try:
            result = build_preview(kind, args)
        except PreviewError as error:
            return self._json(error.http_body, error.status)
        return self._json(result)

    def _sse(self, jid: str, after: int):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for event, data in sse_stream(jid, after):
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ============================================================================
# Entry point
# ============================================================================

def _in_wsl() -> bool:
    import os
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _wsl_vm_ip():
    """Best-effort IP address of this WSL2 VM as seen from the Windows host.

    In the default NAT networking mode this is the 172.x address WSL hands
    the VM; traffic from the host to it is direct (no per-connection
    localhost proxying, which is the flaky part). Returns None when it
    can't be determined (e.g. mirrored networking mode).
    """
    import ipaddress
    import subprocess

    def clean(cand):
        try:
            ip = ipaddress.ip_address(cand)
            if not ip.is_loopback and not ip.is_link_local:
                return str(ip)
        except (ValueError, TypeError):
            pass
        return None

    for cmd in (["ip", "-4", "route", "get", "1.1.1.1"], ["hostname", "-I"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        tokens = []
        if cmd[0] == "ip":
            # "1.1.1.1 via 172.28.160.1 dev eth0 src 172.28.160.100"
            if "src" in out:
                tokens = [out.rsplit("src", 1)[1].strip().split()[0]]
        else:
            tokens = out.split()
        for t in tokens:
            if c := clean(t):
                return c
    return None


def _open_browser(url: str) -> None:
    """Open the Workbench URL in the user's browser, quietly.

    The stdlib webbrowser hands the URL to an OS launcher (xdg-open, gio,
    open, ...) whose child inherits this server's stderr — so a machine
    with no web app configured (the usual WSL2 case) spits raw output
    like “gio: <url>: Operation not supported” into the console. We run
    the candidate launchers ourselves with output silenced instead: the
    first one that succeeds wins, and if none does we print one friendly
    line.
    """
    import os
    import shlex
    import subprocess

    # Small helpers whose exit code is trustworthy: 0 = a handler was
    # launched, non-zero = nothing was opened.
    reliable = {"xdg-open", "gio", "gvfs-open", "x-www-browser", "kfmclient", "kfm", "open"}

    def candidates():
        env_browser = (os.environ.get("BROWSER") or "").strip()
        if env_browser:
            try:
                parts = shlex.split(env_browser)
                if parts:
                    yield [p.replace("%s", url).replace("%u", url) for p in parts]
            except ValueError:
                pass
        if _in_wsl():
            # Windows-side launchers only: the user's browser lives in
            # Windows. (The Linux-side xdg-open/gio would just produce the
            # “no default web app” noise in a WSL session.)
            yield ["explorer.exe", url]
            yield ["cmd.exe", "/c", "start", "", url]
        elif sys.platform == "darwin":
            yield ["open", url]
        elif os.name != "nt":
            yield ["xdg-open", url]
            yield ["gio", "open", "--", url]

    for cmd in candidates():
        name = os.path.basename(cmd[0])
        try:
            if name in reliable:
                try:
                    rc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode
                except subprocess.TimeoutExpired:
                    return  # it may have forked a browser before hanging — don't fire another
                if rc == 0:
                    return
                # non-zero: this helper genuinely opened nothing — next candidate is safe
            else:
                # GUI shells (explorer.exe, cmd /c start, a browser binary
                # from $BROWSER) may open the browser and still exit
                # non-zero or linger, so waiting on them and retrying the
                # next candidate opens a *second* browser. A successful
                # spawn is success: leave the process and stop.
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except (FileNotFoundError, PermissionError):
            continue  # not present on this platform

    if os.name == "nt" and not _in_wsl():
        try:
            if webbrowser.open(url, new=2):
                return
        except Exception:
            pass
    _diag(f"  (Could not open a browser automatically — visit {url} manually.)")


class WorkbenchHTTPServer(ThreadingHTTPServer):
    """HTTP server that owns the same upstream children as the IPC server."""
    def server_close(self):
        stop_all_jobs()
        super().server_close()


def start_http(host: str, port: int) -> ThreadingHTTPServer:
    """Bind the UI server (no serve_forever — the caller runs it).

    Port 0 lets the OS pick a free one (window mode, where the settings
    port may be taken by something else).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        srv = WorkbenchHTTPServer((host, port), Handler)
    except OSError as e:
        # The one bind failure that matters, stated plainly: another process
        # already owns the port — usually a previous app instance whose window
        # was killed without a clean close (the app tries to reclaim such
        # ports itself at start; this is the last line of defence).
        _diag(
            f"\n  [server] could not bind {host}:{port} — another process already holds that port ({e}).\n"
            f"         Close the other SCM Workbench (or whatever else uses port {port}) and try again.\n",
        )
        sys.exit(1)
    srv.daemon_threads = True
    return srv


def main():
    global _IPC_MODE, _IPC_PROCESS_GROUP_READY
    ap = argparse.ArgumentParser(description="SCM Workbench — local UI for silhouette-card-maker + scm-extras")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None,
                    help="Address to bind (default: 127.0.0.1; on WSL all interfaces of the VM, "
                         "so the Windows host can also reach the server)")
    ap.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    ap.add_argument("--ipc", action="store_true",
                    help="Run the native newline-delimited JSON child protocol without HTTP")
    args = ap.parse_args()
    _IPC_MODE = bool(args.ipc)

    # Make the banner (and any traceback) robust on *any* stream: a freshly
    # spawned Windows child defaults its stdio to the machine's ANSI codepage
    # (e.g. cp1252), which cannot encode the box-drawing characters in the
    # banner below — the write used to raise UnicodeEncodeError before the port
    # was ever bound, so the UI never appeared. `errors="replace"` means a
    # hostile codepage can never kill the server at startup; the worst case is
    # a couple of '?' glyphs where a fancy character used to be.
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass

    settings = load_settings()
    # The helper result is the only completion signal for an external handoff.
    # Reconcile it before serving jobs so a newly launched shell cannot admit
    # work while the old persisted update record is still marked handoff.
    try:
        reconcile_update_result()
    except Exception as exc:
        _diag(f"[updater] result reconciliation deferred: {exc}", error=True)
    # A process may have died after replacing canonical state but before its
    # SCM projection.  Roll that projection forward before serving requests.
    recovery_error = recover_offset_projection()
    if recovery_error:
        _diag(f"[offset] recovery deferred: {recovery_error}", error=True)
    port = args.port if args.port is not None else int(settings.get("port") or DEFAULT_PORT)
    scm, extras = effective_dirs(settings)

    host = args.host
    if host is None:
        # WSL2 NAT networking: bind all interfaces of the VM. The WSL
        # virtual network is only reachable from the Windows host, so this
        # is still "local only" — and now both 127.0.0.1 (from inside WSL)
        # and the VM's own address (from a Windows browser) work. Every other
        # platform keeps the loopback-only default.
        host = "0.0.0.0" if _in_wsl() else "127.0.0.1"

    def _start_background_tasks() -> None:
        # These tasks are independent of transport.  In IPC mode they remain
        # daemonized so the protocol reader is the worker's only lifetime
        # owner; EOF below performs the authoritative job shutdown.
        threading.Thread(target=_update_daemon, daemon=True, name="updater").start()
        if os.environ.get("SCM_WORKBENCH_PACKAGED") == "1" and not os.environ.get("SCM_WORKBENCH_NO_BOOTSTRAP"):
            from scm_workbench import bootstrap as _first_boot

            def _first_boot_then() -> None:
                _first_boot.run_first_boot(
                    DATA_DIR,
                    log=lambda message="": _diag(message),
                )
                invalidate_manifest_cache()

            threading.Thread(
                target=_first_boot_then, daemon=True, name="first-boot",
            ).start()

    if args.ipc:
        # IPC workers have no browser compatibility listener at all.  Keep
        # stdout exclusively for the bounded JSON-lines protocol and let EOF
        # own the same supervised job shutdown as the native shell.
        from scm_workbench import ipc

        def _ipc_eof() -> None:
            stop_all_jobs()

        # The parent's post-spawn setpgid is intentionally retained, but exec
        # can win that race on POSIX. The worker therefore establishes and
        # verifies its own group before acknowledging readiness. If startup
        # fails before here no upstream job has been admitted, so direct-child
        # cleanup remains sufficient.
        if os.name != "nt":
            try:
                os.setpgid(0, 0)
            except (AttributeError, OSError) as exc:
                # A worker already launched as a session/group leader may get
                # EPERM from the idempotent setpgid call; its observed group is
                # the security property, not that syscall's return value.
                if not (hasattr(os, "getpgrp") and os.getpgrp() == os.getpid()):
                    _diag(f"[ipc] process-group setup failed: {exc}", error=True)
                    raise SystemExit(1) from exc
            if os.getpgrp() != os.getpid():
                _diag("[ipc] process-group setup failed: worker is not group leader", error=True)
                raise SystemExit(1)
            _IPC_PROCESS_GROUP_READY = True
        if os.name == "nt":
            _IPC_PROCESS_GROUP_READY = True
        _start_background_tasks()
        try:
            ipc.serve_stdio(on_eof=_ipc_eof)
        finally:
            stop_all_jobs()
        return

    # Standalone mode retains the ordinary HTTP/browser path unchanged.
    server = start_http(host, port)
    actual_port = server.server_address[1]

    out = io.StringIO()
    w = out.write
    w("\n")
    w("  \x1b[1;1mSCM Workbench\x1b[0m  v%s\n" % SERVER_VERSION)
    w("  ───────────────────────────────────────────────────────\n")
    w("  SCM repo:      %s\n" % (scm if scm else "\x1b[31mnot found — point Settings at it\x1b[0m"))
    w("  Extras repo:   %s\n" % (extras if extras else "\x1b[33mnot found (optional)\x1b[0m"))
    w("  Python:        %s\n" % sys.version.split()[0])
    w("  ───────────────────────────────────────────────────────\n")
    url = f"http://{('127.0.0.1' if host == '0.0.0.0' else host)}:{actual_port}"
    w(f"  UI:  {url}\n")
    browser_url = url
    if _in_wsl():
        vm_ip = _wsl_vm_ip()
        if vm_ip:
            browser_url = f"http://{vm_ip}:{actual_port}"
            w(f"  Windows host:  {browser_url}  (your default browser opens here)\n")
        else:
            w("  Windows host:  use the 127.0.0.1 URL above (mirrored networking mode)\n")
    if _in_wsl():
        w("\n  Binds the WSL VM — reachable from the Windows host "
          "(and from your LAN only in mirrored networking mode). Ctrl+C to stop.\n")
    else:
        w("\n  Local only — not exposed to your network. Ctrl+C to stop.\n")
    _diag(out.getvalue())

    # Record this server's pid only for standalone HTTP mode.  An IPC worker
    # has no listener for a future launcher to reclaim.
    try:
        (DATA_DIR / "server.pid").write_text(str(os.getpid()), encoding="ascii")
    except Exception:
        pass

    _start_background_tasks()

    if not args.no_browser and settings.get("auto_open_browser", True):
        threading.Timer(0.4, _open_browser, args=(browser_url,)).start()

    try:
        # Preserve the ordinary HTTP server path exactly.
        server.serve_forever()
    except KeyboardInterrupt:
        _diag("\nBye.")
    finally:
        stop_all_jobs()
        server.server_close()


if __name__ == "__main__":
    main()
