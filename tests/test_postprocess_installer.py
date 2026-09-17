"""Offline, hash-locked dependency installer contract."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import postprocess_installer


class InstallerTests(unittest.TestCase):
    def test_resolution_download_and_install_are_hash_locked_and_offline(self):
        with tempfile.TemporaryDirectory(prefix="postprocess-installer-") as temp:
            stage = Path(temp) / "stage"
            stage.mkdir()
            target = stage / "site-packages"
            report = stage / "resolve-report.json"
            lock = stage / "requirements.lock"
            wheelhouse = stage / "wheels"
            manifest = stage / "installer-manifest.json"
            manifest.write_text(json.dumps({
                "stage": str(stage), "target": str(target),
                "requirements": ["demo==1.0"], "resolve_report": str(report),
                "lock_file": str(lock), "wheelhouse": str(wheelhouse),
            }), encoding="utf-8")
            wheel_bytes = b"fixture wheel bytes"
            digest = hashlib.sha256(wheel_bytes).hexdigest()
            calls = []

            def fake_run(argv, label):
                calls.append((argv, label))
                if "--dry-run" in argv:
                    report.write_text(json.dumps({"install": [{
                        "metadata": {"name": "demo", "version": "1.0"},
                        "download_info": {
                            "url": "https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl",
                            "archive_info": {"hashes": {"sha256": digest}},
                        },
                    }]}), encoding="utf-8")
                elif "download" in argv:
                    (wheelhouse / "demo-1.0-py3-none-any.whl").write_bytes(wheel_bytes)
                else:
                    (target / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
                    (stage / "install-report.json").write_text('{"install": []}', encoding="utf-8")

            with mock.patch.object(postprocess_installer, "_run", side_effect=fake_run):
                postprocess_installer.install(manifest)

            self.assertEqual(len(calls), 3)
            self.assertIn("--dry-run", calls[0][0])
            self.assertIn("--require-hashes", calls[1][0])
            self.assertIn("--no-deps", calls[1][0])
            self.assertIn("--no-index", calls[2][0])
            self.assertIn("--require-hashes", calls[2][0])
            self.assertEqual(
                lock.read_text(encoding="utf-8"),
                f"demo==1.0 --hash=sha256:{digest}\n",
            )

    def test_download_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="postprocess-wheel-") as temp:
            wheelhouse = Path(temp)
            (wheelhouse / "demo.whl").write_bytes(b"wrong")
            with self.assertRaises(postprocess_installer.InstallerError):
                postprocess_installer._verify_downloads(wheelhouse, {"0" * 64})


if __name__ == "__main__":
    unittest.main()
