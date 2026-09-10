#!/usr/bin/env python3
"""
updater.py — check for, and install, newer versions of the SCM Workbench app.

The app is packaged as a Tauri bundle and shipped as GitHub release assets
(macOS: a drag-to-Applications DMG; Windows: a flat portable ZIP). This module
talks to the releases of the Workbench's own repository:

  * fetch the newest release (the release repo is private, so the check
    can't see it until the repo is made public - no credentials anywhere
    in the meantime),
  * compare it with the running version,
  * on "update available" an in-process job downloads the exact platform
    asset. macOS DMGs are mounted read-only and copied into a validated app
    candidate; Windows ZIPs use the bounded extractor. The candidate is then
    handed to the native helper through a durable journal/request protocol.
    The helper owns publication, health verification, rollback, and relaunch.

The swap only touches the *app* folder; the data area (settings, job
history, managed repo copies, the private runtime) lives elsewhere and is
never part of the swap, so an update can never lose user data.

Standard library only — same rule as the rest of the Workbench.
"""

import hashlib
import json
import os
import plistlib
import posixpath
import re
import secrets
import signal
import struct
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

USER_AGENT = "scm-workbench-updater/0.1"
METADATA_MAX_BYTES = 2 * 1024 * 1024
TOTAL_DEADLINE_SECONDS = 30
DOWNLOAD_DEADLINE_SECONDS = 60
ASSET_MAX_BYTES = 1 << 30

ARCHIVE_MEMBER_MAX = 20_000
ARCHIVE_MAX_BYTES = 1 << 30
ARCHIVE_UNCOMPRESSED_MAX = 4 << 30
ARCHIVE_MEMBER_MAX_BYTES = 1 << 30
ARCHIVE_SYMLINK_MAX_BYTES = 4 << 10
ARCHIVE_NAME_MAX_BYTES = 4096
ARCHIVE_COMPONENT_MAX_BYTES = 255
ARCHIVE_COMPRESSION_RATIO_MAX = 200

# macOS uses its standard DMG for both manual and in-app installation.
# Windows remains a portable ZIP application.
MACOS_DMG_ASSET = "scm-workbench-macos.dmg"
WINDOWS_ASSET = "scm-workbench-windows.zip"
HDIUTIL = "/usr/bin/hdiutil"
CODESIGN = "/usr/bin/codesign"
DMG_COMMAND_TIMEOUT = 30
DMG_COMMAND_OUTPUT_MAX = 64 * 1024
DMG_PLIST_MAX_BYTES = 256 * 1024
DMG_TREE_MAX_ENTRIES = 20_000
DMG_TREE_MAX_BYTES = 4 * 1024 * 1024 * 1024
DMG_TREE_MAX_PATH_BYTES = 4096
DMG_TREE_MAX_COMPONENT_BYTES = 255
DMG_PLIST_MAX_FILE_BYTES = 256 * 1024
UPDATE_RESTART_GRACE_SECONDS = 8

# These remain environment-overridable for test fixtures and forks.  They are
# validated at request time: configuration must not turn the API path into a
# second URL or make a release check follow an untrusted origin.
API = os.environ.get("SCM_WORKBENCH_GITHUB_API") or "https://api.github.com"
UPDATE_REPO = os.environ.get("SCM_WORKBENCH_UPDATE_REPO") or "mallen86/scm-workbench"

APP_NAME = "SCM Workbench"

_REPO_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _repo_parts() -> tuple[str, str]:
    if not isinstance(UPDATE_REPO, str) or UPDATE_REPO.count("/") != 1:
        raise UpdateError("the configured update repository is invalid")
    owner, name = UPDATE_REPO.split("/")
    if not (_REPO_PART_RE.fullmatch(owner) and _REPO_PART_RE.fullmatch(name)):
        raise UpdateError("the configured update repository is invalid")
    return owner, name


def _api_origin() -> tuple[str, str, int | None]:
    if not isinstance(API, str):
        raise UpdateError("the configured GitHub API is invalid")
    try:
        parsed = urllib.parse.urlsplit(API)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        host = port = None
        parsed = None
    if (parsed is None or parsed.scheme.lower() != "https" or not host or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment):
        raise UpdateError("the configured GitHub API must be an HTTPS host")
    # A path is allowed for test reverse proxies, but it cannot affect the
    # origin check and is always followed by the fixed API endpoint path.
    return parsed.scheme.lower(), host.lower(), port


def _same_origin(url: str, expected: str) -> bool:
    """Require redirects to retain scheme, host, port, and no userinfo."""
    try:
        actual = urllib.parse.urlsplit(url)
        want = urllib.parse.urlsplit(expected)
        if (actual.username is not None or actual.password is not None or
                actual.scheme.lower() != want.scheme.lower() or
                (actual.hostname or "").lower() != (want.hostname or "").lower()):
            return False
        return actual.port == want.port
    except ValueError:
        return False


def _text(value, field: str, maximum: int, *, required: bool = False,
          allowed_controls: str = "") -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise UpdateError(f"GitHub release field {field} is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise UpdateError(f"GitHub release field {field} is invalid")
    if not value and required:
        raise UpdateError(f"GitHub release field {field} is empty")
    if size > maximum or any((ord(c) < 0x20 or ord(c) == 0x7f) and c not in allowed_controls
                              for c in value):
        raise UpdateError(f"GitHub release field {field} is invalid")
    return value


def _tag(value, field: str = "tag_name") -> str:
    value = _text(value, field, 128, required=True)
    if "/" in value or "\\" in value or value in (".", ".."):
        raise UpdateError(f"GitHub release field {field} is invalid")
    return value


def _github_release_url(url: str, owner: str, repo: str, tag: str) -> str:
    value = _text(url, "html_url", 2048, required=True)
    try:
        parsed = urllib.parse.urlsplit(value)
        expected = f"/{owner}/{repo}/releases/tag/{urllib.parse.quote(tag, safe='') }"
        if (parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "github.com" or
                parsed.port is not None or parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or urllib.parse.unquote(parsed.path) != expected):
            raise ValueError
    except (ValueError, UnicodeError):
        raise UpdateError("GitHub release html_url is invalid")
    return value


def _asset_name(value) -> str:
    value = _text(value, "asset.name", 255, required=True)
    if value in (".", "..") or "/" in value or "\\" in value:
        raise UpdateError("GitHub release asset name is unsafe")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        raise UpdateError("GitHub release asset name is unsafe")
    return value


def _asset_url(value, owner: str, repo: str, tag: str, name: str) -> str:
    value = _text(value, "asset.browser_download_url", 2048, required=True)
    try:
        parsed = urllib.parse.urlsplit(value)
        expected = f"/{owner}/{repo}/releases/download/{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(name, safe='') }"
        if (parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "github.com" or
                parsed.port is not None or parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or urllib.parse.unquote(parsed.path) != expected):
            raise ValueError
    except (ValueError, UnicodeError):
        raise UpdateError("GitHub release asset URL is invalid")
    return value


class UpdateError(Exception):
    """A check/install problem with a user-showable message."""


class AuthRequiredError(UpdateError):
    """The release repo can't be seen anonymously (it is still private -
    making it public is all that's needed)."""


# ----------------------------------------------------------------------------
# Versions
# ----------------------------------------------------------------------------

_VER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.\-]+))?$")


def canonical_version(s: str) -> str | None:
    """Return the release version in the package's canonical spelling."""
    match = _VER_RE.fullmatch(str(s or "").strip())
    if not match:
        return None
    major, minor, patch = (int(match.group(i) or 0) for i in (1, 2, 3))
    suffix = f"-{match.group(4)}" if match.group(4) else ""
    return f"{major}.{minor}.{patch}{suffix}"


def parse_version(s) -> tuple:
    """'v0.1.0' / '1.2' / '2.0.0-rc1' -> a comparable tuple (None if unparseable).

    A prerelease suffix sorts *before* its final release, so 0.2.0-rc1 is
    offered for 0.1.0 but 0.2.0-rc1 is not an update over 0.2.0."""
    m = _VER_RE.match(str(s or "").strip())
    if not m:
        return None
    nums = tuple(int(m.group(i) or 0) for i in (1, 2, 3))
    return nums + ((-1 if m.group(4) else 0),)


def is_newer(latest, current) -> bool:
    try:
        a, b = parse_version(latest), parse_version(current)
    except Exception:
        return False
    if not a or not b:
        return False
    return a > b


# ----------------------------------------------------------------------------
# GitHub over plain HTTPS
# ----------------------------------------------------------------------------

def _set_response_timeout(response, seconds: float) -> None:
    """Best-effort socket timeout update across urllib/test response shapes."""
    seconds = max(0.001, float(seconds))
    candidates = [response]
    seen = set()
    while candidates:
        obj = candidates.pop(0)
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        setter = getattr(obj, "settimeout", None)
        if callable(setter):
            try:
                setter(seconds)
            except (OSError, ValueError):
                pass
        for attr in ("fp", "raw", "_sock", "sock", "socket"):
            try:
                child = getattr(obj, attr, None)
            except Exception:
                child = None
            if child is not None and child is not obj:
                candidates.append(child)


