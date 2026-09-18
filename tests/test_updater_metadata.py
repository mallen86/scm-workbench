import hashlib
import io
import json
import os
import re
import tempfile
import threading
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scm_workbench import server, updater


def current_asset_name():
    return updater.expected_asset_name()


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
    def release_raw(tag="v2.0.0", *, body="notes", assets=None,
                    prerelease=None, **extra):
        asset_name = "scm-workbench-macos.dmg"
        prerelease = updater.is_prerelease(tag) if prerelease is None else prerelease
        assets = assets if assets is not None else [{
            "id": 7,
            "name": asset_name,
            "browser_download_url":
                f"https://github.com/owner/workbench/releases/download/{tag}/"
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
            "draft": False,
            "prerelease": prerelease,
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
        self.assertEqual(set(result), {"tag", "name", "body", "published", "url", "assets", "prerelease"})
        self.assertFalse(result["prerelease"])

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

    def test_semver_prerelease_ordering_is_numeric_and_stable_aware(self):
        ordered = [
            "v0.9.0-alpha", "v0.9.0-alpha.1", "v0.9.0-alpha.beta",
            "v0.9.0-beta", "v0.9.0-beta.2", "v0.9.0-beta.10",
            "v0.9.0-rc.1", "v0.9.0",
        ]
        for older, newer in zip(ordered, ordered[1:]):
            with self.subTest(older=older, newer=newer):
                self.assertTrue(updater.is_newer(newer, older))
                self.assertFalse(updater.is_newer(older, newer))
        self.assertEqual(updater.canonical_version("v1.2"), "1.2.0")
        self.assertEqual(updater.canonical_version("v1.2.3-beta.1+build.07"),
                         "1.2.3-beta.1+build.07")
        self.assertEqual(updater.parse_version("v1.2.3+one"),
                         updater.parse_version("v1.2.3+two"))
        self.assertTrue(updater.is_prerelease("v1.2.3-beta.1+build"))
        self.assertFalse(updater.is_prerelease("v1.2.3+build"))
        for invalid in ("v01.2.3", "v1.2.3-beta..1", "v1.2.3-beta.01",
                        "v1.2.3+", " v1.2.3", "v1.2.3 ", "nightly"):
            self.assertIsNone(updater.canonical_version(invalid))
        with self.mock_metadata(self.release_raw("v1.2.3+build.7")):
            self.assertEqual(updater.latest_release()["tag"], "v1.2.3+build.7")

    def test_stable_channel_rejects_prerelease_metadata(self):
        with self.mock_metadata(self.release_raw("v0.9.0-beta.1")):
            with self.assertRaisesRegex(updater.UpdateError, "stable channel"):
                updater.latest_release()

    def test_beta_channel_selects_highest_published_semver(self):
        releases = [
            self.release_raw("v0.8.1"),
            self.release_raw("v0.9.0-beta.1"),
            self.release_raw("v0.9.0-beta.10"),
            self.release_raw("v99.0.0", draft=True),
            {"tag_name": "nightly", "draft": False, "prerelease": False},
        ]
        with self.mock_metadata(releases) as request:
            result = updater.latest_release(include_prereleases=True)
        request.assert_called_once_with(
            f"/repos/owner/workbench/releases?per_page={updater.RELEASE_LIST_MAX}", timeout=25,
        )
        self.assertEqual(result["tag"], "v0.9.0-beta.10")
        self.assertTrue(result["prerelease"])

        releases = [self.release_raw("v1.0.0"), self.release_raw("v0.9.0-rc.1")]
        with self.mock_metadata(releases):
            self.assertEqual(
                updater.latest_release(include_prereleases=True)["tag"], "v1.0.0",
            )

    def test_beta_release_list_is_bounded_and_unambiguous(self):
        invalid = (
            {},
            [self.release_raw()] * (updater.RELEASE_LIST_MAX + 1),
            [self.release_raw("v2.0.0-beta.1", prerelease=False)],
            [self.release_raw("v2.0.0"), self.release_raw("2.0.0")],
            [self.release_raw("v2.0.0+one"), self.release_raw("v2.0.0+two")],
            [{"tag_name": "nightly", "draft": False, "prerelease": False}],
        )
        for document in invalid:
            with self.subTest(shape=type(document).__name__, size=len(document)):
                with self.mock_metadata(document):
                    with self.assertRaises(updater.UpdateError):
                        updater.latest_release(include_prereleases=True)

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

    def test_linux_target_resolution_is_distro_and_architecture_bound(self):
        for release, expected in (
            ({"ID": "debian", "ID_LIKE": ""}, "deb"),
            ({"ID": "ubuntu", "ID_LIKE": "debian"}, "deb"),
            ({"ID": "arch", "ID_LIKE": ""}, "arch"),
            ({"ID": "manjaro", "ID_LIKE": "arch"}, "arch"),
        ):
            with self.subTest(release=release):
                self.assertEqual(updater.linux_package_format(
                    os_release=release, machine="x86_64"), expected)
        for machine in ("aarch64", "arm64", "i686", ""):
            with self.subTest(machine=machine), self.assertRaises(updater.UpdateError):
                updater.linux_package_format(os_release={"ID": "arch"}, machine=machine)
        for release in ({"ID": "fedora"}, {"ID": "endeavouros", "ID_LIKE": "arch"}):
            with self.subTest(release=release), self.assertRaisesRegex(
                    updater.UpdateError, "no package for Linux distribution"):
                updater.linux_package_format(os_release=release, machine="amd64")

    def test_os_release_parser_is_bounded_strict_and_nonexecuting(self):
        parsed = updater._parse_os_release(
            'NAME="Arch Linux"\nID=arch\nID_LIKE="arch linux"\nHOME_URL="https://archlinux.org/"\n'
        )
        self.assertEqual(parsed["ID"], "arch")
        self.assertEqual(parsed["ID_LIKE"], "arch linux")
        for invalid in (
            "ID=arch\nID=manjaro\n",
            "ID =arch\n",
            "ID='unterminated\n",
            "ID=arch extra\n",
            "id=arch\n",
            "ID=arch\x00\n",
            "ID=arch\n" * (updater.OS_RELEASE_MAX_LINES + 1),
            "ID=" + "x" * updater.OS_RELEASE_MAX_BYTES,
        ):
            with self.subTest(sample=invalid[:40]), self.assertRaises(updater.UpdateError):
                updater._parse_os_release(invalid)
        with tempfile.TemporaryDirectory(prefix="scm-os-release-") as temp:
            path = Path(temp) / "os-release"
            path.write_text("ID=manjaro\nID_LIKE=arch\n", encoding="utf-8")
            self.assertEqual(updater._read_os_release(path)["ID"], "manjaro")
            path.write_bytes(b"x" * (updater.OS_RELEASE_MAX_BYTES + 1))
            with self.assertRaisesRegex(updater.UpdateError, "too large"):
                updater._read_os_release(path)

    def test_pick_asset_requires_exact_target_name_and_unique_match(self):
        mac = {"name": updater.MACOS_DMG_ASSET, "id": 1}
        legacy = {"name": "scm-workbench-macos.zip", "id": 9}
        win = {"name": updater.WINDOWS_ASSET, "id": 2}
        deb = {"name": updater.LINUX_DEB_ASSET, "id": 3}
        arch = {"name": updater.LINUX_ARCH_ASSET, "id": 4}
        release = {"assets": [
            {"name": "scm-workbench-macos-arm64.zip"},
            legacy, mac, win, deb, arch, {"name": "windows-debug.zip"},
        ]}
        self.assertIs(updater.pick_asset(release, "darwin-arm64"), mac)
        self.assertIs(updater.pick_asset(release, "macos"), mac)
        self.assertIs(updater.pick_asset(release, "windows-x64"), win)
        self.assertIs(updater.pick_asset(release, "win32"), win)
        self.assertIs(updater.pick_asset(release, "linux-deb-amd64"), deb)
        self.assertIs(updater.pick_asset(release, "linux-arch-x86_64"), arch)
        self.assertEqual(updater.install_mode("linux"), "manual")
        self.assertEqual(updater.install_mode("manjaro"), "manual")
        self.assertEqual(updater.install_mode("darwin"), "automatic")

        for target, expected in (
            ("linux-deb-amd64", updater.LINUX_DEB_ASSET),
            ("linux-arch-x86_64", updater.LINUX_ARCH_ASSET),
            ("darwin", updater.MACOS_DMG_ASSET),
            ("win32", updater.WINDOWS_ASSET),
        ):
            with self.subTest(platform=target):
                assets = list(release["assets"])
                assets.append({"name": expected})
                with self.assertRaises(updater.UpdateError):
                    updater.pick_asset({"assets": assets}, target)
        with self.assertRaises(updater.UpdateError):
            updater.pick_asset({"assets": [mac, deb]}, "linux-arch-x86_64")

    def test_linux_packages_are_metadata_only_and_never_self_installed(self):
        for name, suffix in (
            (updater.LINUX_DEB_ASSET, ".deb"),
            (updater.LINUX_ARCH_ASSET, ".pkg.tar.zst"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                    updater.UpdateError, "installed manually"):
                updater.prepare_asset(
                    {"name": name}, Path("download" + suffix),
                    Path("candidate"), "v2.0.0",
                )

    def test_macos_requires_dmg_and_never_falls_back_to_legacy_zip(self):
        dmg = {"name": updater.MACOS_DMG_ASSET, "id": 10}
        legacy = {"name": "scm-workbench-macos.zip", "id": 11}
        self.assertIs(updater.pick_asset({"assets": [legacy, dmg]}, "darwin"), dmg)
        with self.assertRaisesRegex(updater.UpdateError, "unambiguous"):
            updater.pick_asset({"assets": [legacy]}, "darwin")
        with self.assertRaisesRegex(updater.UpdateError, "unambiguous"):
            updater.pick_asset({"assets": [dmg, dict(dmg, id=12)]}, "darwin")


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
        self.mode = patch.object(updater, "install_mode", return_value="automatic")
        self.repo.start()
        self.mode.start()

    def tearDown(self):
        self.mode.stop()
        self.repo.stop()
        self.temp.cleanup()

    @staticmethod
    def asset(tag="v2.0.0"):
        name = current_asset_name()
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
        with patch.object(updater, "latest_release", return_value=release) as lookup, \
                patch.object(updater, "download",
                              side_effect=updater.UpdateError("stop after selection")) as download:
            updater.run_job(job, self.plan(stale), io.StringIO())
        lookup.assert_called_once_with(include_prereleases=False)
        download.assert_called_once()
        args, kwargs = download.call_args
        self.assertEqual(args[0], current["url"])
        self.assertIs(kwargs["expected_asset"], current)
        self.assertNotEqual(kwargs["expected_asset"], stale)
        self.assertEqual(job["status"], "fail")

    def test_run_job_reverifies_the_checked_beta_channel(self):
        current = self.asset("v2.0.0-beta.2")
        release = {"tag": "v2.0.0-beta.2", "assets": [current]}
        plan = self.plan(self.asset("v1.0.0"))
        plan["channel"] = "beta"
        job = self.job()
        with patch.object(updater, "latest_release", return_value=release) as lookup, \
                patch.object(updater, "download",
                              side_effect=updater.UpdateError("stop after selection")):
            updater.run_job(job, plan, io.StringIO())
        lookup.assert_called_once_with(include_prereleases=True)
        self.assertEqual(job["status"], "fail")

    def test_run_job_stable_channel_rejects_a_prerelease_before_download(self):
        prerelease = self.asset("v2.0.0-beta.1")
        release = {"tag": "v2.0.0-beta.1", "prerelease": True,
                   "assets": [prerelease]}
        job = self.job()
        with patch.object(updater, "latest_release", return_value=release), \
                patch.object(updater, "download") as download:
            updater.run_job(job, self.plan(self.asset("v1.0.0")), io.StringIO())
        download.assert_not_called()
        self.assertEqual(job["status"], "fail")
        self.assertIn("stable channel returned a prerelease", "".join(job["log_lines"]))

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
            "SETTINGS_FILE": server.SETTINGS_FILE,
            "UPDATE_STATE_FILE": server.UPDATE_STATE_FILE,
            "SERVER_VERSION": server.SERVER_VERSION,
        }
        server.DATA_DIR = Path(self.temp.name)
        server.SETTINGS_FILE = Path(self.temp.name) / "settings.json"
        server.UPDATE_STATE_FILE = Path(self.temp.name) / "update-state.json"
        server.SERVER_VERSION = "1.0.0"
        server.save_settings(json.loads(json.dumps(server.DEFAULT_SETTINGS)))
        self.repo = patch.object(updater, "UPDATE_REPO", "owner/workbench")
        self.repo.start()
        server._RELEASE_NOTES_CACHE.clear()
        with server._UPDATE_CHECK_CONDITION:
            self.old_update_check = (
                server._UPDATE_CHECKING, server._UPDATE_CHECK_GENERATION,
                server._UPDATE_CHECK_RESULT,
            )
            server._UPDATE_CHECKING = False
            server._UPDATE_CHECK_GENERATION = 0
            server._UPDATE_CHECK_RESULT = None

    def tearDown(self):
        with server._UPDATE_CHECK_CONDITION:
            (server._UPDATE_CHECKING, server._UPDATE_CHECK_GENERATION,
             server._UPDATE_CHECK_RESULT) = self.old_update_check
            server._UPDATE_CHECK_CONDITION.notify_all()
        server._RELEASE_NOTES_CACHE.clear()
        server.DATA_DIR = self.old["DATA_DIR"]
        server.SETTINGS_FILE = self.old["SETTINGS_FILE"]
        server.UPDATE_STATE_FILE = self.old["UPDATE_STATE_FILE"]
        server.SERVER_VERSION = self.old["SERVER_VERSION"]
        self.repo.stop()
        self.temp.cleanup()

    @staticmethod
    def installable_asset(tag):
        """A minimal asset the state validator accepts (real owner/repo/name)."""
        name = current_asset_name()
        owner, repo = updater._repo_parts()
        return {"id": 1, "tag": tag, "name": name, "size": 1, "digest": None,
                "url": f"https://github.com/{owner}/{repo}/releases/download/{tag}/{name}"}

    def valid_state(self, **changes):
        state = server._default_update_state("stable")
        state.update(status="up-to-date", latest="v2.0.0", prerelease=False,
                     checked_at=10.0)
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

    def test_cached_asset_for_another_local_target_is_rejected(self):
        tag = "v2.0.0"
        state = self.valid_state(
            status="update-available", latest=tag, checked_at=time.time(),
            asset=self.installable_asset(tag),
        )
        server.save_update_state(state)
        before = server.UPDATE_STATE_FILE.read_bytes()
        wrong_target = (updater.LINUX_ARCH_ASSET
                        if state["asset"]["name"] != updater.LINUX_ARCH_ASSET
                        else updater.LINUX_DEB_ASSET)
        with patch.object(updater, "expected_asset_name", return_value=wrong_target):
            loaded = server.load_update_state()
        self.assertEqual(loaded["status"], "error")
        self.assertEqual(loaded["reason"], "saved update state is invalid")
        self.assertEqual(server.UPDATE_STATE_FILE.read_bytes(), before)

    def test_legacy_stable_state_is_projected_without_rewriting_it(self):
        state = self.valid_state()
        state.pop("channel")
        state.pop("prerelease")
        server.UPDATE_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        before = server.UPDATE_STATE_FILE.read_bytes()

        loaded = server.load_update_state()

        self.assertEqual(loaded["channel"], "stable")
        self.assertFalse(loaded["prerelease"])
        self.assertTrue(server._valid_update_state(loaded))
        self.assertEqual(server.UPDATE_STATE_FILE.read_bytes(), before)

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

    def test_equal_release_is_up_to_date_not_an_installable_update(self):
        asset_name = current_asset_name()
        release = {
            "tag": "v1.0.0", "name": "same", "body": "", "published": "",
            "url": "https://github.com/owner/workbench/releases/tag/v1.0.0",
            "assets": [{
                "id": 1, "tag": "v1.0.0", "name": asset_name,
                "url": "https://github.com/owner/workbench/releases/download/v1.0.0/" + asset_name,
                "size": 1, "digest": None,
            }],
        }
        with patch.object(updater, "latest_release", return_value=release), \
                patch.object(updater, "pick_asset") as pick:
            state = server.run_update_check()
        self.assertEqual(state["status"], "up-to-date")
        self.assertEqual(state["latest"], "v1.0.0")
        self.assertIsNone(state["asset"])
        pick.assert_not_called()

    def test_saved_beta_preference_remains_active_in_simple_mode(self):
        self.assertTrue(server.update_settings({"update_channel": "beta"})["ok"])
        self.assertEqual(server.load_settings()["ui_mode"], "simple")
        self.assertEqual(server._selected_update_channel(), "beta")

        with patch.object(updater, "latest_release",
                          side_effect=updater.UpdateError("offline")) as lookup:
            state = server.run_update_check()

        lookup.assert_called_once_with(include_prereleases=True)
        self.assertEqual(state["channel"], "beta")

    def test_beta_setting_binds_lookup_and_persisted_state(self):
        tag = "v2.0.0-beta.2"
        asset = self.installable_asset(tag)
        release = {
            "tag": tag, "name": "beta", "body": "", "published": "",
            "url": f"https://github.com/owner/workbench/releases/tag/{tag}",
            "assets": [asset], "prerelease": True,
        }
        self.assertTrue(server.update_settings({
            "ui_mode": "advanced", "update_channel": "beta",
        })["ok"])
        with patch.object(updater, "latest_release", return_value=release) as lookup:
            state = server.run_update_check()

        lookup.assert_called_once_with(include_prereleases=True)
        self.assertEqual(state["status"], "update-available")
        self.assertEqual(state["channel"], "beta")
        self.assertTrue(state["prerelease"])
        self.assertEqual(server.load_update_state(), state)

    def test_channel_switch_hides_cache_and_blocks_stale_install(self):
        stable = self.valid_state(
            status="update-available", latest="v2.0.0", prerelease=False,
            checked_at=time.time(), asset=self.installable_asset("v2.0.0"),
        )
        server.save_update_state(stable)
        self.assertTrue(server.update_settings({
            "ui_mode": "advanced", "update_channel": "beta",
        })["ok"])

        view = server.updates_view()["state"]
        self.assertEqual((view["status"], view["channel"]), ("never", "beta"))
        job, errors = server.start_update_job()
        self.assertIsNone(job)
        self.assertIn("No newer update", errors[0])

        checked = server._default_update_state("beta")
        checked.update(status="up-to-date", latest="v2.0.0-beta.1",
                       prerelease=True, checked_at=time.time())
        with patch.object(server, "run_update_check", return_value=checked) as run:
            result = server._update_check_result(False)
        run.assert_called_once_with("beta")
        self.assertEqual(result["state"]["channel"], "beta")

    def test_a_state_promising_the_running_version_is_not_an_update(self):
        """A successful update leaves behind the state that asked for it.

        The app relaunches carrying the "update-available" snapshot written by
        the check, and nothing rewrites it at that moment. Reporting it raw
        made the Settings card and the sidebar notice offer the version the
        user had just installed.
        """
        installed = "v" + server.SERVER_VERSION.lstrip("v")
        stale = self.valid_state(status="update-available", latest=installed,
                                 current="0.0.1", checked_at=time.time(),
                                 asset=self.installable_asset(installed))
        server.save_update_state(stale)

        # The stored bytes are untouched: this is a read-time correction.
        before = server.UPDATE_STATE_FILE.read_bytes()
        view = server.updates_view()
        self.assertEqual(view["state"]["status"], "up-to-date")
        self.assertEqual(server.UPDATE_STATE_FILE.read_bytes(), before)
        # The correction keeps the exact field set the stored-state validator
        # requires (the view adds its own "checking" marker on top).
        corrected = server.current_update_state(stale)
        self.assertTrue(server._valid_update_state(corrected))
        self.assertEqual(corrected["latest"], installed)
        self.assertIsNone(corrected["reason"])
        self.assertEqual(set(corrected) - set(stale), set())

        # The cached check path reads the same state, so it agrees.
        cached = server._update_check_result(False)
        self.assertEqual(cached["state"]["status"], "up-to-date")
        self.assertTrue(cached["state"]["cached"])

    def test_a_state_promising_a_newer_version_stays_installable(self):
        newer = self.valid_state(status="update-available", latest="v99.0.0",
                                 checked_at=time.time(),
                                 asset=self.installable_asset("v99.0.0"))
        server.save_update_state(newer)
        view = server.updates_view()
        self.assertEqual(view["state"]["status"], "update-available")
        self.assertEqual(view["state"]["latest"], "v99.0.0")
        self.assertIsInstance(view["state"]["asset"], dict)

    def test_an_unreadable_promise_is_not_treated_as_an_update(self):
        # A tag that cannot be compared cannot be shown as newer than what is
        # running, so it must not raise the notice either.
        for latest in (None, "not-a-version"):
            state = self.valid_state(status="update-available", latest=latest,
                                     checked_at=time.time())
            self.assertEqual(server.current_update_state(state)["status"], "up-to-date")

    def test_explicit_force_bypasses_a_fresh_cached_update_check(self):
        fresh = self.valid_state(checked_at=time.time())
        server.save_update_state(fresh)
        checked = self.valid_state(latest="v3.0.0", checked_at=time.time())
        with patch.object(server, "run_update_check", return_value=checked) as run:
            cached = server._update_check_result(False)
            forced = server._update_check_result(True)
        self.assertTrue(cached["state"]["cached"])
        self.assertEqual(forced, {"ok": True, "state": checked})
        run.assert_called_once_with("stable")

    def test_ordinary_startup_does_not_wait_for_an_update_result(self):
        with patch.dict(os.environ, {"SCM_WORKBENCH_UPDATE_TOKEN": ""}), \
                patch.object(server, "reconcile_update_result", return_value=False) as reconcile, \
                patch.object(server.time, "sleep") as sleep:
            server._poll_update_result()
        reconcile.assert_called_once_with()
        sleep.assert_not_called()

    def test_reconciled_handoff_does_not_wait_again(self):
        token = "a" * 64
        with patch.dict(os.environ, {"SCM_WORKBENCH_UPDATE_TOKEN": token}), \
                patch.object(server, "read_persisted_jobs", return_value=[]), \
                patch.object(server, "reconcile_update_result", return_value=False) as reconcile, \
                patch.object(server.time, "sleep") as sleep:
            server._poll_update_result()
        reconcile.assert_called_once_with()
        sleep.assert_not_called()

    def test_authenticated_handoff_waits_for_its_pending_result(self):
        token = "a" * 64
        handoff = {"kind": "update", "status": "handoff", "update_token": token}
        with patch.dict(os.environ, {"SCM_WORKBENCH_UPDATE_TOKEN": token}), \
                patch.object(server, "read_persisted_jobs", return_value=[handoff]), \
                patch.object(server, "reconcile_update_result",
                             side_effect=[False, False, True]) as reconcile, \
                patch.object(server.time, "sleep") as sleep:
            server._poll_update_result()
        self.assertEqual(reconcile.call_count, 3)
        sleep.assert_called_once_with(0.5)

    def test_packaging_smoke_can_disable_release_lookups(self):
        server.save_update_state(self.valid_state())
        with patch.dict(os.environ, {"SCM_WORKBENCH_NO_UPDATE_CHECK": "1"}), \
                patch.object(server, "run_update_check") as run:
            result = server._update_check_result(True)
        self.assertTrue(result["state"]["cached"])
        self.assertEqual(result["state"]["status"], "up-to-date")
        run.assert_not_called()

        with patch.dict(os.environ, {"SCM_WORKBENCH_NO_UPDATE_CHECK": "1"}), \
                patch.object(server, "_poll_update_result") as poll, \
                patch.object(server, "run_update_check") as run, \
                patch.object(server.time, "sleep") as sleep:
            server._update_daemon()
        sleep.assert_called_once_with(1)
        poll.assert_called_once_with()
        run.assert_not_called()

    def test_update_check_is_singleflight_and_waiters_get_same_result(self):
        asset_name = current_asset_name()
        asset = {
            "id": 4, "tag": "v2.0.0", "name": asset_name,
            "url": "https://github.com/owner/workbench/releases/download/v2.0.0/" + asset_name,
            "size": 10, "digest": None,
        }
        release = {"tag": "v2.0.0", "name": "two", "body": "notes", "published": "",
                   "url": "https://github.com/owner/workbench/releases/tag/v2.0.0",
                   "assets": [asset]}
        started = threading.Event()
        release_gate = threading.Event()
        calls = []

        def lookup(**kwargs):
            calls.append((threading.get_ident(), kwargs))
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
            self.assertEqual(calls[0][1], {"include_prereleases": False})
            release_gate.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(server.load_update_state(), results[0])

    def test_channel_switch_discards_an_inflight_stable_result(self):
        asset = self.installable_asset("v2.0.0")
        release = {
            "tag": "v2.0.0", "name": "stable", "body": "", "published": "",
            "url": "https://github.com/owner/workbench/releases/tag/v2.0.0",
            "assets": [asset], "prerelease": False,
        }
        started = threading.Event()
        finish = threading.Event()
        results = []

        def lookup(**_kwargs):
            started.set()
            self.assertTrue(finish.wait(2))
            return release

        with patch.object(updater, "latest_release", side_effect=lookup):
            worker = threading.Thread(
                target=lambda: results.append(server.run_update_check("stable")),
            )
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertTrue(server.update_settings({
                "ui_mode": "advanced", "update_channel": "beta",
            })["ok"])
            finish.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual((results[0]["channel"], results[0]["status"]),
                         ("stable", "never"))
        self.assertFalse(server.UPDATE_STATE_FILE.exists())
        with server._UPDATE_CHECK_CONDITION:
            self.assertFalse(server._UPDATE_CHECKING)
            self.assertIsNone(server._UPDATE_CHECK_RESULT)

    def test_update_check_generation_order_publishes_newer_result(self):
        def release(tag, name):
            asset_name = current_asset_name()
            return {
                "tag": tag, "name": name, "body": "", "published": "",
                "url": "", "assets": [{
                    "id": 1, "tag": tag, "name": asset_name,
                    "url": "https://github.com/owner/workbench/releases/download/"
                           + tag + "/" + asset_name,
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
        self.old = (server.DATA_DIR, server.SETTINGS_FILE, server.UPDATE_STATE_FILE)
        server.DATA_DIR = Path(self.temp.name)
        server.SETTINGS_FILE = Path(self.temp.name) / "settings.json"
        server.UPDATE_STATE_FILE = Path(self.temp.name) / "update-state.json"
        server.save_settings(json.loads(json.dumps(server.DEFAULT_SETTINGS)))
        self.repo = patch.object(updater, "UPDATE_REPO", "owner/workbench")
        self.repo.start()
        server._RELEASE_NOTES_CACHE.clear()
        self.state = server._default_update_state("stable")
        self.state.update(status="up-to-date", latest="v2.0.0", prerelease=False,
                          checked_at=10.0,
                          release_url="https://github.com/owner/workbench/releases/tag/v2.0.0",
                          published="2026-09-06T12:30:00Z")
        server.save_update_state(self.state)

    def tearDown(self):
        server._RELEASE_NOTES_CACHE.clear()
        server.DATA_DIR, server.SETTINGS_FILE, server.UPDATE_STATE_FILE = self.old
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
        with patch.object(updater, "latest_release", side_effect=lambda timeout=15, **kwargs: (calls.append(kwargs) or self.release("v2.0.0"))):
            first = server.release_notes_view()
            second = server.release_notes_view()
        self.assertEqual(first, second)
        self.assertEqual(calls, [{"include_prereleases": False}])

        server._RELEASE_NOTES_CACHE["v2.0.0"] = (time.monotonic() - server._RELEASE_NOTES_CACHE_TTL - 1, first)
        with patch.object(updater, "latest_release", return_value=self.release("v2.0.0")) as latest:
            server.release_notes_view()
        latest.assert_called_once_with(timeout=15, include_prereleases=False)

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

    def test_beta_notes_use_the_channel_bound_release_list(self):
        tag = "v2.1.0-beta.1"
        server.update_settings({"ui_mode": "advanced", "update_channel": "beta"})
        state = server._default_update_state("beta")
        state.update(
            status="up-to-date", latest=tag, prerelease=True, checked_at=10.0,
            release_url=f"https://github.com/owner/workbench/releases/tag/{tag}",
            published="2026-09-06T12:30:00Z",
        )
        server.save_update_state(state)
        with patch.object(updater, "latest_release", return_value=self.release(tag)) as latest:
            result = server.release_notes_view(expected_tag=tag)
        self.assertTrue(result["ok"])
        latest.assert_called_once_with(timeout=15, include_prereleases=True)

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
