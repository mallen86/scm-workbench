#!/usr/bin/env python3
"""Assemble the Debian/Ubuntu x86_64 package from a baked Linux bundle.

The package owns an immutable payload under /usr/lib/scm-workbench and exposes
it through /usr/bin/scm-workbench. Mutable settings, repositories, logs, and
job output stay in the invoking user's XDG data directory.

Run after the Tauri release binary has been built and ``bake_runtime.py`` has
populated ``build/linux/runtime``::

    python scripts/build_linux_deb.py \
        --binary /path/to/target/release/scm-workbench \
        --bundle build/linux
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
ASSET_NAME = "scm-workbench-linux-amd64.deb"
PACKAGE_ROOT = Path("usr/lib/scm-workbench")
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
        raise SystemExit("linux package: could not read scm_workbench version")
    return match.group("version")


def debian_version(version: str) -> str:
    """Map strict SemVer ordering onto Debian's version ordering.

    Debian's ``~`` sorts before the empty string, so 1.0.0~beta.1 correctly
    precedes 1.0.0 while SemVer build metadata remains non-ordering metadata.
    """
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ValueError(f"invalid application version: {version}")
    result = ".".join(match.group(name) for name in ("core", "minor", "patch"))
    if match.group("pre"):
        result += "~" + match.group("pre")
    if match.group("build"):
        result += "+" + match.group("build")
    return result


def copy_source_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
    )


def installed_size_kib(root: Path) -> int:
    total = sum(
        path.stat(follow_symlinks=False).st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    return max(1, (total + 1023) // 1024)


def write_metadata(stage: Path, version: str) -> None:
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

    doc = stage / "usr/share/doc/scm-workbench"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "copyright").write_text(
        "SCM Workbench\nCopyright: Michael Allen and contributors\n"
        "License: see the project repository for license terms.\n",
        encoding="utf-8",
    )

    control_dir = stage / "DEBIAN"
    control_dir.mkdir(mode=0o755)
    size = installed_size_kib(stage)
    (control_dir / "control").write_text(
        f"""Package: scm-workbench
Version: {debian_version(version)}
Section: graphics
Priority: optional
Architecture: amd64
Maintainer: Michael Allen <mtallen@outlook.com>
Installed-Size: {size}
Depends: libwebkit2gtk-4.1-0, libgtk-3-0, xdg-utils
Homepage: https://github.com/mallen86/scm-workbench
Description: Desktop workbench for Silhouette Card Maker
 Fetch card art, manage card backs, create print-and-cut PDFs, and run the
 supporting Silhouette Card Maker utilities from one native desktop app.
""",
        encoding="utf-8",
    )


def assemble(binary: Path, bundle: Path, version: str) -> Path:
    runtime = bundle / "runtime"
    runtime_python = runtime / "python/install/bin/python3.13"
    if not binary.is_file():
        raise SystemExit(f"linux package: Tauri binary is missing: {binary}")
    if not runtime_python.is_file():
        raise SystemExit(f"linux package: baked runtime is missing: {runtime_python}")

    stage = bundle / "deb-root"
    shutil.rmtree(stage, ignore_errors=True)
    payload = stage / PACKAGE_ROOT
    (payload / "app").mkdir(parents=True)

    shutil.copy2(binary, payload / "scm-workbench")
    (payload / "scm-workbench").chmod(0o755)
    copy_source_tree(ROOT / "scm_workbench", payload / "app/scm_workbench")
    copy_source_tree(ROOT / "ui", payload / "app/ui")
    copy_source_tree(runtime, payload / "runtime")

    launcher = stage / "usr/bin/scm-workbench"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to("../lib/scm-workbench/scm-workbench")
    write_metadata(stage, version)

    output = bundle / ASSET_NAME
    output.unlink(missing_ok=True)
    command = ["dpkg-deb", "--root-owner-group", "--build", str(stage), str(output)]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        detail = (result.stderr or result.stdout or "dpkg-deb failed").strip()[-2000:]
        raise SystemExit(f"linux package: {detail}")
    if not output.is_file() or output.stat().st_size == 0:
        raise SystemExit("linux package: dpkg-deb produced no artifact")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path,
                        help="release scm-workbench Tauri executable")
    parser.add_argument("--bundle", default=ROOT / "build/linux", type=Path,
                        help="bundle directory containing runtime/ and receiving the .deb")
    args = parser.parse_args()

    if not sys.platform.startswith("linux"):
        raise SystemExit("linux package: .deb assembly must run on Linux")
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise SystemExit(f"linux package: unsupported release architecture: {platform.machine()}")
    if shutil.which("dpkg-deb") is None:
        raise SystemExit("linux package: dpkg-deb is required")

    bundle = args.bundle.resolve()
    bundle.mkdir(parents=True, exist_ok=True)
    artifact = assemble(args.binary.resolve(), bundle, project_version())
    print(f"linux package: built {artifact} ({artifact.stat().st_size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
