import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scm_workbench import server


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_linux_deb.py"
SPEC = importlib.util.spec_from_file_location("build_linux_deb", SCRIPT)
build_linux_deb = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(build_linux_deb)

ARCH_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_linux_arch.py"
ARCH_SPEC = importlib.util.spec_from_file_location("build_linux_arch", ARCH_SCRIPT)
build_linux_arch = importlib.util.module_from_spec(ARCH_SPEC)
assert ARCH_SPEC.loader is not None
ARCH_SPEC.loader.exec_module(build_linux_arch)


class LinuxPackagingTests(unittest.TestCase):
    def test_semver_maps_to_debian_ordering(self):
        self.assertEqual(build_linux_deb.debian_version("1.2.3"), "1.2.3")
        self.assertEqual(
            build_linux_deb.debian_version("1.2.3-beta.4+build.7"),
            "1.2.3~beta.4+build.7",
        )
        for invalid in ("v1.2.3", "1.2", "1.02.3", "1.2.3-beta..1"):
            with self.subTest(version=invalid), self.assertRaises(ValueError):
                build_linux_deb.debian_version(invalid)

    def test_metadata_uses_immutable_payload_and_runtime_dependencies(self):
        with tempfile.TemporaryDirectory(prefix="scm-linux-package-test-") as temp:
            root = Path(temp) / "root"
            stage = Path(temp) / "stage"
            (root / "tauri/icons").mkdir(parents=True)
            (root / "tauri/icons/512.png").write_bytes(b"png")
            payload = stage / build_linux_deb.PACKAGE_ROOT
            payload.mkdir(parents=True)
            (payload / "scm-workbench").write_bytes(b"binary")

            with patch.object(build_linux_deb, "ROOT", root):
                build_linux_deb.write_metadata(stage, "0.9.0-beta.1")

            control = (stage / "DEBIAN/control").read_text(encoding="utf-8")
            self.assertIn("Version: 0.9.0~beta.1", control)
            self.assertIn("Architecture: amd64", control)
            self.assertIn("libwebkit2gtk-4.1-0", control)
            self.assertIn("libgtk-3-0", control)
            self.assertIn("xdg-utils", control)
            desktop = (stage / "usr/share/applications/scm-workbench.desktop").read_text(
                encoding="utf-8"
            )
            self.assertIn("Exec=/usr/bin/scm-workbench", desktop)
            self.assertIn("Icon=scm-workbench", desktop)

    def test_arch_version_preserves_stable_and_beta_upgrade_order_shape(self):
        self.assertEqual(build_linux_arch.arch_version("1.2.3"), "1.2.3")
        self.assertEqual(
            build_linux_arch.arch_version("1.2.3-beta.4+build.7"),
            "1.2.3beta.4",
        )
        self.assertEqual(build_linux_arch.arch_version("1.2.3-1"), "1.2.3pre.1")
        self.assertEqual(build_linux_arch.arch_version("1.2.3-rc-one.2"),
                         "1.2.3rc_one.2")
        for invalid in ("v1.2.3", "1.2", "1.02.3", "1.2.3-beta..1"):
            with self.subTest(version=invalid), self.assertRaises(ValueError):
                build_linux_arch.arch_version(invalid)

    def test_arch_recipe_and_staged_payload_match_linux_layout(self):
        with tempfile.TemporaryDirectory(prefix="scm-arch-package-test-") as temp:
            root = Path(temp) / "root"
            bundle = Path(temp) / "bundle"
            binary = Path(temp) / "scm-workbench"
            (root / "scm_workbench").mkdir(parents=True)
            (root / "scm_workbench/server.py").write_text("# worker\n", encoding="utf-8")
            (root / "scm_workbench/cached.pyc").write_bytes(b"cache")
            (root / "ui").mkdir()
            (root / "ui/index.html").write_text("app", encoding="utf-8")
            (root / "tauri/icons").mkdir(parents=True)
            (root / "tauri/icons/512.png").write_bytes(b"png")
            (root / "LICENSE.md").write_text("license", encoding="utf-8")
            template = root / "packaging/arch/PKGBUILD.in"
            template.parent.mkdir(parents=True)
            template.write_text(
                (Path(__file__).resolve().parents[1] / "packaging/arch/PKGBUILD.in")
                .read_text(encoding="utf-8"), encoding="utf-8",
            )
            runtime_python = bundle / "runtime/python/install/bin/python3.13"
            runtime_python.parent.mkdir(parents=True)
            runtime_python.write_bytes(b"python")
            binary.write_bytes(b"binary")

            with patch.object(build_linux_arch, "ROOT", root), \
                    patch.object(build_linux_arch, "PKGBUILD_TEMPLATE", template):
                work = build_linux_arch.stage_payload(binary, bundle)
                package_version = build_linux_arch.render_pkgbuild(
                    work, "0.9.0-beta.1",
                )

            payload = work / "root/usr/lib/scm-workbench"
            self.assertEqual(package_version, "0.9.0beta.1")
            self.assertEqual((payload / "scm-workbench").read_bytes(), b"binary")
            self.assertTrue((payload / "runtime/python/install/bin/python3.13").is_file())
            self.assertTrue((payload / "app/scm_workbench/server.py").is_file())
            self.assertFalse((payload / "app/scm_workbench/cached.pyc").exists())
            launcher = work / "root/usr/bin/scm-workbench"
            self.assertTrue(launcher.is_symlink())
            self.assertEqual(launcher.readlink().as_posix(),
                             "../lib/scm-workbench/scm-workbench")
            self.assertTrue((work / "root/usr/share/applications/scm-workbench.desktop").is_file())
            self.assertTrue((work / "root/usr/share/icons/hicolor/512x512/apps/scm-workbench.png").is_file())
            self.assertEqual(
                (work / "root/usr/share/licenses/scm-workbench/LICENSE.md")
                .read_text(encoding="utf-8"), "license",
            )
            pkgbuild = (work / "PKGBUILD").read_text(encoding="utf-8")
            self.assertIn("pkgver=0.9.0beta.1", pkgbuild)
            self.assertIn("arch=('x86_64')", pkgbuild)
            self.assertIn("license=('MIT')", pkgbuild)
            self.assertIn("depends=('webkit2gtk-4.1' 'gtk3' 'xdg-utils')", pkgbuild)
            self.assertIn("options=('!strip' '!debug')", pkgbuild)
            self.assertNotIn(build_linux_arch.PKGBUILD_VERSION_TOKEN, pkgbuild)
            self.assertEqual(
                build_linux_arch.ASSET_NAME,
                "scm-workbench-linux-arch-x86_64.pkg.tar.zst",
            )

    def test_linux_bundle_shape_requires_complete_regular_payload(self):
        with tempfile.TemporaryDirectory(prefix="scm-linux-shape-test-") as temp:
            bundle = Path(temp) / "scm-workbench"
            (bundle / "app/scm_workbench").mkdir(parents=True)
            (bundle / "app/ui").mkdir()
            (bundle / "runtime").mkdir()
            (bundle / "scm-workbench").write_bytes(b"binary")
            with patch.object(server.sys, "platform", "linux"):
                self.assertTrue(server._bundle_shape(bundle))
                (bundle / "runtime").rmdir()
                self.assertFalse(server._bundle_shape(bundle))


if __name__ == "__main__":
    unittest.main()
