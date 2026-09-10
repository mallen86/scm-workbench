"""Secure cutting template deletion contracts for native and browser modes."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server


class TemplateDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-template-delete-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.scm = self.root / "scm"
        self.extras = self.root / "extras"
        self.outside = self.root / "outside.dxf"
        self.data.mkdir()
        for repo in (self.scm, self.extras):
            (repo / "cutting_templates/dxf").mkdir(parents=True)
            (repo / "cutting_templates/borderless/dxf").mkdir(parents=True)
        self.outside.write_text("outside", encoding="utf-8")

        self.old = {name: getattr(server, name) for name in (
            "DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
            "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE", "_IPC_MODE",
        )}
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = self.data / "offsets.json"
        server.UPDATE_STATE_FILE = self.data / "updates.json"
        server._IPC_MODE = False
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({"scm_dir": str(self.scm), "extras_dir": str(self.extras)})
        server.save_settings(settings)
        server.invalidate_manifest_cache()

    def tearDown(self):
        for name, value in self.old.items():
            setattr(server, name, value)
        server.invalidate_manifest_cache()
        self.temp.cleanup()

    @staticmethod
    def call(path, **extra):
        return ipc.dispatch({
            "id": "delete-template", "method": "template.delete",
            "params": {"path": str(path), **extra},
        })

    def test_native_deletes_default_and_borderless_dxf_files(self):
        for path in (
            self.scm / "cutting_templates/dxf/legal-standard-v1.dxf",
            self.extras / "cutting_templates/borderless/dxf/legal-extra-v1.dxf",
        ):
            path.write_text("dxf", encoding="utf-8")
            result = self.call(path)
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"], {
                "ok": True, "errors": [], "name": path.name,
            })
            self.assertFalse(path.exists())

    def test_native_rejects_wrong_envelopes_and_unsafe_paths(self):
        self.assertIn("template.delete", ipc.ALLOWED_METHODS)
        for params in ({}, {"path": "x", "extra": True}, {"path": 3}):
            result = ipc.dispatch({
                "id": "delete-template", "method": "template.delete", "params": params,
            })
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "bad_request")
        for path in ("bad\npath", "x" * (server.ACTION_PATH_MAX_BYTES + 1), "\ud800"):
            result = self.call(path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "bad_request")

    def test_delete_is_limited_to_regular_dxf_files_in_exact_template_folders(self):
        studio = self.scm / "cutting_templates/custom.studio3"
        studio.write_text("studio", encoding="utf-8")
        nested = self.scm / "cutting_templates/dxf/nested/custom.dxf"
        nested.parent.mkdir()
        nested.write_text("nested", encoding="utf-8")
        directory = self.scm / "cutting_templates/dxf/folder.dxf"
        directory.mkdir()
        for path in (studio, nested, directory, self.outside):
            result = self.call(path)
            self.assertTrue(result["ok"])
            self.assertFalse(result["result"]["ok"])
            self.assertTrue(path.exists())

    def test_symlink_template_is_rejected_without_touching_target(self):
        target = self.root / "target.dxf"
        target.write_text("target", encoding="utf-8")
        link = self.scm / "cutting_templates/dxf/link.dxf"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable: {error}")
        result = self.call(link)
        self.assertTrue(result["ok"])
        self.assertFalse(result["result"]["ok"])
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "target")

    def _http(self, body):
        request = urllib.request.Request(
            self.base + "/api/templates/delete",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_browser_route_matches_native_and_packaged_http_is_closed(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            browser_path = self.scm / "cutting_templates/dxf/browser.dxf"
            browser_path.write_text("browser", encoding="utf-8")
            status, result = self._http({"path": str(browser_path)})
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertFalse(browser_path.exists())

            with mock.patch.object(server, "_IPC_MODE", True), \
                    mock.patch.object(server, "delete_template",
                                      side_effect=AssertionError("path work")):
                status, result = self._http({"path": str(self.outside)})
            self.assertEqual(status, 403)
            self.assertFalse(result["ok"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
