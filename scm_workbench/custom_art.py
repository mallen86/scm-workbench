"""Bounded, copy-only custom card art imports into fixed SCM image folders.

The module takes the server module as a dependency at call time, avoiding an
import cycle and sharing its image/job and repository mutation fences.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import unicodedata

MAX_FILES = 256
MAX_FILE = 32 * 1024 * 1024
MAX_BATCH = 512 * 1024 * 1024
MAX_RESULT = 256 * 1024
CHUNK = 64 * 1024
TTL = 600.0
UPLOAD_DEADLINE = 120.0
UPLOAD_READ_TIMEOUT = 5.0
DESTINATIONS = frozenset(("front", "double_sided", "back"))
EXTENSIONS = frozenset((".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".dib", ".avif", ".heif", ".heic", ".qoi", ".dds", ".jp2", ".j2k"))
PREFIX = ".wb-custom-art-"
_lock = threading.Lock()
_operations: dict[str, dict] = {}


class ImportError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = " ".join(str(message).split())[:256] or "custom art import failed"


def rejection(message):
    return {"ok": False, "errors": [ImportError(message).message]}


def destination(value):
    if not isinstance(value, str) or value not in DESTINATIONS:
        raise ImportError("destination must be front, double_sided, or back")
    return value


def name(value):
    if not isinstance(value, str) or not value:
        raise ImportError("image name is required")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ImportError("image name must be valid UTF-8") from exc
    if (size > 255 or value in (".", "..") or not value.strip() or
            any(unicodedata.category(c) == "Cc" for c in value) or
            any(c in value for c in '/\\:*?"<>|') or value.endswith((".", " ")) or
            value.startswith(PREFIX)):
        raise ImportError("unsafe image name")
    stem = value.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem):
        raise ImportError("reserved image name")
    if Path(value).suffix.lower() not in EXTENSIONS:
        raise ImportError("unrecognized image extension")
    return value


def source_path(value):
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise ImportError("source must be an absolute path")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ImportError("source path must be valid UTF-8") from exc
    if size > 4096 or any(unicodedata.category(c) == "Cc" for c in value):
        raise ImportError("source path is unsafe or too long")
    return value


def _directory(scm, dest, *, create, server):
    root = Path(os.path.abspath(os.fspath(scm)))
    target = root / "game" / dest
    if os.name == "nt":
        # Windows has no dir_fd traversal; re-check each component before
        # operations, as with the existing decklist and back-image imports.
        for part in (root, root / "game", target):
            if create:
                try:
                    part.mkdir()
                except FileExistsError:
                    pass
            observed = os.lstat(part)
            if server._is_reparse_or_symlink(observed) or not stat.S_ISDIR(observed.st_mode):
                raise ImportError("image destination contains a link or non-directory")
        return target, None
    if not all(hasattr(os, key) for key in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise ImportError("secure image directory handles are unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fds = []
    try:
        current = os.open(root, flags)
        fds.append(current)
        for part in ("game", dest):
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
            current = os.open(part, flags, dir_fd=current)
            fds.append(current)
        return target, fds.pop()
    except OSError as exc:
        raise ImportError("image destination contains a link or non-directory") from exc
    finally:
        for fd in fds:
            os.close(fd)


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, getattr(st, "st_mtime_ns", st.st_mtime),
            getattr(st, "st_ctime_ns", st.st_ctime))


def _content_identity(st):
    return (st.st_dev, st.st_ino, st.st_size, getattr(st, "st_mtime_ns", st.st_mtime))


def _collision_name(original, suffix):
    if suffix == 0:
        return original
    stem, ext = os.path.splitext(original)
    marker = f" ({suffix + 1})"
    available = 255 - len((marker + ext).encode("utf-8"))
    if available < 1:
        raise ImportError("image name is too long for collision suffix")
    trimmed = stem.encode("utf-8")[:available].decode("utf-8", "ignore")
    return name(trimmed + marker + ext)


def _entry_stat(directory, dirfd, entry, server):
    """Inspect a directory entry without following it, using a handle on Windows.

    Windows path timestamps may be cached; use the reopened object's identity
    rather than a path stat's timestamps there.
    """
    if dirfd is not None:
        return os.stat(entry, dir_fd=dirfd, follow_symlinks=False)
    _directory(directory.parent.parent, directory.name, create=False, server=server)
    try:
        fd, observed = server._open_windows_regular_file(directory / entry)
    except server.ArtifactExportError as exc:
        raise ImportError("image destination changed during import") from exc
    try:
        if not os.path.samestat(observed, os.lstat(directory / entry)):
            raise ImportError("image destination changed during import")
        return observed
    finally:
        os.close(fd)


def _same_object(left, right):
    return stat.S_ISREG(left.st_mode) and stat.S_ISREG(right.st_mode) and os.path.samestat(left, right)


def _verify_destination(directory, dirfd, server):
    """Reject a renamed/replaced destination before publishing into its handle."""
    _, current_fd = _directory(directory.parent.parent, directory.name, create=False, server=server)
    try:
        if dirfd is not None and not os.path.samestat(os.fstat(dirfd), os.fstat(current_fd)):
            raise ImportError("image destination changed during import")
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _verify_temporary_bytes(handle, size, expected_digest, deadline):
    """Distinguish metadata-only ctime drift from edits with restored mtime."""
    os.lseek(handle, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        if time.monotonic() >= deadline:
            raise ImportError("custom art import timed out")
        chunk = os.read(handle, min(CHUNK, remaining))
        if not chunk:
            raise ImportError("temporary image changed during import")
        digest.update(chunk)
        remaining -= len(chunk)
    if time.monotonic() >= deadline:
        raise ImportError("custom art import timed out")
    if os.read(handle, 1) or digest.digest() != expected_digest:
        raise ImportError("temporary image changed during import")


def _copy(handle, observed, source_name, directory, dirfd, deadline, server):
    temp = None
    tempfd = None
    published_name = None
    temp_identity = None
    success = False
    parent_handles = []
    try:
        if dirfd is None:
            # Windows has no dir_fd operations. Pin the validated directory
            # against rename while checking the temp and final file handles.
            try:
                parent_handles = server._open_windows_artifact_parent(directory)
            except server.ArtifactExportError as exc:
                raise ImportError("image destination changed during import") from exc
        for _ in range(16):
            temp = PREFIX + secrets.token_hex(16) + ".tmp"
            try:
                tempfd = os.open(temp if dirfd is not None else directory / temp,
                                 os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) |
                                 getattr(os, "O_BINARY", 0), 0o600, **({"dir_fd": dirfd} if dirfd is not None else {}))
                temp_identity = os.fstat(tempfd)
                break
            except FileExistsError:
                continue
        if tempfd is None:
            raise ImportError("could not create temporary image")
        digest = hashlib.sha256()
        remaining = observed.st_size
        while remaining:
            if time.monotonic() >= deadline:
                raise ImportError("custom art import timed out")
            chunk = os.read(handle, min(CHUNK, remaining))
            if not chunk:
                raise ImportError("selected image changed while reading")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(tempfd, view)
                if written <= 0:
                    raise ImportError("could not write image")
                view = view[written:]
            remaining -= len(chunk)
        if os.read(handle, 1) or _identity(os.fstat(handle)) != _identity(observed):
            raise ImportError("selected image changed while reading")
        os.fsync(tempfd)
        temp_identity = os.fstat(tempfd)
        if not stat.S_ISREG(temp_identity.st_mode) or temp_identity.st_size != observed.st_size:
            raise ImportError("temporary image changed during import")
        for suffix in range(1001):
            if time.monotonic() >= deadline:
                raise ImportError("custom art import timed out")
            candidate = _collision_name(source_name, suffix)
            # A discovered temporary name is untrusted even though our own
            # descriptor remains open: it may have been swapped after creation.
            _verify_destination(directory, dirfd, server)
            current = os.fstat(tempfd)
            temporary = _entry_stat(directory, dirfd, temp, server)
            if (not _same_object(current, temp_identity) or
                    not _same_object(temporary, temp_identity) or
                    _content_identity(current) != _content_identity(temp_identity) or
                    _content_identity(temporary) != _content_identity(temp_identity)):
                fields = sorted({field for st in (current, temporary)
                                 for field, actual, expected in zip(
                                     ("device", "inode", "size", "mtime"),
                                     _content_identity(st), _content_identity(temp_identity))
                                 if actual != expected})
                server._diag("custom art temporary identity mismatch: " +
                             ", ".join(fields or ["file type"]), error=True)
                raise ImportError("temporary image changed during import")
            if (_identity(current) != _identity(temp_identity) or
                    _identity(temporary) != _identity(temp_identity)):
                # Indexers/security software may update metadata without touching
                # image bytes. Do not confuse that with corruption, but also do
                # not blindly ignore ctime: same-size edits can restore mtime.
                try:
                    _verify_temporary_bytes(tempfd, observed.st_size, digest.digest(), deadline)
                except ImportError as exc:
                    server._diag("custom art temporary byte verification failed: " + exc.message, error=True)
                    raise
                verified = os.fstat(tempfd)
                if (not _same_object(verified, temp_identity) or
                        _content_identity(verified) != _content_identity(temp_identity)):
                    raise ImportError("temporary image changed during import")
                temp_identity = verified
                server._diag("custom art temporary metadata changed; copied bytes verified")
            try:
                if dirfd is None:
                    os.link(directory / temp, directory / candidate)
                else:
                    os.link(temp, candidate, src_dir_fd=dirfd, dst_dir_fd=dirfd,
                            follow_symlinks=False)
                published_name = candidate
                # A swap between the check and link must not publish attacker
                # bytes (including a hard link to a different regular file).
                # Check the names both before and after a content readback.
                # Linking legitimately changes ctime, so metadata alone cannot
                # detect same-size edits that restore mtime during publication.
                for verification_pass in range(2):
                    current = os.fstat(tempfd)
                    temporary = _entry_stat(directory, dirfd, temp, server)
                    final = _entry_stat(directory, dirfd, candidate, server)
                    if any(not _same_object(st, temp_identity) or
                           _content_identity(st) != _content_identity(temp_identity)
                           for st in (current, temporary, final)):
                        raise ImportError("temporary image changed during publication")
                    if verification_pass == 0:
                        _verify_temporary_bytes(tempfd, observed.st_size, digest.digest(), deadline)
                _verify_destination(directory, dirfd, server)
                success = True
                return candidate
            except FileExistsError:
                continue
        raise ImportError("too many image name collisions")
    except OSError as exc:
        raise ImportError("could not copy image") from exc
    finally:
        # Windows CRT descriptors do not share deletion. Close ours only after
        # publication checks; cleanup still verifies the recorded identity.
        if tempfd is not None and dirfd is None:
            os.close(tempfd)
            tempfd = None
        # Never remove a replaced entry. In particular a failed post-link
        # check must not delete somebody else's candidate or temporary file.
        for entry in (published_name if not success else None, temp):
            if entry is None or temp_identity is None:
                continue
            try:
                if _same_object(_entry_stat(directory, dirfd, entry, server), temp_identity):
                    os.unlink(entry if dirfd is not None else directory / entry,
                              **({"dir_fd": dirfd} if dirfd is not None else {}))
            except (OSError, ImportError, server.ArtifactExportError):
                server._diag("custom art temporary cleanup failed", error=True)
        if tempfd is not None:
            os.close(tempfd)
        if parent_handles:
            server._close_windows_handles(parent_handles)


def _open_source(path, server):
    try:
        if os.name == "nt":
            return server._open_windows_regular_file(Path(path))
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) |
                     getattr(os, "O_NONBLOCK", 0))
        try:
            return fd, os.fstat(fd)
        except Exception:
            os.close(fd)
            raise
    except (OSError, server.ArtifactExportError) as exc:
        raise ImportError("could not open selected image") from exc


def _safe_failed_name(value):
    """Keep partial failures useful without passing unsafe names to Rust."""
    if not isinstance(value, str):
        return "invalid image name"
    cleaned = "".join("_" if unicodedata.category(char) == "Cc" or
                      char in "/\\:" else char for char in value)
    try:
        cleaned = cleaned.encode("utf-8")[:255].decode("utf-8", "ignore")
    except UnicodeError:
        return "invalid image name"
    return cleaned if cleaned not in ("", ".", "..") else "invalid image name"


def _size(result):
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _run_back(entries, server, settings, deadline, progress=None):
    """Use the existing single-card-back quarantine/rollback transaction."""
    display_name, open_file = entries[0]
    handle = None
    try:
        source_name = name(display_name)
        server._back_image_name(source_name)
        if time.monotonic() >= deadline:
            raise ImportError("custom art import timed out")
        handle, observed = open_file()
        if (server._is_reparse_or_symlink(observed) or not stat.S_ISREG(observed.st_mode)
                or observed.st_size > server.BACK_IMAGE_SOURCE_MAX_BYTES):
            raise ImportError("card back must be a regular image no larger than 32 MiB")
        if not server._image_header_is_image(os.read(handle, 16)):
            raise ImportError("selected file is not a recognized image")
        os.lseek(handle, 0, os.SEEK_SET)
        owned, handle = handle, None  # importer closes it even on failure
        back = server.import_back_image_opened(owned, observed, source_name, settings)
        result = {"ok": True, "destination": "back", "imported": 1,
                  "names": [back["name"]], "failed": []}
        return result if _size(result) <= MAX_RESULT else rejection("custom art result exceeds 256 KiB")
    except (ImportError, server.BackImageImportError) as exc:
        return rejection(str(exc))
    except (OSError, ValueError):
        return rejection("could not import card back")
    finally:
        if handle is not None:
            os.close(handle)
        if progress is not None:
            progress(1)


def _run(dest, entries, server, settings, deadline, progress=None):
    if dest == "back":
        return _run_back(entries, server, settings, deadline, progress)

    names, failed = [], []
    total_bytes = 0
    scm, _ = server.effective_dirs(settings)
    if not scm:
        return rejection("no copy of silhouette-card-maker is connected yet")
    try:
        with server._image_delete_lock():
            directory, dirfd = _directory(scm, dest, create=True, server=server)
            try:
                for display_name, open_file in entries:
                    failed_name = _safe_failed_name(display_name)
                    if time.monotonic() >= deadline:
                        failed.append({"name": failed_name, "error": "custom art import timed out"})
                        if progress is not None:
                            progress(len(names) + len(failed))
                        continue
                    handle = None
                    try:
                        source_name = name(display_name)
                        handle, observed = open_file()
                        if (server._is_reparse_or_symlink(observed) or not stat.S_ISREG(observed.st_mode)
                                or observed.st_size > MAX_FILE):
                            raise ImportError("selected image is not a bounded regular file")
                        total_bytes += observed.st_size
                        if total_bytes > MAX_BATCH:
                            raise ImportError("batch exceeds 512 MiB")
                        if not server._image_header_is_image(os.read(handle, 16)):
                            raise ImportError("selected file is not a recognized image")
                        os.lseek(handle, 0, os.SEEK_SET)
                        published = _copy(handle, observed, source_name, directory, dirfd, deadline, server)
                        names.append(published)
                    except ImportError as exc:
                        failed.append({"name": failed_name, "error": exc.message})
                    except (OSError, ValueError):
                        failed.append({"name": failed_name, "error": "could not import image"})
                    finally:
                        if handle is not None:
                            os.close(handle)
                        if progress is not None:
                            progress(len(names) + len(failed))
            finally:
                if dirfd is not None:
                    os.close(dirfd)
    except server.ImageDeleteError as exc:
        return rejection(exc.message)
    except (ImportError, OSError) as exc:
        return rejection(exc.message if isinstance(exc, ImportError) else "image destination is unavailable")
    result = {"ok": True, "destination": dest, "imported": len(names), "names": names, "failed": failed}
    if _size(result) > MAX_RESULT:
        # 256 names of <=255 UTF-8 bytes plus 256 bounded errors fit comfortably.
        return rejection("custom art result exceeds 256 KiB")
    if names:
        server.invalidate_manifest_cache()
    return result


def import_selected(dest, paths, server, settings=None, deadline=None, progress=None):
    destination(dest)
    if not isinstance(paths, list) or not 1 <= len(paths) <= (1 if dest == "back" else MAX_FILES):
        raise ImportError("back requires exactly one image" if dest == "back" else "source_paths must contain 1 to 256 files")
    paths = [source_path(p) for p in paths]
    entries = [(os.path.basename(p), lambda p=p: _open_source(p, server)) for p in paths]
    return _run(dest, entries, server, settings if settings is not None else server.load_settings(),
                deadline if deadline is not None else time.monotonic() + TTL, progress)


def import_bytes(dest, image_name, stream, length, server, settings=None, *, socket=None):
    destination(dest)
    name(image_name)
    limit = server.BACK_IMAGE_SOURCE_MAX_BYTES if dest == "back" else MAX_FILE
    if not isinstance(length, int) or not 0 < length <= limit:
        raise ImportError("card back must be between 1 byte and 32 MiB" if dest == "back" else "image size must be between 1 byte and 32 MiB")
    # Raw HTTP bytes are staged in a private file, never held as a 32 MiB JSON
    # value. Read exactly Content-Length and reject a truncated transfer.
    import tempfile
    deadline = time.monotonic() + UPLOAD_DEADLINE
    previous_timeout = socket.gettimeout() if socket is not None else None
    try:
        with tempfile.TemporaryFile() as file:
            remaining = length
            read = getattr(stream, "read1", None) or stream.read
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ImportError("image upload timed out")
                if socket is not None:
                    socket.settimeout(min(UPLOAD_READ_TIMEOUT, left))
                try:
                    chunk = read(min(CHUNK, remaining))
                except (OSError, TimeoutError) as exc:
                    raise ImportError("image upload interrupted or timed out") from exc
                if time.monotonic() >= deadline:
                    raise ImportError("image upload timed out")
                if not chunk:
                    raise ImportError("incomplete image upload")
                file.write(chunk)
                remaining -= len(chunk)
            file.flush()
            def open_file():
                fd = os.dup(file.fileno())
                os.lseek(fd, 0, os.SEEK_SET)
                return fd, os.fstat(fd)
            return _run(dest, [(image_name, open_file)], server,
                        settings if settings is not None else server.load_settings(), deadline)
    finally:
        if socket is not None:
            socket.settimeout(previous_timeout)


def open_folder(dest, server, settings=None):
    try:
        destination(dest)
        scm, _ = server.effective_dirs(settings if settings is not None else server.load_settings())
        if not scm:
            raise ImportError("no copy of silhouette-card-maker is connected yet")
        with server._image_delete_lock():
            directory, fd = _directory(scm, dest, create=True, server=server)
            try:
                message = server.reveal_path(directory)
            finally:
                if fd is not None:
                    os.close(fd)
            if message:
                raise ImportError("could not open image folder")
        return {"ok": True, "errors": []}
    except server.ImageDeleteError as exc:
        return rejection(exc.message)
    except (ImportError, OSError):
        return rejection("could not open image folder")


def _prune():
    now = time.monotonic()
    for key, value in list(_operations.items()):
        if value.get("ended") is not None and now - value["ended"] > TTL:
            del _operations[key]


def start(dest, paths, server):
    operation_id = None
    try:
        destination(dest)
        if not isinstance(paths, list) or not 1 <= len(paths) <= (1 if dest == "back" else MAX_FILES):
            raise ImportError("back requires exactly one image" if dest == "back" else "source_paths must contain 1 to 256 files")
        for path in paths:
            source_path(path)
        with _lock:
            _prune()
            active = sum(op["ended"] is None for op in _operations.values())
            if active >= 2 or len(_operations) >= 34:
                return rejection("custom art import is busy")
            operation_id = secrets.token_hex(16)
            record = {"ended": None, "completed": 0, "total": len(paths), "result": None}
            _operations[operation_id] = record
        settings = server.load_settings()
        deadline = time.monotonic() + TTL
        def work():
            try:
                # One batch owns one exclusion lease. Keep progress bounded even
                # though the byte-copying work is never on the IPC reader.
                def progress(count):
                    with _lock:
                        record["completed"] = count
                result = import_selected(dest, paths, server, settings, deadline, progress)
            except Exception:
                result = rejection("custom art import failed")
            with _lock:
                record["result"] = result
                record["completed"] = len(result.get("names", [])) + len(result.get("failed", []))
                record["ended"] = time.monotonic()
        threading.Thread(target=work, daemon=True, name="custom-art-import").start()
        return {"ok": True, "operation_id": operation_id}
    except ImportError as exc:
        return rejection(exc.message)
    except Exception:
        if operation_id is not None:
            with _lock:
                _operations.pop(operation_id, None)
        return rejection("could not start custom art import")


def poll(operation_id):
    with _lock:
        _prune()
        record = _operations.get(operation_id)
        if record is None:
            raise ImportError("operation not found")
        if record["ended"] is None:
            return {"ok": True, "status": "running", "completed": record["completed"], "total": record["total"]}
        return {"ok": True, "status": "done", "result": record["result"]}
