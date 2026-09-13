"""Contracts for card-back import and create-PDF back-image safety."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server
try:
    from test_phase0_baseline import JPEG, PNG, Phase0Fixture
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).parent))
    from test_phase0_baseline import JPEG, PNG, Phase0Fixture


JP2 = b"\x00\x00\x00\x0cjP  \r\n\x87\n\x00\x00\x00\x14ftypjp2 "


class BackImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-back-")
        self.root = Path(self.temp.name)
        self.fixture = Phase0Fixture(self.root)
        self.old = {name: getattr(server, name) for name in (
            "DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
            "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE", "_IPC_MODE",
            "_IMAGE_JOB_USERS", "_IMAGE_DELETE_LOCK",
        )}
        self.data = self.root / "data"
        self.data.mkdir()
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = self.data / "offsets.json"
        server.UPDATE_STATE_FILE = self.data / "updates.json"
        server._IPC_MODE = False
        server._IMAGE_JOB_USERS = 0
        server._IMAGE_DELETE_LOCK = __import__("threading").Lock()
        self.settings = {**json.loads(json.dumps(server.DEFAULT_SETTINGS)),
                          "scm_dir": str(self.fixture.scm),
                          "extras_dir": str(self.fixture.extras)}
        server.save_settings(self.settings)
        server.MANIFEST_CACHE.clear()
        server._INFO_SNAP.clear()
        server._REPOS_MTIME.clear()

    def tearDown(self):
        for name, value in self.old.items():
            setattr(server, name, value)
        self.temp.cleanup()

    def test_private_import_replaces_only_recognized_images_and_preserves_users(self):
        back = self.fixture.scm / "game" / "back"
        (back / "old.png").write_bytes(PNG)
        (back / "notes.txt").write_text("keep", encoding="utf-8")
        source = self.root / "selected-without-extension"
        source.write_bytes(JPEG)
        response = ipc.dispatch({"id": "back", "method": "back_images.import_selected",
                                 "params": {"source_path": str(source)}})
        self.assertTrue(response["ok"], response)
        result = response["result"]
        self.assertEqual(result["name"], source.name)
        self.assertEqual([item["name"] for item in result["back_images"]], [source.name])
        self.assertTrue((back / "notes.txt").exists())
        self.assertTrue((back / "EMPTY.md").exists())
        self.assertFalse((back / "old.png").exists())

    def test_remove_flow_deletes_recognized_backs_only(self):
        back = self.fixture.scm / "game" / "back"
        (back / "one.png").write_bytes(PNG)
        (back / "two.jpg").write_bytes(JPEG)
        (back / "notes.txt").write_text("keep", encoding="utf-8")
        result = server.delete_images("game/back", self.settings)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["names"], ["one.png", "two.jpg"])
        self.assertTrue((back / "EMPTY.md").exists())
        self.assertEqual((back / "notes.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(server._scan_back_images(self.fixture.scm), [])

    def test_source_bounds_magic_bytes_and_final_symlink_are_rejected(self):
        source = self.root / "not-an-image"
        source.write_bytes(b"plain text")
        with self.assertRaises(server.BackImageImportError):
            server.import_back_image(str(source), self.settings)
        oversized = self.root / "huge.png"
        with mock.patch.object(server, "BACK_IMAGE_SOURCE_MAX_BYTES", 2):
            oversized.write_bytes(PNG)
            with self.assertRaises(server.BackImageImportError):
                server.import_back_image(str(oversized), self.settings)
        link = self.root / "selected.png"
        try:
            link.symlink_to(self.fixture.scm / "game" / "front" / "card.png")
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaises(server.BackImageImportError):
            server.import_back_image(str(link), self.settings)

    def test_busy_image_fence_returns_a_specific_bounded_error(self):
        source = self.root / "back.png"
        source.write_bytes(PNG)
        server._IMAGE_DELETE_LOCK.acquire()
        try:
            with self.assertRaisesRegex(server.BackImageImportError, "another image operation is busy"):
                server.import_back_image(str(source), self.settings)
        finally:
            server._IMAGE_DELETE_LOCK.release()

    def test_unrelated_collision_gets_a_suffix_without_overwrite(self):
        back = self.fixture.scm / "game" / "back"
        unrelated = back / "picked.png"
        unrelated.write_text("keep", encoding="utf-8")
        source = self.root / "picked.png"
        source.write_bytes(PNG)
        result = server.import_back_image(str(source), self.settings)
        self.assertEqual(result["name"], "picked (2).png")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertTrue((back / "picked (2).png").exists())

    def test_windows_transaction_uses_collision_suffix_for_publication(self):
        back = self.fixture.scm / "game" / "back"
        unrelated = back / "picked.png"
        unrelated.write_text("keep", encoding="utf-8")
        source = self.root / "picked.png"
        source.write_bytes(PNG)
        source_fd = os.open(source, os.O_RDONLY)
        try:
            source_stat = os.fstat(source_fd)
            def stable_open(path):
                fd = os.open(path, os.O_RDONLY)
                return fd, os.fstat(fd)
            with mock.patch.object(server, "_open_windows_regular_file", side_effect=stable_open):
                result = server._import_back_image_windows(
                    source_fd, source_stat, source.name, back)
        finally:
            os.close(source_fd)
        self.assertEqual(result["name"], "picked (2).png")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertTrue((back / "picked (2).png").exists())

    def test_post_move_identity_change_restores_all_old_names(self):
        back = self.fixture.scm / "game" / "back"
        old = back / "old.png"
        old.write_bytes(PNG)
        source = self.root / "new.jpg"
        source.write_bytes(JPEG)
        real_identity = server._back_image_content_identity
        calls = 0

        def changed_after_move(value):
            nonlocal calls
            calls += 1
            return ("changed",) if calls == 2 else real_identity(value)

        with mock.patch.object(server, "_back_image_content_identity", side_effect=changed_after_move):
            with self.assertRaises(server.BackImageImportError):
                server.import_back_image(str(source), self.settings)
        self.assertEqual(old.read_bytes(), PNG)
        self.assertFalse((back / "new.jpg").exists())
        self.assertFalse(any(p.name.startswith(server.BACK_IMAGE_QUARANTINE_PREFIX) for p in back.iterdir()))

    def test_publish_never_depends_on_unlinking_a_duplicate_top_level_temp(self):
        back = self.fixture.scm / "game" / "back"
        (back / "old.png").write_bytes(PNG)
        source = self.root / "new.jpg"
        source.write_bytes(JPEG)
        real_unlink = server.os.unlink

        def reject_top_level_temp(name, *args, **kwargs):
            if str(name).startswith(server.BACK_IMAGE_TEMP_PREFIX):
                raise OSError("top-level temp cleanup unavailable")
            return real_unlink(name, *args, **kwargs)

        with mock.patch.object(server.os, "unlink", side_effect=reject_top_level_temp):
            result = server.import_back_image(str(source), self.settings)
        self.assertTrue(result["ok"])
        self.assertEqual([item["name"] for item in server._scan_back_images(self.fixture.scm)], ["new.jpg"])
        self.assertFalse(any(p.name.startswith(server.BACK_IMAGE_TEMP_PREFIX) for p in back.iterdir()))

    def test_cleanup_failure_keeps_new_top_level_image_and_hidden_backup(self):
        back = self.fixture.scm / "game" / "back"
        old = back / "old.png"
        old.write_bytes(PNG)
        source = self.root / "new.jpg"
        source.write_bytes(JPEG)
        real_unlink = server.os.unlink

        def fail_quarantine_cleanup(name, *args, **kwargs):
            if name == "old.png" and kwargs.get("dir_fd") is not None:
                raise OSError("fixture cleanup failure")
            return real_unlink(name, *args, **kwargs)

        with mock.patch.object(server.os, "unlink", side_effect=fail_quarantine_cleanup):
            result = server.import_back_image(str(source), self.settings)
        self.assertTrue(result["ok"])
        self.assertTrue((back / "new.jpg").exists())
        hidden = [p for p in back.iterdir() if p.is_dir() and p.name.startswith(server.BACK_IMAGE_QUARANTINE_PREFIX)]
        self.assertEqual(len(hidden), 1)
        self.assertTrue((hidden[0] / "old.png").exists())

    def test_failed_rollback_keeps_the_original_and_reports_it(self):
        # The last leg of a failure path can itself fail. The user's original
        # image must never be deleted for that; it stays in the hidden
        # quarantine and the worker says so instead of failing silently.
        import io
        back = self.fixture.scm / "game" / "back"
        old = back / "old.png"
        old.write_bytes(PNG)
        source = self.root / "new.jpg"
        source.write_bytes(JPEG)
        real = server._rename_delete_candidate
        state = {"publish_seen": False}

        def flaky(directory_fd, source_name, destination_name, **kwargs):
            if source_name.startswith(server.BACK_IMAGE_TEMP_PREFIX):
                state["publish_seen"] = True
                raise OSError("fixture publish failure")
            if state["publish_seen"]:
                raise OSError("fixture rollback failure")
            return real(directory_fd, source_name, destination_name, **kwargs)

        stderr = io.StringIO()
        with mock.patch.object(server, "_rename_delete_candidate", side_effect=flaky), \
                mock.patch.object(server.sys, "stderr", stderr):
            with self.assertRaises(server.BackImageImportError):
                server.import_back_image(str(source), self.settings)
        self.assertIn("quarantined", stderr.getvalue())
        self.assertFalse((back / "new.jpg").exists())
        stranded = [p for root_, _dirs, files in os.walk(back)
                    for p in (Path(root_) / f for f in files) if p.name == "old.png"]
        self.assertEqual(len(stranded), 1, "the original image was lost")
        self.assertEqual(stranded[0].read_bytes(), PNG)

    def test_publish_failure_rolls_back_old_image_and_temp_files(self):
        back = self.fixture.scm / "game" / "back"
        old = back / "old.png"
        old.write_bytes(PNG)
        source = self.root / "new.jpg"
        source.write_bytes(JPEG)
        real_rename = server._rename_delete_candidate

        def fail_publish(directory_fd, source_name, destination_name, **kwargs):
            if source_name.startswith(server.BACK_IMAGE_TEMP_PREFIX):
                raise OSError("fixture publish failure")
            return real_rename(directory_fd, source_name, destination_name, **kwargs)

        with mock.patch.object(server, "_rename_delete_candidate", side_effect=fail_publish):
            with self.assertRaises(server.BackImageImportError):
                server.import_back_image(str(source), self.settings)
        self.assertEqual(old.read_bytes(), PNG)
        self.assertFalse(any(p.name.startswith(server.BACK_IMAGE_TEMP_PREFIX) for p in back.iterdir()))
        self.assertFalse(any(p.name.startswith(server.BACK_IMAGE_QUARANTINE_PREFIX) for p in back.iterdir()))

    def test_browser_route_matches_import_result_and_packaged_http_fails_closed(self):
        source = self.root / "browser.png"
        source.write_bytes(PNG)
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/back-images/import"

        def post():
            request = urllib.request.Request(url, data=json.dumps({"path": str(source)}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        try:
            status, body = post()
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["name"], source.name)
            server._IPC_MODE = True
            status, body = post()
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])
            self.assertIn("native picker required", body["errors"][0])
        finally:
            server._IPC_MODE = False
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def build_pdf(self, back_dir, *, only_fronts=False):
        args = {"front_dir": "game/front", "back_dir": str(back_dir), "card_size": "standard",
                "paper_size": "letter", "output_path": "game/output/game.pdf",
                "only_fronts": only_fronts}
        return server.build_command(
            "create_pdf", args, self.settings, server.get_info(), write_deck=False)[-1]

    def test_create_pdf_preview_rejects_default_and_custom_multiple_back_images(self):
        back = self.fixture.scm / "game" / "back"
        (back / "one.png").write_bytes(PNG)
        (back / "two.jpg").write_bytes(JPEG)
        errors = self.build_pdf("game/back")
        self.assertTrue(any("contains 2" in error for error in errors), errors)
        self.assertEqual(self.build_pdf("game/back", only_fronts=True), [])
        custom = self.root / "custom-backs"
        custom.mkdir()
        (custom / "one.jp2").write_bytes(JP2)
        (custom / "two.jp2").write_bytes(JP2)
        errors = self.build_pdf(custom)
        self.assertTrue(any("contains 2" in error for error in errors), errors)

    def test_create_pdf_preview_counts_a_linked_back_like_upstream(self):
        # Upstream resolves symlinks before counting, so one linked image is a
        # working setup and must not be refused. Two images still are.
        back = self.fixture.scm / "game" / "back"
        target = self.root / "linked.png"
        target.write_bytes(PNG)
        link = back / "linked.png"
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        self.assertEqual([item["name"] for item in server._scan_back_images(self.fixture.scm)], ["linked.png"])
        self.assertEqual(self.build_pdf("game/back"), [])
        (back / "two.jpg").write_bytes(JPEG)
        errors = self.build_pdf("game/back")
        self.assertTrue(any("contains 2" in error for error in errors), errors)

    def test_create_pdf_preview_rejects_a_truncated_back_scan(self):
        back = self.fixture.scm / "game" / "back"
        (back / "one.png").write_bytes(PNG)
        (back / "two.txt").write_text("two")
        with mock.patch.object(server, "BACK_IMAGE_SCAN_MAX_SCANNED", 1):
            errors = self.build_pdf("game/back")
        self.assertTrue(any("could not be safely checked" in error for error in errors), errors)

    def test_import_scan_allows_exact_entry_limit_and_rejects_one_more(self):
        back = self.fixture.scm / "game" / "back"
        for child in back.iterdir():
            child.unlink()
        (back / "one.txt").write_text("one")
        (back / "two.txt").write_text("two")
        source = self.root / "new.png"
        source.write_bytes(PNG)
        with mock.patch.object(server, "BACK_IMAGE_SCAN_MAX_SCANNED", 2):
            self.assertTrue(server.import_back_image(str(source), self.settings)["ok"])
        (back / "three.txt").write_text("three")
        with mock.patch.object(server, "BACK_IMAGE_SCAN_MAX_SCANNED", 2):
            with self.assertRaisesRegex(server.BackImageImportError, "directory is too large"):
                server.import_back_image(str(source), self.settings)


if __name__ == "__main__":
    unittest.main()
