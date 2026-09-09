"""Focused tests for the external update handoff contract."""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path, PosixPath
from unittest.mock import patch

from scm_workbench import server, updater


class HandoffRecordTests(unittest.TestCase):
    def test_journal_and_request_are_exact_bounded_atomic_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "SCM Workbench"
            token = "a" * 64
            updater._write_handoff_records(root, token, "v2.0.0", target, 11, 12)
            journal = json.loads((root / ".update-journal.json").read_text())
            self.assertEqual(set(journal), {
                "version", "token", "phase", "expected_version", "target",
                "candidate_name", "backup_name", "old_shell_pid", "old_worker_pid",
            })
            self.assertEqual(journal["phase"], "prepared")
            self.assertEqual(journal["expected_version"], "2.0.0")
            self.assertEqual(journal["candidate_name"], f".SCM-Workbench-candidate-{token}")
            self.assertEqual(json.loads((root / ".update-launch-request.json").read_text()),
                             {"version": 1, "token": token})
            self.assertLess((root / ".update-journal.json").stat().st_size, 64 * 1024)

    def test_malformed_release_tag_cannot_quiesce_or_write_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = {name: getattr(server, name) for name in (
                "DATA_DIR", "JOBS_FILE", "LOGS_DIR", "JOBS", "_UPDATE_QUIESCING",
                "_UPDATE_QUIESCING_JOB")}
            try:
                server.DATA_DIR = root / "data"
                server.JOBS_FILE = server.DATA_DIR / "jobs.json"
                server.LOGS_DIR = server.DATA_DIR / "logs"
                server.JOBS = {}
                server._UPDATE_QUIESCING = False
                server._UPDATE_QUIESCING_JOB = None
                job = {"id": "job", "ts": time.time(), "status": "running"}
                with self.assertRaises(updater.UpdateError):
                    updater._begin_handoff(job, {"latest": "not-a-version"}, "a" * 64,
                                            root / "target", root / "candidate")
                self.assertFalse(server._UPDATE_QUIESCING)
                self.assertFalse((server.DATA_DIR / ".update-journal.json").exists())
            finally:
                for name, value in old.items():
                    setattr(server, name, value)

    def test_run_job_cross_boundary_handoff_and_fresh_process_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            bundle = root / "installed"
            data.mkdir()
            bundle.mkdir()
            old = {name: getattr(server, name) for name in (
                "DATA_DIR", "JOBS_FILE", "LOGS_DIR", "UPDATE_STATE_FILE", "SERVER_VERSION",
                "JOBS", "_UPDATE_QUIESCING", "_UPDATE_QUIESCING_JOB")}
            try:
                server.DATA_DIR = data
                server.JOBS_FILE = data / "jobs.json"
                server.LOGS_DIR = data / "logs"
                server.UPDATE_STATE_FILE = data / "update-state.json"
                server.SERVER_VERSION = "1.0.0"
                server.JOBS = {}
                server._UPDATE_QUIESCING = False
                server._UPDATE_QUIESCING_JOB = None
                job = {
                    "id": "job", "ts": time.time(), "kind": "update", "title": "Update",
                    "cmd": "update", "args": {}, "status": "running", "exit_code": None,
                    "log_file": str(data / "logs/job.log"), "log_lines": [], "subs": [],
                    "started": time.time(), "duration": None,
                }
                server.JOBS[job["id"]] = job
                asset = {"name": "scm-workbench-macos.dmg", "url": "https://example.invalid/a",
                         "size": 1, "tag": "v2.0.0", "digest": None}
                release = {"tag": "v2.0.0", "assets": [asset]}

                def fake_download(_url, destination, **_kwargs):
                    Path(destination).parent.mkdir(parents=True, exist_ok=True)
                    Path(destination).write_bytes(b"zip")

                def fake_extract(_asset, _archive, destination, _expected_version, **_kwargs):
                    Path(destination).mkdir()
                    return Path(destination)

                legacy = []
                log = io.StringIO()
                with patch.object(updater, "_is_install_bundle", return_value=True), \
                     patch.object(updater, "latest_release", return_value=release), \
                     patch.object(updater, "pick_asset", return_value=asset), \
                     patch.object(updater, "download", side_effect=fake_download), \
                     patch.object(updater, "prepare_asset", side_effect=fake_extract) as prepare, \
                     patch.object(updater, "swap_bundle", side_effect=lambda *a, **k: legacy.append("swap")), \
                     patch.object(updater, "relaunch_detached", side_effect=lambda *a, **k: legacy.append("relaunch")), \
                     patch.object(updater, "stop_ancestors", side_effect=lambda *a, **k: legacy.append("stop")):
                    updater.run_job(job, {
                        "repo": "owner/workbench", "current": "1.0.0", "latest": "v2.0.0",
                        "asset": asset, "bundle": str(bundle), "work": data / "update",
                    }, log)
                self.assertEqual(job["status"], "handoff")
                self.assertEqual(prepare.call_args.args[0], asset)
                self.assertEqual(prepare.call_args.args[3], "v2.0.0")
                self.assertTrue(server._UPDATE_QUIESCING)
                self.assertEqual(legacy, [])
                journal = json.loads((data / ".update-journal.json").read_text())
                self.assertEqual(journal["expected_version"], "2.0.0")
                self.assertTrue((bundle.parent / journal["candidate_name"]).is_dir())
                self.assertEqual(json.loads((data / ".update-launch-request.json").read_text())["token"],
                                 journal["token"])
                persisted = json.loads((server.JOBS_FILE).read_text())
                self.assertEqual(persisted[0]["status"], "handoff")
                self.assertEqual(persisted[0]["expected_version"], "2.0.0")

                # Simulate the rollback/new-shell boundary with a fresh process:
                # only the persisted handoff row is available to reconciliation.
                token = journal["token"]
                (data / ".update-result.json").write_text(json.dumps({
                    "version": 1, "token": token, "success": False,
                    "expected_version": "2.0.0", "message": "bounded failure",
                }))
                server.JOBS = {}
                self.assertTrue(server.reconcile_update_result())
                self.assertFalse((data / ".update-result.json").exists())
                finalized = json.loads(server.JOBS_FILE.read_text())[0]
                self.assertEqual(finalized["status"], "fail")
                self.assertEqual(finalized["update_token"], token)
                self.assertFalse(server._UPDATE_QUIESCING)
            finally:
                for name, value in old.items():
                    setattr(server, name, value)

    def test_reconciliation_canonical_success_merge_failure_and_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = {name: getattr(server, name) for name in (
                "DATA_DIR", "JOBS_FILE", "LOGS_DIR", "UPDATE_STATE_FILE", "SERVER_VERSION",
                "JOBS", "_UPDATE_QUIESCING", "_UPDATE_QUIESCING_JOB")}
            try:
                server.DATA_DIR = root / "data"
                server.JOBS_FILE = server.DATA_DIR / "jobs.json"
                server.LOGS_DIR = server.DATA_DIR / "logs"
                server.UPDATE_STATE_FILE = server.DATA_DIR / "update-state.json"
                server.SERVER_VERSION = "0.3.7"
                server.JOBS = {}
                server._UPDATE_QUIESCING = True
                server._UPDATE_QUIESCING_JOB = "handoff"
                token = "b" * 64
                handoff = {"id": "handoff", "ts": time.time(), "kind": "update",
                           "title": "Update", "cmd": "update", "args": {},
                           "status": "handoff", "exit_code": None, "log_file": "x",
                           "duration": None, "update_token": token,
                           "expected_version": "0.3.7"}
                live = {"id": "live", "ts": time.time(), "kind": "create_pdf",
                        "title": "Live", "cmd": "live", "status": "running"}
                server.DATA_DIR.mkdir()
                server.JOBS_FILE.write_text(json.dumps([handoff]))
                server.JOBS["live"] = live
                result = {"version": 1, "token": token, "success": True,
                          "expected_version": "0.3.7", "message": "update completed"}
                result_path = server.DATA_DIR / ".update-result.json"
                result_path.write_text(json.dumps(result))
                self.assertTrue(server.reconcile_update_result())
                self.assertEqual(server.JOBS["live"]["status"], "running")
                self.assertEqual(server.load_update_state()["status"], "up-to-date")
                self.assertFalse(result_path.exists())
                self.assertFalse(server.reconcile_update_result())  # idempotent replay

                # A malformed/non-canonical expected version is stale and is
                # neither accepted nor removed.
                result["expected_version"] = "v0.3.7"
                result_path.write_text(json.dumps(result))
                self.assertFalse(server.reconcile_update_result())
                self.assertTrue(result_path.exists())

                # A durable write failure leaves the exact result for replay.
                result["expected_version"] = "0.3.7"
                server.JOBS = {}
                server.JOBS_FILE.write_text(json.dumps([handoff]))
                result_path.write_text(json.dumps(result))
                with patch.object(server, "_persist_jobs", side_effect=OSError("disk full")):
                    self.assertFalse(server.reconcile_update_result())
                self.assertTrue(result_path.exists())
            finally:
                for name, value in old.items():
                    setattr(server, name, value)

    def test_dev_checkout_is_not_an_install_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"SCM_WORKBENCH_PACKAGED": "1", "SCM_WORKBENCH_BUNDLE": ""}, clear=False), \
                 patch.object(server.sys, "argv", [str(Path(td) / "python")]):
                self.assertIsNone(server._own_bundle())

    def test_windows_bundle_discovery_requires_flat_release_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "bundle"
            (root / "app/scm_workbench").mkdir(parents=True)
            (root / "app/ui").mkdir()
            (root / "runtime").mkdir()
            (root / "SCM Workbench.exe").write_bytes(b"MZ")
            worker = root / "runtime/python/install/python.exe"
            worker.parent.mkdir(parents=True)
            worker.write_bytes(b"python")
            stack = contextlib.ExitStack()
            with stack:
                stack.enter_context(patch.dict(os.environ, {"SCM_WORKBENCH_PACKAGED": "1"}, clear=False))
                stack.enter_context(patch.object(server.sys, "platform", "win32"))
                if os.name != "nt":
                    stack.enter_context(patch.object(server.os, "name", "nt"))
                    stack.enter_context(patch.object(server, "Path", PosixPath))
                stack.enter_context(patch.object(server.sys, "argv", [str(worker)]))
                self.assertEqual(str(server._own_bundle()), str(root.resolve()))


if __name__ == "__main__":
    unittest.main()