def _response_read(response, size: int, deadline: float, *, label: str) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UpdateError(f"GitHub {label} timed out")
    _set_response_timeout(response, remaining)
    try:
        data = response.read(size)
    except Exception as exc:
        if isinstance(exc, (TimeoutError, OSError, urllib.error.URLError)):
            raise UpdateError(f"GitHub {label} timed out") from exc
        raise
    # A read can return after its socket timeout or a maliciously slow test
    # seam can return bytes late.  Do not accept those bytes.
    if time.monotonic() > deadline:
        raise UpdateError(f"GitHub {label} timed out")
    return data


def gh_request(path: str, method: str = "GET", timeout: int = 30,
              stream_to=None, progress=None):
    """Make a bounded API request and close every response.

    Metadata responses are capped at 2 MiB plus one byte so a server cannot
    make the JSON parser consume unbounded memory.  ``timeout`` is a total
    monotonic deadline, not a fresh socket timeout for every read.
    """
    owner, repo = _repo_parts()
    scheme, host, port = _api_origin()
    base = API.rstrip("/")
    url = base + path if path.startswith("/") else path
    if path.startswith("/"):
        expected_origin = f"{scheme}://{host}" + (f":{port}" if port is not None else "")
        parsed_base = urllib.parse.urlsplit(base)
        if parsed_base.path:
            # Keep a configured proxy prefix while still checking its origin.
            expected_origin = urllib.parse.urlunsplit((scheme, parsed_base.netloc, "", "", ""))
    else:
        expected_origin = url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers, method=method)
    deadline = time.monotonic() + min(max(float(timeout), 0.0), TOTAL_DEADLINE_SECONDS)
    try:
        remaining = max(0.001, deadline - time.monotonic())
        r = urllib.request.urlopen(req, timeout=remaining)
    except urllib.error.HTTPError as e:
        final_error_url = getattr(e, "geturl", lambda: url)()
        if not _same_origin(final_error_url, expected_origin):
            try:
                e.close()
            except Exception:
                pass
            raise UpdateError("GitHub redirected the release lookup to an untrusted host")
        try:
            e.close()
        except Exception:
            pass
        return e.code, e.headers, b""
    except Exception as e:
        raise UpdateError(f"could not reach GitHub ({urllib.parse.urlsplit(url).netloc}): {e}")
    try:
        final_url = getattr(r, "geturl", lambda: url)()
        if not _same_origin(final_url, expected_origin):
            raise UpdateError("GitHub redirected the release lookup to an untrusted host")
        if stream_to is not None:
            raw_total = r.headers.get("Content-Length")
            try:
                total = int(raw_total or 0)
            except (TypeError, ValueError):
                raise UpdateError("GitHub returned an invalid Content-Length")
            done = 0
            if progress is not None:
                progress(0, total)
            with open(stream_to, "wb") as f:
                while True:
                    remaining = deadline - time.monotonic()
                    b = _response_read(r, min(1 << 20, max(1, int(max(0.001, remaining) * (1 << 20)))),
                                       deadline, label="download")
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    if progress is not None:
                        progress(done, total)
            return r.status, r.headers, None
        raw_length = r.headers.get("Content-Length")
        if raw_length is not None:
            try:
                if int(raw_length) > METADATA_MAX_BYTES:
                    raise UpdateError("GitHub release metadata is too large")
            except (TypeError, ValueError):
                raise UpdateError("GitHub returned an invalid Content-Length")
        chunks = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpdateError("GitHub release lookup timed out")
            b = _response_read(r, min(64 * 1024, METADATA_MAX_BYTES + 1 - size),
                               deadline, label="release lookup")
            if not b:
                break
            chunks.append(b)
            size += len(b)
            if size > METADATA_MAX_BYTES:
                raise UpdateError("GitHub release metadata is too large")
        return r.status, r.headers, b"".join(chunks)
    finally:
        r.close()


def _validated_asset(raw: dict, owner: str, repo: str, tag: str) -> dict:
    if not isinstance(raw, dict):
        raise UpdateError("GitHub release asset is invalid")
    asset_id = raw.get("id")
    if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0:
        raise UpdateError("GitHub release asset id is invalid")
    name = _asset_name(raw.get("name"))
    url = _asset_url(raw.get("browser_download_url"), owner, repo, tag, name)
    size = raw.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= ASSET_MAX_BYTES:
        raise UpdateError("GitHub release asset size is invalid")
    if "digest" in raw:
        digest = raw["digest"]
        if digest is not None and (not isinstance(digest, str) or
                                   not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)):
            raise UpdateError("GitHub release asset digest is invalid")
    else:
        digest = None
    return {"id": asset_id, "tag": tag, "name": name, "url": url,
            "size": size, "digest": digest}


def latest_release(timeout: int = 25) -> dict:
    """Fetch and strictly validate the newest release metadata."""
    owner, repo = _repo_parts()
    status, headers, body = gh_request(f"/repos/{owner}/{repo}/releases/latest", timeout=timeout)
    if status == 200:
        try:
            rel = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise UpdateError("GitHub returned invalid release metadata")
        if not isinstance(rel, dict):
            raise UpdateError("GitHub returned invalid release metadata")
        tag = _tag(rel.get("tag_name"))
        # GitHub currently calls these target_commitish; accepting the
        # aliases makes the schema explicit for compatible API fixtures.
        for field in ("version", "ref", "target_commitish"):
            if field in rel and rel[field] is not None:
                _text(rel[field], field, 256, required=True)
        name = _text(rel.get("name"), "name", 512) or tag
        body_text = _text(rel.get("body"), "body", 256 * 1024,
                           allowed_controls="\n\r\t")
        published = _text(rel.get("published_at"), "published_at", 64)
        url = _github_release_url(rel.get("html_url"), owner, repo, tag)
        raw_assets = rel.get("assets")
        if not isinstance(raw_assets, list) or len(raw_assets) > 100:
            raise UpdateError("GitHub release assets are invalid")
        assets = [_validated_asset(a, owner, repo, tag) for a in raw_assets]
        return {"tag": tag, "name": name, "body": body_text,
                "published": published, "url": url, "assets": assets}
    if status == 404:
        raise AuthRequiredError(
            "the release repo can't be seen — it is still private; "
            "making it public is all that's needed for checks to work")
    if status in (403, 429):
        raise UpdateError("GitHub rate-limited the check — try again in a few minutes")
    raise UpdateError(f"GitHub API error {status} on the releases lookup")


def pick_asset(release: dict, platform: str = None) -> dict:
    """Select one exact supported release asset.

    macOS accepts exactly one DMG and Windows accepts exactly one portable
    ZIP. Other package names never become implicit fallbacks.
    """
    platform = platform or (sys.platform if os.name != "nt" else "win32")
    if platform in ("darwin", "macos", "darwin-arm64", "macos-arm64"):
        expected = (MACOS_DMG_ASSET,)
        label = "darwin arm64"
    elif platform in ("win32", "windows", "windows-x64", "win64"):
        expected = (WINDOWS_ASSET,)
        label = "windows x64"
    else:
        raise UpdateError(f"the release has no installable archive for {platform}")
    assets = release.get("assets") if isinstance(release, dict) else None
    assets = assets if isinstance(assets, list) else []
    matches = {
        name: [a for a in assets if isinstance(a, dict) and a.get("name") == name]
        for name in expected
    }
    if any(len(values) > 1 for values in matches.values()):
        names = ", ".join(str(a.get("name", "")) for a in assets if isinstance(a, dict)) or "none"
        raise UpdateError(f"the release has no unambiguous {label} asset (assets: {names})")
    for name in expected:
        if matches[name]:
            return matches[name][0]
    names = ", ".join(str(a.get("name", "")) for a in assets if isinstance(a, dict)) or "none"
    raise UpdateError(f"the release has no unambiguous {label} asset (assets: {names})")


# ----------------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------------

# GitHub's documented release redirect hosts.  Keep this exact (no wildcard
# subdomains): older GitHub deployments used objects.githubusercontent.com.
_DOWNLOAD_HOSTS = frozenset(("github.com", "release-assets.githubusercontent.com",
                             "objects.githubusercontent.com"))


def _download_metadata(url, expected_asset):
    if isinstance(url, dict):
        if expected_asset is not None and url is not expected_asset:
            raise UpdateError("download asset metadata does not match its URL")
        expected_asset = url
        url = expected_asset.get("url")
    if not isinstance(expected_asset, dict):
        raise UpdateError("download requires validated release asset metadata")
    tag = expected_asset.get("tag")
    name = expected_asset.get("name")
    size = expected_asset.get("size")
    digest = expected_asset.get("digest")
    try:
        tag = _tag(tag, "asset.tag")
        name = _asset_name(name)
    except UpdateError:
        raise UpdateError("download asset metadata is invalid")
    owner, repo = _repo_parts()
    bound_url = _asset_url(expected_asset.get("url"), owner, repo, tag, name)
    if url != bound_url:
        raise UpdateError("download URL is not the validated release asset")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= ASSET_MAX_BYTES:
        raise UpdateError("download asset size is invalid")
    if digest is not None and (not isinstance(digest, str) or
                               not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)):
        raise UpdateError("download asset digest is invalid")
    return bound_url, tag, name, size, digest


