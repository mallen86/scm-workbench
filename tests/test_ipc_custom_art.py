"""Native private import and browser raw-upload validation parity."""
import http.client
import socket
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from scm_workbench import custom_art, ipc, server

PNG = b"\x89PNG\r\n\x1a\ncontents"


class CustomArtTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scm = self.root / "scm"
        self.scm.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        for target, attribute, value in [(server, "DATA_DIR", self.data),
                                         (server.repo_sync, "data_dir", lambda: self.data),
                                         (server, "_IMAGE_DELETE_LOCK", threading.Lock()),
                                         (server, "_IPC_MODE", False)]:
            patch = mock.patch.object(target, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "load_settings", return_value={"scm_dir": str(self.scm)})
        patch.start()
        self.addCleanup(patch.stop)
        with custom_art._lock:
            custom_art._operations.clear()

    def dispatch(self, method, params):
        return ipc.dispatch({"id": "test", "method": method, "params": params})

    def http(self, route, body=b"", headers=None):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            conn.request("POST", route, body, headers or {})
            resp = conn.getresponse()
            result = resp.status, json.loads(resp.read())
            conn.close()
            return result
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_private_start_is_async_poll_terminal(self):
        path = self.root / "art.png"
        path.write_bytes(PNG)
        ack = self.dispatch("custom_art.import_selected", {"destination": "front", "source_paths": [str(path)]})
        self.assertTrue(ack["ok"])
        self.assertTrue(ack["result"]["ok"])
        op = ack["result"]["operation_id"]
        for _ in range(200):
            poll = self.dispatch("custom_art.import_poll", {"operation_id": op})
            if poll["result"]["status"] == "done":
                break
            time.sleep(.01)
        self.assertEqual(poll["result"]["result"]["names"], ["art.png"])
        self.assertEqual(poll["result"]["result"]["failed"], [])
        self.assertEqual((self.scm / "game" / "front" / "art.png").read_bytes(), PNG)
        self.assertEqual(path.read_bytes(), PNG)

    def test_exact_native_validation_and_missing_id(self):
        cases = [({"destination": "back", "source_paths": ["/tmp/art.png", "/tmp/other.png"]}, "custom_art.import_selected"),
                 ({"destination": "front", "source_paths": ["relative.png"]}, "custom_art.import_selected"),
                 ({"destination": "front", "source_paths": ["/tmp/\n.png"]}, "custom_art.import_selected"),
                 ({"destination": "front", "source_paths": ["/tmp/" + "x" * 4096]}, "custom_art.import_selected"),
                 ({"destination": "front", "source_paths": ["/tmp/a.png"], "extra": 1}, "custom_art.import_selected"),
                 ({"destination": "front", "source_paths": []}, "custom_art.import_selected"),
                 ({"destination": "front", "source_paths": ["/tmp/a.png"] * 257}, "custom_art.import_selected"),
                 ({"destination": {}}, "custom_art.open_folder"),
                 ({"destination": "front", "path": "/tmp"}, "custom_art.open_folder"),
                 ({"operation_id": "BAD"}, "custom_art.import_poll")]
        for params, method in cases:
            with self.subTest(method=method, params=str(params)[:80]):
                response = self.dispatch(method, params)
                self.assertEqual(response["error"]["code"], "bad_request")
        missing = self.dispatch("custom_art.import_poll", {"operation_id": "0" * 32})
        self.assertEqual(missing["error"]["code"], "bad_request")
        self.assertNotIn("custom_art.import_selected", ipc.ALLOWED_METHODS - ipc.PRIVATE_METHODS)
        self.assertNotIn("custom_art.import_poll", ipc.ALLOWED_METHODS - ipc.PRIVATE_METHODS)

    def test_busy_registry_and_expiry(self):
        with custom_art._lock:
            for i in range(2):
                custom_art._operations[str(i) * 32] = {"ended": None, "total": 1, "completed": 0, "result": None}
        response = self.dispatch("custom_art.import_selected", {"destination": "front", "source_paths": ["/tmp/image.png"]})
        self.assertEqual(response["result"]["ok"], False)
        self.assertIn("busy", response["result"]["errors"][0])
        with custom_art._lock:
            custom_art._operations.clear()
            custom_art._operations["0" * 32] = {"ended": time.monotonic() - custom_art.TTL - 1,
                                                   "total": 1, "completed": 1, "result": {"ok": True}}
        self.assertEqual(self.dispatch("custom_art.import_poll", {"operation_id": "0" * 32})["error"]["code"], "bad_request")

    def test_http_raw_upload_rejects_hostile_request_and_native_mode(self):
        route = "/api/custom-art/import?destination=front&name=upload.png"
        headers = {"Content-Type": "application/octet-stream"}
        status, result = self.http(route, PNG, headers)
        self.assertEqual(status, 200)
        self.assertEqual(result["names"], ["upload.png"])
        for suffix in ("&unknown=1", "&name=again.png", "&path=/tmp/hack"):
            status, result = self.http(route + suffix, PNG, headers)
            self.assertEqual(status, 400)
            self.assertFalse(result["ok"])
        for badroute in ("/api/custom-art/import?destination=unknown&name=a.png",
                         "/api/custom-art/import?destination=front&name=..%2Fescape.png"):
            self.assertEqual(self.http(badroute, PNG, headers)[0], 400)
        self.assertEqual(self.http(route, PNG, {"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.http("/api/custom-art/import?destination=front&name=%GG.png", PNG, headers)[0], 400)
        self.assertEqual(self.http(route, b"invalid", headers)[1]["failed"][0]["name"], "upload.png")
        with mock.patch.object(server, "_IPC_MODE", True):
            self.assertEqual(self.http(route, PNG, headers)[0], 403)
            self.assertEqual(self.http("/api/custom-art/open-folder", b'{}', {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.http("/api/custom-art/open-folder", b'{"destination":"front","path":"/tmp"}',
                                   {"Content-Type": "application/json"})[0], 400)

    def test_upload_deadline_and_socket_timeout_restore(self):
        class FakeSocket:
            timeout = 31
            values = []
            def gettimeout(self):
                return self.timeout
            def settimeout(self, value):
                self.values.append(value)
                self.timeout = value
        class Drip:
            def read1(self, count):
                self.assert_count(count)
                clock[0] += .06
                return b"x"
            def assert_count(self, count):
                self_outer.assertLessEqual(count, custom_art.CHUNK)
            def read(self, count):
                self_outer.fail("buffered read would wait for a full chunk")
        self_outer = self
        clock = [10.0]
        fake = FakeSocket()
        with mock.patch.object(custom_art.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(custom_art, "UPLOAD_DEADLINE", .1):
            with self.assertRaisesRegex(custom_art.ImportError, "timed out"):
                custom_art.import_bytes("front", "slow.png", Drip(), 10, server,
                                        {"scm_dir": str(self.scm)}, socket=fake)
        self.assertEqual(fake.timeout, 31)
        self.assertTrue(all(0 < v <= .1 for v in fake.values[:-1]))
        self.assertFalse((self.scm / "game" / "front" / "slow.png").exists())
        with self.assertRaisesRegex(custom_art.ImportError, "interrupted or timed out"):
            class TimedOut:
                def read1(self, size):
                    raise socket.timeout()
            custom_art.import_bytes("front", "slow.png", TimedOut(), 10, server,
                                    {"scm_dir": str(self.scm)}, socket=fake)
        self.assertEqual(fake.timeout, 31)

    def test_http_slow_truncated_upload_rejected_and_connection_closed(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(custom_art, "UPLOAD_DEADLINE", .15):
                with socket.create_connection(("127.0.0.1", httpd.server_port), timeout=2) as client:
                    client.sendall(b"POST /api/custom-art/import?destination=front&name=slow.png HTTP/1.1\r\n"
                                   b"Host: 127.0.0.1\r\nContent-Type: application/octet-stream\r\n"
                                   b"Content-Length: 30\r\n\r\n" + PNG[:2])
                    chunks = []
                    while True:
                        data = client.recv(4096)
                        if not data:
                            break
                        chunks.append(data)
            reply = b"".join(chunks)
            self.assertIn(b"400 Bad Request", reply)
            self.assertIn(b"timed out", reply)
            self.assertFalse((self.scm / "game" / "front" / "slow.png").exists())
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_native_and_http_back_replace_with_single_image(self):
        back = self.scm / "game" / "back"
        back.mkdir(parents=True)
        (back / "old.png").write_bytes(PNG)
        self.assertEqual(server.BACK_IMAGE_SOURCE_MAX_BYTES, 32 * 1024 * 1024)
        large_image = PNG + b"\0" * (9 * 1024 * 1024 - len(PNG))
        path = self.root / "new.png"
        path.write_bytes(large_image)
        ack = self.dispatch("custom_art.import_selected", {"destination": "back", "source_paths": [str(path)]})
        self.assertTrue(ack["result"]["ok"])
        for _ in range(200):
            poll = self.dispatch("custom_art.import_poll", {"operation_id": ack["result"]["operation_id"]})
            if poll["result"]["status"] == "done": break
            time.sleep(.01)
        self.assertEqual(poll["result"]["result"]["names"], ["new.png"])
        self.assertFalse((back / "old.png").exists())
        self.assertEqual((back / "new.png").read_bytes(), large_image)
        status, result = self.http("/api/custom-art/import?destination=back&name=upload.png", large_image,
                                   {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        self.assertEqual(result["names"], ["upload.png"])
        self.assertEqual([item["name"] for item in server._scan_back_images(self.scm)], ["upload.png"])
        self.assertFalse((back / "new.png").exists())
        status, rejected = self.http("/api/custom-art/import?destination=back&name=oversized.png",
                                     PNG, {"Content-Type": "application/octet-stream",
                                           "Content-Length": str(server.BACK_IMAGE_SOURCE_MAX_BYTES + 1)})
        self.assertEqual(status, 400)
        self.assertIn("32 MiB", rejected["errors"][0])
        self.assertEqual((back / "upload.png").read_bytes(), large_image)
        self.assertEqual(self.dispatch("custom_art.import_selected", {"destination": "back", "source_paths": [str(path), str(path)]})["error"]["code"], "bad_request")
        self.assertEqual([item["name"] for item in server._scan_back_images(self.scm)], ["upload.png"])

    def test_native_open_folder_response(self):
        with mock.patch.object(server, "reveal_path", return_value=None) as reveal:
            reply = self.dispatch("custom_art.open_folder", {"destination": "back"})
        self.assertEqual(reply["result"], {"ok": True, "errors": []})
        reveal.assert_called_once_with(self.scm / "game" / "back")
