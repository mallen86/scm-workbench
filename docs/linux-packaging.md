# Linux packaging

SCM Workbench's first Linux release target is x86_64/amd64 Debian-family
desktops:

- Ubuntu 22.04 LTS or newer
- Debian 12 or newer

Arch Linux packaging is a follow-up target, not part of the current `.deb`.

## Installed layout

The Debian package owns one immutable application tree:

```text
/usr/lib/scm-workbench/
├── scm-workbench          # Tauri shell
├── app/
│   ├── scm_workbench/     # Python worker
│   └── ui/                # embedded/source parity assets
└── runtime/python/        # pinned private Python 3.13 runtime
/usr/bin/scm-workbench     # relative symlink to the shell
/usr/share/applications/scm-workbench.desktop
/usr/share/icons/hicolor/128x128/apps/scm-workbench.png
```

The app never writes to `/usr/lib/scm-workbench`. Mutable state follows the
XDG data convention at `$XDG_DATA_HOME/scm-workbench`, falling back to
`~/.local/share/scm-workbench`. Managed repositories, settings, logs, images,
and output therefore survive package replacement or removal.

The packaged shell starts exactly one bundled Python worker over bounded
JSON-lines stdin/stdout. It does not start the browser-mode HTTP server. Unix
process-group supervision is used for worker shutdown and forced cleanup.

## Build prerequisites

The canonical build runs on Ubuntu 22.04 x86_64. In addition to Python 3.13,
`uv`, Rust, and Node.js, install:

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends \
  clang zstd pkg-config libwebkit2gtk-4.1-dev libgtk-3-dev \
  libayatana-appindicator3-dev librsvg2-dev libssl-dev patchelf \
  xdg-utils xvfb xauth xdotool dbus-x11
```

Then build from the repository root:

```sh
uv venv
uv sync
PIP_FIND_LINKS="file://$(pwd)/ciwheels" scripts/build.sh linux
```

The result is `build/linux/scm-workbench-linux-amd64.deb`. The build script
uses the explicitly pinned GNU Linux python-build-standalone archive, bakes all
runtime imports, copies only the application allowlist, normalizes package file
modes and timestamps, and invokes `dpkg-deb --root-owner-group`.

A release build must run `python scripts/inject_version.py` first so the tag is
reflected in Python, Cargo, Tauri, and Debian metadata. The canonical sequence
and dependency list live in `.github/workflows/package.yml`.

## Verification

Install the package through apt so its runtime dependencies are resolved:

```sh
sudo apt-get install ./build/linux/scm-workbench-linux-amd64.deb
sudo dpkg -V scm-workbench
```

Check for unresolved native libraries and run the lifecycle smoke under a
virtual display:

```sh
if ldd /usr/lib/scm-workbench/scm-workbench | grep -q 'not found'; then
  ldd /usr/lib/scm-workbench/scm-workbench
  exit 1
fi

runtime_dir="$(mktemp -d)"
chmod 700 "$runtime_dir"
XDG_RUNTIME_DIR="$runtime_dir" \
  xvfb-run -a dbus-run-session -- \
  scripts/check_linux_package.sh /usr/lib/scm-workbench/scm-workbench
```

The smoke verifies:

- required imports in the private Python runtime;
- a direct bounded worker RPC exchange;
- embedded UI render markers through the Tauri native bridge;
- no listener on the browser-server port;
- graceful shell exit reaping the worker; and
- forced shell termination leaving no worker behind.

`tests/test_linux_packaging.py` also validates exact package names, paths,
metadata, desktop integration, copy allowlists, traversal rejection, and Linux
bundle-shape recognition.

## Updates

Linux app files are package-manager-owned, so Workbench deliberately does not
attempt an in-place self-update. Update metadata selects only the exact
`scm-workbench-linux-amd64.deb` asset from the configured GitHub repository and
reports `install_mode: "manual"`. The Settings page and standing update notice
open the validated, tag-bound GitHub release page. The user then installs the
new package with their software manager or `apt`.

This keeps release discovery bounded without granting the app authority to
replace `/usr` files. Data under the XDG data directory is not touched.

## Arch follow-up

Arch support should reuse the same native shell, private runtime, native IPC,
XDG data path, and manual update policy. It still needs a separately validated
package recipe, dependency names, installed-layout fixture, clean-container
install test, and WebKitGTK lifecycle smoke before it can be added to the
release matrix.
