"""Core storage and filesystem primitives for trusted image processors.

This module deliberately has no dependency on :mod:`server`: the registry and
image transaction code are usable by both the HTTP and native workers and can
be exercised with a temporary data directory.  Python processors are *trusted
local code*, not a security sandbox.  The runner is a separate process and all
input/output files are bounded and validated before publication.
"""
from __future__ import annotations

import ast
import hashlib
import itertools
import json
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

CONTRACT_VERSION = "1"
INTERPRETER_FINGERPRINT_SCHEMA = 2
SOURCE_MAX_BYTES = 256 * 1024
NAME_MAX_BYTES = 96
REQUIREMENT_LINE_MAX_BYTES = 256
REQUIREMENTS_MAX_BYTES = 8 * 1024
REQUIREMENTS_MAX_COUNT = 32
PROCESSOR_MAX_COUNT = 64
BUNDLED_PROCESSOR_MAX_COUNT = 8
PROCESSOR_STORAGE_MAX_COUNT = PROCESSOR_MAX_COUNT + BUNDLED_PROCESSOR_MAX_COUNT
REVISIONS_MAX_COUNT = 20
SAVED_SOURCE_MAX_BYTES = 8 * 1024 * 1024
SCAN_MAX_ENTRIES = 8192
IMAGE_MAX_COUNT = 1024
IMAGE_MAX_BYTES = 64 * 1024 * 1024
IMAGE_TOTAL_MAX_BYTES = 8 * 1024 * 1024 * 1024
PATH_MAX_BYTES = 4096
NAME_MAX_FILE_BYTES = 255
RUN_MAX_BYTES = IMAGE_TOTAL_MAX_BYTES * 2
FREE_SPACE_RESERVE_BYTES = 512 * 1024 * 1024
OUTPUT_MAX_BYTES = IMAGE_MAX_BYTES
METADATA_MAX_BYTES = 512 * 1024
JOURNAL_MAX_BYTES = 16 * 1024 * 1024

_RECOVERY_LOCK = threading.RLock()
_RECOVERED_ROOTS: set[str] = set()
_INTERPRETER_PROBE_LOCK = threading.RLock()
_INTERPRETER_PROBE_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_ENVIRONMENT_MIGRATION_LOCK = threading.RLock()

_FORMATS = ("png", "jpeg", "gif", "webp", "bmp")
_EXT_FORMAT = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp", ".bmp": "bmp"}
_IMAGE_ROLES = frozenset({"front", "double_sided", "back"})


class PostProcessingError(Exception):
    """Base class for bounded, user-facing post-processing errors."""


class ValidationError(PostProcessingError):
    pass


class ConflictError(PostProcessingError):
    pass


class NotFoundError(PostProcessingError):
    pass


class IntegrityError(PostProcessingError):
    pass


class CancelledError(PostProcessingError):
    pass


class TransactionError(PostProcessingError):
    def __init__(self, message: str, *, committed: bool = False,
                 rollback_safe: bool = True):
        super().__init__(message)
        self.committed = committed
        self.rollback_safe = rollback_safe


def _utf8(value: Any, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"invalid {label}")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"invalid {label} encoding") from exc
    if len(raw) > limit or (not raw and not empty):
        raise ValidationError(f"invalid {label} length")
    if "\x00" in value or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValidationError(f"invalid {label} control character")
    return value


def _safe_component(value: str, label: str, limit: int = 128) -> str:
    value = _utf8(value, label, limit)
    if value != value.strip() or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValidationError(f"invalid {label}")
    return value


def _is_link_or_reparse(observed: os.stat_result) -> bool:
    return stat.S_ISLNK(observed.st_mode) or bool(getattr(observed, "st_reparse_tag", 0))


_WINDOWS_LIMIT_JOB: Any = None


def _apply_windows_job_limits(*, cpu_seconds: int, address_space: int, processes: int) -> bool:
    """Establish nested Job Object limits for a Windows helper process."""
    global _WINDOWS_LIMIT_JOB
    if os.name != "nt":
        return True
    if _WINDOWS_LIMIT_JOB is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            return False
        limits = ExtendedLimits()
        limits.BasicLimitInformation.PerProcessUserTimeLimit = max(1, cpu_seconds) * 10_000_000
        limits.BasicLimitInformation.ActiveProcessLimit = max(1, processes)
        limits.BasicLimitInformation.LimitFlags = 0x00000002 | 0x00000008 | 0x00000200 | 0x00002000
        limits.JobMemoryLimit = max(256 * 1024 * 1024, address_space)
        if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or
                not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
            kernel.CloseHandle(handle)
            return False
        _WINDOWS_LIMIT_JOB = handle
        return True
    except Exception:
        return False


def _private(path: Path, *, directory: bool = False) -> None:
    if os.name != "posix":
        return
    mode = 0o700 if directory else 0o600
    try:
        os.chmod(path, mode, follow_symlinks=False)
        observed = os.lstat(path)
    except (OSError, NotImplementedError) as exc:
        raise IntegrityError("could not establish private post-processing permissions") from exc
    if _is_link_or_reparse(observed) or stat.S_IMODE(observed.st_mode) != mode:
        raise IntegrityError("post-processing path permissions are not private")


def _no_links(path: Path, *, allow_missing_leaf: bool = False) -> None:
    """Reject links/reparse points in every component of an application path."""
    path = Path(os.path.abspath(path))
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for i, part in enumerate(parts):
        current /= part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and i == len(parts) - 1:
                return
            raise ValidationError("path component is missing")
        if _is_link_or_reparse(st):
            # macOS exposes the conventional temporary directory through a
            # /private alias; this is an OS path alias, not an application
            # controlled link and is safe to normalize.
            resolved = current.resolve(strict=False)
            if current.parent == Path(current.anchor) and resolved.parts[:2] == (current.anchor, "private"):
                current = resolved
                continue
            raise ValidationError("symbolic links and reparse points are not allowed")


def _bounded_children(path: Path, limit: int, label: str) -> list[Path]:
    with os.scandir(path) as scan:
        entries = list(itertools.islice(scan, limit + 1))
    if len(entries) > limit:
        raise IntegrityError(f"{label} contains too many entries")
    return [Path(entry.path) for entry in entries]


def _contained(root: Path, path: Path) -> Path:
    root = Path(root).resolve()
    candidate = Path(path)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValidationError("path escapes its root") from exc
    _no_links(candidate, allow_missing_leaf=True)
    return candidate


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, getattr(os, "O_DIRECTORY", os.O_RDONLY))
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError:
        pass


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    _no_links(path.parent, allow_missing_leaf=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _private(path.parent, directory=True)
    if os.path.lexists(path) and _is_link_or_reparse(os.lstat(path)):
        raise IntegrityError("refusing to replace a symbolic link or reparse point")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        try: tmp.unlink()
        except OSError: pass


def _atomic_json(path: Path, value: Mapping[str, Any], *, max_bytes: int = METADATA_MAX_BYTES) -> None:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > max_bytes:
        raise ValidationError("metadata is too large")
    _atomic_bytes(path, raw)


def _read_regular_bytes(path: Path, label: str, max_bytes: int) -> bytes:
    """Read one bounded private file without following or racing its pathname."""
    path = Path(path)
    try:
        before = os.lstat(path)
        if (_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode) or
                getattr(before, "st_nlink", 1) != 1):
            raise IntegrityError(f"invalid {label} file")
        if before.st_size > max_bytes:
            raise IntegrityError(f"{label} is too large")
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    getattr(before, "st_nlink", 1))
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                    getattr(opened, "st_nlink", 1)) != identity:
                raise IntegrityError(f"{label} changed while opening")
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after_read = os.fstat(fd)
        finally:
            os.close(fd)
        after = os.stat(path, follow_symlinks=False)
        if (len(raw) > max_bytes or
                (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns,
                 getattr(after_read, "st_nlink", 1)) != identity or
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 getattr(after, "st_nlink", 1)) != identity):
            raise IntegrityError(f"{label} changed while reading")
        return raw
    except IntegrityError:
        raise
    except OSError as exc:
        raise IntegrityError(f"could not read {label}") from exc


def _read_json(path: Path, label: str, *, missing: Any = None, max_bytes: int = METADATA_MAX_BYTES) -> Any:
    try:
        raw = _read_regular_bytes(path, label, max_bytes)
    except IntegrityError as exc:
        if not os.path.lexists(path) and isinstance(exc.__cause__, FileNotFoundError):
            return missing
        raise
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise IntegrityError(f"malformed {label}") from exc
    return value


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFC", _utf8(name, "processor name", NAME_MAX_BYTES))
    if not name.strip() or name != name.strip() or any(c in name for c in "/\\"):
        raise ValidationError("invalid processor name")
    return name


_REQ_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:\[([A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:,[A-Za-z0-9][A-Za-z0-9._-]{0,63})*)\])?(?:==([A-Za-z0-9][A-Za-z0-9._+!-]{0,127}))?$")


def normalize_requirements(requirements: str | Sequence[str] | None) -> tuple[str, ...]:
    """Normalize safe wheel dependency requests; never return pip options."""
    if requirements is None: lines: list[str] = []
    elif isinstance(requirements, str): lines = requirements.splitlines()
    elif isinstance(requirements, Sequence) and not isinstance(requirements, (bytes, bytearray)):
        lines = list(requirements)
    else: raise ValidationError("requirements must be text or a list")
    result: dict[str, str] = {}
    total = 0
    if len(lines) > REQUIREMENTS_MAX_COUNT: raise ValidationError("too many requirements")
    for raw in lines:
        if not isinstance(raw, str): raise ValidationError("invalid requirement")
        if not raw.strip(): continue
        line = raw.strip()
        size = len(line.encode("utf-8"))
        total += size + 1
        if size > REQUIREMENT_LINE_MAX_BYTES or total > REQUIREMENTS_MAX_BYTES: raise ValidationError("requirements are too large")
        if line.startswith("-") or any(c in line for c in "\r\n\x00") or any(x in line for x in ("://", "@", ";", "#")):
            raise ValidationError("requirement must be a package name or exact version pin")
        match = _REQ_RE.fullmatch(line)
        if not match: raise ValidationError("invalid requirement")
        name, extras, version = match.groups()
        canonical_name = name.lower().replace("_", "-").replace(".", "-")
        extra_part = "" if not extras else "[" + ",".join(sorted(x.lower() for x in extras.split(","))) + "]"
        value = canonical_name + extra_part + ("==" + version if version else "")
        key = canonical_name
        if key in result:
            raise ValidationError("duplicate or conflicting requirement")
        result[key] = value
    return tuple(sorted(result.values()))


