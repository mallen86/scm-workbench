"""End-to-end job coverage for managed image post-processing."""

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import server
from scm_workbench.postprocessing import ProcessorStore
from tests.test_postprocessors import JPEG


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


class PostprocessJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="postprocess-job-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.repo = self.root / "scm"
        (self.repo / "game" / "front").mkdir(parents=True)
        (self.repo / "game" / "double_sided").mkdir(parents=True)
        (self.repo / "game" / "back").mkdir(parents=True)
        self.data.mkdir()
        self.settings = {
            "scm_dir": str(self.repo),
            "extras_dir": str(self.repo),
            "ui_mode": "advanced",
        }
        names = ("DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR")
        self.old = {name: getattr(server, name) for name in names}
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.JOBS.clear()
        server.invalidate_manifest_cache()
        self.settings_patch = mock.patch.object(server, "load_settings", return_value=self.settings)
        self.settings_patch.start()
        self.store = ProcessorStore(self.data, self.repo)

    def tearDown(self):
        server.stop_all_jobs(timeout=3)
        server.JOBS.clear()
        self.settings_patch.stop()
        for name, value in self.old.items():
            setattr(server, name, value)
        server.invalidate_manifest_cache()
        self.temp.cleanup()

    def save_and_trust(self, source, requirements=""):
        item = self.store.save("Processor", source, requirements)
        python = server.job_python(self.settings)
        fingerprint = self.store.environment_metadata(
            item["requirements"], interpreter=python,
        )["fingerprint"]
        self.store.trust(
            item["id"], item["revision"], fingerprint, interpreter=python,
        )
        server.invalidate_manifest_cache()
        return item

    def wait(self, job):
        deadline = time.time() + 15
        while job["status"] == "running" and time.time() < deadline:
            time.sleep(0.02)
        self.assertNotEqual(job["status"], "running", job.get("log_lines"))

    def start_images(self, item):
        return server.start_job("postprocess_images", {
            "processor_id": item["id"],
            "revision_hash": item["revision"],
            "scope": "front",
        })

    def test_only_fixed_linux_gpu_runner_gets_larger_bounded_address_space(self):
        advanced = server.BUILTIN_ADVANCED_UPSCALER_ID
        for platform, expected in (("linux", 16), ("linux2", 16),
                                   ("darwin", 4), ("win32", 4)):
            with self.subTest(platform=platform):
                self.assertEqual(server._postprocess_address_space(advanced, platform=platform),
                                 expected * 1024 ** 3)
        self.assertEqual(server._postprocess_address_space("custom-processor", platform="linux"),
                         4 * 1024 ** 3)

    def test_system_cuda_library_paths_only_reach_fixed_linux_runner(self):
        cuda = self.root / "cuda" / "lib64"
        cudnn = self.root / "cudnn" / "lib"
        cuda.mkdir(parents=True)
        cudnn.mkdir(parents=True)
        inherited = f"{cuda}::relative:{self.root / 'missing'}:{cudnn}"
        run_dir = self.root / "run"
        run_dir.mkdir()
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": inherited}), mock.patch.object(
                server.sys, "platform", "linux"):
            self.assertNotIn("LD_LIBRARY_PATH", server._postprocess_env(run_dir))
            self.assertNotIn("LD_LIBRARY_PATH", server._postprocess_env(run_dir, installing=True))
            env = server._postprocess_env(run_dir, system_gpu_libraries=True)
            self.assertEqual(env["LD_LIBRARY_PATH"], f"{cuda}:{cudnn}")
            with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "x" * 4097}):
                self.assertNotIn("LD_LIBRARY_PATH", server._postprocess_env(
                    run_dir, system_gpu_libraries=True))
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": inherited}), mock.patch.object(
                server.sys, "platform", "darwin"):
            self.assertNotIn("LD_LIBRARY_PATH", server._postprocess_env(
                run_dir, system_gpu_libraries=True))

    def test_progress_frames_require_exact_sequential_entry_identity(self):
        job = {
            "kind": "postprocess_images", "image_total": 1,
            "postprocess_entries": [{"name": "card.png", "role": "front"}],
            "log_lines": [], "subs": [],
        }
        prefix = "WB_POSTPROCESS_PROGRESS "
        server._append_job_line(job, prefix + json.dumps({
            "index": 1.5, "total": 1, "name": "card.png", "role": "front",
        }))
        server._append_job_line(job, prefix + json.dumps({
            "index": True, "total": 1, "name": "card.png", "role": "front",
        }))
        server._append_job_line(job, prefix + json.dumps({
            "index": 1, "total": 1, "name": "other.png", "role": "front",
        }))
        self.assertNotIn("progress", job)
        server._append_job_line(job, prefix + json.dumps({
            "index": 1, "total": 1, "name": "card.png", "role": "front",
        }))
        self.assertEqual(job["progress"], {"current": 1, "total": 1, "label": "card.png"})

    def test_advanced_activity_is_bounded_authorized_and_never_completes_an_image(self):
        first = {"index": 1, "total": 2, "name": "Card  A.png", "role": "front",
                 "phase": "initializing", "provider": "CUDAExecutionProvider", "tile": 0, "tiles": 0}
        job = {"kind": "postprocess_images", "postprocess_builtin_activity": True,
               "image_total": 2, "postprocess_entries": [
                   {"name": "Card  A.png", "role": "front"},
                   {"name": "B.png", "role": "front"}],
               "progress": {"current": 0, "total": 2}, "log_lines": [], "subs": []}
        prefix = server._ADVANCED_ACTIVITY_PREFIX
        def send(frame):
            server._append_job_line(job, prefix + json.dumps(frame))
        with mock.patch.object(server.sys, "platform", "linux"):
            for bad in ({**first, "index": 2}, {**first, "name": "other.png"},
                        {**first, "tile": True}, {**first, "unexpected": 1},
                        {**first, "phase": "tile", "tile": 1, "tiles": 2},
                        {**first, "provider": "CoreMLExecutionProvider"},
                        {**first, "name": "x" * 5000}):
                send(bad)
            self.assertNotIn("activity", job["progress"])
            send(first)
            self.assertEqual(job["progress"]["current"], 0)
            for invalid in ({**first, "reason": "missing_cudnn"},
                            {**first, "phase": "fallback", "provider": "CPUExecutionProvider", "reason": "other"},
                            {**first, "phase": "fallback", "provider": "CPUExecutionProvider", "reason": None},
                            {**first, "phase": "fallback", "provider": "CPUExecutionProvider", "reason": "x" * 5000}):
                send(invalid)
                self.assertEqual(job["progress"]["activity"], first)
                self.assertNotIn("postprocess_cpu_reason", job)
            send({**first, "phase": "fallback", "provider": "CPUExecutionProvider", "reason": "missing_cudnn"})
            self.assertEqual(job["postprocess_cpu_warning"], "cuda")
            self.assertEqual(job["postprocess_cpu_reason"], "missing_cudnn")
            send({**first, "phase": "tile", "provider": "CPUExecutionProvider", "tiles": 4})
            send({**first, "phase": "tile", "provider": "CPUExecutionProvider", "tile": 2, "tiles": 4})
            self.assertEqual(job["progress"]["activity"]["tile"], 2)
            for bad in ({**first, "phase": "tile", "provider": "CPUExecutionProvider", "tile": 1, "tiles": 4},
                        {**first, "phase": "tile", "provider": "CPUExecutionProvider", "tile": 3, "tiles": 10},
                        {**first, "phase": "fallback", "provider": "CUDAExecutionProvider"},
                        {**first, "phase": "tile", "provider": "CPUExecutionProvider", "tile": 3, "tiles": 4, "index": 2}):
                send(bad)
            self.assertEqual(job["progress"]["activity"]["tile"], 2)
            self.assertEqual(job["progress"]["current"], 0)
            job["postprocess_builtin_activity"] = False
            send({**first, "phase": "tile", "provider": "CPUExecutionProvider", "tile": 4, "tiles": 4})
            self.assertEqual(job["progress"]["activity"]["tile"], 2)
            job["postprocess_builtin_activity"] = True
            send({**first, "phase": "tile", "provider": "CPUExecutionProvider", "tile": 4, "tiles": 4})
            self.assertEqual(job["progress"]["current"], 0)
            server._append_job_line(job, "WB_POSTPROCESS_PROGRESS " + json.dumps(
                {"index": 1, "total": 2, "name": "Card  A.png", "role": "front"}))
            self.assertEqual(job["progress"]["current"], 1)
            self.assertEqual(job["postprocess_cpu_reason"], "missing_cudnn")
            send(first)  # stale activity for a completed image
            self.assertNotIn("activity", job["progress"])
            job["kind"] = "postprocess_dependencies"
            send({**first, "index": 2, "name": "B.png"})
            self.assertNotIn("activity", job["progress"])

    def test_advanced_reason_requires_cuda_transition_and_legacy_frames_remain_valid(self):
        base = {"index": 1, "total": 1, "name": "card.png", "role": "front",
                "phase": "initializing", "provider": "CPUExecutionProvider", "tile": 0, "tiles": 0}
        job = {"kind": "postprocess_images", "postprocess_builtin_activity": True,
               "postprocess_cuda_profile": "cuda13", "image_total": 1,
               "postprocess_entries": [{"name": "card.png", "role": "front"}],
               "progress": {"current": 0, "total": 1}, "log_lines": [], "subs": []}
        def send(frame):
            server._append_job_line(job, server._ADVANCED_ACTIVITY_PREFIX + json.dumps(frame))
        with mock.patch.object(server.sys, "platform", "linux"):
            send(base)
            send({**base, "phase": "fallback", "reason": "missing_cudnn"})
            self.assertNotIn("postprocess_cpu_warning", job)
            send({**base, "phase": "fallback"})  # legacy CPU-only fallback
            self.assertEqual(job["postprocess_cpu_warning"], "cuda13")
            self.assertNotIn("postprocess_cpu_reason", job)
            send({**base, "phase": "tile", "tiles": 1, "reason": "missing_cudnn"})
            self.assertEqual(job["progress"]["activity"]["phase"], "fallback")
            send({**base, "phase": "tile", "tiles": 1})
            self.assertEqual(job["progress"]["activity"]["phase"], "tile")

    def test_unicode_and_spaced_filenames_keep_image_progress_sequential(self):
        names = ("01-before.png", "02-A\u00a0B-\U0001f0a1.png",
                 "03-after.png", "04-double  space.png")
        for name in names:
            (self.repo / "game" / "front" / name).write_bytes(PNG)
        item = self.save_and_trust("def process_image(image_path, context):\n    return None\n")
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.wait(job)
        frames = [json.loads(line.removeprefix("WB_POSTPROCESS_PROGRESS "))
                  for line in job["log_lines"] if line.startswith("WB_POSTPROCESS_PROGRESS ")]
        self.assertEqual([frame["name"] for frame in frames], list(names))
        self.assertEqual(job["status"], "ok", job["log_lines"])
        self.assertEqual(job["progress"], {"current": len(names), "total": len(names),
                                           "label": names[-1]})

    def test_accented_filename_runner_forces_utf8_in_isolated_mode(self):
        names = ("01-before.png", "02-Pok\u00e9 Pad1.png", "03-after.png")
        for name in names:
            (self.repo / "game" / "front" / name).write_bytes(PNG)
        item = self.save_and_trust("def process_image(image_path, context):\n    return None\n")
        args = {"processor_id": item["id"], "revision_hash": item["revision"],
                "scope": "front"}
        run = self.data / "postprocessing" / "runs" / "accented-progress"
        prepared = {"scm_path": str(self.repo), "cancel_event": threading.Event(),
                    "postprocess_run": str(run), "postprocess_manifest": str(run / "manifest.json")}
        argv, _cwd, env = server._prepare_image_postprocess_job(prepared, args)
        # -I ignores PYTHONIOENCODING and PYTHONUTF8. Windows pipe output
        # otherwise uses a locale codepage, corrupting the name on UTF-8 decode.
        self.assertIn(("-X", "utf8"), list(zip(argv, argv[1:])))
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "ok", job["log_lines"])
        self.assertEqual(job["progress"], {"current": 3, "total": 3, "label": names[-1]})
        frames = [json.loads(line.removeprefix("WB_POSTPROCESS_PROGRESS "))
                  for line in job["log_lines"] if line.startswith("WB_POSTPROCESS_PROGRESS ")]
        self.assertEqual([frame["name"] for frame in frames], list(names))

    def test_install_progress_uses_only_known_installer_stages(self):
        job = {"kind": "postprocess_dependencies", "log_lines": [], "subs": []}
        server._append_job_line(job, "[processor libraries] Downloading the locked wheel set")
        self.assertEqual(job["progress"], {"label": "Downloading the locked wheel set"})
        server._append_job_line(job, "[processor libraries] untrusted arbitrary output")
        self.assertEqual(job["progress"], {"label": "Downloading the locked wheel set"})
        server._append_job_line(job, "[processor libraries] Offline wheel installation complete")
        self.assertEqual(job["progress"], {"label": "Verifying and publishing installed libraries"})

    def test_stage_quota_tolerates_disappearing_installer_directories(self):
        root = self.root / "mutable-stage"
        vanished = root / "pip-unpack"
        vanished.mkdir(parents=True)
        real_scandir = server.os.scandir

        def racing_scandir(path):
            if Path(path) == vanished:
                vanished.rmdir()
                raise FileNotFoundError(path)
            return real_scandir(path)

        enough_space = mock.Mock(free=server.POSTPROCESS_FREE_SPACE_RESERVE_BYTES + 1)
        with mock.patch.object(server.shutil, "disk_usage", return_value=enough_space):
            with mock.patch.object(server.os, "scandir", side_effect=racing_scandir):
                self.assertIsNone(server._postprocess_stage_limit_reason(root, 1024, 10))
            for name in ("one", "two", "three"):
                (root / name).write_bytes(b"x")
            self.assertEqual(
                server._postprocess_stage_limit_reason(root, 1024, 2),
                "entry-count (3 > 2)",
            )

    def test_preview_reports_the_bounded_selected_image_count(self):
        (self.repo / "game" / "front" / "card 1.png").write_bytes(PNG)
        (self.repo / "game" / "front" / "card 2.png").write_bytes(PNG)
        (self.repo / "game" / "double_sided" / "back.png").write_bytes(PNG)
        item = self.save_and_trust(
            "def process_image(image_path, context):\n    return None\n"
        )
        args = {"processor_id": item["id"], "revision_hash": item["revision"], "scope": "front"}
        self.assertEqual(server.build_preview("postprocess_images", args)["image_count"], 2)
        args["scope"] = "both"
        self.assertEqual(server.build_preview("postprocess_images", args)["image_count"], 3)

    def test_back_only_preview_and_job_in_simple_and_advanced_modes(self):
        front = self.repo / "game/front/front.png"
        double = self.repo / "game/double_sided/double.png"
        back = self.repo / "game/back/card.png"
        front.write_bytes(PNG); double.write_bytes(PNG)
        back.write_bytes(JPEG)  # The real JPEG format must survive the PNG filename.
        placeholder = self.repo / "game/back/EMPTY.md"
        placeholder.write_text("keep", encoding="utf-8")
        source = (
            "def process_image(image_path, context):\n"
            "    assert context['role'] == 'back'\n"
            "    with image_path.open('ab') as output:\n"
            "        output.write(b'\\n')\n"
        )
        item = self.save_and_trust(source)
        args = {"processor_id": item["id"], "revision_hash": item["revision"], "scope": "back"}
        for mode in ("simple", "advanced"):
            with self.subTest(mode=mode):
                self.settings["ui_mode"] = mode
                server.invalidate_manifest_cache()
                scope = server.get_manifest()["postprocess_images"]["groups"][0]["options"][2]
                self.assertIn(["back", "Back only"], scope["choices"])
                self.assertEqual(server.build_preview("postprocess_images", args)["image_count"], 1)
                self.assertEqual(server.build_preview("postprocess_images", {**args, "scope": "both"})["image_count"], 2)
                job, errors = server.start_job("postprocess_images", args)
                self.assertEqual(errors, [])
                self.wait(job)
                self.assertEqual(job["status"], "ok", job["log_lines"])
                self.assertEqual(job["postprocess_outcome"], "committed")
                self.assertEqual(job["progress"]["total"], 1)
                self.assertEqual(job["progress"]["current"], 1)
                self.assertTrue(any('"role":"back"' in line for line in job["log_lines"]))
                self.assertEqual(back.read_bytes(), JPEG + b"\n")
                self.assertEqual(front.read_bytes(), PNG)
                self.assertEqual(double.read_bytes(), PNG)
                self.assertEqual(placeholder.read_text(encoding="utf-8"), "keep")
                back.write_bytes(JPEG)

    def test_image_job_reports_staged_total_before_first_result(self):
        (self.repo / "game" / "front" / "card.png").write_bytes(PNG)
        item = self.save_and_trust("def process_image(image_path, context):\n    return None\n")
        run = self.data / "postprocessing" / "runs" / "initial-progress"
        job = {"scm_path": str(self.repo), "cancel_event": threading.Event(),
               "postprocess_run": str(run), "postprocess_manifest": str(run / "manifest.json")}
        server._prepare_image_postprocess_job(job, {
            "processor_id": item["id"], "revision_hash": item["revision"], "scope": "front",
        })
        self.assertEqual(job["progress"], {"current": 0, "total": 1})
        self.assertEqual(job["image_total"], 1)

    def test_simple_mode_can_preview_the_ready_bundled_upscaler(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        self.settings["ui_mode"] = "simple"
        store = server._postprocessor_store()
        item = store.get(server.BUILTIN_SIMPLE_UPSCALER_ID, include_source=False)
        status = store.status(item["id"], interpreter=server.job_python(self.settings))
        self.assertTrue(status["processor"]["bundled"])
        self.assertTrue(status["processor"]["ready_to_run"])
        args = {
            "processor_id": item["id"],
            "revision_hash": item["revision"],
            "scope": "front",
        }
        preview = server.build_preview("postprocess_images", args)
        self.assertEqual(preview["errors"], [])
        self.assertEqual(preview["image_count"], 1)
        with self.assertRaises(server.PreviewError):
            server.build_preview("postprocess_dependencies", {
                "processor_id": item["id"],
                "revision_hash": item["revision"],
                "requirements": "",
            })

    def test_simple_mode_can_preview_only_the_fixed_optional_install(self):
        self.settings["ui_mode"] = "simple"
        server.invalidate_manifest_cache()
        store = server._postprocessor_store()
        item = store.get(server.BUILTIN_ADVANCED_UPSCALER_ID, include_source=False)
        args = {"processor_id": item["id"], "revision_hash": item["revision"],
                "requirements": "\n".join(server.advanced_model.REQUIREMENTS)}
        preview = server.build_preview("postprocess_dependencies", args)
        self.assertEqual(preview["errors"], [])
        self.assertIn("postprocess_installer", str(preview["cmd"]))
        command = server.build_command("postprocess_dependencies", args, self.settings,
                                       server.get_info_cached(), write_deck=False)
        self.assertEqual(command[3], "Install Advanced Upscaler")
        self.assertEqual(command[5], [])
        self.assertFalse(server.postprocessor_status(item["id"])["processor"]["ready_to_run"])
        custom = self.store.save("Custom", "def process_image(image_path, context):\n    pass\n", "numpy==2.5.3")
        with self.assertRaises(server.PreviewError):
            server.build_preview("postprocess_dependencies", {
                "processor_id": custom["id"], "revision_hash": custom["revision"],
                "requirements": "numpy==2.5.3",
            })
        args["requirements"] = "numpy==2.5.3"
        self.assertTrue(server.build_preview("postprocess_dependencies", args)["errors"])

    def test_linux_profile_override_only_for_fixed_model(self):
        with mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(
                server.advanced_model, "REQUIREMENTS",
                server.advanced_model.requirements_for_platform("linux", "cuda12")), mock.patch.object(
                server.cuda_detection, "detect_cuda_profile", return_value={
                    "recommended": "cuda13", "reason": "CUDA 13 runtime visible"}):
            server.invalidate_manifest_cache()
            item = server._postprocessor_store().get(server.BUILTIN_ADVANCED_UPSCALER_ID)
            args = {"processor_id": item["id"], "revision_hash": item["revision"],
                    "requirements": "", "cuda_profile": "cuda13"}
            self.assertEqual(server.build_preview("postprocess_dependencies", args)["errors"], [])
            stage_job = {"id": "1" * 32}
            argv, stage, env = server._prepare_dependency_job(stage_job, args, server.job_python(self.settings))
            try:
                payload = json.loads((stage / "installer-manifest.json").read_text())
                self.assertEqual(payload["cuda_profile"], "cuda13")
                self.assertEqual(tuple(payload["requirements"]), server.advanced_model.requirements_for_platform("linux", "cuda13"))
                self.assertEqual(stage_job["dependency_revision"], item["revision"])
                self.assertEqual(server._postprocessor_store().get(item["id"])["revision"], item["revision"])
            finally:
                import shutil
                shutil.rmtree(stage)
            args["requirements"] = "\n".join(server.advanced_model.requirements_for_platform("linux", "cuda12"))
            self.assertTrue(server.build_preview("postprocess_dependencies", args)["errors"])
            args["requirements"] = ""
            args["cuda_profile"] = "amd"
            self.assertTrue(server.build_preview("postprocess_dependencies", args)["errors"])
            custom = self.store.save("Custom", "def process_image(image_path, context):\n    pass\n", "numpy==2.5.3")
            args.update(processor_id=custom["id"], revision_hash=custom["revision"],
                        requirements="numpy==2.5.3", cuda_profile="cuda13")
            server.invalidate_manifest_cache()
            self.assertTrue(server.build_preview("postprocess_dependencies", args)["errors"])

    def test_failed_or_cancelled_profile_switch_keeps_previous_metadata(self):
        with mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(
                server.advanced_model, "REQUIREMENTS",
                server.advanced_model.requirements_for_platform("linux", "cuda12")):
            server.invalidate_manifest_cache()
            store = server._postprocessor_store()
            item = store.get(server.BUILTIN_ADVANCED_UPSCALER_ID)
            metadata = store._metadata(item["id"])
            metadata.update(installed_revision=item["revision"], installed_environment="a" * 64,
                            installed_tree_digest="b" * 64, environment="a" * 64,
                            trusted_tree_digest="b" * 64, lock_hash="c" * 64)
            server.postprocessing._atomic_json(store._processor(item["id"]) / "metadata.json", metadata)
            args = {"processor_id": item["id"], "revision_hash": item["revision"],
                    "requirements": "", "cuda_profile": "cuda13"}
            prepare = server._prepare_dependency_job
            cancel_current = False
            def fake_child(job, install_args, python):
                _argv, stage, env = prepare(job, install_args, python)
                code = ("import time; time.sleep(5)" if cancel_current else
                        "import sys; print('fixture failure'); sys.exit(1)")
                return ([str(python), "-I", "-B", "-c", code], stage, env)
            with mock.patch.object(server, "_prepare_dependency_job", side_effect=fake_child):
                for cancel in (True, False):
                    cancel_current = cancel
                    job, errors = server.start_job("postprocess_dependencies", args)
                    self.assertEqual(errors, [])
                    if cancel:
                        server.kill_job(job["id"])
                    self.wait(job)
                    self.assertEqual(job["status"], "killed" if cancel else "fail")
                    self.assertEqual(store._metadata(item["id"]), metadata)
                    self.assertEqual(store.get(item["id"])["revision"], item["revision"])

    def test_optional_install_starts_without_an_scm_checkout(self):
        self.settings["ui_mode"] = "simple"
        # A partially downloaded managed checkout is not an SCM repo. Its
        # absence must not block installation into Workbench's own data root.
        with mock.patch.object(server, "effective_dirs", return_value=(None, None)):
            server.invalidate_manifest_cache()
            store = server._postprocessor_store()
            item = store.get(server.BUILTIN_ADVANCED_UPSCALER_ID, include_source=False)
            args = {"processor_id": item["id"], "revision_hash": item["revision"],
                    "requirements": "\n".join(server.advanced_model.REQUIREMENTS)}
            self.assertEqual(server.get_manifest()["postprocess_dependencies"]["needs"], [])
            preview = server.build_preview("postprocess_dependencies", args)
            self.assertEqual(preview["errors"], [])
            self.assertEqual(preview["cwd"], str(self.data))
            run_errors = server.build_preview("postprocess_images", {
                "processor_id": item["id"], "revision_hash": item["revision"],
                "scope": "front",
            })["errors"]
            self.assertTrue(any("SCM repo not found" in error for error in run_errors))
            # Do not download the real model in this regression: a failed
            # installer child still proves admission, logging and history.
            prepare_installer = server._prepare_dependency_job

            def fail_installer(job, install_args, python):
                _argv, stage, env = prepare_installer(job, install_args, python)
                return ([str(python), "-I", "-B", "-c",
                         "import sys; print('fixture installer failed'); sys.exit(1)"],
                        stage, env)

            with mock.patch.object(server, "_prepare_dependency_job", side_effect=fail_installer):
                job, errors = server.start_job("postprocess_dependencies", args)
                self.assertEqual(errors, [])
                self.assertIsNotNone(job)
                self.wait(job)
            self.assertEqual(job["status"], "fail")
            self.assertEqual(job["title"], "Install Advanced Upscaler")
            self.assertIn(job["id"], [row["id"] for row in server.list_jobs()["jobs"]])
            self.assertIn("fixture installer failed", "\n".join(server.get_job_log(job["id"])["lines"]))

            # The same no-checkout path must complete a real, empty
            # dependency installation; only the model network step is faked.
            self.settings["ui_mode"] = "advanced"
            custom = self.store.save("No libraries", "def process_image(image_path, context):\n    pass\n", "")
            empty_job, errors = server.start_job("postprocess_dependencies", {
                "processor_id": custom["id"], "revision_hash": custom["revision"],
                "requirements": "",
            })
            self.assertEqual(errors, [])
            self.wait(empty_job)
            self.assertEqual(empty_job["status"], "ok", empty_job["log_lines"])

    def test_scm_jpeg_named_png_is_counted_and_processed_with_its_real_format(self):
        front = self.repo / "game" / "front" / "scm-image.png"
        back = self.repo / "game" / "double_sided" / "back.png"
        front.write_bytes(JPEG)
        back.write_bytes(PNG)
        item = self.save_and_trust("def process_image(image_path, context):\n    return None\n")
        args = {"processor_id": item["id"], "revision_hash": item["revision"], "scope": "both"}
        self.assertEqual(server.build_preview("postprocess_images", args)["image_count"], 2)
        job, errors = server.start_job("postprocess_images", args)
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "ok", job["log_lines"])
        self.assertEqual(job["progress"]["total"], 2)
        self.assertEqual(job["progress"]["current"], 2)
        self.assertEqual(front.read_bytes(), JPEG)
        self.assertEqual(back.read_bytes(), PNG)

    def test_mislabeled_jpeg_must_remain_jpeg_after_processing(self):
        image = self.repo / "game" / "front" / "scm-image.png"
        image.write_bytes(JPEG)
        source = (
            "import base64\n"
            f"DATA = {base64.b64encode(PNG).decode()!r}\n"
            "def process_image(image_path, context):\n"
            "    image_path.write_bytes(base64.b64decode(DATA))\n"
        )
        item = self.save_and_trust(source)
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "fail", job["log_lines"])
        self.assertEqual(image.read_bytes(), JPEG)

    def test_preparation_preserves_the_approved_source_bytes(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        source = (
            "def process_image(image_path, context):\r\n"
            "    return None\r\n"
        )
        item = self.save_and_trust(source)
        run_dir = self.data / "postprocessing" / "runs" / "source-bytes"
        job = {
            "id": "source-bytes",
            "scm_path": str(self.repo),
            "postprocess_run": str(run_dir),
            "postprocess_manifest": str(run_dir / "manifest.json"),
            "cancel_event": threading.Event(),
        }

        server._prepare_image_postprocess_job(job, {
            "processor_id": item["id"],
            "revision_hash": item["revision"],
            "scope": "front",
        })

        self.assertEqual(
            (run_dir / "processor.py").read_bytes(),
            source.encode("utf-8"),
        )

    def test_advanced_source_copy_does_not_receive_the_app_model_path(self):
        image = self.repo / "game/front/card.png"
        image.write_bytes(PNG)
        store = server._postprocessor_store()
        built_in = store.get(server.BUILTIN_ADVANCED_UPSCALER_ID, include_source=False)
        copied = store.duplicate(built_in["id"], expected_revision=built_in["revision"])
        self.assertNotEqual(copied["id"], built_in["id"])
        site_packages = self.root / "custom-environment/site-packages"
        site_packages.mkdir(parents=True)
        run_dir = self.data / "postprocessing/runs/source-only-copy"
        job = {
            "id": "source-only-copy", "scm_path": str(self.repo),
            "postprocess_run": str(run_dir),
            "postprocess_manifest": str(run_dir / "manifest.json"),
            "cancel_event": threading.Event(),
        }
        # Even a separately installed and trusted custom copy must not be
        # granted the fixed built-in's verified model capability.
        ready = {"processor": {"trusted": True},
                 "environment": {"ready": True, "path": str(site_packages.parent)}}
        with (mock.patch.object(server, "_postprocessor_store", return_value=store),
              mock.patch.object(store, "status", return_value=ready),
              mock.patch.object(server, "_verify_dependency_environment")):
            server._prepare_image_postprocess_job(job, {
                "processor_id": copied["id"], "revision_hash": copied["revision"],
                "scope": "front",
            })
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("model_path", manifest)
        self.assertEqual(manifest["environment"], str(site_packages))
        self.assertEqual(manifest["limits"]["cpu_seconds"], 900)
        self.assertEqual(manifest["limits"]["address_space"], 4 * 1024 ** 3)
        normalized, errors, _warnings = server.normalize_args(
            server.get_manifest()["postprocess_images"],
            {"processor_id": copied["id"], "revision_hash": copied["revision"],
             "scope": "front", "cpu_seconds": None, "model_path": str(site_packages / "model.onnx")})
        self.assertEqual(errors, [])
        self.assertNotIn("cpu_seconds", normalized)
        self.assertNotIn("model_path", normalized)

        fixed_run = self.data / "postprocessing/runs/fixed-linux-gpu"
        fixed_job = {
            "id": "fixed-linux-gpu", "scm_path": str(self.repo),
            "postprocess_run": str(fixed_run),
            "postprocess_manifest": str(fixed_run / "manifest.json"),
            "cancel_event": threading.Event(),
        }
        with (mock.patch.object(server, "_postprocessor_store", return_value=store),
              mock.patch.object(store, "status", return_value=ready),
              mock.patch.object(server, "_verify_dependency_environment"),
              mock.patch.object(server.advanced_model, "verify_model", return_value=True),
              mock.patch.object(server.sys, "platform", "linux")):
            server._prepare_image_postprocess_job(fixed_job, {
                "processor_id": built_in["id"], "revision_hash": built_in["revision"],
                "scope": "front",
            })
        fixed_manifest = json.loads((fixed_run / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("model_path", fixed_manifest)
        self.assertEqual(fixed_manifest["limits"]["address_space"], 16 * 1024 ** 3)
        self.assertIsNone(fixed_manifest["limits"]["cpu_seconds"])
        self.assertEqual(fixed_manifest["limits"]["file_size"], 512 * 1024 * 1024)
        self.assertEqual(fixed_manifest["limits"]["open_files"], 128)
        self.assertEqual(fixed_manifest["limits"]["processes"], 8)

    def test_success_publishes_atomically_and_releases_exclusive_lease(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        item = self.save_and_trust(
            "def process_image(image_path, context):\n"
            "    with open(image_path, 'ab') as stream:\n"
            "        stream.write(b'\\0')\n"
        )
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.assertIn(" -B ", f" {job['cmd']} ")
        self.wait(job)
        self.assertEqual(job["status"], "ok", job["log_lines"])
        self.assertEqual(job["postprocess_outcome"], "committed")
        self.assertEqual(image.read_bytes(), PNG + b"\0")
        self.assertEqual(job["progress"]["current"], 1)
        self.assertEqual(job["progress"]["total"], 1)
        self.assertEqual(server._POSTPROCESS_USERS, 0)
        self.assertFalse(Path(job["postprocess_run"]).exists())

    def test_fixed_advanced_preparation_has_no_timer_but_remains_cancellable(self):
        image = self.repo / "game/front/card.png"
        image.write_bytes(PNG)
        store = server._postprocessor_store()
        fixed = store.get(server.BUILTIN_ADVANCED_UPSCALER_ID, include_source=False)
        args = {"processor_id": fixed["id"], "revision_hash": fixed["revision"], "scope": "front"}
        ready = {"processor": {"trusted": True}, "environment": {"ready": True}}
        entered = threading.Event()
        def blocked(job, _args):
            entered.set()
            self.assertTrue(job["cancel_event"].wait(5))
            raise server.postprocessing.CancelledError("cancelled")
        with (mock.patch.object(server, "_postprocessor_status", return_value=ready),
              mock.patch.object(server, "_prepare_image_postprocess_job", side_effect=blocked),
              mock.patch.object(server.threading, "Timer", side_effect=AssertionError("unexpected timer"))):
            job, errors = server.start_job("postprocess_images", args)
            self.assertEqual(errors, [])
            self.assertTrue(entered.wait(2))
            self.assertIsNone(job["deadline_seconds"])
            self.assertNotIn("timeout_timer", job)
            self.assertTrue(server.kill_job(job["id"]))
            self.wait(job)
        self.assertEqual(job["status"], "killed")
        self.assertEqual(image.read_bytes(), PNG)
        self.assertEqual(server._POSTPROCESS_USERS, 0)

    def test_custom_staging_wall_timeout_still_applies(self):
        image = self.repo / "game/front/card.png"
        image.write_bytes(PNG)
        item = self.save_and_trust("def process_image(image_path, context):\n    return None\n")
        def blocked(job, _args):
            self.assertTrue(job["cancel_event"].wait(5))
            raise server.postprocessing.CancelledError("timed out")
        with (mock.patch.object(server, "POSTPROCESS_RUN_TIMEOUT_SECONDS", 0.05),
              mock.patch.object(server, "_prepare_image_postprocess_job", side_effect=blocked)):
            job, errors = self.start_images(item)
            self.assertEqual(errors, [])
            self.wait(job)
        self.assertTrue(job["timed_out"])
        self.assertEqual(job["status"], "fail")
        self.assertEqual(image.read_bytes(), PNG)

    def test_fixed_advanced_run_ignores_idle_limit_but_custom_run_does_not(self):
        import subprocess
        command = [str(server.job_python(self.settings)), "-c", "import time; time.sleep(0.35)"]
        for unlimited in (True, False):
            with self.subTest(unlimited=unlimited):
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                job = {"id": "idle-fixture", "kind": "postprocess_images", "log_lines": [],
                       "subs": [], "proc": proc, "proc_lock": threading.Lock(),
                       "deadline_seconds": None if unlimited else 3600}
                with (mock.patch.object(server, "POSTPROCESS_RUN_IDLE_TIMEOUT_SECONDS", 0.05),
                      mock.patch.object(server, "_append_job_line")):
                    try:
                        rc = server._drain_postprocess_process(job, proc, None)
                    finally:
                        if proc.poll() is None:
                            server._terminate_and_reap(proc, job["proc_lock"])
                        proc.stdout.close()
                if unlimited:
                    self.assertEqual(rc, 0)
                    self.assertFalse(job.get("idle_timed_out"))
                else:
                    self.assertTrue(job.get("idle_timed_out"))

    def test_staging_is_registered_and_cancellable_before_runner_spawn(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        item = self.save_and_trust(
            "def process_image(image_path, context):\n    return None\n"
        )
        self.settings["ui_mode"] = "simple"
        entered = threading.Event()
        def blocked(job, _args):
            entered.set()
            self.assertTrue(job["cancel_event"].wait(5))
            raise server.postprocessing.CancelledError("cancelled")
        with mock.patch.object(server, "_prepare_image_postprocess_job", side_effect=blocked):
            job, errors = self.start_images(item)
            self.assertEqual(errors, [])
            self.assertTrue(entered.wait(2))
            self.assertEqual(job["deadline_seconds"], server.POSTPROCESS_RUN_TIMEOUT_SECONDS)
            self.assertIsNotNone(job.get("timeout_timer"))
            self.assertTrue(server.kill_job(job["id"]))
            self.wait(job)
        self.assertEqual(job["status"], "killed")
        self.assertEqual(image.read_bytes(), PNG)
        self.assertEqual(server._POSTPROCESS_USERS, 0)

    def test_runner_descendants_cannot_hold_the_job_open_after_callback_completion(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        item = self.save_and_trust(
            "import subprocess, sys\n"
            "def process_image(image_path, context):\n"
            "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        )
        started = time.monotonic()
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "ok", job["log_lines"])
        self.assertEqual(job["postprocess_outcome"], "unchanged")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(image.read_bytes(), PNG)

    def test_closed_output_cannot_bypass_the_idle_watchdog(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        item = self.save_and_trust(
            "import os, time\n"
            "def process_image(image_path, context):\n"
            "    os.close(1)\n"
            "    os.close(2)\n"
            "    time.sleep(30)\n"
        )
        with mock.patch.object(server, "POSTPROCESS_RUN_IDLE_TIMEOUT_SECONDS", 0.1):
            started = time.monotonic()
            job, errors = self.start_images(item)
            self.assertEqual(errors, [])
            self.wait(job)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(job["status"], "fail")
        self.assertEqual(job["postprocess_outcome"], "unchanged")
        self.assertEqual(image.read_bytes(), PNG)

    def test_zero_exit_without_all_callback_progress_is_rejected(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        item = self.save_and_trust(
            "import os\n"
            "def process_image(image_path, context):\n"
            "    os._exit(0)\n"
        )
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "fail")
        self.assertEqual(job["postprocess_outcome"], "unchanged")
        self.assertEqual(image.read_bytes(), PNG)
        self.assertTrue(any("did not complete every callback" in line for line in job["log_lines"]))

    def test_inference_failure_does_not_commit_or_trust_custom_activity(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        frame = json.dumps({"index": 1, "total": 1, "name": "card.png", "role": "front",
                            "phase": "fallback", "provider": "CPUExecutionProvider",
                            "tile": 0, "tiles": 0, "reason": "missing_cudnn"})
        item = self.save_and_trust(
            "def process_image(image_path, context):\n"
            f"    print({(server._ADVANCED_ACTIVITY_PREFIX + frame)!r}, flush=True)\n"
            "    raise RuntimeError('CPU inference failed')\n"
        )
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "fail")
        self.assertEqual(job["postprocess_outcome"], "unchanged")
        self.assertEqual(image.read_bytes(), PNG)
        self.assertNotIn("postprocess_cpu_warning", job)
        self.assertNotIn("postprocess_cpu_reason", job)
        self.assertEqual(job["progress"]["current"], 0)

    def test_invalid_result_never_replaces_original(self):
        image = self.repo / "game" / "front" / "card.png"
        image.write_bytes(PNG)
        item = self.save_and_trust(
            "def process_image(image_path, context):\n"
            "    open(image_path, 'wb').write(b'not an image')\n"
        )
        job, errors = self.start_images(item)
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "fail")
        self.assertEqual(job["postprocess_outcome"], "unchanged")
        self.assertEqual(image.read_bytes(), PNG)
        self.assertEqual(server._POSTPROCESS_USERS, 0)
        self.assertTrue(any("originals were not changed" in line for line in job["log_lines"]))

    def _staged_fixed_profile(self, profile, label):
        """A verified-tree fixture; the real installer is covered separately."""
        stage = self.data / "postprocessing" / "environments" / f".install-{label}"
        target = stage / "site-packages"
        target.mkdir(parents=True)
        (target / server.advanced_model.MODEL_NAME).write_bytes(b"fixture model")
        report = {"install": [{"metadata": {"name": "fixture", "version": "1.0"},
                 "download_info": {"url": "https://files.pythonhosted.org/packages/fixture-1.0-py3-none-any.whl",
                                   "archive_info": {"hashes": {"sha256": "a" * 64}}}}]}
        (stage / "resolve-report.json").write_text(json.dumps(report), encoding="utf-8")
        (stage / "requirements.lock").write_text(f"fixture==1.0 --hash=sha256:{'a' * 64}\n", encoding="utf-8")
        item = server._postprocessor_store().get(server.BUILTIN_ADVANCED_UPSCALER_ID)
        return {"id": label, "dependency_stage": str(stage), "dependency_target": str(target),
                "dependency_report": str(stage / "resolve-report.json"),
                "dependency_lock": str(stage / "requirements.lock"),
                "dependency_environment": None, "dependency_fingerprint": None,
                "dependency_requirements": list(server.advanced_model.requirements_for_platform("linux", profile)),
                "dependency_processor_id": item["id"], "dependency_revision": item["revision"],
                "dependency_python": str(server.job_python(self.settings)),
                "dependency_cuda_profile": profile}

    def test_switch_publication_failure_keeps_old_profile_in_both_cache_branches(self):
        with mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(
                server.advanced_model, "REQUIREMENTS",
                server.advanced_model.requirements_for_platform("linux", "cuda12")), mock.patch.object(
                server.advanced_model, "verify_model", return_value=True):
            store = server._postprocessor_store()
            self.assertTrue(server._finalize_dependency_job(self._staged_fixed_profile("cuda12", "seed-12")))
            ident = server.BUILTIN_ADVANCED_UPSCALER_ID
            old = store._metadata(ident)
            original_atomic = server.postprocessing._atomic_json
            def reject_commit(path, value, **kwargs):
                if path.name == "metadata.json" and path.parent.name == ident:
                    raise OSError("metadata publication failed")
                return original_atomic(path, value, **kwargs)
            # A new fingerprint is published first; a failed pointer commit
            # must remove only that new tree, never the ready CUDA 12 tree.
            new_job = self._staged_fixed_profile("cuda13", "reject-new")
            with mock.patch.object(server.postprocessing, "_atomic_json", side_effect=reject_commit):
                with self.assertRaisesRegex(OSError, "metadata publication failed"):
                    server._finalize_dependency_job(new_job)
            self.assertEqual(store._metadata(ident), old)
            self.assertFalse(Path(store.environment_metadata(
                server.advanced_model.requirements_for_platform("linux", "cuda13"),
                hashlib.sha256(b"fixture==1.0 --hash=sha256:" + b"a" * 64 + b"\n").hexdigest(),
                interpreter=server.job_python(self.settings))["path"]).exists())
            self.assertTrue(server._postprocessor_status(store, ident, server.job_python(self.settings))["processor"]["ready_to_run"])
            # Cache CUDA 13, then switch back to 12 so CUDA 13 exists as a
            # verified, reusable tree. A failed reuse must leave both trees.
            self.assertTrue(server._finalize_dependency_job(self._staged_fixed_profile("cuda13", "seed-13")))
            self.assertTrue(server._finalize_dependency_job(self._staged_fixed_profile("cuda12", "back-12")))
            old = store._metadata(ident)
            cached = store.environment_metadata(
                server.advanced_model.requirements_for_platform("linux", "cuda13"),
                hashlib.sha256(b"fixture==1.0 --hash=sha256:" + b"a" * 64 + b"\n").hexdigest(),
                interpreter=server.job_python(self.settings))["path"]
            with mock.patch.object(server.postprocessing, "_atomic_json", side_effect=reject_commit):
                with self.assertRaisesRegex(OSError, "metadata publication failed"):
                    server._finalize_dependency_job(self._staged_fixed_profile("cuda13", "reject-cached"))
            self.assertEqual(store._metadata(ident), old)
            self.assertTrue(Path(cached).exists())
            self.assertTrue(server._postprocessor_status(store, ident, server.job_python(self.settings))["processor"]["ready_to_run"])

    def test_ambiguous_postcommit_error_does_not_delete_active_profile(self):
        with mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(
                server.advanced_model, "REQUIREMENTS",
                server.advanced_model.requirements_for_platform("linux", "cuda12")), mock.patch.object(
                server.advanced_model, "verify_model", return_value=True):
            server._postprocessor_store()
            original_atomic = server.postprocessing._atomic_json
            ident = server.BUILTIN_ADVANCED_UPSCALER_ID
            def raise_after_commit(path, value, **kwargs):
                original_atomic(path, value, **kwargs)
                if path.name == "metadata.json" and path.parent.name == ident:
                    raise OSError("directory sync failed after metadata rename")
            with mock.patch.object(server.postprocessing, "_atomic_json", side_effect=raise_after_commit):
                self.assertTrue(server._finalize_dependency_job(
                    self._staged_fixed_profile("cuda13", "postcommit-new")))
            store = server._postprocessor_store()
            current = server._postprocessor_status(store, ident, server.job_python(self.settings))
            self.assertTrue(current["processor"]["ready_to_run"])
            self.assertEqual(current["processor"]["cuda_profile"], "cuda13")
            self.assertTrue(Path(current["environment"]["path"]).exists())
            self.assertTrue(server._finalize_dependency_job(self._staged_fixed_profile("cuda12", "postcommit-back")))
            with mock.patch.object(server.postprocessing, "_atomic_json", side_effect=raise_after_commit):
                self.assertTrue(server._finalize_dependency_job(
                    self._staged_fixed_profile("cuda13", "postcommit-cached")))
            current = server._postprocessor_status(store, ident, server.job_python(self.settings))
            self.assertTrue(current["processor"]["ready_to_run"])
            self.assertEqual(current["processor"]["cuda_profile"], "cuda13")

    def test_status_failure_after_optional_activation_cannot_rollback_published_tree(self):
        with mock.patch.object(server.sys, "platform", "linux"), mock.patch.object(
                server.advanced_model, "REQUIREMENTS",
                server.advanced_model.requirements_for_platform("linux", "cuda12")), mock.patch.object(
                server.advanced_model, "verify_model", return_value=True):
            server._postprocessor_store()
            for profile, label in (("cuda13", "status-new"), ("cuda12", "status-cached"),
                                   ("cuda13", "status-reuse")):
                job = self._staged_fixed_profile(profile, label)
                with mock.patch.object(server.postprocessing.ProcessorStore, "status", side_effect=OSError("read failed after publication")):
                    self.assertTrue(server._finalize_dependency_job(job))
                store = server._postprocessor_store()
                item = store.get(server.BUILTIN_ADVANCED_UPSCALER_ID)
                self.assertEqual(tuple(item["requirements"]),
                                 server.advanced_model.requirements_for_platform("linux", profile))
                current = server._postprocessor_status(store, item["id"], server.job_python(self.settings))
                self.assertTrue(current["processor"]["ready_to_run"])
                self.assertTrue(Path(current["environment"]["path"]).exists())

    def test_locked_dependency_environment_binds_trust_to_resolved_hashes(self):
        item = self.store.save(
            "Libraries", "def process_image(image_path, context):\n    return None\n",
            "demo==1.0",
        )
        stage = self.data / "postprocessing" / "environments" / ".install-fixture"
        target = stage / "site-packages"
        target.mkdir(parents=True)
        (target / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
        artifact_hash = "a" * 64
        report = {"install": [{
            "metadata": {"name": "demo", "version": "1.0"},
            "download_info": {
                "url": "https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl",
                "archive_info": {"hashes": {"sha256": artifact_hash}},
            },
        }]}
        report_path = stage / "resolve-report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        lock_text = f"demo==1.0 --hash=sha256:{artifact_hash}\n"
        lock_path = stage / "requirements.lock"
        lock_path.write_bytes(lock_text.encode("utf-8"))
        job = {
            "id": "fixture", "dependency_stage": str(stage),
            "dependency_target": str(target), "dependency_report": str(report_path),
            "dependency_lock": str(lock_path), "dependency_environment": None,
            "dependency_fingerprint": None, "dependency_requirements": ["demo==1.0"],
            "dependency_processor_id": item["id"], "dependency_revision": item["revision"],
            "dependency_python": str(server.job_python(self.settings)),
        }
        self.assertFalse(server._finalize_dependency_job(job))
        status = self.store.status(item["id"], interpreter=server.job_python(self.settings))
        self.assertTrue(status["environment"]["ready"])
        self.assertEqual(
            status["environment"]["fingerprint"],
            self.store.environment_metadata(
                ["demo==1.0"], hashlib.sha256(lock_text.encode()).hexdigest(),
                interpreter=server.job_python(self.settings),
            )["fingerprint"],
        )
        self.store.trust(
            item["id"], item["revision"], status["environment"]["fingerprint"],
            interpreter=server.job_python(self.settings),
        )
        trusted_status = self.store.status(
            item["id"], interpreter=server.job_python(self.settings),
        )
        self.assertTrue(trusted_status["processor"]["trusted"])

        repeat_stage = self.data / "postprocessing" / "environments" / ".install-fixture-repeat"
        repeat_target = repeat_stage / "site-packages"
        repeat_target.mkdir(parents=True)
        (repeat_target / "demo.py").write_text("VALUE = 99\n", encoding="utf-8")
        repeat_report = repeat_stage / "resolve-report.json"
        repeat_report.write_text(json.dumps(report), encoding="utf-8")
        repeat_lock = repeat_stage / "requirements.lock"
        repeat_lock.write_bytes(lock_text.encode("utf-8"))
        repeat_job = {
            **job,
            "id": "fixture-repeat",
            "dependency_stage": str(repeat_stage),
            "dependency_target": str(repeat_target),
            "dependency_report": str(repeat_report),
            "dependency_lock": str(repeat_lock),
        }
        self.assertTrue(server._finalize_dependency_job(repeat_job))
        trusted_status = self.store.status(
            item["id"], interpreter=server.job_python(self.settings),
        )
        self.assertTrue(trusted_status["processor"]["trusted"])
        self.assertEqual(
            (Path(trusted_status["environment"]["path"]) / "site-packages" / "demo.py").read_text(encoding="utf-8"),
            "VALUE = 1\n",
        )

        (Path(trusted_status["environment"]["path"]) / "site-packages" / "demo.py").write_text(
            "VALUE = 2\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(Exception, "changed after installation"):
            server._verify_dependency_environment(trusted_status["environment"])

        repair_stage = self.data / "postprocessing" / "environments" / ".install-fixture-repair"
        repair_target = repair_stage / "site-packages"
        repair_target.mkdir(parents=True)
        (repair_target / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
        repair_report = repair_stage / "resolve-report.json"
        repair_report.write_text(json.dumps(report), encoding="utf-8")
        repair_lock = repair_stage / "requirements.lock"
        repair_lock.write_bytes(lock_text.encode("utf-8"))
        repair_job = {
            **job,
            "id": "fixture-repair",
            "dependency_stage": str(repair_stage),
            "dependency_target": str(repair_target),
            "dependency_report": str(repair_report),
            "dependency_lock": str(repair_lock),
        }
        self.assertTrue(server._finalize_dependency_job(repair_job))
        self.assertTrue(repair_job["dependency_environment_repaired"])
        repaired = self.store.status(
            item["id"], interpreter=server.job_python(self.settings),
        )
        self.assertTrue(repaired["processor"]["trusted"])
        server._verify_dependency_environment(repaired["environment"])
        self.assertEqual(
            (Path(repaired["environment"]["path"]) / "site-packages" / "demo.py").read_text(encoding="utf-8"),
            "VALUE = 1\n",
        )

    def test_dependency_job_preserves_trust_for_unchanged_environment(self):
        item = self.save_and_trust(
            "def process_image(image_path, context):\n    return None\n"
        )
        job, errors = server.start_job("postprocess_dependencies", {
            "processor_id": item["id"],
            "revision_hash": item["revision"],
            "requirements": "",
        })
        self.assertEqual(errors, [])
        self.assertEqual(
            job["stage_max_entries"], server.POSTPROCESS_INSTALL_STAGE_MAX_ENTRIES,
        )
        self.assertEqual(job["deadline_seconds"], server.POSTPROCESS_INSTALL_TIMEOUT_SECONDS)
        self.assertIsNotNone(job.get("timeout_timer"))
        self.assertGreater(job["stage_max_entries"], server.POSTPROCESS_ENV_MAX_FILES)
        self.assertIn(" -B ", f" {job['cmd']} ")
        self.wait(job)
        self.assertEqual(job["status"], "ok", job["log_lines"])
        status = self.store.status(item["id"], interpreter=server.job_python(self.settings))
        self.assertTrue(status["environment"]["ready"])
        self.assertTrue(status["processor"]["trusted"])
        self.assertTrue(any("existing trust remains valid" in line for line in job["log_lines"]))
        self.assertFalse(Path(job["dependency_stage"]).exists())
        self.assertEqual(server._PACKAGE_INSTALL_USERS, 0)


if __name__ == "__main__":
    unittest.main()
