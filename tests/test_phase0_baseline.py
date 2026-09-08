"""Phase 0 parity checks for the Workbench/native IPC boundary.

These tests deliberately build only the metadata and file shapes the Workbench
reads from its two sister repositories.  The sister repositories remain the
source of truth for their implementations: fixture scripts are empty markers
used to verify delegation paths, never copies of application code.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from scm_workbench import repo_sync, server


PNG = b"\x89PNG\r\n\x1a\nfixture"
JPEG = b"\xff\xd8\xff\xe0fixture"


class Phase0Fixture:
    """A minimal, synthetic SCM + scm-extras checkout shape."""

    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.scm = root / "silhouette-card-maker"
        self.extras = root / "scm-extras"
        self.outside = root / "outside"
        self._make_scm()
        self._make_extras()
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("outside", encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def _marker(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture marker; upstream owns this implementation\n", encoding="utf-8")

    def _make_scm(self) -> None:
        self.scm.mkdir()
        (self.scm / "pyproject.toml").write_text('version = "fixture"\n', encoding="utf-8")
        self._write_json(
            self.scm / "assets/layouts.json",
            {
                "ppi": 300,
                "defaults": {"card_radius": "3mm"},
                "card_sizes": {"standard": {"width": "63mm", "height": "88mm"}},
                "paper_sizes": {"letter": {"width": "8.5in", "height": "11in"}},
                "layouts": {"letter": {"standard": {"default": {}}}},
                "specialty_layouts": {},
            },
        )
        for script in (
            "create_pdf.py",
            "offset_pdf.py",
            "generate_calibration.py",
            "generate_dxf.py",
        ):
            self._marker(self.scm / script)
        self._marker(self.scm / "plugins/mtg/fetch.py")
        for directory in (
            "game/front",
            "game/back",
            "game/double_sided",
            "game/decklist",
            "game/output",
            "cutting_templates/dxf",
        ):
            (self.scm / directory).mkdir(parents=True, exist_ok=True)
        (self.scm / "game/front/card.png").write_bytes(PNG)
        (self.scm / "game/decklist/example.txt").write_text("Fixture Card\n", encoding="utf-8")
        (self.scm / "game/front/README.md").write_text("placeholder", encoding="utf-8")
        (self.scm / "game/back/EMPTY.md").write_text("placeholder", encoding="utf-8")

    def _make_extras(self) -> None:
        self.extras.mkdir()
        self._write_json(
            self.extras / "assets/layouts_extra.json",
            {
                "card_sizes": {"extra": {"width": "70mm", "height": "100mm", "aliases": []}},
                "paper_sizes": {},
                "layouts": {},
            },
        )
        self._marker(self.extras / "generate.py")
        self._marker(self.extras / "generate_readme_tables.py")
        (self.extras / "cutting_templates/dxf").mkdir(parents=True, exist_ok=True)


class HttpContractTests(unittest.TestCase):
    """Exercise contracts through the same loopback HTTP boundary as the UI."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-phase0-")
        cls.fixture = Phase0Fixture(Path(cls.temp.name))

        # server.DATA_DIR is normally selected at import time.  Assigning all
        # derived paths here keeps this test process isolated and avoids ever
        # touching the checkout's real data directory.
        server.DATA_DIR = cls.fixture.data
        server.SETTINGS_FILE = cls.fixture.data / "settings.json"
        server.JOBS_FILE = cls.fixture.data / "jobs.json"
        server.LOGS_DIR = cls.fixture.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = cls.fixture.data / "offsets_by_size.json"
        server.UPDATE_STATE_FILE = cls.fixture.data / "update-state.json"
        os.environ["SCM_WORKBENCH_DATA"] = str(cls.fixture.data)
        server.MANIFEST_CACHE.clear()
        server._INFO_SNAP.clear()
        server._REPOS_MTIME.clear()
        server.save_settings({
            **json.loads(json.dumps(server.DEFAULT_SETTINGS)),
            "scm_dir": str(cls.fixture.scm),
            "extras_dir": str(cls.fixture.extras),
        })
        cls.httpd = server.start_http("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        os.environ.pop("SCM_WORKBENCH_DATA", None)
        cls.temp.cleanup()

    def setUp(self):
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({
            "scm_dir": str(self.fixture.scm),
            "extras_dir": str(self.fixture.extras),
        })
        server.save_settings(settings)
        server.MANIFEST_CACHE.clear()
        server._INFO_SNAP.clear()
        server._REPOS_MTIME.clear()

    def request(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            return error.code, json.loads(raw) if raw else {}

    def test_standalone_browser_serves_root_relative_embedded_assets(self):
        expected_types = {
            "/theme.css": "text/css",
            "/js/app.js": "text/javascript",
            "/logo.svg": "image/svg+xml",
        }
        for path, expected_type in expected_types.items():
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
                    self.assertEqual(response.headers.get_content_type(), expected_type)

    def test_settings_persistence_shape_and_nested_merge(self):
        status, result = self.request("POST", "/api/settings", {
            "scm_dir": str(self.fixture.scm),
            "defaults": {"card_size": "extra", "ppi": 600},
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["settings"]["defaults"]["card_size"], "extra")
        self.assertEqual(result["settings"]["defaults"]["paper_size"], "letter")

        persisted = json.loads(server.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(
            {"scm_dir", "extras_dir", "python", "port", "theme", "ui_mode",
             "auto_open_browser", "onboarded", "defaults", "repos"},
            set(persisted),
        )
        status, loaded = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(loaded, result["settings"])

    def test_allowed_root_rejects_parent_and_symlink_escape(self):
        roots = server.allowed_roots(server.load_settings())
        inside = self.fixture.scm / "game/front/card.png"
        self.assertTrue(server._inside(inside, roots))
        self.assertFalse(server._inside(self.fixture.outside / "secret.txt", roots))

        link = self.fixture.scm / "game/front/outside-link"
        try:
            link.symlink_to(self.fixture.outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable in this checkout: %s" % error)
        self.assertFalse(server._inside(link / "secret.txt", roots))
        status, body = self.request("POST", "/api/fs", {
            "path": str(link / "secret.txt"), "op": "delete_images",
        })
        self.assertEqual(status, 403)
        self.assertIn("outside", body["errors"][0])

        status, body = self.request("POST", "/api/fs", {
            "path": str(self.fixture.scm / ".." / "outside" / "secret.txt"),
            "op": "delete_images",
        })
        self.assertEqual(status, 403)
        self.assertIn("outside", body["errors"][0])

    def test_image_magic_bytes_are_extension_independent(self):
        signatures = {
            "jpeg": JPEG,
            "png": b"\x89PNG\r\n\x1a\n",
            "gif": b"GIF89a",
            "webp": b"RIFFxxxxWEBP",
            "tiff-le": b"II*\x00",
            "tiff-be": b"MM\x00*",
            "bmp": b"BMfixture",
            "avif": b"\x00\x00\x00\x18ftypavif",
            "qoi": b"qoif",
            "dds": b"DDS <wal",
            "jp2": b"\x00\x00\x00\x0cJP 11\x0a\x0d\x08",
        }
        for name, magic in signatures.items():
            path = Path(self.temp.name) / (name + ".not-an-image-extension")
            path.write_bytes(magic)
            self.assertTrue(server.is_image_file(path), name)
        for name, data in {"short": b"", "text": b"not an image", "near": b"RIFFxxxxNOPE"}.items():
            path = Path(self.temp.name) / (name + ".bin")
            path.write_bytes(data)
            self.assertFalse(server.is_image_file(path), name)

    def test_managed_output_collision_naming_via_http(self):
        source = self.fixture.data / "logs" / "output.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"managed output")
        destination = Path(self.temp.name) / "user-files" / "result.pdf"
        destination.parent.mkdir()
        destination.write_bytes(b"existing")
        (destination.with_name("result (2).pdf")).write_bytes(b"existing two")

        status, body = self.request("POST", "/api/files/save", {
            "src": str(source), "dest": str(destination),
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "result (3).pdf")
        self.assertEqual(Path(body["dest"]).read_bytes(), b"managed output")

        status, body = self.request("POST", "/api/files/save", {
            "src": str(self.fixture.outside / "secret.txt"),
            "dest": str(destination),
        })
        self.assertEqual(status, 403)
        self.assertIn("managed location", body["errors"][0])

    def test_url_and_path_rejection_contract(self):
        status, body = self.request("GET", "/api/file?url=" + urllib.parse.quote("file:///etc/passwd"))
        self.assertEqual(status, 400)
        self.assertEqual(body["errors"], ["only http(s) URLs can be opened"])

        status, body = self.request("GET", "/api/file?path=" + urllib.parse.quote("../../outside/secret.txt"))
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "path outside sandbox")

        status, body = self.request("GET", "/api/file?url=" + urllib.parse.quote("javascript:alert(1)"))
        self.assertEqual(status, 400)
        self.assertIn("http(s)", body["errors"][0])

    def test_manifest_has_every_current_job_kind(self):
        status, manifest = self.request("GET", "/api/manifest")
        self.assertEqual(status, 200)
        expected = {
            "create_pdf", "offset_pdf", "calibration", "dxf_single", "dxf_batch",
            "dxf_list", "clean_up", "repo_update", "repo_init", "extras_generate",
            "extras_tables",
        }
        expected.update("fetch:" + slug for slug in server.PLUGINS)
        self.assertEqual(set(manifest), expected)
        for kind, spec in manifest.items():
            self.assertTrue(spec.get("title"), kind)
            self.assertIn("groups", spec, kind)
            self.assertIn("cwd", spec, kind)

    def test_fetch_manifest_keeps_picker_below_source_and_preferences_collapsed(self):
        status, manifest = self.request("GET", "/api/manifest")
        self.assertEqual(status, 200)
        groups = manifest["fetch:mtg"]["groups"]
        self.assertEqual(groups[0]["simple_rows"], [
            ["deck_source"],
            ["deck_file", "deck_name", "deck_text", "deck_url"],
        ])
        self.assertTrue(groups[2]["collapsible"])

    def test_preview_delegates_to_upstream_script_paths(self):
        status, preview = self.request("GET", "/api/preview?" + urllib.parse.urlencode({
            "kind": "create_pdf",
            "args": json.dumps({"card_size": "standard", "paper_size": "letter"}),
        }))
        self.assertEqual(status, 200)
        self.assertEqual(preview["cwd"], str(self.fixture.scm))
        self.assertIn("create_pdf.py", preview["cmd"])
        self.assertFalse(preview["errors"])
        self.assertFalse(preview["no_front_images"])

        status, preview = self.request("GET", "/api/preview?" + urllib.parse.urlencode({
            "kind": "extras_generate", "args": json.dumps({"mode": "missing"}),
        }))
        self.assertEqual(status, 200)
        self.assertEqual(preview["cwd"], str(self.fixture.extras))
        self.assertIn("generate.py", preview["cmd"])
        self.assertFalse(preview["errors"])

        status, preview = self.request("GET", "/api/preview?" + urllib.parse.urlencode({
            "kind": "fetch:mtg",
            "args": json.dumps({"deck_file": "example.txt", "format": "simple"}),
        }))
        self.assertEqual(status, 200)
        self.assertEqual(preview["cwd"], str(self.fixture.scm))
        self.assertIn("plugins/mtg/fetch.py", preview["cmd"])
        self.assertIn("game/decklist/example.txt", preview["cmd"])
        self.assertFalse(preview["errors"])


class RepoSyncFixtureTests(unittest.TestCase):
    """Offline checks for the managed-copy user-data preservation boundary."""

    def test_stash_and_restore_preserves_user_files_not_placeholders(self):
        with tempfile.TemporaryDirectory(prefix="scm-workbench-repos-") as temp:
            root = Path(temp)
            old = root / "old"
            stage = root / "stash"
            new = root / "new"
            for rel in repo_sync.USER_DATA_PATHS:
                (old / rel).mkdir(parents=True, exist_ok=True)
            user_files = {
                "data/offset_data.json": b'{"x_offset": 2}',
                "game/front/card.png": PNG,
                "game/back/back.jpg": JPEG,
                "game/double_sided/foil.png": PNG,
                "game/decklist/deck.txt": b"card list\n",
                "game/output/game.pdf": b"pdf bytes",
            }
            for rel, content in user_files.items():
                (old / rel).write_bytes(content)
            (old / "game/front/README.md").write_text("upstream placeholder", encoding="utf-8")
            (old / "game/decklist/EMPTY.md").write_text("upstream placeholder", encoding="utf-8")

            saved = repo_sync.stash_user_data(old, stage)
            self.assertEqual({rel for rel, _ in saved}, set(user_files))
            self.assertFalse((stage / "game/front/README.md").exists())
            self.assertFalse((stage / "game/decklist/EMPTY.md").exists())

            # A replacement tree may supply fresh upstream placeholders and
            # tracked files; restoring staged user files must win on collisions.
            (new / "game/front").mkdir(parents=True)
            (new / "game/front/README.md").write_text("new upstream", encoding="utf-8")
            repo_sync.restore_user_data(saved, new)
            for rel, content in user_files.items():
                self.assertEqual((new / rel).read_bytes(), content)
            self.assertEqual((new / "game/front/README.md").read_text(encoding="utf-8"), "new upstream")


if __name__ == "__main__":
    unittest.main()
