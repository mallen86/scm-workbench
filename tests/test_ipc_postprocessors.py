"""Native registry contract for Advanced image post-processors."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, postprocessing, server


SOURCE = "def process_image(image_path, context):\n    return None\n"


class IpcPostprocessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ipc-postprocessors-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.repo = self.root / "scm"
        (self.repo / "game" / "front").mkdir(parents=True)
        self.data.mkdir()
        self.settings = {
            "scm_dir": str(self.repo), "extras_dir": str(self.repo),
            "ui_mode": "advanced",
        }
        self.old = {name: getattr(server, name) for name in (
            "DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
        )}
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.invalidate_manifest_cache()
        self.patch = mock.patch.object(server, "load_settings", side_effect=lambda: dict(self.settings))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        for name, value in self.old.items():
            setattr(server, name, value)
        server.invalidate_manifest_cache()
        self.temp.cleanup()

    def call(self, method, params=None):
        response = ipc.dispatch({"id": "post", "method": method, "params": params or {}})
        self.assertTrue(response["ok"], response)
        return response["result"]

    def test_save_list_get_trust_edit_and_delete(self):
        saved = self.call("postprocessors.save", {
            "name": "Example", "source": SOURCE, "requirements": "",
            "processor_id": None, "expected_revision": None,
        })
        self.assertTrue(saved["ok"])
        processor = saved["processor"]

        listed = self.call("postprocessors.list")
        self.assertEqual(len(listed["processors"]), 1)
        self.assertNotIn("source", listed["processors"][0])
        self.assertTrue(listed["processors"][0]["environment_ready"])

        detail = self.call("postprocessors.get", {"processor_id": processor["id"]})
        self.assertEqual(detail["source"], SOURCE)
        self.assertEqual(detail["environment"]["status"], "ready")
        fingerprint = detail["environment_fingerprint"]

        stale = self.call("postprocessors.trust", {
            "processor_id": processor["id"],
            "revision_hash": processor["revision"],
            "environment_fingerprint": "0" * 64,
        })
        self.assertFalse(stale["ok"])
        trusted = self.call("postprocessors.trust", {
            "processor_id": processor["id"],
            "revision_hash": processor["revision"],
            "environment_fingerprint": fingerprint,
        })
        self.assertTrue(trusted["ok"])

        edited = self.call("postprocessors.save", {
            "name": "Example", "source": SOURCE + "\n# revised\n", "requirements": "",
            "processor_id": processor["id"],
            "expected_revision": processor["revision"],
        })["processor"]
        self.assertNotEqual(edited["revision"], processor["revision"])
        self.assertFalse(edited["trusted"])
        deleted = self.call("postprocessors.delete", {
            "processor_id": processor["id"],
            "expected_revision": edited["revision"],
        })
        self.assertTrue(deleted["ok"])
        self.assertEqual(self.call("postprocessors.list")["processors"], [])

    def test_stale_runtime_environment_does_not_hide_or_block_source_edits(self):
        saved = self.call("postprocessors.save", {
            "name": "Libraries", "source": SOURCE, "requirements": "demo==1.0",
            "processor_id": None, "expected_revision": None,
        })["processor"]
        store = postprocessing.ProcessorStore(self.data, self.repo)
        metadata_path = store._processor(saved["id"]) / "metadata.json"
        metadata = store._metadata(saved["id"])
        metadata.update({
            "trusted": saved["revision"], "environment": "a" * 64,
            "trusted_tree_digest": "b" * 64,
            "installed_revision": saved["revision"],
            "installed_environment": "a" * 64,
            "installed_tree_digest": "b" * 64,
            "lock_hash": "c" * 64,
        })
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with mock.patch("scm_workbench.postprocessing.environment_fingerprint", return_value="d" * 64):
            detail = self.call("postprocessors.get", {"processor_id": saved["id"]})
            listed = self.call("postprocessors.list")["processors"][0]
            edited = self.call("postprocessors.save", {
                "name": "Libraries", "source": SOURCE + "\n# still editable\n",
                "requirements": "demo==1.0", "processor_id": saved["id"],
                "expected_revision": saved["revision"],
            })["processor"]

        self.assertEqual(detail["source"], SOURCE)
        self.assertEqual(detail["environment"]["status"], "stale")
        self.assertFalse(detail["environment_ready"])
        self.assertFalse(detail["trusted"])
        self.assertEqual(listed["environment_status"], "stale")
        self.assertNotEqual(edited["revision"], saved["revision"])

    def test_private_import_is_bounded_and_simple_mode_denies_mutation(self):
        source = self.root / "chosen.py"
        source.write_text(SOURCE, encoding="utf-8")
        imported = self.call("postprocessors.import_selected", {"source_path": str(source)})
        self.assertTrue(imported["ok"])

        self.settings["ui_mode"] = "simple"
        denied = self.call("postprocessors.save", {
            "name": "Denied", "source": SOURCE, "requirements": "",
            "processor_id": None, "expected_revision": None,
        })
        self.assertFalse(denied["ok"])
        self.assertIn("Advanced", denied["errors"][0])

    def test_invalid_native_parameters_are_protocol_errors(self):
        response = ipc.dispatch({
            "id": "post", "method": "postprocessors.save",
            "params": {"name": "Missing source"},
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "bad_request")


if __name__ == "__main__":
    unittest.main()
