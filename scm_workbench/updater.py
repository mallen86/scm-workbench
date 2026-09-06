#!/usr/bin/env python3
"""
updater.py — check for, and install, newer versions of the SCM Workbench app.

The app is packaged with Briefcase and shipped as a GitHub *release* zip
(macOS: "SCM Workbench.app", Windows: "SCM Workbench.exe" + src/). This
module talks to the releases of the Workbench's own repository:

  * fetch the newest release (the release repo is private, so the check
    can't see it until the repo is made public - no credentials anywhere
    in the meantime),
  * compare it with the running version,
  * on "update available" an in-process job downloads the right platform's
    zip, extracts it, swaps it over the current app folder, relaunches the
    new one and quits the old one.

The swap only touches the *app* folder; the data area (settings, job
history, managed repo copies, the private runtime) lives elsewhere and is
never part of the swap, so an update can never lose user data.

Standard library only — same rule as the rest of the Workbench.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
    """Select exactly the supported archive for one supported architecture."""
    platform = platform or (sys.platform if os.name != "nt" else "win32")
    if platform in ("darwin", "macos", "darwin-arm64", "macos-arm64"):
        expected = "scm-workbench-macos.zip"
        label = "darwin arm64"
    elif platform in ("win32", "windows", "windows-x64", "win64"):
        expected = "scm-workbench-windows.zip"
        label = "windows x64"
    else:
        raise UpdateError(f"the release has no installable archive for {platform}")
    assets = release.get("assets") if isinstance(release, dict) else None
    matches = [a for a in (assets or []) if isinstance(a, dict) and a.get("name") == expected]
    if len(matches) != 1:
        names = ", ".join(str(a.get("name", "")) for a in (assets or []) if isinstance(a, dict)) or "none"
        raise UpdateError(f"the release has no unambiguous {label} zip to install (assets: {names})")
    return matches[0]


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
        if progress is not None:
            progress(0, expected_size)
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
                    progress(done, expected_size)
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


def extract_app(zip_path: Path, dest_dir: Path, log=print) -> Path:
    """Unzip a release into dest_dir and return the app folder it contains.

    macOS releases hold "SCM Workbench.app"; Windows releases hold
    "SCM Workbench.exe" (+ src/) directly at the archive top level — the
    returned path is the folder to swap in place of the current one.

    The extract is symlink-aware, on purpose: `zipfile.extractall` *dereferences*
    zip symlinks (the pbs runtime's bin/python → python3.13, the pkgconfig
    aliases, the libpython version link, …) into small regular files holding
    the target's *text*. The ad-hoc signature seals those entries **as links**,
    so a dereferenced extract is a bundle that passes no seal and refuses to
    launch on Apple silicon — and unlike a Gatekeeper gate, that refusal is
    silent. (The pipeline's own `zip -y` exists for exactly this reason in
    the other direction: to keep the links *inside* the archive.)

    The same extract is mode-restoring: BSD `zip` records each entry's Unix
    mode in its external-attr field, but Python's `extractall` ignores that
    field and writes every file `0644` (the `unzip`/`ditto` CLIs do it
    right - the same 0755 `python3.13` comes out of `extractall` as 0644).
    A bundle whose main executable is not executable is a *spawn* failure on
    the user's machine - `RBSRequestError 5` / `POSIX 111`, no dialog - so
    each member is written through the documented extract-to-tmp-then-chmod
    path instead, restoring the mode the archive's own metadata claims.
    """
    import stat

    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        # path-traversal guard, same policy as the repo sync
        infos = zf.infolist()
        for i in infos:
            if i.filename.startswith("/") or ".." in i.filename.split("/"):
                raise UpdateError(f"the release archive has an unsafe entry ({i.filename}) — not installing it")

        def _extract(i) -> None:
            target = dest_dir / i.filename
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                return
            # extract to a sibling tmp name, then chmod to the mode the zip
            # metadata claims (its low 12 bits), then rename into place -
            # the workaround CPython itself documents, because extractall
            # never applies the stored mode
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".wbtmp")
            with zf.open(i) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            attr = i.external_attr >> 16
            mode = stat.S_IMODE(attr) if attr else 0o644
            os.chmod(tmp, 0o777 & mode)
            os.rename(tmp, target)
        for i in infos:
            _extract(i)
        # restore the links extractall flattened (idempotent: a non-link
        # entry's external_attr carries no S_IFLNK, so nothing moves)
        fixed = 0
        for i in zf.infolist():
            if i.is_dir():
                continue
            mode = (i.external_attr >> 16) & 0o77777777
            if not stat.S_ISLNK(mode):
                continue
            target = zf.read(i).decode("utf-8", "replace").rstrip("\n")
            if not target:
                continue
            p = dest_dir / i.filename
            try:
                # a previous (pre-fix) extract may have left the flattened
                # text file where the link belongs: remove it, then link
                if p.is_symlink():
                    if os.readlink(p) == target:
                        continue
                    p.unlink()
                elif p.is_file():
                    p.unlink()
                elif p.exists():
                    continue  # a directory sits where a link should be — leave it
                os.symlink(target, p)
                fixed += 1
            except OSError as e:
                log(f"    ! could not restore the link {i.filename.split('/')[-1]}: {e}")
        if fixed:
            log(f"    (restored {fixed} symlink{'s' if fixed != 1 else ''} the plain extract had flattened)")

    top = sorted(p for p in dest_dir.iterdir() if not p.name.startswith("."))
    # 1) the normal shape: the bundle itself at the archive top level
    for p in top:
        if p.is_dir() and p.name.endswith(".app"):
            if not (p / "Contents" / "MacOS").is_dir():
                raise UpdateError(f"the archive’s “{p.name}” is not a macOS app bundle")
            return p
    for p in top:
        if p.is_file() and p.suffix.lower() == ".exe":
            if not (dest_dir / "src").is_dir():
                raise UpdateError(f"the archive’s “{p.name}” is missing its src/ runtime — not installing it")
            return dest_dir
    # 2) a flat archive (GitHub-tarball style: the bundle's contents at top level)
    if (dest_dir / "Contents" / "MacOS").is_dir():
        return dest_dir
    raise UpdateError("could not find the app inside the release archive")


def swap_bundle(new_bundle: Path, old_bundle: Path, log=print) -> Path:
    """Replace old_bundle with new_bundle (same parent), keeping the old one
    as a `.old-<stamp>` sibling until the next launch sweeps it. Returns the
    backup path. Rolls the move back if the second one fails."""
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

def run_job(job: dict, plan: dict, log_f) -> None:
    """Download + install a newer release, then hand over to the new app.

    plan keys: repo, current, latest, asset {name,url,size},
    bundle (the app folder to replace, None when not packaged), work (the
    scratch dir), force (allow same-version reinstalls).
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

    try:
        # the UI's progress strip reads job["progress"]; until the first
        # stage sets it, the bar shows indeterminate so a slow first byte
        # never reads as "nothing is happening".
        job["progress"] = {"stage": "fetch", "done": 0, "total": 0}
        emit(f"Update to {plan.get('latest') or 'the latest release'} — repo {plan.get('repo')}")
        # 1) re-verify (the state that started the job can be a few minutes old)
        rel = latest_release()
        same_release = (rel["tag"].lstrip("v") == plan.get("current"))
        if (same_release or not is_newer(rel["tag"], plan.get("current"))) and not plan.get("force"):
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
        # 2) the right zip.  Never fall back to the stale state asset: the
        # reverified release and its tag must supply the install metadata.
        try:
            asset = pick_asset({"tag": rel["tag"], "assets": rel["assets"]})
            if asset.get("tag") != rel["tag"]:
                raise UpdateError("the release asset is not bound to the release tag")
        except (KeyError, TypeError, UpdateError) as exc:
            fail(f"no installable zip is attached to the newest release ({exc})")
            return
        size_mb = asset.get("size", 0) / 1e6
        emit(f"Downloading {asset['name']} ({size_mb:.0f} MB) from the release …")
        # 3) download (progress line, throttled to ~1/s)
        work = Path(plan["work"]); work.mkdir(parents=True, exist_ok=True)
        dest = work / asset["name"]
        last = {"t": 0.0}

        def progress(done: int, total: int) -> None:
            if total and (time.time() - last["t"]) > 1.0:
                last["t"] = time.time()
                # raw numbers only: the UI derives the speed/eta line the way
                # the repo-prep bar does (it needs the timestamps, which only
                # the client side has)
                job["progress"] = {"stage": "download", "done": done, "total": total}
                emit(f"    ↓ {done / 1e6:.1f} / {total / 1e6:.1f} MB")
        download(asset["url"], dest, progress=progress, expected_asset=asset)
        emit(f"    downloaded {dest.stat().st_size / 1e6:.1f} MB")
        # 4) extract + verify (indeterminate: a zip of this shape has no
        # cheap per-file counter, and the stage label is the information)
        job["progress"] = {"stage": "extract", "done": 0, "total": 0}
        emit("Extracting the new app …")
        new_bundle = extract_app(dest, work / "staging", log=emit)
        emit(f"    ready: {new_bundle}")
        # 5) no bundle of our own (a dev run) — hand the files over instead
        bundle = plan.get("bundle")
        if not bundle:
            finish(True,
                   "Done — the new build is unpacked at\n    "
                   f"    {new_bundle}\n"
                   "    This server has no app folder to swap (running from a source\n"
                   "    checkout), so move/copy it over your existing install manually.")
            return
        old_bundle = Path(bundle)
        if not old_bundle.exists():
            fail(f"the current app folder is gone ({old_bundle}) — not swapping")
            return
        # 6) the swap. The data area is a sibling of all this, never inside it.
        job["progress"] = {"stage": "install", "done": 0, "total": 0}
        emit(f"Installing over {old_bundle} — the app will close and reopen by itself in a few seconds.\n"
             "    (Your data folder is not part of the app folder and stays as-is.)")
        backup = swap_bundle(new_bundle, old_bundle, log=emit)
        emit(f"    swapped — the previous version is kept at “{backup.name}” until the next launch")
        # 7) relaunch the new one (detached, lands after we exit), then quit the
        #    window host and ourselves — in that order on purpose.
        relaunch_detached(old_bundle, log=emit)
        stop_ancestors(log=emit)
        if sys.platform == "darwin":
            finish(True,
                   "New version is starting. One thing to expect on macOS: the update re-signs\n"
                   "    the app in place, so the relaunched copy reads to the system as brand new.\n"
                   "    - If it is refused (\\u201cSCM Workbench cannot be opened\\u201d in\n"
                   "      System Settings → Privacy & Security), click **Open Anyway** and reopen.\n"
                   "    - If it opens but the window stays on its starting page, that is the\n"
                   "      webview wedged by the fresh signature: quit the app fully and reopen it;\n"
                   "      if that still does not load, a reboot clears it.\n"
                   "    If a prompt asks about the local network, allow it too.\n"
                   "    (If the window doesn't reopen within ~10 s, launch "
                   f"{old_bundle} yourself — everything is already in place.)")
        else:
            finish(True, f"New version is starting. If the window doesn't reopen within ~10 s, "
                          f"launch {old_bundle} yourself — everything is already in place.")
    except UpdateError as e:
        fail(str(e))
    except Exception as e:
        import traceback
        emit("    " + traceback.format_exc(limit=3).replace("\n", "\n    "))
        fail(f"the update failed: {e}")
