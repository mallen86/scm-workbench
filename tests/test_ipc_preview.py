"""Native preview IPC contracts and HTTP parity."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server
from test_phase0_baseline import Phase0Fixture


class PreviewIpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-preview-")
        cls.fixture = Phase0Fixture(Path(cls.temp.name))
        cls.old_globals = {
            name: getattr(server, name)
            for name in ("DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
                         "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE")
        }
        cls.old_caches = {
            name: dict(getattr(server, name))
            for name in ("MANIFEST_CACHE", "_INFO_SNAP", "_REPOS_MTIME")
        }
        cls.old_env = os.environ.get("SCM_WORKBENCH_DATA")
        os.environ["SCM_WORKBENCH_DATA"] = str(cls.fixture.data)
        server.DATA_DIR = cls.fixture.data
        server.SETTINGS_FILE = cls.fixture.data / "settings.json"
        server.JOBS_FILE = cls.fixture.data / "jobs.json"
        server.LOGS_DIR = cls.fixture.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = cls.fixture.data / "offsets.json"
        server.UPDATE_STATE_FILE = cls.fixture.data / "updates.json"
        server.MANIFEST_CACHE.clear()
        server._INFO_SNAP.clear()
        server._REPOS_MTIME.clear()
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({
            "scm_dir": str(cls.fixture.scm),
            "extras_dir": str(cls.fixture.extras),
        })
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
        for name, value in cls.old_globals.items():
            setattr(server, name, value)
        for name, value in cls.old_caches.items():
            cache = getattr(server, name)
            cache.clear()
            cache.update(value)
        if cls.old_env is None:
            os.environ.pop("SCM_WORKBENCH_DATA", None)
        else:
            os.environ["SCM_WORKBENCH_DATA"] = cls.old_env
        cls.temp.cleanup()

    def request(self, args):
        query = urllib.parse.urlencode({"kind": "fetch:mtg", "args": json.dumps(args)})
        try:
            with urllib.request.urlopen(self.base + "/api/preview?" + query, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    @staticmethod
    def call(params):
        return ipc.dispatch({"id": "preview-test", "method": "preview", "params": params})

    def test_fixture_http_and_native_preview_results_match(self):
        args = {"deck_file": "example.txt", "format": "simple"}
        status, http_result = self.request(args)
        native = self.call({"kind": "fetch:mtg", "args": args})
        self.assertEqual(status, 200)
        self.assertEqual(native, {"id": "preview-test", "ok": True, "result": http_result})

    def test_http_non_object_args_keep_bad_args_contract(self):
        query = urllib.parse.urlencode({"kind": "fetch:mtg", "args": "[]"})
        request = urllib.request.Request(self.base + "/api/preview?" + query)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(json.loads(raised.exception.read()), {"error": "bad args"})

    def test_preview_validation_is_strict_and_bounded(self):
        bad_params = [
            {},
            {"kind": "fetch:mtg", "args": {}, "extra": True},
            {"kind": "fetch:mtg"},
            {"kind": 4, "args": {}},
            {"kind": "fetch:mtg", "args": []},
            {"kind": "", "args": {}},
            {"kind": "é" * 65, "args": {}},  # 130 UTF-8 bytes
            {"kind": "fetch:mtg", "args": {"text": "x" * ipc.MAX_PREVIEW_ARGS_SIZE}},
        ]
        for params in bad_params:
            with self.subTest(params=list(params)):
                result = self.call(params)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "bad_request")

    def test_unknown_kind_matches_http_not_found_semantics(self):
        query = urllib.parse.urlencode({"kind": "not-a-kind", "args": "{}"})
        request = urllib.request.Request(self.base + "/api/preview?" + query)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(json.loads(raised.exception.read()), {"error": "unknown kind"})
        native = self.call({"kind": "not-a-kind", "args": {}})
        self.assertEqual(native["ok"], False)
        self.assertEqual(native["error"]["code"], "bad_request")

    def test_result_bound_is_distinct_from_handler_failure(self):
        oversized = {"preview": "x" * ipc.MAX_PREVIEW_RESULT_SIZE}
        with mock.patch.object(server, "build_preview", return_value=oversized), \
                mock.patch.object(ipc.traceback, "print_exc") as trace:
            result = self.call({"kind": "fetch:mtg", "args": {}})
        self.assertEqual(result, {
            "id": "preview-test", "ok": False,
            "error": {"code": "result_too_large", "message": "preview result exceeds 512 KiB"},
        })
        trace.assert_not_called()

        with mock.patch.object(server, "build_preview", side_effect=RuntimeError("boom")), \
                mock.patch.object(ipc.traceback, "print_exc") as trace:
            result = self.call({"kind": "fetch:mtg", "args": {}})
        self.assertEqual(result["error"], {"code": "internal", "message": "request handler failed"})
        trace.assert_called_once()

    def test_preview_does_not_write_pasted_decklist(self):
        decklist = self.fixture.scm / "game" / "decklist"
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in decklist.iterdir()}
        args = {
            "deck_source": "paste", "deck_name": "preview-only.txt",
            "deck_text": "Fixture Card\n", "format": "simple",
        }
        self.assertEqual(self.request(args)[0], 200)
        self.assertTrue(self.call({"kind": "fetch:mtg", "args": args})["ok"])
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in decklist.iterdir()}
        self.assertEqual(after, before)
        self.assertFalse((decklist / "preview-only.txt").exists())


if __name__ == "__main__":
    unittest.main()
