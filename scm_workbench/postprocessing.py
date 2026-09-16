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
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

CONTRACT_VERSION = "1"
SOURCE_MAX_BYTES = 256 * 1024
NAME_MAX_BYTES = 96
REQUIREMENT_LINE_MAX_BYTES = 256
REQUIREMENTS_MAX_BYTES = 8 * 1024
REQUIREMENTS_MAX_COUNT = 32
PROCESSOR_MAX_COUNT = 64
REVISIONS_MAX_COUNT = 20
SAVED_SOURCE_MAX_BYTES = 8 * 1024 * 1024
SCAN_MAX_ENTRIES = 8192
IMAGE_MAX_COUNT = 1024
IMAGE_MAX_BYTES = 64 * 1024 * 1024
IMAGE_TOTAL_MAX_BYTES = 8 * 1024 * 1024 * 1024
PATH_MAX_BYTES = 4096
NAME_MAX_FILE_BYTES = 255
RUN_MAX_BYTES = IMAGE_TOTAL_MAX_BYTES * 2
OUTPUT_MAX_BYTES = IMAGE_MAX_BYTES
METADATA_MAX_BYTES = 512 * 1024
JOURNAL_MAX_BYTES = 512 * 1024

_FORMATS = ("png", "jpeg", "gif", "webp", "bmp")
_EXT_FORMAT = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp", ".bmp": "bmp"}


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


class TransactionError(PostProcessingError):
    pass


def _utf8(value: Any, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"invalid {label}")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"invalid {label} encoding") from exc
    if len(raw) > limit or (not raw and not empty):
        raise ValidationError(f"invalid {label} length")
    if "\x00" in value or any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) == "Cc" for c in value):
        raise ValidationError(f"invalid {label} control character")
    return value


def _safe_component(value: str, label: str, limit: int = 128) -> str:
    value = _utf8(value, label, limit)
    if value != value.strip() or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValidationError(f"invalid {label}")
    return value


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
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0):
            # macOS exposes the conventional temporary directory through a
            # /private alias; this is an OS path alias, not an application
            # controlled link and is safe to normalize.
            resolved = current.resolve(strict=False)
            if current.parent == Path(current.anchor) and resolved.parts[:2] == (current.anchor, "private"):
                current = resolved
                continue
            raise ValidationError("symbolic links and reparse points are not allowed")


def _contained(root: Path, path: Path) -> Path:
    root = Path(root).resolve()
    candidate = Path(path)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValidationError("path escapes its root") from exc
    _no_links(candidate, allow_missing_leaf=True)
    return candidate


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    _no_links(path.parent, allow_missing_leaf=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path) and path.is_symlink():
        raise IntegrityError("refusing to replace a symbolic link")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        try:
            dfd = os.open(path.parent, getattr(os, "O_DIRECTORY", os.O_RDONLY))
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError:
            pass
    finally:
        try: tmp.unlink()
        except OSError: pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > METADATA_MAX_BYTES:
        raise ValidationError("metadata is too large")
    _atomic_bytes(path, raw)


def _read_json(path: Path, label: str, *, missing: Any = None) -> Any:
    try: raw = Path(path).read_bytes()
    except FileNotFoundError: return missing
    except OSError as exc: raise IntegrityError(f"could not read {label}") from exc
    if len(raw) > METADATA_MAX_BYTES: raise IntegrityError(f"{label} is too large")
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
            raise ValidationError("requirement must be a pinned package request")
        match = _REQ_RE.fullmatch(line)
        if not match: raise ValidationError("invalid requirement")
        name, extras, version = match.groups()
        canonical_name = name.lower().replace("_", "-").replace(".", "-")
        extra_part = "" if not extras else "[" + ",".join(sorted(x.lower() for x in extras.split(","))) + "]"
        value = canonical_name + extra_part + ("==" + version if version else "")
        key = canonical_name + extra_part
        if key in result and result[key] != value: raise ValidationError("conflicting duplicate requirement")
        result[key] = value
    return tuple(sorted(result.values()))


