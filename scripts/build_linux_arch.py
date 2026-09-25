#!/usr/bin/env python3
"""Assemble the Arch Linux x86_64 package from a baked Linux bundle.

The package uses the same immutable ``/usr/lib/scm-workbench`` payload as the
Debian package. Mutable settings, repositories, logs, and job output remain in
the invoking user's XDG data directory.

Run after the Tauri release binary has been built and ``bake_runtime.py`` has
populated the bundle's ``runtime`` directory::

    python scripts/build_linux_arch.py \
        --binary /path/to/target/release/scm-workbench \
        --bundle build/arch
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSET_NAME = "scm-workbench-linux-arch-x86_64.pkg.tar.zst"
PACKAGE_ROOT = Path("usr/lib/scm-workbench")
PKGBUILD_TEMPLATE = ROOT / "packaging/arch/PKGBUILD.in"
PKGBUILD_VERSION_TOKEN = "__SCM_WORKBENCH_PKGVER__"
VERSION_RE = re.compile(
    r'^__version__\s*=\s*["\'](?P<version>[^"\']+)["\']\s*$', re.MULTILINE
)
SEMVER_RE = re.compile(
    r"^(?P<core>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def project_version() -> str:
    text = (ROOT / "scm_workbench/_version.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit("arch package: could not read scm_workbench version")
    return match.group("version")


def arch_version(version: str) -> str:
    """Map strict SemVer to a readable pacman-ordered ``pkgver``.

    A prerelease identifier is joined directly to the patch number so pacman's
    ``vercmp`` orders ``1.2.3beta.2`` before ``1.2.3``. A leading numeric
    prerelease receives ``pre`` for the same reason. SemVer build metadata does
    not affect precedence and is therefore omitted from the package version.
    """
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ValueError(f"invalid application version: {version}")
    result = ".".join(match.group(name) for name in ("core", "minor", "patch"))
    prerelease = match.group("pre")
    if prerelease:
        parts = prerelease.split(".")
        first = parts.pop(0)
        # Hyphens are valid inside SemVer identifiers but not inside Arch's
        # pkgver. An underscore retains a visible boundary without becoming
        # the package filename's pkgver/pkgrel separator.
        first = first.replace("-", "_")
        result += ("pre." if first.isdigit() else "") + first
        if parts:
            result += "." + ".".join(part.replace("-", "_") for part in parts)
    if not re.fullmatch(r"[0-9A-Za-z._+]+", result):
        raise ValueError(f"application version cannot be represented for Arch: {version}")
    return result


def copy_source_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
    )


def write_shared_metadata(stage: Path) -> None:
    desktop = stage / "usr/share/applications/scm-workbench.desktop"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SCM Workbench\n"
        "Comment=Fetch card art and prepare print-and-cut PDFs\n"
        "Exec=/usr/bin/scm-workbench\n"
        "Icon=scm-workbench\n"
        "Terminal=false\n"
        "Categories=Graphics;Utility;\n"
        "StartupNotify=true\n"
        "StartupWMClass=scm-workbench\n",
        encoding="utf-8",
    )

    icon = stage / "usr/share/icons/hicolor/512x512/apps/scm-workbench.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "tauri/icons/512.png", icon)

    license_path = stage / "usr/share/licenses/scm-workbench/LICENSE.md"
    license_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE.md", license_path)


def stage_payload(binary: Path, bundle: Path) -> Path:
    runtime = bundle / "runtime"
    runtime_python = runtime / "python/install/bin/python3.13"
    if not binary.is_file() or binary.is_symlink():
        raise SystemExit(f"arch package: Tauri binary is missing or unsafe: {binary}")
    if runtime.is_symlink() or not runtime.is_dir() or not runtime_python.is_file():
        raise SystemExit(f"arch package: baked runtime is missing or unsafe: {runtime_python}")

    work = bundle / "arch-package"
    if os.path.lexists(work):
        if work.is_symlink():
            raise SystemExit(f"arch package: staging path is a symbolic link: {work}")
        shutil.rmtree(work)
    root = work / "root"
    payload = root / PACKAGE_ROOT
    (payload / "app").mkdir(parents=True)

    shutil.copy2(binary, payload / "scm-workbench")
    (payload / "scm-workbench").chmod(0o755)
    copy_source_tree(ROOT / "scm_workbench", payload / "app/scm_workbench")
    copy_source_tree(ROOT / "ui", payload / "app/ui")
    (payload / "app/docs").mkdir()
    shutil.copy2(ROOT / "docs/image-postprocessing.md", payload / "app/docs/image-postprocessing.md")
    copy_source_tree(ROOT / "docs/licenses", payload / "app/docs/licenses")
    copy_source_tree(runtime, payload / "runtime")

    launcher = root / "usr/bin/scm-workbench"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to("../lib/scm-workbench/scm-workbench")
    write_shared_metadata(root)
    return work


def render_pkgbuild(work: Path, version: str) -> str:
    template = PKGBUILD_TEMPLATE.read_text(encoding="utf-8")
    if template.count(PKGBUILD_VERSION_TOKEN) != 1:
        raise SystemExit("arch package: PKGBUILD template has an invalid version token")
    package_version = arch_version(version)
    (work / "PKGBUILD").write_text(
        template.replace(PKGBUILD_VERSION_TOKEN, package_version), encoding="utf-8"
    )
    return package_version


def assemble(binary: Path, bundle: Path, version: str) -> Path:
    work = stage_payload(binary, bundle)
    package_version = render_pkgbuild(work, version)
    generated = work / f"scm-workbench-{package_version}-1-x86_64.pkg.tar.zst"

    result = subprocess.run(
        ["makepkg", "--force", "--clean", "--noconfirm", "--nodeps", "--nocheck"],
        cwd=work,
    )
    if result.returncode:
        raise SystemExit(f"arch package: makepkg failed with exit {result.returncode}")
    if (not generated.is_file() or generated.is_symlink() or
            generated.stat().st_size <= 0):
        raise SystemExit(f"arch package: makepkg did not produce {generated.name}")

    output = bundle / ASSET_NAME
    output.unlink(missing_ok=True)
    os.replace(generated, output)
    shutil.rmtree(work)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path,
                        help="release scm-workbench Tauri executable")
    parser.add_argument("--bundle", default=ROOT / "build/arch", type=Path,
                        help="bundle directory containing runtime/ and receiving the package")
    args = parser.parse_args()

    if not sys.platform.startswith("linux"):
        raise SystemExit("arch package: assembly must run on Linux")
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise SystemExit(f"arch package: unsupported release architecture: {platform.machine()}")
    if shutil.which("makepkg") is None:
        raise SystemExit("arch package: makepkg is required")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("arch package: makepkg must run as an unprivileged user")

    bundle = args.bundle.resolve()
    bundle.mkdir(parents=True, exist_ok=True)
    artifact = assemble(args.binary.resolve(), bundle, project_version())
    print(f"arch package: built {artifact} ({artifact.stat().st_size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
