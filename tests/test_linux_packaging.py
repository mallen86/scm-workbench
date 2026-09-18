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
