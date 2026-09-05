"""Focused contract tests for the Python child JSON-lines adapter."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server
from test_phase0_baseline import Phase0Fixture


class FlushCapture(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class IpcProtocolTests(unittest.TestCase):
    def test_success_methods_and_one_flush_per_line(self):
        fake = {
            "info": {"fixture": "info"},
            "manifest": {"fixture": "manifest"},
            "settings.get": {"fixture": "settings"},
        }
        source = b"".join(
            json.dumps({"id": str(i), "method": method, "params": {}}).encode() + b"\n"
            for i, method in enumerate(fake)
        )
        output = FlushCapture()
        diagnostics = io.StringIO()
        process_stdout = io.StringIO()
        with mock.patch.object(server, "get_info", return_value=fake["info"]), \
             mock.patch.object(server, "get_manifest", return_value=fake["manifest"]), \
             mock.patch.object(server, "load_settings", return_value=fake["settings.get"]), \
             mock.patch.object(ipc.sys, "stderr", diagnostics), \
             mock.patch.object(ipc.sys, "stdout", process_stdout):
            ipc.serve_stdio(io.BytesIO(source), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses, [
            {"id": "0", "ok": True, "result": fake["info"]},
            {"id": "1", "ok": True, "result": fake["manifest"]},
            {"id": "2", "ok": True, "result": fake["settings.get"]},
        ])
        self.assertEqual(output.flushes, 3)
        self.assertEqual(diagnostics.getvalue().splitlines(), [
            "[ipc] served info", "[ipc] served manifest", "[ipc] served settings.get",
        ])
        self.assertNotIn("[ipc]", output.getvalue().decode())
        self.assertEqual(process_stdout.getvalue(), "")

    def test_malformed_unknown_bad_id_and_non_object_requests_survive(self):
        source = b"not json\n" + b"[]\n" + b'{"id":"u","method":"nope","params":{}}\n' \
            + b'{"id":4,"method":"info","params":{}}\n' \
            + b'{"id":"missing-params","method":"info"}\n'
        output = io.BytesIO()
        with mock.patch.object(server, "get_info", return_value={}):
            ipc.serve_stdio(io.BytesIO(source), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["error"]["code"] for r in responses],
                         ["bad_request", "bad_request", "unknown_method", "bad_request", "bad_request"])
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[2]["id"], "u")
        self.assertEqual(responses[3]["id"], None)

    def test_utf8_and_oversized_lines_are_rejected_and_next_frame_survives(self):
        oversized = b"x" * ipc.MAX_LINE_SIZE + b"\n"
        source = oversized + b"\xff\n" + b'{"id":"ok","method":"info","params":{}}\n'
        output = io.BytesIO()
        with mock.patch.object(server, "get_info", return_value={"ok": True}):
            ipc.serve_stdio(io.BytesIO(source), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], "bad_request")
        self.assertIn("1 MiB", responses[0]["error"]["message"])
        self.assertEqual(responses[1]["error"]["code"], "bad_request")
        self.assertEqual(responses[2], {"id": "ok", "ok": True, "result": {"ok": True}})

    def test_response_limit_replaces_oversized_result(self):
        output = io.BytesIO()
        request = b'{"id":"large","method":"info","params":{}}\n'
        with mock.patch.object(server, "get_info", return_value={"data": "x" * 1000}):
            ipc.serve_stdio(io.BytesIO(request), output, max_response_size=256)
        response = json.loads(output.getvalue())
        self.assertEqual(response, {
            "id": "large", "ok": False,
            "error": {"code": "internal", "message": "response exceeds configured limit"},
        })
        self.assertLessEqual(len(output.getvalue()), 256)

    def test_oversized_error_frame_is_bounded_too(self):
        output = io.BytesIO()
        ipc._write(output, {"id": "x" * 1000, "ok": False,
                            "error": {"code": "bad_request", "message": "bad"}},
                   max_response_size=128)
        response = json.loads(output.getvalue())
        self.assertEqual(response["ok"], False)
        self.assertLessEqual(len(output.getvalue()), 128)

    def test_handler_failure_is_structured_and_eof_is_clean(self):
        output = io.BytesIO()
        request = b'{"id":"x","method":"manifest","params":{}}\n'
        with mock.patch.object(server, "get_manifest", side_effect=RuntimeError("secret")), \
             mock.patch("scm_workbench.ipc.traceback.print_exc") as trace:
            ipc.serve_stdio(io.BytesIO(request), output)
        response = json.loads(output.getvalue())
        self.assertEqual(response, {
            "id": "x", "ok": False,
            "error": {"code": "internal", "message": "request handler failed"},
        })
        trace.assert_called_once()

    def test_oversized_input_drains_to_newline_and_next_frame_survives(self):
        # The suffix before the newline is one logical frame, not a second
        # request.  The following newline-delimited frame is the one that must
        # remain aligned and be processed.
        source = b"x" * 20 + b'{"id":"not-a-frame"}\n' + b'{"id":"ok","method":"info","params":{}}\n'
        output = io.BytesIO()
        with mock.patch.object(server, "get_info", return_value={"ok": True}):
            ipc.serve_stdio(io.BytesIO(source), output, max_line_size=64)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], "bad_request")
        self.assertEqual(responses[1], {"id": "ok", "ok": True, "result": {"ok": True}})

    def test_http_and_ipc_reads_have_structural_parity(self):
        with tempfile.TemporaryDirectory(prefix="scm-workbench-ipc-") as temp:
            root = Path(temp)
            fixture = Phase0Fixture(root)
            data = fixture.data
            names = ("DATA_DIR", "SETTINGS_FILE", "LOGS_DIR", "JOBS_FILE",
                     "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE", "_IPC_MODE")
            old_globals = {name: getattr(server, name) for name in names}
            old_caches = {
                "MANIFEST_CACHE": dict(server.MANIFEST_CACHE),
                "_INFO_SNAP": dict(server._INFO_SNAP),
                "_REPOS_MTIME": dict(server._REPOS_MTIME),
            }
            old_env = os.environ.get("SCM_WORKBENCH_DATA")
            httpd = None
            try:
                os.environ["SCM_WORKBENCH_DATA"] = str(data)
                server.DATA_DIR = data
                server.SETTINGS_FILE = data / "settings.json"
                server.LOGS_DIR = data / "logs"
                server.JOBS_FILE = data / "jobs.json"
                server.PER_SIZE_OFFSETS_FILE = data / "offsets.json"
                server.UPDATE_STATE_FILE = data / "updates.json"
                server._IPC_MODE = False
                server.MANIFEST_CACHE.clear()
                server._INFO_SNAP.clear()
                server._REPOS_MTIME.clear()
                configured = json.loads(json.dumps(server.DEFAULT_SETTINGS))
                configured.update({
                    "scm_dir": str(fixture.scm), "extras_dir": str(fixture.extras),
                    "theme": "fixture-theme",
                })
                server.save_settings(configured)
                httpd = server.start_http("127.0.0.1", 0)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % httpd.server_address[1]
                paths = {"info": "/api/info", "manifest": "/api/manifest",
                         "settings.get": "/api/settings"}
                for method, path in paths.items():
                    with urllib.request.urlopen(base + path, timeout=5) as response:
                        http_value = json.loads(response.read())
                    output = io.BytesIO()
                    request = json.dumps({"id": method, "method": method, "params": {}}).encode() + b"\n"
                    ipc.serve_stdio(io.BytesIO(request), output)
                    ipc_value = json.loads(output.getvalue())["result"]
                    self.assertEqual(http_value, ipc_value, method)
                self.assertEqual(json.loads(server.SETTINGS_FILE.read_text())["theme"], "fixture-theme")
                self.assertEqual(json.loads(server.SETTINGS_FILE.read_text())["scm_dir"], str(fixture.scm))
                self.assertEqual(json.loads(output.getvalue())["result"]["theme"], "fixture-theme")
            finally:
                if httpd is not None:
                    httpd.shutdown()
                    httpd.server_close()
                for name, value in old_globals.items():
                    setattr(server, name, value)
                for name, value in old_caches.items():
                    getattr(server, name).clear()
                    getattr(server, name).update(value)
                if old_env is None:
                    os.environ.pop("SCM_WORKBENCH_DATA", None)
                else:
                    os.environ["SCM_WORKBENCH_DATA"] = old_env

    def test_ipc_eof_stops_server_process(self):
        with tempfile.TemporaryDirectory(prefix="scm-workbench-ipc-eof-") as temp:
            env = dict(os.environ, SCM_WORKBENCH_DATA=temp, SCM_WORKBENCH_NO_BOOTSTRAP="1")
            process = subprocess.Popen(
                [sys.executable, "-m", "scm_workbench.server", "--ipc", "--no-browser", "--port", "0"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            try:
                banner = []
                while True:
                    line = process.stderr.readline().decode("utf-8", "replace")
                    if not line:
                        break
                    banner.append(line)
                    if "UI:  http://" in line:
                        break
                ui_line = next(line for line in banner if "UI:  http://" in line)
                match = re.search(r"UI:\s+http://[^:]+:(\d+)", ui_line)
                self.assertIsNotNone(match)
                with urllib.request.urlopen("http://127.0.0.1:%s/api/settings" % match.group(1), timeout=5) as response:
                    self.assertEqual(json.loads(response.read())["port"], server.DEFAULT_PORT)
                self.assertEqual(process.poll(), None)
                process.stdin.close()
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(process.stdout.read(), b"")
                process.stderr.close()
                process.stdout.close()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
