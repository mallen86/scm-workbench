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
        lock_path.write_text(lock_text, encoding="utf-8")
        job = {
            "id": "fixture", "dependency_stage": str(stage),
            "dependency_target": str(target), "dependency_report": str(report_path),
            "dependency_lock": str(lock_path), "dependency_environment": None,
            "dependency_fingerprint": None, "dependency_requirements": ["demo==1.0"],
            "dependency_processor_id": item["id"], "dependency_revision": item["revision"],
            "dependency_python": str(server.job_python(self.settings)),
        }
        server._finalize_dependency_job(job)
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
        (Path(trusted_status["environment"]["path"]) / "site-packages" / "demo.py").write_text(
            "VALUE = 2\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(Exception, "changed after installation"):
            server._verify_dependency_environment(trusted_status["environment"])

    def test_dependency_job_publishes_ready_marker_and_resets_trust(self):
        item = self.save_and_trust(
            "def process_image(image_path, context):\n    return None\n"
        )
        job, errors = server.start_job("postprocess_dependencies", {
            "processor_id": item["id"],
            "revision_hash": item["revision"],
            "requirements": "",
        })
        self.assertEqual(errors, [])
        self.wait(job)
        self.assertEqual(job["status"], "ok", job["log_lines"])
        status = self.store.status(item["id"], interpreter=server.job_python(self.settings))
        self.assertTrue(status["environment"]["ready"])
        self.assertFalse(status["processor"]["trusted"])
        self.assertFalse(Path(job["dependency_stage"]).exists())
        self.assertEqual(server._PACKAGE_INSTALL_USERS, 0)


if __name__ == "__main__":
    unittest.main()
