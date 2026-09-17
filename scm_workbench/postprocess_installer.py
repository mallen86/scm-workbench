"""Trusted wheel-only dependency installer for image processors.

The worker writes a private manifest and launches this helper in its own job
process group.  Resolution happens against the fixed PyPI index, every
transitive wheel is pinned into a hash lock, wheels are downloaded and checked,
and installation then runs offline into a staging target.  Nothing is
published here; the parent validates and atomically publishes the environment.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

# ``-I`` intentionally removes the source checkout from sys.path.  This is a
# trusted Workbench bootstrap, so add only its package parent before importing
# the shared validators; the later user processor runner does not do this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scm_workbench import postprocessing  # noqa: E402

MAX_MANIFEST_BYTES = 128 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_WHEELS = 256
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_WHEELS_BYTES = 1024 * 1024 * 1024
PYPI_INDEX = "https://pypi.org/simple"


class InstallerError(Exception):
    pass


def _regular(path: Path, label: str, limit: int | None = None) -> os.stat_result:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise InstallerError(f"{label} is missing") from exc
    if (stat.S_ISLNK(observed.st_mode) or getattr(observed, "st_reparse_tag", 0) or
            not stat.S_ISREG(observed.st_mode) or getattr(observed, "st_nlink", 1) != 1):
        raise InstallerError(f"{label} is not a private regular file")
    if limit is not None and observed.st_size > limit:
        raise InstallerError(f"{label} is too large")
    return observed


def _read_regular(path: Path, label: str, limit: int) -> bytes:
    before = _regular(path, label, limit)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                getattr(before, "st_nlink", 1))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                    getattr(opened, "st_nlink", 1)) != identity:
                raise InstallerError(f"{label} changed while opening")
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
    except OSError as exc:
        raise InstallerError(f"could not read {label}") from exc
    if (len(raw) > limit or
            (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns,
             getattr(after_read, "st_nlink", 1)) != identity or
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             getattr(after, "st_nlink", 1)) != identity):
        raise InstallerError(f"{label} changed while reading")
    return raw


def _hash_regular(path: Path, label: str, limit: int) -> tuple[int, str]:
    before = _regular(path, label, limit)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                getattr(before, "st_nlink", 1))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    total = 0
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                    getattr(opened, "st_nlink", 1)) != identity:
                raise InstallerError(f"{label} changed while opening")
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise InstallerError(f"{label} is too large")
                digest.update(chunk)
            after_read = os.fstat(fd)
        finally:
            os.close(fd)
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise InstallerError(f"could not read {label}") from exc
    if (total != before.st_size or
            (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns,
             getattr(after_read, "st_nlink", 1)) != identity or
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             getattr(after, "st_nlink", 1)) != identity):
        raise InstallerError(f"{label} changed while reading")
    return total, digest.hexdigest()


def _load_manifest(path: Path) -> dict:
    if not path.is_absolute():
        raise InstallerError("installer manifest must be absolute")
    try:
        value = json.loads(_read_regular(path, "installer manifest", MAX_MANIFEST_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerError("installer manifest is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "stage", "target", "requirements", "resolve_report", "lock_file", "wheelhouse",
    }:
        raise InstallerError("installer manifest has invalid fields")
    stage = Path(value["stage"])
    if not stage.is_absolute() or stage.is_symlink() or not stage.is_dir():
        raise InstallerError("installer stage is invalid")
    try:
        postprocessing._no_links(stage)
    except postprocessing.PostProcessingError as exc:
        raise InstallerError("installer stage is invalid") from exc
    resolved_stage = stage.resolve(strict=True)
    if Path(os.path.abspath(path)) != Path(os.path.abspath(stage)) / "installer-manifest.json":
        raise InstallerError("installer manifest is outside its stage")
    for key in ("target", "resolve_report", "lock_file", "wheelhouse"):
        candidate = Path(value[key])
        if not candidate.is_absolute():
            raise InstallerError("installer path is invalid")
        try:
            candidate.resolve(strict=False).relative_to(resolved_stage)
        except ValueError as exc:
            raise InstallerError("installer path escapes its stage") from exc
    value["requirements"] = list(postprocessing.normalize_requirements(value["requirements"]))
    return value


def _run(argv: list[str], label: str) -> None:
    print(f"[processor libraries] {label}", flush=True)
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, check=False, shell=False,
        )
    except OSError as exc:
        raise InstallerError(f"could not start {label.lower()}") from exc
    if completed.returncode != 0:
        raise InstallerError(f"{label.lower()} failed")


def _read_report(path: Path) -> dict:
    try:
        value = json.loads(_read_regular(path, "pip resolution report", MAX_REPORT_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerError("pip resolution report is invalid") from exc
    return value


def _verify_downloads(wheelhouse: Path, expected: set[str]) -> None:
    try:
        entries = list(itertools.islice(wheelhouse.iterdir(), MAX_WHEELS + 1))
    except OSError as exc:
        raise InstallerError("could not inspect downloaded wheels") from exc
    if not entries or len(entries) > MAX_WHEELS:
        raise InstallerError("downloaded wheel count is invalid")
    observed: set[str] = set()
    total = 0
    for path in entries:
        if path.suffix.lower() != ".whl":
            raise InstallerError("download produced a non-wheel artifact")
        size, value = _hash_regular(path, "downloaded wheel", MAX_WHEEL_BYTES)
        total += size
        if total > MAX_WHEELS_BYTES:
            raise InstallerError("downloaded wheels exceed the storage limit")
        if value not in expected or value in observed:
            raise InstallerError("downloaded wheel hash does not match the lock")
        observed.add(value)
    if observed != expected:
        raise InstallerError("downloaded wheel set is incomplete")


def _set_limits() -> None:
    if os.name == "nt":
        if not postprocessing._apply_windows_job_limits(
                cpu_seconds=900, address_space=4 * 1024 * 1024 * 1024, processes=16):
            raise InstallerError("Windows process limits could not be established")
        return
    if os.name != "posix":
        return
    try:
        import resource
    except ImportError:
        return
    values = {
        "RLIMIT_CPU": 900,
        "RLIMIT_AS": 4 * 1024 * 1024 * 1024,
        "RLIMIT_FSIZE": MAX_WHEEL_BYTES,
        "RLIMIT_NOFILE": 128,
    }
    for name, value in values.items():
        if not hasattr(resource, name):
            continue
        try:
            kind = getattr(resource, name)
            _soft, hard = resource.getrlimit(kind)
            bounded = min(value, hard) if hard != resource.RLIM_INFINITY else value
            resource.setrlimit(kind, (bounded, bounded))
        except (OSError, ValueError):
            continue


def install(manifest_path: Path) -> None:
    manifest = _load_manifest(manifest_path)
    requirements = manifest["requirements"]
    if not requirements:
        raise InstallerError("installer requires at least one package")
    target = Path(manifest["target"])
    resolve_report = Path(manifest["resolve_report"])
    lock_file = Path(manifest["lock_file"])
    wheelhouse = Path(manifest["wheelhouse"])
    target.mkdir(mode=0o700, exist_ok=False)
    wheelhouse.mkdir(mode=0o700, exist_ok=False)

    common = [sys.executable, "-m", "pip"]
    _run(common + [
        "install", "--dry-run", "--ignore-installed", "--isolated",
        "--disable-pip-version-check", "--no-input", "--no-cache-dir",
        "--only-binary=:all:", "--index-url", PYPI_INDEX,
        "--report", str(resolve_report), *requirements,
    ], "Resolving compatible PyPI wheels")
    wheels = postprocessing.validate_wheel_report(_read_report(resolve_report), max_artifacts=MAX_WHEELS)
    try:
        with lock_file.open("x", encoding="utf-8") as stream:
            postprocessing._private(lock_file)
            stream.write(postprocessing.wheel_lock_text(wheels))
            stream.flush(); os.fsync(stream.fileno())
    except OSError as exc:
        raise InstallerError("could not write the dependency lock") from exc
    expected_hashes = {wheel["sha256"] for wheel in wheels}
    if len(expected_hashes) != len(wheels):
        raise InstallerError("pip reported duplicate wheel artifacts")

    _run(common + [
        "download", "--isolated", "--disable-pip-version-check", "--no-input",
        "--no-cache-dir", "--only-binary=:all:", "--no-deps", "--require-hashes",
        "--index-url", PYPI_INDEX, "--dest", str(wheelhouse), "-r", str(lock_file),
    ], "Downloading the locked wheel set")
    _verify_downloads(wheelhouse, expected_hashes)

    install_report = Path(manifest["stage"]) / "install-report.json"
    _run(common + [
        "install", "--isolated", "--disable-pip-version-check", "--no-input",
        "--no-cache-dir", "--no-index", "--find-links", str(wheelhouse),
        "--only-binary=:all:", "--no-deps", "--no-compile", "--require-hashes",
        "--target", str(target), "--report", str(install_report),
        "-r", str(lock_file),
    ], "Installing the verified wheels offline")
    _regular(install_report, "pip install report", MAX_REPORT_BYTES)
    print("[processor libraries] Offline wheel installation complete", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        _set_limits()
        install(Path(args.manifest))
        return 0
    except Exception as exc:
        print(f"processor library installation failed: {' '.join(str(exc).split())[:512]}",
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
