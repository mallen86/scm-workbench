"""CUDA profile detection and transactional built-in revision selection."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import advanced_model, cuda_detection, postprocessing, server


class DetectionTests(unittest.TestCase):
    def setUp(self):
        cuda_detection._CACHE = None

    def tearDown(self):
        cuda_detection._CACHE = None

    def test_runtime_pairs_not_driver_version_and_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(cuda_detection, "_library_dirs", return_value=[root]), mock.patch.object(
                    cuda_detection, "_loader_cache", return_value="") as loader:
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda12")
                (root / "libcudart.so.13").touch()
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda12")
                cuda_detection._CACHE = None
                (root / "libcublas.so.13").touch()
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda13")
                (root / "libcudart.so.12").touch()
                (root / "libcublas.so.12").touch()
                cuda_detection._CACHE = None
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda13")
                self.assertEqual(loader.call_count, 3)
                (root / "libcublas.so.13").unlink()
                cuda_detection._CACHE = None
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda12")

    @unittest.skipIf(os.name == "nt", "ldconfig reports POSIX paths; Linux-only detection")
    def test_loader_cache_requires_existing_absolute_libraries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / "libcudart.so.13", root / "libcublas.so.13"]
            for path in paths:
                path.touch()
            cache = "".join(f"\t{path.name} (libc6,x86-64) => {path}\n" for path in paths)
            with mock.patch.object(cuda_detection, "_library_dirs", return_value=[]), mock.patch.object(
                    cuda_detection, "_loader_cache", return_value=cache):
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda13")
                paths[1].unlink()
                cuda_detection._CACHE = None
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda12")

    @unittest.skipIf(os.name == "nt", "LD_LIBRARY_PATH uses POSIX paths; Windows uses DirectML")
    def test_only_runner_visible_cuda_can_win_auto_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            system, toolkit = Path(temp) / "system", Path(temp) / "cuda-13.1" / "lib64"
            system.mkdir(); toolkit.mkdir(parents=True)
            for name in ("libcudart.so.12", "libcublas.so.12"):
                (system / name).touch()
            for name in ("libcudart.so.13", "libcublas.so.13"):
                (toolkit / name).touch()
            with mock.patch.object(cuda_detection, "_SYSTEM_DIRS", (str(system),)), mock.patch.object(
                    cuda_detection, "_loader_cache", return_value=""), mock.patch.dict(
                    os.environ, {"LD_LIBRARY_PATH": ""}):
                self.assertEqual(cuda_detection._library_dirs(), [system])
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda12")
                cuda_detection._CACHE = None
                os.environ["LD_LIBRARY_PATH"] = str(toolkit)
                self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda13")
                with mock.patch.object(server.sys, "platform", "linux"):
                    work = Path(temp) / "work"
                    work.mkdir()
                    self.assertEqual(server._postprocess_env(work, system_gpu_libraries=True)["LD_LIBRARY_PATH"],
                                     str(toolkit))
                cuda_detection._CACHE = None
                os.environ["LD_LIBRARY_PATH"] = ""
                cache = "".join(f"\t{name} (libc6,x86-64) => {toolkit / name}\n" for name in
                                ("libcudart.so.13", "libcublas.so.13"))
                with mock.patch.object(cuda_detection, "_loader_cache", return_value=cache):
                    self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda13")

    @unittest.skipIf(os.name == "nt", "Linux loader path validation uses POSIX path semantics")
    def test_bounded_hostile_loader_output(self):
        with mock.patch.object(cuda_detection, "_library_dirs", return_value=[]), mock.patch.object(
                cuda_detection, "_loader_cache", return_value="nvidia-smi CUDA Version: 13.1\nlibcudart.so.13 => relative\n"):
            self.assertEqual(cuda_detection.detect_cuda_profile()["recommended"], "cuda12")
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": ":relative:/tmp:" + "x" * 5000}):
            self.assertEqual(cuda_detection.inherited_library_dirs(), ())
            self.assertEqual(cuda_detection._library_dirs(), [Path(path) for path in cuda_detection._SYSTEM_DIRS])
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": ":relative:" + temp}):
            self.assertEqual(cuda_detection.inherited_library_dirs(), (temp,))
            self.assertEqual(cuda_detection.inherited_library_dirs(temp + ":" + temp + "\n"), (temp,))
            self.assertEqual(cuda_detection.inherited_library_dirs(":".join([temp] * 33)), ())

    @unittest.skipIf(os.name == "nt", "Linux ldconfig capture uses selectable POSIX pipes")
    def test_loader_capture_kills_on_excess_and_timeout(self):
        real_popen = subprocess.Popen
        launched = []
        def spawn(_argv, **kwargs):
            script = ("import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 4096))" if not launched else
                      "import time; time.sleep(10)")
            process = real_popen([sys.executable, "-c", script], **kwargs)
            launched.append(process)
            return process
        with mock.patch.object(cuda_detection.os.path, "isfile", side_effect=lambda path: path == "/sbin/ldconfig"), mock.patch.object(
                cuda_detection.subprocess, "Popen", side_effect=spawn):
            self.assertEqual(cuda_detection._loader_cache(), "")
            self.assertLessEqual(launched[0].poll() or 0, 0)
            start = time.monotonic()
            self.assertEqual(cuda_detection._loader_cache(), "")
            self.assertLess(time.monotonic() - start, 2.5)
            self.assertIsNotNone(launched[1].poll())


class ProfileTests(unittest.TestCase):
    def test_exact_profile_pins(self):
        self.assertIn("onnxruntime-gpu==1.26.0", advanced_model.requirements_for_platform("linux", "cuda12"))
        self.assertIn("onnxruntime-gpu==1.30.0", advanced_model.requirements_for_platform("linux", "cuda13"))
        self.assertNotEqual(advanced_model.requirements_for_platform("linux", "cuda12"),
                            advanced_model.requirements_for_platform("linux", "cuda13"))
        with self.assertRaises(ValueError):
            advanced_model.requirements_for_platform("linux", "amd")

    def test_switch_and_failed_switch_preserve_revision_and_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            store = postprocessing.ProcessorStore(temp)
            source = "def process_image(image_path, context):\n    return None\n"
            ident = server.BUILTIN_ADVANCED_UPSCALER_ID
            twelve = advanced_model.requirements_for_platform("linux", "cuda12")
            thirteen = advanced_model.requirements_for_platform("linux", "cuda13")
            first = store.provision_bundled(ident, "Advanced Upscaler (AI 4×)", source,
                                            requirements=twelve, optional_model=True)
            old_meta = store._metadata(ident)
            with self.assertRaises(postprocessing.IntegrityError):
                store.activate_optional_environment(ident, first["revision"], thirteen, "a" * 64)
            self.assertEqual(old_meta, store._metadata(ident))
            old_fp = store.environment_metadata(twelve, "a" * 64)["fingerprint"]
            new_fp = store.environment_metadata(thirteen, "b" * 64)["fingerprint"]
            self.assertNotEqual(old_fp, new_fp)
            def ready(req, lock_hash="", **kwargs):
                return {"ready": True, "fingerprint": old_fp if tuple(req) == twelve else new_fp,
                        "tree_digest": "c" * 64, "status": "ready", "path": temp, "requirements": list(req)}
            with mock.patch.object(store, "environment_metadata", side_effect=ready):
                switched = store.activate_optional_environment(ident, first["revision"], thirteen, "b" * 64)
                self.assertTrue(switched["processor"]["ready_to_run"])
                self.assertEqual(tuple(store.get(ident)["requirements"]), thirteen)
                self.assertEqual(store._metadata(ident)["installed_environment"], new_fp)
                back = store.activate_optional_environment(ident, switched["processor"]["revision"], twelve, "a" * 64)
                self.assertTrue(back["processor"]["ready_to_run"])
                self.assertEqual(store.get(ident)["revision"], first["revision"])
            with self.assertRaises(postprocessing.ConflictError):
                store.activate_optional_environment(ident, switched["processor"]["revision"], thirteen, "b" * 64)

    def test_damaged_bundled_revision_repairs_without_losing_approved_profile(self):
        for profile in ("cuda12", "cuda13"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temp, mock.patch.object(
                    server.sys, "platform", "linux"), mock.patch.object(
                    advanced_model, "REQUIREMENTS", advanced_model.requirements_for_platform("linux", "cuda12")):
                old_data = server.DATA_DIR
                server.DATA_DIR = Path(temp)
                try:
                    store = server._postprocessor_store()
                    ident = server.BUILTIN_ADVANCED_UPSCALER_ID
                    original = store.get(ident)
                    if profile == "cuda13":
                        with mock.patch.object(store, "environment_metadata", return_value={
                                "ready": True, "fingerprint": "a" * 64, "tree_digest": "b" * 64,
                                "path": temp, "status": "ready"}):
                            original = store.activate_optional_environment(
                                ident, original["revision"],
                                advanced_model.requirements_for_platform("linux", "cuda13"), "c" * 64)["processor"]
                    before = store._metadata(ident)
                    source = store.get(ident)["source"]
                    path = store._processor(ident) / "revisions" / f"{original['revision']}.py"
                    stat_before = path.stat().st_mtime_ns
                    self.assertEqual(server._postprocessor_store()._metadata(ident), before)
                    self.assertEqual(path.stat().st_mtime_ns, stat_before)
                    path.write_bytes(b"damaged bundled source")
                    refreshed = server._postprocessor_store()
                    self.assertEqual(refreshed.get(ident)["source"], source)
                    self.assertEqual(refreshed._metadata(ident), before)
                    self.assertEqual(tuple(refreshed.get(ident)["requirements"]),
                                     advanced_model.requirements_for_platform("linux", profile))
                    path.unlink()
                    self.assertEqual(server._postprocessor_store().get(ident)["source"], source)
                    self.assertEqual(store._metadata(ident), before)
                finally:
                    server.DATA_DIR = old_data
                    server.invalidate_manifest_cache()

    def test_corrupt_profile_metadata_does_not_claim_cuda13(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(
                advanced_model, "REQUIREMENTS", advanced_model.requirements_for_platform("linux", "cuda12")):
            old_data = server.DATA_DIR
            server.DATA_DIR = Path(temp)
            try:
                store = server._postprocessor_store()
                ident = server.BUILTIN_ADVANCED_UPSCALER_ID
                metadata = store._metadata(ident)
                metadata["active_revision"] = "f" * 64
                postprocessing._atomic_json(store._processor(ident) / "metadata.json", metadata)
                repaired = server._postprocessor_store()
                self.assertEqual(tuple(repaired.get(ident)["requirements"]),
                                 advanced_model.requirements_for_platform("linux", "cuda12"))
                self.assertNotEqual(repaired._metadata(ident)["active_revision"], "f" * 64)
            finally:
                server.DATA_DIR = old_data
                server.invalidate_manifest_cache()

    def test_legacy_profile_does_not_change_on_listing_with_cuda13_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            old_data = server.DATA_DIR
            server.DATA_DIR = Path(temp)
            try:
                with mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(
                        advanced_model, "REQUIREMENTS", advanced_model.requirements_for_platform("linux", "cuda12")), mock.patch.object(
                        cuda_detection, "detect_cuda_profile", return_value={
                            "recommended": "cuda13", "reason": "CUDA 13 runtime visible"}):
                    store = server._postprocessor_store()
                    ident = server.BUILTIN_ADVANCED_UPSCALER_ID
                    original = store.get(ident)
                    before = store._metadata(ident)
                    fingerprint, tree, lock = "a" * 64, "b" * 64, "c" * 64
                    before.update(installed_revision=original["revision"],
                                  installed_environment=fingerprint, installed_tree_digest=tree,
                                  environment=fingerprint, trusted_tree_digest=tree, lock_hash=lock)
                    postprocessing._atomic_json(store._processor(ident) / "metadata.json", before)
                    original_environment_metadata = postprocessing.ProcessorStore.environment_metadata
                    def environment_metadata(instance, req, lock_hash="", **kwargs):
                        if tuple(req) == advanced_model.REQUIREMENTS:
                            return {"ready": True, "status": "ready", "fingerprint": fingerprint,
                                    "tree_digest": tree, "path": temp, "requirements": list(req)}
                        return original_environment_metadata(instance, req, lock_hash, **kwargs)
                    with mock.patch.object(postprocessing.ProcessorStore, "environment_metadata", environment_metadata), mock.patch.object(
                            server, "_verify_dependency_environment"), mock.patch.object(
                            advanced_model, "verify_model", return_value=True):
                        self.assertEqual(server.postprocessors_list()["ok"], True)
                        status = server.postprocessor_get(ident)
                        self.assertEqual(status["cuda_profile"], "cuda12")
                        self.assertTrue(status["ready_to_run"])
                        self.assertEqual(tuple(status["requirements"]),
                                         advanced_model.requirements_for_platform("linux", "cuda12"))
                    self.assertEqual(store._metadata(ident), before)
                    self.assertEqual(store.get(ident)["revision"], original["revision"])
            finally:
                server.DATA_DIR = old_data
                server.invalidate_manifest_cache()


if __name__ == "__main__":
    unittest.main()
