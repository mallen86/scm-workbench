# Linux packaging

SCM Workbench supports x86_64 Linux desktops through two package formats:

- `scm-workbench-linux-amd64.deb` for Ubuntu 22.04 or newer and Debian 12 or newer;
- `scm-workbench-linux-arch-x86_64.pkg.tar.zst` for current Arch Linux and Manjaro.

Compatible derivatives use the package format of their base distribution:
Linux Mint 21+ (Ubuntu-based), LMDE 6+ (Debian-based), and suitable Pop!_OS/Zorin
versions select the Debian package; Arch-based derivatives such as EndeavourOS
and Garuda select the Arch package. Derivatives must still satisfy the base
version and runtime dependency requirements above; recognizing a family is not
a guarantee of binary compatibility on older distributions.

There is no Linux ARM64 package. Both packages contain the same native shell,
application code, UI, and pinned private Python 3.13 runtime.

## Installed layout

Both package managers own one immutable application tree:

```text
/usr/lib/scm-workbench/
├── scm-workbench          # Tauri shell
├── app/
│   ├── scm_workbench/     # Python worker
│   └── ui/                # embedded/source parity assets
└── runtime/python/        # pinned private Python 3.13 runtime
/usr/bin/scm-workbench     # relative symlink to the shell
/usr/share/applications/scm-workbench.desktop
/usr/share/icons/hicolor/512x512/apps/scm-workbench.png
```

The Arch package additionally installs the project license under
`/usr/share/licenses/scm-workbench/`.

The app never writes to `/usr/lib/scm-workbench`. Mutable state follows the XDG
data convention at `$XDG_DATA_HOME/scm-workbench`, falling back to
`~/.local/share/scm-workbench`. Managed repositories, settings, logs, images,
and output therefore survive package replacement or removal.

The packaged shell starts exactly one bundled Python worker over bounded
JSON-lines stdin/stdout. It does not start the browser-mode HTTP server. Unix
process-group supervision is used for worker shutdown and forced cleanup.

## Debian/Ubuntu build

The canonical shell/runtime build runs on Ubuntu 22.04 x86_64. In addition to
Python 3.13, `uv`, Rust, and Node.js, install:

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

The result is `build/linux/scm-workbench-linux-amd64.deb`.
`scripts/build_linux_deb.py` normalizes the package layout and invokes
`dpkg-deb --root-owner-group`.

## Arch/Manjaro build

A local Arch build needs the Tauri prerequisites plus `base-devel`, `python`,
Rust, Node.js, `uv`, `clang`, `pkgconf`, `patchelf`, and `zstd`. The relevant
Arch runtime dependency names are `webkit2gtk-4.1`, `gtk3`, and `xdg-utils`.

```sh
uv python install 3.13
uv venv --python 3.13
uv sync
PIP_FIND_LINKS="file://$(pwd)/ciwheels" scripts/build.sh arch
```

The result is `build/arch/scm-workbench-linux-arch-x86_64.pkg.tar.zst`.
`scripts/build_linux_arch.py` stages the same immutable payload as the Debian
builder and renders `packaging/arch/PKGBUILD.in` before invoking `makepkg` as an
unprivileged user. It disables package-time stripping so `makepkg` cannot
mutate the prebuilt shell or private runtime.

The release workflow avoids compiling the Linux application twice. It builds
the shell and private runtime on Ubuntu 22.04, then packages those exact bytes
inside a digest-pinned official `archlinux:base-devel` container. Arch package
versions map prereleases so pacman's `vercmp` orders beta releases before their
stable release.

A release build must run `python scripts/inject_version.py` first so the tag is
reflected in Python, Cargo, Tauri, Debian metadata, and Arch metadata. The
canonical sequence, container digests, and dependency lists live in
`.github/workflows/package.yml`.

## Verification

### Debian/Ubuntu

Install through apt so runtime dependencies are resolved:

```sh
sudo apt-get install ./build/linux/scm-workbench-linux-amd64.deb
sudo dpkg -V scm-workbench
```

Then run the common lifecycle smoke under a virtual display:

```sh
runtime_dir="$(mktemp -d)"
chmod 700 "$runtime_dir"
XDG_RUNTIME_DIR="$runtime_dir" \
  xvfb-run -a dbus-run-session -- \
  scripts/check_linux_package.sh /usr/lib/scm-workbench/scm-workbench
```

### Arch/Manjaro

Install with pacman and verify package ownership:

```sh
sudo pacman -U ./build/arch/scm-workbench-linux-arch-x86_64.pkg.tar.zst
pacman -Qkk scm-workbench
```

