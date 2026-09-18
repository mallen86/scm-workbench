#!/bin/sh
# Install and exercise the Arch/Manjaro package in a clean root container.
set -eu

package="${1:-}"
case "$package" in /*.pkg.tar.zst) ;; *) echo "arch smoke: package path must be an absolute .pkg.tar.zst" >&2; exit 2 ;; esac
[ -f "$package" ] || { echo "arch smoke: package is missing: $package" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "arch smoke: package installation requires a root container" >&2; exit 2; }
[ "$(uname -m)" = x86_64 ] || { echo "arch smoke: expected x86_64" >&2; exit 2; }

export LC_ALL=C
pacman -Syu --noconfirm --needed \
    xorg-server-xvfb xorg-xauth xdotool dbus python ttf-dejavu
pacman -U --noconfirm "$package"

pacman -Qip "$package" | grep -E '^Architecture[[:space:]]*: x86_64$'
pacman -Qip "$package" | grep -E '^Depends On[[:space:]]*:.*webkit2gtk-4.1.*gtk3.*xdg-utils'
pacman -Qkk scm-workbench
test "$(readlink /usr/bin/scm-workbench)" = "../lib/scm-workbench/scm-workbench"
pacman -Qo /usr/lib/scm-workbench/scm-workbench >/dev/null
if ldd /usr/lib/scm-workbench/scm-workbench | grep -q 'not found'; then
    ldd /usr/lib/scm-workbench/scm-workbench
    exit 1
fi

# Verify the package's own updater code resolves both Arch and Manjaro to the
# exact Arch asset rather than seeing a source checkout through the test mount.
PYTHONPATH=/usr/lib/scm-workbench/app \
    /usr/lib/scm-workbench/runtime/python/install/bin/python3.13 -B - <<'PY'
from scm_workbench import updater
assert updater.package_format() == "arch"
assert updater.expected_asset_name() == updater.LINUX_ARCH_ASSET
PY

useradd --create-home scm-smoke
runtime_dir="$(mktemp -d /tmp/scm-workbench-runtime.XXXXXX)"
chown scm-smoke:scm-smoke "$runtime_dir"
chmod 700 "$runtime_dir"
runuser -u scm-smoke -- env \
    HOME=/home/scm-smoke \
    XDG_RUNTIME_DIR="$runtime_dir" \
    GDK_BACKEND=x11 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    WEBKIT_DISABLE_DMABUF_RENDERER=1 \
    xvfb-run -a dbus-run-session -- \
    "$(dirname "$0")/check_linux_package.sh" \
    /usr/lib/scm-workbench/scm-workbench
rm -rf "$runtime_dir"

echo "arch smoke: package ownership, target selection, and lifecycle checks pass"
