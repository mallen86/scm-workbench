"""End-to-end job coverage for managed image post-processing."""

import base64
import hashlib
import json
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
