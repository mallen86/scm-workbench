"""Native template and managed-directory artifact contracts."""

import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server
from test_phase0_baseline import Phase0Fixture


class ArtifactIpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-artifacts-")
        cls.fixture = Phase0Fixture(Path(cls.temp.name))
        cls.fixture.data.mkdir()
        cls.old = {name: getattr(server, name) for name in (
            "DATA_DIR", "UI_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
            "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE",
        )}
        server.DATA_DIR = cls.fixture.data
        server.UI_DIR = Path(cls.temp.name) / "ui"
        server.UI_DIR.mkdir()
        server.SETTINGS_FILE = cls.fixture.data / "settings.json"
        server.JOBS_FILE = cls.fixture.data / "jobs.json"
        server.LOGS_DIR = cls.fixture.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = cls.fixture.data / "offsets.json"
        server.UPDATE_STATE_FILE = cls.fixture.data / "updates.json"
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({"scm_dir": str(cls.fixture.scm), "extras_dir": str(cls.fixture.extras)})
        server.save_settings(settings)
        cls.httpd = server.start_http("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        for name, value in cls.old.items():
            setattr(server, name, value)
        cls.temp.cleanup()

    def http_template(self, paper="letter", card="standard", borderless=False):
        query = urllib.parse.urlencode({
            "paper": paper, "card": card, "borderless": "1" if borderless else "0",
        })
        with urllib.request.urlopen(self.base + "/api/template?" + query, timeout=5) as response:
            return json.loads(response.read())

    @staticmethod
    def native_template(paper="letter", card="standard", borderless=False):
        return ipc.dispatch({"id": "artifact", "method": "template.resolve", "params": {
            "paper": paper, "card": card, "borderless": borderless,
        }})

    def test_template_parity_precedence_and_version_selection(self):
        scm_dir = self.fixture.scm / "cutting_templates"
        extras_dir = self.fixture.extras / "cutting_templates"
        for version in (1, 3):
            (scm_dir / f"letter-standard-v{version}.studio3").write_text("scm")
        (extras_dir / "letter-standard-v9.studio3").write_text("extras")
        http = self.http_template()
        native = self.native_template()
        self.assertEqual(native, {"id": "artifact", "ok": True, "result": http})
        self.assertEqual(http["repo"], "silhouette-card-maker")
        self.assertEqual(http["name"], "letter-standard-v3.studio3")

        (extras_dir / "borderless").mkdir()
        (extras_dir / "borderless" / "letter-extra-borderless-v2.studio3").write_text("extra")
        self.assertEqual(self.http_template(card="extra", borderless=True)["repo"], "scm-extras")
        self.assertEqual(self.native_template(card="extra", borderless=True)["ok"], True)

    def test_template_symlink_escape_is_ignored(self):
        outside = self.fixture.outside / "letter2-standard-v99.studio3"
        outside.write_text("secret")
        target = self.fixture.scm / "cutting_templates" / outside.name
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable: %s" % error)
        result = self.native_template(paper="letter2")
        self.assertFalse(result["result"]["ok"])
        self.assertNotIn("v99", json.dumps(result))

    def test_template_and_file_validation_is_strict(self):
        bad_templates = [
            {}, {"paper": "letter", "card": "standard", "borderless": False, "extra": 1},
            {"paper": "", "card": "standard", "borderless": False},
            {"paper": "letter", "card": "standard", "borderless": 1},
            {"paper": "é" * 65, "card": "standard", "borderless": False},
        ]
        for params in bad_templates:
            result = ipc.dispatch({"id": "bad", "method": "template.resolve", "params": params})
            self.assertEqual(result["error"]["code"], "bad_request")
        for params in ({}, {"path": "x", "images_only": 0},
                       {"path": "x" + "é" * 2048, "images_only": False}):
            result = ipc.dispatch({"id": "bad", "method": "file.list", "params": params})
            self.assertEqual(result["error"]["code"], "bad_request")

    def test_file_list_images_security_missing_and_no_side_effect(self):
        directory = self.fixture.scm / "game" / "front"
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in directory.iterdir()}
        result = ipc.dispatch({"id": "list", "method": "file.list", "params": {
            "path": str(directory), "images_only": True,
        }})
        self.assertTrue(result["result"]["exists"])
        self.assertEqual([item["name"] for item in result["result"]["items"]], ["card.png"])
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in directory.iterdir()}
        self.assertEqual(before, after)

        # An entry that resolves outside the sandbox is never disclosed, and
        # makes the listing explicitly incomplete so UI code cannot authorize
        # a destructive follow-up from the remaining names.
        outside_image = self.fixture.outside / "escaped.png"
        outside_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
        escaped_link = directory / "escaped.png"
        try:
            escaped_link.symlink_to(outside_image)
        except (OSError, NotImplementedError):
            pass
        else:
            escaped = ipc.dispatch({"id": "list", "method": "file.list", "params": {
                "path": str(directory), "images_only": True,
            }})["result"]
            self.assertTrue(escaped["truncated"])
            self.assertNotIn("escaped.png", [item["name"] for item in escaped["items"]])
        for path in (str(self.fixture.outside / "missing"), str(directory / ".." / ".." / ".." / "outside")):
            error = ipc.dispatch({"id": "list", "method": "file.list", "params": {
                "path": path, "images_only": False,
            }})
            self.assertFalse(error["ok"])
        missing = ipc.dispatch({"id": "list", "method": "file.list", "params": {
            "path": str(directory / "does-not-exist"), "images_only": False,
        }})
        self.assertEqual(missing, {"id": "list", "ok": True, "result": {
            "exists": False, "items": [], "truncated": False, "scanned": 0, "found": 0,
        }})
        file_error = ipc.dispatch({"id": "list", "method": "file.list", "params": {
            "path": str(directory / "card.png"), "images_only": False,
        }})
        self.assertEqual(file_error["error"]["code"], "not_directory")

    def test_file_list_bounds_are_explicit(self):
        directory = self.fixture.scm / "game" / "bounded"
        directory.mkdir()
        for i in range(10):
            (directory / f"{i}.txt").write_text(str(i))
        with mock.patch.object(server, "FILE_LIST_MAX_SCANNED", 5), \
             mock.patch.object(server, "FILE_LIST_MAX_ITEMS", 3):
            result = ipc.dispatch({"id": "list", "method": "file.list", "params": {
                "path": str(directory), "images_only": False,
            }})
        value = result["result"]
        self.assertEqual(len(value["items"]), 3)
        self.assertEqual(value["scanned"], 5)
        self.assertEqual(value["found"], 5)
        self.assertTrue(value["truncated"])
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()),
                             server.FILE_LIST_MAX_RESULT_BYTES)

    def test_file_list_result_budget_is_enforced(self):
        directory = self.fixture.scm / "game" / "encoded-bound"
        directory.mkdir()
        for i in range(8):
            (directory / ("x" * 180 + str(i))).write_text("x")
        with mock.patch.object(server, "FILE_LIST_MAX_RESULT_BYTES", 512):
            result = server.list_files(directory, False)
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(encoded), 512)


if __name__ == "__main__":
    unittest.main()
