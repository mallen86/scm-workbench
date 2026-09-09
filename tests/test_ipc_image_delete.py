"""Focused contracts for the secure native image-deletion slice."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server
from test_phase0_baseline import Phase0Fixture


PNG = b"\x89PNG\r\n\x1a\nfixture"


class ImageDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-delete-")
        self.root = Path(self.temp.name)
        self.fixture = Phase0Fixture(self.root)
        self.old = {name: getattr(server, name) for name in (
            "DATA_DIR", "UI_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
            "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE", "_IPC_MODE",
            "_IMAGE_JOB_USERS",
        )}
        self.data = self.root / "data"
        self.data.mkdir()
        server.DATA_DIR = self.data
        server.UI_DIR = self.root / "ui"
        server.UI_DIR.mkdir()
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = self.data / "offsets.json"
        server.UPDATE_STATE_FILE = self.data / "updates.json"
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({"scm_dir": str(self.fixture.scm), "extras_dir": str(self.fixture.extras)})
        server.save_settings(settings)
        server._IMAGE_DELETE_LOCK = threading.Lock()
        server._IMAGE_JOB_USERS = 0
        with server._IMAGE_DELETE_OP_LOCK:
            server._IMAGE_DELETE_OPS.clear()

    def tearDown(self):
        for name, value in self.old.items():
            setattr(server, name, value)
        self.temp.cleanup()

    def image_dir(self):
        path = self.fixture.scm / "game" / "double_sided"
        (path / "one.bin").write_bytes(PNG)
        (path / "two.jpg").write_bytes(PNG)
        (path / "README.md").write_text("keep", encoding="utf-8")
        return path

    def test_private_async_worker_is_bounded_and_public_shape_stays_exact(self):
        target = self.image_dir()
        started = ipc.dispatch({"id": "x", "method": "fs.delete_images_start", "params": {"path": str(target)}})
        self.assertTrue(started["ok"])
        operation_id = started["result"]["operation_id"]
        self.assertRegex(operation_id, r"^[0-9a-f]{32}$")
        for _ in range(100):
            polled = ipc.dispatch({"id": "x", "method": "fs.delete_images_poll", "params": {"operation_id": operation_id}})
            if polled["result"].get("done"):
                self.assertTrue(polled["result"]["result"]["ok"])
                break
            time.sleep(.005)
        else:
            self.fail("image deletion operation did not finish")
        self.assertEqual(ipc.dispatch({"id": "x", "method": "fs.delete_images_start", "params": {"path": str(target), "extra": 1}})["error"]["code"], "bad_request")
        self.assertEqual(ipc.dispatch({"id": "x", "method": "fs.delete_images_poll", "params": {"operation_id": operation_id, "extra": 1}})["error"]["code"], "bad_request")

    def test_expired_operation_stops_before_deletion(self):
        target = self.image_dir()
        operation_id = "d" * 32
        with server._IMAGE_DELETE_OP_LOCK:
            server._IMAGE_DELETE_OPS[operation_id] = {
                "done": False, "result": None,
                "deadline": time.monotonic() - 1,
            }
        server._run_image_delete_operation(
            operation_id, str(target), server.load_settings())
        with server._IMAGE_DELETE_OP_LOCK:
            result = server._IMAGE_DELETE_OPS[operation_id]["result"]
        self.assertFalse(result["ok"])
        self.assertIn("cancelled", result["errors"][0])
        self.assertTrue((target / "one.bin").exists())

    def test_async_relative_path_is_bound_to_initiating_settings_snapshot(self):
        old_target = self.image_dir()
        old_settings = server.load_settings()
        other = self.root / "other-scm"
        other_target = other / "game" / "double_sided"
        other_target.mkdir(parents=True)
        (other_target / "other.png").write_bytes(PNG)
        changed = dict(old_settings, scm_dir=str(other))
        server.save_settings(changed)
        operation_id = "b" * 32
        with server._IMAGE_DELETE_OP_LOCK:
            server._IMAGE_DELETE_OPS[operation_id] = {
                "done": False, "result": None,
                "deadline": time.monotonic() + 300,
            }
        server._run_image_delete_operation(
            operation_id, "game/double_sided", old_settings)
        with server._IMAGE_DELETE_OP_LOCK:
            result = server._IMAGE_DELETE_OPS[operation_id]["result"]
        self.assertTrue(result["ok"])
        self.assertFalse((old_target / "one.bin").exists())
        self.assertTrue((other_target / "other.png").exists())

    def test_native_exact_params_and_scm_only_root(self):
        path = self.image_dir()
        good = server.delete_images(str(path))
        self.assertEqual(good["deleted"], 2)
        for params in ({}, {"path": str(path), "extra": 1}, {"path": 4}):
            result = ipc.dispatch({"id": "x", "method": "fs.delete_images_start", "params": params})
            self.assertEqual(result["error"]["code"], "bad_request")
        # The public name is a virtual Rust facade; only its bounded private
        # start/poll edges ever reach the worker protocol.
        self.assertEqual(
            ipc.dispatch({"id": "x", "method": "fs.delete_images", "params": {"path": str(path)}})["error"]["code"],
            "unknown_method",
        )
        for path in (self.data, self.fixture.extras, self.fixture.scm, self.fixture.scm / ".."):
            result = server.delete_images(str(path))
            self.assertFalse(result["ok"])
            self.assertEqual(result["deleted"], 0)

    def test_missing_and_non_directory_are_zero_success(self):
        missing = server.delete_images(str(self.fixture.scm / "game" / "not-created"))
        self.assertEqual(missing, {"ok": True, "deleted": 0, "names": [], "dir": str((self.fixture.scm / "game" / "not-created").resolve())})
        regular = self.fixture.scm / "regular"
        regular.write_text("not a directory", encoding="utf-8")
        result = server.delete_images(str(regular))
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 0)

    @unittest.skipIf(os.name == "nt", "POSIX stable-root test")
    def test_root_replacement_before_open_is_rejected(self):
        target = self.image_dir()
        original_root = self.fixture.scm
        moved_root = self.root / "original-scm"
        canonical_root = original_root.resolve()
        real_open = server.os.open
        replaced = False

        def replace_root(path, *args, **kwargs):
            nonlocal replaced
            if not replaced and Path(path) == canonical_root and kwargs.get("dir_fd") is None:
                replaced = True
                original_root.rename(moved_root)
                replacement_target = original_root / "game" / "double_sided"
                replacement_target.mkdir(parents=True)
                (replacement_target / "replacement.png").write_bytes(PNG)
            return real_open(path, *args, **kwargs)

        try:
            with mock.patch.object(server.os, "open", side_effect=replace_root):
                result = server.delete_images(str(target))
            self.assertFalse(result["ok"])
            self.assertTrue((original_root / "game" / "double_sided" / "replacement.png").exists())
            self.assertTrue((moved_root / "game" / "double_sided" / "one.bin").exists())
        finally:
            if original_root.exists():
                import shutil
                shutil.rmtree(original_root)
            if moved_root.exists():
                moved_root.rename(original_root)

    @unittest.skipIf(os.name == "nt", "POSIX reparse test")
    def test_intermediate_and_final_symlinks_fail_closed(self):
        target = self.image_dir()
        outside = self.fixture.outside
        (outside / "secret.png").write_bytes(PNG)
        final = target / "outside.png"
        final.symlink_to(outside / "secret.png")
        result = server.delete_images(str(target))
        self.assertFalse(result["ok"])
        self.assertTrue((outside / "secret.png").exists())
        nested = self.fixture.scm / "game" / "nested-link"
        nested.symlink_to(target, target_is_directory=True)
        result = server.delete_images(str(nested))
        self.assertFalse(result["ok"])
        self.assertTrue((target / "one.bin").exists())

    @unittest.skipIf(os.name == "nt", "POSIX quarantine race test")
    def test_name_swap_before_quarantine_never_deletes_replacement(self):
        target = self.fixture.scm / "game" / "race"
        target.mkdir()
        original = target / "race.png"
        original.write_bytes(PNG)
        saved = self.root / "saved-original.png"
        replacement = self.root / "replacement.txt"
        replacement.write_bytes(b"not an image")
        real_rename = server._rename_delete_candidate
        swapped = False

        def swap_then_rename(directory_fd, source, destination):
            nonlocal swapped
            if not swapped and source == "race.png":
                swapped = True
                original.rename(saved)
                replacement.rename(original)
            return real_rename(directory_fd, source, destination)

        with mock.patch.object(server, "_rename_delete_candidate", side_effect=swap_then_rename):
            result = server.delete_images(str(target))
        self.assertFalse(result["ok"])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(original.read_bytes(), b"not an image")
        self.assertEqual(saved.read_bytes(), PNG)

    def test_oversize_preflight_deletes_nothing_and_nonimages_stay(self):
        target = self.image_dir()
        (target / "three.png").write_bytes(PNG)
        with mock.patch.object(server, "IMAGE_DELETE_MAX_CANDIDATES", 2):
            result = server.delete_images(str(target))
        self.assertFalse(result["ok"])
        self.assertEqual(result["deleted"], 0)
        self.assertTrue((target / "one.bin").exists())
        self.assertTrue((target / "README.md").exists())
        with mock.patch.object(server, "IMAGE_DELETE_MAX_RESULT_BYTES", 1):
            result = server.delete_images(str(target))
        self.assertFalse(result["ok"])
        self.assertTrue((target / "one.bin").exists())

    @unittest.skipIf(os.name == "nt", "POSIX unlink failure seam")
    def test_partial_application_failure_reports_deleted_names(self):
        target = self.image_dir()
        real_unlink = server.os.unlink
        calls = []
        def unlink(name, *args, **kwargs):
            calls.append(name)
            if len(calls) == 2:
                raise OSError("fixture failure")
            return real_unlink(name, *args, **kwargs)
        with mock.patch.object(server.os, "unlink", side_effect=unlink):
            result = server.delete_images(str(target))
        self.assertFalse(result["ok"])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["names"], ["one.bin"])
        self.assertFalse((target / "one.bin").exists())
        self.assertTrue((target / "two.jpg").exists())

    def test_busy_lock_is_nonblocking_and_fd_cleanup_is_complete(self):
        target = self.image_dir()
        held = server._IMAGE_DELETE_LOCK
        self.assertTrue(held.acquire(False))
        try:
            result = server.delete_images(str(target))
            self.assertFalse(result["ok"])
            self.assertIn("busy", result["errors"][0])
        finally:
            held.release()
        result = server.delete_images(str(target))
        self.assertTrue(result["ok"])
        # The directory fd is closed by the operation; a rename is possible
        # immediately after successful deletion.
        target.rename(target.with_name("double_sided-renamed"))

    @unittest.skipIf(os.name == "nt", "POSIX descriptor accounting")
    def test_missing_nested_target_closes_every_directory_fd(self):
        opened = []
        closed = []
        real_open = server.os.open
        real_close = server.os.close

        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def record_close(fd):
            closed.append(fd)
            return real_close(fd)

        missing = self.fixture.scm / "game" / "missing"
        with mock.patch.object(server.os, "open", side_effect=record_open), \
             mock.patch.object(server.os, "close", side_effect=record_close):
            result = server.delete_images(str(missing))
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(opened), sorted(closed))

    def test_job_and_delete_leases_exclude_each_other_without_serializing_jobs(self):
        self.assertTrue(server._acquire_image_job_lease())
        self.assertTrue(server._acquire_image_job_lease())
        try:
            result = server.delete_images(str(self.image_dir()))
            self.assertFalse(result["ok"])
            self.assertIn("job", result["errors"][0])
            self.assertEqual(result["deleted"], 0)
        finally:
            server._release_image_job_lease()
            server._release_image_job_lease()
        self.assertEqual(server._IMAGE_JOB_USERS, 0)

        self.assertTrue(server._IMAGE_DELETE_LOCK.acquire(False))
        try:
            self.assertFalse(server._acquire_image_job_lease())
        finally:
            server._IMAGE_DELETE_LOCK.release()

    def test_http_and_native_success_bodies_have_parity(self):
        native_dir = self.image_dir()
        native = server.delete_images(str(native_dir))
        http_dir = self.fixture.scm / "game" / "back"
        (http_dir / "one.bin").write_bytes(PNG)
        (http_dir / "README.md").write_text("keep", encoding="utf-8")
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/fs" % httpd.server_address[1],
                data=json.dumps({"op": "delete_images", "path": str(http_dir)}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:
                http = json.loads(response.read())
            self.assertEqual(http["ok"], native["ok"])
            self.assertEqual(http["deleted"], 1)
            self.assertEqual(http["names"], ["one.bin"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def test_http_ipc_mode_rejects_before_path_work(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        old_mode = server._IPC_MODE
        server._IPC_MODE = True
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/fs" % httpd.server_address[1],
                data=json.dumps({"op": "delete_images", "path": str(self.image_dir())}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with mock.patch.object(server, "_delete_images_target", side_effect=AssertionError("path work")):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(req, timeout=5)
            self.assertEqual(raised.exception.code, 403)
        finally:
            server._IPC_MODE = old_mode
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