def validate_source(source: str) -> bytes:
    """Validate a revision without importing or executing it."""
    try: raw = source.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc: raise ValidationError("source must be UTF-8 text") from exc
    if len(raw) > SOURCE_MAX_BYTES: raise ValidationError("source is too large")
    if b"\x00" in raw: raise ValidationError("source contains NUL")
    try: tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, UnicodeError) as exc: raise ValidationError("source has invalid Python syntax") from exc
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "process_image"]
    if len(funcs) != 1 or isinstance(funcs[0], ast.AsyncFunctionDef): raise ValidationError("source must define exactly one synchronous process_image")
    fn = funcs[0]
    args = fn.args
    if args.vararg or args.kwarg or args.kwonlyargs or len(args.posonlyargs) + len(args.args) != 2 or args.defaults:
        raise ValidationError("process_image must accept exactly (image_path, context)")
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


def _image_dimensions(path: Path, fmt: str) -> tuple[int, int]:
    with path.open("rb") as stream:
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


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk: break
            digest.update(chunk)
    return digest.hexdigest()


def _stable_image(path: Path, root: Path, role: str) -> ImageRecord | None:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0): return None
        if st.st_size > IMAGE_MAX_BYTES: raise ValidationError("image is too large")
        with path.open("rb") as stream: head = stream.read(64)
        fmt = _format_from_header(head)
        if not fmt: return None
        width, height = _image_dimensions(path, fmt)
        if width <= 0 or height <= 0 or width * height > 200_000_000:
            raise IntegrityError("image dimensions are outside the supported bounds")
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
        except ImportError:
            pass
        except Exception as exc:
            raise IntegrityError("image is not decodable") from exc
        before = os.stat(path, follow_symlinks=False)
        digest = _digest_file(path)
        after = os.stat(path, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns): raise IntegrityError("image changed while reading")
        relative = path.relative_to(root).as_posix()
        if len(relative.encode()) > PATH_MAX_BYTES or len(path.name.encode()) > NAME_MAX_FILE_BYTES: raise ValidationError("image path is too long")
        return ImageRecord(role, relative, path.name, str(path), st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, fmt, digest)
    except FileNotFoundError: return None


def discover_images(scm_root: str | Path, scope: str = "both") -> tuple[ImageRecord, ...]:
    root = Path(scm_root).resolve()
    if scope not in {"both", "front", "double_sided"}: raise ValidationError("invalid image scope")
    roles = ("front", "double_sided") if scope == "both" else (scope,)
    output: list[ImageRecord] = []
    total = 0
    for role in roles:
        directory = root / "game" / role
        _no_links(directory)
        try: entries = list(os.scandir(directory))
        except FileNotFoundError: continue
        if len(entries) > SCAN_MAX_ENTRIES: raise ValidationError("image directory has too many entries")
        for entry in entries:
            if entry.is_symlink(): continue
            record = _stable_image(Path(entry.path), root, role)
            if record:
                output.append(record); total += record.size
                if len(output) > IMAGE_MAX_COUNT or total > IMAGE_TOTAL_MAX_BYTES: raise ValidationError("image batch exceeds its limit")
    output.sort(key=lambda item: (0 if item.role == "front" else 1, item.name.casefold(), item.name, item.relative_path))
    return tuple(output)