def _approved_download_redirect(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme.lower() == "https" and
                (parsed.hostname or "").lower() in _DOWNLOAD_HOSTS and
                parsed.port is None and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def download(url, dest: Path, progress=None, timeout: int = 60, *, expected_asset=None,
             expected_size=None, expected_digest=None, expected_tag=None,
             expected_name=None) -> int:
    """Download one already-validated release asset without damaging ``dest``."""
    if isinstance(url, dict) and expected_asset is None:
        expected_asset = url
        url = expected_asset.get("url")
    if expected_asset is None and any(value is not None for value in
                                      (expected_size, expected_digest, expected_tag, expected_name)):
        expected_asset = {"url": url, "size": expected_size, "digest": expected_digest,
                          "tag": expected_tag, "name": expected_name}
    if expected_asset is not None:
        for key, supplied in (("size", expected_size), ("digest", expected_digest),
                              ("tag", expected_tag), ("name", expected_name)):
            if supplied is not None and expected_asset.get(key) != supplied:
                raise UpdateError(f"download {key} does not match expected metadata")
    url, tag, name, expected_size, expected_digest = _download_metadata(url, expected_asset)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, part_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part",
                                     dir=str(dest.parent))
    os.close(fd)
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    deadline = time.monotonic() + min(max(float(timeout), 0.0), DOWNLOAD_DEADLINE_SECONDS)
    r = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateError("download timed out")
        try:
            r = urllib.request.urlopen(req, timeout=remaining)
        except urllib.error.HTTPError as e:
            try:
                e.close()
            except Exception:
                pass
            if e.code in (401, 404):
                raise UpdateError("the download can't see the asset — the release "
                                   "repo is still private; once it is made public "
                                   "this works")
            raise UpdateError(f"download failed with HTTP {e.code}")
        except Exception as e:
            raise UpdateError(f"download failed: {e}")
        final_url = getattr(r, "geturl", lambda: url)()
        if not _approved_download_redirect(final_url):
            raise UpdateError("download redirected to an untrusted host")
        raw_length = r.headers.get("Content-Length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except (TypeError, ValueError):
                raise UpdateError("download returned an invalid Content-Length")
            if declared < 0 or declared > ASSET_MAX_BYTES or declared != expected_size:
                raise UpdateError("download size does not match the release asset")
        done = 0
        digest = hashlib.sha256()
        # The release metadata size remains the integrity boundary, but only
        # advertise a determinate total when the download response itself
        # confirms it. GitHub/CDN responses without Content-Length must keep
        # the UI's cycling progress animation rather than implying precision.
        progress_total = expected_size if raw_length is not None else 0
        if progress is not None:
            progress(0, progress_total)
        with open(part_name, "wb") as f:
            while True:
                remaining = deadline - time.monotonic()
                read_size = min(1 << 20, expected_size - done + 1)
                b = _response_read(r, read_size, deadline, label="download")
                if not b:
                    break
                done += len(b)
                if done > expected_size:
                    raise UpdateError("download exceeded the release asset size")
                f.write(b)
                digest.update(b)
                if progress is not None:
                    progress(done, progress_total)
            if done != expected_size:
                raise UpdateError("download ended before the release asset size")
            if expected_digest and digest.hexdigest().lower() != expected_digest.split(":", 1)[1].lower():
                raise UpdateError("download digest does not match the release asset")
            f.flush()
            os.fsync(f.fileno())
        os.replace(part_name, str(dest))
        return done
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f"download failed: {e}")
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        try:
            os.unlink(part_name)
        except FileNotFoundError:
            pass


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename *source* to an absent destination, or fail closed."""
    source = os.fspath(source)
    destination = os.fspath(destination)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        # Omitting MOVEFILE_REPLACE_EXISTING is the no-replace operation.
        if move(source, destination, 0x00000008):  # MOVEFILE_WRITE_THROUGH
            return
        error = ctypes.get_last_error()
        if error in (2, 80, 183):  # FILE_NOT_FOUND, FILE_EXISTS, ALREADY_EXISTS
            raise FileExistsError(error, "destination already exists", destination)
        raise OSError(error, "MoveFileExW failed", destination)

    import ctypes
    import errno
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        renamex = getattr(libc, "renamex_np", None)
        if renamex is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        renamex.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        renamex.restype = ctypes.c_int
        result = renamex(os.fsencode(source), os.fsencode(destination), 0x00000004)
    else:
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint)
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(error, "destination already exists", destination)
    if error in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", -1)):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    raise OSError(error, os.strerror(error), destination)


def extract_app(zip_path: Path, dest_dir: Path, log=print, *, publish_bundle_root: bool = False) -> Path:
    """Preflight a bounded ZIP, securely extract it, and publish atomically."""
    from scm_workbench import repo_sync

    zip_path, dest_dir = Path(zip_path), Path(dest_dir)
    stage = None

    def bad(message):
        raise UpdateError(f"the release archive is unsafe: {message}")

    component_aliases = {}
    reserved_stems = {"con", "prn", "aux", "nul"}
    reserved_stems.update(f"com{i}" for i in range(1, 10))
    reserved_stems.update(f"lpt{i}" for i in range(1, 10))

    def archive_name(raw):
        if not isinstance(raw, str):
            bad("entry name is invalid")
        try:
            if not raw.encode("utf-8") or len(raw.encode("utf-8")) > ARCHIVE_NAME_MAX_BYTES:
                bad("entry name is empty or too long")
        except UnicodeEncodeError:
            bad("entry name is not valid UTF-8")
        if ("\x00" in raw or "\\" in raw or raw.startswith("/") or
                raw.startswith("//") or re.match(r"^[A-Za-z]:", raw)):
            bad("entry name has an unsafe path form")
        directory = raw.endswith("/")
        value = raw[:-1] if directory else raw
        parts = value.split("/")
        if not value or any(not p or p in (".", "..") for p in parts):
            bad("entry name has an empty or dot component")
        normalized = []
        parent_key = ""
        for original in parts:
            part = unicodedata.normalize("NFC", original)
            try:
                size = len(part.encode("utf-8"))
            except UnicodeEncodeError:
                bad("entry name is not valid UTF-8")
            if not 1 <= size <= ARCHIVE_COMPONENT_MAX_BYTES or part in (".", ".."):
                bad("entry name component is invalid")
            # Windows aliases are unsafe even when the archive's complete
            # paths are different: Foo/a and foo/b would share a directory.
            folded = part.casefold()
            aliases = component_aliases.setdefault(parent_key, {})
            if folded in aliases and aliases[folded] != part:
                bad("Unicode/case-fold-colliding path component")
            aliases[folded] = part
            trimmed = part.rstrip(". ")
            stem = trimmed.split(".", 1)[0].casefold()
            # Leading-dot names such as .dylibs and .empty are valid on both
            # Windows and macOS. Only an entirely dot/space component, a DOS
            # device stem, an alternate-data-stream colon, or a trailing dot
            # or space is non-portable.
            if (not trimmed or (stem and stem in reserved_stems) or
                    ":" in part or part.endswith((".", " "))):
                bad("entry name has a Windows-unsafe component")
            normalized.append(part)
            parent_key = "/".join(normalized).casefold()
        path = "/".join(normalized)
        if len(path.encode("utf-8")) > ARCHIVE_NAME_MAX_BYTES:
            bad("normalized entry name is too long")
        return path, unicodedata.normalize("NFC", path).casefold(), directory

    def entry_mode(info):
        mode = info.external_attr >> 16
        if not isinstance(mode, int) or mode < 0:
            bad("entry mode is invalid")
        if mode & 0o7000:
            bad("special permission bits are not supported")
        return mode, stat.S_IMODE(mode)

    if os.path.lexists(zip_path) and zip_path.is_symlink():
        bad("archive path is a symbolic link")
    try:
        archive_size = zip_path.stat().st_size
    except OSError as exc:
        raise UpdateError(f"could not read release archive: {exc}") from exc
    if not zip_path.is_file() or archive_size < 0 or archive_size > ARCHIVE_MAX_BYTES:
        bad("archive is too large or not a regular file")
    if os.path.lexists(dest_dir):
        raise UpdateError(f"refusing to replace existing extraction directory {dest_dir}")
    try:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        repo_sync.safe_destination(dest_dir.parent)
    except Exception as exc:
        raise UpdateError(f"extraction destination is unsafe: {exc}") from exc

    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                infos = zf.infolist()
            except (zipfile.BadZipFile, OSError, ValueError) as exc:
                raise UpdateError(f"the release archive is malformed: {exc}") from exc
            if len(infos) > ARCHIVE_MEMBER_MAX:
                bad("too many members")

            # Validate each local record before opening any payload. Data
            # descriptors are accepted only in their unambiguous, bounded
            # classic form and are included in the record range.
            local_ranges = []
            try:
                with open(zip_path, "rb") as archive:
                    for info in infos:
                        has_descriptor = bool(info.flag_bits & 0x08)
                        offset = info.header_offset
                        if offset < 0 or offset + 30 > archive_size:
                            bad("local record is outside the archive")
                        archive.seek(offset)
                        fixed = archive.read(30)
                        if len(fixed) != 30 or fixed[:4] != b"PK\x03\x04":
                            bad("local record is malformed")
                        (_signature, _version, local_flags, local_method,
                         _mtime, _mdate, local_crc, local_compressed,
                         local_uncompressed, name_len, extra_len) = struct.unpack(
                             "<4s5H3I2H", fixed)
                        if local_flags != info.flag_bits or local_method != info.compress_type:
                            bad("local record disagrees with its central entry")
                        raw_name = archive.read(name_len)
                        if len(raw_name) != name_len:
                            bad("local record name is truncated")
                        archive.seek(extra_len, os.SEEK_CUR)
                        try:
                            expected_name = info.orig_filename.encode(
                                "utf-8" if (info.flag_bits & 0x800) else "cp437")
                        except (AttributeError, UnicodeEncodeError):
                            bad("local record name is invalid")
                        if raw_name != expected_name:
                            bad("local record name disagrees with its central entry")
                        data_start = offset + 30 + name_len + extra_len
                        data_end = data_start + info.compress_size
                        if (data_start < offset or data_end < data_start or
                                data_end > archive_size or data_end > zf.start_dir):
                            bad("local record extends outside its archive area")
                        record_end = data_end
                        if has_descriptor:
                            # A descriptor is safe only when its exact length,
                            # signature, and all three values agree with the
                            # central directory. ZIP64 descriptors are outside
                            # the bounded archive format accepted here.
                            archive.seek(data_end)
                            descriptor = archive.read(16)
                            if len(descriptor) < 12:
                                bad("data descriptor is truncated")
                            if descriptor[:4] == b"PK\x07\x08":
                                if len(descriptor) < 16:
                                    bad("data descriptor is truncated")
                                descriptor_crc, descriptor_compressed, descriptor_uncompressed = struct.unpack(
                                    "<III", descriptor[4:16])
                                record_end += 16
                            else:
                                descriptor_crc, descriptor_compressed, descriptor_uncompressed = struct.unpack(
                                    "<III", descriptor[:12])
                                record_end += 12
                            if (descriptor_crc != info.CRC or
                                    descriptor_compressed != info.compress_size or
                                    descriptor_uncompressed != info.file_size):
                                bad("data descriptor is inconsistent")
                            if local_crc not in (0, info.CRC) or local_compressed not in (0, info.compress_size) or local_uncompressed not in (0, info.file_size):
                                bad("local data-descriptor fields are inconsistent")
                        elif (local_crc != info.CRC or
                              local_compressed != info.compress_size or
                              local_uncompressed != info.file_size):
                            bad("local record sizes or checksum are inconsistent")
                        if record_end > archive_size or record_end > zf.start_dir:
                            bad("local record extends outside its archive area")
                        local_ranges.append((offset, record_end))
            except UpdateError:
                raise
            except (OSError, struct.error, ValueError) as exc:
                raise UpdateError(f"the release archive has malformed local records: {exc}") from exc
            previous_end = -1
            for start, end in sorted(local_ranges):
                if start < previous_end:
                    bad("overlapping local records")
                previous_end = end

            members, names, symlinks = [], {}, []
            declared_total = 0
            for info in infos:
                if info.flag_bits & 0x41:  # traditional or strong ZIP encryption
                    bad("encrypted members are not supported")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    bad("unsupported compression method")
                for field in ("file_size", "compress_size", "header_offset"):
                    value = getattr(info, field, None)
                    if (isinstance(value, bool) or not isinstance(value, int) or
                            value < 0 or value > ARCHIVE_MEMBER_MAX_BYTES):
                        bad("member size or offset is invalid")
                path, collision, named_dir = archive_name(info.filename)
                raw_mode, mode = entry_mode(info)
                file_type = stat.S_IFMT(raw_mode)
                if named_dir or info.is_dir() or file_type == stat.S_IFDIR:
                    if file_type not in (0, stat.S_IFDIR) or info.file_size:
                        bad("directory metadata is invalid")
                    kind = "dir"
                elif file_type == stat.S_IFLNK:
                    kind = "symlink"
                elif file_type in (0, stat.S_IFREG):
                    kind = "file"
                else:
                    bad("unsupported special file")
                if collision in names:
                    bad("duplicate or Unicode/case-fold-colliding path")
                if kind != "dir":
                    declared_total += info.file_size
                    if declared_total > ARCHIVE_UNCOMPRESSED_MAX:
                        bad("aggregate uncompressed size exceeds the limit")
                    if info.file_size and (not info.compress_size or
                            info.file_size > info.compress_size * ARCHIVE_COMPRESSION_RATIO_MAX):
                        bad("member compression ratio exceeds the limit")
                member = {"info": info, "path": path, "kind": kind,
                          "mode": mode, "payload": None}
                names[collision] = member
                members.append(member)
                if kind == "symlink":
                    if info.file_size > ARCHIVE_SYMLINK_MAX_BYTES:
                        bad("symlink payload is too large")
                    try:
                        with zf.open(info) as source:
                            payload = source.read(ARCHIVE_SYMLINK_MAX_BYTES + 1)
                    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                        raise UpdateError(f"could not read symlink payload: {exc}") from exc
                    if len(payload) > ARCHIVE_SYMLINK_MAX_BYTES or len(payload) != info.file_size:
                        bad("symlink payload size does not match its declaration")
                    try:
                        target = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        bad("symlink payload is not UTF-8")
                    if (not target or "\x00" in target or "\\" in target or
                            target.startswith("/") or target.startswith("//") or
                            re.match(r"^[A-Za-z]:", target) or
                            any(ord(c) < 0x20 or ord(c) == 0x7f for c in target)):
                        bad("symlink target is unsafe")
                    resolved = unicodedata.normalize(
                        "NFC", posixpath.normpath(posixpath.join(posixpath.dirname(path), target)))
                    if resolved == ".." or resolved.startswith("../"):
                        bad("symlink target escapes the extraction root")
                    member["target_path"] = resolved
                    member["payload"] = target
                    symlinks.append(member)

            path_members = {m["path"]: m for m in members}
            for member in symlinks:
                # Resolve against the normalized archive spelling, not a
                # case-fold alias: a link that only works on a case-insensitive
                # host would be dangling after extraction on macOS/Linux.
                target = path_members.get(member["target_path"])
                if target is None:
                    bad("symlink target does not name a real archive member")
                member["target_member"] = target

            def resolve_link(member, chain=()):
                if member["kind"] != "symlink":
                    return member
                if member["path"] in chain:
                    bad("symlink target cycle")
                return resolve_link(member["target_member"], chain + (member["path"],))

            for member in symlinks:
                resolve_link(member)
            for member in members:
                parts = member["path"].split("/")
                for n in range(1, len(parts)):
                    parent = path_members.get("/".join(parts[:n]))
                    if parent is not None and parent["kind"] != "dir":
                        bad("member is nested beneath a non-directory")
            link_paths = {m["path"] for m in symlinks}
            if any(any(m["path"].startswith(link + "/") for link in link_paths)
                   for m in members):
                bad("member is nested beneath a symlink")

            dirs, dir_modes = set(), {}
            for member in members:
                parts = member["path"].split("/")
                dirs.update("/".join(parts[:n]) for n in range(1, len(parts)))
                if member["kind"] == "dir":
                    dirs.add(member["path"])
                    dir_modes[member["path"]] = member["mode"] or 0o755
            try:
                stage = Path(tempfile.mkdtemp(prefix=f".{dest_dir.name}.", dir=str(dest_dir.parent)))
                repo_sync.safe_destination(stage)
            except Exception as exc:
                raise UpdateError(f"could not create extraction staging directory: {exc}") from exc

            for relative in sorted(dirs, key=lambda p: (p.count("/"), p)):
                try:
                    repo_sync._secure_mkdir_relative(stage, relative, mode=0o700)
                except Exception as exc:
                    raise UpdateError(f"could not create archive directory {relative}: {exc}") from exc

            actual_total = sum(len(m["payload"]) for m in symlinks)
            if actual_total > ARCHIVE_UNCOMPRESSED_MAX:
                bad("actual extracted bytes exceed the aggregate limit")
            for member in members:
                if member["kind"] != "file":
                    continue
                info, relative = member["info"], member["path"]
                try:
                    with repo_sync._secure_open_relative(stage, relative, write=True,
                                                         create_parents=True, mode=0o600) as destination, \
                         zf.open(info) as source:
                        actual = 0
                        while True:
                            chunk = source.read(min(1 << 20, info.file_size - actual + 1))
                            if not chunk:
                                break
                            actual += len(chunk)
                            actual_total += len(chunk)
                            if actual > info.file_size or actual_total > ARCHIVE_UNCOMPRESSED_MAX:
                                bad("actual extracted bytes exceed the declared limits")
                            destination.write(chunk)
                        if actual != info.file_size:
                            bad("member ended before its declared size")
                        mode = member["mode"] or 0o644
                        try:
                            os.fchmod(destination.fileno(), mode)
                        except (AttributeError, NotImplementedError, OSError) as exc:
                            # Windows has limited chmod support. The archive
                            # mode is already restricted to ordinary bits;
                            # use the path operation only where it is safe.
                            if os.name == "nt":
                                try:
                                    os.chmod(stage / relative, mode & 0o777)
                                except (AttributeError, NotImplementedError, OSError):
                                    pass
                            elif not isinstance(exc, NotImplementedError):
                                raise
                        destination.flush()
                        os.fsync(destination.fileno())
                except UpdateError:
                    raise
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise UpdateError(f"could not extract {relative}: {exc}") from exc

            for member in symlinks:
                relative = member["path"]
                try:
                    parts = relative.split("/")
                    if getattr(repo_sync, "_DESCRIPTOR_IO", False) and os.name != "nt":
                        parent_fd = repo_sync._open_dir_chain(stage, parts[:-1], create=True)
                        try:
                            os.symlink(member["payload"], parts[-1], dir_fd=parent_fd)
                        finally:
                            os.close(parent_fd)
                    else:
                        target = repo_sync.safe_path(stage, relative)
                        if os.path.lexists(target):
                            bad("duplicate extracted path")
                        link_target = (member["payload"].replace("/", "\\")
                                       if os.name == "nt" else member["payload"])
                        os.symlink(link_target, target)
                except UpdateError:
                    raise
                except OSError as exc:
                    raise UpdateError(f"could not create archive symlink {relative}: {exc}") from exc

            top = list(stage.iterdir())
            top_names = {p.name for p in top}
            app = stage / "SCM Workbench.app"
            contents = app / "Contents"
            macos = contents / "MacOS"
            mac_ok = (top_names == {app.name} and app.is_dir() and
                      not app.is_symlink() and contents.is_dir() and
                      not contents.is_symlink() and macos.is_dir() and
                      not macos.is_symlink())
            exe = stage / "SCM Workbench.exe"
            app_dir = stage / "app"
            runtime_dir = stage / "runtime"
            windows_ok = (
                top_names == {"SCM Workbench.exe", "app", "runtime"} and
                exe.is_file() and not exe.is_symlink() and exe.stat().st_size > 0 and
                app_dir.is_dir() and not app_dir.is_symlink() and
                (app_dir / "scm_workbench").is_dir() and
                not (app_dir / "scm_workbench").is_symlink() and
                (app_dir / "ui").is_dir() and not (app_dir / "ui").is_symlink() and
                runtime_dir.is_dir() and not runtime_dir.is_symlink())
            if mac_ok == windows_ok:
                bad("archive does not have one unambiguous app shape")
            for relative, mode in dir_modes.items():
                try:
                    os.chmod(stage / relative, mode & 0o777, follow_symlinks=False)
                except NotImplementedError:
                    # The paths are controlled staging paths and have already
                    # been checked as real directories. Windows can therefore
                    # use its ordinary chmod operation when available.
                    try:
                        os.chmod(stage / relative, mode & 0o777)
                    except (AttributeError, NotImplementedError, OSError):
                        # The staging tree has no untrusted symlink path at
                        # this point; inability to restore an ordinary mode is
                        # non-fatal on platforms without no-follow chmod.
                        pass
                except OSError as exc:
                    if os.name != "nt":
                        raise UpdateError(f"could not restore mode for {relative}: {exc}") from exc

            def fsync_dir(path):
                try:
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError:
                    pass
            for directory in [stage] + [stage / p for p in dirs]:
                fsync_dir(directory)
            fsync_dir(stage.parent)
            if os.path.lexists(dest_dir):
                raise UpdateError(f"refusing to replace existing extraction directory {dest_dir}")
            try:
                # A macOS transaction's candidate is itself the .app bundle,
                # not a wrapper directory.  Publishing the app directory as
                # the token-derived sibling keeps the extraction off the data
                # volume and gives Rust the exact target shape it validates.
                source = stage / "SCM Workbench.app" if mac_ok and publish_bundle_root else stage
                _rename_noreplace(source, dest_dir)
                if source is not stage:
                    stage.rmdir()
            except FileExistsError as exc:
                raise UpdateError(f"refusing to replace existing extraction directory {dest_dir}") from exc
            except OSError as exc:
                raise UpdateError(f"could not publish extracted app atomically: {exc}") from exc
            fsync_dir(dest_dir.parent)
            stage = None
            return dest_dir if (mac_ok and publish_bundle_root) else (dest_dir / "SCM Workbench.app" if mac_ok else dest_dir)
    except UpdateError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise UpdateError(f"the release archive is malformed or could not be extracted: {exc}") from exc
    finally:
        if stage is not None:
            try:
                shutil.rmtree(stage)
            except Exception as exc:
                try:
                    log(f"    ! could not clean extraction staging directory: {exc}")
                except Exception:
                    pass


def _bounded_plist(value, *, depth=0, nodes=None):
    """Reject pathological plist structures before inspecting their fields."""
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > 2048 or depth > 16:
        raise UpdateError("the DMG Info.plist is too complex")
    if isinstance(value, dict):
        if len(value) > 256:
            raise UpdateError("the DMG Info.plist has too many keys")
        for key, child in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8")) > 1024:
                raise UpdateError("the DMG Info.plist has an invalid key")
            _bounded_plist(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise UpdateError("the DMG Info.plist has too many values")
        for child in value:
            _bounded_plist(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > 4096:
            raise UpdateError("the DMG Info.plist has an oversized value")
    elif not isinstance(value, (bytes, bytearray, int, float, bool, type(None))):
        raise UpdateError("the DMG Info.plist has an unsupported value")


def _run_fixed_helper(args, *, timeout=DMG_COMMAND_TIMEOUT,
                      output_limit=DMG_COMMAND_OUTPUT_MAX):
    """Run one fixed macOS helper with a deadline and live output caps."""
    if not isinstance(args, list) or not args or args[0] not in (HDIUTIL, CODESIGN):
        raise UpdateError("the updater helper command is not approved")
    if (isinstance(output_limit, bool) or not isinstance(output_limit, int) or
            not 1 <= output_limit <= DMG_PLIST_MAX_BYTES):
        raise UpdateError("the updater helper output limit is invalid")
    process = None
    streams = []
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    reader_error = []

    def kill_process_group():
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            try:
                process.kill()
            except OSError:
                pass

    def drain(stream, destination):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                available = output_limit - len(destination)
                if len(chunk) > available:
                    destination.extend(chunk[:max(0, available)])
                    overflow.set()
                    kill_process_group()
                    return
                destination.extend(chunk)
        except Exception as exc:
            reader_error.append(exc)
            kill_process_group()

    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   shell=False, close_fds=True, start_new_session=True)
        streams = [process.stdout, process.stderr]
        readers = [threading.Thread(target=drain, args=(stream, buffers[index]),
                                    daemon=True, name=f"macos-updater-output-{index}")
                   for index, stream in enumerate(streams)]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=max(0.1, float(timeout)))
        except subprocess.TimeoutExpired as exc:
            kill_process_group()
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise UpdateError("the macOS updater helper timed out") from exc
        finally:
            for reader in readers:
                reader.join(5)
        if any(reader.is_alive() for reader in readers):
            kill_process_group()
            raise UpdateError("the macOS updater helper output did not close")
        if reader_error:
            raise UpdateError("the macOS updater helper output could not be read")
        if overflow.is_set():
            raise UpdateError("the macOS updater helper output is too large")
        return process.returncode, bytes(buffers[0]), bytes(buffers[1])
    except UpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise UpdateError(f"the macOS updater helper could not run: {exc}") from exc
    finally:
        for stream in streams:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _dmg_mount_plist(output: bytes, mountpoint: Path) -> None:
    if len(output) > DMG_PLIST_MAX_BYTES:
        raise UpdateError("hdiutil returned an oversized mount plist")
    try:
        document = plistlib.loads(output)
    except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError) as exc:
        raise UpdateError("hdiutil returned an invalid mount plist") from exc
    _bounded_plist(document)
    entities = document.get("system-entities") if isinstance(document, dict) else None
    if not isinstance(entities, list):
        raise UpdateError("hdiutil returned no mount entities")
    mounts = [entity.get("mount-point") for entity in entities
              if isinstance(entity, dict) and "mount-point" in entity]
    try:
        if (len(mounts) != 1 or not isinstance(mounts[0], str) or
                not mounts[0] or not Path(mounts[0]).is_absolute() or
                Path(mounts[0]).resolve(strict=True) != mountpoint.resolve(strict=True)):
            raise OSError("mount path does not match the updater-owned directory")
        if not mountpoint.is_dir() or mountpoint.is_symlink():
            raise OSError("mountpoint is not a private directory")
    except (OSError, RuntimeError) as exc:
        raise UpdateError(f"hdiutil mounted the DMG at an unexpected path: {exc}") from exc


def _attach_dmg(dmg: Path, mountpoint: Path) -> bytes:
    args = [HDIUTIL, "attach", "-plist", "-readonly", "-noautoopen",
            "-nobrowse", "-mountpoint", str(mountpoint), str(dmg)]
    status, stdout, _stderr = _run_fixed_helper(args, output_limit=DMG_PLIST_MAX_BYTES)
    if status != 0:
        # hdiutil can return a failure after creating a mount. Detaching the
        # updater-owned path is safe even when no mount was established and
        # prevents a partial attach from surviving the failed update.
        try:
            _detach_dmg(mountpoint)
        except UpdateError:
            pass
        raise UpdateError("hdiutil could not attach the update DMG")
    return stdout


def _detach_dmg(mountpoint: Path) -> None:
    args = [HDIUTIL, "detach", str(mountpoint)]
    status, _stdout, _stderr = _run_fixed_helper(args)
    if status != 0:
        raise UpdateError("hdiutil could not detach the update DMG")


def _scan_dmg_tree(app: Path) -> dict:
    """Scan an app without following links, returning a bounded safe manifest."""
    try:
        root_stat = app.lstat()
    except OSError as exc:
        raise UpdateError(f"could not inspect the mounted app: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise UpdateError("the mounted SCM Workbench.app is not a real directory")
    records = {"": {"kind": "dir", "mode": stat.S_IMODE(root_stat.st_mode)}}
    total = [0]
    def visit(directory: Path, parent: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise UpdateError(f"could not read the mounted app: {exc}") from exc
        if len(entries) > DMG_TREE_MAX_ENTRIES:
            raise UpdateError("the mounted app contains too many entries")
        aliases = {}
        for entry in sorted(entries, key=lambda item: item.name):
            name = unicodedata.normalize("NFC", entry.name)
            try:
                encoded = name.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise UpdateError("the mounted app contains invalid UTF-8") from exc
            stem = name.rstrip(". ").split(".", 1)[0].casefold()
            if (not name or name in (".", "..") or "\\" in name or "/" in name or
                    any(ord(char) < 0x20 or ord(char) == 0x7f for char in name) or
                    len(encoded) > DMG_TREE_MAX_COMPONENT_BYTES or name.endswith((".", " ")) or
                    ":" in name or stem in {"con", "prn", "aux", "nul"} or
                    (stem.startswith("com") and stem[3:].isdigit()) or
                    (stem.startswith("lpt") and stem[3:].isdigit())):
                raise UpdateError("the mounted app contains an unsafe path component")
            folded = name.casefold()
            if folded in aliases:
                raise UpdateError("the mounted app contains a case-fold/NFC collision")
            aliases[folded] = name
            relative = f"{parent}/{name}" if parent else name
            if len(relative.encode("utf-8")) > DMG_TREE_MAX_PATH_BYTES:
                raise UpdateError("the mounted app contains an oversized path")
            path = directory / entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise UpdateError("could not inspect a mounted app entry") from exc
            mode = info.st_mode
            kind = stat.S_IFMT(mode)
            if stat.S_ISDIR(mode):
                records[relative] = {"kind": "dir", "mode": stat.S_IMODE(mode)}
                visit(path, relative)
            elif stat.S_ISREG(mode):
                if info.st_size < 0 or info.st_size > DMG_TREE_MAX_BYTES:
                    raise UpdateError("the mounted app contains an oversized file")
                total[0] += info.st_size
                if total[0] > DMG_TREE_MAX_BYTES:
                    raise UpdateError("the mounted app is too large")
                records[relative] = {"kind": "file", "mode": stat.S_IMODE(mode),
                                     "size": info.st_size, "path": path}
            elif stat.S_ISLNK(mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise UpdateError("could not read a mounted app symlink") from exc
                if (not isinstance(target, str) or not target or "\\" in target or
                        target.startswith("/") or target.startswith("//") or
                        re.match(r"^[A-Za-z]:", target) or "\x00" in target or
                        any(ord(char) < 0x20 or ord(char) == 0x7f for char in target) or
                        len(target.encode("utf-8")) > DMG_TREE_MAX_PATH_BYTES):
                    raise UpdateError("the mounted app contains an unsafe symlink")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
                if resolved == ".." or resolved.startswith("../"):
                    raise UpdateError("the mounted app contains an escaping symlink")
                records[relative] = {"kind": "symlink", "mode": stat.S_IMODE(mode),
                                     "target": target, "resolved": resolved}
            else:
                raise UpdateError("the mounted app contains a special file")
            if stat.S_IMODE(mode) & 0o7000:
                raise UpdateError("the mounted app contains unsupported permission bits")
            if len(records) > DMG_TREE_MAX_ENTRIES:
                raise UpdateError("the mounted app contains too many entries")
    visit(app, "")
    for relative, record in records.items():
        if record["kind"] == "symlink":
            current = record["resolved"]
            seen = {relative}
            while True:
                if current in seen:
                    raise UpdateError("the mounted app contains a symlink cycle")
                seen.add(current)
                target = records.get(current)
                if target is None:
                    raise UpdateError("the mounted app contains a dangling symlink")
                if target["kind"] != "symlink":
                    break
                current = target["resolved"]
    return records


def _dmg_bundle_version(app: Path, records: dict, expected_version: str | None) -> None:
    required = ("Contents", "Contents/MacOS", "Contents/MacOS/SCM Workbench",
                "Contents/Info.plist", "Contents/app", "Contents/app/scm_workbench",
                "Contents/app/ui", "Contents/runtime")
    for relative in required:
        record = records.get(relative)
        if record is None or (relative.endswith(("Contents", "MacOS", "app",
                                                 "scm_workbench", "ui", "runtime")) and
                              record["kind"] != "dir"):
            raise UpdateError("the mounted app has an incomplete SCM Workbench.app shape")
    executable = records["Contents/MacOS/SCM Workbench"]
    if executable["kind"] != "file" or not executable["size"] or not (executable["mode"] & 0o111):
        raise UpdateError("the mounted app executable is invalid")
    plist_record = records["Contents/Info.plist"]
    if plist_record["kind"] != "file" or plist_record["size"] > DMG_PLIST_MAX_FILE_BYTES:
        raise UpdateError("the mounted app Info.plist is invalid")
    source_fd = None
    try:
        source_fd = os.open(plist_record["path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != plist_record["size"]:
            raise UpdateError("the mounted app Info.plist changed while reading")
        payload = b""
        while len(payload) <= DMG_PLIST_MAX_FILE_BYTES:
            chunk = os.read(source_fd, DMG_PLIST_MAX_FILE_BYTES + 1 - len(payload))
            if not chunk:
                break
            payload += chunk
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError("could not read the mounted app Info.plist") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
    if len(payload) > DMG_PLIST_MAX_FILE_BYTES:
        raise UpdateError("the mounted app Info.plist is too large")
    try:
        document = plistlib.loads(payload)
    except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError) as exc:
        raise UpdateError("the mounted app Info.plist is invalid") from exc
    _bounded_plist(document)
    if not isinstance(document, dict) or document.get("CFBundleIdentifier") != "com.mallen.scmworkbench" or \
            document.get("CFBundleExecutable") != "SCM Workbench" or \
            document.get("CFBundlePackageType") != "APPL" or \
            any(field in document and document[field] != APP_NAME
                for field in ("CFBundleName", "CFBundleDisplayName")):
        raise UpdateError("the mounted app identity is not SCM Workbench")
    if expected_version is not None:
        version = canonical_version(document.get("CFBundleShortVersionString"))
        expected = canonical_version(expected_version)
        if version is None or expected is None or version != expected:
            raise UpdateError("the mounted app version does not match the release")


def _copy_dmg_tree(source: Path, stage: Path, records: dict) -> None:
    from scm_workbench import repo_sync
    for relative, record in sorted(records.items(), key=lambda item: (item[0].count("/"), item[0])):
        if not relative:
            continue
        if record["kind"] == "dir":
            repo_sync._secure_mkdir_relative(stage, relative, mode=0o700)
            continue
        if record["kind"] == "symlink":
            target = record["target"]
            target_path = repo_sync.safe_path(stage, relative)
            if os.path.lexists(target_path):
                raise UpdateError("the staged app contains a duplicate path")
            os.symlink(target.replace("/", "\\") if os.name == "nt" else target, target_path)
            continue
        destination = None
        source_path = record["path"]
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source_path, flags)
            try:
                source_stat = os.fstat(source_fd)
                if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != record["size"]:
                    raise UpdateError("a mounted app file changed while copying")
                destination = repo_sync._secure_open_relative(stage, relative, write=True,
                                                              create_parents=True, mode=0o600)
                remaining = record["size"]
                while remaining:
                    chunk = os.read(source_fd, min(1 << 20, remaining))
                    if not chunk:
                        raise UpdateError("a mounted app file ended while copying")
                    destination.write(chunk)
                    remaining -= len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
                try:
                    os.fchmod(destination.fileno(), record["mode"] or 0o644)
                except (AttributeError, NotImplementedError):
                    pass
            finally:
                os.close(source_fd)
        except UpdateError:
            raise
        except (OSError, ValueError) as exc:
            raise UpdateError(f"could not safely copy mounted app file {relative}: {exc}") from exc
        finally:
            if destination is not None:
                destination.close()
    # Restore directory modes after their children have been created. The
    # private staging defaults stay restrictive until the complete tree is in
    # place, and no chmod operation follows a symlink.
    for relative, record in sorted(records.items(), key=lambda item: item[0].count("/"),
                                   reverse=True):
        if record["kind"] != "dir":
            continue
        path = stage if not relative else stage / relative
        try:
            os.chmod(path, record["mode"] or 0o755, follow_symlinks=False)
        except (AttributeError, NotImplementedError):
            os.chmod(path, record["mode"] or 0o755)
        except OSError as exc:
            raise UpdateError(f"could not restore mounted app directory mode {relative}: {exc}") from exc


def _verify_macos_candidate(candidate: Path, expected_version: str) -> None:
    records = _scan_dmg_tree(candidate)
    _dmg_bundle_version(candidate, records, expected_version)
    status, _stdout, _stderr = _run_fixed_helper(
        [CODESIGN, "--verify", "--deep", "--strict", str(candidate)])
    if status != 0:
        raise UpdateError("the update app failed macOS code-signature verification")


def prepare_dmg(dmg: Path, candidate: Path, expected_version: str, log=print) -> Path:
    """Mount, validate, copy, detach, and publish one untrusted DMG."""
    if sys.platform != "darwin":
        raise UpdateError("DMG updates are supported only on macOS")
    from scm_workbench import repo_sync
    dmg, candidate = Path(dmg), Path(candidate)
    if not re.fullmatch(r"\.SCM-Workbench-candidate-[0-9a-f]{64}", candidate.name):
        raise UpdateError("the DMG candidate is not token-bound")
    try:
        if dmg.is_symlink() or not dmg.is_file():
            raise OSError("DMG is not a regular file")
    except OSError as exc:
        raise UpdateError(f"the update DMG is unsafe: {exc}") from exc
    work = dmg.parent
    mount = Path(tempfile.mkdtemp(prefix=".scm-workbench-mount-", dir=str(work)))
    os.chmod(mount, 0o700)
    stage = None
    mounted = False
    try:
        mount_output = _attach_dmg(dmg, mount)
        # A successful attach may have mounted the image even when its plist
        # is malformed. Mark it before parsing so the finally block always
        # attempts the matching detach.
        mounted = True
        _dmg_mount_plist(mount_output, mount)
        apps = []
        try:
            entries = list(os.scandir(mount))
        except OSError as exc:
            raise UpdateError(f"could not read the mounted update DMG: {exc}") from exc
        for entry in entries:
            if entry.name == APP_NAME + ".app":
                apps.append(Path(entry.path))
            elif entry.name.casefold().endswith(".app"):
                raise UpdateError("the update DMG contains an unexpected app bundle")
        if len(apps) != 1:
            raise UpdateError("the update DMG does not contain one SCM Workbench.app")
        source = apps[0]
        records = _scan_dmg_tree(source)
        _dmg_bundle_version(source, records, expected_version)
        if candidate.exists() or candidate.is_symlink():
            raise UpdateError("the update candidate path already exists")
        stage = Path(tempfile.mkdtemp(prefix=f".{candidate.name}-", dir=str(candidate.parent)))
        repo_sync.safe_destination(stage)
        _copy_dmg_tree(source, stage, records)
        log("    copied and validated the mounted app")
    finally:
        detach_error = None
        if mounted:
            try:
                _detach_dmg(mount)
            except Exception as exc:
                detach_error = exc
        try:
            mount.rmdir()
        except OSError:
            pass
        if detach_error is not None:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
                stage = None
            raise detach_error
    published = False
    try:
        _rename_noreplace(stage, candidate)
        stage = None
        published = True
        _verify_macos_candidate(candidate, expected_version)
        _fsync_dir(candidate.parent)
        return candidate
    except Exception:
        if published and candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate, ignore_errors=True)
        raise
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def prepare_asset(asset: dict, downloaded: Path, candidate: Path,
                  expected_version: str, log=print) -> Path:
    """Prepare an exact validated asset without leaking format logic to jobs."""
    name = asset.get("name") if isinstance(asset, dict) else None
    if name == MACOS_DMG_ASSET:
        return prepare_dmg(downloaded, candidate, expected_version, log=log)
    if name == WINDOWS_ASSET:
        return extract_app(downloaded, candidate, log=log, publish_bundle_root=True)
    raise UpdateError("the release asset name is not supported")


def swap_bundle(new_bundle: Path, old_bundle: Path, log=print) -> Path:
    """Replace old_bundle with new_bundle, retaining a rollback sibling."""
    parent = old_bundle.parent
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = parent / (old_bundle.name + f".old-{stamp}")
    os.replace(str(old_bundle), str(backup))
    try:
        shutil.move(str(new_bundle), str(old_bundle))
    except BaseException:
        try:
            os.replace(str(backup), str(old_bundle))
        except Exception:
            log(f"    ! could not roll the swap back — the old app is at {backup}")
        raise
    return backup


def _ancestors_to_stop() -> list:
    """The window-hosting process(es) above us — on both platforms the chain
    is exactly two deep: the bundle's launcher runs the UI *in* the stub/exe
    process, and the server (us) is the child it spawned. Killing the parent
    closes the window; we stay up to finish the install."""
    p = os.getppid()
    return [] if p <= 1 else [p]


def stop_ancestors(log=print) -> None:
    """Stop the window host (never the data — that lives in the data area)."""
    for p in _ancestors_to_stop():
        try:
            if os.name == "nt":
                # taskkill is a console app: CREATE_NO_WINDOW, no flash
                subprocess.Popen(["taskkill", "/F", "/PID", str(p)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=0x08000000)
            else:
                os.kill(p, 15)  # SIGTERM — the window host quits cleanly
            log(f"    (stopped the window host, pid {p})")
        except Exception:
            pass
    time.sleep(1.2)


def relaunch_detached(bundle: Path, log=print, delay: float = 2.5) -> None:
    """(Re)start the app a moment after this process has gone — a detached
    one-liner is the parent-agnostic way: the new instance wants the port
    the old one (us) still holds, so the relaunch must land after our exit.

    macOS note: the swapped bundle is re-signed with a fresh ad-hoc seal, so
    to Gatekeeper the relaunch looks like a *different, never-seen* app - the
    launch is blocked behind the "SCM Workbench cannot be opened" entry in
    System Settings → Privacy & Security until the user clicks **Open
    Anyway** there (once, remembered for this build). If a local-network
    prompt shows too (it can, for the same reason), that answer is one
    click as well. The finish message below names both."""
    if sys.platform == "darwin":
        cmd = ["/bin/sh", "-c", f"sleep {delay:.0f} && open '{bundle}'"]
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        exe = next((p for p in bundle.iterdir() if p.suffix.lower() == ".exe"), None)
        target = exe or bundle
        cmd = ["cmd", "/c", f"timeout /t {int(delay)} >nul && start '' \"{target}\""]
        DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, creationflags=DETACHED,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(bundle.parent if target == bundle else target.parent))
    else:
        raise UpdateError("automatic relaunch is only supported on the packaged macOS/Windows apps")


def clean_old_bundles(bundle: Path, log=print) -> None:
    """Drop `.old-<stamp>` backup bundles left by previous updates (called at
    launch: by then the new app is provably healthy enough to start)."""
    try:
        parent = bundle.parent
        for old in parent.glob(bundle.name + ".old-*"):
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
                log(f"(removed the previous version kept at {old.name})")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# The install job (run in a server thread; `job` is the server's job dict)
# ----------------------------------------------------------------------------

JOURNAL_LIMIT = 64 * 1024
UPDATE_SCHEMA_VERSION = 1
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_install_bundle(path: Path) -> bool:
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
                    directory(path / "Contents/app/ui") and directory(path / "Contents/runtime"))
        return (os.name == "nt" and directory(path) and
                regular(path / "SCM Workbench.exe") and
                directory(path / "app/scm_workbench") and directory(path / "app/ui") and
                directory(path / "runtime"))
    except (OSError, RuntimeError):
        return False


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_update_file(path: Path, value: bytes, *, limit: int = JOURNAL_LIMIT) -> None:
    if len(value) > limit:
        raise UpdateError("update handoff record exceeds 64 KiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise UpdateError(f"refusing to replace symlinked {path.name}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _write_handoff_records(data: Path, token: str, expected: str, target: Path,
                           shell_pid: int, worker_pid: int) -> None:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise UpdateError("invalid update handoff token")
    expected = canonical_version(expected)
    if expected is None or len(expected.encode("utf-8")) > 128:
        raise UpdateError("invalid expected update version")
    if int(shell_pid) <= 0 or int(worker_pid) <= 0:
        raise UpdateError("invalid update process identity")
    parent = target.parent
    journal = {
        "version": UPDATE_SCHEMA_VERSION,
        "token": token,
        "phase": "prepared",
        "expected_version": expected,
        "target": str(target),
        "candidate_name": f".SCM-Workbench-candidate-{token}",
        "backup_name": f".SCM-Workbench-backup-{token}",
        "old_shell_pid": int(shell_pid),
        "old_worker_pid": int(worker_pid),
    }
    encoded = json.dumps(journal, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _atomic_update_file(data / ".update-journal.json", encoded)
    request = {"version": UPDATE_SCHEMA_VERSION, "token": token}
    _atomic_update_file(data / ".update-launch-request.json",
                        json.dumps(request, separators=(",", ":")).encode("ascii"),
                        limit=4 * 1024)


def _remove_handoff_files(data: Path, candidate: Path) -> None:
    """Remove only this transaction's files; failure is reported to caller."""
    for path in (data / ".update-launch-request.json", data / ".update-journal.json"):
        if path.is_symlink():
            raise UpdateError(f"refusing to remove symlinked {path.name}")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if candidate.exists() or candidate.is_symlink():
        if candidate.is_symlink() or not candidate.is_dir():
            raise UpdateError("candidate extraction is not a directory")
        shutil.rmtree(candidate)
    _fsync_dir(data)


