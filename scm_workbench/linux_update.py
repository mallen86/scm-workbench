"""Package-manager installation boundary for installed Linux builds.

The privileged entry point copies the caller-owned download to private root
storage and verifies its frozen digest before any package manager sees it.
It never writes user data or accepts executable names/commands from callers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import selectors
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path("/usr/lib/scm-workbench")
HELPER = ROOT / "app/scm_workbench/linux_update.py"
PYTHON = ROOT / "runtime/python/install/bin/python3.13"
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
INSTALL_TIMEOUT = 30 * 60
SEMVER = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?", re.ASCII)
RELEASE_REPO = "mallen86/scm-workbench"
ASSETS = {"deb": "scm-workbench-linux-amd64.deb", "arch": "scm-workbench-linux-arch-x86_64.pkg.tar.zst"}


class InstallError(Exception):
    pass


def trusted(path: Path, *, under: Path | None = None) -> Path:
    resolved = path.resolve(strict=True)
    if under is not None:
        resolved.relative_to(under)
    current = resolved
    while True:
        info = current.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise InstallError("The installed updater is not protected by system ownership.")
        if current == current.parent:
            break
        current = current.parent
    if not resolved.is_file():
        raise InstallError("The system updater is unavailable.")
    return resolved


def family() -> str:
    if not sys.platform.startswith("linux") or os.uname().machine != "x86_64":
        raise InstallError("Linux updates require a supported x86_64 desktop.")
    with open("/etc/os-release", "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise InstallError("Linux distribution information is too large.")
    values = {}
    for line in raw.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip('"').strip("'")
    if values.get("ID") in ("debian", "ubuntu"):
        return "deb"
    if values.get("ID") in ("arch", "manjaro"):
        return "arch"
    raise InstallError("This Linux distribution has no supported update package.")


def context() -> tuple[Path, Path, Path, str]:
    kind = family()
    python = trusted(PYTHON, under=ROOT)
    helper = trusted(HELPER, under=ROOT)
    if Path(__file__).resolve() != helper:
        raise InstallError("In-app installation is only available in the system-installed app.")
    pkexec = trusted(Path("/usr/bin/pkexec"))
    manager = trusted(Path("/usr/bin/apt-get" if kind == "deb" else "/usr/bin/pacman"))
    if not all(os.access(path, os.X_OK) for path in (python, pkexec, manager)):
        raise InstallError("The system updater is not executable.")
    return python, helper, pkexec, kind


def available() -> bool:
    try:
        context()
        return True
    except (OSError, ValueError, InstallError):
        return False


def package_version(tag: str, kind: str) -> str:
    match = SEMVER.fullmatch(tag) if isinstance(tag, str) and len(tag) <= 128 else None
    if not match or kind not in ("deb", "arch"):
        raise InstallError("Invalid package version.")
    major, minor, patch, pre, build = match.groups()
    version = f"{major}.{minor}.{patch}"
    if kind == "deb":
        return version + ("~" + pre if pre else "") + ("+" + build if build else "")
    if pre:
        first, *rest = pre.split(".")
        version += ("pre." if first.isdigit() else "") + first.replace("-", "_")
        if rest:
            version += "." + ".".join(part.replace("-", "_") for part in rest)
    return version + "-1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InstallError("The release verification endpoint redirected unexpectedly.")


def verify_release(tag: str, kind: str, digest: str) -> int:
    # The elevated helper independently checks its immutable upstream origin.
    # A caller cannot authorize an arbitrary root package merely by supplying
    # a matching local hash or forged package-control fields.
    package_version(tag, kind)
    url = "https://api.github.com/repos/" + RELEASE_REPO + "/releases/tags/" + urllib.parse.quote(tag, safe="")
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "Accept-Encoding": "identity",
                                                   "User-Agent": "SCM-Workbench-Linux-Updater"})
    ca = next((path for path in (Path("/etc/ssl/certs/ca-certificates.crt"), Path("/etc/ssl/cert.pem"))
               if path.is_file()), None)
    if ca is None:
        raise InstallError("System CA certificates are required to verify the update.")
    tls = ssl.create_default_context(cafile=str(trusted(ca)))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=tls))
    deadline = time.monotonic() + 30
    chunks = bytearray()
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise InstallError("Release checksum verification failed.")
            while chunk := response.read1(65536):
                chunks.extend(chunk)
                if len(chunks) > 1024 * 1024 or time.monotonic() > deadline:
                    raise InstallError("Release verification exceeded its size or time limit.")
        release = json.loads(chunks)
        if not isinstance(release, dict) or release.get("draft") is not False or release.get("tag_name") != tag:
            raise InstallError("The release is not a published, tag-bound update.")
        assets = release.get("assets")
        if not isinstance(assets, list) or len(assets) > 64:
            raise InstallError("Invalid release assets.")
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == ASSETS[kind]]
        if len(matches) != 1:
            raise InstallError("The release package is missing or ambiguous.")
        asset = matches[0]
        size = asset.get("size")
        if (asset.get("state") != "uploaded" or type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES or
                not isinstance(asset.get("digest"), str) or asset["digest"].lower() != "sha256:" + digest):
            raise InstallError("The package checksum does not match the official release.")
        return size
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise InstallError("Could not re-verify the official release. Check your connection and retry; no package was installed.") from error


def copy_verified(source: Path, target: Path, digest: str, uid: int, *, expected_size: int | None = None) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise InstallError("Invalid package checksum.")
    if not source.is_absolute() or any(ord(c) < 32 for c in str(source)):
        raise InstallError("Invalid package path.")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as inp:
        before = os.fstat(inp.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                before.st_uid != uid or not 0 < before.st_size <= MAX_PACKAGE_BYTES or
                (expected_size is not None and before.st_size != expected_size)):
            raise InstallError("The downloaded package is not a bounded caller-owned file.")
        if shutil.disk_usage(target.parent).free < before.st_size + 64 * 1024**2:
            raise InstallError("Not enough free space to prepare the package safely.")
        hashed = hashlib.sha256()
        total = 0
        deadline = time.monotonic() + 5 * 60
        with target.open("xb") as out:
            os.chmod(target, 0o600)
            while chunk := inp.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_PACKAGE_BYTES or time.monotonic() > deadline:
                    raise InstallError("Preparing the package exceeded its size or time limit.")
                hashed.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        after = os.fstat(inp.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if (identity(before) != identity(after) or total != before.st_size or
                hashed.hexdigest() != digest):
            raise InstallError("The downloaded package changed or its checksum is incorrect.")


def inspect_limits() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))


def inspect_package(package: Path, kind: str, version: str, work: Path) -> None:
    command = ([str(trusted(Path("/usr/bin/dpkg-deb"))), "--field", str(package),
                "Package", "Version", "Architecture"] if kind == "deb" else
               [str(trusted(Path("/usr/bin/bsdtar"))), "-xOf", str(package), ".PKGINFO"])
    # Bound metadata parsing even if a forged CLI request supplies an archive
    # bomb. Inspector limits never apply to the package-manager transaction.
    with (work / "metadata").open("w+b") as output:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=30, preexec_fn=inspect_limits,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
        output.seek(0)
        raw = output.read(65537)
    if result.returncode or len(raw) > 65536:
        raise InstallError("Could not validate the update package metadata.")
    separator = ":" if kind == "deb" else "="
    fields = {}
    for line in raw.decode("utf-8").splitlines():
        key, found, value = line.partition(separator)
        if found:
            key = key.strip()
            if key in fields and key in ("Package", "Version", "Architecture", "pkgname", "pkgver", "arch"):
                raise InstallError("Ambiguous update package metadata.")
            fields[key] = value.strip()
    keys = ("Package", "Version", "Architecture") if kind == "deb" else ("pkgname", "pkgver", "arch")
    expected = ("scm-workbench", version, "amd64" if kind == "deb" else "x86_64")
    if tuple(fields.get(key) for key in keys) != expected:
        raise InstallError("The package name, version, or architecture does not match the update.")


def manager_command(kind: str, package: Path, *, downgrade: bool = False) -> list[str]:
    if kind == "deb":
        return [str(trusted(Path("/usr/bin/apt-get"))), "-y", "-o", "DPkg::Lock::Timeout=120",
                *(["--allow-downgrades"] if downgrade else []), "install", str(package)]
    if kind == "arch":
        return [str(trusted(Path("/usr/bin/pacman"))), "--noconfirm", "-U", str(package)]
    raise InstallError("Unsupported package manager.")


def install_privileged(source: Path, digest: str, tag: str, kind: str, *, downgrade: bool = False) -> None:
    if os.geteuid() != 0:
        raise InstallError("System administrator approval is required.")
    caller = os.environ.get("PKEXEC_UID", "")
    if not caller.isdecimal() or int(caller) <= 0:
        raise InstallError("Installation must be authorized through the OS prompt.")
    if family() != kind:
        raise InstallError("The package does not match this Linux distribution.")
    version = package_version(tag, kind)
    size = verify_release(tag, kind, digest)
    with tempfile.TemporaryDirectory(prefix="scm-workbench-install-", dir="/var/tmp") as temp:
        work = Path(temp)
        package = work / ("update.deb" if kind == "deb" else "update.pkg.tar.zst")
        copy_verified(source, package, digest, int(caller), expected_size=size)
        inspect_package(package, kind, version, work)
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
               "HOME": "/root", "DEBIAN_FRONTEND": "noninteractive"}
        current = installed_version(kind, work)
        if kind == "arch" and not downgrade:
            with (work / "version-order").open("w+b") as output:
                compared = subprocess.run([str(trusted(Path("/usr/bin/vercmp"))), version, current],
                                          stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                          timeout=15, preexec_fn=inspect_limits, env=env)
                output.seek(0)
                order = output.read(32).strip()
            if compared.returncode or order not in (b"-1", b"0", b"1"):
                raise InstallError("Could not verify the installed package version ordering.")
            if order == b"-1":
                raise InstallError("A newer package is already installed. Restart Workbench and check for updates again.")
        run_manager(manager_command(kind, package, downgrade=downgrade), env)
        verify_installed(kind, version, work)


def run_manager(command: list[str], env: dict) -> None:
    # Nonblocking drain: a post-install daemon retaining a pipe must not strand
    # the helper. Only a bounded tail survives, even for very noisy installers.
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=env, start_new_session=True) as process:
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        tail = bytearray()
        deadline = time.monotonic() + INSTALL_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise InstallError("Package installation timed out. Check your package manager before retrying.")
                ready = selector.select(timeout=0.25)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    if chunk:
                        tail.extend(chunk)
                        del tail[:-16384]
                    else:
                        selector.unregister(fd)
            # Drain at most 64 KiB after exit. Background processes cannot keep
            # the pipe (or the completed update) alive indefinitely.
            for _ in range(16):
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                tail.extend(chunk)
                del tail[:-16384]
        if process.returncode:
            detail = " ".join(tail.decode("utf-8", "replace").split())[-1000:]
            raise InstallError("Package installation failed. " + detail)


def installed_version(kind: str, work: Path) -> str:
    command = ([str(trusted(Path("/usr/bin/dpkg-query"))), "--show",
                "--showformat=${db:Status-Status}\\n${Version}\\n${Architecture}\\n", "scm-workbench"]
               if kind == "deb" else
               [str(trusted(Path("/usr/bin/pacman"))), "-Qi", "scm-workbench"])
    with (work / "installed").open("w+b") as output:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=30, preexec_fn=inspect_limits,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
        output.seek(0)
        raw = output.read(65537)
    text = raw.decode("utf-8")
    if kind == "deb":
        lines = text.splitlines()
        matches = len(lines) == 3 and lines[0] == "installed" and lines[2] == "amd64"
        version = lines[1] if matches else ""
    else:
        fields = {}
        for line in text.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        matches = fields.get("Name") == "scm-workbench" and fields.get("Architecture") == "x86_64"
        version = fields.get("Version", "")
    if (result.returncode or len(raw) > 65536 or not matches or
            not re.fullmatch(r"[0-9][0-9A-Za-z.+:~_-]{0,127}", version)):
        raise InstallError("The package manager could not confirm the installed app. Repair the package installation before retrying.")
    return version


def verify_installed(kind: str, version: str, work: Path) -> None:
    if installed_version(kind, work) != version:
        raise InstallError("The package manager did not confirm the expected installed version. Check your package manager before restarting.")


def install(package: Path, digest: str, tag: str, kind: str, *, downgrade: bool = False) -> None:
    python, helper, pkexec, actual = context()
    if actual != kind:
        raise InstallError("The downloaded package does not match this Linux distribution.")
    package_version(tag, kind)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise InstallError("A verified SHA-256 release checksum is required for Linux installation.")
    command = [str(pkexec), str(python), "-I", str(helper), "--package", str(package),
               "--sha256", digest, "--tag", tag, "--family", kind,
               *(["--allow-downgrade"] if downgrade else [])]
    # The privileged helper emits only a bounded final diagnostic, never the
    # package-manager transcript; it owns the transaction timeout.
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=INSTALL_TIMEOUT + 5 * 60)
    except (subprocess.TimeoutExpired, PermissionError) as error:
        raise InstallError("Installation status is uncertain. Check your package manager before retrying or restarting.") from error
    if result.returncode in (126, 127):
        raise InstallError("Administrator approval was cancelled or unavailable. No update was installed.")
    if result.returncode:
        message = " ".join(result.stderr.decode("utf-8", "replace").split())[:1500]
        raise InstallError(message or "The package manager could not install the update.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a verified Workbench Linux package")
    parser.add_argument("--package", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--family", required=True, choices=("deb", "arch"))
    parser.add_argument("--allow-downgrade", action="store_true")
    args = parser.parse_args()
    try:
        if len(args.package.encode("utf-8")) > 4096:
            raise InstallError("The package path is too long.")
        install_privileged(Path(args.package), args.sha256, args.tag, args.family, downgrade=args.allow_downgrade)
        return 0
    except Exception as error:
        print(" ".join(str(error).split())[:1500] or "Package installation failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