def stage_images(records: Sequence[ImageRecord], run_dir: str | Path) -> tuple[dict, ...]:
    run = Path(run_dir); run.mkdir(parents=True, exist_ok=False); _no_links(run)
    staged: list[dict] = []
    for index, record in enumerate(records, 1):
        src = Path(record.source); destination = run / "work" / record.role / record.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _no_links(destination.parent)
        _contained(run, destination)
        _no_links(src)
        try:
            before = os.stat(src, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (record.device, record.inode, record.size, record.mtime_ns): raise IntegrityError("source changed before staging")
            with src.open("rb") as inp, destination.open("xb") as out:
                remaining = IMAGE_MAX_BYTES + 1
                while remaining:
                    chunk = inp.read(min(1024 * 1024, remaining));
                    if not chunk: break
                    out.write(chunk); remaining -= len(chunk)
                out.flush(); os.fsync(out.fileno())
            after = os.stat(src, follow_symlinks=False)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (record.device, record.inode, record.size, record.mtime_ns): raise IntegrityError("source changed while staging")
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
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode): raise IntegrityError("processor result is not a regular file")
            if st.st_size > OUTPUT_MAX_BYTES: raise IntegrityError("processor result is too large")
            with path.open("rb") as stream: fmt = _format_from_header(stream.read(64))
            if fmt != entry.get("format"): raise IntegrityError("processor changed image format")
            width, height = _image_dimensions(path, fmt)
            if width <= 0 or height <= 0 or width * height > 200_000_000: raise IntegrityError("processor result dimensions are too large")
            # Pillow, when present in the worker, performs the authoritative
            # bounded decode.  Header checks remain useful in stdlib-only tests.
            try:
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
            except ImportError: pass
            except Exception as exc: raise IntegrityError("processor result is not a valid image") from exc
            total += st.st_size
        except FileNotFoundError as exc: raise IntegrityError("processor result is missing") from exc
    work = root / "work"
    if work.exists():
        for directory, dirs, files in os.walk(work, followlinks=False):
            for name in files:
                candidate = Path(directory) / name
                if candidate not in expected: raise IntegrityError("processor created an unexpected result")
            if any(Path(directory, name).is_symlink() for name in dirs): raise IntegrityError("processor created a link")
    if total > IMAGE_TOTAL_MAX_BYTES: raise IntegrityError("processor results exceed the batch limit")


class PublicationTransaction:
    """Durable same-filesystem replace transaction with idempotent recovery."""
    def __init__(self, journal_root: str | Path, transaction_id: str | None = None, *, fault: Callable[[str], None] | None = None):
        self.root = Path(journal_root); self.root.mkdir(parents=True, exist_ok=True); _no_links(self.root)
        self.id = transaction_id or uuid.uuid4().hex
        _safe_component(self.id, "transaction id", 64)
        self.journal = self.root / f"{self.id}.json"
        self.fault = fault

    def _write(self, data: dict) -> None: _atomic_json(self.journal, data)
    def _phase(self, phase: str) -> None:
        if self.fault: self.fault(phase)

    def publish(self, replacements: Sequence[tuple[str | Path, str | Path]], *, expected: Mapping[str, Sequence[int]] | None = None) -> None:
        items = []
        for destination, staged in replacements:
            dest = Path(destination); stage = Path(staged)
            _no_links(dest.parent); _no_links(stage)
            if expected and str(dest) in expected:
                st = os.stat(dest, follow_symlinks=False)
                if tuple([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]) != tuple(expected[str(dest)]): raise IntegrityError("destination changed before publication")
            items.append({"destination": str(dest), "staged": str(stage), "quarantine": str(dest.parent / f".wb-old-{self.id}-{len(items)}") , "moved": False, "published": False})
        data = {"version": 1, "id": self.id, "phase": "prepared", "items": items}
        self._write(data); self._phase("journal-prepared")
        try:
            for item in items:
                dest, stage, quarantine = map(Path, (item["destination"], item["staged"], item["quarantine"]))
                _no_links(dest.parent); _no_links(stage)
                if not dest.is_file() or dest.is_symlink():
                    raise IntegrityError("destination is not a stable regular file")
                os.replace(dest, quarantine); item["moved"] = True
                data["phase"] = "quarantined"; self._write(data); self._phase("quarantine")
                # The run directory may live below Workbench DATA_DIR while
                # the checkout is on another filesystem.  Copy to a sibling
                # temp first, then atomically replace the destination.
                fd, temp_name = tempfile.mkstemp(prefix=f".wb-new-{self.id}-", dir=str(dest.parent))
                sibling = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as out, stage.open("rb") as inp:
                        shutil.copyfileobj(inp, out, length=1024 * 1024)
                        out.flush(); os.fsync(out.fileno())
                    _no_links(sibling)
                    os.replace(sibling, dest)
                finally:
                    try: sibling.unlink()
                    except OSError: pass
                item["published"] = True
                data["phase"] = "publishing"; self._write(data); self._phase("publish")
            data["phase"] = "committed"; self._write(data); self._phase("committed")
            self._cleanup(data)
        except Exception as exc:
            if data.get("phase") == "committed":
                # The commit marker is durable.  Cleanup is idempotent and
                # must not roll back a transaction that is already complete.
                self._cleanup(data)
                raise TransactionError("publication committed but cleanup failed") from exc
            try: self.rollback(data)
            except Exception as rollback_exc: raise TransactionError("publication failed and rollback failed") from rollback_exc
            raise TransactionError("publication failed; originals restored") from exc

    def _cleanup(self, data: dict) -> None:
        for item in data["items"]:
            try: Path(item["quarantine"]).unlink()
            except FileNotFoundError: pass
            try: Path(item["staged"]).unlink()
            except FileNotFoundError: pass
        try: self.journal.unlink()
        except FileNotFoundError: pass

    def rollback(self, data: dict | None = None) -> None:
        data = data or _read_json(self.journal, "transaction journal")
        if not isinstance(data, dict): raise TransactionError("invalid transaction journal")
        for item in reversed(data.get("items", [])):
            dest, stage, quarantine = map(Path, (item["destination"], item["staged"], item["quarantine"]))
            if item.get("published") and dest.exists(): dest.unlink()
            elif item.get("moved") and quarantine.exists() and dest.exists():
                # A crash can occur after os.replace(stage, dest) but before
                # the journal records ``published``.  The quarantine proves
                # that this destination is ours, so remove the unjournaled
                # replacement before restoring the original.
                dest.unlink()
            if item.get("moved") and quarantine.exists(): os.replace(quarantine, dest)
            if stage.exists(): stage.unlink()
        try: self.journal.unlink()
        except FileNotFoundError: pass


