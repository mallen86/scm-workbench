"""App-owned TLS dependency is installed only into Windows release runtimes."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bake_runtime.py"
SPEC = importlib.util.spec_from_file_location("bake_runtime", SCRIPT)
bake_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bake_runtime)


class BakeWindowsTLSTests(unittest.TestCase):
    def test_installs_pinned_wheel_on_all_windows_builders(self):
        python = Path("runtime/python.exe")
        with patch.object(bake_runtime.sys, "platform", "win32"), \
             patch.object(bake_runtime.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0)) as run:
            bake_runtime.bake_workbench_deps(python)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [str(python), "-m", "pip", "install"])
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--require-hashes", command)
        self.assertEqual(command[-2:],
                         ["-r", str(SCRIPT.parents[1] / "packaging" / "windows-runtime-requirements.txt")])
        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_legacy_packaging_metadata_keeps_the_dependency_windows_only(self):
        import tomllib
        metadata = tomllib.loads((SCRIPT.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        app = metadata["tool"]["briefcase"]["app"]["scm-workbench"]
        self.assertIn("truststore==0.10.5", app["windows"]["requires"])
        locked = (SCRIPT.parents[1] / "packaging" / "windows-runtime-requirements.txt").read_text()
        self.assertIn("truststore==0.10.5", locked)
        self.assertIn("--hash=sha256:9aaaedaefaf06d8b206278cf8b5012bc897f485a874503501e12d776df78951c", locked)
        for requirements in (app["requires"], app["macOS"]["requires"],
                             metadata["project"].get("dependencies", [])):
            self.assertFalse(any("truststore" in item.lower() for item in requirements))

    def test_no_dependency_install_for_macos_or_linux(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), \
                 patch.object(bake_runtime.sys, "platform", platform), \
                 patch.object(bake_runtime.subprocess, "run") as run:
                bake_runtime.bake_workbench_deps(Path("runtime/python"))
                run.assert_not_called()

    def test_install_failure_stops_packaging(self):
        with patch.object(bake_runtime.sys, "platform", "win32"), \
             patch.object(bake_runtime.subprocess, "run", return_value=SimpleNamespace(
                 returncode=1, stderr="wheel unavailable", stdout="")):
            with self.assertRaisesRegex(SystemExit, "Windows TLS dependency install failed"):
                bake_runtime.bake_workbench_deps(Path("runtime/python.exe"))

    def test_main_bakes_app_dependency_after_upstream_requirements(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            order = []
            with patch.object(bake_runtime.sys, "argv", ["bake_runtime", "--bundle", directory]), \
                 patch.object(bake_runtime, "fetch_runtime", return_value=python), \
                 patch.object(bake_runtime, "bake_deps",
                              side_effect=lambda *args: order.append("upstream")) as upstream, \
                 patch.object(bake_runtime, "bake_workbench_deps",
                              side_effect=lambda *args: order.append("app")) as app:
                bake_runtime.main()
            upstream.assert_called_once_with(python, Path(directory).resolve())
            app.assert_called_once_with(python)
            self.assertEqual(order, ["upstream", "app"])


if __name__ == "__main__":
    unittest.main()
