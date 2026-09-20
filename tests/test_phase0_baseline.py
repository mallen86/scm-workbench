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
from unittest import mock

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
        """Write a no-op script with realistic, machine-probeable help text."""
        path.parent.mkdir(parents=True, exist_ok=True)
        common = {
            "create_pdf.py": [
                "--front_dir_path", "--back_dir_path", "--double_sided_dir_path",
                "--output_path", "--output_images", "--card_size", "--paper_size",
                "--registration", "--registration_orientation", "--specialty",
                "--only_fronts", "--fit", "--fit_backs", "--crop", "--crop_backs",
                "--extend_edges", "--extend_edges_backs", "--extend_corners",
                "--extend_corners_backs", "--extend_bleed", "--extend_bleed_backs",
                "--ppi", "--quality", "--skip", "--label", "--show_outline",
                "--borderless", "--load_offset",
            ],
            "offset_pdf.py": [
                "--pdf_path", "--output_pdf_path", "-x", "-y", "-a", "--ppi", "-s",
            ],
            "generate.py": ["--all"],
            "fetch.py": [
                "-i", "--prefer_older_sets", "--prefer_set", "--ignore_set",
                "--prefer_showcase", "--prefer_extra_art", "--prefer_lang",
                "--prefer_ub", "--ignore_ub", "--tokens",
            ],
        }
        dxf = {
            "single": [
                "--card_size", "--card_width", "--card_height", "--card_radius",
                "--card_name", "--paper_size", "--paper_width", "--paper_height",
                "--paper_name", "--variant", "--orientation", "--save",
            ],
            "batch": ["--all", "--optimize"],
            "list": [],
        }
        script = f'''# fixture marker; upstream owns the real implementation
import argparse
import sys

# Constructing an argparse parser makes --help probing explicitly safe while
# the fixture remains a no-op for ordinary job delegation tests.
_probe_parser = argparse.ArgumentParser(add_help=False)
if "--help" in sys.argv:
    command = sys.argv[1] if {path.name!r} == "generate_dxf.py" and len(sys.argv) > 1 else ""
    options = {dxf!r}.get(command, []) if {path.name!r} == "generate_dxf.py" else {common!r}.get({path.name!r}, [])
    suffix = f" {{command}}" if command else ""
    print(f"Usage: {path.name}{{suffix}} [OPTIONS]")
    print("Options:")
    metavars = {{"--extend_corners": "TEXT"}}
    for option in options:
        print(f"  {{option}} {{metavars.get(option, 'VALUE')}}")
    print("  --help")
'''
        path.write_text(script, encoding="utf-8")

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
                "layouts": {"letter": {"standard": {
                    "default": {"num_rows": 2, "num_cols": 4},
                    "borderless": {"num_rows": 3, "num_cols": 3},
                }}},
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
        server._IPC_MODE = False
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
        server._IPC_MODE = False
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
        self.assertEqual(result["settings"]["defaults"]["ppi"], 600)

        persisted = json.loads(server.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(
            {"scm_dir", "extras_dir", "python", "port", "theme", "ui_mode",
             "update_channel", "auto_open_browser", "onboarded", "defaults", "repos"},
            set(persisted),
        )
        self.assertEqual(persisted["update_channel"], "stable")
        status, loaded = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(loaded, result["settings"])

        status, manifest = self.request("GET", "/api/manifest")
        self.assertEqual(status, 200)
        defaults = {option["key"]: option.get("default")
                    for group in manifest["create_pdf"]["groups"]
                    for option in group["options"]}
        self.assertEqual(defaults["ppi"], 600)
        self.assertIn("--ppi 600", server.build_preview("create_pdf", {})["cmd"])

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
            "jp2": b"\x00\x00\x00\x0cjP  \r\n\x87\n\x00\x00\x00\x14ftypjp2 ",
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
        self.assertEqual(manifest["fetch:mtg"]["script"]["probe"], "help")
        self.assertTrue(all(
            manifest[f"fetch:{slug}"]["script"]["probe"] == "parser"
            for slug in server.PLUGINS if slug != "mtg"
        ))

    def test_info_and_manifest_share_one_repository_snapshot(self):
        server.invalidate_manifest_cache()
        original = server.get_info
        with mock.patch.object(server, "get_info", wraps=original) as scan:
            info_status, _info = self.request("GET", "/api/info")
            manifest_status, _manifest = self.request("GET", "/api/manifest")
            self.assertEqual(scan.call_count, 1)
            second_info_status, _second_info = self.request("GET", "/api/info")
        self.assertEqual((info_status, manifest_status, second_info_status), (200, 200, 200))
        self.assertEqual(scan.call_count, 2)

    def test_simple_pdf_presets_expand_to_fixed_cli_values_in_titled_sections(self):
        status, manifest = self.request("GET", "/api/manifest")
        self.assertEqual(status, 200)
        create = manifest["create_pdf"]
        self.assertEqual(create["simple_rows"], [["card_size", "paper_size"]])
        self.assertEqual(create["simple_sections"], [
            {"title": "Print setup", "rows": [["borderless", "load_offset", "only_fronts", "skip_bottom_left"]]},
            {"title": "Image finishing", "rows": [["mpcfill_crop"]]},
        ])
        options = {option["key"]: option for group in create["groups"] for option in group["options"]}
        self.assertTrue(options["extend_corners_simple"]["simple_only"])
        self.assertTrue(options["extend_corners_simple"]["available"])
        self.assertTrue(options["extend_corners_simple"]["default"])
        self.assertEqual(options["extend_corners"]["default"], "3.5mm")
        self.assertIn("switch to Advanced mode", options["mpcfill_crop"]["help"])

        settings = server.load_settings()
        settings["ui_mode"] = "simple"
        server.save_settings(settings)
        server.invalidate_manifest_cache()
        args = {
            "card_size": "standard", "paper_size": "letter", "mpcfill_crop": True,
        }
        preview = server.build_preview("create_pdf", args)
        self.assertFalse(preview["errors"])
        self.assertIn("--crop 3mm", preview["cmd"])
        self.assertIn("--extend_corners 3.5mm", preview["cmd"])

        retained_advanced_value = server.build_preview(
            "create_pdf", {**args, "extend_corners": "4mm", "extend_corners_simple": False})
        self.assertFalse(retained_advanced_value["errors"])
        self.assertIn("--extend_corners 3.5mm", retained_advanced_value["cmd"])
        self.assertNotIn("--extend_corners 4mm", retained_advanced_value["cmd"])

        settings["ui_mode"] = "advanced"
        server.save_settings(settings)
        server.invalidate_manifest_cache()
        advanced_default = server.build_preview("create_pdf", {**args, "extend_corners": "3.5mm"})
        self.assertFalse(advanced_default["errors"])
        self.assertNotIn("--crop 3mm", advanced_default["cmd"])
        self.assertIn("--extend_corners 3.5mm", advanced_default["cmd"])
        advanced_custom = server.build_preview("create_pdf", {**args, "extend_corners": "4mm"})
        self.assertFalse(advanced_custom["errors"])
        self.assertIn("--extend_corners 4mm", advanced_custom["cmd"])

    def test_advanced_pdf_directories_opt_into_browse_and_reset_controls(self):
        create = server.get_manifest()["create_pdf"]
        options = {option["key"]: option for group in create["groups"] for option in group["options"]}
        browsable = {key for key, option in options.items() if option.get("browse_directory")}
        self.assertEqual(browsable, {"front_dir", "double_sided_dir", "output_path"})
        self.assertEqual(options["front_dir"]["default"], "game/front")
        self.assertEqual(options["double_sided_dir"]["default"], "game/double_sided")
        self.assertEqual(options["output_path"]["default"], "game/output/game.pdf")
        self.assertEqual(options["output_path"]["browse_filename"], "game.pdf")

    def test_skip_indexes_are_a_plain_comma_separated_input(self):
        create = server.get_manifest()["create_pdf"]
        options = {option["key"]: option for group in create["groups"] for option in group["options"]}
        skip = options["skip"]
        self.assertEqual(skip["type"], "text")
        self.assertTrue(skip["int_list"])
        self.assertEqual(skip["placeholder"], "ex: 0, 4")
        self.assertIn("Comma-separated", skip["help"])

        args, errors, _warnings = server.normalize_args(create, {"skip": "0, 4  6"})
        self.assertFalse(errors)
        self.assertEqual(args["skip"], [0, 4, 6])
        _args, errors, _warnings = server.normalize_args(create, {"skip": "0, nope"})
        self.assertTrue(any("not a valid index" in error for error in errors))

        preview = server.build_preview("create_pdf", {
            "card_size": "standard", "paper_size": "letter", "skip": "0, 4 6",
        })
        self.assertFalse(preview["errors"])
        self.assertIn("--skip 0 --skip 4 --skip 6", preview["cmd"])

    def test_skip_bottom_left_uses_the_paper_and_borderless_layout_index(self):
        expected = {
            ("letter", False): 4, ("letter", True): 6,
            ("a4", False): 4, ("a4", True): 6,
            ("a3", False): 12, ("a3", True): 12,
            ("tabloid", False): 12, ("tabloid", True): 12,
            ("arch_b", False): 12, ("arch_b", True): 14,
            ("legal", False): 5, ("legal", True): 5,
        }
        for (paper, borderless), index in expected.items():
            with self.subTest(paper=paper, borderless=borderless):
                self.assertEqual(server._bottom_left_skip_index(paper, borderless), index)
        self.assertEqual(server._bottom_left_skip_index("ARCH-B", True), 14)
        self.assertIsNone(server._bottom_left_skip_index("custom", False))

        create = server.get_manifest()["create_pdf"]
        options = {option["key"]: option for group in create["groups"] for option in group["options"]}
        self.assertTrue(options["skip_bottom_left"]["simple"])
        self.assertEqual(options["skip_bottom_left"]["requires_flags"], ["--skip"])

        ordinary = server.build_preview("create_pdf", {
            "card_size": "standard", "paper_size": "letter", "skip_bottom_left": True,
        })
        self.assertFalse(ordinary["errors"])
        self.assertIn("--skip 4", ordinary["cmd"])
        borderless = server.build_preview("create_pdf", {
            "card_size": "standard", "paper_size": "letter", "borderless": True,
            "skip": "6", "skip_bottom_left": True,
        })
        self.assertFalse(borderless["errors"])
        self.assertEqual(borderless["cmd"].count("--skip 6"), 1)

    def test_custom_paper_label_names_the_dxf_and_saved_size(self):
        preview = server.build_preview("dxf_single", {
            "card_mode": "named", "card_size": "standard",
            "paper_mode": "custom", "paper_width": "8.5in",
            "paper_height": "14in", "paper_name": "legal", "save": True,
        })
        self.assertFalse(preview["errors"])
        self.assertIn("--paper_name legal", preview["cmd"])
        self.assertTrue(preview["cmd"].endswith(
            "cutting_templates/dxf/legal-standard-v1.dxf --save"))

        unsafe = server.build_preview("dxf_single", {
            "card_mode": "named", "card_size": "standard",
            "paper_mode": "custom", "paper_width": "8.5in",
            "paper_height": "14in", "paper_name": "../legal", "save": True,
        })
        self.assertTrue(any("safe filename label" in error for error in unsafe["errors"]))

    def test_a_second_template_for_the_same_size_keeps_the_first(self):
        """Generating the same size twice must not replace the first template.

        The name used to be fixed at '-v1', so the second run overwrote the
        first. A taken name now takes the next version, and the preview and the
        run resolve it identically because they share one command builder.
        """
        # The fixture repo is shared with the other contract tests, so anything
        # this test writes is removed again on the way out.
        written_dirs = [self.fixture.scm / "cutting_templates" / "dxf",
                        self.fixture.scm / "cutting_templates" / "borderless" / "dxf"]
        for directory in written_dirs:
            self.addCleanup(
                lambda d=directory: d.rmdir() if d.is_dir() and not any(d.iterdir()) else None)
        self.addCleanup(lambda: [f.unlink() for d in written_dirs if d.is_dir()
                                 for f in d.iterdir() if f.suffix == ".dxf"])

        args = {"card_mode": "named", "card_size": "standard",
                "paper_mode": "custom", "paper_width": "8.5in",
                "paper_height": "14in", "paper_name": "legal", "save": True}
        first = server.build_preview("dxf_single", args)
        self.assertFalse(first["errors"])
        self.assertIn("cutting_templates/dxf/legal-standard-v1.dxf", first["cmd"])

        # What the first run would have written.
        written = self.fixture.scm / "cutting_templates" / "dxf" / "legal-standard-v1.dxf"
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(b"the first template")

        second = server.build_preview("dxf_single", args)
        self.assertFalse(second["errors"])
        self.assertIn("cutting_templates/dxf/legal-standard-v2.dxf", second["cmd"])
        self.assertNotIn("legal-standard-v1.dxf", second["cmd"])
        self.assertEqual(written.read_bytes(), b"the first template")

        # The run path is the same builder, so it names the same new file.
        normalized, _errors, _warnings = server.normalize_args(
            server.get_manifest()["dxf_single"], args)
        argv, _cwd, _env, _title, _warnings, errs = server.build_command(
            "dxf_single", normalized, server.load_settings(), server.get_info_cached())
        self.assertFalse(errs)
        self.assertEqual(argv[-2], "cutting_templates/dxf/legal-standard-v2.dxf")
        self.assertTrue(second["cmd"].endswith(
            "cutting_templates/dxf/legal-standard-v2.dxf --save"))

        # And a borderless template versions in its own folder.
        borderless = dict(args, variant="borderless")
        b_first = server.build_preview("dxf_single", borderless)
        self.assertIn("cutting_templates/borderless/dxf/legal-standard-borderless-v1.dxf", b_first["cmd"])
        (self.fixture.scm / "cutting_templates" / "borderless" / "dxf").mkdir(parents=True, exist_ok=True)
        (self.fixture.scm / "cutting_templates" / "borderless" / "dxf"
         / "legal-standard-borderless-v1.dxf").write_bytes(b"borderless one")
        b_second = server.build_preview("dxf_single", borderless)
        self.assertIn("cutting_templates/borderless/dxf/legal-standard-borderless-v2.dxf", b_second["cmd"])
        # The default folder's counter is its own.
        self.assertIn("cutting_templates/dxf/legal-standard-v2.dxf",
                      server.build_preview("dxf_single", args)["cmd"])

    def test_fetch_manifest_keeps_picker_below_source_and_preferences_collapsed(self):
        status, manifest = self.request("GET", "/api/manifest")
        self.assertEqual(status, 200)
        groups = manifest["fetch:mtg"]["groups"]
        self.assertEqual(groups[0]["simple_rows"], [
            ["deck_source"],
            ["deck_file", "deck_name", "deck_text", "deck_url"],
        ])
        self.assertTrue(groups[2]["collapsible"])
        toggles = [o for o in groups[2]["options"] if o["type"] == "toggle"]
        self.assertEqual(len(toggles), 7)
        self.assertTrue(all(o["width"] == "quarter" for o in toggles))

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
            "kind": "create_pdf",
            "args": json.dumps({"card_size": "extra", "paper_size": "letter"}),
        }))
        self.assertEqual(status, 200)
        self.assertFalse(preview["errors"])
        self.assertNotIn("SCM_EXTRA_LAYOUTS is set automatically", "\n".join(preview["warnings"]))

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
