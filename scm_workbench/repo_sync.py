#!/usr/bin/env python3
"""
repo_sync.py — keep the Workbench's *managed copies* of the sister repos current.

The Workbench can own its own copies of silhouette-card-maker and scm-extras
(living in the Workbench's data area). This module is how those copies are
created and updated:

  * GitHub's plain-HTTPS endpoints only (api.github.com + raw.githubusercontent.com)
    — no git binary, no credentials, nothing installed on the host machine.
  * "check" is metadata-only (a couple of small API calls); "update" fetches
    ONLY the files that changed between the deployed commit and the target
    (GitHub compare API + per-file raw fetch). A full tarball swap is the
    fallback when the diff is too large or the API misbehaves.
  * User-generated files (card art, decklists, printed PDFs, offsets, …) are
    all *untracked* in the repos, so an update never deletes or overwrites
    them. Locally-edited *tracked* files are reconciled against a stored hash
    manifest (which always records the PRISTINE upstream hash per path):
      - upstream-only change   -> new content applied
      - user-only change       -> local content kept
      - changed on both sides  -> local content KEPT, with a warning

Run as a module from the Workbench server (fast actions: check / refs / save)
or as a CLI (long actions, streamed into a Workbench job console: update / init):

    python -m scm_workbench.repo_sync init   --repo scm   [--tarball path/to/local.tar.gz]
    python -m scm_workbench.repo_sync update --repo scm   [--force-full]
    python -m scm_workbench.repo_sync check  --repo scm   [--json]
    python -m scm_workbench.repo_sync refs   --repo scm

Exit codes: 0 ok, 1 failure (message on stdout, or {"error": ...} with --json).
"""

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from pathlib import Path, PurePosixPath

USER_AGENT = "scm-workbench/1.1"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DIFF_FILE_CAP = 300          # GitHub's compare API returns at most this many files
GH_JSON_CAP = 8 * 1024 * 1024
RAW_FILE_CAP = 256 * 1024 * 1024
TARBALL_CAP = 1024 * 1024 * 1024
TAR_MEMBER_CAP = TARBALL_CAP
REFS_RESULT_CAP = 512 * 1024
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_GH_API_HOST = "api.github.com"
_GH_RAW_HOST = "raw.githubusercontent.com"
_GH_CODELOAD_HOST = "codeload.github.com"

REPOS = {
    "scm": {
        "owner": "Alan-Cha", "repo": "silhouette-card-maker",
        "name": "silhouette-card-maker", "rel": "repos/silhouette-card-maker",
        "requirements": "requirements.txt",
        # default to the latest *release* — unreleased main can carry breaking
        # changes between releases (scm-extras publishes no releases, so it
        # defaults to its main branch)
        "default_source": "latest-release",
    },
    "extras": {
        "owner": "Alan-Cha", "repo": "scm-extras",
        "name": "scm-extras", "rel": "repos/scm-extras",
        "requirements": None,
        "default_source": "main",
    },
}


class RepoError(Exception):
    pass


def _text(value, label, limit=256, allow_empty=False):
    """Validate externally supplied text without ever echoing unbounded input."""
    if not isinstance(value, str):
        raise RepoError(f"invalid {label}")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        raise RepoError(f"invalid {label} encoding")
    if (not allow_empty and not raw) or len(raw) > limit:
        raise RepoError(f"invalid {label} length")
    if any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) == "Cc" for c in value):
        raise RepoError(f"invalid {label} control character")
    return value