def validate_source(source: str) -> bytes:
    """Validate a revision without importing or executing it."""
    try: raw = source.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc: raise ValidationError("source must be UTF-8 text") from exc
    if len(raw) > SOURCE_MAX_BYTES: raise ValidationError("source is too large")
    if b"\x00" in raw: raise ValidationError("source contains NUL")
    try: tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValidationError("source has invalid Python syntax") from exc
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "process_image"]
    if len(funcs) != 1 or isinstance(funcs[0], ast.AsyncFunctionDef): raise ValidationError("source must define exactly one synchronous process_image")
    fn = funcs[0]
    args = fn.args
    if args.vararg or args.kwarg or args.kwonlyargs or len(args.posonlyargs) + len(args.args) != 2 or args.defaults:
        raise ValidationError("process_image must accept exactly (image_path, context)")
    try:
        compile(tree, "<saved post-processor>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValidationError("source has invalid Python syntax") from exc
    return source.encode("utf-8")


def revision_digest(source: str, requirements: Sequence[str], contract: str = CONTRACT_VERSION) -> str:
    raw = validate_source(source)
    req = normalize_requirements(requirements)
    material = raw + b"\n\0" + json.dumps(req, separators=(",", ":")).encode() + b"\n\0" + contract.encode()
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class ImageRecord:
    role: str
    relative_path: str
    name: str
    source: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    format: str
    digest: str


def _format_from_header(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
    if head.startswith(b"\xff\xd8\xff"): return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")): return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP": return "webp"
    if head.startswith(b"BM"): return "bmp"
    return None


def _source_image_format(expected: str, head: bytes) -> str | None:
    actual = _format_from_header(head)
    if actual == expected:
        return actual
    # Some SCM fetchers save JPEG responses under .png names. Accept only
    # this observed mismatch, then pin the *real* decoded format throughout
    # staging and result validation. All other mismatches stay excluded.
    if expected == "png" and actual == "jpeg":
        return actual
    return None


def _image_dimensions(stream, fmt: str) -> tuple[int, int]:
    stream.seek(0)
    head = stream.read(64)
    if fmt == "png" and len(head) >= 24: return struct.unpack(">II", head[16:24])
    if fmt == "gif" and len(head) >= 10: return struct.unpack("<HH", head[6:10])
    if fmt == "bmp" and len(head) >= 26: return struct.unpack("<ii", head[18:26])[:2]
    if fmt == "webp" and len(head) >= 30:
        if head[12:16] == b"VP8X": return (1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little"))
    if fmt == "jpeg":
        stream.seek(2)
        while True:
            marker = stream.read(2)
            if len(marker) != 2: break
            while marker[0] != 0xFF: marker = bytes((marker[1],)) + stream.read(1)
            code = marker[1]
            if code in (0xD8, 0xD9): continue
            length_raw = stream.read(2)
            if len(length_raw) != 2: break
            length = struct.unpack(">H", length_raw)[0]
            if code in range(0xC0, 0xC4) or code in range(0xC5, 0xC8) or code in range(0xC9, 0xCC) or code in range(0xCD, 0xD0):
                body = stream.read(5)
                if len(body) == 5: return struct.unpack(">HH", body[1:5])
            stream.seek(max(0, length - 2), 1)
    raise IntegrityError("could not determine image dimensions")


def _decoded_dimensions(stream, fmt: str) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError:
        return _image_dimensions(stream, fmt)
    try:
        import warnings
        stream.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(stream) as image:
                actual = str(image.format or "").lower()
                if actual == "jpg": actual = "jpeg"
                if actual != fmt:
                    raise IntegrityError("image decoder format does not match its header")
                dimensions = tuple(image.size)
                image.verify()
                return dimensions
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError("image is not decodable") from exc


def _digest_stream(stream, *, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while True:
        if cancelled and cancelled(): raise CancelledError("post-processing was cancelled")
        chunk = stream.read(1024 * 1024)
        if not chunk: break
        digest.update(chunk)
    return digest.hexdigest()


def _digest_file(path: Path, *, cancelled: Callable[[], bool] | None = None) -> str:
    try:
        before = os.lstat(path)
        if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise IntegrityError("file is not a regular file")
        if before.st_size > OUTPUT_MAX_BYTES:
            raise IntegrityError("file is too large")
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity:
                raise IntegrityError("file changed while opening")
            digest = _digest_stream(stream, cancelled=cancelled)
            after_read = os.fstat(stream.fileno())
        after = os.stat(path, follow_symlinks=False)
        if ((after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns) != identity or
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity):
            raise IntegrityError("file changed while reading")
        return digest
    except (CancelledError, IntegrityError):
        raise
    except OSError as exc:
        raise IntegrityError("could not read regular file") from exc


def _stable_image(path: Path, root: Path, role: str, *, cancelled: Callable[[], bool] | None = None) -> ImageRecord | None:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0): return None
        if st.st_size > IMAGE_MAX_BYTES: raise ValidationError("image is too large")
        _utf8(path.name, "image name", NAME_MAX_FILE_BYTES)
        expected_format = _EXT_FORMAT.get(path.suffix.lower())
        if not expected_format: return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise IntegrityError("image changed while opening") from exc
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _is_link_or_reparse(opened) or not stat.S_ISREG(opened.st_mode):
                raise IntegrityError("image changed while opening")
            identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            if identity != (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns):
                raise IntegrityError("image changed while opening")
            head = stream.read(64)
            fmt = _source_image_format(expected_format, head)
            if fmt is None: return None
            width, height = _decoded_dimensions(stream, fmt)
            if width <= 0 or height <= 0 or width * height > 200_000_000:
                raise IntegrityError("image dimensions are outside the supported bounds")
            digest = _digest_stream(stream, cancelled=cancelled)
            after_read = os.fstat(stream.fileno())
        after = os.stat(path, follow_symlinks=False)
        if ((after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns) != identity or
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity):
            raise IntegrityError("image changed while reading")
        relative = path.relative_to(root).as_posix()
        if len(relative.encode()) > PATH_MAX_BYTES or len(path.name.encode()) > NAME_MAX_FILE_BYTES: raise ValidationError("image path is too long")
        return ImageRecord(role, relative, path.name, str(path), st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, fmt, digest)
    except FileNotFoundError: return None


def _scope_roles(scope: str) -> tuple[str, ...]:
    if scope == "both": return ("front", "double_sided")
    if not isinstance(scope, str) or scope not in _IMAGE_ROLES: raise ValidationError("invalid image scope")
    return (scope,)


def count_images(scm_root: str | Path, scope: str = "both") -> int:
    """Return a fast bounded inventory count for previews; execution revalidates fully."""
    root = Path(scm_root).resolve()
    roles = _scope_roles(scope)
    count = total = 0
    for role in roles:
        directory = root / "game" / role
        if not os.path.lexists(directory): continue
        _no_links(directory)
        try: entries = _bounded_children(directory, SCAN_MAX_ENTRIES, "image directory")
        except FileNotFoundError: continue
        except IntegrityError as exc: raise ValidationError("image directory has too many entries") from exc
        for path in entries:
            try:
                observed = os.lstat(path)
                if _is_link_or_reparse(observed) or not stat.S_ISREG(observed.st_mode):
                    continue
                _utf8(path.name, "image name", NAME_MAX_FILE_BYTES)
                expected_format = _EXT_FORMAT.get(path.suffix.lower())
                if not expected_format: continue
                identity = (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
                fd = os.open(path, flags)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity:
                        raise IntegrityError("image changed while counting")
                    head = os.read(fd, 64)
                    after_read = os.fstat(fd)
                finally:
                    os.close(fd)
                after = os.stat(path, follow_symlinks=False)
                if ((after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns) != identity or
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity):
                    raise IntegrityError("image changed while counting")
                if _source_image_format(expected_format, head) is None: continue
                if observed.st_size > IMAGE_MAX_BYTES: raise ValidationError("image is too large")
            except FileNotFoundError:
                continue
            count += 1; total += observed.st_size
            if count > IMAGE_MAX_COUNT or total > IMAGE_TOTAL_MAX_BYTES:
                raise ValidationError("image batch exceeds its limit")
    return count


def _natural_name_key(value: str) -> tuple:
    return tuple((1, int(part), len(part), part) if part.isdigit()
                 else (0, part.casefold(), part) for part in re.split(r"(\d+)", value))


def discover_images(scm_root: str | Path, scope: str = "both", *, cancelled: Callable[[], bool] | None = None) -> tuple[ImageRecord, ...]:
    root = Path(scm_root).resolve()
    roles = _scope_roles(scope)
    output: list[ImageRecord] = []
    total = 0
    for role in roles:
        directory = root / "game" / role
        if not os.path.lexists(directory): continue
        _no_links(directory)
        try: entries = _bounded_children(directory, SCAN_MAX_ENTRIES, "image directory")
        except FileNotFoundError: continue
        except IntegrityError as exc: raise ValidationError("image directory has too many entries") from exc
        for path in entries:
            if cancelled and cancelled(): raise CancelledError("post-processing was cancelled")
            if path.is_symlink(): continue
            record = _stable_image(path, root, role, cancelled=cancelled)
            if record:
                output.append(record); total += record.size
                if len(output) > IMAGE_MAX_COUNT or total > IMAGE_TOTAL_MAX_BYTES: raise ValidationError("image batch exceeds its limit")
    output.sort(key=lambda item: (0 if item.role == "front" else 1,
                                  _natural_name_key(item.name), item.relative_path))
    return tuple(output)


def stage_images(records: Sequence[ImageRecord], run_dir: str | Path, *, cancelled: Callable[[], bool] | None = None) -> tuple[dict, ...]:
    run = Path(run_dir); run.mkdir(parents=True, exist_ok=False, mode=0o700); _no_links(run); _private(run, directory=True)
    staged: list[dict] = []
    for index, record in enumerate(records, 1):
        if cancelled and cancelled():
            shutil.rmtree(run, ignore_errors=True)
            raise CancelledError("post-processing was cancelled")
        src = Path(record.source); destination = run / "work" / record.role / record.name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(destination.parent, directory=True)
        _no_links(destination.parent)
        _contained(run, destination)
        _no_links(src)
        try:
            before = os.stat(src, follow_symlinks=False)
            expected_identity = (record.device, record.inode, record.size, record.mtime_ns)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != expected_identity:
                raise IntegrityError("source changed before staging")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
            source_fd = os.open(src, flags)
            with os.fdopen(source_fd, "rb") as inp, destination.open("xb") as out:
                opened = os.fstat(inp.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != expected_identity:
                    raise IntegrityError("source changed while opening for staging")
                _private(destination)
                remaining = IMAGE_MAX_BYTES + 1
                until_space_check = 0
                while remaining:
                    if cancelled and cancelled(): raise CancelledError("post-processing was cancelled")
                    if until_space_check <= 0:
                        if shutil.disk_usage(run).free < FREE_SPACE_RESERVE_BYTES:
                            raise ValidationError("not enough free space to stage the image batch safely")
                        until_space_check = 64 * 1024 * 1024
                    chunk = inp.read(min(1024 * 1024, remaining));
                    if not chunk: break
                    out.write(chunk); remaining -= len(chunk); until_space_check -= len(chunk)
                after_read = os.fstat(inp.fileno())
                out.flush(); os.fsync(out.fileno())
            after = os.stat(src, follow_symlinks=False)
            if ((after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns) != expected_identity or
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != expected_identity or
                    _digest_file(destination, cancelled=cancelled) != record.digest):
                raise IntegrityError("source changed while staging")
        except Exception:
            shutil.rmtree(run, ignore_errors=True); raise
        staged.append({"role": record.role, "relative_path": record.relative_path, "name": record.name, "source": record.source, "staged": str(destination), "format": record.format, "digest": record.digest, "index": index, "total": len(records), "identity": [record.device, record.inode, record.size, record.mtime_ns]})
    return tuple(staged)


def validate_staged_results(entries: Sequence[Mapping[str, Any]], *, run_dir: str | Path) -> None:
    root = Path(run_dir).resolve(); expected: set[Path] = set(); total = 0
    for entry in entries:
        path = _contained(root, Path(str(entry["staged"])))
        expected.add(path)
        try:
            st = os.lstat(path)
            if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or
                    getattr(st, "st_nlink", 1) != 1):
                raise IntegrityError("processor result is not a private regular file")
            if st.st_size > OUTPUT_MAX_BYTES: raise IntegrityError("processor result is too large")
            identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity:
                    raise IntegrityError("processor result changed while opening")
                fmt = _format_from_header(stream.read(64))
                if fmt != entry.get("format"): raise IntegrityError("processor changed image format")
                width, height = _decoded_dimensions(stream, fmt)
                after_read = os.fstat(stream.fileno())
            after = os.stat(path, follow_symlinks=False)
            if ((after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns) != identity or
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity):
                raise IntegrityError("processor result changed while validating")
            if width <= 0 or height <= 0 or width * height > 200_000_000: raise IntegrityError("processor result dimensions are too large")
            total += st.st_size
        except FileNotFoundError as exc: raise IntegrityError("processor result is missing") from exc
    work = root / "work"
    expected_by_directory: dict[Path, set[Path]] = {}
    for path in expected:
        expected_by_directory.setdefault(path.parent, set()).add(path)
    children = _bounded_children(work, len(expected_by_directory), "processor work directory")
    observed_directories: set[Path] = set()
    for child in children:
        observed = os.lstat(child)
        if (_is_link_or_reparse(observed) or not stat.S_ISDIR(observed.st_mode) or
                child not in expected_by_directory):
            raise IntegrityError("processor created an unexpected directory or link")
        observed_directories.add(child)
    if observed_directories != set(expected_by_directory):
        raise IntegrityError("processor result directory is missing")
    for directory, expected_files in expected_by_directory.items():
        observed_files = set(_bounded_children(directory, len(expected_files), "processor result directory"))
        if observed_files != expected_files:
            raise IntegrityError("processor created an unexpected result")
    if total > IMAGE_TOTAL_MAX_BYTES: raise IntegrityError("processor results exceed the batch limit")


def _publish_exclusive(source: Path, destination: Path) -> None:
    """Atomically rename one same-directory file without overwriting a peer."""
    if sys.platform == "darwin":
        import ctypes
        import ctypes.util
        library = ctypes.util.find_library("c")
        libc = ctypes.CDLL(library or None, use_errno=True)
        renamex = libc.renamex_np
        renamex.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        renamex.restype = ctypes.c_int
        if renamex(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:  # RENAME_EXCL
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        return
    if os.name == "nt":
        import ctypes
        move = ctypes.windll.kernel32.MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
        move.restype = ctypes.c_int
        if not move(str(source), str(destination), 0x00000008):  # MOVEFILE_WRITE_THROUGH
            raise ctypes.WinError()
        return
    os.link(source, destination, follow_symlinks=False)
    source.unlink()


class PublicationTransaction:
    """Durable same-filesystem replace transaction with idempotent recovery."""
    def __init__(self, journal_root: str | Path, transaction_id: str | None = None, *, fault: Callable[[str], None] | None = None):
        self.root = Path(journal_root); self.root.mkdir(parents=True, exist_ok=True, mode=0o700); _no_links(self.root); _private(self.root, directory=True)
        self.id = transaction_id or uuid.uuid4().hex
        _safe_component(self.id, "transaction id", 64)
        self.journal = self.root / f"{self.id}.json"
        self.fault = fault

    def _write(self, data: dict) -> None: _atomic_json(self.journal, data, max_bytes=JOURNAL_MAX_BYTES)
    def _phase(self, phase: str) -> None:
        if self.fault: self.fault(phase)

    def publish(self, replacements: Sequence[tuple[str | Path, str | Path]], *,
                expected: Mapping[str, Sequence[int]] | None = None,
                expected_digests: Mapping[str, str] | None = None) -> None:
        if not (1 <= len(replacements) <= IMAGE_MAX_COUNT):
            raise ValidationError("publication image count is invalid")
        items = []
        destinations: set[Path] = set()
        stages: set[Path] = set()
        checkout_root: Path | None = None
        for destination, staged in replacements:
            raw_dest = Path(destination); raw_stage = Path(staged)
            if not raw_dest.is_absolute() or not raw_stage.is_absolute():
                raise IntegrityError("publication paths are invalid")
            dest = Path(os.path.abspath(raw_dest)); stage = Path(os.path.abspath(raw_stage))
            if dest in destinations or stage in stages:
                raise IntegrityError("publication contains duplicate paths")
            destinations.add(dest); stages.add(stage)
            if (not dest.is_absolute() or not stage.is_absolute() or
                    len(str(dest).encode("utf-8")) > PATH_MAX_BYTES or
                    len(str(stage).encode("utf-8")) > PATH_MAX_BYTES):
                raise IntegrityError("publication paths are invalid")
            _no_links(dest.parent); _no_links(stage)
            candidate_checkout = dest.parent.parent.parent
            if (dest.parent.name not in _IMAGE_ROLES or
                    dest.parent.parent.name != "game"):
                raise IntegrityError("publication destination is outside an image directory")
            if checkout_root is None:
                checkout_root = candidate_checkout
            elif candidate_checkout != checkout_root:
                raise IntegrityError("publication destinations span multiple checkouts")
            staged_stat = os.lstat(stage)
            if (_is_link_or_reparse(staged_stat) or not stat.S_ISREG(staged_stat.st_mode) or
                    getattr(staged_stat, "st_nlink", 1) != 1):
                raise IntegrityError("staged result is not a stable regular file")
            expected_identity = list(expected[str(dest)]) if expected and str(dest) in expected else None
            if (expected_identity is not None and (len(expected_identity) != 4 or
                    any(not isinstance(value, int) or value < 0 for value in expected_identity))):
                raise IntegrityError("invalid expected destination identity")
            original_digest = expected_digests.get(str(dest)) if expected_digests else None
            if original_digest is not None and (not isinstance(original_digest, str) or
                    not re.fullmatch(r"[0-9a-f]{64}", original_digest)):
                raise IntegrityError("invalid expected destination digest")
            if expected_identity is not None:
                st = os.stat(dest, follow_symlinks=False)
                if [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns] != expected_identity: raise IntegrityError("destination changed before publication")
            quarantine = dest.parent / f".wb-old-{self.id}-{len(items)}"
            if os.path.lexists(quarantine): raise IntegrityError("publication quarantine already exists")
            items.append({"destination": str(dest), "staged": str(stage), "quarantine": str(quarantine),
                          "expected": expected_identity, "original_digest": original_digest,
                          "staged_digest": _digest_file(stage), "moved": False,
                          "quarantine_verified": False, "published": False})
        data = {"version": 1, "id": self.id, "phase": "prepared",
                "checkout_root": str(checkout_root), "items": items}
        self._write(data); self._phase("journal-prepared")
        try:
            for item in items:
                dest, stage, quarantine = map(Path, (item["destination"], item["staged"], item["quarantine"]))
                _no_links(dest.parent); _no_links(stage)
                destination_stat = os.lstat(dest)
                if _is_link_or_reparse(destination_stat) or not stat.S_ISREG(destination_stat.st_mode):
                    raise IntegrityError("destination is not a stable regular file")
                if item.get("expected") is not None:
                    current = os.stat(dest, follow_symlinks=False)
                    if [current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns] != item["expected"]:
                        raise IntegrityError("destination changed before publication")
                if shutil.disk_usage(dest.parent).free < os.lstat(stage).st_size + FREE_SPACE_RESERVE_BYTES:
                    raise ValidationError("not enough free space to publish the processed image batch safely")
                os.replace(dest, quarantine); self._phase("quarantine-renamed"); item["moved"] = True
                if item.get("original_digest") is not None and _digest_file(quarantine) != item["original_digest"]:
                    raise IntegrityError("destination content changed before publication")
                item["quarantine_verified"] = True
                data["phase"] = "quarantined"; self._write(data); self._phase("quarantine")
                # The run directory may live below Workbench DATA_DIR while
                # the checkout is on another filesystem.  Copy to a sibling
                # temp first, then atomically replace the destination.
                fd, temp_name = tempfile.mkstemp(prefix=f".wb-new-{self.id}-", dir=str(dest.parent))
                sibling = Path(temp_name)
                try:
                    if os.name == "posix":
                        os.fchmod(fd, stat.S_IMODE(destination_stat.st_mode))
                    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                    if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
                    try:
                        source_fd = os.open(stage, flags)
                    except Exception:
                        os.close(fd)
                        raise
                    with os.fdopen(fd, "wb") as out, os.fdopen(source_fd, "rb") as inp:
                        opened = os.fstat(inp.fileno())
                        if (_is_link_or_reparse(opened) or not stat.S_ISREG(opened.st_mode) or
                                getattr(opened, "st_nlink", 1) != 1 or opened.st_size > OUTPUT_MAX_BYTES):
                            raise IntegrityError("staged result changed during publication")
                        remaining = OUTPUT_MAX_BYTES + 1
                        while remaining:
                            chunk = inp.read(min(1024 * 1024, remaining))
                            if not chunk: break
                            out.write(chunk); remaining -= len(chunk)
                        if remaining <= 0 or inp.read(1):
                            raise IntegrityError("staged result changed during publication")
                        out.flush(); os.fsync(out.fileno())
                    _no_links(sibling)
                    if _digest_file(sibling) != item["staged_digest"]:
                        raise IntegrityError("staged result changed during publication")
                    # Exclusive publication is atomic and refuses to clobber
                    # a destination recreated by an external process after the
                    # original was quarantined.
                    _publish_exclusive(sibling, dest)
                    _fsync_directory(dest.parent)
                    self._phase("destination-published")
                finally:
                    try: sibling.unlink()
                    except OSError: pass
                item["published"] = True
                data["phase"] = "publishing"; self._write(data); self._phase("publish")
            data["phase"] = "committed"; self._write(data); self._phase("committed")
            self._cleanup(data)
        except Exception as exc:
            if data.get("phase") == "committed":
                # The commit marker is durable. Cleanup is idempotent and must
                # not roll back a transaction that is already complete.
                try:
                    self._cleanup(data)
                except Exception as cleanup_exc:
                    raise TransactionError("publication committed but cleanup needs recovery", committed=True) from cleanup_exc
                raise TransactionError("publication committed and cleanup recovered", committed=True) from exc
            try: self.rollback(data)
            except Exception as rollback_exc:
                raise TransactionError("publication failed and rollback failed", rollback_safe=False) from rollback_exc
            raise TransactionError("publication failed; originals restored") from exc

    def _cleanup(self, data: dict) -> None:
        destination_parents: set[Path] = set()
        for item in data["items"]:
            quarantine = Path(item["quarantine"])
            destination_parents.add(quarantine.parent)
            if os.path.lexists(quarantine):
                observed = os.lstat(quarantine)
                expected_old = item.get("expected")
                expected_digest = item.get("original_digest")
                if (_is_link_or_reparse(observed) or not stat.S_ISREG(observed.st_mode) or
                        not item.get("quarantine_verified") or
                        (expected_old is not None and
                         [observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns] != expected_old) or
                        (expected_digest is not None and _digest_file(quarantine) != expected_digest)):
                    raise TransactionError("committed quarantine changed before cleanup", committed=True)
                quarantine.unlink()
            staged = Path(item["staged"])
            if os.path.lexists(staged):
                observed = os.lstat(staged)
                if (_is_link_or_reparse(observed) or not stat.S_ISREG(observed.st_mode) or
                        getattr(observed, "st_nlink", 1) != 1 or
                        _digest_file(staged) != item.get("staged_digest")):
                    raise TransactionError("committed staging file changed before cleanup", committed=True)
                staged.unlink()
        for parent in destination_parents: _fsync_directory(parent)
        try: self.journal.unlink()
        except FileNotFoundError: pass
        _fsync_directory(self.root)

    def rollback(self, data: dict | None = None) -> None:
        data = data or _read_json(self.journal, "transaction journal", max_bytes=JOURNAL_MAX_BYTES)
        if not isinstance(data, dict): raise TransactionError("invalid transaction journal")
        for item in reversed(data.get("items", [])):
            dest, stage, quarantine = map(Path, (item["destination"], item["staged"], item["quarantine"]))
            quarantine_present = os.path.lexists(quarantine)
            if quarantine_present:
                observed_old = os.lstat(quarantine)
                if _is_link_or_reparse(observed_old) or not stat.S_ISREG(observed_old.st_mode):
                    raise TransactionError("publication quarantine is not a regular file")
                expected_old = item.get("expected")
                if expected_old is not None and [observed_old.st_dev, observed_old.st_ino, observed_old.st_size, observed_old.st_mtime_ns] != expected_old:
                    raise TransactionError("publication quarantine identity changed")
                expected_old_digest = item.get("original_digest")
                if (item.get("quarantine_verified") and expected_old_digest is not None and
                        _digest_file(quarantine) != expected_old_digest):
                    raise TransactionError("publication quarantine content changed")
            # Filesystem state is authoritative: a crash may happen after a
            # rename but before its journal flag is persisted.
            replacement_present = item.get("published") or quarantine_present
            if replacement_present:
                if not os.path.lexists(dest):
                    if not quarantine_present:
                        raise TransactionError("published destination is missing before rollback")
                else:
                    try:
                        observed = os.lstat(dest)
                        expected_digest = item.get("staged_digest")
                        if expected_digest is None and stage.is_file() and not stage.is_symlink():
                            expected_digest = _digest_file(stage)
                        regular = stat.S_ISREG(observed.st_mode) and not _is_link_or_reparse(observed)
                        matches = (regular and expected_digest is not None and
                                   _digest_file(dest) == expected_digest)
                        already_restored = (regular and not quarantine_present and
                                            item.get("original_digest") is not None and
                                            _digest_file(dest) == item["original_digest"])
                    except (OSError, PostProcessingError):
                        matches = already_restored = False
                    if matches:
                        dest.unlink()
                    elif not already_restored:
                        raise TransactionError("published destination changed before rollback")
            if quarantine_present:
                if os.path.lexists(dest): raise TransactionError("cannot restore over an existing destination")
                os.replace(quarantine, dest)
                _fsync_directory(dest.parent)
            if os.path.lexists(stage):
                observed_stage = os.lstat(stage)
                if (_is_link_or_reparse(observed_stage) or not stat.S_ISREG(observed_stage.st_mode) or
                        getattr(observed_stage, "st_nlink", 1) != 1 or
                        _digest_file(stage) != item.get("staged_digest")):
                    raise TransactionError("publication staging file changed before rollback")
                stage.unlink()
        try: self.journal.unlink()
        except FileNotFoundError: pass
        _fsync_directory(self.root)


def _validate_transaction_journal(
        data: Any, journal: Path, root: Path, expected_checkout: str | Path | None) -> None:
    if (not isinstance(data, dict) or
            set(data) != {"version", "id", "phase", "checkout_root", "items"} or
            data.get("version") != 1 or data.get("id") != journal.stem or
            data.get("phase") not in {"prepared", "quarantined", "publishing", "committed"} or
            not isinstance(data.get("items"), list) or not (1 <= len(data["items"]) <= IMAGE_MAX_COUNT)):
        raise IntegrityError("invalid transaction journal")
    run_root = (root.parent / "runs").resolve(strict=False)
    checkout_value = data.get("checkout_root")
    if not isinstance(checkout_value, str) or len(checkout_value.encode("utf-8")) > PATH_MAX_BYTES:
        raise IntegrityError("invalid transaction journal checkout")
    checkout_root = Path(checkout_value)
    if (not checkout_root.is_absolute() or
            checkout_root != Path(os.path.abspath(checkout_root))):
        raise IntegrityError("invalid transaction journal checkout")
    _no_links(checkout_root)
    if (expected_checkout is None or
            checkout_root.resolve() != Path(expected_checkout).resolve()):
        raise IntegrityError("transaction journal does not match the configured SCM checkout")
    for index, item in enumerate(data["items"]):
        if (not isinstance(item, dict) or set(item) !=
                {"destination", "staged", "quarantine", "expected", "original_digest",
                 "staged_digest", "moved", "quarantine_verified", "published"}):
            raise IntegrityError("invalid transaction journal")
        try:
            destination = Path(item["destination"]); staged = Path(item["staged"]); quarantine = Path(item["quarantine"])
        except (KeyError, TypeError):
            raise IntegrityError("invalid transaction journal")
        if (not destination.is_absolute() or not staged.is_absolute() or not quarantine.is_absolute() or
                any(path != Path(os.path.abspath(path)) for path in (destination, staged, quarantine)) or
                any(len(str(path).encode("utf-8")) > PATH_MAX_BYTES for path in (destination, staged, quarantine))):
            raise IntegrityError("invalid transaction journal")
        if (destination.parent.name not in _IMAGE_ROLES or
                destination.parent.parent != checkout_root / "game" or
                quarantine != destination.parent / f".wb-old-{data['id']}-{index}"):
            raise IntegrityError("invalid transaction journal destination")
        try:
            staged.resolve(strict=False).relative_to(run_root)
            _no_links(staged, allow_missing_leaf=True)
        except (ValueError, ValidationError) as exc:
            raise IntegrityError("invalid transaction journal staging path") from exc
        digest = item.get("staged_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise IntegrityError("invalid transaction journal digest")
        original_digest = item.get("original_digest")
        if original_digest is not None and (not isinstance(original_digest, str) or
                not re.fullmatch(r"[0-9a-f]{64}", original_digest)):
            raise IntegrityError("invalid transaction journal digest")
        expected = item.get("expected")
        if (expected is not None and (not isinstance(expected, list) or len(expected) != 4 or
                any(not isinstance(value, int) or value < 0 for value in expected))):
            raise IntegrityError("invalid transaction journal identity")
        if (not isinstance(item.get("moved"), bool) or
                not isinstance(item.get("quarantine_verified"), bool) or
                not isinstance(item.get("published"), bool) or
                (item["quarantine_verified"] and not item["moved"]) or
                (item["published"] and not item["moved"]) or
                (data["phase"] == "committed" and
                 not (item["moved"] and item["quarantine_verified"] and item["published"]))):
            raise IntegrityError("invalid transaction journal state")
        _no_links(destination.parent)


def recover_transactions(
        journal_root: str | Path, checkout_root: str | Path | None = None) -> tuple[str, ...]:
    root = Path(journal_root); recovered = []
    if not os.path.lexists(root): return ()
    _no_links(root)
    journals = sorted(path for path in _bounded_children(root, 1024, "transaction storage")
                      if path.suffix == ".json")
    for journal in journals:
        data = _read_json(journal, "transaction journal", max_bytes=JOURNAL_MAX_BYTES)
        _validate_transaction_journal(data, journal, root, checkout_root)
        tx = PublicationTransaction(root, str(data["id"]))
        if data.get("phase") == "committed": tx._cleanup(data)
        else: tx.rollback(data)
        recovered.append(tx.id)
    return tuple(recovered)


def interpreter_fingerprint(interpreter: str | Path = sys.executable) -> str:
    """Hash wheel/runtime compatibility, not one app bundle's file identity.

    The resolved path and stat tuple deliberately remain in the probe-cache key
    so replacing an interpreter always re-runs the bounded probe. They are not
    part of the returned fingerprint: copying or rebuilding the same compatible
    runtime changes its inode and timestamps without invalidating installed
    wheels. A Python minor/ABI/platform change still produces a new fingerprint.
    """
    path = Path(interpreter).resolve()
    try:
        observed = path.stat()
    except OSError as exc:
        raise ValidationError("could not inspect the selected Python interpreter") from exc
    identity = (str(path), observed.st_dev, observed.st_ino, observed.st_size,
                observed.st_mtime_ns, observed.st_ctime_ns)
    with _INTERPRETER_PROBE_LOCK:
        probe = _INTERPRETER_PROBE_CACHE.get(identity)
    if probe is None:
        code = ("import json,platform,struct,sys,sysconfig;print(json.dumps({"
                "'implementation':platform.python_implementation(),'version':list(sys.version_info[:3]),"
                "'abi':getattr(sys,'abiflags',''),'soabi':sysconfig.get_config_var('SOABI') or '',"
                "'cache_tag':getattr(sys.implementation,'cache_tag','') or '',"
                "'platform':sysconfig.get_platform(),'machine':platform.machine(),"
                "'multiarch':sysconfig.get_config_var('MULTIARCH') or '',"
                "'byteorder':sys.byteorder,'pointer_bits':struct.calcsize('P')*8}))")
        environment = {key: value for key, value in os.environ.items() if key in
                       {"PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}}
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen([str(path), "-I", "-B", "-c", code], stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.DEVNULL,
                                           env=environment, shell=False)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                try:
                    process.kill(); process.wait(timeout=1)
                except (UnboundLocalError, OSError, subprocess.TimeoutExpired): pass
                raise ValidationError("could not inspect the selected Python interpreter") from exc
            if process.returncode != 0 or output.tell() > 4096:
                raise ValidationError("could not inspect the selected Python interpreter")
            output.seek(0)
            try: probe = json.loads(output.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("selected Python interpreter returned invalid metadata") from exc
        required = {"implementation", "version", "abi", "soabi", "cache_tag", "platform",
                    "machine", "multiarch", "byteorder", "pointer_bits"}
        string_keys = required - {"version", "pointer_bits"}
        if (not isinstance(probe, dict) or set(probe) != required or
                not isinstance(probe.get("version"), list) or len(probe["version"]) != 3 or
                any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 999
                    for value in probe["version"]) or
                not isinstance(probe.get("pointer_bits"), int) or isinstance(probe["pointer_bits"], bool) or
                probe["pointer_bits"] not in (32, 64, 128) or
                any(not isinstance(probe.get(key), str) or len(probe[key]) > 512 for key in string_keys)):
            raise ValidationError("selected Python interpreter returned invalid metadata")
        with _INTERPRETER_PROBE_LOCK:
            if len(_INTERPRETER_PROBE_CACHE) >= 64: _INTERPRETER_PROBE_CACHE.clear()
            _INTERPRETER_PROBE_CACHE[identity] = probe
    value = {key: probe[key] for key in probe if key != "version"}
    value.update({"schema": INTERPRETER_FINGERPRINT_SCHEMA, "python": probe["version"][:2]})
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def environment_fingerprint(requirements: Sequence[str], lock_hash: str = "", *, interpreter: str | Path = sys.executable, contract: str = CONTRACT_VERSION) -> str:
    req = normalize_requirements(requirements)
    return hashlib.sha256(json.dumps({"requirements": req, "lock": lock_hash, "interpreter": interpreter_fingerprint(interpreter), "contract": contract}, sort_keys=True).encode()).hexdigest()


def wheel_only_pip_argv(interpreter: str | Path, requirements: Sequence[str], *, target: str | Path, report: str | Path | None = None, offline: bool = False) -> list[str]:
    req = normalize_requirements(requirements)
    argv = [str(interpreter), "-B", "-m", "pip", "install", "--isolated", "--disable-pip-version-check", "--no-input", "--only-binary=:all:", "--target", str(target)]
    if offline: argv += ["--no-index", "--require-hashes"]
    if report is not None: argv += ["--report", str(report)]
    return argv + list(req)


def validate_wheel_report(report: Mapping[str, Any], *, max_artifacts: int = 256) -> tuple[dict, ...]:
    if not isinstance(report, Mapping) or not isinstance(report.get("install"), list): raise ValidationError("invalid pip report")
    if len(report["install"]) > max_artifacts: raise ValidationError("pip report has too many distributions")
    result = []
    seen: set[str] = set()
    for item in report["install"]:
        if not isinstance(item, Mapping): raise ValidationError("invalid pip report entry")
        metadata = item.get("metadata")
        download = item.get("download_info")
        digest = download.get("archive_info", {}).get("hashes", {}).get("sha256") if isinstance(download, Mapping) else None
        if not isinstance(metadata, Mapping) or not isinstance(download, Mapping) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValidationError("report entry lacks a wheel hash")
        url = str(download.get("url", ""))
        parsed = urlsplit(url)
        try: trusted_port = parsed.port in (None, 443)
        except ValueError: trusted_port = False
        if (parsed.scheme != "https" or parsed.hostname not in {"pypi.org", "files.pythonhosted.org"} or
                not trusted_port or not parsed.path.lower().endswith(".whl") or
                parsed.username or parsed.password):
            raise ValidationError("only wheels from the Python Package Index are accepted")
        name = str(metadata.get("name", "")); version = str(metadata.get("version", ""))
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", canonical) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version) or canonical in seen):
            raise ValidationError("report entry has an invalid package identity")
        seen.add(canonical)
        result.append({"name": name, "canonical_name": canonical, "version": version,
                       "url": url, "sha256": digest.lower()})
    return tuple(result)


def wheel_lock_text(wheels: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for wheel in sorted(wheels, key=lambda value: str(value.get("canonical_name", ""))):
        name = str(wheel.get("canonical_name", "")); version = str(wheel.get("version", "")); digest = str(wheel.get("sha256", ""))
        if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", name) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version) or
                not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValidationError("invalid wheel lock entry")
        rows.append(f"{name}=={version} --hash=sha256:{digest}")
    if not rows:
        raise ValidationError("wheel lock is empty")
    return "\n".join(rows) + "\n"


def _legacy_environment_compatible(environment: Mapping[str, Any], wheels: Sequence[Mapping[str, Any]],
                                   interpreter: str | Path) -> bool:
    """Prove that a verified legacy tree was resolved for this Python ABI.

    Old ready markers contain an identity-bound fingerprint but not the probe
    that produced it.  Migration therefore fails closed unless the immutable
    tree has one exact current-interpreter native wheel tag, every installed
    wheel tag is accepted by the current interpreter, and every
    ``Requires-Python`` declaration accepts its version.
    """
    try:
        if not (1 <= len(wheels) <= 256):
            return False
        expected = []
        for wheel in wheels:
            name = str(wheel.get("canonical_name", ""))
            version = str(wheel.get("version", ""))
            if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", name) or
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version)):
                return False
            expected.append((name, version))
        if len({name for name, _version in expected}) != len(expected):
            return False

        site_packages = Path(str(environment.get("path") or "")) / "site-packages"
        _no_links(site_packages)
        if not site_packages.is_dir():
            return False
        distributions = []
        metadata_bytes = 0
        dist_info = []
        for child in _bounded_children(site_packages, 20_000, "dependency environment"):
            if child.name.endswith(".dist-info"):
                observed = os.lstat(child)
                if _is_link_or_reparse(observed) or not stat.S_ISDIR(observed.st_mode):
                    return False
                dist_info.append(child)
        if len(dist_info) != len(expected):
            return False
        actual = []
        tag_combinations = 0
        for directory in dist_info:
            wheel_raw = _read_regular_bytes(directory / "WHEEL", "wheel metadata", 16 * 1024)
            package_raw = _read_regular_bytes(directory / "METADATA", "package metadata", 128 * 1024)
            metadata_bytes += len(wheel_raw) + len(package_raw)
            if metadata_bytes > 8 * 1024 * 1024:
                return False
            wheel_metadata = BytesParser(policy=EMAIL_POLICY).parsebytes(wheel_raw)
            package_metadata = BytesParser(policy=EMAIL_POLICY).parsebytes(package_raw)
            names = package_metadata.get_all("Name", [])
            versions = package_metadata.get_all("Version", [])
            requires_python = package_metadata.get_all("Requires-Python", [])
            tags = wheel_metadata.get_all("Tag", [])
            if len(names) != 1 or len(versions) != 1 or len(requires_python) > 1 or not (1 <= len(tags) <= 64):
                return False
            name = re.sub(r"[-_.]+", "-", str(names[0])).lower()
            version = str(versions[0])
            if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", name) or
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version)):
                return False
            clean_tags = []
            for value in tags:
                tag = str(value)
                parts = tag.split("-")
                if (len(tag) > 256 or len(parts) != 3 or
                        any(not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", part) for part in parts)):
                    return False
                combinations = 1
                for part in parts:
                    combinations *= len(part.split("."))
                tag_combinations += combinations
                if tag_combinations > 4096:
                    return False
                clean_tags.append(tag)
            specifier = str(requires_python[0]) if requires_python else ""
            if len(specifier) > 256 or any(unicodedata.category(character).startswith("C") for character in specifier):
                return False
            actual.append((name, version))
            distributions.append({"tags": clean_tags, "requires_python": specifier})
        if sorted(actual) != sorted(expected):
            return False

        payload = json.dumps({"distributions": distributions}, separators=(",", ":")).encode("utf-8")
        if len(payload) > 1024 * 1024:
            return False
        code = (
            "import json,platform,sys\n"
            "try:\n from packaging.specifiers import SpecifierSet\n from packaging.tags import interpreter_name,interpreter_version,parse_tag,sys_tags\n from packaging.version import Version\n"
            "except ImportError:\n from pip._vendor.packaging.specifiers import SpecifierSet\n from pip._vendor.packaging.tags import interpreter_name,interpreter_version,parse_tag,sys_tags\n from pip._vendor.packaging.version import Version\n"
            "p=json.load(sys.stdin); current=set(sys_tags()); preferred=interpreter_name()+interpreter_version(); bound=False\n"
            "for d in p['distributions']:\n"
            " declared=set()\n"
            " for raw in d['tags']: declared.update(parse_tag(raw))\n"
            " if not declared.intersection(current): raise SystemExit(2)\n"
            " if d['requires_python'] and not SpecifierSet(d['requires_python']).contains(Version(platform.python_version()),prereleases=True): raise SystemExit(2)\n"
            " if any(t.interpreter==preferred and t.abi!='none' and t.platform!='any' for t in declared): bound=True\n"
            "print(json.dumps({'compatible':True,'bound':bound},separators=(',',':')))"
        )
        environment_variables = {key: value for key, value in os.environ.items() if key in
                                 {"PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}}
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen([str(Path(interpreter).resolve()), "-I", "-B", "-c", code],
                                       stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                       env=environment_variables, shell=False)
            try:
                process.communicate(payload, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=1)
                return False
            if process.returncode != 0 or output.tell() > 4096:
                return False
            output.seek(0)
            result = json.loads(output.read().decode("utf-8"))
        return result == {"compatible": True, "bound": True}
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError, PostProcessingError,
            subprocess.SubprocessError, ValueError):
        return False


class ProcessorStore:
    """Immutable processor revisions below an injected Workbench data root."""
    def __init__(self, data_root: str | Path, scm_root: str | Path | None = None, *, contract: str = CONTRACT_VERSION):
        self.data_root = Path(data_root).resolve(); self.root = self.data_root / "postprocessing"; self.scm_root = Path(scm_root).resolve() if scm_root else None; self.contract = contract
        for directory in (self.root, self.root / "processors", self.root / "environments", self.root / "runs", self.root / "transactions"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700); _no_links(directory); _private(directory, directory=True)
        recovery_key = str(self.root)
        with _RECOVERY_LOCK:
            if recovery_key not in _RECOVERED_ROOTS:
                recover_transactions(self.root / "transactions", self.scm_root)
                self.cleanup(max_age=0, prune_environments=True)
                _RECOVERED_ROOTS.add(recovery_key)

    def _processor(self, processor_id: str) -> Path:
        processor_id = _safe_component(processor_id, "processor id", 64)
        if not re.fullmatch(r"[0-9a-f]{32}", processor_id):
            raise ValidationError("invalid processor id")
        return _contained(self.root / "processors", self.root / "processors" / processor_id)

    def _registry_entries(self) -> list[Path]:
        return _bounded_children(
            self.root / "processors", PROCESSOR_STORAGE_MAX_COUNT,
            "processor registry",
        )

    def _custom_processor_count(self) -> int:
        count = 0
        for directory in self._registry_entries():
            try:
                if (directory.is_dir() and not directory.is_symlink() and
                        re.fullmatch(r"[0-9a-f]{32}", directory.name) and
                        self._metadata(directory.name).get("bundled")):
                    continue
            except PostProcessingError:
                pass
            count += 1
        return count

    def _saved_source_bytes(self) -> int:
        total = count = 0
        for processor in self._registry_entries():
            if processor.is_symlink() or not processor.is_dir():
                continue
            revisions = processor / "revisions"
            if not revisions.is_dir() or revisions.is_symlink():
                continue
            for source_path in _bounded_children(revisions, REVISIONS_MAX_COUNT * 2, "processor revisions"):
                if source_path.suffix != ".py":
                    continue
                observed = os.lstat(source_path)
                if _is_link_or_reparse(observed) or not stat.S_ISREG(observed.st_mode):
                    raise IntegrityError("invalid processor source file")
                count += 1; total += observed.st_size
                if count > PROCESSOR_STORAGE_MAX_COUNT * REVISIONS_MAX_COUNT or total > SAVED_SOURCE_MAX_BYTES:
                    raise IntegrityError("saved processor source limit was exceeded")
        return total

    def _metadata(self, processor_id: str) -> dict:
        value = _read_json(self._processor(processor_id) / "metadata.json", "processor metadata")
        if not isinstance(value, dict) or value.get("id") != processor_id:
            raise IntegrityError("invalid processor metadata")
        try:
            normalize_name(value.get("name"))
        except ValidationError as exc:
            raise IntegrityError("invalid processor metadata") from exc
        for key, length in (("active_revision", 64), ("trusted", 64), ("environment", 64),
                            ("trusted_tree_digest", 64), ("installed_revision", 64),
                            ("installed_environment", 64), ("installed_tree_digest", 64),
                            ("lock_hash", 64)):
            candidate = value.get(key)
            if candidate is not None and (not isinstance(candidate, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", candidate)):
                raise IntegrityError("invalid processor metadata")
        if "bundled" in value and value.get("bundled") is not True:
            raise IntegrityError("invalid processor metadata")
        if "optional_model" in value and (value.get("optional_model") is not True or not value.get("bundled")):
            raise IntegrityError("invalid processor metadata")
        return value

    def provision_bundled(self, processor_id: str, name: str, source: str, *,
                          interpreter: str | Path = sys.executable,
                          requirements: Sequence[str] = (), optional_model: bool = False) -> dict:
        """Publish app-owned source without installing any optional assets.

        Only the caller's fixed, shipped optional model may declare libraries.
        A compatible verified installation survives app restarts; a changed
        shipped revision needs a new install before it can run again.
        """
        if requirements and not optional_model:
            raise ValidationError("bundled runtime processors cannot request libraries")
        req = normalize_requirements(requirements)
        directory = self._processor(processor_id)
        name = normalize_name(name)
        raw = validate_source(source)
        revision = revision_digest(source, req, self.contract)
        environment = self.environment_metadata(req, interpreter=interpreter)
        if not optional_model and not environment["ready"]:
            raise IntegrityError("bundled processor runtime is not ready")
        exists = os.path.lexists(directory)
        if exists:
            try:
                observed = os.lstat(directory)
            except OSError as exc:
                raise IntegrityError("could not inspect bundled processor storage") from exc
            if _is_link_or_reparse(observed) or not stat.S_ISDIR(observed.st_mode):
                raise IntegrityError("invalid bundled processor storage")
            metadata_path = directory / "metadata.json"
            if os.path.lexists(metadata_path):
                metadata = self._metadata(processor_id)
                if not metadata.get("bundled"):
                    raise ConflictError("bundled processor id is already in use")
                if (metadata.get("name") == name and
                        metadata.get("active_revision") == revision and
                        bool(metadata.get("optional_model")) == optional_model and
                        metadata.get("trusted") == revision and
                        (optional_model or (
                            metadata.get("environment") == environment["fingerprint"] and
                            metadata.get("trusted_tree_digest") == environment.get("tree_digest")))):
                    try:
                        current = self.get(processor_id)
                        if current.get("source") == source:
                            return current
                    except PostProcessingError:
                        # This reserved app-owned entry is repaired below.
                        pass
        else:
            if len(self._registry_entries()) >= PROCESSOR_STORAGE_MAX_COUNT:
                raise ValidationError("processor storage limit reached")
            directory.mkdir(mode=0o700)
            _private(directory, directory=True)

        revisions = directory / "revisions"
        revisions.mkdir(exist_ok=True, mode=0o700)
        _no_links(revisions)
        _private(revisions, directory=True)
        source_path = revisions / f"{revision}.py"
        revision_meta = revisions / f"{revision}.json"
        if not source_path.exists() and self._saved_source_bytes() + len(raw) > SAVED_SOURCE_MAX_BYTES:
            raise ValidationError("saved processor source limit reached")
        # The app owns this reserved entry, so repairing a missing or damaged
        # shipped revision is safe; user-authored immutable revisions are never
        # rewritten by this path.
        _atomic_bytes(source_path, raw)
        _atomic_json(revision_meta, {
            "revision": revision,
            "requirements": list(req),
            "contract": self.contract,
            "source_bytes": len(raw),
        })
        _atomic_json(directory / "metadata.json", {
            "id": processor_id,
            "name": name,
            "active_revision": revision,
            "trusted": revision,
            "environment": environment["fingerprint"] if not optional_model else None,
            "trusted_tree_digest": environment.get("tree_digest") if not optional_model else None,
            "bundled": True,
            **({"optional_model": True} if optional_model else {}),
            "updated": time.time(),
        })
        # Built-in revisions cannot be reverted or edited. Prune superseded
        # app versions only after the replacement metadata is durable.
        for old_path in _bounded_children(revisions, REVISIONS_MAX_COUNT * 2,
                                          "processor revisions"):
            if (old_path.stem != revision and old_path.suffix in {".py", ".json"} and
                    re.fullmatch(r"[0-9a-f]{64}", old_path.stem)):
                old_path.unlink()
        return self.get(processor_id)

    def save(self, name: str, source: str, requirements: str | Sequence[str] | None = None, *, processor_id: str | None = None, expected_revision: str | None = None) -> dict:
        name = normalize_name(name); raw = validate_source(source); req = normalize_requirements(requirements); revision = revision_digest(source, req, self.contract)
        current_meta = None
        current_requirements: tuple[str, ...] = ()
        if processor_id is None:
            if self._custom_processor_count() >= PROCESSOR_MAX_COUNT: raise ValidationError("processor limit reached")
            processor_id = uuid.uuid4().hex
            while self._processor(processor_id).exists(): processor_id = uuid.uuid4().hex
            directory = self._processor(processor_id); directory.mkdir(mode=0o700); _private(directory, directory=True)
            (directory / "revisions").mkdir(mode=0o700); _private(directory / "revisions", directory=True)
            current = None
        else:
            current_meta = self._metadata(processor_id); current = current_meta.get("active_revision")
            if current_meta.get("bundled"):
                raise ConflictError("bundled processors are read-only")
            if expected_revision != current: raise ConflictError("processor revision is stale")
            current_requirements = tuple(self.get(
                processor_id, revision=current, include_source=False,
            )["requirements"])
            directory = self._processor(processor_id); directory.joinpath("revisions").mkdir(exist_ok=True, mode=0o700); _private(directory / "revisions", directory=True)
        source_path = directory / "revisions" / f"{revision}.py"; revision_meta = directory / "revisions" / f"{revision}.json"
        revision_entries = _bounded_children(directory / "revisions", REVISIONS_MAX_COUNT * 2, "processor revisions")
        revisions = [path for path in revision_entries if path.suffix == ".py"]
        if not source_path.exists():
            if len(revisions) >= REVISIONS_MAX_COUNT:
                candidates = [path for path in revisions if path.stem != current]
                if not candidates:
                    raise ValidationError("revision limit reached")
                victim = min(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
                victim.unlink()
                try: victim.with_suffix(".json").unlink()
                except FileNotFoundError: pass
            if self._saved_source_bytes() + len(raw) > SAVED_SOURCE_MAX_BYTES:
                raise ValidationError("saved processor source limit reached")
            _atomic_bytes(source_path, raw)
        else:
            if _read_regular_bytes(source_path, "processor source", SOURCE_MAX_BYTES) != raw:
                raise IntegrityError("immutable revision content was changed")
        if not revision_meta.exists(): _atomic_json(revision_meta, {"revision": revision, "requirements": list(req), "contract": self.contract, "source_bytes": len(raw)})
        metadata = {"id": processor_id, "name": name, "active_revision": revision, "trusted": None, "updated": time.time()}
        if (req and current_meta is not None and current_requirements == req and
                current_meta.get("installed_revision") == current and
                current_meta.get("installed_environment") is not None and
                current_meta.get("installed_tree_digest") is not None and
                current_meta.get("lock_hash") is not None):
            # A source-only edit changes what must be trusted, not the exact
            # interpreter/lock/tree environment already associated with it.
            metadata.update({
                "installed_revision": revision,
                "installed_environment": current_meta["installed_environment"],
                "installed_tree_digest": current_meta["installed_tree_digest"],
                "lock_hash": current_meta["lock_hash"],
            })
        _atomic_json(directory / "metadata.json", metadata)
        return self.get(processor_id)

    def get(self, processor_id: str, *, revision: str | None = None, include_source: bool = True) -> dict:
        metadata = self._metadata(processor_id); revision = revision or metadata.get("active_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision): raise IntegrityError("invalid processor revision")
        directory = self._processor(processor_id) / "revisions"; meta = _read_json(directory / f"{revision}.json", "revision metadata")
        source_path = directory / f"{revision}.py"
        if not isinstance(meta, dict): raise NotFoundError("processor revision not found")
        try:
            raw = _read_regular_bytes(source_path, "processor source", SOURCE_MAX_BYTES)
            source = raw.decode("utf-8")
        except IntegrityError as exc:
            if not os.path.lexists(source_path) and isinstance(exc.__cause__, FileNotFoundError):
                raise NotFoundError("processor revision not found") from exc
            raise
        except UnicodeDecodeError as exc:
            raise IntegrityError("could not read processor source") from exc
        requirements = normalize_requirements(meta.get("requirements", []))
        if (len(raw) != meta.get("source_bytes") or
                revision_digest(source, requirements, self.contract) != revision):
            raise IntegrityError("processor revision content does not match its digest")
        result = {"id": metadata["id"], "name": metadata["name"], "active_revision": metadata.get("active_revision"), "revision": revision, "requirements": list(requirements), "trusted": metadata.get("trusted") == revision, "source_bytes": len(raw), "bundled": bool(metadata.get("bundled")), "optional_model": bool(metadata.get("optional_model"))}
        if include_source: result["source"] = source
        return result

    def list(self) -> tuple[dict, ...]:
        result = []
        directories = sorted(self._registry_entries())
        for directory in directories:
            if directory.is_dir() and not directory.is_symlink():
                try: result.append(self.get(directory.name, include_source=False))
                except PostProcessingError: continue
        return tuple(result)

    def revisions(self, processor_id: str) -> tuple[dict, ...]:
        """Return bounded, verified summaries for every immutable revision."""
        metadata = self._metadata(processor_id)
        directory = self._processor(processor_id) / "revisions"
        result = []
        for source_path in _bounded_children(
                directory, REVISIONS_MAX_COUNT * 2, "processor revisions"):
            if source_path.suffix != ".py":
                continue
            item = self.get(processor_id, revision=source_path.stem, include_source=False)
            observed = os.lstat(source_path)
            result.append({
                "revision": item["revision"],
                "source_bytes": item["source_bytes"],
                "saved_at": observed.st_mtime,
                "active": item["revision"] == metadata.get("active_revision"),
            })
        result.sort(key=lambda item: (-item["saved_at"], item["revision"]))
        return tuple(result)

    def trust(self, processor_id: str, revision: str, environment: str | None = None, *, interpreter: str | Path = sys.executable) -> dict:
        metadata = self._metadata(processor_id)
        if metadata.get("bundled"):
            raise ConflictError("bundled processors are already app-trusted")
        if revision != metadata.get("active_revision"):
            raise ConflictError("processor revision is stale")
        item = self.get(processor_id, revision=revision, include_source=False)
        # Never accept a client-selected dependency environment.  Approval is
        # tied to the one fingerprint derived from this saved revision.
        lock_hash = (metadata.get("lock_hash") or "") if metadata.get("installed_revision") == revision else ""
        selected = self.environment_metadata(item["requirements"], lock_hash, interpreter=interpreter)
        expected = selected["fingerprint"]
        if environment is not None and environment != expected:
            raise ConflictError("post-processor environment is stale")
        if not selected["ready"]:
            raise ConflictError("processor libraries are not ready")
        metadata["trusted"] = revision
        metadata["environment"] = expected
        metadata["trusted_tree_digest"] = selected.get("tree_digest")
        _atomic_json(self._processor(processor_id) / "metadata.json", metadata)
        return self.get(processor_id, revision=revision, include_source=False)

    def record_environment(self, processor_id: str, revision: str, lock_hash: str, *, interpreter: str | Path = sys.executable) -> dict:
        if self._metadata(processor_id).get("bundled") and not self._metadata(processor_id).get("optional_model"):
            raise ConflictError("bundled processors use the app runtime")
        item = self.get(processor_id, include_source=False)
        requirements = tuple(item["requirements"])
        if (not isinstance(lock_hash, str) or
                (requirements and not re.fullmatch(r"[0-9a-f]{64}", lock_hash)) or
                (not requirements and lock_hash != "")):
            raise ValidationError("invalid dependency lock hash")
        if revision != item.get("active_revision"):
            raise ConflictError("processor revision is stale")
        environment = self.environment_metadata(requirements, lock_hash, interpreter=interpreter)
        if not environment["ready"]:
            raise IntegrityError("installed dependency environment is not ready")
        metadata = self._metadata(processor_id)
        trust_still_valid = (
            metadata.get("trusted") == revision and
            metadata.get("environment") == environment["fingerprint"] and
            metadata.get("trusted_tree_digest") == environment.get("tree_digest")
        )
        metadata.update({
            "installed_revision": revision,
            "installed_environment": environment["fingerprint"],
            "installed_tree_digest": environment.get("tree_digest"),
            "lock_hash": lock_hash or None,
        })
        if metadata.get("optional_model"):
            metadata.update({"trusted": revision, "environment": environment["fingerprint"],
                             "trusted_tree_digest": environment.get("tree_digest")})
        elif not trust_still_valid:
            metadata.update({"trusted": None, "environment": None, "trusted_tree_digest": None})
        _atomic_json(self._processor(processor_id) / "metadata.json", metadata)
        return self.status(processor_id, interpreter=interpreter)

    def remove_optional_environment(self, processor_id: str, revision: str) -> str | None:
        """Make an app-owned optional processor unavailable without deleting its source."""
        metadata = self._metadata(processor_id)
        if not metadata.get("bundled") or not metadata.get("optional_model"):
            raise ConflictError("only the optional app-owned processor can be uninstalled")
        if revision != metadata.get("active_revision"):
            raise ConflictError("processor revision is stale")
        previous = metadata.get("installed_environment")
        metadata.update({"trusted": revision, "environment": None, "trusted_tree_digest": None,
                         "installed_revision": None, "installed_environment": None,
                         "installed_tree_digest": None, "lock_hash": None})
        _atomic_json(self._processor(processor_id) / "metadata.json", metadata)
        return previous

    def invalidate_trust(self, processor_id: str, *, expected_revision: str | None = None) -> dict:
        metadata = self._metadata(processor_id)
        if metadata.get("bundled"):
            raise ConflictError("bundled processors are already app-trusted")
        if expected_revision is not None and expected_revision != metadata.get("active_revision"):
            raise ConflictError("processor revision is stale")
        metadata["trusted"] = None
        metadata["environment"] = None
        metadata["trusted_tree_digest"] = None
        _atomic_json(self._processor(processor_id) / "metadata.json", metadata)
        return self.get(processor_id, include_source=False)

    def delete(self, processor_id: str, *, expected_revision: str | None = None) -> None:
        metadata = self._metadata(processor_id)
        if metadata.get("bundled"):
            raise ConflictError("bundled processors cannot be deleted")
        if expected_revision is not None and expected_revision != metadata.get("active_revision"): raise ConflictError("processor revision is stale")
        shutil.rmtree(self._processor(processor_id))

    def duplicate(self, processor_id: str, *, name: str | None = None, expected_revision: str | None = None) -> dict:
        item = self.get(processor_id)
        # The copy is an ordinary untrusted processor: it receives source and
        # requirements, never the built-in's installed model or environment.
        if expected_revision is not None and expected_revision != item["active_revision"]:
            raise ConflictError("processor revision is stale")
        return self.save(name or (item["name"] + " copy"), item["source"], item["requirements"])

    def import_selected(self, source_path: str | Path, *, name: str | None = None) -> dict:
        """Import one stable, regular UTF-8 Python file selected by the OS picker."""
        raw_path = str(source_path)
        _utf8(raw_path, "selected processor path", PATH_MAX_BYTES)
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValidationError("selected processor path must be absolute")
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or getattr(before, "st_reparse_tag", 0):
                raise ValidationError("selected processor is not a regular file")
            if before.st_size > SOURCE_MAX_BYTES:
                raise ValidationError("source is too large")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            try:
                observed = os.fstat(fd)
                if (observed.st_dev, observed.st_ino, observed.st_size) != (before.st_dev, before.st_ino, before.st_size):
                    raise ValidationError("selected processor changed while opening")
                raw = b""
                while len(raw) <= SOURCE_MAX_BYTES:
                    chunk = os.read(fd, min(64 * 1024, SOURCE_MAX_BYTES + 1 - len(raw)))
                    if not chunk: break
                    raw += chunk
                after = os.fstat(fd)
            finally:
                os.close(fd)
            if len(raw) > SOURCE_MAX_BYTES or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                raise ValidationError("selected processor changed while reading")
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("selected processor is not UTF-8") from exc
        except OSError as exc:
            raise ValidationError("could not read selected processor") from exc
        clean_name = name or path.stem or "Imported processor"
        return self.save(clean_name, source, "")

    def _environment_metadata_at(self, requirements: Sequence[str], lock_hash: str, fingerprint: str,
                                 *, path: Path | None = None,
                                 accepted_marker_fingerprints: set[str] | None = None) -> dict:
        req = normalize_requirements(requirements)
        location = path or (self.root / "environments" / fingerprint)
        if not req:
            return {"fingerprint": fingerprint, "requirements": [], "ready": True,
                    "status": "ready", "path": str(location), "files": 0, "bytes": 0,
                    "tree_digest": None, "wheels": []}
        ready = _read_json(location / "ready.json", "environment marker", missing=None)
        locked_wheels: list[dict[str, str]] = []
        lock_valid = False
        if isinstance(ready, dict) and isinstance(ready.get("wheels"), list):
            try:
                locked = ready["wheels"]
                if not (1 <= len(locked) <= 256):
                    raise ValidationError("invalid wheel lock")
                lock_text = wheel_lock_text(locked).encode("utf-8")
                lock_valid = hashlib.sha256(lock_text).hexdigest() == lock_hash
                locked_wheels = [{"name": str(wheel["canonical_name"]),
                                  "version": str(wheel["version"]),
                                  "sha256": str(wheel["sha256"])} for wheel in locked]
            except (KeyError, TypeError, ValidationError):
                lock_valid = False
        tree_valid = (isinstance(ready, dict) and
                      isinstance(ready.get("files"), int) and not isinstance(ready.get("files"), bool) and
                      ready.get("files") >= 0 and
                      isinstance(ready.get("bytes"), int) and not isinstance(ready.get("bytes"), bool) and
                      ready.get("bytes") >= 0 and
                      isinstance(ready.get("tree_digest"), str) and
                      re.fullmatch(r"[0-9a-f]{64}", ready["tree_digest"]))
        accepted = accepted_marker_fingerprints or {fingerprint}
        ready_valid = (isinstance(ready, dict) and ready.get("fingerprint") in accepted and
                       ready.get("requirements") == list(req) and ready.get("lock_hash", "") == lock_hash and
                       lock_valid and tree_valid and location.is_dir() and not location.is_symlink())
        return {"fingerprint": fingerprint, "requirements": list(req), "ready": bool(ready_valid),
                "status": "ready" if ready_valid else "missing", "path": str(location),
                "files": ready.get("files") if ready_valid else None,
                "bytes": ready.get("bytes") if ready_valid else None,
                "tree_digest": ready.get("tree_digest") if ready_valid else None,
                "wheels": locked_wheels if ready_valid else []}

    def environment_metadata(self, requirements: Sequence[str], lock_hash: str = "", *, interpreter: str | Path = sys.executable) -> dict:
        req = normalize_requirements(requirements)
        fingerprint = environment_fingerprint(req, lock_hash, interpreter=interpreter, contract=self.contract)
        return self._environment_metadata_at(req, lock_hash, fingerprint)

    def _migrate_legacy_environment(self, processor_id: str, item: Mapping[str, Any], lock_hash: str,
                                    current: Mapping[str, Any], interpreter: str | Path,
                                    verifier: Callable[[dict], None]) -> bool:
        """Re-key one exact verified pre-schema-2 environment without reinstalling it."""
        requirements = tuple(item.get("requirements") or ())
        if not requirements:
            # A dependency-free tree has no immutable wheel metadata with which
            # to prove the legacy interpreter ABI. Re-approval is cheap and is
            # safer than guessing at an irreversible old fingerprint.
            return False
        with _ENVIRONMENT_MIGRATION_LOCK:
            metadata = self._metadata(processor_id)
            revision = item.get("revision")
            old_fingerprint = metadata.get("installed_environment")
            new_fingerprint = current.get("fingerprint")
            if (metadata.get("installed_revision") != revision or metadata.get("lock_hash") != lock_hash or
                    not isinstance(old_fingerprint, str) or old_fingerprint == new_fingerprint or
                    not isinstance(new_fingerprint, str)):
                return False
            environments = self.root / "environments"
            new_environment = self.environment_metadata(requirements, lock_hash, interpreter=interpreter)
            candidate = new_environment if new_environment["ready"] else self._environment_metadata_at(
                requirements, lock_hash, old_fingerprint,
                path=environments / old_fingerprint,
                accepted_marker_fingerprints={old_fingerprint, new_fingerprint},
            )
            if (not candidate["ready"] or
                    metadata.get("installed_tree_digest") != candidate.get("tree_digest")):
                return False
            try:
                marker = _read_json(Path(candidate["path"]) / "ready.json", "environment marker")
                wheels = marker.get("wheels") if isinstance(marker, dict) else None
                # The bounded metadata check is intentionally first: a truly
                # incompatible runtime can fail cheaply instead of re-hashing
                # a multi-gigabyte tree on every status refresh. No migration
                # occurs until the full immutable tree is verified below.
                if not isinstance(wheels, list) or not _legacy_environment_compatible(candidate, wheels, interpreter):
                    return False
                verifier(dict(candidate))
            except (OSError, PostProcessingError):
                return False

            if Path(candidate["path"]) != Path(new_environment["path"]):
                old_path = Path(candidate["path"])
                new_path = Path(new_environment["path"])
                if os.path.lexists(new_path):
                    return False
                original_marker = marker
                migrated_marker = {**marker, "fingerprint": new_fingerprint}
                try:
                    _atomic_json(old_path / "ready.json", migrated_marker)
                    os.rename(old_path, new_path)
                    _fsync_directory(environments)
                except OSError:
                    if old_path.is_dir() and not old_path.is_symlink():
                        try: _atomic_json(old_path / "ready.json", original_marker)
                        except (OSError, PostProcessingError): pass
                    return False
                new_environment = self.environment_metadata(requirements, lock_hash, interpreter=interpreter)
                if not new_environment["ready"]:
                    return False

            latest = self._metadata(processor_id)
            stable_keys = ("active_revision", "installed_revision", "installed_environment",
                           "installed_tree_digest", "lock_hash", "trusted", "environment",
                           "trusted_tree_digest")
            if any(latest.get(key) != metadata.get(key) for key in stable_keys):
                return False
            was_trusted = (metadata.get("trusted") == revision and
                           metadata.get("environment") == old_fingerprint and
                           metadata.get("trusted_tree_digest") == candidate.get("tree_digest"))
            latest.update({
                "installed_environment": new_fingerprint,
                "installed_tree_digest": candidate.get("tree_digest"),
            })
            if was_trusted:
                latest.update({"environment": new_fingerprint,
                               "trusted_tree_digest": candidate.get("tree_digest")})
            else:
                latest.update({"trusted": None, "environment": None, "trusted_tree_digest": None})
            _atomic_json(self._processor(processor_id) / "metadata.json", latest)
            return True

    def status(self, processor_id: str, *, interpreter: str | Path = sys.executable,
               environment_verifier: Callable[[dict], None] | None = None) -> dict:
        item = self.get(processor_id, include_source=False)
        metadata = self._metadata(processor_id)
        lock_hash = (metadata.get("lock_hash") or "") if metadata.get("installed_revision") == item["revision"] else ""
        environment = self.environment_metadata(item["requirements"], lock_hash, interpreter=interpreter)
        installed_environment = metadata.get("installed_environment")
        stale = installed_environment is not None and installed_environment != environment["fingerprint"]
        if stale and environment_verifier is not None and self._migrate_legacy_environment(
                processor_id, item, lock_hash, environment, interpreter, environment_verifier):
            metadata = self._metadata(processor_id)
            environment = self.environment_metadata(item["requirements"], lock_hash, interpreter=interpreter)
            installed_environment = metadata.get("installed_environment")
            stale = installed_environment is not None and installed_environment != environment["fingerprint"]
        if stale:
            # A different Python compatibility fingerprint is expected after a
            # real runtime change. Never use or trust the old tree, but keep the
            # processor source editable and present an actionable reinstall
            # state instead of misreporting ordinary staleness as corruption.
            environment = {**environment, "ready": False, "status": "stale", "stale": True}
        elif metadata.get("installed_tree_digest") not in (None, environment.get("tree_digest")):
            raise IntegrityError("processor environment metadata is inconsistent")
        trusted = (not stale and item.get("trusted") and
                   metadata.get("environment") == environment["fingerprint"] and
                   metadata.get("trusted_tree_digest") == environment.get("tree_digest"))
        processor = {**item, "trusted": bool(trusted),
                     "environment_fingerprint": environment["fingerprint"],
                     "environment_ready": environment["ready"],
                     "environment_status": environment["status"],
                     "ready_to_run": bool(trusted and environment["ready"])}
        return {"processor": processor, "environment": environment}

    def cleanup(self, *, max_age: float = 24 * 3600, prune_environments: bool = False) -> None:
        cutoff = time.time() - max_age
        for child in _bounded_children(self.root / "runs", 1024, "processor run storage"):
            if child.is_dir() and not child.is_symlink() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        # Fingerprint-named environments are reusable state, not temporary
        # cleanup candidates.  Only abandoned install/backup directories age
        # out here.
        environments = _bounded_children(self.root / "environments", PROCESSOR_MAX_COUNT * REVISIONS_MAX_COUNT * 2, "processor environments")
        referenced: set[str] = set()
        if prune_environments:
            processors = _bounded_children(self.root / "processors", PROCESSOR_STORAGE_MAX_COUNT * 2, "processor registry")
            for processor in processors:
                metadata_path = processor / "metadata.json"
                incomplete = (processor.name.startswith(".new-") or
                              (re.fullmatch(r"[0-9a-f]{32}", processor.name) and not metadata_path.is_file()))
                if (incomplete and not processor.is_symlink() and processor.is_dir() and
                        processor.stat().st_mtime < cutoff):
                    shutil.rmtree(processor, ignore_errors=True)
                    continue
                try:
                    metadata = self._metadata(processor.name)
                except PostProcessingError:
                    continue
                referenced.update(value for value in (metadata.get("environment"), metadata.get("installed_environment")) if value)
        for child in environments:
            stale_stage = child.name.startswith((".install-", ".old-"))
            stale_environment = (prune_environments and re.fullmatch(r"[0-9a-f]{64}", child.name) and
                                 child.name not in referenced and child.stat().st_mtime < time.time() - 7 * 24 * 3600)
            if ((stale_stage and child.stat().st_mtime < cutoff) or stale_environment) and child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)

    # Names useful to callers that prefer explicit verbs.
    save_revision = save
    list_processors = list
    get_processor = get
    delete_processor = delete
    duplicate_processor = duplicate

__all__ = [name for name in globals() if not name.startswith("_")]
