import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scm_workbench import server, updater


class FakeResponse:
    def __init__(self, body=b"", *, status=200, headers=None, url=None, chunked=False):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.url = url
        self.chunked = chunked
        self.closed = False
        self.read_calls = []
        self.timeout_calls = []

    def geturl(self):
        return self.url or "https://api.github.com/fixture"

    def read(self, size=-1):
        self.read_calls.append(size)
        if not self.body:
            return b""
        if size is None or size < 0:
            size = len(self.body)
        result, self.body = self.body[:size], self.body[size:]
        return result

    def settimeout(self, seconds):
        self.timeout_calls.append(seconds)

    def close(self):
        self.closed = True


class UpdaterMetadataTests(unittest.TestCase):
    def setUp(self):
        self.api = patch.object(updater, "API", "https://api.github.com")
        self.repo = patch.object(updater, "UPDATE_REPO", "owner/workbench")
        self.api.start()
        self.repo.start()

    def tearDown(self):
        self.repo.stop()
        self.api.stop()

    @staticmethod
    def release_raw(tag="v2.0.0", *, body="notes", assets=None, **extra):
        asset_name = "scm-workbench-macos.zip"
        assets = assets if assets is not None else [{
            "id": 7,
            "name": asset_name,
            "browser_download_url":
                "https://github.com/owner/workbench/releases/download/v2.0.0/"
                + asset_name,
            "size": 123,
            "digest": "sha256:" + "a" * 64,
        }]
        raw = {
            "tag_name": tag,
            "name": "Workbench " + tag,
            "body": body,
            "published_at": "2026-09-06T12:30:00Z",
            "html_url": "https://github.com/owner/workbench/releases/tag/" + tag,
            "assets": assets,
        }
        raw.update(extra)
        return raw

    def mock_metadata(self, raw, status=200):
        return patch.object(
            updater, "gh_request",
            return_value=(status, {}, json.dumps(raw).encode("utf-8")),
        )

    def test_configured_repo_and_api_are_validated_before_network(self):
        for bad_repo in ("owner", "owner/a/b", "/workbench", "owner/../x", "owner/a\\b"):
            with self.subTest(repo=bad_repo), patch.object(updater, "UPDATE_REPO", bad_repo), \
                    patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(updater.UpdateError):
                    updater.gh_request("/releases/latest")
                urlopen.assert_not_called()

        for bad_api in (
            "http://api.github.com", "https://user:pass@api.github.com",
            "https://api.github.com?redirect=evil", "https://api.github.com#fragment",
        ):
            with self.subTest(api=bad_api), patch.object(updater, "API", bad_api), \
                    patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(updater.UpdateError):
                    updater.gh_request("/releases/latest")
                urlopen.assert_not_called()

    def test_gh_request_declared_and_streamed_metadata_caps_and_closes(self):
        declared = FakeResponse(b"{}", headers={"Content-Length": str(updater.METADATA_MAX_BYTES + 1)})
        with patch("urllib.request.urlopen", return_value=declared):
            with self.assertRaisesRegex(updater.UpdateError, "too large"):
                updater.gh_request("/releases/latest")
        self.assertTrue(declared.closed)
        self.assertEqual(declared.read_calls, [])

        streamed = FakeResponse(b"x" * (updater.METADATA_MAX_BYTES + 1))
        with patch("urllib.request.urlopen", return_value=streamed):
            with self.assertRaisesRegex(updater.UpdateError, "too large"):
                updater.gh_request("/releases/latest")
        self.assertTrue(streamed.closed)
        self.assertLessEqual(max(streamed.read_calls), 64 * 1024)

    def test_gh_request_uses_one_monotonic_deadline_for_reads(self):
        response = FakeResponse(b"metadata")
        # Initial deadline, urlopen timeout calculation, then a read after the
        # deadline. A fresh socket timeout on every read would not fail here.
        with patch("urllib.request.urlopen", return_value=response), \
                patch.object(updater.time, "monotonic", side_effect=[0.0, 0.0, 6.0]):
            with self.assertRaisesRegex(updater.UpdateError, "timed out"):
                updater.gh_request("/releases/latest", timeout=5)
        self.assertTrue(response.closed)

    def test_response_read_refreshes_socket_timeout_and_checks_after_read(self):
        response = FakeResponse(b"ok")
        with patch.object(updater.time, "monotonic", side_effect=[10.0, 10.25]):
            self.assertEqual(
                updater._response_read(response, 2, 11.0, label="download"), b"ok"
            )
        self.assertEqual(response.timeout_calls, [1.0])

        late = FakeResponse(b"late")
        with patch.object(updater.time, "monotonic", side_effect=[10.0, 11.1]):
            with self.assertRaisesRegex(updater.UpdateError, "timed out"):
                updater._response_read(late, 4, 11.0, label="download")
        self.assertEqual(late.timeout_calls, [1.0])

    def test_gh_request_rejects_untrusted_final_host_and_accepts_statuses(self):
        redirected = FakeResponse(b"ok", url="https://evil.example/releases/latest")
        with patch("urllib.request.urlopen", return_value=redirected):
            with self.assertRaisesRegex(updater.UpdateError, "untrusted host"):
                updater.gh_request("/releases/latest")
        self.assertTrue(redirected.closed)

        response = FakeResponse(b"denied", status=403, url="https://api.github.com/other")
        with patch("urllib.request.urlopen", return_value=response):
            status, headers, body = updater.gh_request("/releases/latest")
        self.assertEqual((status, body), (403, b"denied"))
        self.assertTrue(response.closed)

        fp = io.BytesIO()
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/workbench/releases/latest", 404,
            "not found", {}, fp,
        )
        with patch("urllib.request.urlopen", side_effect=error):
            status, _, body = updater.gh_request("/releases/latest")
        self.assertEqual((status, body), (404, b""))
        self.assertTrue(fp.closed)

        evil_error = urllib.error.HTTPError(
            "https://evil.example/redirected", 404, "not found", {}, io.BytesIO(),
        )
        with patch("urllib.request.urlopen", side_effect=evil_error):
            with self.assertRaisesRegex(updater.UpdateError, "untrusted host"):
                updater.gh_request("/releases/latest")

    def test_latest_release_rejects_strict_shapes_and_accepts_valid_digest(self):
        with self.mock_metadata(self.release_raw()) as request:
            result = updater.latest_release()
        request.assert_called_once_with("/repos/owner/workbench/releases/latest", timeout=25)
        self.assertEqual(result["assets"][0]["id"], 7)
        self.assertEqual(result["assets"][0]["digest"], "sha256:" + "a" * 64)
        self.assertEqual(set(result), {"tag", "name", "body", "published", "url", "assets"})

        invalid = [
            ("tag", {"tag_name": "../escape"}),
            ("body type", {"body": 42}),
            ("html URL", {"html_url": "https://github.com/owner/other/releases/tag/v2.0.0"}),
            ("assets type", {"assets": {}}),
            ("too many assets", {"assets": [{}] * 101}),
        ]
        for label, change in invalid:
            with self.subTest(label=label):
                raw = self.release_raw(**change)
                with self.mock_metadata(raw):
                    with self.assertRaises(updater.UpdateError):
                        updater.latest_release()

        asset = self.release_raw()["assets"][0]
        bad_assets = [
            ("id", {**asset, "id": True}),
            ("name", {**asset, "name": "../scm-workbench-macos.zip"}),
            ("name separator", {**asset, "name": "macos\\zip"}),
            ("URL binding", {**asset, "browser_download_url": "https://github.com/owner/workbench/releases/download/v1.0.0/scm-workbench-macos.zip"}),
            ("size", {**asset, "size": 0}),
            ("size bool", {**asset, "size": True}),
            ("size max", {**asset, "size": updater.ASSET_MAX_BYTES + 1}),
            ("digest", {**asset, "digest": "sha256:not-a-digest"}),
        ]
        for label, bad in bad_assets:
            with self.subTest(asset=label):
                with self.mock_metadata(self.release_raw(assets=[bad])):
                    with self.assertRaises(updater.UpdateError):
                        updater.latest_release()

    def test_latest_release_accepts_multiline_body_but_rejects_forbidden_controls(self):
        body = "first line\nsecond line\r\nindented\tline"
        with self.mock_metadata(self.release_raw(body=body)):
            self.assertEqual(updater.latest_release()["body"], body)

        for control in ("\x00", "\x01", "\x1f", "\x7f"):
            with self.subTest(control=repr(control)), self.mock_metadata(
                    self.release_raw(body="safe" + control)):
                with self.assertRaises(updater.UpdateError):
                    updater.latest_release()

    def test_latest_release_enforces_final_body_size_and_field_limits(self):
        for field, value in (
            ("body", "x" * (256 * 1024 + 1)),
            ("name", "x" * 513),
            ("published_at", "x" * 65),
            ("tag_name", "x" * 129),
        ):
            with self.subTest(field=field):
                with self.mock_metadata(self.release_raw(**{field: value})):
                    with self.assertRaises(updater.UpdateError):
                        updater.latest_release()

        # The body cap is checked after decoding too, not just by a server's
        # Content-Length declaration (which is absent from this mocked API).
        body = json.dumps(self.release_raw(body="valid"), ensure_ascii=False).encode()
        with patch.object(updater, "gh_request", return_value=(200, {}, body)):
            self.assertEqual(updater.latest_release()["body"], "valid")

    def test_pick_asset_requires_exact_architecture_name_and_unique_match(self):
        mac = {"name": "scm-workbench-macos.zip", "id": 1}
        win = {"name": "scm-workbench-windows.zip", "id": 2}
        release = {"assets": [
            {"name": "scm-workbench-macos-arm64.zip"},
            mac,
            win,
            {"name": "windows-debug.zip"},
        ]}
        self.assertIs(updater.pick_asset(release, "darwin-arm64"), mac)
        self.assertIs(updater.pick_asset(release, "macos"), mac)
        self.assertIs(updater.pick_asset(release, "windows-x64"), win)
        self.assertIs(updater.pick_asset(release, "win32"), win)

        for platform in ("linux", "darwin", "win32"):
            with self.subTest(platform=platform):
                assets = list(release["assets"])
                expected = "scm-workbench-macos.zip" if platform == "darwin" else "scm-workbench-windows.zip"
                assets.append({"name": expected})
                with self.assertRaises(updater.UpdateError):
                    updater.pick_asset({"assets": assets}, platform)
        with self.assertRaises(updater.UpdateError):
            updater.pick_asset({"assets": [mac]}, "linux")


class UpdaterDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-updater-download-")
        self.repo = patch.object(updater, "UPDATE_REPO", "owner/workbench")
        self.repo.start()

    def tearDown(self):
        self.repo.stop()
        self.temp.cleanup()

    def asset(self, **changes):
        asset = {
            "id": 7,
            "tag": "v2.0.0",
            "name": "scm-workbench-macos.zip",
            "url": "https://github.com/owner/workbench/releases/download/v2.0.0/"
                   "scm-workbench-macos.zip",
            "size": 3,
            "digest": None,
        }
        asset.update(changes)
        return asset

    def dest(self):
        return Path(self.temp.name) / "download.zip"

    def response(self, body=b"new", *, url=None, headers=None):
        response_headers = {"Content-Length": str(len(body))}
        if headers:
            response_headers.update(headers)
        return FakeResponse(body, headers=response_headers, url=url or self.asset()["url"])

    def assert_no_partial(self, dest):
        self.assertEqual(list(dest.parent.glob(f".{dest.name}.*.part")), [])

    def test_download_requires_validated_asset_and_initial_binding(self):
        dest = self.dest()
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(updater.UpdateError, "validated"):
                updater.download(self.asset()["url"], dest)
            urlopen.assert_not_called()

        base = self.asset()
        bad_urls = {
            "owner": base["url"].replace("/owner/workbench/", "/other/workbench/"),
            "repo": base["url"].replace("/owner/workbench/", "/owner/other/"),
            "tag": base["url"].replace("/download/v2.0.0/", "/download/v1.0.0/"),
            "name": base["url"].replace(base["name"], "other.zip"),
        }
        for label, bad_url in bad_urls.items():
            with self.subTest(binding=label), patch("urllib.request.urlopen") as urlopen:
                bad = dict(base, url=bad_url)
                with self.assertRaises(updater.UpdateError):
                    updater.download(bad_url, dest, expected_asset=bad)
                urlopen.assert_not_called()

    def test_download_accepts_only_exact_https_approved_final_hosts(self):
        asset = self.asset()
        approved = (
            "https://github.com/releases/download/v2.0.0/scm-workbench-macos.zip",
            "https://release-assets.githubusercontent.com/download/asset",
            "https://objects.githubusercontent.com/download/asset",
        )
        for final_url in approved:
            with self.subTest(host=final_url), tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / "download.zip"
                response = self.response(url=final_url)
                with patch("urllib.request.urlopen", return_value=response):
                    self.assertEqual(updater.download(asset["url"], dest,
                                                      expected_asset=asset), 3)
                self.assertTrue(response.closed)
                self.assertEqual(dest.read_bytes(), b"new")

        rejected = (
            "http://github.com/download/asset",
            "https://github.com.evil.example/download/asset",
            "https://release-assets.githubusercontent.com.evil.example/download/asset",
            "https://github.com:443/download/asset",
            "https://user:pass@github.com/download/asset",
        )
        for final_url in rejected:
            with self.subTest(host=final_url):
                dest = self.dest()
                response = self.response(url=final_url)
                with patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaisesRegex(updater.UpdateError, "untrusted host"):
                        updater.download(asset["url"], dest, expected_asset=asset)
                self.assertTrue(response.closed)
                self.assert_no_partial(dest)

    def test_download_rejects_malformed_mismatched_and_oversize_content_length(self):
        asset = self.asset()
        for label, value in (("malformed", "three"), ("mismatch", "2"),
                             ("oversize", str(updater.ASSET_MAX_BYTES + 1))):
            with self.subTest(length=label):
                dest = self.dest()
                dest.write_bytes(b"old")
                response = self.response(headers={"Content-Length": value})
                with patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(updater.UpdateError):
                        updater.download(asset["url"], dest, expected_asset=asset)
                self.assertTrue(response.closed)
                self.assertEqual(dest.read_bytes(), b"old")
                self.assert_no_partial(dest)

    def test_download_without_content_length_reports_unknown_progress_total(self):
        asset = self.asset()
        dest = self.dest()
        response = FakeResponse(b"new", headers={}, url=asset["url"])
        progress = []
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(updater.download(asset["url"], dest, progress=lambda done, total: progress.append((done, total)),
                                              expected_asset=asset), 3)
        self.assertEqual(progress, [(0, 0), (3, 0)])
        self.assertEqual(dest.read_bytes(), b"new")
        self.assertTrue(response.closed)

    def test_download_requires_exact_stream_size_and_cleans_failed_partials(self):
        asset = self.asset()
        for label, body in (("under", b"ab"), ("over", b"abcd")):
            with self.subTest(stream=label):
                dest = self.dest()
                dest.write_bytes(b"old")
                response = self.response(body)
                with patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(updater.UpdateError):
                        updater.download(asset["url"], dest, expected_asset=asset)
                self.assertTrue(response.closed)
                self.assertEqual(dest.read_bytes(), b"old")
                self.assert_no_partial(dest)

    def test_download_has_one_total_deadline_and_closes_response(self):
        dest = self.dest()
        response = self.response()
        with patch("urllib.request.urlopen", return_value=response), \
                patch.object(updater.time, "monotonic",
                             side_effect=[0.0, 0.0, 0.0, 2.0]):
            with self.assertRaisesRegex(updater.UpdateError, "timed out"):
                updater.download(self.asset()["url"], dest,
                                 expected_asset=self.asset(), timeout=1)
        self.assertTrue(response.closed)
        self.assert_no_partial(dest)

    def test_download_default_and_maximum_total_deadline_are_sixty_seconds(self):
        for supplied_timeout in (60, 600):
            with self.subTest(timeout=supplied_timeout), tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / "download.zip"
                response = self.response()
                with patch("urllib.request.urlopen", return_value=response) as urlopen, \
                        patch.object(updater.time, "monotonic", return_value=100.0):
                    updater.download(self.asset()["url"], dest,
                                     expected_asset=self.asset(), timeout=supplied_timeout)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], 60.0)
                self.assertTrue(response.closed)

    def test_download_cleans_partial_when_open_or_replace_fails(self):
        asset = self.asset()
        dest = self.dest()
        dest.write_bytes(b"old")
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(updater.UpdateError):
                updater.download(asset["url"], dest, expected_asset=asset)
        self.assertEqual(dest.read_bytes(), b"old")
        self.assert_no_partial(dest)

        response = self.response()
        with patch("urllib.request.urlopen", return_value=response), \
                patch.object(updater.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(updater.UpdateError):
                updater.download(asset["url"], dest, expected_asset=asset)
        self.assertTrue(response.closed)
        self.assertEqual(dest.read_bytes(), b"old")
        self.assert_no_partial(dest)

    def test_download_matches_sha256_and_preserves_existing_dest_on_failure(self):
        body = b"new"
        asset = self.asset(digest="sha256:" + hashlib.sha256(body).hexdigest())
        dest = self.dest()
        dest.write_bytes(b"old")
        response = self.response(body)
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(updater.download(asset["url"], dest,
                                               expected_asset=asset), len(body))
        self.assertTrue(response.closed)
        self.assertEqual(dest.read_bytes(), body)
        self.assert_no_partial(dest)

        bad = self.asset(digest="sha256:" + "0" * 64)
        response = self.response(body)
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(updater.UpdateError, "digest"):
                updater.download(bad["url"], dest, expected_asset=bad)
        self.assertTrue(response.closed)
        self.assertEqual(dest.read_bytes(), body)
        self.assert_no_partial(dest)

    def test_download_replaces_destination_only_after_complete_success(self):
        asset = self.asset()
        dest = self.dest()
        dest.write_bytes(b"old")
        response = self.response()
        real_replace = updater.os.replace
        observed = []

        def replace(part, final):
            observed.append((Path(part).exists(), dest.read_bytes()))
            real_replace(part, final)

        with patch("urllib.request.urlopen", return_value=response), \
                patch.object(updater.os, "replace", side_effect=replace):
            updater.download(asset["url"], dest, expected_asset=asset)
        self.assertEqual(observed, [(True, b"old")])
        self.assertEqual(dest.read_bytes(), b"new")
        self.assert_no_partial(dest)


class UpdaterJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-updater-job-")
        self.repo = patch.object(updater, "UPDATE_REPO", "owner/workbench")
        self.repo.start()

    def tearDown(self):
        self.repo.stop()
        self.temp.cleanup()

    @staticmethod
    def asset(tag="v2.0.0"):
        name = "scm-workbench-macos.zip"
        return {
            "id": 7,
            "tag": tag,
            "name": name,
            "url": "https://github.com/owner/workbench/releases/download/" + tag + "/" + name,
            "size": 3,
            "digest": None,
        }

    def plan(self, stale_asset):
        return {
            "repo": "owner/workbench", "current": "1.0.0", "latest": "v9.0.0",
            "asset": stale_asset, "work": self.temp.name, "bundle": None,
            "force": False,
        }

    @staticmethod
    def job():
        return {"log_lines": [], "subs": [], "started": time.time()}

    def test_run_job_uses_reverified_asset_not_stale_plan_asset(self):
        current = self.asset("v2.0.0")
        stale = self.asset("v1.0.0")
        release = {"tag": "v2.0.0", "assets": [current]}
        job = self.job()
        with patch.object(updater, "latest_release", return_value=release), \
                patch.object(updater, "download",
                              side_effect=updater.UpdateError("stop after selection")) as download:
            updater.run_job(job, self.plan(stale), io.StringIO())
        download.assert_called_once()
        args, kwargs = download.call_args
        self.assertEqual(args[0], current["url"])
        self.assertIs(kwargs["expected_asset"], current)
        self.assertNotEqual(kwargs["expected_asset"], stale)
        self.assertEqual(job["status"], "fail")

    def test_run_job_rejects_reverified_asset_bound_to_wrong_tag_before_download(self):
        mismatched = self.asset("v1.0.0")
        release = {"tag": "v2.0.0", "assets": [mismatched]}
        job = self.job()
        with patch.object(updater, "latest_release", return_value=release), \
                patch.object(updater, "download") as download:
            updater.run_job(job, self.plan(self.asset("v1.0.0")), io.StringIO())
        download.assert_not_called()
        self.assertEqual(job["status"], "fail")
        self.assertIn("not bound to the release tag", "".join(job["log_lines"]))


class UpdateStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-updater-state-")
        self.old = {
            "DATA_DIR": server.DATA_DIR,
            "UPDATE_STATE_FILE": server.UPDATE_STATE_FILE,
            "SERVER_VERSION": server.SERVER_VERSION,
        }
        server.DATA_DIR = Path(self.temp.name)
        server.UPDATE_STATE_FILE = Path(self.temp.name) / "update-state.json"
        server.SERVER_VERSION = "1.0.0"
        self.repo = patch.object(updater, "UPDATE_REPO", "owner/workbench")
        self.repo.start()
        server._RELEASE_NOTES_CACHE.clear()
        with server._UPDATE_CHECK_CONDITION:
            server._UPDATE_CHECKING = False
            server._UPDATE_CHECK_GENERATION = 0
            server._UPDATE_CHECK_RESULT = None

    def tearDown(self):
        with server._UPDATE_CHECK_CONDITION:
            server._UPDATE_CHECKING = False
            server._UPDATE_CHECK_CONDITION.notify_all()
        server._RELEASE_NOTES_CACHE.clear()
        server.DATA_DIR = self.old["DATA_DIR"]
        server.UPDATE_STATE_FILE = self.old["UPDATE_STATE_FILE"]
        server.SERVER_VERSION = self.old["SERVER_VERSION"]
        self.repo.stop()
        self.temp.cleanup()

    def valid_state(self, **changes):
        state = server._default_update_state()
        state.update(status="up-to-date", latest="v2.0.0", checked_at=10.0)
        state.update(changes)
        return state

    def test_corrupt_state_is_safe_and_never_repaired_or_overwritten(self):
        path = server.UPDATE_STATE_FILE
        path.write_bytes(b"{not-json")
        before = path.read_bytes()
        loaded = server.load_update_state()
        self.assertEqual(loaded["status"], "error")
        self.assertEqual(loaded["reason"], "saved update state is invalid")
        self.assertEqual(path.read_bytes(), before)

        path.write_text(json.dumps({"status": "up-to-date", "extra": "field"}), encoding="utf-8")
        before = path.read_bytes()
        self.assertEqual(server.load_update_state()["status"], "error")
        self.assertEqual(path.read_bytes(), before)

        path.write_bytes(b"x" * (server._UPDATE_STATE_MAX_BYTES + 1))
        self.assertEqual(server.load_update_state()["status"], "error")
        self.assertEqual(path.read_bytes(), b"x" * (server._UPDATE_STATE_MAX_BYTES + 1))

    def test_symlink_state_is_rejected_without_touching_target(self):
        target = Path(self.temp.name) / "target.json"
        target.write_text("target", encoding="utf-8")
        try:
            server.UPDATE_STATE_FILE.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable: %s" % error)
        with self.assertRaises(OSError):
            server.save_update_state(self.valid_state())
        self.assertEqual(target.read_text(encoding="utf-8"), "target")
        self.assertTrue(server.UPDATE_STATE_FILE.is_symlink())

    def test_atomic_temp_or_replace_failure_keeps_previous_state_and_cleans_temp(self):
        server.save_update_state(self.valid_state())
        path = server.UPDATE_STATE_FILE
        before = path.read_bytes()
        with patch.object(server.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                server.save_update_state(self.valid_state(checked_at=11.0))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).glob(".update-state.json.*.tmp")), [])

    def test_update_check_is_singleflight_and_waiters_get_same_result(self):
        asset = {
            "id": 4, "tag": "v2.0.0", "name": "scm-workbench-macos.zip",
            "url": "https://github.com/owner/workbench/releases/download/v2.0.0/scm-workbench-macos.zip",
            "size": 10, "digest": None,
        }
        release = {"tag": "v2.0.0", "name": "two", "body": "notes", "published": "",
                   "url": "https://github.com/owner/workbench/releases/tag/v2.0.0",
                   "assets": [asset]}
        started = threading.Event()
        release_gate = threading.Event()
        calls = []

        def lookup():
            calls.append(threading.get_ident())
            started.set()
            self.assertTrue(release_gate.wait(2))
            return release

        results = []
        with patch.object(updater, "latest_release", side_effect=lookup):
            first = threading.Thread(target=lambda: results.append(server.run_update_check()))
            second = threading.Thread(target=lambda: results.append(server.run_update_check()))
            first.start()
            self.assertTrue(started.wait(2))
            second.start()
            time.sleep(0.03)
            self.assertEqual(len(calls), 1)
            release_gate.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(server.load_update_state(), results[0])

    def test_update_check_generation_order_publishes_newer_result(self):
        def release(tag, name):
            return {
                "tag": tag, "name": name, "body": "", "published": "",
                "url": "", "assets": [{
                    "id": 1, "tag": tag, "name": "scm-workbench-macos.zip",
                    "url": "https://github.com/owner/workbench/releases/download/"
                           + tag + "/scm-workbench-macos.zip",
                    "size": 1, "digest": None,
                }],
            }
        older = release("v2.0.0", "old")
        newer = release("v3.0.0", "new")
        with patch.object(updater, "latest_release", side_effect=[older, newer]):
            first = server.run_update_check()
            second = server.run_update_check()
        self.assertEqual(first["latest"], "v2.0.0")
        self.assertEqual(second["latest"], "v3.0.0")
        self.assertEqual(server.load_update_state()["latest"], "v3.0.0")
        self.assertGreater(server._UPDATE_CHECK_GENERATION, 1)


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-updater-notes-")
        self.old = (server.DATA_DIR, server.UPDATE_STATE_FILE)
        server.DATA_DIR = Path(self.temp.name)
        server.UPDATE_STATE_FILE = Path(self.temp.name) / "update-state.json"
        self.repo = patch.object(updater, "UPDATE_REPO", "owner/workbench")
        self.repo.start()
        server._RELEASE_NOTES_CACHE.clear()
        self.state = server._default_update_state()
        self.state.update(status="up-to-date", latest="v2.0.0", checked_at=10.0,
                          release_url="https://github.com/owner/workbench/releases/tag/v2.0.0",
                          published="2026-09-06T12:30:00Z")
        server.save_update_state(self.state)

    def tearDown(self):
        server._RELEASE_NOTES_CACHE.clear()
        server.DATA_DIR, server.UPDATE_STATE_FILE = self.old
        self.repo.stop()
        self.temp.cleanup()

    @staticmethod
    def release(tag, body="notes", name=None):
        return {"tag": tag, "name": name or tag, "body": body,
                "published": "2026-09-06T12:30:00Z",
                "url": "https://github.com/owner/workbench/releases/tag/" + tag,
                "assets": []}

    def test_expected_tag_binding_rejects_other_release_without_network(self):
        with patch.object(updater, "latest_release") as latest:
            result = server.release_notes_view(expected_tag="v1.0.0")
        self.assertFalse(result["ok"])
        latest.assert_not_called()

    def test_release_notes_cache_ttl_and_bounded_count(self):
        calls = []
        with patch.object(updater, "latest_release", side_effect=lambda timeout=15: (calls.append(1) or self.release("v2.0.0"))):
            first = server.release_notes_view()
            second = server.release_notes_view()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

        server._RELEASE_NOTES_CACHE["v2.0.0"] = (time.monotonic() - server._RELEASE_NOTES_CACHE_TTL - 1, first)
        with patch.object(updater, "latest_release", return_value=self.release("v2.0.0")) as latest:
            server.release_notes_view()
        latest.assert_called_once()

        for i in range(server._RELEASE_NOTES_CACHE_MAX + 2):
            tag = "v%d.0.0" % (10 + i)
            state = dict(
                self.state, latest=tag,
                release_url="https://github.com/owner/workbench/releases/tag/" + tag,
            )
            server.save_update_state(state)
            with patch.object(updater, "latest_release", return_value=self.release(tag)):
                server.release_notes_view()
        self.assertLessEqual(len(server._RELEASE_NOTES_CACHE), server._RELEASE_NOTES_CACHE_MAX)
        self.assertNotIn("v2.0.0", server._RELEASE_NOTES_CACHE)

    def test_notes_fall_back_to_safe_state_metadata_when_network_fails(self):
        with patch.object(updater, "latest_release", side_effect=updater.UpdateError("offline")):
            result = server.release_notes_view(expected_tag="v2.0.0")
        self.assertTrue(result["ok"])
        self.assertEqual(result["tag"], "v2.0.0")
        self.assertEqual(result["url"], self.state["release_url"])
        self.assertEqual(result["body"], "")

    def test_markdown_is_escaped_and_only_intended_tags_are_emitted(self):
        source = '''# Heading <img src=x onerror=alert(1)>
<script>alert(1)</script>
Raw <b>HTML</b> and [\"><img src=x onerror=alert(1)>](https://example.com)
[x](javascript:alert(1)) [x](data:text/html,alert(1))
- **bold** and *italic* with `inline <script>`
```html
<script>alert(1)</script>
<img src=x onerror=alert(1)>
```'''
        rendered = server._render_release_notes(source)
        allowed = {"h1", "h2", "h3", "h4", "p", "ul", "li", "code", "pre", "b", "i"}
        tags = re.findall(r"</?([A-Za-z][A-Za-z0-9]*)\b", rendered)
        self.assertTrue(set(tags) <= allowed, rendered)
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("<img", rendered.lower())
        self.assertNotRegex(rendered.lower(), r"<[^>]*onerror=")
        self.assertNotRegex(rendered.lower(), r"<[^>]*href=")
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("<pre><code>", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("<b>bold</b>", rendered)
        self.assertIn("<i>italic</i>", rendered)

    def test_markdown_source_and_rendered_caps(self):
        with self.assertRaises(updater.UpdateError):
            server._render_release_notes("x" * (server._RELEASE_NOTES_SOURCE_MAX + 1))
        # Blank-separated paragraphs add enough renderer markup to exceed the
        # output cap while remaining below the source cap.
        source = "a\n\n" * 70000
        self.assertLess(len(source.encode()), server._RELEASE_NOTES_SOURCE_MAX)
        with self.assertRaisesRegex(updater.UpdateError, "rendered"):
            server._render_release_notes(source)


if __name__ == "__main__":
    unittest.main()