def _brief(value, limit=120):
    """A bounded, single-line representation suitable for logs/errors."""
    s = value if isinstance(value, str) else repr(value)
    s = " ".join(s.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def validate_repo_key(key: str) -> str:
    _text(key, "repository key", 32)
    if key not in REPOS:
        raise RepoError("unknown repository")
    return key


def validate_source(source: str) -> str:
    """Validate a conservative ``git check-ref-format``-like ref selector."""
    source = _text(source, "source")
    if source != source.strip() or any(c.isspace() for c in source):
        raise RepoError("source must not contain whitespace")
    if source.startswith(("/", "\\", "-", ".")) or source.endswith(("/", ".")):
        raise RepoError("invalid source form")
    if re.match(r"^[A-Za-z]:", source) or "\\" in source:
        raise RepoError("invalid source form")
    if ".." in source or "@{" in source:
        raise RepoError("invalid source form")
    if any(c in source for c in "~^:?*[%#[]") or "://" in source:
        raise RepoError("invalid source form")
    parts = source.split("/")
    if any(not part or part in (".", "..") or part.startswith(".") or
           part.endswith(".") or part.lower().endswith(".lock")
           for part in parts):
        raise RepoError("invalid source path")
    # PurePosixPath documents the intended ref/path interpretation without
    # accepting platform-specific absolute or drive paths.
    if not isinstance(PurePosixPath(source), PurePosixPath):  # pragma: no cover
        raise RepoError("invalid source")
    return source


def validate_sha(value: str, label="SHA") -> str:
    _text(value, label, 40)
    if not _SHA_RE.fullmatch(value):
        raise RepoError(f"invalid {label}")
    return value


def validate_repo_path(path: str) -> str:
    """Validate a portable, repository-relative POSIX path."""
    path = _text(path, "repository path", 4096)
    if path.startswith("/") or path.startswith("\\") or "\\" in path:
        raise RepoError("invalid repository path")
    if re.match(r"^[A-Za-z]:", path) or path.startswith("//"):
        raise RepoError("invalid repository path")
    parts = path.split("/")
    if not 1 <= len(parts) <= 64 or any(p in ("", ".", "..") for p in parts):
        raise RepoError("invalid repository path")
    for part in parts:
        if len(part.encode("utf-8")) > 255:
            raise RepoError("repository path component too long")
    # Keep this explicitly POSIX: PurePosixPath never treats a Windows drive
    # or backslash as a portable repository path.
    PurePosixPath(path)
    return path


def _ensure_no_symlink_components(path: Path) -> Path:
    """Reject symlinks in an I/O path, including a symlink at the leaf."""
    p = Path(os.path.abspath(os.fspath(path)))
    current = Path(p.anchor) if p.anchor else Path()
    for part in p.parts[1:] if p.anchor else p.parts:
        current = current / part
        if current.is_symlink():
            # macOS exposes /tmp and /var as aliases into /private; these are
            # platform roots, not an application-controlled escape.
            resolved = current.resolve(strict=False)
            if current.parent == Path(current.anchor) and resolved.parts[:2] == (current.anchor, "private"):
                current = resolved
                continue
            raise RepoError("refusing a path containing a symbolic link")
    return p


def safe_path(root: Path, relative: str) -> Path:
    """Return a repository path with canonical containment and no symlinks."""
    relative = validate_repo_path(relative)
    root_input = Path(root)
    _ensure_no_symlink_components(root_input)
    root = root_input if _WINDOWS_FALLBACK else root_input.resolve()
    _ensure_no_symlink_components(root)
    candidate = root.joinpath(*relative.split("/"))
    _ensure_no_symlink_components(candidate)
    canonical = candidate.resolve(strict=False)
    try:
        canonical.relative_to(root)
    except ValueError:
        raise RepoError("path escapes repository")
    return candidate


def safe_destination(path: Path) -> Path:
    """Check an arbitrary destination before creating or writing it."""
    p = _ensure_no_symlink_components(Path(path))
    canonical = p.resolve(strict=False)
    # A symlink component has already been rejected; this containment check
    # catches unusual path spellings such as a parent containing '..'.
    try:
        canonical.relative_to(Path(p.anchor).resolve())
    except ValueError:
        raise RepoError("destination path escapes its root")
    return p


# Descriptor-relative operations are the write boundary.  The POSIX path uses
# openat-style traversal so a checked parent cannot be swapped for a symlink
# between validation and the final open.  Windows lacks the corresponding
# dir_fd/O_NOFOLLOW primitives; its fallback rechecks every component around
# each operation and is only used for the platform's own staging paths.
_DESCRIPTOR_IO = (
    os.name != "nt" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and
    os.open in getattr(os, "supports_dir_fd", set()) and
    os.mkdir in getattr(os, "supports_dir_fd", set()) and
    os.unlink in getattr(os, "supports_dir_fd", set())
)
# Tests may force this abstraction on non-Windows with a mocked
# _windows_open_checked implementation; production only selects it on Windows.
_WINDOWS_FALLBACK = os.name == "nt"


def _windows_final_path(kernel32, handle):
    import ctypes
    size = 512
    while size <= 32768:
        buf = ctypes.create_unicode_buffer(size)
        got = kernel32.GetFinalPathNameByHandleW(handle, buf, size, 0)
        if got == 0:
            raise RepoError("could not resolve Windows file handle")
        if got < size - 1:
            value = buf.value
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return os.path.normcase(os.path.normpath(value))
        size *= 2
    raise RepoError("Windows file handle path is too long")


def _windows_open_checked(path: Path, write=False, create_parents=False,
                          mode=0o644, expected_root=None, expected_path=None,
                          directory=False, delete=False):
    """Open a Windows file only after CreateFileW handle validation.

    FILE_FLAG_OPEN_REPARSE_POINT makes the handle refer to the reparse point
    itself.  We reject that handle and every reparse parent before a writable
    handle is truncated.  A same-user process can still replace an ancestor
    after this final check; Windows has no portable openat equivalent here, so
    callers fail closed on every check but cannot claim immunity from that
    post-check race.
    """
    if os.name != "nt":
        raise RepoError("Windows safe I/O is unavailable on this platform")
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                    wintypes.DWORD, wintypes.DWORD]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                           ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
    kernel32.SetEndOfFile.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    GENERIC_READ, GENERIC_WRITE, DELETE = 0x80000000, 0x40000000, 0x00010000
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE = 1, 2, 4
    OPEN_EXISTING, CREATE_NEW = 3, 1
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_BEGIN = 0

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("attrs", wintypes.DWORD),
            ("creation", wintypes.FILETIME),
            ("last_access", wintypes.FILETIME),
            ("last_write", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    def create(candidate, access, disposition, directory=False):
        flags = FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= FILE_FLAG_BACKUP_SEMANTICS
        handle = kernel32.CreateFileW(
            str(candidate), access,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, disposition, flags, None)
        handle_value = handle.value if hasattr(handle, "value") else handle
        if handle_value in (None, INVALID_HANDLE_VALUE):
            return None, ctypes.get_last_error()
        return handle, 0

    def check(handle, root_final=None, exact=None, directory=False):
        info = _FileInfo()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise RepoError("could not inspect Windows file handle")
        if info.attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            raise RepoError("refusing a Windows reparse point")
        is_dir = bool(info.attrs & FILE_ATTRIBUTE_DIRECTORY)
        if is_dir != directory:
            raise RepoError("refusing an unexpected Windows file type")
        final = _windows_final_path(kernel32, handle)
        if root_final is not None:
            prefix = root_final.rstrip("\\") + "\\"
            if final != root_final and not final.startswith(prefix):
                raise RepoError("Windows handle escaped its trusted root")
        if exact is not None and final != os.path.normcase(os.path.normpath(str(exact))):
            raise RepoError("Windows handle resolved to an unexpected path")

    candidate = Path(path)
    checked = safe_destination(candidate)
    # Do not resolve through a Windows reparse point before CreateFileW gets a
    # chance to inspect it.  abspath normalizes .. without following links.
    expected = (Path(os.path.abspath(os.fspath(expected_path)))
                if expected_path is not None else Path(os.path.abspath(os.fspath(checked))))
    root = (Path(os.path.abspath(os.fspath(expected_root)))
            if expected_root is not None else Path(expected.anchor))
    # Walk/create every parent using checked directory handles before opening
    # the leaf.  This rejects reparse parents instead of following them.
    parent = expected.parent
    parent_parts = []
    drive_root = Path(expected.anchor)
    try:
        parent_parts = list(parent.relative_to(drive_root).parts)
    except ValueError:
        raise RepoError("invalid Windows path root")
    root_handle, root_error = create(root, GENERIC_READ, OPEN_EXISTING, directory=True)
    if root_handle is None:
        raise RepoError("could not open the trusted Windows root")
    try:
        check(root_handle, None, directory=True)
        root_final = _windows_final_path(kernel32, root_handle)
    finally:
        kernel32.CloseHandle(root_handle)

    current = drive_root
    for part in parent_parts:
        current = current / part
        if create_parents:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise RepoError(f"could not create Windows parent: {_brief(exc)}")
        handle, error = create(current, GENERIC_READ, OPEN_EXISTING, directory=True)
        if handle is None:
            raise RepoError("could not open a Windows parent directory")
        try:
            check(handle, None, directory=True)
        finally:
            kernel32.CloseHandle(handle)

    access = GENERIC_READ | (GENERIC_WRITE if write else 0) | (DELETE if delete else 0)
    handle, error = create(expected, access, OPEN_EXISTING, directory=directory)
    new_file = False
    if handle is None and write and error in (2, 3):  # file/path not found
        handle, error = create(expected, access, CREATE_NEW, directory=directory)
        new_file = handle is not None
    if handle is None:
        raise RepoError("could not open Windows file safely")
    try:
        check(handle, root_final, exact=expected, directory=directory)
        if directory:
            return None
        if delete:
            class _Disposition(ctypes.Structure):
                _fields_ = [("DeleteFile", wintypes.BOOL)]
            disposition = _Disposition(True)
            if not kernel32.SetFileInformationByHandle(
                    handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise RepoError("could not delete Windows file handle")
            return None
        if write and not new_file:
            if not kernel32.SetFilePointerEx(handle, ctypes.c_longlong(0), None, FILE_BEGIN):
                raise RepoError("could not position Windows file handle")
            if not kernel32.SetEndOfFile(handle):
                raise RepoError("could not truncate Windows file handle")
        fd_flags = getattr(os, "O_BINARY", 0)
        handle_value = handle.value if hasattr(handle, "value") else handle
        fd = msvcrt.open_osfhandle(handle_value, (os.O_RDWR if write else os.O_RDONLY) | fd_flags)
        handle = None  # ownership transferred to the CRT descriptor
        return os.fdopen(fd, "wb" if write else "rb")
    except Exception:
        # CREATE_NEW may have made an empty file before a final-handle check
        # failed.  Remove it through that same handle; never path-delete it.
        if handle is not None and new_file:
            try:
                class _Disposition(ctypes.Structure):
                    _fields_ = [("DeleteFile", wintypes.BOOL)]
                disposition = _Disposition(True)
                kernel32.SetFileInformationByHandle(
                    handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition))
            except Exception:
                pass
        raise
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def _open_dir_chain(root: Path, parts, create=False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(os.fspath(root), flags)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _absolute_parts(path: Path):
    checked = safe_destination(Path(path))
    canonical = checked.resolve(strict=False)
    root = Path(canonical.anchor)
    rel = canonical.relative_to(root)
    parts = list(rel.parts)
    if not parts:
        raise RepoError("refusing to operate on a filesystem root")
    return root, parts


def _secure_open_relative(root: Path, relative: str, write=False,
                          create_parents=False, mode=0o644):
    relative = validate_repo_path(relative)
    root_input = Path(root)
    safe_destination(root_input)
    root = root_input if _WINDOWS_FALLBACK else root_input.resolve()
    safe_destination(root)
    parts = relative.split("/")
    leaf = parts[-1]
    if _DESCRIPTOR_IO and not _WINDOWS_FALLBACK:
        parent_fd = _open_dir_chain(root, parts[:-1], create=create_parents)
        try:
            if write:
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
                fd = os.open(leaf, flags, mode, dir_fd=parent_fd)
            else:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RepoError("refusing a non-regular repository file")
            return os.fdopen(fd, "wb" if write else "rb")
        except Exception:
            os.close(fd)
            raise
    if _WINDOWS_FALLBACK:
        target = root.joinpath(*parts)
        return _windows_open_checked(target, write=write,
                                     create_parents=create_parents, mode=mode,
                                     expected_root=root, expected_path=target)
    target = safe_path(root, relative)
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_destination(target)
        try:
            fh = open(target, "wb")
        except OSError as exc:
            raise RepoError(f"could not open repository file: {_brief(exc)}")
        try:
            safe_destination(target)
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise RepoError("refusing a non-regular repository file")
            return fh
        except Exception:
            fh.close()
            raise
    safe_destination(target)
    try:
        fh = open(target, "rb")
    except OSError as exc:
        raise RepoError(f"could not open repository file: {_brief(exc)}")
    try:
        safe_destination(target)
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise RepoError("refusing a non-regular repository file")
        return fh
    except Exception:
        fh.close()
        raise


def _secure_open_absolute(path: Path, write=False, create_parents=False,
                          mode=0o644):
    if _WINDOWS_FALLBACK:
        return _windows_open_checked(Path(path), write=write,
                                     create_parents=create_parents, mode=mode,
                                     expected_path=Path(path))
    root, parts = _absolute_parts(Path(path))
    if _DESCRIPTOR_IO:
        parent_fd = _open_dir_chain(root, parts[:-1], create=create_parents)
        leaf = parts[-1]
        try:
            if write:
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
                fd = os.open(leaf, flags, mode, dir_fd=parent_fd)
            else:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RepoError("refusing a non-regular file")
            return os.fdopen(fd, "wb" if write else "rb")
        except Exception:
            os.close(fd)
            raise
    target = Path(path)
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_destination(target)
        try:
            fh = open(target, "wb")
        except OSError as exc:
            raise RepoError(f"could not open file: {_brief(exc)}")
        try:
            safe_destination(target)
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise RepoError("refusing a non-regular file")
            return fh
        except Exception:
            fh.close()
            raise
    safe_destination(target)
    try:
        fh = open(target, "rb")
    except OSError as exc:
        raise RepoError(f"could not open file: {_brief(exc)}")
    try:
        safe_destination(target)
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise RepoError("refusing a non-regular file")
        return fh
    except Exception:
        fh.close()
        raise


def _secure_write_bytes(path: Path, data: bytes):
    with _secure_open_absolute(path, write=True, create_parents=True) as fh:
        fh.write(data)


def _secure_copy_file(source: Path, root: Path, relative: str):
    with _secure_open_absolute(Path(source), write=False) as src:
        mode = stat.S_IMODE(os.fstat(src.fileno()).st_mode)
        with _secure_open_relative(root, relative, write=True,
                                   create_parents=True, mode=mode) as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
            try:
                os.fchmod(dst.fileno(), mode)
            except (AttributeError, OSError):
                pass


def _secure_mkdir_relative(root: Path, relative: str, mode=0o755):
    relative = validate_repo_path(relative)
    root_input = Path(root)
    safe_destination(root_input)
    root = root_input if _WINDOWS_FALLBACK else root_input.resolve()
    safe_destination(root)
    parts = relative.split("/")
    if _DESCRIPTOR_IO and not _WINDOWS_FALLBACK:
        parent_fd = _open_dir_chain(root, parts[:-1], create=True)
        leaf = parts[-1]
        try:
            try:
                os.mkdir(leaf, mode, dir_fd=parent_fd)
            except FileExistsError:
                pass
            fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            os.fchmod(fd, mode & 0o777)
        finally:
            os.close(fd)
        return
    if _WINDOWS_FALLBACK:
        for index in range(1, len(parts) + 1):
            target = root.joinpath(*parts[:index])
            try:
                target.mkdir()
            except FileExistsError:
                pass
            _windows_open_checked(target, expected_root=root, expected_path=target,
                                  directory=True)
        return
    target = safe_path(root, relative)
    target.mkdir(parents=True, exist_ok=True)
    safe_destination(target)
    if target.is_symlink() or not target.is_dir():
        raise RepoError("refusing an unsafe directory")
    try:
        target.chmod(mode & 0o777)
    except OSError:
        pass


def _secure_unlink_relative(root: Path, relative: str):
    relative = validate_repo_path(relative)
    root_input = Path(root)
    safe_destination(root_input)
    root = root_input if _WINDOWS_FALLBACK else root_input.resolve()
    safe_destination(root)
    parts = relative.split("/")
    if _DESCRIPTOR_IO and not _WINDOWS_FALLBACK:
        parent_fd = _open_dir_chain(root, parts[:-1], create=False)
        try:
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise RepoError("refusing to unlink a symbolic link")
            os.unlink(parts[-1], dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        return
    if _WINDOWS_FALLBACK:
        target = root.joinpath(*parts)
        _windows_open_checked(target, delete=True, expected_root=root,
                              expected_path=target)
        return
    target = safe_path(root, relative)
    safe_destination(target)
    if target.is_symlink():
        raise RepoError("refusing to unlink a symbolic link")
    target.unlink()


def _secure_unlink_absolute(path: Path):
    path = Path(path)
    if _WINDOWS_FALLBACK:
        _windows_open_checked(path, delete=True, expected_path=path)
        return
    root, parts = _absolute_parts(path)
    if _DESCRIPTOR_IO and not _WINDOWS_FALLBACK:
        parent_fd = _open_dir_chain(root, parts[:-1], create=False)
        try:
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise RepoError("refusing to unlink a symbolic link")
            os.unlink(parts[-1], dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        return
    target = safe_destination(path)
    if target.is_symlink():
        raise RepoError("refusing to unlink a symbolic link")
    target.unlink()


def _secure_hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with _secure_open_absolute(Path(path), write=False) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# Locations & state
# ----------------------------------------------------------------------------

def data_dir() -> Path:
    p = os.environ.get("SCM_WORKBENCH_DATA")
    if p:
        return Path(p).expanduser().resolve()
    # Dev checkout: the data area lives at the repo root (one level up from
    # this package). Inside an app bundle the launcher always sets the env var.
    return Path(__file__).resolve().parent.parent / "data"


def state_file() -> Path:
    return data_dir() / "repos-state.json"


def manifest_file(key: str) -> Path:
    return data_dir() / f"repos-manifest-{validate_repo_key(key)}.json"


def _atomic_write_json(path: Path, value) -> None:
    """Replace JSON metadata without shared temporary names or torn writes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=1, ensure_ascii=False).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # Directory fsync is not available on every supported platform/filesystem.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (AttributeError, OSError):
            pass
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _load_json_object(path: Path, label: str, missing=None):
    """Strict metadata reader used before any read-modify-write operation."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return missing
    except OSError as exc:
        raise RepoError(f"could not read {label}: {_brief(exc)}")
    if len(raw) > GH_JSON_CAP:
        raise RepoError(f"{label} is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RepoError(f"malformed {label}")
    if not isinstance(value, dict):
        raise RepoError(f"invalid {label} shape")
    return value


def _validate_state_shape(st: dict) -> dict:
    if not isinstance(st, dict):
        raise RepoError("invalid repository state shape")
    for key, value in st.items():
        if key not in REPOS:
            continue
        if not isinstance(value, dict):
            raise RepoError("invalid repository state entry")
        deployed = value.get("deployed")
        if deployed is not None and not isinstance(deployed, dict):
            raise RepoError("invalid deployed state shape")
        if deployed:
            if not isinstance(deployed.get("sha"), str):
                raise RepoError("invalid deployed SHA")
            validate_sha(deployed["sha"], "deployed SHA")
        checks = value.get("last_check")
        if checks is not None and not isinstance(checks, dict):
            raise RepoError("invalid repository check state shape")
        if isinstance(checks, dict):
            if checks.get("checked") is not None and not isinstance(checks["checked"], dict):
                raise RepoError("invalid repository check result shape")
            checked_at = checks.get("checked_at")
            if checked_at is not None and (isinstance(checked_at, bool) or
                                            not isinstance(checked_at, (int, float))):
                raise RepoError("invalid repository check timestamp")
    return st


def _load_state_for_mutation() -> dict:
    return _validate_state_shape(_load_json_object(state_file(), "repository state", {}))


def load_state() -> dict:
    # Read-only views remain deliberately tolerant: a damaged metadata file
    # should be reportable in the UI, but must never be guessed over by a
    # mutating operation.
    try:
        value = _load_json_object(state_file(), "repository state", {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_state(st: dict) -> None:
    _validate_state_shape(st)
    _atomic_write_json(state_file(), st)


def _acquire_windows_lock(fh):
    """Acquire an msvcrt byte lock without LK_LOCK's short retry limit."""
    import msvcrt
    fh.seek(0, 2)
    if fh.tell() == 0:
        fh.write(" ")
        fh.flush()
    fh.seek(0)
    # msvcrt reports a held byte as EACCES/EDEADLK (and on some Python/CRT
    # combinations as ERROR_LOCK_VIOLATION).  Only those contention errors are
    # retried; permission/I/O/etc. failures are real errors.
    contention_errno = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    contention_winerror = {32, 33, 36, 170, 212}
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return msvcrt
        except Exception as exc:
            if (not isinstance(exc, OSError) or
                    (getattr(exc, "errno", None) not in contention_errno and
                     getattr(exc, "winerror", None) not in contention_winerror)):
                raise RepoError("could not acquire repository lock")
            time.sleep(0.05)


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    """Portable exclusive lock for one stable lock-file inode."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    locked = False
    msvcrt = None
    try:
        if sys.platform == "win32":
            msvcrt = _acquire_windows_lock(fh)
            locked = True
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            if locked and sys.platform == "win32":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


@contextlib.contextmanager
def _state_lock():
    """Global state mutex. Operation locks must always be acquired first."""
    with _file_lock(data_dir() / ".repos-lock"):
        yield


@contextlib.contextmanager
def _repo_operation_lock(key: str):
    """Serialize all mutating/checking work for one managed repository."""
    key = validate_repo_key(key)
    with _file_lock(data_dir() / f".repos-{key}-lock"):
        yield


# Short private name retained for focused callers and tests.
_repo_lock = _repo_operation_lock


# ----------------------------------------------------------------------------
# Live progress (the UI polls this through /api/info while a clone/update runs)
# progress.json: {"scm": {"stage": "download", "done": 123456, "total": 590000000}, …}
# ----------------------------------------------------------------------------

def progress_file() -> Path:
    return data_dir() / "progress.json"


def _read_progress() -> dict:
    try:
        with open(progress_file()) as f:
            return json.load(f)
    except Exception:
        return {}


def load_progress() -> dict:
    return _read_progress()


# Per-process rate state for the progress rows: speed/ETA are computed on
# every callback tick, even for ticks whose file write is throttled.
_prog_rate = {}


def _rate_for(key: str, done: int, total: int, stage):
    """Exponential-moving-average speed (bytes/s) + ETA (s) for one tick."""
    st = _prog_rate.setdefault(key, {})
    if stage is not None:
        st.clear()
    now = time.time()
    speed = st.get("speed")
    last_ts, last_done = st.get("ts"), st.get("done")
    if last_ts is not None and last_done is not None and now > last_ts and done > last_done:
        inst = (done - last_done) / (now - last_ts)
        if speed is not None:
            inst = 0.6 * inst + 0.4 * speed
        speed = int(inst)
    st["ts"], st["done"] = now, done
    if speed is not None:
        st["speed"] = speed
    eta = None
    if speed and total and total >= 1000 and total > done:
        eta = max(1, int((total - done) / speed))
    return speed, eta


def set_progress(key: str, **kw) -> None:
    """Record progress for one repo. Writes are throttled: while a download
    streams, a cb tick per 1 MB chunk only rewrites the file every ~2% of
    progress (or on a stage change), so the UI stays smooth, not chatty.
    Every accepted write stamps ts: the info view drops rows whose stamp is
    old, so a row whose final clear never landed (a crash mid-bootstrap) can
    never present itself as a live stage again."""
    try:
        with _state_lock():
            d = _read_progress()
            cur = d.get(key) or {}
            stage = kw.get("stage", cur.get("stage"))
            row = {**cur, **kw}
            if kw.get("done") is not None:
                speed, eta = _rate_for(key, int(kw["done"]), int(row.get("total") or 0), kw.get("stage"))
                if speed:
                    row["speed"] = speed
                if eta is not None:
                    row["eta"] = eta
            if kw.get("done") is not None and cur.get("total") and stage == cur.get("stage"):
                span = max(1, int(cur["total"] * 0.02))
                if abs(int(kw["done"]) - int(cur.get("done") or 0)) < span:
                    return
            row["ts"] = time.time()
            d[key] = row
            _atomic_write_json(progress_file(), d)
    except Exception:
        pass


def clear_progress(key: str) -> None:
    try:
        _prog_rate.pop(key, None)
        with _state_lock():
            d = _read_progress()
            if key in d:
                del d[key]
                _atomic_write_json(progress_file(), d)
    except Exception:
        pass


def repo_dir(key: str) -> Path:
    return data_dir() / REPOS[validate_repo_key(key)]["rel"]


def load_manifest(key: str) -> dict:
    try:
        value = _load_json_object(manifest_file(key), "repository manifest", {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _validate_manifest_shape(man: dict) -> dict:
    if not isinstance(man, dict) or not isinstance(man.get("files"), dict):
        raise RepoError("invalid repository manifest shape")
    for path, digest in man["files"].items():
        validate_repo_path(path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RepoError("invalid repository manifest hash")
    if man.get("sha") is not None:
        validate_sha(man["sha"], "manifest SHA")
    return man


def _load_manifest_for_mutation(key: str):
    value = _load_json_object(manifest_file(key), "repository manifest", None)
    return None if value is None else _validate_manifest_shape(value)


def save_manifest(key: str, man: dict) -> None:
    key = validate_repo_key(key)
    _validate_manifest_shape(man)
    _atomic_write_json(manifest_file(key), man)


def load_source(key: str) -> str:
    """Which ref the user asked for: 'main' | 'latest-release' | a tag/sha (pinned)."""
    key = validate_repo_key(key)
    st = load_state().get(key) or {}
    if st.get("source") is not None:
        return validate_source(st["source"])
    settings_file = data_dir() / "settings.json"
    try:
        settings = json.loads(settings_file.read_text())
        source = (settings.get("repos", {}).get(key) or {}).get("source")
        if source is not None:
            return validate_source(source)
    except RepoError:
        raise
    except Exception:
        pass
    return validate_source(REPOS[key].get("default_source", "main"))


def _settings_source(key: str):
    """Return the explicit settings source, preserving load_source semantics."""
    try:
        settings = json.loads((data_dir() / "settings.json").read_text())
        repos = settings.get("repos", {})
        value = (repos.get(key) or {}).get("source") if isinstance(repos, dict) else None
        return validate_source(value) if value is not None else None
    except RepoError:
        raise
    except Exception:
        return None


def _source_snapshot(key: str, st: dict = None):
    """Capture the effective source and only its currently relevant authority.

    State is the higher-priority authority.  In particular, do not inspect a
    malformed settings source when a valid state source already determines the
    answer: an ignored lower-priority setting must not block an operation.
    """
    key = validate_repo_key(key)
    if st is None:
        st = load_state()
    entry = st.get(key) or {}
    state_source = entry.get("source")
    if state_source is not None:
        state_source = validate_source(state_source)
        return state_source, ("state", state_source)
    settings_source = _settings_source(key)
    if settings_source is not None:
        return settings_source, ("settings", settings_source)
    default = validate_source(REPOS[key].get("default_source", "main"))
    return default, ("default", default)


def set_source(key: str, source: str) -> None:
    key = validate_repo_key(key)
    source = validate_source(source)
    # The operation lock is deliberately outside the state lock: this is the
    # repository -> global-state order used by init/update/check everywhere.
    with _repo_lock(key):
        with _state_lock():
            st = _load_state_for_mutation()
            r = st.get(key)
            if r is None:
                r = {}
                st[key] = r
            if not isinstance(r, dict):
                raise RepoError("invalid repository state entry")
            r["source"] = source
            # A check is tied to the effective source; never serve its target
            # after changing that source.
            r.pop("last_check", None)
            save_state(st)


# ----------------------------------------------------------------------------
# GitHub over plain HTTPS
# ----------------------------------------------------------------------------

def _validate_https_host(url: str, allowed_hosts) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise RepoError("GitHub response used an invalid URL")
    if (parsed.scheme.lower() != "https" or not hostname or
            hostname.lower() not in set(allowed_hosts) or port is not None or
            parsed.username is not None or parsed.password is not None or
            parsed.netloc.lower() != hostname.lower()):
        raise RepoError("GitHub response used an untrusted URL")
    return hostname.lower()


def _response_url(response, initial_url: str) -> str:
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        try:
            final = geturl()
        except Exception:
            raise RepoError("GitHub response URL was unavailable")
        if final is None:
            return initial_url
        if not isinstance(final, str):
            raise RepoError("GitHub response URL was invalid")
        return final
    # urllib responses always provide geturl(); this fallback keeps small local
    # test doubles and older embedders compatible without weakening real checks.
    return initial_url


def gh_json(path: str, params: dict = None):
    path = _text(path, "API path", 2048)
    if not path.startswith("/") or "://" in path or "\\" in path:
        raise RepoError("invalid GitHub API path")
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            _validate_https_host(_response_url(r, url), {_GH_API_HOST})
            length = r.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except (TypeError, ValueError):
                    raise RepoError("GitHub API returned an invalid content length")
                if declared < 0:
                    raise RepoError("GitHub API returned an invalid content length")
                if declared > GH_JSON_CAP:
                    raise RepoError("GitHub API response is too large")
            chunks, total = [], 0
            while True:
                b = r.read(min(1 << 16, GH_JSON_CAP + 1 - total))
                if not b:
                    break
                chunks.append(b)
                total += len(b)
                if total > GH_JSON_CAP:
                    raise RepoError("GitHub API response is too large")
            try:
                value = json.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RepoError("GitHub API returned malformed JSON")
            if not isinstance(value, (dict, list)):
                raise RepoError("GitHub API returned an invalid JSON shape")
            return value
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RepoError("GitHub API rate limit or permission error — try again shortly.")
        if e.code == 404:
            return None
        raise RepoError(f"GitHub API error {e.code}")
    except RepoError:
        raise
    except Exception as e:
        raise RepoError(f"could not reach GitHub ({_brief(path)}): {_brief(e)}")


def gh_get_bytes(url: str, timeout: int = 120, progress_cb=None,
                 max_bytes: int = RAW_FILE_CAP):
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise RepoError("invalid download size limit")
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        raise RepoError("refusing download from an untrusted host")
    initial_host = _validate_https_host(url, {_GH_API_HOST, _GH_RAW_HOST})
    allowed_final = {initial_host}
    if (initial_host == _GH_API_HOST and "/tarball/" in parsed.path):
        allowed_final.add(_GH_CODELOAD_HOST)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except Exception as e:
        raise RepoError(f"download failed: {_brief(e)}")
    try:
        _validate_https_host(_response_url(r, url), allowed_final)
        header = r.headers.get("Content-Length")
        if header is not None:
            try:
                total = int(header)
            except (TypeError, ValueError):
                raise RepoError("download returned an invalid content length")
            if total < 0:
                raise RepoError("download returned an invalid content length")
        else:
            total = 0
        if total > max_bytes:
            raise RepoError("download exceeds its size limit")
        chunks, done = [], 0
        if progress_cb is not None:
            progress_cb(0, total)
        while True:
            b = r.read(min(1 << 20, max_bytes + 1 - done))
            if not b:
                break
            chunks.append(b)
            done += len(b)
            if done > max_bytes:
                raise RepoError("download exceeds its size limit")
            if progress_cb is not None:
                progress_cb(done, total)
        return b"".join(chunks)
    finally:
        r.close()


def repo_api(key: str) -> dict:
    key = validate_repo_key(key)
    d = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}")
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise RepoError("GitHub repository response has an invalid shape")
    return d


def commit_info(key: str, ref: str):
    key = validate_repo_key(key)
    ref = validate_source(ref)
    d = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/commits/{urllib.parse.quote(ref, safe='')}")
    if d is None:
        return None
    if not isinstance(d, dict) or not d.get("sha"):
        return None
    sha = validate_sha(d["sha"])
    c = d.get("commit")
    if c is not None and not isinstance(c, dict):
        raise RepoError("GitHub commit response has an invalid shape")
    c = c or {}
    committer = c.get("committer") or {}
    author = c.get("author") or {}
    if not isinstance(committer, dict) or not isinstance(author, dict):
        raise RepoError("GitHub commit response has an invalid shape")
    date = committer.get("date") or author.get("date")
    if date is not None:
        date = _text(date, "commit date", 128)
    message = c.get("message") or ""
    if not isinstance(message, str):
        message = ""
    return {
        "sha": sha,
        "ref": ref,
        "date": date,
        "message": message.splitlines()[0][:100] if message.splitlines() else "",
    }


def _source_is_builtin_default(key: str) -> bool:
    """True when no user-chosen source is on record (state or settings)."""
    key = validate_repo_key(key)
    if (load_state().get(key) or {}).get("source"):
        return False
    try:
        settings = json.loads((data_dir() / "settings.json").read_text())
    except Exception:
        return True
    return not (settings.get("repos", {}).get(key) or {}).get("source")


def resolve_target(key: str, source: str) -> dict:
    """Turn the user's source choice into a concrete {sha, ref, date}."""
    key = validate_repo_key(key)
    source = validate_source(source)
    if source == "latest-release":
        rel = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/releases/latest")
        if rel is not None and not isinstance(rel, dict):
            raise RepoError("GitHub release response has an invalid shape")
        if rel and rel.get("tag_name"):
            tag = validate_source(rel["tag_name"])
            info = commit_info(key, f"refs/tags/{tag}") or commit_info(key, tag)
            if info:
                info["ref"] = tag
                name = rel.get("name")
                info["release_name"] = _text(name, "release name", 256, allow_empty=True) if isinstance(name, str) else None
                return info
        if _source_is_builtin_default(key):
            # built-in default but no releases published (yet) — track the
            # default branch instead so a first launch never dead-ends
            source = "main"
        else:
            raise RepoError(f"no GitHub releases are published for {REPOS[key]['name']} yet — use “Latest (main)” or a pinned ref.")
    ref = source if source != "main" else validate_source(repo_api(key).get("default_branch") or "main")
    info = commit_info(key, ref)
    if not info:
        raise RepoError(f"could not resolve ref “{_brief(ref)}” for {REPOS[key]['name']}.")
    return info


def list_refs(key: str) -> dict:
    key = validate_repo_key(key)
    api = repo_api(key)
    default = api.get("default_branch") or "main"
    try:
        default = validate_source(default)
    except RepoError:
        raise RepoError("GitHub repository returned an invalid default branch")
    meta = {"default_branch": default, "tags": [], "releases": []}
    tags = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/tags", {"per_page": 100})
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        raise RepoError("GitHub tags response has an invalid shape")
    for t in tags[:100]:
        if not isinstance(t, dict):
            continue
        commit = t.get("commit")
        if not isinstance(commit, dict):
            continue
        try:
            name = validate_source(t.get("name"))
            sha = validate_sha(commit.get("sha"))
        except RepoError:
            continue
        meta["tags"].append({"name": name, "sha": sha})
    rels = gh_json(f"/repos/{REPOS[key]['owner']}/{REPOS[key]['repo']}/releases", {"per_page": 30})
    if rels is None:
        rels = []
    if not isinstance(rels, list):
        raise RepoError("GitHub releases response has an invalid shape")
    for r in rels[:30]:
        if not isinstance(r, dict):
            continue
        try:
            tag = validate_source(r.get("tag_name"))
            name = r.get("name")
            date = r.get("published_at")
            if name is not None:
                name = _text(name, "release name", 256, allow_empty=True)
            if date is not None:
                date = _text(date, "release date", 128, allow_empty=True)
            prerelease = r.get("prerelease")
            if not isinstance(prerelease, bool):
                raise RepoError("invalid prerelease")
        except RepoError:
            continue
        meta["releases"].append({"tag": tag, "name": name, "date": date,
                                 "prerelease": prerelease})
    if len(json.dumps(meta, ensure_ascii=False).encode("utf-8")) > REFS_RESULT_CAP:
        raise RepoError("GitHub refs result is too large")
    return meta


def compare(key: str, base_sha: str, head_sha: str) -> dict:
    key = validate_repo_key(key)
    base_sha = validate_sha(base_sha, "base SHA")
    head_sha = validate_sha(head_sha, "head SHA")
    o, r = REPOS[key]["owner"], REPOS[key]["repo"]
    d = gh_json(f"/repos/{o}/{r}/compare/{base_sha}...{head_sha}")
    if d is None or not isinstance(d, dict):
        raise RepoError("compare API returned an invalid response")
    status = d.get("status")
    if not isinstance(status, str) or status not in {"ahead", "behind", "identical", "diverged"}:
        raise RepoError("compare API returned an invalid status")
    if "total_commits" not in d:
        raise RepoError("compare API returned no commit count")
    commits = d["total_commits"]
    if isinstance(commits, bool) or not isinstance(commits, int) or commits < 0:
        raise RepoError("compare API returned an invalid commit count")
    raw_files = d.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > DIFF_FILE_CAP:
        raise RepoError("compare API returned too many files")
    files = []
    for f in raw_files:
        if not isinstance(f, dict):
            raise RepoError("compare API returned an invalid file entry")
        path = validate_repo_path(f.get("filename"))
        previous = f.get("previous_filename")
        if previous is not None:
            previous = validate_repo_path(previous)
        file_status = f.get("status")
        if not isinstance(file_status, str) or file_status not in {"added", "modified", "removed", "renamed", "copied", "changed"}:
            raise RepoError("compare API returned an invalid file status")
        files.append({"path": path, "status": file_status, "previous": previous})
    return {"files": files, "too_many": len(files) >= DIFF_FILE_CAP,
            "commits": commits, "status": status}


def download_to(key: str, sha: str, path: str, dest: Path, log=print) -> int:
    key = validate_repo_key(key)
    sha = validate_sha(sha)
    path = validate_repo_path(path)
    dest = safe_destination(Path(dest))
    data = gh_get_bytes(f"{RAW}/{REPOS[key]['owner']}/{REPOS[key]['repo']}/{sha}/{urllib.parse.quote(path, safe='/')}")
    _secure_write_bytes(dest, data)
    n = len(data)
    if n >= 1024 * 1024:
        log(f"    ↓ {_brief(path)} ({n / 1e6:.1f} MB)")
    elif n >= 10 * 1024:
        log(f"    ↓ {_brief(path)} ({n // 1024} KB)")
    return n


def sha256_file(p: Path) -> str:
    return _secure_hash_file(Path(p))


def safe_members(members: list):
    """Strip the {owner}-{repo}-{sha}/ wrapper after strict tar validation."""
    if not members:
        raise RepoError("empty tarball")
    first = members[0]
    if not isinstance(first.name, str) or not first.isdir():
        raise RepoError("unexpected tarball layout")
    root_name = first.name.rstrip("/")
    if first.name not in (root_name, root_name + "/"):
        raise RepoError("unexpected tarball layout")
    if "/" in root_name or "\\" in root_name or re.match(r"^[A-Za-z]:", root_name) or root_name in ("", ".", ".."):
        raise RepoError("unsafe tarball wrapper")
    _text(root_name, "tarball path", 256)
    root = root_name + "/"
    seen = set()
    for i, m in enumerate(members):
        if not isinstance(m.name, str) or (i == 0 and m.name not in (root_name, root)) or (i != 0 and not m.name.startswith(root)):
            raise RepoError("unexpected tarball layout")
        if m.issym() or m.islnk() or m.isdev() or not (m.isdir() or m.isfile()):
            raise RepoError("unsafe tarball member type")
        if m.isfile() and (m.size < 0 or m.size > TAR_MEMBER_CAP):
            raise RepoError("tarball member is too large")
        rel = m.name[len(root):]
        if i == 0 and not rel:
            m.name = ""
            continue
        if not rel:
            raise RepoError("duplicate tarball path")
        had_trailing_slash = rel.endswith("/")
        if had_trailing_slash:
            if not m.isdir():
                raise RepoError("unsafe tarball member path")
            rel = rel[:-1]
        try:
            rel = validate_repo_path(rel)
        except RepoError:
            raise RepoError("unsafe path in tarball")
        if rel in seen:
            raise RepoError("duplicate path in tarball")
        seen.add(rel)
        m.name = rel  # strip the {owner}-{repo}-{sha}/ prefix
    return root


def extract_tarball(tar_path: Path, dest: Path, log=print):
    """Extract a GitHub tarball through descriptor-relative safe writes."""
    tar_path = safe_destination(Path(tar_path))
    dest = safe_destination(Path(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = safe_destination(dest)
    dest.mkdir(exist_ok=True)
    safe_destination(dest)
    with tarfile.open(tar_path, "r:*") as tf:
        members = tf.getmembers()
        safe_members(members)
        if members and members[0].name == "":
            members = members[1:]
        for member in members:
            name = validate_repo_path(member.name)
            mode = stat.S_IMODE(member.mode) or (0o755 if member.isdir() else 0o644)
            if member.isdir():
                _secure_mkdir_relative(dest, name, mode)
                continue
            if not member.isfile() or member.size < 0 or member.size > TAR_MEMBER_CAP:
                raise RepoError("unsafe tarball member")
            source = tf.extractfile(member)
            if source is None:
                raise RepoError("tarball member has no data")
            with source, _secure_open_relative(dest, name, write=True,
                                                create_parents=True,
                                                mode=mode) as target:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1 << 20, remaining))
                    if not chunk:
                        raise RepoError("truncated tarball member")
                    target.write(chunk)
                    remaining -= len(chunk)
                try:
                    os.fchmod(target.fileno(), mode & 0o777)
                except (AttributeError, OSError):
                    pass


def tracked_paths(tree_dir: Path) -> list:
    tree_input = Path(tree_dir)
    _ensure_no_symlink_components(tree_input)
    tree_dir = tree_input.resolve()
    _ensure_no_symlink_components(tree_dir)
    out = []
    for p in sorted(tree_dir.rglob("*")):
        rel = str(p.relative_to(tree_dir)).replace(os.sep, "/")
        rel = validate_repo_path(rel)
        safe_path(tree_dir, rel)
        if p.is_symlink():
            raise RepoError("refusing a symbolic link in repository tree")
        if p.is_file():
            out.append(rel)
    return out


# ----------------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------------
# manifest[path] is always the PRISTINE (upstream) sha256 of that path at the
# currently deployed commit.  local hash != manifest hash  ==  "user-edit".
#
#   apply:  local pristine  -> replace with new pristine
#           local user-edited, new pristine == local -> no-op
#           local user-edited, new pristine differs   -> KEEP local, warn
#           local missing                             -> place new pristine
#   delete: local pristine  -> remove
#           local user-edited -> KEEP local, warn (it becomes the user's file)

def _decide(local: Path, old_hash, new_pristine: Path):
    local = _ensure_no_symlink_components(Path(local))
    if new_pristine is not None:
        new_pristine = _ensure_no_symlink_components(Path(new_pristine))
    if not local.exists():
        return "place"
    new_hash = sha256_file(new_pristine) if new_pristine is not None else None
    cur = sha256_file(local)
    if new_hash is not None and cur == new_hash:
        return "noop"
    if old_hash is not None and cur == old_hash:
        return "replace"
    # local differs from the recorded pristine (a user edit):
    if new_hash is not None and old_hash is not None and old_hash == new_hash:
        return "keep-user"      # upstream didn't change it -> keep silently
    return "keep-conflict"       # changed on both sides


def apply_changes(key: str, man: dict, target: dict,
                  apply_ops: dict, delete_paths: list,
                  pristine_for, log=print) -> dict:
    """Validate the complete plan and all staging files before live mutation."""
    key = validate_repo_key(key)
    if not isinstance(man, dict) or not isinstance(man.get("files"), dict):
        raise RepoError("invalid repository manifest shape")
    old_files = dict(man["files"])
    for path, digest in old_files.items():
        validate_repo_path(path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RepoError("invalid repository manifest hash")
    if not isinstance(target, dict) or "sha" not in target:
        raise RepoError("invalid update target")
    validate_sha(target["sha"], "target SHA")
    if not isinstance(apply_ops, dict) or not isinstance(delete_paths, list):
        raise RepoError("invalid repository update plan")

    validated_ops = []
    apply_paths = set()
    previous_paths = set()
    for path, prev in apply_ops.items():
        path = validate_repo_path(path)
        if path in apply_paths:
            raise RepoError("duplicate update path")
        apply_paths.add(path)
        if prev is not None:
            prev = validate_repo_path(prev)
            if prev in previous_paths:
                raise RepoError("conflicting rename source")
            previous_paths.add(prev)
        validated_ops.append((path, prev))

    validated_deletes = []
    delete_set = set()
    for path in delete_paths:
        path = validate_repo_path(path)
        if path in delete_set:
            raise RepoError("duplicate delete path")
        if path in apply_paths or path in previous_paths:
            raise RepoError("conflicting update and delete paths")
        delete_set.add(path)
        validated_deletes.append(path)
    if apply_paths & previous_paths:
        raise RepoError("conflicting update paths")

    repo = safe_destination(repo_dir(key))
    # Resolve every local path before asking for staging content.  This makes
    # malformed later entries fail before any repository mutation is possible.
    for path in apply_paths | previous_paths | delete_set:
        safe_path(repo, path)

    pristine_by_path = {}
    staging_paths = set()
    for path, _prev in validated_ops:
        pristine = pristine_for(path)
        if pristine is not None:
            pristine = Path(pristine)
            safe_destination(pristine)
            if pristine.is_symlink() or not pristine.is_file():
                raise RepoError("invalid pristine staging file")
            canonical = str(pristine.resolve())
            if canonical in staging_paths:
                raise RepoError("duplicate pristine staging file")
            staging_paths.add(canonical)
        pristine_by_path[path] = pristine

    new_manifest = dict(old_files)
    applied = replaced = deleted = 0
    conflicts = []

    for path, prev in validated_ops:
        if prev is not None and prev != path:
            # upstream rename: drop the old local slot when it's still pristine,
            # warn when the user edited it (their copy would otherwise strand silently)
            old_local = safe_path(repo, prev)
            if old_local.exists():
                if sha256_file(old_local) == old_files.get(prev):
                    _secure_unlink_relative(repo, prev)
                else:
                    conflicts.append(prev)
            new_manifest.pop(prev, None)
        pristine = pristine_by_path[path]
        local = safe_path(repo, path)
        d = _decide(local, old_files.get(path), pristine)
        if pristine is None:
            if d in ("place", "replace"):
                log(f"    ! skipped {_brief(path)} (new content could not be fetched)")
            continue
        if d in ("place", "replace"):
            # The descriptor-relative copy recreates missing parents and
            # revalidates the destination at the final open boundary.
            safe_destination(local)
            _secure_copy_file(pristine, repo, path)
            new_manifest[path] = sha256_file(local)
            if d == "replace":
                replaced += 1
            else:
                applied += 1
        elif d == "keep-conflict":
            conflicts.append(path)
            new_manifest[path] = sha256_file(pristine)
        else:  # noop or keep-user: local already matches (or is the user's own edit of)
            new_manifest[path] = sha256_file(pristine)

    for path in validated_deletes:
        local = safe_path(repo, path)
        if not local.exists():
            new_manifest.pop(path, None)
            continue
        if sha256_file(local) == old_files.get(path):
            _secure_unlink_relative(repo, path)
            deleted += 1
        else:
            conflicts.append(path)
        new_manifest.pop(path, None)

    man2 = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date"),
            "files": new_manifest}
    return {"applied": applied + replaced, "deleted": deleted, "conflicts": conflicts, "manifest": man2}


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def _check_repo_locked(key: str, force: bool = False) -> dict:
    """Check implementation; caller holds the per-repository operation lock."""
    # A settings/state source can change while the network request is in flight
    # (the settings editor is not part of this module). Retry once rather than
    # caching a result for the wrong authority.
    for attempt in range(2):
        with _state_lock():
            st = _load_state_for_mutation()
            entry = st.get(key) or {}
            source, source_sig = _source_snapshot(key, st)
            deployed = entry.get("deployed")
            lc = entry.get("last_check") or {}
            if (not force and lc.get("checked") and
                    time.time() - lc.get("checked_at", 0) < 3600):
                checked = lc["checked"]
                # Cache entries from before source binding are misses.  The
                # effective source is sufficient here: ignored settings below
                # a state source do not invalidate, while settings-backed
                # changes do.
                if checked.get("source") == source:
                    return {"repo": key, "ok": True, "cached": True,
                            "deployed": deployed, "target": checked.get("target"),
                            "up_to_date": checked.get("up_to_date"),
                            "source": source}
        try:
            target = resolve_target(key, source)
        except RepoError as e:
            return {"repo": key, "ok": False, "error": str(e), "deployed": deployed}

        with _state_lock():
            fresh = _load_state_for_mutation()
            fresh_entry = fresh.get(key) or {}
            fresh_source, fresh_sig = _source_snapshot(key, fresh)
            if fresh_sig != source_sig or fresh_source != source:
                if attempt == 0:
                    continue
                raise RepoError("repository source changed while checking; retry")
            fresh_deployed = fresh_entry.get("deployed")
            res = {"repo": key, "ok": True, "cached": False, "target": target,
                   "deployed": fresh_deployed, "source": source,
                   "up_to_date": bool(fresh_deployed and
                                      fresh_deployed.get("sha") == target["sha"])}
            if not fresh_deployed:
                res["note"] = "no managed copy yet — run “Download latest” first."
            r = fresh.setdefault(key, {})
            old_check = r.get("last_check") if isinstance(r.get("last_check"), dict) else {}
            r["last_check"] = {**old_check, "checked": res, "checked_at": time.time()}
            save_state(fresh)
            return res
    raise RepoError("repository source changed while checking; retry")


def check_repo(key: str, force: bool = False) -> dict:
    """Resolve and cache a check while serializing it with init/update."""
    key = validate_repo_key(key)
    with _repo_lock(key):
        return _check_repo_locked(key, force=force)


def cmd_check(key: str, as_json: bool = False):
    key = validate_repo_key(key)
    res = check_repo(key, force=True)
    if as_json:
        print(json.dumps(res))
        return res
    t, d = res.get("target"), res.get("deployed")
    if res.get("ok") and res["up_to_date"] and t:
        print(f"{REPOS[key]['name']}: up to date at {t['ref']} ({t['sha'][:7]}, {(t.get('date') or '?')[:10]})")
    elif res.get("ok") and d and t:
        print(f"{REPOS[key]['name']}: update available — {t['ref']} ({t['sha'][:7]}, {(t.get('date') or '?')[:10]}) "
              f"is newer than deployed {d.get('ref')} ({d['sha'][:7]})")
    elif res.get("ok"):
        print(f"{REPOS[key]['name']}: {res.get('note', 'no managed copy yet')}")
    else:
        print(f"check {key}: {res.get('error')}")
    return res


def cmd_refs(key: str, as_json: bool = False):
    key = validate_repo_key(key)
    meta = list_refs(key)
    if as_json:
        print(json.dumps(meta))
    else:
        print(f"{REPOS[key]['name']}: default branch “{meta['default_branch']}”, "
              f"{len(meta['tags'])} tag(s), {len(meta['releases'])} release(s)")
        for t in meta["tags"][:10]:
            print(f"  tag {t['name']} @ {t['sha'][:7] if t['sha'] else '?'}")
    return meta


_KNOWN_BAD_PINS = {
    # Upstream pins that were never published to PyPI (or yank-rot): rewrite
    # them to the newest resolvable release so the whole requirements file can
    # install atomically.
    ("filetype", "1.2.1"): "filetype==1.2.0",
    ("pypdfium2", "5.12.1"): "pypdfium2==5.9.0",
}


def apply_bad_pin_fixes(lines) -> list:
    """Rewrite the requirements pins that can never install as written.

    Shared by the user-facing first-boot dependency sync and the CI runtime
    bake (scripts/bake_runtime.py) so both apply exactly the same rewrites.
    """
    patched = []
    for line in lines:
        s = line.strip()
        if "==" in s and not s.startswith(("-", "#")):
            parts = s.split("==", 1)
            fix = _KNOWN_BAD_PINS.get((parts[0].strip().lower(), parts[1].strip()))
            if fix:
                tail = line.split("==", 1)[1]
                comment = "  # " + tail.split("#", 1)[1].strip() if "#" in tail else ""
                s = fix + comment
        patched.append(s)
    return patched


def _sync_deps(key: str, log=print) -> None:
    # Only when the app launcher says it's a real packaged app: then the
    # interpreter belongs to the app, so pip-ing into it is safe and keeps
    # the managed copy runnable. Never in a dev checkout — that would touch
    # the user's own Python environment.
    if not os.environ.get("SCM_WORKBENCH_PACKAGED"):
        return
    req = REPOS[key]["requirements"]
    repo = repo_dir(key)
    if not req or not (repo / req).is_file():
        return
    # The job scripts run on the provisioned relocatable runtime, so its
    # site-packages — not this process's interpreter — is the target.
    py = os.environ.get("SCM_WORKBENCH_PYTHON")
    interpreter = [py, "-m", "pip"] if py else [sys.executable, "-m", "pip"]
    src = (repo / req).read_text(encoding="utf-8", errors="replace")
    patched = apply_bad_pin_fixes(src.splitlines())
    work = repo / ".wb-requirements.txt"
    _secure_write_bytes(work, ("\n".join(patched) + "\n").encode("utf-8"))
    run_kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        # pip is a console app on Windows: CREATE_NO_WINDOW keeps the one-time
        # dependency sync from flashing a terminal (its output is captured anyway).
        r = subprocess.run([*interpreter, "install", "--disable-pip-version-check", "-q",
                            "-r", str(work)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", **run_kw)
    finally:
        if work.exists():
            _secure_unlink_absolute(work)
    if r.returncode == 0:
        log("  dependencies ok (installed into the app's private runtime)")
    else:
        tail = " ".join((r.stderr or r.stdout or "").strip().split())[-160:]
        hint = (" — " + tail) if tail else " — check the repo's requirements.txt pins"
        log(f"  ! dependency sync: some pinned requirements could not be installed (continuing){hint}")


# User data lives in these repo subfolders (decklists, fetched card images,
# generated output, calibration data). A full re-deploy must never lose it:
# copies are staged before the tree is replaced and put back afterwards.
# Upstream placeholder files (README/EMPTY) are not user data.
USER_DATA_PATHS = ("data", "game/front", "game/back", "game/double_sided",
                   "game/decklist", "game/output")
_PRISTINE_NAMES = {"README.md", "EMPTY.md"}


def stash_user_data(repo: Path, dest: Path, log=print) -> list:
    """Copy user files out of a tree that is about to be replaced.
    Returns [(relpath, staged_path), …]."""
    repo = safe_destination(repo)
    dest = safe_destination(dest)
    if not _WINDOWS_FALLBACK:
        repo, dest = repo.resolve(), dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    safe_destination(dest)
    saved = []
    for base_rel in USER_DATA_PATHS:
        rel = validate_repo_path(base_rel)
        d = safe_path(repo, rel)
        if d.is_symlink():
            raise RepoError("refusing a symbolic link in user data")
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            rel = str(f.relative_to(repo)).replace(os.sep, "/")
            rel = validate_repo_path(rel)
            safe_path(repo, rel)
            if f.is_symlink():
                raise RepoError("refusing a symbolic link in user data")
            if not f.is_file() or f.name in _PRISTINE_NAMES:
                continue
            sp = safe_destination(dest / rel)
            _secure_copy_file(f, dest, rel)
            saved.append((rel, sp))
    if saved:
        log(f"[repos] staged {len(saved)} user file(s) from the old tree "
            f"(decklists/images/output/offsets) — they will be restored after the re-deploy")
    return saved


def restore_user_data(saved: list, repo: Path, log=print) -> None:
    """Put staged user files back into (a freshly replaced) tree. User data
    wins over any same-named upstream file."""
    repo = safe_destination(repo)
    if not _WINDOWS_FALLBACK:
        repo = repo.resolve()
    for rel, sp in saved:
        rel = validate_repo_path(rel)
        safe_path(repo, rel)
        _secure_copy_file(Path(sp), repo, rel)
    if saved:
        log(f"[repos] restored {len(saved)} user file(s) into the new tree")


def verify_deployed(key: str) -> bool:
    """Cheap offline spot check: does the deployed tree still match the
    recorded state (same sha in state/manifest, probed file hashes intact)?
    The launcher uses this to decide when a managed copy needs a safe
    re-deploy; the update flow itself repairs drift, so this mostly catches
    interrupted work."""
    st = load_state().get(key) or {}
    deployed = st.get("deployed") or {}
    if not deployed.get("sha"):
        return False
    man = load_manifest(key)
    files = man.get("files") or {}
    if not files:
        return False
    if man.get("sha") and man["sha"] != deployed["sha"]:
        return False
    repo = repo_dir(key)
    probe = sorted(files)[0]
    p = safe_path(repo, probe)
    if not p.is_file():
        return False
    try:
        return sha256_file(p) == files[probe]
    except Exception:
        return False


def _cmd_init_locked(key: str, tarball: str = None, log=print, force_redeploy: bool = False):
    key = validate_repo_key(key)
    meta = REPOS[key]
    with _state_lock():
        st = _load_state_for_mutation()
        source, source_sig = _source_snapshot(key, st)
        rstate = st.get(key) or {}
        if not isinstance(rstate, dict):
            raise RepoError("invalid repository state entry")
        _load_manifest_for_mutation(key)
    if rstate.get("deployed") and not tarball and not force_redeploy:
        log(f"[init {key}] already deployed at {rstate['deployed']['ref']} — nothing to do.")
        return {"ok": True, "noop": True}
    log(f"[init {key}] resolving target “{source}” …")
    target = resolve_target(key, source)
    log(f"[init {key}] target: {target['ref']} @ {target['sha'][:7]}")
    repo = safe_destination(repo_dir(key))
    stash_dir = safe_destination(data_dir() / f".repos-stash-{key}-{int(time.time())}")
    saved = []
    try:
        if repo.exists():
            log(f"[init {key}] replacing existing copy at {repo}")
            saved = stash_user_data(repo, stash_dir, log)
            shutil.rmtree(repo, ignore_errors=True)
        if tarball:
            tp = safe_destination(Path(tarball))
            log(f"[init {key}] extracting local tarball {tp.name} ({tp.stat().st_size / 1e6:.0f} MB) …")
            extract_tarball(tp, repo, log)
        else:
            tmp = data_dir() / f".repos-download-{key}.tar.gz"
            set_progress(key, stage="download", done=0, total=0, label="full snapshot")
            log(f"[init {key}] downloading full snapshot from GitHub …")
            data = gh_get_bytes(f"{API}/repos/{meta['owner']}/{meta['repo']}/tarball/{target['sha']}",
                                  timeout=1800, progress_cb=lambda d, t: set_progress(key, done=d, total=t),
                                  max_bytes=TARBALL_CAP)
            _secure_write_bytes(tmp, data)
            log(f"[init {key}] {len(data) / 1e6:.0f} MB received — extracting to {repo} …")
            set_progress(key, stage="extract", done=0, total=0)
            extract_tarball(tmp, repo, log)
            if tmp.exists():
                _secure_unlink_absolute(tmp)
            set_progress(key, stage="fingerprint", done=0, total=0, unit="files")
        restore_user_data(saved, repo, log)
        man = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date"), "files": {}}
        paths = tracked_paths(repo)
        for i, p in enumerate(paths, 1):
            man["files"][p] = sha256_file(repo / p)
            # one heartbeat per file: the stage now shows a real count instead
            # of sitting on “fingerprinting the files” for minutes (writes are
            # throttled to ~2% of the file count by set_progress)
            set_progress(key, stage="fingerprint", done=i, total=len(paths), unit="files")
        save_manifest(key, man)
        with _state_lock():
            # Reload inside the state lock. Preserve any source selected while
            # the network/deploy work was in progress instead of resurrecting
            # the captured source.
            st = _load_state_for_mutation()
            rstate = st.get(key) or {}
            if not isinstance(rstate, dict):
                raise RepoError("invalid repository state entry")
            current_source, current_sig = _source_snapshot(key, st)
            rstate["deployed"] = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date")}
            rstate["mode"] = "bundled"
            if current_sig == source_sig and current_source == source:
                rstate["source"] = source
            st[key] = rstate
            save_state(st)
        log(f"[init {key}] {REPOS[key]['name']} deployed at {target['ref']} ({target['sha'][:7]}) — "
            f"{len(man['files'])} tracked files fingerprinted")
        try:
            _sync_deps(key, log)
        except Exception as e:
            log(f"  ! dependency sync failed: {e}")
    finally:
        try:
            if stash_dir.exists():
                shutil.rmtree(stash_dir, ignore_errors=True)
        except Exception:
            pass
        clear_progress(key)
    return {"ok": True, "files": len(man["files"])}


def cmd_init(key: str, tarball: str = None, log=print, force_redeploy: bool = False):
    key = validate_repo_key(key)
    with _repo_lock(key):
        return _cmd_init_locked(key, tarball=tarball, log=log,
                                force_redeploy=force_redeploy)


def _cmd_update_locked(key: str, force_full: bool = False, log=print):
    key = validate_repo_key(key)
    meta = REPOS[key]
    with _state_lock():
        st = _load_state_for_mutation()
        source, _source_sig = _source_snapshot(key, st)
        rstate = st.get(key) or {}
        if not isinstance(rstate, dict):
            raise RepoError("invalid repository state entry")
        deployed = rstate.get("deployed")
        _load_manifest_for_mutation(key)
    if not deployed:
        raise RepoError("no managed copy of this repo yet — run “Download latest” (init) first.")
    log(f"[update {key}] resolving target “{source}” …")
    target = resolve_target(key, source)
    log(f"[update {key}] target: {target['ref']} @ {target['sha'][:7]} ({(target.get('date') or '?')[:10]})")
    if deployed["sha"] == target["sha"]:
        log(f"[update {key}] already at {target['ref']} ({target['sha'][:7]}) — nothing to do.")
        return {"ok": True, "noop": True}

    return _run_update(key, meta, st, rstate, deployed, source, target, force_full, log)


def cmd_update(key: str, force_full: bool = False, log=print):
    key = validate_repo_key(key)
    with _repo_lock(key):
        return _cmd_update_locked(key, force_full=force_full, log=log)


def _run_update(key, meta, st, rstate, deployed, source, target, force_full, log):
    key = validate_repo_key(key)
    t0 = time.time()
    try:
        man = _load_manifest_for_mutation(key)
        if man is None:
            raise RepoError("repository manifest is missing")
        old_files = man["files"]
        repo = safe_destination(repo_dir(key))
        mode = "full" if force_full else "diff"

        if mode == "diff":
            try:
                cmp = compare(key, deployed["sha"], target["sha"])
                if cmp["status"] != "ahead":
                    # the target is older than (behind) or unrelated to (diverged) the deployed
                    # commit — GitHub's merge-base diff can't express that, so swap full snapshots
                    log(f"[update {key}] moving to an older/diverged ref — using full-tarball sync.")
                    mode = "full"
                elif cmp["too_many"]:
                    log(f"[update {key}] {DIFF_FILE_CAP}+ files changed — switching to full-tarball sync.")
                    mode = "full"
                else:
                    log(f"[update {key}] {len(cmp['files'])} file(s) changed across {cmp['commits']} commit(s)")
            except RepoError as e:
                log(f"[update {key}] diff unavailable ({e}) — switching to full-tarball sync.")
                mode = "full"

        if mode == "diff":
            staging = safe_destination(data_dir() / f".repos-diff-{key}-{int(time.time())}")
            staging.mkdir(parents=True, exist_ok=True)
            safe_destination(staging)

            set_progress(key, stage="update", done=0, total=len(cmp["files"]))

            _count = [0]

            def pristine_for(path):
                """Fetch the new pristine content into staging; return its path (or None on failure)."""
                sp = safe_path(staging, path)
                try:
                    download_to(key, target["sha"], path, sp, log)
                    _count[0] += 1
                    set_progress(key, done=_count[0])
                    return sp
                except RepoError as e:
                    log(f"    ! could not fetch {_brief(path)}: {_brief(e)}")
                    return None

            apply_ops, delete_paths = {}, []
            for f in cmp["files"]:
                if f["status"] == "removed":
                    delete_paths.append(f["path"])
                else:
                    apply_ops[f["path"]] = f.get("previous")
            res = apply_changes(key, man, target, apply_ops, delete_paths, pristine_for, log)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            log(f"[update {key}] downloading full snapshot of {target['ref']} …")
            set_progress(key, stage="download", done=0, total=0, label="full snapshot")
            tmp = data_dir() / f".repos-download-{key}.tar.gz"
            data = gh_get_bytes(f"{API}/repos/{meta['owner']}/{meta['repo']}/tarball/{target['sha']}",
                                  timeout=1800, progress_cb=lambda d, t: set_progress(key, done=d, total=t),
                                  max_bytes=TARBALL_CAP)
            _secure_write_bytes(tmp, data)
            log(f"[update {key}] {len(data) / 1e6:.0f} MB received — extracting & reconciling …")
            staging = safe_destination(data_dir() / f".repos-staging-{key}-{int(time.time())}")
            staging.mkdir(parents=True, exist_ok=True)
            safe_destination(staging)
            set_progress(key, stage="extract", done=0, total=0)
            extract_tarball(tmp, staging, log)
            set_progress(key, stage="apply", done=0, total=0)
            new_tree = {p: staging / p for p in tracked_paths(staging)}
            apply_ops = {p: None for p in new_tree}
            delete_paths = [p for p in old_files if p not in new_tree]
            res = apply_changes(key, man, target, apply_ops, delete_paths,
                                 lambda p: new_tree.get(p), log)
            shutil.rmtree(staging, ignore_errors=True)
            if tmp.exists():
                _secure_unlink_absolute(tmp)

        # state write under the lock, with a fresh reload (the UI server's
        # check/update handlers and the launcher both write this file)
        new_manifest = res["manifest"]
        with _state_lock():
            st = _load_state_for_mutation()
            rstate = st.get(key) or {}
            if not isinstance(rstate, dict):
                raise RepoError("invalid repository state entry")
            rstate["deployed"] = {"sha": target["sha"], "ref": target.get("ref"), "date": target.get("date")}
            rstate["last_update"] = time.time()
            rstate["last_update_secs"] = round(time.time() - t0, 1)
            # Re-record the check against the new deployed commit while
            # retaining extension fields written by other state clients.
            checked = {"repo": key, "ok": True, "cached": False, "target": target,
                       "deployed": rstate["deployed"], "source": source,
                       "up_to_date": True}
            old_check = rstate.get("last_check") if isinstance(rstate.get("last_check"), dict) else {}
            old_checked = old_check.get("checked") if isinstance(old_check.get("checked"), dict) else {}
            rstate["last_check"] = {**old_check, "checked": {**old_checked, **checked},
                                     "checked_at": time.time()}
            st[key] = rstate
            save_state(st)
        save_manifest(key, new_manifest)

        for p in res["conflicts"][:10]:
            log(f"  ! kept your local version of {_brief(p)} (upstream also changed it — merge manually if needed)")
        log(f"[update {key}] done in {res['applied']} applied, {res['deleted']} removed, "
            f"{len(res['conflicts'])} conflict(s) kept — {meta['name']} now at "
            f"{target['ref']} ({target['sha'][:7]})")
        try:
            _sync_deps(key, log)
        except Exception as e:
            log(f"  ! dependency sync failed: {e}")
        return {"ok": True, "applied": res["applied"], "deleted": res["deleted"],
                 "conflicts": res["conflicts"]}
    finally:
        clear_progress(key)



def main():
    ap = argparse.ArgumentParser(description="Workbench repo sync")
    ap.add_argument("cmd", choices=["init", "update", "check", "refs"])
    ap.add_argument("--repo", required=True, choices=list(REPOS))
    ap.add_argument("--tarball", default=None,
                    help="local tarball to extract instead of downloading (init only)")
    ap.add_argument("--force-full", action="store_true",
                    help="update via full tarball instead of the file diff")
    ap.add_argument("--json", action="store_true", help="machine-readable output for check/refs")
    a = ap.parse_args()
    try:
        if a.cmd == "init":
            out = cmd_init(a.repo, tarball=a.tarball)
        elif a.cmd == "update":
            out = cmd_update(a.repo, force_full=a.force_full)
        elif a.cmd == "check":
            out = cmd_check(a.repo, as_json=a.json)
        else:
            out = cmd_refs(a.repo, as_json=a.json)
        if isinstance(out, dict) and out.get("ok") is False:
            sys.exit(1)
    except RepoError as e:
        if a.json:
            print(json.dumps({"repo": a.repo, "ok": False, "error": str(e)}))
        else:
            print(f"error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
