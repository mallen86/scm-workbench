"""Dedicated contracts for the native decklist-import migration.

The tests intentionally exercise the worker's file boundary rather than the
sister repository implementation.  Every fixture is temporary and source bytes
are treated as opaque data.
"""

import contextlib
import json
import os
import shutil
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


class DecklistImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-decklists-")
        cls.fixture = Phase0Fixture(Path(cls.temp.name))
        cls.old_globals = {
            name: getattr(server, name) for name in (
                "DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
                "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE", "_IPC_MODE",
            )
        }
        cls.old_env = os.environ.get("SCM_WORKBENCH_DATA")
        os.environ["SCM_WORKBENCH_DATA"] = str(cls.fixture.data)
        server.DATA_DIR = cls.fixture.data
        server.SETTINGS_FILE = cls.fixture.data / "settings.json"
        server.JOBS_FILE = cls.fixture.data / "jobs.json"
        server.LOGS_DIR = cls.fixture.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = cls.fixture.data / "offsets.json"
        server.UPDATE_STATE_FILE = cls.fixture.data / "updates.json"
        server._IPC_MODE = False
        cls._save_settings()
        cls.httpd = server.start_http("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def _save_settings(cls):
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({"scm_dir": str(cls.fixture.scm), "extras_dir": str(cls.fixture.extras)})
        server.save_settings(settings)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        for name, value in cls.old_globals.items():
            setattr(server, name, value)
        if cls.old_env is None:
            os.environ.pop("SCM_WORKBENCH_DATA", None)
        else:
            os.environ["SCM_WORKBENCH_DATA"] = cls.old_env
        cls.temp.cleanup()

    def setUp(self):
        directory = self.deckdir
        if os.path.lexists(directory):
            if directory.is_symlink():
                directory.unlink()
            else:
                shutil.rmtree(directory)
        directory.mkdir(parents=True)
        self._save_settings()
        server.invalidate_manifest_cache()
        server._IPC_MODE = False

    @property
    def deckdir(self):
        return self.fixture.scm / "game" / "decklist"

    @property
    def outside(self):
        return self.fixture.outside

    def source(self, name="deck.txt", content=b"one\r\ntwo\nthree\x00\xff"):
        path = self.outside / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    @staticmethod
    def native(path, request_id="deck"):
        return ipc.dispatch({
            "id": request_id,
            "method": "decklists.import_selected",
            "params": {"source_path": str(path)},
        })

    def http(self, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def assert_no_temp_files(self):
        self.assertEqual(list(self.deckdir.glob(server.DECKLIST_TEMP_PREFIX + "*")), [])

    def test_dispatch_is_public_allowlisted_method_with_exact_params_and_path_bound(self):
        path = self.source()
        with mock.patch.object(server, "import_decklist", return_value={"ok": True}) as imported:
            result = self.native(path, "exact")
        self.assertEqual(result, {"id": "exact", "ok": True, "result": {"ok": True}})
        imported.assert_called_once_with(str(path))
        self.assertNotIn("decklists.import_selected", {"info", "manifest"})
        for params in ({}, {"source_path": str(path), "extra": 1}, {"source_path": 4}):
            response = ipc.dispatch({"id": "bad", "method": "decklists.import_selected", "params": params})
            self.assertEqual(response["error"]["code"], "bad_request")
        empty = ipc.dispatch({"id": "empty", "method": "decklists.import_selected",
                              "params": {"source_path": ""}})
        self.assertFalse(empty["result"]["ok"])
        exact_path = "x" * server.DECKLIST_PATH_MAX_BYTES
        response = ipc.dispatch({"id": "exact-path", "method": "decklists.import_selected",
                                 "params": {"source_path": exact_path}})
        self.assertIn("result", response)
        self.assertFalse(response["result"]["ok"])
        over = "x" * (server.DECKLIST_PATH_MAX_BYTES + 1)
        response = ipc.dispatch({"id": "long", "method": "decklists.import_selected",
                                 "params": {"source_path": over}})
        self.assertEqual(response["error"]["code"], "bad_request")
        response = ipc.dispatch({"id": "control", "method": "decklists.import_selected",
                                 "params": {"source_path": str(self.outside / "bad\x01.txt")}})
        self.assertFalse(response["result"]["ok"])
        response = ipc.dispatch({"id": "utf8", "method": "decklists.import_selected",
                                 "params": {"source_path": "\udcff"}})
        self.assertEqual(response["error"]["code"], "bad_request")

    def test_http_and_native_success_and_failure_bodies_are_identical(self):
        path = self.source("same.txt", b"opaque\ncontent")
        http_status, http_result = self.http("/api/decklists/import", {"path": str(path)})
        self.assertEqual(http_status, 200)
        (self.deckdir / http_result["name"]).unlink()
        native = self.native(path, "parity")
        self.assertEqual(native["result"], http_result)
        self.assertEqual(native["id"], "parity")

        missing = self.outside / "missing.txt"
        http_status, http_result = self.http("/api/decklists/import", {"path": str(missing)})
        self.assertEqual(http_status, 400)
        native = self.native(missing, "missing")
        self.assertEqual(native["result"], http_result)
        self.assertEqual(native["result"]["ok"], False)

    def test_ipc_mode_http_route_requires_native_picker(self):
        server._IPC_MODE = True
        status, result = self.http("/api/decklists/import", {"path": str(self.source())})
        self.assertEqual(status, 400)
        self.assertEqual(result, {"ok": False, "errors": [
            "native picker required for packaged decklist import"]})

    def test_missing_directory_and_final_symlink_sources_fail_closed(self):
        self.assertFalse(self.native(self.outside / "not-there.txt")["result"]["ok"])
        source_dir = self.outside / "directory.txt"
        source_dir.mkdir()
        with self.assertRaises(server.DecklistImportError):
            server.import_decklist(str(source_dir))
        link = self.outside / "selected.txt"
        try:
            link.symlink_to(self.source("real.txt"))
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable: %s" % error)
        result = self.native(link)["result"]
        self.assertFalse(result["ok"])
        self.assertIn("selected", result["errors"][0])

    def test_multiline_opaque_bytes_are_copied_exactly(self):
        content = b"first\r\nsecond\n\x00\xff\x80\tlast\r\n"
        source = self.source("opaque.txt", content)
        result = server.import_decklist(str(source))
        self.assertTrue(result["ok"])
        self.assertEqual((self.deckdir / result["name"]).read_bytes(), content)
        self.assertEqual((self.deckdir / result["name"]).stat().st_size, len(content))
        self.assert_no_temp_files()

    def test_source_size_boundary_is_inclusive_and_over_limit_is_rejected(self):
        exact = self.source("eight.bin", b"x" * server.DECKLIST_SOURCE_MAX_BYTES)
        result = server.import_decklist(str(exact))
        self.assertTrue(result["ok"])
        self.assertEqual((self.deckdir / result["name"]).stat().st_size,
                         server.DECKLIST_SOURCE_MAX_BYTES)
        over = self.source("too-large.bin", b"x" * (server.DECKLIST_SOURCE_MAX_BYTES + 1))
        result = self.native(over)["result"]
        self.assertFalse(result["ok"])
        self.assertIn("8 MiB", result["errors"][0])
        self.assert_no_temp_files()

    def test_source_shrink_and_growth_are_detected_using_open_source_seam(self):
        source = self.source("changing.txt", b"abcdef")
        original = server._open_decklist_source

        def changed_stat(size):
            fd, source_stat, name = original(str(source))
            values = list(source_stat)
            values[6] = size
            return fd, os.stat_result(values), name

        with mock.patch.object(server, "_open_decklist_source",
                               side_effect=lambda _: changed_stat(7)):
            shrinking = self.native(source, "shrinking")["result"]
        self.assertFalse(shrinking["ok"])
        self.assertIn("changed while reading", shrinking["errors"][0])
        self.assert_no_temp_files()

        with mock.patch.object(server, "_open_decklist_source",
                               side_effect=lambda _: changed_stat(5)):
            growing = self.native(source, "growing")["result"]
        self.assertFalse(growing["ok"])
        self.assertIn("grew while reading", growing["errors"][0])
        self.assert_no_temp_files()

    def test_source_name_utf8_controls_reserved_names_and_limits(self):
        names = ["bad\nname.txt", "CON", "CON.txt", "README.md",
                 server.DECKLIST_TEMP_PREFIX + "chosen.tmp", "a/b.txt"]
        for index, name in enumerate(names):
            with self.assertRaises(server.DecklistImportError):
                server._decklist_name(name)
            # Windows itself refuses control characters in file names. Exercise
            # the source-handle boundary as well for every name the host can
            # represent; the pure validator above covers the rest.
            if "/" not in name and not any(ord(c) < 0x20 for c in name):
                path = self.source(name, b"x")
                result = self.native(path, "name-%d" % index)["result"]
                self.assertFalse(result["ok"], name)
        accepted = "é" * 127 + "a"  # exactly 255 UTF-8 bytes
        self.assertEqual(len(accepted.encode("utf-8")), server.DECKLIST_NAME_MAX_BYTES)
        self.assertEqual(server._decklist_name(accepted), accepted)
        with self.assertRaises(server.DecklistImportError):
            server._decklist_name(accepted + "b")
        with self.assertRaises(server.DecklistImportError):
            server._decklist_name("\udcff")
        with self.assertRaises(server.DecklistImportError):
            server._decklist_name("trailing.")
        with self.assertRaises(server.DecklistImportError):
            server._decklist_name("trailing ")
        with self.assertRaises(server.DecklistImportError):
            server._decklist_name("A:B.txt")

    def test_destination_game_and_decklist_symlink_components_are_rejected(self):
        outside = self.outside / "destination"
        outside.mkdir()
        game = self.fixture.scm / "game"
        real_game = self.fixture.scm / "real-game"
        game.rename(real_game)
        try:
            game.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            real_game.rename(game)
            self.skipTest("symlinks unavailable: %s" % error)
        try:
            result = self.native(self.source("game-link.txt"))["result"]
            self.assertFalse(result["ok"])
            game.unlink()
        finally:
            if os.path.lexists(game):
                game.unlink()
            real_game.rename(game)

        deck = self.deckdir
        deck.rmdir()
        try:
            deck.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            deck.mkdir()
            self.skipTest("symlinks unavailable: %s" % error)
        result = self.native(self.source("deck-link.txt"))["result"]
        self.assertFalse(result["ok"])

    @unittest.skipIf(os.name == "nt", "dirfd scanning is POSIX-only")
    def test_info_scan_stays_on_open_directory_if_parent_is_swapped(self):
        (self.deckdir / "safe.deck").write_bytes(b"safe")
        outside_game = self.outside / "outside-game"
        outside_decklist = outside_game / "decklist"
        outside_decklist.mkdir(parents=True)
        (outside_decklist / "secret.deck").write_bytes(b"secret")
        game = self.fixture.scm / "game"
        parked = self.fixture.scm / "game-parked"
        original_open = server._open_posix_decklist_directory
        swapped = False

        def open_then_swap(scm, *, create=True):
            nonlocal swapped
            result = original_open(scm, create=create)
            if not swapped and not create:
                game.rename(parked)
                game.symlink_to(outside_game, target_is_directory=True)
                swapped = True
            return result

        try:
            with mock.patch.object(server, "_open_posix_decklist_directory",
                                   side_effect=open_then_swap):
                info = server.read_scm_info(self.fixture.scm, self.fixture.extras)
            self.assertEqual([entry["name"] for entry in info["decklists"]], ["safe.deck"])
        finally:
            if swapped:
                game.unlink()
                parked.rename(game)

    def test_existing_target_symlink_is_not_overwritten(self):
        source = self.source("target.txt", b"new")
        outside = self.outside / "target-original.txt"
        outside.write_bytes(b"old")
        target = self.deckdir / "target.txt"
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable: %s" % error)
        result = server.import_decklist(str(source))
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "target (2).txt")
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_bytes(), b"old")
        self.assertEqual((self.deckdir / result["name"]).read_bytes(), b"new")

    def test_all_collision_slots_are_bounded_and_leave_no_temp(self):
        source = self.source("collision.txt", b"new")
        (self.deckdir / "collision.txt").write_bytes(b"original")
        for suffix in range(2, 1001):
            (self.deckdir / f"collision ({suffix}).txt").write_bytes(b"occupied")
        result = self.native(source, "collision")["result"]
        self.assertFalse(result["ok"])
        self.assertIn("collisions", result["errors"][0])
        self.assertEqual((self.deckdir / "collision.txt").read_bytes(), b"original")
        self.assert_no_temp_files()

    def test_atomic_copy_cleans_temp_on_write_and_publish_failure(self):
        source = self.source("atomic.txt", b"atomic bytes")
        real_write = os.write
        with mock.patch.object(server.os, "write", side_effect=OSError("write failed")):
            result = self.native(source, "write-failure")["result"]
        self.assertFalse(result["ok"])
        self.assertFalse((self.deckdir / "atomic.txt").exists())
        self.assert_no_temp_files()

        existing = self.deckdir / "atomic.txt"
        existing.write_bytes(b"do not replace")
        real_link = os.link
        with mock.patch.object(server.os, "link", side_effect=OSError("publish failed")):
            result = self.native(source, "publish-failure")["result"]
        self.assertFalse(result["ok"])
        self.assertEqual(existing.read_bytes(), b"do not replace")
        self.assert_no_temp_files()
        self.assertIsNotNone(real_write)
        self.assertIsNotNone(real_link)

    def test_external_publish_collision_chooses_fresh_name_without_overwrite(self):
        source = self.source("race.txt", b"new")
        target = self.deckdir / "race.txt"
        calls = []
        real_link = os.link

        def link_once(src, dst, *args, **kwargs):
            calls.append(Path(dst).name)
            if len(calls) == 1:
                target.write_bytes(b"external winner")
                raise FileExistsError(dst)
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(server.os, "link", side_effect=link_once):
            result = server.import_decklist(str(source))
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "race (2).txt")
        self.assertEqual(target.read_bytes(), b"external winner")
        self.assertEqual((self.deckdir / "race (2).txt").read_bytes(), b"new")
        self.assert_no_temp_files()

    def test_concurrent_imports_are_unique_and_preserve_both_payloads(self):
        first = self.source("first/parallel.txt", b"first")
        second = self.source("second/parallel.txt", b"second")
        results = []
        errors = []

        def run(path):
            try:
                results.append(server.import_decklist(str(path)))
            except Exception as error:  # assertion below reports any worker failure
                errors.append(error)

        threads = [threading.Thread(target=run, args=(first,)), threading.Thread(target=run, args=(second,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        names = {result["name"] for result in results}
        self.assertEqual(names, {"parallel.txt", "parallel (2).txt"})
        self.assertEqual({(self.deckdir / name).read_bytes() for name in names}, {b"first", b"second"})
        self.assert_no_temp_files()

    def test_lock_contention_seam_fails_without_waiting(self):
        @contextlib.contextmanager
        def busy_lock():
            raise server.DecklistImportError("repository is busy")
            yield  # pragma: no cover

        started = time.monotonic()
        with mock.patch.object(server, "_decklist_lock", return_value=busy_lock()):
            result = self.native(self.source("busy.txt"), "busy")["result"]
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(result, {"ok": False, "errors": ["repository is busy"]})
        self.assert_no_temp_files()

    def test_scan_counts_items_and_result_budget_are_explicit(self):
        for index in range(8):
            (self.deckdir / f"{index}.txt").write_text("x", encoding="utf-8")
        (self.deckdir / "README.md").write_text("placeholder", encoding="utf-8")
        (self.deckdir / "folder").mkdir()
        outside = self.outside / "escaped.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            (self.deckdir / "escaped.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        with mock.patch.object(server, "DECKLIST_SCAN_MAX_SCANNED", 5), \
             mock.patch.object(server, "DECKLIST_SCAN_MAX_ITEMS", 2), \
             mock.patch.object(server, "DECKLIST_SCAN_MAX_RESULT_BYTES", 160):
            listing = server._decklist_scan(self.deckdir)
        self.assertEqual(set(listing), {"items", "scanned", "found", "truncated"})
        self.assertLessEqual(len(listing["items"]), 2)
        self.assertLessEqual(listing["scanned"], 5)
        self.assertGreaterEqual(listing["found"], len(listing["items"]))
        self.assertTrue(listing["truncated"])
        encoded = json.dumps(listing, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), 160)

    @unittest.skipIf(os.name == "nt", "byte filenames are POSIX-only")
    def test_scan_is_sorted_and_invalid_utf8_or_scan_limited_results_fail_closed(self):
        (self.deckdir / "z.txt").write_bytes(b"z")
        (self.deckdir / "a.txt").write_bytes(b"a")
        (self.deckdir / "m.txt").write_bytes(b"m")
        raw_bad = os.fsencode(self.deckdir) + b"/bad-\xff.txt"
        bad_name_created = False
        try:
            bad_fd = os.open(raw_bad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(bad_fd)
            bad_name_created = True
        except OSError:
            pass  # macOS filesystems reject non-UTF-8 names before the scanner sees them
        listing = server._decklist_scan(self.deckdir)
        self.assertEqual([item["name"] for item in listing["items"]], ["a.txt", "m.txt", "z.txt"])
        self.assertEqual(listing["truncated"], bad_name_created)

        with mock.patch.object(server, "DECKLIST_SCAN_MAX_SCANNED", 2):
            limited = server._decklist_scan(self.deckdir)
        self.assertEqual(limited["items"], [])
        self.assertEqual(limited["scanned"], 2)
        self.assertTrue(limited["truncated"])

    def test_import_result_is_bounded_and_manifest_cache_is_invalidated(self):
        server.MANIFEST_CACHE.update({"stale": {"value": True}})
        server._INFO_SNAP.update({"stale": True})
        server._REPOS_MTIME.update({"t": 1})
        source = self.source("cached.txt", b"x")
        with mock.patch.object(server, "DECKLIST_SCAN_MAX_RESULT_BYTES", 180):
            result = server.import_decklist(str(source))
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), 180)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"] or len(result["decklists"]) <= 1)
        self.assertEqual(server.MANIFEST_CACHE, {})
        self.assertEqual(server._INFO_SNAP, {})
        self.assertEqual(server._REPOS_MTIME, {})


if __name__ == "__main__":
    unittest.main()
