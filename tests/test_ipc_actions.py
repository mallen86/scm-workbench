"""Native OS-action validation and launcher delegation contracts."""

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server


class NativeActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-actions-")
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.file = self.data / "fixture.txt"
        self.file.write_text("fixture", encoding="utf-8")
        self.directory = self.data / "folder"
        self.directory.mkdir()
        self.outside = self.root / "outside.txt"
        self.outside.write_text("outside", encoding="utf-8")
        self.roots = [self.data]
        self.old_data_dir = server.DATA_DIR
        server.DATA_DIR = self.data
        self.roots_patch = mock.patch.object(server, "allowed_roots", return_value=self.roots)
        self.roots_patch.start()

    def tearDown(self):
        self.roots_patch.stop()
        server.DATA_DIR = self.old_data_dir
        self.temp.cleanup()

    def call(self, method, params):
        return ipc.dispatch({"id": "action", "method": method, "params": params})

    def test_allowlist_and_exact_params(self):
        self.assertIn("file.open", ipc.ALLOWED_METHODS)
        self.assertIn("file.reveal", ipc.ALLOWED_METHODS)
        self.assertIn("url.open", ipc.ALLOWED_METHODS)
        for method, params in (
            ("file.open", {}),
            ("file.open", {"path": str(self.file), "extra": 1}),
            ("file.reveal", {"path": 4}),
            ("url.open", {"url": "https://example.test", "extra": 1}),
            ("url.open", {"url": ""}),
        ):
            result = self.call(method, params)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "bad_request")

    def test_path_bounds_and_controls(self):
        for value in ("x" * (server.ACTION_PATH_MAX_BYTES + 1), "bad\x00path", "bad\npath"):
            result = self.call("file.open", {"path": value})
            self.assertEqual(result["error"]["code"], "bad_request")
        result = self.call("file.open", {"path": "\ud800"})
        self.assertEqual(result["error"]["code"], "bad_request")
        result = self.call("url.open", {"url": "https://" + "x" * server.ACTION_URL_MAX_BYTES})
        self.assertEqual(result["error"]["code"], "bad_request")

    def test_path_type_and_canonical_checks(self):
        with mock.patch.object(server, "open_path", return_value=None) as launcher:
            result = self.call("file.open", {"path": str(self.file)})
        self.assertEqual(result["result"], {"ok": True, "errors": []})
        launcher.assert_called_once_with(self.file.resolve())

        for value in (str(self.outside), str(self.data / ".." / "outside.txt")):
            result = self.call("file.open", {"path": value})
            self.assertEqual(result["result"]["ok"], False)
            self.assertIn("outside", result["result"]["errors"][0])
        self.assertFalse(self.call("file.open", {"path": str(self.directory)})["result"]["ok"])
        self.assertFalse(self.call("file.reveal", {"path": str(self.data / "missing")})["result"]["ok"])

    def test_internal_symlink_is_canonicalized_and_escape_is_denied(self):
        inside_link = self.data / "inside-link"
        escape_link = self.data / "escape-link"
        try:
            inside_link.symlink_to(self.file)
            escape_link.symlink_to(self.outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable: %s" % error)
        with mock.patch.object(server, "open_path", return_value=None) as launcher:
            self.assertTrue(self.call("file.open", {"path": str(inside_link)})["result"]["ok"])
        launcher.assert_called_once_with(self.file.resolve())
        self.assertFalse(self.call("file.open", {"path": str(escape_link)})["result"]["ok"])

    def test_url_matrix_and_single_argument_delegation(self):
        for value in ("ftp://example.test", "https://", "https://user:pass@example.test",
                      "https://example.test:notaport", "https://example.test:",
                      "https://example.test:99999", "https://example.test/a b"):
            result = self.call("url.open", {"url": value})
            self.assertFalse(result["result"]["ok"])
            self.assertTrue(result["result"]["errors"])
        injection_url = "HtTpS://example.test/a;echo%20injected&&still-one-argument"
        with mock.patch.object(server, "open_url", return_value=None) as launcher:
            result = self.call("url.open", {"url": injection_url})
        self.assertEqual(result["result"], {"ok": True, "errors": []})
        launcher.assert_called_once_with(injection_url)

    def test_launcher_error_is_bounded_and_stdout_isolated(self):
        output = io.StringIO()
        with mock.patch.object(server, "open_url", side_effect=OSError("x\n" * 10000)), \
                mock.patch.object(ipc.sys, "stdout", output):
            result = self.call("url.open", {"url": "https://example.test"})
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), 8192)
        self.assertEqual(output.getvalue(), "")

    def test_windows_reveal_uses_explorer_selection(self):
        with mock.patch.object(server.os, "name", "nt"), \
                mock.patch.object(server.subprocess, "Popen") as popen:
            result = self.call("file.reveal", {"path": str(self.file)})
        self.assertTrue(result["result"]["ok"])
        args = popen.call_args.args[0]
        self.assertEqual(args, ["explorer", f"/select,{self.file.resolve()}"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertIs(popen.call_args.kwargs["stdout"], server.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], server.subprocess.DEVNULL)

    def _http_request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.http_base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_ipc_http_file_route_is_closed_before_any_file_work(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.http_base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            calls = {
                name: mock.patch.object(server, name, side_effect=AssertionError(name))
                for name in ("load_settings", "allowed_roots", "url_open_action",
                             "file_open_action", "file_reveal_action", "list_files")
            }
            with (
                mock.patch.object(server, "_IPC_MODE", True),
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
                calls["load_settings"], calls["allowed_roots"],
                calls["url_open_action"], calls["file_open_action"],
                calls["file_reveal_action"], calls["list_files"],
            ):
                requests = (
                    "/api/file",
                    "/api/file?path=" + urllib.parse.quote(str(self.file)),
                    "/api/file?path=" + urllib.parse.quote(str(self.directory)) + "&images_only=1",
                    "/api/file?path=" + urllib.parse.quote(str(self.file)) + "&open=1",
                    "/api/file?path=" + urllib.parse.quote(str(self.directory)) + "&reveal=1",
                    "/api/file?url=" + urllib.parse.quote("https://example.test"),
                )
                for path in requests:
                    status, body = self._http_request("GET", path)
                    self.assertEqual(status, 403, path)
                    self.assertEqual(body, {"ok": False, "errors": [
                        "native file access is required in packaged mode"]})
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def test_standalone_raw_file_keeps_bytes_mime_and_safe_disposition(self):
        payload = b"not text; preserve these bytes"
        raw_file = self.data / "fixture.pdf"
        raw_file.write_bytes(payload)
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.http_base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            request = urllib.request.Request(
                self.http_base + "/api/file?path=" + urllib.parse.quote(str(raw_file)),
                method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.read(), payload)
                self.assertEqual(response.headers["Content-Type"], "application/pdf")
                self.assertEqual(response.headers["Content-Disposition"],
                                 'inline; filename="fixture.pdf"; filename*=UTF-8\'\'fixture.pdf')
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

        unsafe_name = 'bad"' + chr(13) + chr(10) + "-é.txt"
        disposition = server._content_disposition(unsafe_name)
        self.assertEqual(disposition, 'inline; filename="bad___-_.txt"; '
                         "filename*=UTF-8''bad%22%0D%0A-%C3%A9.txt")
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)

    def test_http_and_native_action_semantics_have_parity(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.http_base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            injection_url = "HtTpS://example.test/a;echo%20injected&&still-one-argument"
            successes = (
                ("file.open", {"path": str(self.file)}, "GET",
                 "/api/file?path=" + urllib.parse.quote(str(self.file)) + "&open=1", "open_path"),
                ("file.reveal", {"path": str(self.directory)}, "POST",
                 "/api/reveal", "reveal_path"),
                ("url.open", {"url": injection_url}, "GET",
                 "/api/file?url=" + urllib.parse.quote(injection_url), "open_url"),
            )
            for method, params, http_method, path, launcher_name in successes:
                body = {"path": str(self.directory)} if method == "file.reveal" else None
                with mock.patch.object(server, launcher_name, return_value=None) as launcher:
                    status, http_result = self._http_request(http_method, path, body)
                    native_result = self.call(method, params)
                self.assertEqual(status, 200, method)
                self.assertEqual(http_result, native_result["result"], method)
                launcher.assert_called()
                if method == "url.open":
                    # The complete URL remains one launcher argument; shell
                    # metacharacters are never split into a command.
                    self.assertEqual(launcher.call_args.args, (injection_url,))

            invalid_url = "https://example.test/a b"
            with mock.patch.object(server, "open_url", return_value=None):
                status, http_result = self._http_request(
                    "GET", "/api/file?url=" + urllib.parse.quote(invalid_url))
                native_result = self.call("url.open", {"url": invalid_url})
            self.assertEqual(status, 400)
            self.assertEqual(http_result, native_result["result"])

            outside = str(self.outside)
            with mock.patch.object(server, "open_path", return_value=None):
                status, http_result = self._http_request(
                    "GET", "/api/file?path=" + urllib.parse.quote(outside) + "&open=1")
                native_result = self.call("file.open", {"path": outside})
            self.assertEqual(status, 403)
            self.assertEqual(http_result, native_result["result"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
