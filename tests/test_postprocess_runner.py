import base64
from contextlib import redirect_stdout
import io
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
    def test_all_processors_can_omit_cpu_limit_but_memory_and_other_limits_stay_bounded(self):
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
            self.assertIsNone(load()["limits"]["cpu_seconds"])
            payload["model_path"] = str(model)
            self.assertIsNone(load()["limits"]["cpu_seconds"])
            payload["limits"]["address_space"] = 16 * 1024 ** 3
            with self.assertRaisesRegex(postprocess_runner.RunnerError, "fixed bundled upscaler"):
                load()
            source.write_bytes((Path(postprocess_runner.__file__).parent /
                                "builtin_processors/advanced_upscaler.py").read_bytes())
            self.assertEqual(load()["limits"]["address_space"], 16 * 1024 ** 3)
            self.assertIsNone(load()["limits"]["cpu_seconds"])
            payload["limits"] = {**LIMITS, "cpu_seconds": None}
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
    def test_omitted_cpu_limit_does_not_remove_other_limits_or_raise_inherited_hard_caps(self):
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

    def test_fixed_source_matching_normalizes_only_bundled_windows_newlines(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "processor.py"
            bundled = Path(postprocess_runner.__file__).parent / "builtin_processors/simple_upscaler.py"
            source.write_bytes(bundled.read_bytes().replace(b"\r\n", b"\n"))
            real_read = postprocess_runner._read_regular
            def windows_bundle(path, label, limit):
                raw, identity = real_read(path, label, limit)
                if label == "bundled processor":
                    raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                return raw, identity
            manifest = {"source_path": str(source), "run_root": str(root)}
            with mock.patch.object(postprocess_runner, "_read_regular", side_effect=windows_bundle):
                self.assertTrue(postprocess_runner._fixed_upscaler_source(manifest))
                source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
                self.assertTrue(postprocess_runner._fixed_upscaler_source(manifest))
                source.write_bytes(source.read_bytes() + b"\n# custom edit\n")
                self.assertFalse(postprocess_runner._fixed_upscaler_source(manifest))

    @unittest.skipUnless(os.name == "nt" and sys.maxsize > 2**32,
                         "requires real Windows x64 Job Objects")
    def test_real_windows_job_object_retains_memory_cap_without_cpu_quota(self):
        # Apply limits in a disposable child, not to the unittest process.
        # Query the real x64 ABI without allocating anything near the ceiling.
        script = r'''
import ctypes, importlib.util, json, struct, sys
from ctypes import wintypes
spec = importlib.util.spec_from_file_location("runner_check", sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
assert runner._set_windows_limits({"address_space": 16 * 1024**3,
                                  "cpu_seconds": None, "processes": 8})
kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x00000800)
query = kernel.QueryInformationJobObject
query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                  ctypes.POINTER(wintypes.DWORD))
query.restype = wintypes.BOOL
info = ctypes.create_string_buffer(144)
assert query(runner._WINDOWS_JOB_HANDLE, 9, ctypes.byref(info), ctypes.sizeof(info), None)
print(json.dumps({"flags": struct.unpack_from("I", info.raw, 16)[0],
                  "memory": struct.unpack_from("Q", info.raw, 120)[0]}))
'''
        result = subprocess.run([sys.executable, "-I", "-c", script, str(Path(postprocess_runner.__file__).resolve())],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual["memory"], 16 * 1024**3)
        self.assertEqual(actual["flags"] & (0x2 | 0x4), 0)  # No CPU quota.
        self.assertEqual(actual["flags"] & (0x8 | 0x200 | 0x2000), 0x8 | 0x200 | 0x2000)

    def test_windows_refuses_too_small_memory_budget_instead_of_rounding_up(self):
        import ctypes
        with (mock.patch.object(postprocess_runner, "_WINDOWS_JOB_HANDLE", None),
              mock.patch.object(ctypes, "WinDLL", create=True) as dll):
            self.assertFalse(postprocess_runner._set_windows_limits({**LIMITS, "address_space": 1}))
            self.assertFalse(postprocess_runner._set_windows_limits({**LIMITS, "address_space": True}))
            self.assertFalse(postprocess_runner._set_windows_limits({**LIMITS, "address_space": 17 * 1024 ** 3}))
            dll.assert_not_called()

    def test_windows_rechecks_headroom_before_limits_and_never_raises_parent_budget(self):
        from types import SimpleNamespace
        policy = SimpleNamespace(read_windows_memory=mock.Mock(return_value="memory-info"),
                                 upscaler_budget=mock.Mock(return_value=6 * 1024 ** 3))
        manifest = {"limits": {**LIMITS, "cpu_seconds": None, "address_space": 16 * 1024 ** 3}}
        with (mock.patch.object(postprocess_runner.os, "name", "nt"),
              mock.patch.object(postprocess_runner, "_fixed_upscaler_source", return_value=True),
              mock.patch.object(postprocess_runner, "_load_memory_policy", return_value=policy),
              mock.patch.object(postprocess_runner, "_set_windows_limits", return_value=True) as apply,
              mock.patch("builtins.print")):
            postprocess_runner._set_limits(manifest)
            self.assertEqual(apply.call_args.args[0]["address_space"], 6 * 1024 ** 3)
            self.assertIsNone(apply.call_args.args[0]["cpu_seconds"])
            policy.upscaler_budget.assert_called_with("memory-info")
            policy.upscaler_budget.return_value = 12 * 1024 ** 3
            postprocess_runner._set_limits(manifest)
            self.assertEqual(apply.call_args.args[0]["address_space"], 6 * 1024 ** 3)

    def test_windows_headroom_failure_prevents_limits_or_processor_import(self):
        from scm_workbench import postprocess_memory
        from types import SimpleNamespace
        error = postprocess_memory.MemoryBudgetError("Not enough available memory; close other applications.")
        policy = SimpleNamespace(read_windows_memory=mock.Mock(side_effect=error),
                                 upscaler_budget=mock.Mock())
        with (mock.patch.object(postprocess_runner.os, "name", "nt"),
              mock.patch.object(postprocess_runner, "_fixed_upscaler_source", return_value=True),
              mock.patch.object(postprocess_runner, "_load_memory_policy", return_value=policy),
              mock.patch.object(postprocess_runner, "_set_windows_limits") as apply):
            with self.assertRaisesRegex(postprocess_memory.MemoryBudgetError, "close other applications"):
                postprocess_runner._set_limits({"limits": {**LIMITS, "cpu_seconds": None}})
            apply.assert_not_called()

    def test_memory_policy_loads_under_real_isolated_runner_without_package_imports(self):
        source = str(Path(postprocess_runner.__file__).resolve())
        code = (f"import runpy; runner = runpy.run_path({source!r}); "
                "policy = runner['_load_memory_policy'](); "
                "print(policy.upscaler_budget(policy.MemoryInfo(32*policy.GIB,24*policy.GIB,40*policy.GIB)))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(int(result.stdout.strip()), 16 * 1024 ** 3)

    def test_memory_error_names_image_and_explains_budget_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "game/front").mkdir(parents=True)
            image = root / "game/front/card.png"
            image.write_bytes(png())
            entries = stage_images(discover_images(root), root / "run")
            text = "def process_image(image_path, context):\n    raise MemoryError()\n"
            source = root / "run/processor.py"
            source.write_bytes(text.encode("utf-8"))
            manifest = root / "run/manifest.json"
            manifest.write_text(json.dumps({"source_path": str(source), "run_root": str(root / "run"),
                "entries": runner_entries(entries), "revision": revision_digest(text, []),
                "requirements": [], "contract": CONTRACT_VERSION, "environment": "",
                "environment_root": str(root / "environments"),
                "limits": {**LIMITS, "cpu_seconds": None}}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-B", str(Path(postprocess_runner.__file__).resolve()),
                                     "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("card.png", result.stdout)
            self.assertIn("1.00-GiB Workbench memory budget", result.stdout)
            self.assertIn("Close other applications", result.stdout)
            self.assertEqual(image.read_bytes(), png())

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
    assert set(context) == {'role', 'relative_path', 'name', 'index', 'total'}
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

    def test_skip_collector_is_one_use_plain_text_and_utf8_bounded(self):
        for reason in (None, {}, "", "   ", "bad\nreason", "bad\u202ereason", "é" * 129):
            with self.subTest(reason=reason):
                with self.assertRaises(postprocess_runner.RunnerError):
                    postprocess_runner._collect_skip([], reason)
        reasons = []
        postprocess_runner._collect_skip(reasons, "é" * 128)
        self.assertEqual(reasons, ["é" * 128])
        with self.assertRaises(postprocess_runner.RunnerError):
            postprocess_runner._collect_skip(reasons, "another reason")

    def test_fixed_simple_reports_skip_identity_then_normal_callback_progress(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "game/front").mkdir(parents=True)
            original = root / "game/front/a.png"
            Image.new("RGB", (2, 3)).save(original, dpi=(1200, 1200))
            original_bytes = original.read_bytes()
            Image.new("RGB", (2, 3)).save(root / "game/front/b.png", dpi=(600, 600))
            entries = stage_images(discover_images(root), root / "run")
            source_text = (Path(postprocess_runner.__file__).parent /
                           "builtin_processors/simple_upscaler.py").read_text(encoding="utf-8")
            source = root / "run/processor.py"
            source.write_bytes(source_text.encode("utf-8"))
            manifest = root / "run/manifest.json"
            manifest.write_text(json.dumps({
                "source_path": str(source), "run_root": str(root / "run"),
                "entries": runner_entries(entries), "revision": revision_digest(source_text, []),
                "requirements": [], "contract": CONTRACT_VERSION, "environment": "",
                "environment_root": str(root / "environments"), "limits": LIMITS,
            }), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-B", str(Path(postprocess_runner.__file__).resolve()),
                                     "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            frames = result.stdout.splitlines()
            self.assertEqual(len(frames), 3)
            self.assertTrue(frames[0].startswith(postprocess_runner.SKIP_PREFIX))
            skip = json.loads(frames[0].removeprefix(postprocess_runner.SKIP_PREFIX))
            self.assertEqual(set(skip), {"index", "total", "name", "role", "reason"})
            self.assertEqual((skip["index"], skip["total"], skip["name"], skip["role"]), (1, 2, "a.png", "front"))
            self.assertIn("already at or above 1200", skip["reason"])
            self.assertTrue(all(line.startswith(postprocess_runner.PROGRESS_PREFIX) for line in frames[1:]))
            self.assertEqual((root / "run/work/front/a.png").read_bytes(), original_bytes)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertIn("game/front/a.png", result.stderr)
            # Simulate a Windows CRLF package under real isolated execution.
            package = root / "crlf-package"
            (package / "builtin_processors").mkdir(parents=True)
            runner = package / "postprocess_runner.py"
            runner.write_bytes(Path(postprocess_runner.__file__).read_bytes())
            crlf = source_text.replace("\n", "\r\n")
            (package / "builtin_processors/simple_upscaler.py").write_bytes(crlf.encode("utf-8"))
            source.write_bytes(crlf.encode("utf-8"))
            data = json.loads(manifest.read_text())
            data["revision"] = revision_digest(crlf, [])
            manifest.write_text(json.dumps(data), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-B", str(runner), "--manifest", str(manifest)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.splitlines()[0].startswith(postprocess_runner.SKIP_PREFIX))
            self.assertIn("already at or above 1200", result.stdout)
            source.write_bytes(source_text.encode("utf-8"))
            data["revision"] = revision_digest(source_text, [])
            manifest.write_text(json.dumps(data), encoding="utf-8")
            # Collecting a skip is not enough: a failed callback or non-None
            # return must not turn it into an accepted result frame.
            for fails in (False, True):
                def bad_callback(image_path, context):
                    context["_workbench_report_skip"]("embedded resolution is already at or above 1200 PPI")
                    if fails:
                        raise RuntimeError("callback failed")
                    return {"skipped": True}
                output = io.StringIO()
                with (mock.patch.object(postprocess_runner, "_set_limits"),
                      mock.patch.object(postprocess_runner, "_load_processor", return_value=(bad_callback, None)),
                      redirect_stdout(output)):
                    with self.assertRaises(postprocess_runner.RunnerError):
                        postprocess_runner.run(manifest)
                self.assertNotIn(postprocess_runner.SKIP_PREFIX, output.getvalue())

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
