"""Fixed, optional RealESRGAN_x4plus asset for the app-owned processor.

Only the installer calls download_model. Image jobs never access the network.
The converted ONNX weights have the same upstream checkpoint provenance; see
https://huggingface.co/skillsafe-ai/realesrgan-x4plus and docs/licenses/Real-ESRGAN.txt.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Exact roots *and* transitive dependencies; the installer additionally locks
# each platform-specific wheel by SHA-256 before the offline installation.
# The Linux GPU wheel supports CUDA 12.x / cuDNN 9 with system libraries; it
# includes the CPU provider for systems without compatible NVIDIA hardware.
_COMMON_REQUIREMENTS = (
    "flatbuffers==25.12.19", "numpy==2.5.3",
    "packaging==26.3", "protobuf==7.36.2",
)


def requirements_for_platform(platform: str) -> tuple[str, ...]:
    if platform == "win32":
        # DirectML supports Windows GPUs and includes a CPU provider. SymPy
        # depends on mpmath; both must be pinned for the trusted installer.
        runtime = ("onnxruntime-directml==1.24.4", "sympy==1.14.0", "mpmath==1.3.0")
    elif platform.startswith("linux"):
        runtime = ("onnxruntime-gpu==1.26.0",)
    else:
        # The macOS wheel includes CoreML and CPU execution providers.
        runtime = ("onnxruntime==1.30.0",)
    return tuple(sorted((*_COMMON_REQUIREMENTS, *runtime)))


REQUIREMENTS = requirements_for_platform(sys.platform)
MODEL_NAME = "RealESRGAN_x4plus.onnx"
MODEL_SHA256 = "4851ec156207d271f5328605d0582eeb851e656227da8aca093ced9e60789291"
MODEL_BYTES = 67_051_650
MODEL_URL = (
    "https://huggingface.co/skillsafe-ai/realesrgan-x4plus/resolve/"
    "107475e49b7d46412cc966481efccc3500955801/model.onnx"
)
_ALLOWED_HOSTS = frozenset({"huggingface.co", "us.aws.cdn.hf.co"})


def _approved_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS and
                parsed.port in (None, 443) and not parsed.username and not parsed.password and
                not parsed.fragment and len(url) < 4096)
    except ValueError:
        return False


class _PinnedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        self.redirects = 0

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > 3 or not _approved_url(newurl):
            raise ValueError("model download redirected to an unapproved location")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def verify_model(path: Path) -> bool:
    """Hash a regular, size-pinned model without loading or executing it."""
    try:
        st = path.lstat()
        if not path.is_file() or path.is_symlink() or st.st_size != MODEL_BYTES or st.st_nlink != 1:
            return False
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns):
                return False
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            final = os.fstat(fd)
            if (final.st_size, final.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                return False
        finally:
            os.close(fd)
        return digest.hexdigest() == MODEL_SHA256
    except OSError:
        return False


def download_model(destination: Path) -> None:
    """Stream the fixed model into a private install stage, bounded and verified."""
    if not _approved_url(MODEL_URL):
        raise ValueError("model source is not approved")
    opener = urllib.request.build_opener(_PinnedRedirects())
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "SCM-Workbench/model-installer"})
    with opener.open(request, timeout=20) as response:
        if not _approved_url(response.geturl()):
            raise ValueError("model source changed during download")
        declared = response.headers.get("Content-Length")
        if declared is not None and (not declared.isdecimal() or int(declared) != MODEL_BYTES):
            raise ValueError("model download has an unexpected size")
        digest = hashlib.sha256()
        count = 0
        # An exclusive file inside the private install stage is never made
        # ready until both the exact byte count and SHA-256 match.
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > MODEL_BYTES:
                    raise ValueError("model download exceeds its size limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if count != MODEL_BYTES or digest.hexdigest() != MODEL_SHA256:
            raise ValueError("model download did not match the pinned size and SHA-256")
