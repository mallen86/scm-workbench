import base64
import json
import subprocess
import sys
import os
from unittest import mock
import tempfile
import unittest
from pathlib import Path

from scm_workbench.postprocessing import CONTRACT_VERSION, revision_digest, stage_images, discover_images
from scm_workbench import postprocess_runner


LIMITS = {"cpu_seconds": 60, "address_space": 1024 * 1024 * 1024,
          "file_size": 128 * 1024 * 1024, "open_files": 64, "processes": 8}


def runner_entries(entries):
    return [{key: entry[key] for key in ("role", "relative_path", "name", "staged", "index", "total")}
            for entry in entries]


def png():
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
    )


class RunnerTests(unittest.TestCase):
    def test_only_fixed_source_can_omit_cpu_limit_and_other_limits_stay_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            run = root / "run"
            run.mkdir()
            source = run / "processor.py"
            model = root / "environment/RealESRGAN_x4plus.onnx"
            model.parent.mkdir()
            model.write_bytes(b"test model")
            source.write_text("def process_image(image_path, context): pass\n", encoding="utf-8")
            payload = {"source_path": str(source), "run_root": str(run), "entries": [],
                       "environment": str(model.parent), "environment_root": str(root),
                       "revision": "a" * 64, "requirements": [], "contract": CONTRACT_VERSION,
                       "limits": {**LIMITS, "cpu_seconds": None}}
            manifest = run / "manifest.json"
            def load():
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                return postprocess_runner._load_manifest(manifest)
            with self.assertRaisesRegex(postprocess_runner.RunnerError, "manifest limits"):
                load()
            payload["model_path"] = str(model)
            with self.assertRaisesRegex(postprocess_runner.RunnerError, "fixed processor"):
                load()
            source.write_bytes((Path(postprocess_runner.__file__).parent /
                                "builtin_processors/advanced_upscaler.py").read_bytes())
            self.assertIsNone(load()["limits"]["cpu_seconds"])
            for name, value in (("cpu_seconds", 0), ("cpu_seconds", 3601),
                                ("address_space", None), ("file_size", -1),
                                ("open_files", 0), ("processes", 65)):
                with self.subTest(name=name, value=value):
                    payload["limits"] = {**LIMITS, "cpu_seconds": None, name: value}
                    with self.assertRaisesRegex(postprocess_runner.RunnerError, "manifest limits"):
                        load()
            payload.pop("model_path")
            payload["limits"] = LIMITS
            self.assertEqual(load()["limits"], LIMITS)

    @unittest.skipUnless(os.name == "posix", "POSIX resource limits")
    def test_cpu_limit_removed_only_for_fixed_runner_without_losing_other_limits(self):
        import resource
        limits = {**LIMITS, "cpu_seconds": None}
        applied = []
        def inherited(kind):
            return (30, resource.RLIM_INFINITY if kind == resource.RLIMIT_CPU else 4096)
        with (mock.patch.object(resource, "getrlimit", side_effect=inherited),
              mock.patch.object(resource, "setrlimit", side_effect=lambda *args: applied.append(args))):
            postprocess_runner._set_limits({"limits": limits})
        self.assertIn((resource.RLIMIT_CPU, (resource.RLIM_INFINITY, resource.RLIM_INFINITY)), applied)
        self.assertIn((resource.RLIMIT_FSIZE, (4096, 4096)), applied)
        self.assertIn((resource.RLIMIT_NOFILE, (64, 64)), applied)
        applied.clear()
        with (mock.patch.object(resource, "getrlimit", return_value=(30, 300)),
              mock.patch.object(resource, "setrlimit", side_effect=lambda *args: applied.append(args))):
            postprocess_runner._set_limits({"limits": limits})
        self.assertIn((resource.RLIMIT_CPU, (300, 300)), applied)
        applied.clear()
        with (mock.patch.object(resource, "getrlimit", return_value=(30, resource.RLIM_INFINITY)),
              mock.patch.object(resource, "setrlimit", side_effect=lambda *args: applied.append(args))):
            postprocess_runner._set_limits({"limits": LIMITS})
        self.assertIn((resource.RLIMIT_CPU, (60, 60)), applied)

    def test_windows_job_object_retains_memory_process_and_kill_limits(self):
        import ctypes
        flags = []
        kernel = mock.Mock()
        kernel.CreateJobObjectW.return_value = 123
        kernel.GetCurrentProcess.return_value = 456
        kernel.SetInformationJobObject.side_effect = lambda _handle, _class, pointer, _size: (
            flags.append((pointer._obj.basic.flags, pointer._obj.basic.process_time,
                          pointer._obj.basic.active, pointer._obj.job_memory)) or True)
        kernel.AssignProcessToJobObject.return_value = True
        with mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel):
            for cpu in (None, 60):
                with mock.patch.object(postprocess_runner, "_WINDOWS_JOB_HANDLE", None):
                    self.assertTrue(postprocess_runner._set_windows_limits({**LIMITS, "cpu_seconds": cpu}))
        self.assertEqual(flags[0], (0x00000008 | 0x00000200 | 0x00002000, 0, 8,
                                    LIMITS["address_space"]))
        self.assertEqual(flags[1], (flags[0][0] | 0x00000002, 60 * 10_000_000, 8,
                                    LIMITS["address_space"]))

    def test_one_process_callback_order_and_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "game/front").mkdir(parents=True)
            (root / "game/double_sided").mkdir(parents=True)
            (root / "game/front/b.png").write_bytes(png())
            (root / "game/front/a.png").write_bytes(png())
            records = discover_images(root)
            entries = stage_images(records, root / "run")
            source = root / "run" / "processor.py"
            source_text = """
from pathlib import Path
STATE = []
def process_image(image_path, context):
    STATE.append(context['name'])
    Path(image_path).write_bytes(Path(image_path).read_bytes())
"""
            source.write_bytes(source_text.encode("utf-8"))
            manifest = root / "run" / "manifest.json"
            manifest.write_text(json.dumps({"source_path": str(source), "run_root": str(root / "run"),
                "entries": runner_entries(entries), "revision": revision_digest(source_text, []),
                "requirements": [], "contract": CONTRACT_VERSION, "environment": "",
                "environment_root": str(root / "environments"), "limits": LIMITS}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-u", "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line for line in result.stdout.splitlines() if line.startswith("WB_POSTPROCESS_PROGRESS ")]
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line.split(" ", 1)[1])["index"] for line in lines], [1, 2])

    def test_back_only_role_is_bounded_to_the_private_back_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            back = root / "game/back"; back.mkdir(parents=True)
            (back / "card.png").write_bytes(png())
            entries = stage_images(discover_images(root, "back"), root / "run")
            source_text = (
                "def process_image(image_path, context):\n"
                "    assert context['role'] == 'back'\n"
                "    assert context['relative_path'] == 'game/back/card.png'\n"
            )
            source = root / "run/processor.py"; source.write_bytes(source_text.encode("utf-8"))
            self.assertEqual(source.read_bytes(), source_text.encode("utf-8"))
            manifest = root / "run/manifest.json"
            payload = {"source_path": str(source), "run_root": str(root / "run"),
                       "entries": runner_entries(entries), "revision": revision_digest(source_text, []),
                       "requirements": [], "contract": CONTRACT_VERSION, "environment": "",
                       "environment_root": str(root / "environments"), "limits": LIMITS}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            command = [sys.executable, "-I", "-B", "scm_workbench/postprocess_runner.py",
                       "--manifest", str(manifest)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            progress = [json.loads(line.split(" ", 1)[1]) for line in result.stdout.splitlines()
                        if line.startswith("WB_POSTPROCESS_PROGRESS ")]
            self.assertEqual(progress, [{"index": 1, "total": 1, "name": "card.png", "role": "back"}])
            payload["entries"][0]["relative_path"] = "game/front/card.png"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("manifest entry is invalid", result.stdout)

    def test_private_environment_is_inserted_without_processing_pth_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); (root / "run/work/front").mkdir(parents=True)
            image = root / "run/work/front/a.png"; image.write_bytes(png())
            environment_root = root / "environments"
            site_packages = environment_root / ("a" * 64) / "site-packages"
            site_packages.mkdir(parents=True)
            (site_packages / "demo_dependency.py").write_text("VALUE = b'\\0'\n", encoding="utf-8")
            # Direct sys.path insertion must not execute .pth contents.
            (site_packages / "hostile.pth").write_text("raise RuntimeError('pth executed')\n", encoding="utf-8")
            source_text = (
                "import demo_dependency\n"
                "def process_image(image_path, context):\n"
                "    with open(image_path, 'ab') as stream:\n"
                "        stream.write(demo_dependency.VALUE)\n"
            )
            source = root / "run" / "processor.py"; source.write_bytes(source_text.encode("utf-8"))
            requirements = ["demo==1.0"]
            manifest = root / "run" / "manifest.json"
            manifest.write_text(json.dumps({
                "source_path": str(source), "run_root": str(root / "run"),
                "entries": [{"staged": str(image), "name": "a.png", "role": "front",
                             "relative_path": "game/front/a.png", "index": 1, "total": 1}],
                "revision": revision_digest(source_text, requirements),
                "requirements": requirements, "contract": CONTRACT_VERSION,
                "environment": str(site_packages), "environment_root": str(environment_root),
                "limits": LIMITS,
            }), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I", "-B", "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(image.read_bytes().endswith(b"\0"))
            self.assertEqual(list(site_packages.rglob("*.pyc")), [])

    def test_non_none_result_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); (root / "run/work/front").mkdir(parents=True)
            image = root / "run/work/front/a.png"; image.write_bytes(png())
            source_text = "def process_image(image_path, context):\n    return 1\n"
            source = root / "run" / "processor.py"; source.write_bytes(source_text.encode("utf-8"))
            manifest = root / "run" / "manifest.json"; manifest.write_text(json.dumps({
                "source_path": str(source), "run_root": str(root / "run"),
                "entries": [{"staged": str(image), "name": "a.png", "role": "front",
                             "relative_path": "game/front/a.png", "index": 1, "total": 1}],
                "revision": revision_digest(source_text, []), "requirements": [],
                "contract": CONTRACT_VERSION, "environment": "",
                "environment_root": str(root / "environments"), "limits": LIMITS}))
            result = subprocess.run([sys.executable, "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must return None", result.stdout)


if __name__ == "__main__":
    unittest.main()