def recover_transactions(journal_root: str | Path) -> tuple[str, ...]:
    root = Path(journal_root); recovered = []
    if not root.exists(): return ()
    for journal in sorted(root.glob("*.json")):
        data = _read_json(journal, "transaction journal")
        if not isinstance(data, dict) or data.get("version") != 1: raise IntegrityError("invalid transaction journal")
        tx = PublicationTransaction(root, str(data.get("id") or journal.stem))
        if data.get("phase") == "committed": tx._cleanup(data)
        else: tx.rollback(data)
        recovered.append(tx.id)
    return tuple(recovered)


def interpreter_fingerprint(interpreter: str | Path = sys.executable) -> str:
    path = Path(interpreter)
    try: st = path.stat(); identity = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
    except OSError: identity = []
    value = {"implementation": platform.python_implementation(), "version": list(sys.version_info[:3]), "abi": getattr(sys, "abiflags", ""), "platform": platform.platform(), "machine": platform.machine(), "interpreter": str(path.resolve()), "identity": identity}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def environment_fingerprint(requirements: Sequence[str], lock_hash: str = "", *, interpreter: str | Path = sys.executable, contract: str = CONTRACT_VERSION) -> str:
    req = normalize_requirements(requirements)
    return hashlib.sha256(json.dumps({"requirements": req, "lock": lock_hash, "interpreter": interpreter_fingerprint(interpreter), "contract": contract}, sort_keys=True).encode()).hexdigest()


def wheel_only_pip_argv(interpreter: str | Path, requirements: Sequence[str], *, target: str | Path, report: str | Path | None = None, offline: bool = False) -> list[str]:
    req = normalize_requirements(requirements)
    argv = [str(interpreter), "-m", "pip", "install", "--isolated", "--disable-pip-version-check", "--no-input", "--only-binary=:all:", "--target", str(target)]
    if offline: argv += ["--no-index", "--require-hashes"]
    if report is not None: argv += ["--report", str(report)]
    return argv + list(req)


def validate_wheel_report(report: Mapping[str, Any], *, max_artifacts: int = 256) -> tuple[dict, ...]:
    if not isinstance(report, Mapping) or not isinstance(report.get("install"), list): raise ValidationError("invalid pip report")
    if len(report["install"]) > max_artifacts: raise ValidationError("pip report has too many distributions")
    result = []
    for item in report["install"]:
        if not isinstance(item, Mapping): raise ValidationError("invalid pip report entry")
        metadata = item.get("metadata")
        download = item.get("download_info")
        digest = download.get("archive_info", {}).get("hashes", {}).get("sha256") if isinstance(download, Mapping) else None
        if not isinstance(metadata, Mapping) or not isinstance(download, Mapping) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValidationError("report entry lacks a wheel hash")
        url = str(download.get("url", ""))
        if not url.startswith("https://") or not url.lower().endswith(".whl"): raise ValidationError("only HTTPS wheels are accepted")
        result.append({"name": str(metadata.get("name", "")), "version": str(metadata.get("version", "")), "url": url, "sha256": digest.lower()})
    return tuple(result)


