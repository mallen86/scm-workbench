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
        server._invalidate_script_capability_cache()
        with server._UPDATE_CHECK_CONDITION:
            self.saved_update_check = (
                server._UPDATE_CHECKING, server._UPDATE_CHECK_GENERATION,
                server._UPDATE_CHECK_RESULT,
            )
            server._UPDATE_CHECKING = False
            server._UPDATE_CHECK_RESULT = None
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
        with server._UPDATE_CHECK_CONDITION:
            (server._UPDATE_CHECKING, server._UPDATE_CHECK_GENERATION,
             server._UPDATE_CHECK_RESULT) = self.saved_update_check
            server._UPDATE_CHECK_CONDITION.notify_all()
        server._invalidate_script_capability_cache()
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
            "update_channel": "beta",
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
            {"theme": "blue"}, {"ui_mode": "simple "}, {"update_channel": "nightly"},
            {"update_channel": True}, {"onboarded": 1},
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

    def test_ui_only_change_preserves_manifest_and_capability_caches(self):
        server.MANIFEST_CACHE["old"] = {"stale": True}
        server._INFO_SNAP.update(t=1, v={"stale": True})
        server._REPOS_MTIME["t"] = 1
        with server._SCRIPT_CAPABILITY_CACHE_LOCK:
            server._SCRIPT_CAPABILITY_CACHE["old"] = (0.0, {"status": "ok"})

        response = self.native({"theme": "light"})

        self.assertTrue(response["result"]["ok"])
        self.assertEqual(server.MANIFEST_CACHE, {"old": {"stale": True}})
        self.assertFalse(server._INFO_SNAP)
        self.assertEqual(server._REPOS_MTIME, {"t": 1})
        with server._SCRIPT_CAPABILITY_CACHE_LOCK:
            self.assertIn("old", server._SCRIPT_CAPABILITY_CACHE)

    def test_update_channel_and_mode_changes_invalidate_cached_release_result(self):
        self.assertTrue(self.native({"ui_mode": "advanced"})["result"]["ok"])
        with server._UPDATE_CHECK_CONDITION:
            generation = server._UPDATE_CHECK_GENERATION
            server._UPDATE_CHECK_RESULT = server._default_update_state("stable")

        response = self.native({"update_channel": "beta"})

        self.assertTrue(response["result"]["ok"])
        self.assertEqual(response["result"]["settings"]["update_channel"], "beta")
        self.assertEqual(server._selected_update_channel(), "beta")
        with server._UPDATE_CHECK_CONDITION:
            self.assertEqual(server._UPDATE_CHECK_GENERATION, generation + 1)
            self.assertIsNone(server._UPDATE_CHECK_RESULT)
            cached = server._default_update_state("beta")
            server._UPDATE_CHECK_RESULT = cached
            generation = server._UPDATE_CHECK_GENERATION

        same = self.native({"update_channel": "beta"})

        self.assertTrue(same["result"]["ok"])
        with server._UPDATE_CHECK_CONDITION:
            self.assertEqual(server._UPDATE_CHECK_GENERATION, generation)
            self.assertEqual(server._UPDATE_CHECK_RESULT, cached)

        simple = self.native({"ui_mode": "simple"})
        self.assertTrue(simple["result"]["ok"])
        self.assertEqual(simple["result"]["settings"]["update_channel"], "beta")
        self.assertEqual(server._selected_update_channel(), "stable")
        with server._UPDATE_CHECK_CONDITION:
            self.assertEqual(server._UPDATE_CHECK_GENERATION, generation + 1)
            self.assertIsNone(server._UPDATE_CHECK_RESULT)
            generation = server._UPDATE_CHECK_GENERATION

        advanced = self.native({"ui_mode": "advanced"})
        self.assertTrue(advanced["result"]["ok"])
        self.assertEqual(server._selected_update_channel(), "beta")
        with server._UPDATE_CHECK_CONDITION:
            self.assertEqual(server._UPDATE_CHECK_GENERATION, generation + 1)

    def test_repo_paths_and_python_apply_to_the_next_command_without_restart(self):
        scm = self.data / "new-scm"
        extras = self.data / "new-extras"
        python = self.data / "new-python"
        scm.mkdir()
        extras.mkdir()
        python.write_bytes(b"python")
        server.MANIFEST_CACHE["old"] = {"stale": True}
        server._INFO_SNAP.update(t=1, v={"stale": True})
        server._REPOS_MTIME["t"] = 1
        with server._SCRIPT_CAPABILITY_CACHE_LOCK:
            server._SCRIPT_CAPABILITY_CACHE["old"] = (0.0, {"status": "ok"})

        response = self.native({
            "scm_dir": str(scm), "extras_dir": str(extras), "python": str(python),
        })

        self.assertTrue(response["result"]["ok"])
        self.assertFalse(server.MANIFEST_CACHE)
        self.assertFalse(server._INFO_SNAP)
        self.assertFalse(server._REPOS_MTIME)
        with server._SCRIPT_CAPABILITY_CACHE_LOCK:
            self.assertFalse(server._SCRIPT_CAPABILITY_CACHE)
        settings = server.load_settings()
        self.assertEqual(server.effective_dirs(settings), (scm, extras))
        argv, cwd, _env, _title, _warnings, errors = server.build_command(
            "repo_update", {"repo": "scm"}, settings, {})
        self.assertFalse(errors)
        self.assertEqual(argv[0], str(python))
        self.assertEqual(cwd, server.WB_ROOT)

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
