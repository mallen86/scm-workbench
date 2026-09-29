"""Bounded advisory detection of system CUDA runtime libraries (never loads them)."""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import subprocess
import threading
import time

_LOCK = threading.Lock()
_CACHE: tuple[float, str, str] | None = None
_CACHE_SECONDS = 60
_NAMES = {major: (f"libcudart.so.{major}", f"libcublas.so.{major}") for major in (12, 13)}
# Ordinary Linux loader search directories, not CUDA toolkit installs: a
# toolkit under /usr/local/cuda* is not visible unless registered in ldconfig
# or explicitly inherited by the fixed runner through LD_LIBRARY_PATH.
_SYSTEM_DIRS = ("/lib", "/lib64", "/lib/x86_64-linux-gnu", "/usr/lib",
                "/usr/lib64", "/usr/lib/x86_64-linux-gnu")
_MAX_CACHE_BYTES = 1024 * 1024


def inherited_library_dirs(raw: str | None = None) -> tuple[str, ...]:
    """The same bounded, absolute, existing directories passed to the runner."""
    if raw is None:
        raw = os.environ.get("LD_LIBRARY_PATH", "")
    if len(raw) > 4096:
        return ()
    paths = tuple(path for path in raw.split(":") if path and os.path.isabs(path) and
                  all(ord(char) >= 32 and ord(char) != 127 for char in path) and os.path.isdir(path))
    return paths if 0 < len(paths) <= 32 else ()


def _library_dirs() -> list[Path]:
    return [*(Path(path) for path in _SYSTEM_DIRS),
            *(Path(path) for path in inherited_library_dirs())]


def _loader_cache() -> str:
    executable = next((path for path in ("/sbin/ldconfig", "/usr/sbin/ldconfig") if os.path.isfile(path)), None)
    if not executable:
        return ""
    process = None
    try:
        # Bound bytes *as they arrive*, not after an unbounded temporary file
        # is filled. The fixed executable is killed and reaped on timeout or
        # excess output; no user command or CUDA library is executed.
        process = subprocess.Popen([executable, "-p"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
        deadline = time.monotonic() + 1
        chunks = []
        total = 0
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return ""
                chunk = os.read(process.stdout.fileno(), min(64 * 1024, _MAX_CACHE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_CACHE_BYTES:
                    return ""
                chunks.append(chunk)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return b"".join(chunks).decode("utf-8", "replace") if process.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            if process.stdout is not None:
                process.stdout.close()


def detect_cuda_profile() -> dict:
    """Recommend a major when both runtime and BLAS are visible; no GPU guarantee."""
    global _CACHE
    now = time.monotonic()
    with _LOCK:
        if _CACHE is not None and now - _CACHE[0] < _CACHE_SECONDS:
            return {"recommended": _CACHE[1], "reason": _CACHE[2]}
        names = set()
        for directory in _library_dirs():
            for pair in _NAMES.values():
                for name in pair:
                    try:
                        if (directory / name).is_file():
                            names.add(name)
                    except OSError:
                        pass
        cache = _loader_cache()
        # Only recognize exact SONAMEs in ldconfig's bounded output. A driver
        # reporting CUDA 13 support is not evidence of a CUDA 13 runtime.
        for major, pair in _NAMES.items():
            for name in pair:
                for match in re.finditer(r"(?m)^\s*" + re.escape(name) +
                                         r"\s+\([^\n]{0,160}\)\s+=>\s+(/[^\n]{1,512})$", cache):
                    try:
                        if Path(match.group(1)).is_file():
                            names.add(name)
                            break
                    except OSError:
                        pass
        major = next((m for m in (13, 12) if all(n in names for n in _NAMES[m])), None)
        recommended = f"cuda{major}" if major else "cuda12"
        reason = (f"CUDA {major} runtime and BLAS libraries are visible to the loader" if major else
                  "No complete CUDA 12 or 13 runtime/BLAS pair was found; defaulting to CUDA 12 for CPU fallback")
        _CACHE = (now, recommended, reason)
        return {"recommended": recommended, "reason": reason}
