"""Upstream script capability discovery and compatibility gating."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import server

try:
    from test_phase0_baseline import Phase0Fixture
except ModuleNotFoundError:  # package-style unittest invocation
    from tests.test_phase0_baseline import Phase0Fixture


class ScriptCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-capabilities-")
        self.root = Path(self.temp.name)
        with server._SCRIPT_CAPABILITY_CACHE_LOCK:
            server._SCRIPT_CAPABILITY_CACHE.clear()

    def tearDown(self):
        with server._SCRIPT_CAPABILITY_CACHE_LOCK:
            server._SCRIPT_CAPABILITY_CACHE.clear()
        self.temp.cleanup()

    @staticmethod
    def _write(path: Path, source: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def _probe(self, source: str, *, config=None):
        repo = self.root / "repo"
        script = self._write(repo / "tool.py", source)
        target = {"repo": "scm", "path": script.name, "help_args": ["--help"]}
        if config:
            target.update(config)
        result = server._probe_script_capability(
            target, {"scm": repo, "extras": None}, {"python": sys.executable},
        )
        return result, repo

    def test_help_probe_enumerates_long_and_short_flags(self):
        result, _repo = self._probe(
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--borderless', action='store_true')\n"
            "parser.add_argument('-x', '--x_offset')\n"
            "parser.parse_args()\n"
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue({"--borderless", "-x", "--x_offset", "--help"}.issubset(result["flags"]))
        self.assertTrue(result["usage"].startswith("usage:"))

    def test_script_change_during_probe_is_rejected(self):
        repo = self.root / "race"
        script = self._write(
            repo / "tool.py",
            "import argparse\nparser = argparse.ArgumentParser()\nparser.parse_args()\n",
        )
        config = {"repo": "scm", "path": "tool.py", "help_args": ["--help"]}

        def mutate(*_args, **_kwargs):
            script.write_text(script.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
            return {"status": "ok", "flags": ["--help"], "usage": "usage: tool.py"}

        with mock.patch.object(server, "_run_script_help", side_effect=mutate):
            result = server._probe_script_capability(
                config, {"scm": repo, "extras": None}, {"python": sys.executable},
            )
        self.assertEqual(result["status"], "unsafe")

    def test_probe_cache_is_reused_and_changes_with_script_fingerprint(self):
        source = (
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--first')\n"
            "parser.parse_args()\n"
        )
        first, repo = self._probe(source)
        self.assertIn("--first", first["flags"])
        config = {"repo": "scm", "path": "tool.py", "help_args": ["--help"]}
        with mock.patch.object(server, "_run_script_help", side_effect=AssertionError("cache miss")):
            cached = server._probe_script_capability(
                config, {"scm": repo, "extras": None}, {"python": sys.executable},
            )
        self.assertEqual(cached["flags"], first["flags"])

        self._write(
            repo / "tool.py",
            source.replace("parser.parse_args()", "parser.add_argument('--second')\nparser.parse_args()"),
        )
        changed = server._probe_script_capability(
            config, {"scm": repo, "extras": None}, {"python": sys.executable},
        )
        self.assertIn("--second", changed["flags"])

    def test_expected_subcommand_must_appear_in_usage(self):
        result, _repo = self._probe(
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--card_size')\n"
            "parser.print_help()\n",
            config={"help_args": ["single", "--help"], "usage_command": "single"},
        )
        self.assertEqual(result["status"], "wrong_command_shape")

    def test_source_without_parser_is_not_executed_for_help(self):
        sentinel = self.root / "executed"
        result, _repo = self._probe(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('ran')\n"
        )
        self.assertEqual(result["status"], "no_safe_help")
        self.assertFalse(sentinel.exists())

    def test_links_and_oversized_sources_fail_closed(self):
        repo = self.root / "unsafe"
        target = self._write(
            self.root / "outside.py",
            "import argparse\nargparse.ArgumentParser().parse_args()\n",
        )
        script = repo / "tool.py"
        script.parent.mkdir(parents=True)
        try:
            script.symlink_to(target)
        except OSError:
            pass
        config = {"repo": "scm", "path": "tool.py", "help_args": ["--help"]}
        if script.is_symlink():
            linked = server._probe_script_capability(
                config, {"scm": repo, "extras": None}, {"python": sys.executable},
            )
            self.assertEqual(linked["status"], "unsafe")
            script.unlink()

        script.write_text("import argparse\n" + ("# padding\n" * 20), encoding="utf-8")
        with mock.patch.object(server, "SCRIPT_CAPABILITY_SOURCE_MAX_BYTES", 32):
            oversized = server._probe_script_capability(
                config, {"scm": repo, "extras": None}, {"python": sys.executable},
            )
        self.assertEqual(oversized["status"], "source_too_large")

    def test_exists_only_probe_never_executes_top_level_script(self):
        sentinel = self.root / "executed"
        result, _repo = self._probe(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('ran')\n",
            config={"probe": "exists"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["enumerated"])
        self.assertFalse(sentinel.exists())

    def test_help_probe_bounds_runtime_and_output(self):
        with mock.patch.object(server, "SCRIPT_CAPABILITY_TIMEOUT", 0.05):
            timeout, _repo = self._probe(
                "import argparse, time\n"
                "parser = argparse.ArgumentParser()\n"
                "time.sleep(2)\n"
                "parser.parse_args()\n"
            )
        self.assertEqual(timeout["status"], "timeout")

        with mock.patch.object(server, "SCRIPT_CAPABILITY_OUTPUT_MAX_BYTES", 128):
            oversized, _repo = self._probe(
                "import argparse, sys\n"
                "parser = argparse.ArgumentParser()\n"
                "if '--help' in sys.argv: print('x' * 4096)\n"
                "parser.parse_args()\n"
            )
        self.assertEqual(oversized["status"], "output_too_large")

        options = "".join(
            f"parser.add_argument('--flag_{index}')\n"
            for index in range(server.SCRIPT_CAPABILITY_FLAG_MAX + 1)
        )
        too_many, _repo = self._probe(
            "import argparse\nparser = argparse.ArgumentParser()\n" + options + "parser.parse_args()\n"
        )
        self.assertEqual(too_many["status"], "too_many_options")

    def test_legacy_scm_disables_borderless_and_normalizes_default_layout(self):
        fixture_root = self.root / "fixture"
        fixture_root.mkdir()
        fixture = Phase0Fixture(fixture_root)
        layouts_path = fixture.scm / "assets" / "layouts.json"
        layouts = json.loads(layouts_path.read_text(encoding="utf-8"))
        layouts["layouts"] = {
            "letter": {"standard": {"num_rows": 2, "num_cols": 4}}
        }
        layouts_path.write_text(json.dumps(layouts), encoding="utf-8")

        # This is the pre-v3 Create PDF surface: required baseline flags are
        # present, while --borderless and the newer finishing switches are not.
        fixture._marker(fixture.scm / "create_pdf.py")
        source = (fixture.scm / "create_pdf.py").read_text(encoding="utf-8")
        source = source.replace("'--borderless', ", "").replace("'--borderless',", "")
        source = source.replace("'--quality', ", "").replace("'--quality',", "")
        (fixture.scm / "create_pdf.py").write_text(source, encoding="utf-8")

        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({
            "scm_dir": str(fixture.scm),
            "extras_dir": str(fixture.extras),
            "python": sys.executable,
        })
        info = {
            "scm": server.read_scm_info(fixture.scm, fixture.extras),
            "extras": server.read_extras_info(fixture.extras),
        }
        manifest = server.build_manifest(info)
        server._apply_script_capabilities(manifest, settings, info)

        create = manifest["create_pdf"]
        options = {option["key"]: option for group in create["groups"] for option in group["options"]}
        self.assertTrue(create["available"])
        self.assertFalse(options["borderless"]["available"])
        self.assertIn("borderless", options["borderless"]["unavailable_reason"].lower())
        self.assertFalse(options["quality"]["available"])
        self.assertTrue(options["card_size"]["available"])
        self.assertIn("default", info["scm"]["layouts"]["letter"]["standard"])
        self.assertEqual(
            server._pdf_preview_page_slots(
                info, {"paper_size": "letter", "card_size": "standard"}, settings,
            ),
            8,
        )
        safe, safe_errors, _warnings = server.normalize_args(
            create, {"card_size": "standard", "paper_size": "letter"},
        )
        self.assertFalse(safe_errors)
        with mock.patch.object(server, "get_manifest", return_value=manifest):
            argv, _cwd, _env, _title, _warnings, command_errors = server.build_command(
                "create_pdf", safe, settings, info, write_deck=False,
            )
        self.assertFalse(command_errors)
        self.assertNotIn("--quality", argv)
        self.assertNotIn("--borderless", argv)

        raw = {"card_size": "standard", "paper_size": "letter", "borderless": True}
        normalized, errors, _warnings = server.normalize_args(create, raw)
        self.assertNotIn("borderless", normalized)
        self.assertTrue(any("borderless" in error.lower() for error in errors))
        with mock.patch.object(server, "get_manifest", return_value=manifest), \
                mock.patch.object(server.subprocess, "Popen") as popen:
            job, start_errors = server.start_job("create_pdf", raw)
        self.assertIsNone(job)
        self.assertTrue(any("borderless" in error.lower() for error in start_errors))
        popen.assert_not_called()

    def test_unavailable_scalar_and_multi_choices_are_rejected(self):
        spec = {"groups": [{"options": [
            {
                "key": "variant", "label": "Variant", "type": "segment", "default": "default",
                "choices": [["default", "Default"], ["borderless", "Borderless"]],
                "unavailable_choices": {"borderless": "borderless is unsupported"},
            },
            {
                "key": "languages", "label": "Languages", "type": "choice_chips", "default": [],
                "choices": [["en", "EN"], ["ja", "JA"]],
                "unavailable_choices": {"ja": "Japanese is unsupported"},
            },
        ]}]}
        normalized, errors, _warnings = server.normalize_args(
            spec, {"variant": "borderless", "languages": ["en", "ja"]},
        )
        self.assertEqual(normalized["variant"], "default")
        self.assertEqual(normalized["languages"], ["en"])
        self.assertEqual(len(errors), 2)

    def test_missing_required_flags_disables_entire_workflow(self):
        fixture_root = self.root / "required"
        fixture_root.mkdir()
        fixture = Phase0Fixture(fixture_root)
        script = fixture.scm / "create_pdf.py"
        script.write_text(
            "import argparse\nparser = argparse.ArgumentParser()\n"
            "parser.add_argument('--borderless', action='store_true')\nparser.parse_args()\n",
            encoding="utf-8",
        )
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({"scm_dir": str(fixture.scm), "extras_dir": str(fixture.extras), "python": sys.executable})
        info = {"scm": server.read_scm_info(fixture.scm, fixture.extras),
                "extras": server.read_extras_info(fixture.extras)}
        manifest = server.build_manifest(info)
        server._apply_script_capabilities(manifest, settings, info)
        create = manifest["create_pdf"]
        self.assertFalse(create["available"])
        self.assertIn("required", create["unavailable_reason"].lower())
        self.assertTrue(all(option["available"] is False
                            for group in create["groups"] for option in group["options"]))


if __name__ == "__main__":
    unittest.main()