class ProcessorStore:
    """Immutable processor revisions below an injected Workbench data root."""
    def __init__(self, data_root: str | Path, scm_root: str | Path | None = None, *, contract: str = CONTRACT_VERSION):
        self.data_root = Path(data_root).resolve(); self.root = self.data_root / "postprocessing"; self.scm_root = Path(scm_root).resolve() if scm_root else None; self.contract = contract
        for directory in (self.root, self.root / "processors", self.root / "environments", self.root / "runs", self.root / "transactions"): directory.mkdir(parents=True, exist_ok=True); _no_links(directory)
        recover_transactions(self.root / "transactions")

    def _processor(self, processor_id: str) -> Path:
        processor_id = _safe_component(processor_id, "processor id", 64)
        if not re.fullmatch(r"[0-9a-f]{32}", processor_id):
            raise ValidationError("invalid processor id")
        return _contained(self.root / "processors", self.root / "processors" / processor_id)

    def _metadata(self, processor_id: str) -> dict:
        value = _read_json(self._processor(processor_id) / "metadata.json", "processor metadata")
        if not isinstance(value, dict): raise IntegrityError("invalid processor metadata")
        return value

    def save(self, name: str, source: str, requirements: str | Sequence[str] | None = None, *, processor_id: str | None = None, expected_revision: str | None = None) -> dict:
        name = normalize_name(name); raw = validate_source(source); req = normalize_requirements(requirements); revision = revision_digest(source, req, self.contract)
        if processor_id is None:
            if len(list((self.root / "processors").iterdir())) >= PROCESSOR_MAX_COUNT: raise ValidationError("processor limit reached")
            processor_id = uuid.uuid4().hex
            while self._processor(processor_id).exists(): processor_id = uuid.uuid4().hex
            directory = self._processor(processor_id); directory.mkdir(); (directory / "revisions").mkdir()
            current = None
        else:
            current_meta = self._metadata(processor_id); current = current_meta.get("active_revision")
            if expected_revision != current: raise ConflictError("processor revision is stale")
            directory = self._processor(processor_id); directory.joinpath("revisions").mkdir(exist_ok=True)
        source_path = directory / "revisions" / f"{revision}.py"; revision_meta = directory / "revisions" / f"{revision}.json"
        revisions = list((directory / "revisions").glob("*.py"))
        if not source_path.exists():
            if len(revisions) >= REVISIONS_MAX_COUNT: raise ValidationError("revision limit reached")
            if sum(p.stat().st_size for p in revisions) + len(raw) > SAVED_SOURCE_MAX_BYTES: raise ValidationError("saved processor source limit reached")
            _atomic_bytes(source_path, raw)
        elif source_path.read_bytes() != raw:
            raise IntegrityError("immutable revision content was changed")
        if not revision_meta.exists(): _atomic_json(revision_meta, {"revision": revision, "requirements": list(req), "contract": self.contract, "source_bytes": len(raw)})
        metadata = {"id": processor_id, "name": name, "active_revision": revision, "trusted": None, "updated": time.time()}
        _atomic_json(directory / "metadata.json", metadata)
        return self.get(processor_id)

    def get(self, processor_id: str, *, revision: str | None = None, include_source: bool = True) -> dict:
        metadata = self._metadata(processor_id); revision = revision or metadata.get("active_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision): raise IntegrityError("invalid processor revision")
        directory = self._processor(processor_id) / "revisions"; meta = _read_json(directory / f"{revision}.json", "revision metadata")
        source_path = directory / f"{revision}.py"
        if not isinstance(meta, dict) or not source_path.is_file(): raise NotFoundError("processor revision not found")
        result = {"id": metadata["id"], "name": metadata["name"], "active_revision": metadata.get("active_revision"), "revision": revision, "requirements": list(normalize_requirements(meta.get("requirements", []))), "trusted": metadata.get("trusted") == revision, "source_bytes": meta.get("source_bytes", 0)}
        if include_source: result["source"] = source_path.read_text(encoding="utf-8")
        return result

    def list(self) -> tuple[dict, ...]:
        result = []
        for directory in sorted((self.root / "processors").iterdir()):
            if directory.is_dir() and not directory.is_symlink():
                try: result.append(self.get(directory.name, include_source=False))
                except PostProcessingError: continue
        return tuple(result)

    def trust(self, processor_id: str, revision: str, environment: str | None = None, *, interpreter: str | Path = sys.executable) -> dict:
        item = self.get(processor_id, revision=revision, include_source=False)
        # Trust is tied to the exact revision and dependency fingerprint.  For
        # the empty environment there is nothing to install, so compute the
        # stable fingerprint instead of requiring the UI to know it.
        fingerprint = environment or self.environment_metadata(item["requirements"], interpreter=interpreter)["fingerprint"]
        _safe_component(fingerprint, "environment fingerprint", 128)
        metadata = self._metadata(processor_id)
        metadata["trusted"] = revision
        metadata["environment"] = fingerprint
        _atomic_json(self._processor(processor_id) / "metadata.json", metadata)
        return self.get(processor_id, revision=revision, include_source=False)

    def delete(self, processor_id: str, *, expected_revision: str | None = None) -> None:
        metadata = self._metadata(processor_id)
        if expected_revision is not None and expected_revision != metadata.get("active_revision"): raise ConflictError("processor revision is stale")
        shutil.rmtree(self._processor(processor_id))

    def duplicate(self, processor_id: str, *, name: str | None = None, expected_revision: str | None = None) -> dict:
        item = self.get(processor_id)
        if expected_revision is not None and expected_revision != item["active_revision"]:
            raise ConflictError("processor revision is stale")
        return self.save(name or (item["name"] + " copy"), item["source"], item["requirements"])

    def import_selected(self, source_path: str | Path, *, name: str | None = None) -> dict:
        """Import one stable, regular UTF-8 Python file selected by the OS picker."""
        path = Path(source_path)
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

    def environment_metadata(self, requirements: Sequence[str], lock_hash: str = "", *, interpreter: str | Path = sys.executable) -> dict:
        req = normalize_requirements(requirements); fingerprint = environment_fingerprint(req, lock_hash, interpreter=interpreter, contract=self.contract)
        path = self.root / "environments" / fingerprint; ready = _read_json(path / "ready.json", "environment marker", missing=None)
        # The private empty environment is always compatible and needs no pip
        # job.  Materialize its marker so trust/status and execution agree.
        if not req and not isinstance(ready, dict):
            path.mkdir(parents=True, exist_ok=True)
            _atomic_json(path / "ready.json", {"version": 1, "fingerprint": fingerprint, "requirements": [], "empty": True})
            ready = {"empty": True}
        return {"fingerprint": fingerprint, "requirements": list(req), "ready": isinstance(ready, dict), "path": str(path)}

    def status(self, processor_id: str, *, interpreter: str | Path = sys.executable) -> dict:
        item = self.get(processor_id, include_source=False)
        environment = self.environment_metadata(item["requirements"], interpreter=interpreter)
        trusted = item.get("trusted") and self._metadata(processor_id).get("environment") == environment["fingerprint"]
        return {"processor": {**item, "trusted": bool(trusted), "environment_fingerprint": self._metadata(processor_id).get("environment")},
                "environment": environment}

    def cleanup(self, *, max_age: float = 24 * 3600) -> None:
        cutoff = time.time() - max_age
        for parent in (self.root / "runs", self.root / "environments"):
            for child in parent.iterdir():
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)

    # Names useful to callers that prefer explicit verbs.
    save_revision = save
    list_processors = list
    get_processor = get
    delete_processor = delete
    duplicate_processor = duplicate

__all__ = [name for name in globals() if not name.startswith("_")]
