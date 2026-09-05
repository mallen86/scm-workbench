"""Settings-set validation and HTTP/native parity contracts."""

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


class SettingsIpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-settings-")
        self.data = Path(self.temp.name)
        self.saved = {
            name: getattr(server, name)
            for name in ("DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
                         "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE")
        }
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = self.data / "offsets.json"
        server.UPDATE_STATE_FILE = self.data / "update-state.json"
        server.MANIFEST_CACHE.clear()
        server._INFO_SNAP.clear()
        server._REPOS_MTIME.clear()
        server.save_settings(json.loads(json.dumps(server.DEFAULT_SETTINGS)))
        self.httpd = server.start_http("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        for name, value in self.saved.items():
            setattr(server, name, value)
        self.temp.cleanup()

    def native(self, changes):
        return ipc.dispatch({"id": "settings", "method": "settings.set",
                             "params": {"changes": changes}})

    def http(self, changes):
        req = urllib.request.Request(
            self.base + "/api/settings",
            data=json.dumps(changes).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_all_supported_field_groups_and_nested_merge(self):
        changes = {
            "scm_dir": "",
            "extras_dir": "éxtras",
            "python": "/usr/bin/python3",
            "port": 65535,
            "theme": "light",
            "ui_mode": "advanced",
            "auto_open_browser": False,
            "onboarded": True,
            "defaults": {"card_size": "custom", "paper_size": "a4", "ppi": 0, "quality": 42.5},
        }
        response = self.native(changes)
        self.assertTrue(response["ok"])
        settings = response["result"]["settings"]
        self.assertEqual(settings["defaults"], {
            "card_size": "custom", "paper_size": "a4", "ppi": 0, "quality": 42.5,
        })
        self.assertEqual(server.load_settings(), settings)

        # A nested patch preserves the sibling defaults.
        response = self.native({"defaults": {"quality": 99}})
        self.assertTrue(response["result"]["ok"])
        self.assertEqual(response["result"]["settings"]["defaults"]["card_size"], "custom")
        self.assertEqual(response["result"]["settings"]["defaults"]["quality"], 99)

    def test_invalid_patches_have_no_mutation_and_http_parity(self):
        before = server.SETTINGS_FILE.read_bytes()
        invalid = [
            {}, {"repos": {}}, {"unknown": 1}, {"port": True}, {"port": 1023},
            {"theme": "blue"}, {"ui_mode": "simple "}, {"onboarded": 1},
            {"scm_dir": "a\x00b"}, {"python": "\ud800"},
            {"defaults": {}}, {"defaults": {"card_size": ""}},
            {"defaults": {"ppi": True}}, {"defaults": {"ppi": 10001}},
            {"defaults": {"quality": float("inf")}},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                native = self.native(changes)
                self.assertEqual(native["result"]["ok"], False)
                status, http = self.http(changes)
                self.assertEqual(status, 400)
                self.assertEqual(http, native["result"])
                self.assertEqual(server.SETTINGS_FILE.read_bytes(), before)

    def test_size_and_exact_native_envelope(self):
        response = self.native({"scm_dir": "x" * (server.SETTINGS_CHANGES_MAX_BYTES + 100)})
        self.assertFalse(response["result"]["ok"])
        self.assertFalse(self.native({})["result"]["ok"])
        self.assertEqual(ipc.dispatch({"id": "x", "method": "settings.set", "params": {}})
                         ["error"]["code"], "bad_request")
        self.assertEqual(ipc.dispatch({"id": "x", "method": "settings.set",
                                       "params": {"changes": {}, "extra": 1}})
                         ["error"]["code"], "bad_request")

    def test_success_invalidates_both_caches(self):
        server.MANIFEST_CACHE["old"] = {"stale": True}
        server._INFO_SNAP.update(t=1, v={"stale": True})
        server._REPOS_MTIME["t"] = 1
        response = self.native({"theme": "light"})
        self.assertTrue(response["result"]["ok"])
        self.assertFalse(server.MANIFEST_CACHE)
        self.assertFalse(server._INFO_SNAP)
        self.assertFalse(server._REPOS_MTIME)

    def test_concurrent_disjoint_updates_are_not_lost(self):
        results = []
        barrier = threading.Barrier(2)

        def write(changes):
            barrier.wait()
            results.append(server.update_settings(changes))

        threads = [threading.Thread(target=write, args=({"theme": "light"},)),
                   threading.Thread(target=write, args=({"onboarded": True},))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(server.load_settings()["theme"], "light")
        self.assertTrue(server.load_settings()["onboarded"])

    def test_atomic_replace_failure_does_not_claim_success(self):
        before = server.SETTINGS_FILE.read_bytes()
        with mock.patch.object(server.os, "replace", side_effect=OSError("no replace")):
            with self.assertRaises(OSError):
                server.update_settings({"theme": "light"})
        self.assertEqual(server.SETTINGS_FILE.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
