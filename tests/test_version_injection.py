"""Release-version injection accepts SemVer beta tags across package metadata."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class VersionInjectionTests(unittest.TestCase):
    def test_beta_tag_is_pinned_into_every_version_slot(self):
        with tempfile.TemporaryDirectory(prefix="scm-version-injection-") as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            (root / "scm_workbench").mkdir()
            (root / "tauri").mkdir()
            shutil.copy2(ROOT / "scripts" / "inject_version.py",
                         root / "scripts" / "inject_version.py")
            (root / "scm_workbench" / "_version.py").write_text(
                '__version__ = "0.0.0"\n', encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname = "fixture"\nversion = "0.0.0"\n',
                encoding="utf-8",
            )
            (root / "tauri" / "tauri.conf.json").write_text(
                json.dumps({"version": "0.0.0"}, indent=2) + "\n", encoding="utf-8",
            )
            (root / "tauri" / "Cargo.toml").write_text(
                '[package]\nname = "scm-workbench"\nversion = "0.0.0"\n',
                encoding="utf-8",
            )
            (root / "tauri" / "Cargo.lock").write_text(
                '[[package]]\nname = "scm-workbench"\nversion = "0.0.0"\n',
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(root / "scripts" / "inject_version.py"),
                 "v0.9.0-beta.1"],
                cwd=root, capture_output=True, text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            expected = "0.9.0-beta.1"
            self.assertIn(f'__version__ = "{expected}"',
                          (root / "scm_workbench" / "_version.py").read_text())
            self.assertIn(f'version = "{expected}"',
                          (root / "pyproject.toml").read_text())
            self.assertEqual(json.loads(
                (root / "tauri" / "tauri.conf.json").read_text()
            )["version"], expected)
            self.assertIn(f'version = "{expected}"',
                          (root / "tauri" / "Cargo.toml").read_text())
            self.assertIn(f'version = "{expected}"',
                          (root / "tauri" / "Cargo.lock").read_text())

    def test_malformed_semver_tags_are_rejected_before_writing(self):
        with tempfile.TemporaryDirectory(prefix="scm-version-invalid-") as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            script = root / "scripts" / "inject_version.py"
            shutil.copy2(ROOT / "scripts" / "inject_version.py", script)
            for value in ("v1.2", "v01.2.3", "v1.2.3-beta..1",
                          "v1.2.3-beta.01", "v1.2.3+", "v1.2.3.4"):
                with self.subTest(value=value):
                    result = subprocess.run(
                        [sys.executable, str(script), value], cwd=root,
                        capture_output=True, text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("is not a usable version", result.stderr)


if __name__ == "__main__":
    unittest.main()
