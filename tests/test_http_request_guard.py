"""Browser-compatibility server refuses cross-site, rebinding, and LAN callers."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import server


class RequestRejectionUnitTests(unittest.TestCase):
    def reject(self, method="GET", path="/api/settings", **headers):
        return server.http_request_rejection(method, path, {
            key.replace("_", "-"): value for key, value in headers.items()})

    def test_loopback_and_ip_hosts_are_accepted(self):
        for host in ("127.0.0.1:8037", "localhost:8037", "[::1]:8037", "172.28.160.100:8037"):
            self.assertIsNone(self.reject(Host=host), host)

    def test_rebinding_host_names_are_refused_even_for_static_pages(self):
        for path in ("/", "/api/settings"):
            self.assertIsNotNone(self.reject(path=path, Host="attacker.example:8037"))
        self.assertIsNotNone(self.reject(Host="evil@127.0.0.1"))
        self.assertIsNotNone(self.reject(Host="127.0.0.1:notaport"))

    def test_origin_must_match_exactly(self):
        host = "127.0.0.1:8037"
        self.assertIsNone(self.reject("POST", Host=host, Origin="http://127.0.0.1:8037"))
        for origin in ("https://evil.example", "http://127.0.0.1:9999", "null",
                       "http://localhost:8037", "https://127.0.0.1:8037"):
            self.assertIsNotNone(self.reject("POST", Host=host, Origin=origin), origin)

    def test_cross_site_fetch_metadata_is_refused_for_api_but_not_navigation(self):
        host = "127.0.0.1:8037"
        for site in ("cross-site", "same-site"):
            self.assertIsNotNone(self.reject(Host=host, Sec_Fetch_Site=site))
            self.assertIsNotNone(self.reject("POST", Host=host, Sec_Fetch_Site=site))
            self.assertIsNone(self.reject(path="/", Host=host, Sec_Fetch_Site=site))
        for site in ("same-origin", "none"):
            self.assertIsNone(self.reject("POST", Host=host, Sec_Fetch_Site=site))

    def test_cross_origin_referer_is_refused_for_mutations_without_origin(self):
        host = "127.0.0.1:8037"
        self.assertIsNotNone(self.reject("POST", Host=host, Referer="https://evil.example/page"))
        self.assertIsNone(self.reject("POST", Host=host, Referer="http://127.0.0.1:8037/pdf"))
        self.assertIsNone(self.reject("POST", Host=host))

    def test_client_addresses_are_loopback_or_explicitly_allowed(self):
        self.assertTrue(server.http_client_allowed("127.0.0.1"))
        self.assertTrue(server.http_client_allowed("::1"))
        self.assertTrue(server.http_client_allowed("::ffff:127.0.0.1"))
        self.assertFalse(server.http_client_allowed("192.168.1.20"))
        self.assertFalse(server.http_client_allowed("not-an-address"))
        self.assertTrue(server.http_client_allowed("172.28.160.1", frozenset({"172.28.160.1"})))
        holder = type("Holder", (), {"extra_clients": frozenset()})()
        self.assertFalse(server.WorkbenchHTTPServer.verify_request(holder, None, ("10.0.0.5", 1234)))
        self.assertTrue(server.WorkbenchHTTPServer.verify_request(holder, None, ("127.0.0.1", 1234)))


class RequestGuardHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-guard-")
        self.data = Path(self.temp.name)
        self.saved = {name: getattr(server, name) for name in ("DATA_DIR", "SETTINGS_FILE")}
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.save_settings(json.loads(json.dumps(server.DEFAULT_SETTINGS)))
        self.httpd = server.start_http("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        for name, value in self.saved.items():
            setattr(server, name, value)
        self.temp.cleanup()

    def send(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
        finally:
            conn.close()

    def test_cross_origin_simple_post_cannot_change_settings(self):
        before = server.load_settings()
        status, body = self.send("POST", "/api/settings",
                                 json.dumps({"python": "/tmp/evil-python", "scm_dir": "/tmp"}),
                                 {"Content-Type": "text/plain", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertEqual(server.load_settings(), before)

    def test_same_origin_post_is_still_accepted(self):
        status, body = self.send("POST", "/api/settings", json.dumps({"theme": "light"}),
                                 {"Content-Type": "application/json",
                                  "Origin": f"http://127.0.0.1:{self.port}",
                                  "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["theme"], "light")

    def test_rebinding_host_cannot_read_settings(self):
        status, body = self.send("GET", "/api/settings", headers={"Host": f"attacker.example:{self.port}"})
        self.assertEqual(status, 403)
        self.assertNotIn("python", body)

    def test_side_effecting_get_requires_the_action_header(self):
        with mock.patch.object(server, "url_open_action",
                               return_value=({"ok": True, "errors": []}, 200)) as opened:
            status, _body = self.send("GET", "/api/file?url=https%3A%2F%2Fexample.test")
            self.assertEqual(status, 403)
            opened.assert_not_called()
            status, body = self.send("GET", "/api/file?url=https%3A%2F%2Fexample.test",
                                     headers={server.ACTION_REQUEST_HEADER: "1"})
            self.assertEqual((status, body["ok"]), (200, True))
            opened.assert_called_once_with("https://example.test")
        with mock.patch.object(server, "file_open_action") as opened:
            status, _body = self.send("GET", "/api/file?path=x&open=1")
            self.assertEqual(status, 403)
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