def _begin_handoff(job: dict, plan: dict, token: str, target: Path,
                   candidate: Path) -> None:
    """Commit the handoff fence and its exact durable records atomically."""
    from scm_workbench import server
    with server.JOBS_LOCK:
        others = [j for j in server.JOBS.values()
                  if j is not job and j.get("status") == "running"]
        if others:
            raise UpdateError("another job is still running; the update was not handed off")
        expected = canonical_version(plan.get("latest") or "")
        if expected is None:
            raise UpdateError("the release tag is not a valid version")
        server._UPDATE_QUIESCING = True
        server._UPDATE_QUIESCING_JOB = job.get("id")
        job["status"] = "handoff"
        job["update_token"] = token
        job["expected_version"] = expected
        # RLock makes the durable publication part of the same admission
        # critical section: no ordinary job can slip in between the status and
        # the journal/request records.
        try:
            server._persist_jobs(strict=True)
            _write_handoff_records(server.DATA_DIR, token, job["expected_version"], target,
                                   os.getppid(), os.getpid())
        except Exception:
            try:
                _remove_handoff_files(server.DATA_DIR, candidate)
            except Exception:
                pass
            job["status"] = "running"
            job.pop("update_token", None)
            job.pop("expected_version", None)
            server._UPDATE_QUIESCING = False
            server._UPDATE_QUIESCING_JOB = None
            server._persist_jobs()
            raise


