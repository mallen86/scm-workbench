"""Native registry contract for built-in and user image post-processors."""

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
        self.assertEqual(len(listed["processors"]), 3)
        custom = next(item for item in listed["processors"] if item["id"] == processor["id"])
        bundled = next(item for item in listed["processors"] if item["id"] == server.BUILTIN_SIMPLE_UPSCALER_ID)
        self.assertNotIn("source", custom)
        self.assertTrue(custom["environment_ready"])
        self.assertTrue(bundled["bundled"])
        self.assertTrue(bundled["trusted"])
        self.assertTrue(bundled["ready_to_run"])
        optional = next(item for item in listed["processors"] if item["id"] == server.BUILTIN_ADVANCED_UPSCALER_ID)
        self.assertTrue(optional["bundled"])
        self.assertTrue(optional["optional_model"])
        self.assertTrue(optional["trusted"])
        self.assertFalse(optional["ready_to_run"])
        self.assertEqual(optional["requirements"], list(server.advanced_model.REQUIREMENTS))
        detail = self.call("postprocessors.get", {"processor_id": processor["id"]})
        self.assertEqual(detail["source"], SOURCE)
        self.assertEqual(detail["environment"]["status"], "ready")
        self.assertEqual([item["revision"] for item in detail["revisions"]],
                         [processor["revision"]])
        self.assertTrue(detail["revisions"][0]["active"])
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
        current = self.call("postprocessors.get", {"processor_id": processor["id"]})
        self.assertEqual({item["revision"] for item in current["revisions"]},
                         {processor["revision"], edited["revision"]})
        historical = self.call("postprocessors.get", {
            "processor_id": processor["id"], "revision_hash": processor["revision"],
        })
        self.assertEqual(historical["source"], SOURCE)
        self.assertFalse(historical["active"])
        self.assertNotIn("environment", historical)
        deleted = self.call("postprocessors.delete", {
            "processor_id": processor["id"],
            "expected_revision": edited["revision"],
        })
        self.assertTrue(deleted["ok"])
        remaining = self.call("postprocessors.list")["processors"]
        self.assertEqual({item["id"] for item in remaining},
                         {server.BUILTIN_SIMPLE_UPSCALER_ID, server.BUILTIN_ADVANCED_UPSCALER_ID})

    def test_bundled_upscaler_is_read_only_but_can_be_duplicated(self):
        detail = self.call("postprocessors.get", {
            "processor_id": server.BUILTIN_SIMPLE_UPSCALER_ID,
        })
        self.assertTrue(detail["bundled"])
        self.assertEqual(detail["requirements"], [])
        self.assertIn("Image.Resampling.LANCZOS", detail["source"])

        saved = self.call("postprocessors.save", {
            "name": detail["name"], "source": detail["source"], "requirements": "",
            "processor_id": detail["id"], "expected_revision": detail["revision"],
        })
        self.assertFalse(saved["ok"])
        self.assertIn("read-only", saved["errors"][0])
        deleted = self.call("postprocessors.delete", {
            "processor_id": detail["id"], "expected_revision": detail["revision"],
        })
        self.assertFalse(deleted["ok"])
        self.assertIn("cannot be deleted", deleted["errors"][0])

        duplicate = self.call("postprocessors.duplicate", {
            "processor_id": detail["id"], "name": "Custom Upscaler",
            "expected_revision": detail["revision"],
        })
        self.assertTrue(duplicate["ok"])
        self.assertFalse(duplicate["processor"]["bundled"])
        self.assertFalse(duplicate["processor"]["trusted"])

    def test_advanced_upscaler_can_duplicate_source_without_inheriting_model(self):
        built_in = self.call("postprocessors.get", {
            "processor_id": server.BUILTIN_ADVANCED_UPSCALER_ID,
        })
        stale = self.call("postprocessors.duplicate", {
            "processor_id": built_in["id"], "name": "Old revision copy",
            "expected_revision": "0" * 64,
        })
        self.assertFalse(stale["ok"])
        self.assertIn("stale", stale["errors"][0])

        duplicated = self.call("postprocessors.duplicate", {
            "processor_id": built_in["id"], "name": "Custom AI source",
            "expected_revision": built_in["revision"],
        })
        self.assertTrue(duplicated["ok"])
        copy = duplicated["processor"]
        self.assertNotEqual(copy["id"], built_in["id"])
        self.assertEqual(copy["source"], built_in["source"])
        self.assertEqual(copy["requirements"], built_in["requirements"])
        self.assertFalse(copy["bundled"])
        self.assertFalse(copy["optional_model"])
        self.assertFalse(copy["trusted"])
        detail = self.call("postprocessors.get", {"processor_id": copy["id"]})
        self.assertFalse(detail["ready_to_run"])
        self.assertFalse(detail["optional_model"])

        self.settings["ui_mode"] = "simple"
        forbidden = self.call("postprocessors.duplicate", {
            "processor_id": built_in["id"], "name": "Not allowed in Simple mode",
            "expected_revision": built_in["revision"],
        })
        self.assertFalse(forbidden["ok"])
        self.assertIn("Advanced mode", forbidden["errors"][0])

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
            listed = next(item for item in self.call("postprocessors.list")["processors"]
                          if item["id"] == saved["id"])
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

    def test_guide_returns_version_bound_rendered_markdown(self):
        guide = self.root / "guide.md"
        guide.write_text(
            "# Processor guide\n\nUse **trusted** code.\n\n```python\nprint('<script>')\n```\n\n<script>bad()</script>\n",
            encoding="utf-8",
        )
        with mock.patch.object(server, "POSTPROCESS_GUIDE_FILE", guide):
            result = self.call("postprocessors.guide")
        self.assertEqual(result["title"], "Image post-processing guide")
        self.assertEqual(result["version"], server.SERVER_VERSION)
        self.assertIn("<h1>Processor guide</h1>", result["body"])
        self.assertIn("<b>trusted</b>", result["body"])
        self.assertIn("<pre><code>", result["body"])
        self.assertIn("&lt;script&gt;", result["body"])
        self.assertNotIn("<script>", result["body"])
        self.assertNotIn("```", result["body"])

        oversized = self.root / "oversized-guide.md"
        oversized.write_bytes(b"x" * (server.POSTPROCESS_GUIDE_MAX_BYTES + 1))
        with mock.patch.object(server, "POSTPROCESS_GUIDE_FILE", oversized):
            rejected = self.call("postprocessors.guide")
        self.assertFalse(rejected["ok"])
        self.assertIn("too large", rejected["errors"][0])

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
        item = imported["processor"]
        for method, params in (
            ("postprocessors.duplicate", {
                "processor_id": item["id"], "name": "Denied copy",
                "expected_revision": item["revision"],
            }),
            ("postprocessors.trust", {
                "processor_id": item["id"], "revision_hash": item["revision"],
                "environment_fingerprint": None,
            }),
            ("postprocessors.delete", {
                "processor_id": item["id"], "expected_revision": item["revision"],
            }),
        ):
            result = self.call(method, params)
            self.assertFalse(result["ok"])
            self.assertIn("Advanced", result["errors"][0])
        listed = self.call("postprocessors.list")["processors"]
        self.assertTrue(next(row for row in listed
                             if row["id"] == server.BUILTIN_SIMPLE_UPSCALER_ID)["ready_to_run"])

    def test_only_fixed_optional_asset_can_be_removed_in_simple_mode(self):
        self.settings["ui_mode"] = "simple"
        optional = self.call("postprocessors.get", {
            "processor_id": server.BUILTIN_ADVANCED_UPSCALER_ID,
        })
        self.assertTrue(optional["bundled"])
        self.assertFalse(optional["environment_ready"])
        rejected = self.call("postprocessors.optional.remove", {
            "processor_id": server.BUILTIN_SIMPLE_UPSCALER_ID,
            "revision_hash": optional["revision"],
        })
        self.assertFalse(rejected["ok"])
        stale = self.call("postprocessors.optional.remove", {
            "processor_id": optional["id"], "revision_hash": "0" * 64,
        })
        self.assertFalse(stale["ok"])
        store = postprocessing.ProcessorStore(self.data, self.repo)
        installed = "a" * 64
        environment = store.root / "environments" / installed
        environment.mkdir(parents=True)
        (environment / "model.onnx").write_bytes(b"pretend installed asset")
        metadata = store._metadata(optional["id"])
        metadata["installed_environment"] = installed
        (store._processor(optional["id"]) / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        removed = self.call("postprocessors.optional.remove", {
            "processor_id": optional["id"], "revision_hash": optional["revision"],
        })
        self.assertTrue(removed["ok"])
        self.assertTrue(removed["space_reclaimed"])
        self.assertFalse(environment.exists())
        self.assertFalse(self.call("postprocessors.get", {
            "processor_id": optional["id"],
        })["ready_to_run"])

    def test_missing_optional_model_downgrades_readiness_without_hiding_source(self):
        store = server._postprocessor_store()
        item = store.get(server.BUILTIN_ADVANCED_UPSCALER_ID, include_source=False)
        forged = {"processor": {**item, "environment_ready": True, "ready_to_run": True},
                  "environment": {"ready": True, "path": str(self.root), "status": "ready"}}
        with mock.patch.object(postprocessing.ProcessorStore, "status", return_value=forged):
            result = server.postprocessor_status(item["id"])
        self.assertTrue(result["processor"]["trusted"])
        self.assertFalse(result["processor"]["ready_to_run"])
        self.assertEqual(result["processor"]["environment_status"], "stale")
        self.assertTrue(server.postprocessor_get(item["id"])["optional_model"])

    def test_invalid_native_parameters_are_protocol_errors(self):
        response = ipc.dispatch({
            "id": "post", "method": "postprocessors.save",
            "params": {"name": "Missing source"},
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "bad_request")

        response = ipc.dispatch({
            "id": "post", "method": "postprocessors.optional.remove",
            "params": {"processor_id": server.BUILTIN_ADVANCED_UPSCALER_ID},
        })
        self.assertEqual(response["error"]["code"], "bad_request")

        response = ipc.dispatch({
            "id": "post", "method": "postprocessors.guide", "params": {"unexpected": True},
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "bad_request")

        response = ipc.dispatch({
            "id": "post", "method": "postprocessors.get",
            "params": {"processor_id": "a" * 32, "revision_hash": "not-a-revision"},
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "bad_request")


if __name__ == "__main__":
    unittest.main()