`scripts/check_arch_package.sh` is the clean-container contract. It updates the
rolling container as one transaction, installs Xvfb/DBus test dependencies,
installs the package, checks metadata and `ldd`, verifies updater target
selection from the package's own code, and runs the common lifecycle smoke as
an unprivileged user. CI runs it independently in digest-pinned current Arch
and Manjaro base containers.

The common smoke verifies:

- required imports in the private Python runtime;
- the bounded PDF preview helper;
- embedded UI render markers through the Tauri native bridge;
- no listener on the browser-server port;
- no WebView HTTP request;
- graceful native close reaping the worker; and
- forced shell termination leaving no running worker.

A container without a real init may retain an already-terminated orphan as a
zombie. The smoke distinguishes that inert state from a running process while
still failing on any live worker.

`tests/test_linux_packaging.py` also validates package versions, exact names,
paths, metadata, desktop integration, copy allowlists, and Linux bundle-shape
recognition.

## Updates

Linux app files remain package-manager-owned. An installed Workbench can
now download and install updates through the operating system, rather than
replacing its own files. It reads bounded `/etc/os-release` metadata,
requires x86_64, and resolves exactly one supported package family:

- Debian/Ubuntu and derivatives declaring `ubuntu`/`debian` in `ID_LIKE` select
  `scm-workbench-linux-amd64.deb`;
- Arch/Manjaro and derivatives declaring `arch` in `ID_LIKE` select
  `scm-workbench-linux-arch-x86_64.pkg.tar.zst`;
- unknown lineage, conflicting families, malformed metadata, or unsupported
  CPU architectures fail closed.

Release selection and the isolated privileged installer use the same strict
metadata parser and family resolver. `ID_LIKE` is a bounded list of exact tokens,
not a substring/name guess or a probe for whichever package manager is present.
CPU architecture is checked separately: `x86_64`/`amd64` are supported;
ARM64, 32-bit x86, and RISC-V must never receive these packages.

Release metadata must contain one unambiguous exact asset for the resolved
target. Cached update state is revalidated against that target, so moving an
XDG data directory between distributions cannot retain authority for the wrong
package. Both interface modes offer the same download/install action from
Settings and the standing update notice.

The installed packages depend on Polkit (`policykit-1` on Debian/Ubuntu,
`polkit` on Arch/Manjaro) and system CA certificates. A desktop authentication
agent must be running to display the administrator prompt. Source checkouts,
missing `pkexec`, unprotected installations, and unsupported distributions keep
the validated release-page/manual-install flow. Older Workbench Linux releases
must be upgraded manually once to obtain this installer.

## Linux update boundary

1. Re-verify the selected release and download the exact asset with its required
   GitHub SHA-256 digest. Downloads retain the existing origin and size limits.
2. Require other jobs in this worker to finish, then fence new job admission.
   Existing jobs are never forcibly stopped to perform an update.
3. Request OS administrator approval with `pkexec`, invoking only the fixed,
   root-owned bundled interpreter and standalone `linux_update.py` helper.
   Writable helper/interpreter paths are rejected; no shell or caller-supplied
   command is accepted.
4. The elevated helper independently fetches bounded public metadata from the
   immutable official GitHub release origin, with system TLS verification and
   no redirects or environment proxies. It checks the tag, published status,
   unique asset, size, and digest. A forged local hash cannot authorize a package
   from another publisher. Custom update repositories require manual installation.
5. Copy the bounded, regular, single-link caller-owned package into private
   root-owned `/var/tmp` storage and verify its frozen size/hash again. Concurrent
   changes to the original download cannot change the package being installed.
6. Inspect the package name (`scm-workbench`), version, and architecture with
   bounded system tools, then invoke apt or pacman with fixed arguments and a
   clean environment. Explicit beta-to-stable downgrades remain supported.
7. Verify the package manager's installed version before reporting success.
   The native restart action closes IPC and reaps the old worker before launching
   the new app. It uses the shell's frozen startup path, not `/proc/self/exe`,
   which points to a deleted ELF after package replacement. A restart failure keeps an explicit quit/reopen action visible.

Metadata inspection has 30-second, 64 KiB, memory, and CPU bounds. Installation
has a 30-minute bound and bounded retained diagnostics; Debian lock contention
waits at most two minutes. Authentication refusal does not install anything.
Package-manager failures are surfaced in both modes without claiming rollback:
if a transaction is interrupted or the OS reports partial configuration, repair
it with apt/pacman before retrying. Closing Workbench is not a cancellation of
an already-authorized system package transaction.

The app never receives general root authority and never directly replaces
`/usr` files. Its private root staging directory is cleaned on normal completion
or failure; package ownership and OS dependency handling remain authoritative.
Data under the XDG data directory is not touched.