def run_job(job: dict, plan: dict, log_f) -> None:
    """Download + install a newer release, then hand over to the new app.

    plan keys: repo, current, latest, asset {name,url,size},
    bundle (the app folder to replace, None when not packaged), and work (the
    scratch directory).
    """
    def emit(s: str) -> None:
        job["log_lines"].append(s)
        log_f.write(s + "\n")
        log_f.flush()
        for q in list(job["subs"]):
            try:
                q.put(("line", s))
            except Exception:
                pass

    def fail(msg: str) -> None:
        emit(f"    ! {msg}")
        job["status"] = "fail"
        job["exit_code"] = 1
        job["ended"] = time.time()
        job["duration"] = round(job["ended"] - job["started"], 2)
        for q in list(job["subs"]):
            try:
                q.put(("done", "fail", 1))
            except Exception:
                pass

    def finish(ok: bool, final: str) -> None:
        emit(final)
        job["status"] = "ok" if ok else "fail"
        job["exit_code"] = 0 if ok else 1
        job["ended"] = time.time()
        job["duration"] = round(job["ended"] - job["started"], 2)
        for q in list(job["subs"]):
            try:
                q.put(("done", job["status"], job["exit_code"]))
            except Exception:
                pass

    candidate = None
    handoff_committed = False
    try:
        # the UI's progress strip reads job["progress"]; until the first
        # stage sets it, the bar shows indeterminate so a slow first byte
        # never reads as "nothing is happening".
        job["progress"] = {"stage": "fetch", "done": 0, "total": 0}
        emit(f"Update to {plan.get('latest') or 'the latest release'} — repo {plan.get('repo')}")
        # 1) re-verify (the state that started the job can be a few minutes old)
        rel = latest_release()
        same_release = (rel["tag"].lstrip("v") == plan.get("current"))
        if same_release or not is_newer(rel["tag"], plan.get("current")):
            if same_release:
                # The running app *is* the newest release (a check that ran
                # while the release it found was still unpublished, or a
                # manual re-check): the check side shows this as
                # "update-available" - the user's button press starts the
                # install, so this re-verification must not dead-end it with
                # "Nothing to do" (nor re-save "up-to-date" over the
                # update-available state, which would flip the card back to a
                # stuck 24-hour "Up to date" on the next render).
                finish(True, f"You're already on the newest release ({rel['tag']}) - nothing to install.")
                return
            finish(True, f"Nothing to do — v{plan.get('current')} is the latest release ({rel['tag']}).")
            return
        # 2) the right exact asset. Never fall back to the stale state asset:
        # the reverified release and its tag must supply the install metadata.
        try:
            asset = pick_asset({"tag": rel["tag"], "assets": rel["assets"]})
            if asset.get("tag") != rel["tag"]:
                raise UpdateError("the release asset is not bound to the release tag")
        except (KeyError, TypeError, UpdateError) as exc:
            fail(f"no unambiguous installable asset is attached to the newest release ({exc})")
            return
        size_mb = asset.get("size", 0) / 1e6
        emit(f"Downloading {asset['name']} ({size_mb:.0f} MB) from the release …")
        # 3) download (progress line, throttled to ~1/s)
        work = Path(plan["work"]); work.mkdir(parents=True, exist_ok=True)
        dest = work / asset["name"]
        last = {"t": 0.0}

        def progress(done: int, total: int) -> None:
            # total=0 means the CDN omitted Content-Length. Publish that shape
            # immediately so the client deliberately stays indeterminate.
            job["progress"] = {"stage": "download", "done": done, "total": total}
            if total and (time.time() - last["t"]) > 1.0:
                last["t"] = time.time()
                # raw numbers only: the UI derives the speed/eta line the way
                # the repo-prep bar does (it needs the timestamps, which only
                # the client side has)
                emit(f"    ↓ {done / 1e6:.1f} / {total / 1e6:.1f} MB")
        download(asset["url"], dest, progress=progress, expected_asset=asset)
        emit(f"    downloaded {dest.stat().st_size / 1e6:.1f} MB")
        bundle = plan.get("bundle")
        if not bundle or not _is_install_bundle(Path(bundle)):
            fail("automatic updates require a complete packaged app; development checkouts are not installable")
            return
        old_bundle = Path(bundle).resolve(strict=True)
        if not old_bundle.is_dir():
            fail(f"the current app folder is not a directory ({old_bundle})")
            return
        # 4) Prepare the exact asset through one format facade. DMGs are
        # mounted and copied away before detach; ZIPs retain the legacy path.
        # There is no staging directory in the data area and no legacy swap path.
        token = secrets.token_hex(32)
        candidate = old_bundle.parent / f".SCM-Workbench-candidate-{token}"
        if candidate.exists() or candidate.is_symlink():
            raise UpdateError("the update candidate path already exists")
        job["progress"] = {"stage": "extract", "done": 0, "total": 0}
        emit("Extracting the new app …")
        new_bundle = prepare_asset(asset, dest, candidate, rel["tag"], log=emit)
        if Path(new_bundle).resolve() != candidate.resolve():
            raise UpdateError("extraction did not publish the token-bound candidate")
        emit(f"    ready: {new_bundle}")
        # 5) Persist the handoff fence and request while holding the same lock
        # ordinary jobs use for admission.  From this point the old process
        # never swaps, relaunches, stops ancestors, or reports success.
        # The native shell keeps the old app open for a bounded eight-second
        # grace period after helper ownership is acknowledged. Publish the
        # matching wall-clock deadline before the durable handoff so Simple
        # mode can render a countdown rather than disappearing abruptly.
        job["progress"] = {"stage": "handoff", "done": 0, "total": 0,
                           "restart_at": time.time() + UPDATE_RESTART_GRACE_SECONDS}
        emit("Handing the candidate to the native updater …")
        _begin_handoff(job, plan, token, old_bundle, candidate)
        handoff_committed = True
        emit("    handoff durable — the native helper will restart the app and verify its health")
        return
    except UpdateError as e:
        if handoff_committed:
            return
        if candidate is not None and not handoff_committed:
            try:
                if candidate.exists() or candidate.is_symlink():
                    if candidate.is_symlink() or not candidate.is_dir():
                        candidate.unlink()
                    else:
                        shutil.rmtree(candidate)
                    _fsync_dir(candidate.parent)
            except Exception as cleanup_error:
                emit(f"    ! could not clean update candidate: {cleanup_error}")
        fail(str(e))
    except Exception as e:
        if handoff_committed:
            return
        if candidate is not None:
            try:
                if candidate.exists() or candidate.is_symlink():
                    if candidate.is_symlink() or not candidate.is_dir():
                        candidate.unlink()
                    else:
                        shutil.rmtree(candidate)
                    _fsync_dir(candidate.parent)
            except Exception:
                pass
        import traceback
        emit("    " + traceback.format_exc(limit=3).replace("\n", "\n    "))
        fail(f"the update failed: {e}")
