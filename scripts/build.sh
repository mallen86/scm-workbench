#!/bin/sh
# Local build of the app, matching the packaging workflow (package.yml) so a
# from-scratch build on a laptop produces the same bundle CI ships. The app is
# a single Tauri shell (tauri/) that hosts one webview and spawns the bundled
# runtime running the UI server — there is no briefcase step anymore.
#
#   Usage: scripts/build.sh [macos|linux|arch|windows]
#
# What it runs (the three steps the CI job runs, in order):
#   1. cargo build --release --features custom-protocol   (the window)
#   2. scripts/bake_runtime.py --bundle build/<platform>  (the private runtime)
#   3. assemble the .app / .exe bundle (see package.yml for the full layout +
#      signing; on macOS this step is the "Assemble the .app" block there).
#
# The PIP_FIND_LINKS export mirrors the workflow: a local build resolves the
# vendored proxy_tools wheel from ./ciwheels the same way CI does.
set -e
cd "$(dirname "$0")/.."
plat="${1:-macos}"
case "$plat" in
    macos|linux|arch|windows) ;;
    *) echo "usage: scripts/build.sh [macos|linux|arch|windows]" >&2; exit 2 ;;
esac

if [ ! -x .venv/bin/python ]; then
    echo ".venv is missing - set it up first (CONTRIBUTING.md: uv venv && uv sync)." >&2
    exit 1
fi
if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
    echo ".venv is not Python 3.13 - recreate it (uv venv && uv sync), see CONTRIBUTING.md." >&2
    exit 1
fi

export PIP_FIND_LINKS="file://$PWD/ciwheels"

echo "==> 1/3  building the Tauri shell ($plat)"
( cd tauri && cargo build --release --features custom-protocol )

echo "==> 2/3  baking the bundled runtime into build/$plat"
.venv/bin/python scripts/bake_runtime.py --bundle "build/$plat"

echo "==> 3/3  assembling the bundle"
# The assembly is platform-specific (macOS .app, Debian .deb, Arch
# .pkg.tar.zst, or Windows flat bundle). The canonical release version lives in
# package.yml. Local builds reproduce release layouts; Windows remains CI-owned.
if [ "$plat" = "macos" ]; then
    bundle="$PWD/build/macos"
    app="$bundle/SCM Workbench.app"
    rm -rf "$app"
    mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$app/Contents/app"
    cp "$PWD/tauri/target/release/scm-workbench" "$app/Contents/MacOS/SCM Workbench"
    cp -R "$PWD/scm_workbench" "$app/Contents/app/scm_workbench"
    cp -R "$PWD/ui" "$app/Contents/app/ui"
    mkdir -p "$app/Contents/app/docs"
    cp "$PWD/docs/image-postprocessing.md" "$app/Contents/app/docs/image-postprocessing.md"
    mv "$bundle/runtime" "$app/Contents/runtime"
    rm -rf "$bundle/.bake"
    echo "    built: $app"
elif [ "$plat" = "linux" ] || [ "$plat" = "arch" ]; then
    case "${CARGO_TARGET_DIR:-}" in
        "") binary="$PWD/tauri/target/release/scm-workbench" ;;
        /*) binary="$CARGO_TARGET_DIR/release/scm-workbench" ;;
        *) binary="$PWD/tauri/$CARGO_TARGET_DIR/release/scm-workbench" ;;
    esac
    if [ "$plat" = "arch" ]; then
        .venv/bin/python scripts/build_linux_arch.py --binary "$binary" --bundle "$PWD/build/arch"
    else
        .venv/bin/python scripts/build_linux_deb.py --binary "$binary" --bundle "$PWD/build/linux"
    fi
else
    echo "    (windows: run the package.yml 'Assemble the bundle' steps, or push a tag)"
fi
