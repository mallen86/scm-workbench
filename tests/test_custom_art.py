"""Copy-only custom card art and hostile filesystem regression coverage."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock

from scm_workbench import custom_art, server

PNG = b"\x89PNG\r\n\x1a\n" + b"test image"
JPEG = b"\xff\xd8\xff" + b"test image"


class CustomArtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scm = self.root / "scm"
        self.scm.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.patches = [mock.patch.object(server, "DATA_DIR", self.data),
                        mock.patch.object(server.repo_sync, "data_dir", return_value=self.data),
                        mock.patch.object(server, "_IMAGE_DELETE_LOCK", threading.Lock())]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.settings = {"scm_dir": str(self.scm)}

    def source(self, name, content=PNG):
        p = self.root / name
        p.write_bytes(content)
        return str(p)

    def test_successful_custom_import_use_persists_and_failure_does_not_mark(self):
        self.assertFalse(custom_art.custom_art_used(server))
        failed = custom_art.import_selected("front", [self.source("bad.png", b"not image")], server, self.settings)
        self.assertEqual(failed["imported"], 0)
        self.assertFalse(custom_art.custom_art_used(server))
        partial = custom_art.import_selected("front", [self.source("good.png"), self.source("also-bad.png", b"bad")], server, self.settings)
        self.assertEqual(partial["imported"], 1)
        self.assertTrue(custom_art.custom_art_used(server))
        persisted = json.loads((self.data / "custom-art-use.json").read_text("utf-8"))
        self.assertEqual(set(persisted), {"used_at"})
        self.assertGreater(persisted["used_at"], 0)
        self.assertTrue(custom_art.custom_art_used(server), "the durable flag survives a fresh info read")

    def test_custom_usage_marker_is_bounded_and_strict(self):
        path = self.data / "custom-art-use.json"
        for raw in (b"x" * 257, b'{"used_at":NaN}', b'{"used_at":Infinity}',
                    b'{"used_at":true}', b'{"used_at":-1}', b'{"used_at":1,"extra":1}', b'[]'):
            path.write_bytes(raw)
            self.assertFalse(custom_art.custom_art_used(server))
        path.write_bytes(b'{"used_at":1}')
        self.assertTrue(custom_art.custom_art_used(server))

    def test_fixed_destinations_copy_collision_no_replace_and_originals(self):
        src = self.source("one.png")
        folder = self.scm / "game" / "front"
        folder.mkdir(parents=True)
        (folder / "one.png").write_bytes(JPEG)
        result = custom_art.import_selected("front", [src, src], server, self.settings)
        self.assertEqual(result, {"ok": True, "destination": "front", "imported": 2,
                                  "names": ["one (2).png", "one (3).png"], "failed": []})
        self.assertEqual((folder / "one.png").read_bytes(), JPEG)
        self.assertEqual((folder / "one (2).png").read_bytes(), PNG)
        self.assertEqual(Path(src).read_bytes(), PNG)
        self.assertFalse((self.scm / "game" / "double_sided").exists())

    def test_full_length_unicode_name_can_receive_collision_suffix(self):
        long_name = "é" * 125 + ".png"
        src = self.source(long_name)
        folder = self.scm / "game" / "front"
        folder.mkdir(parents=True)
        (folder / long_name).write_bytes(JPEG)
        result = custom_art.import_selected("front", [src], server, self.settings)
        self.assertEqual(result["imported"], 1)
        self.assertTrue(result["names"][0].endswith(" (2).png"))
        self.assertLessEqual(len(result["names"][0].encode("utf-8")), 255)
        self.assertEqual((folder / long_name).read_bytes(), JPEG)

    def test_magic_name_symlink_and_partial_failures(self):
        good = self.source("good.png")
        wrong = self.source("fake.png", b"not an image")
        linked = self.root / "linked.png"
        linked.symlink_to(good)
        bad_extension = self.source("bad.txt")
        result = custom_art.import_selected("double_sided", [good, wrong, str(linked), bad_extension], server, self.settings)
        self.assertEqual(result["names"], ["good.png"])
        self.assertEqual([row["name"] for row in result["failed"]], ["fake.png", "linked.png", "bad.txt"])
        self.assertFalse(any(str(self.root) in row["error"] for row in result["failed"]))
        for bad in ("../escape.png", "CON.png", "name:stream.png", "bad\\name.png", "bad?.png", "bad|.png", "COM¹.png", "end. ", ".wb-custom-art-hi.png", "x\x00.png"):
            with self.subTest(bad=bad), self.assertRaises(custom_art.ImportError):
                custom_art.name(bad)
        self.assertEqual(custom_art.destination("back"), "back")
        with self.assertRaises(custom_art.ImportError):
            custom_art.destination("other")

    def test_back_replaces_via_existing_transaction_and_rejects_multi_before_mutation(self):
        old = self.scm / "game" / "back" / "old.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(JPEG)
        (old.parent / "placeholder.txt").write_text("keep")
        first = self.source("new.png")
        second = self.source("another.png")
        with self.assertRaisesRegex(custom_art.ImportError, "exactly one"):
            custom_art.import_selected("back", [first, second], server, self.settings)
        self.assertEqual(old.read_bytes(), JPEG)
        result = custom_art.import_selected("back", [first], server, self.settings)
        self.assertEqual(result, {"ok": True, "destination": "back", "imported": 1,
                                  "names": ["new.png"], "failed": []})
        self.assertFalse(old.exists())
        self.assertEqual((old.parent / "new.png").read_bytes(), PNG)
        self.assertEqual((old.parent / "placeholder.txt").read_text(), "keep")
        self.assertEqual(Path(first).read_bytes(), PNG)
        result = custom_art.import_bytes("back", "upload.png", io.BytesIO(JPEG), len(JPEG), server, self.settings)
        self.assertEqual(result["names"], ["upload.png"])
        self.assertFalse((old.parent / "new.png").exists())
        self.assertEqual((old.parent / "upload.png").read_bytes(), JPEG)
        self.assertEqual(len(server._scan_back_images(self.scm)), 1)

    @unittest.skipIf(os.name == "nt", "POSIX rename hook; Windows rollback is covered by card-back importer tests")
    def test_back_rollback_limits_and_untrusted_sources(self):
        old = self.scm / "game" / "back" / "old.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(JPEG)
        source = self.source("new.png")
        rename = server._rename_delete_candidate
        def fail_publication(source_fd, source_name, target_name, **kwargs):
            if source_name.startswith(server.BACK_IMAGE_TEMP_PREFIX):
                raise OSError("publication failed after quarantine")
            return rename(source_fd, source_name, target_name, **kwargs)
        with mock.patch.object(server, "_rename_delete_candidate", side_effect=fail_publication):
            result = custom_art.import_selected("back", [source], server, self.settings)
        self.assertFalse(result["ok"])
        self.assertEqual(old.read_bytes(), JPEG)
        self.assertFalse((old.parent / "new.png").exists())
        self.assertEqual([item["name"] for item in server._scan_back_images(self.scm)], ["old.png"])
        linked = self.root / "link.png"
        linked.symlink_to(source)
        self.assertFalse(custom_art.import_selected("back", [str(linked)], server, self.settings)["ok"])
        with self.assertRaisesRegex(custom_art.ImportError, "32 MiB"):
            custom_art.import_bytes("back", "big.png", io.BytesIO(PNG), server.BACK_IMAGE_SOURCE_MAX_BYTES + 1, server, self.settings)
        self.assertEqual(old.read_bytes(), JPEG)

    def test_destination_symlink_and_busy_job_fence(self):
        (self.scm / "game").symlink_to(self.root, target_is_directory=True)
        result = custom_art.import_selected("front", [self.source("ok.png")], server, self.settings)
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "front").exists())
        (self.scm / "game").unlink()
        with mock.patch.object(server, "_IMAGE_JOB_USERS", 1):
            result = custom_art.import_selected("front", [self.source("ok.png")], server, self.settings)
            self.assertFalse(result["ok"])
            self.assertFalse((self.scm / "game" / "front").exists())

    def test_raw_bytes_and_failed_source_read_preserve_successes(self):
        result = custom_art.import_bytes("front", "upload.png", io.BytesIO(PNG), len(PNG), server, self.settings)
        self.assertEqual(result["names"], ["upload.png"])
        with self.assertRaises(custom_art.ImportError):
            custom_art.import_bytes("front", "short.png", io.BytesIO(b"x"), 20, server, self.settings)
        self.assertFalse((self.scm / "game" / "front" / "short.png").exists())
        result = custom_art.import_selected("front", [self.source("two.png"), str(self.root / "missing.png")], server, self.settings)
        self.assertEqual(result["names"], ["two.png"])
        self.assertEqual(result["failed"][0]["name"], "missing.png")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "pipe.png"
        os.mkfifo(fifo)
        result = custom_art.import_selected("front", [str(fifo)], server, self.settings)
        self.assertEqual(result["names"], [])
        self.assertEqual(result["failed"][0]["name"], "pipe.png")

    def test_oversized_sparse_source_is_not_read(self):
        path = self.root / "huge.png"
        with path.open("wb") as file:
            file.write(PNG)
            file.truncate(custom_art.MAX_FILE + 1)
        result = custom_art.import_selected("front", [str(path)], server, self.settings)
        self.assertEqual(result["names"], [])
        self.assertEqual(result["failed"][0]["name"], "huge.png")
        self.assertFalse((self.scm / "game" / "front" / "huge.png").exists())
        with self.assertRaises(custom_art.ImportError):
            custom_art.import_bytes("front", "huge.png", io.BytesIO(PNG), custom_art.MAX_FILE + 1, server, self.settings)

    def test_later_copy_failure_keeps_first_publication(self):
        first = self.source("first.png")
        second = self.source("second.png")
        real_copy = custom_art._copy
        def fail_second(handle, observed, source_name, directory, dirfd, deadline, server_module):
            if source_name == "second.png":
                raise custom_art.ImportError("could not write image")
            return real_copy(handle, observed, source_name, directory, dirfd, deadline, server_module)
        with mock.patch.object(custom_art, "_copy", side_effect=fail_second):
            result = custom_art.import_selected("front", [first, second], server, self.settings)
        self.assertEqual(result["names"], ["first.png"])
        self.assertEqual(result["failed"], [{"name": "second.png", "error": "could not write image"}])
        self.assertEqual((self.scm / "game" / "front" / "first.png").read_bytes(), PNG)
        self.assertFalse((self.scm / "game" / "front" / "second.png").exists())

    def test_open_folder_creates_only_fixed_folder(self):
        with mock.patch.object(server, "reveal_path", return_value=None) as reveal:
            self.assertEqual(custom_art.open_folder("double_sided", server, self.settings), {"ok": True, "errors": []})
            reveal.assert_called_once_with(self.scm / "game" / "double_sided")
        self.assertTrue((self.scm / "game" / "double_sided").is_dir())
        self.assertFalse(custom_art.open_folder("other", server, self.settings)["ok"])

    def test_metadata_only_temporary_change_keeps_valid_import_and_collision_safety(self):
        source = self.source("metadata.png")
        for destination in ("front", "double_sided"):
            with self.subTest(destination=destination):
                folder = self.scm / "game" / destination
                folder.mkdir(parents=True)
                (folder / "metadata.png").write_bytes(JPEG)
                changed = []
                real_stat = custom_art._entry_stat
                def update_metadata(directory, dirfd, entry, module):
                    before = real_stat(directory, dirfd, entry, module)
                    if changed or not entry.startswith(custom_art.PREFIX):
                        return before
                    # Inject one metadata-only observation. Immediate chmods
                    # can share a ctime tick on Linux; waiting for the clock
                    # would make this regression timing-dependent again.
                    after = SimpleNamespace(**{field: getattr(before, field) for field in (
                        "st_mode", "st_dev", "st_ino", "st_size", "st_mtime", "st_mtime_ns",
                        "st_ctime", "st_ctime_ns")})
                    after.st_ctime += 1
                    after.st_ctime_ns += 1_000_000_000
                    self.assertEqual(custom_art._content_identity(before), custom_art._content_identity(after))
                    self.assertNotEqual(custom_art._identity(before), custom_art._identity(after))
                    changed.append(True)
                    return after
                with mock.patch.object(custom_art, "_entry_stat", side_effect=update_metadata), \
                        mock.patch.object(custom_art, "_verify_temporary_bytes", wraps=custom_art._verify_temporary_bytes) as verify_bytes:
                    if destination == "front":
                        result = custom_art.import_selected(destination, [source], server, self.settings)
                    else:
                        result = custom_art.import_bytes(destination, "metadata.png", io.BytesIO(PNG), len(PNG), server, self.settings)
                self.assertEqual(changed, [True])
                self.assertGreaterEqual(verify_bytes.call_count, 2, "verify both metadata recovery and publication")
                self.assertEqual(result["names"], ["metadata (2).png"], result)
                self.assertEqual(result["failed"], [])
                self.assertEqual((folder / "metadata.png").read_bytes(), JPEG)
                self.assertEqual((folder / "metadata (2).png").read_bytes(), PNG)
                self.assertFalse(list(folder.glob(custom_art.PREFIX + "*")))
        self.assertEqual(Path(source).read_bytes(), PNG)

    def test_temporary_byte_verification_is_bounded_and_detects_content_changes(self):
        data = PNG + b"x" * custom_art.CHUNK
        with tempfile.TemporaryFile(mode="w+b") as temporary:
            temporary.write(data)
            temporary.flush()
            digest = hashlib.sha256(data).digest()
            with mock.patch.object(custom_art.os, "read", wraps=os.read) as reads:
                custom_art._verify_temporary_bytes(temporary.fileno(), len(data), digest, time.monotonic() + 10)
            self.assertTrue(all(0 < call.args[1] <= custom_art.CHUNK for call in reads.call_args_list))
            for size, expected in ((len(data) + 1, digest), (len(data) - 1, digest), (len(data), b"wrong digest")):
                with self.subTest(size=size), self.assertRaisesRegex(custom_art.ImportError, "temporary image changed"):
                    custom_art._verify_temporary_bytes(temporary.fileno(), size, expected, time.monotonic() + 10)
            with mock.patch.object(custom_art.time, "monotonic", side_effect=[0, 11]), \
                    self.assertRaisesRegex(custom_art.ImportError, "timed out"):
                custom_art._verify_temporary_bytes(temporary.fileno(), len(data), digest, 10)
            self.assertEqual(os.lseek(temporary.fileno(), 0, os.SEEK_CUR), custom_art.CHUNK)

    @unittest.skipIf(os.name == "nt", "POSIX ctime tracks edits with restored mtime")
    def test_temporary_same_size_edit_with_restored_mtime_is_rejected(self):
        source = self.source("tampered.png")
        real_verify = custom_art._verify_destination
        changed = []
        def tamper(directory, dirfd, module):
            real_verify(directory, dirfd, module)
            if changed:
                return
            temporary = next(directory.glob(custom_art.PREFIX + "*"))
            before = temporary.stat()
            temporary.write_bytes(PNG[:-1] + b"X")
            os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(custom_art._content_identity(before), custom_art._content_identity(temporary.stat()))
            changed.append(True)
        with mock.patch.object(custom_art, "_verify_destination", side_effect=tamper):
            result = custom_art.import_selected("front", [source], server, self.settings)
        self.assertEqual(result["imported"], 0)
        self.assertIn("temporary image changed", result["failed"][0]["error"])
        self.assertEqual(list((self.scm / "game" / "front").iterdir()), [])
        self.assertEqual(Path(source).read_bytes(), PNG)

    def test_in_place_content_edit_at_publication_is_rejected(self):
        source = self.source("publication.png")
        folder = self.scm / "game" / "front"
        real_link = os.link
        for when in ("before_link", "after_link"):
            with self.subTest(when=when):
                def changed_link(temp, destination, **kwargs):
                    temporary = folder / temp if isinstance(temp, str) else temp
                    before = temporary.stat()
                    if when == "after_link":
                        real_link(temp, destination, **kwargs)
                    temporary.write_bytes(PNG[:-1] + b"X")
                    os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
                    self.assertEqual(custom_art._content_identity(before), custom_art._content_identity(temporary.stat()))
                    if when == "before_link":
                        real_link(temp, destination, **kwargs)
                with mock.patch.object(custom_art.os, "link", side_effect=changed_link):
                    result = custom_art.import_selected("front", [source], server, self.settings)
                self.assertEqual(result["imported"], 0, result)
                self.assertIn("temporary image changed", result["failed"][0]["error"])
                self.assertEqual(list(folder.iterdir()), [])
                self.assertEqual(Path(source).read_bytes(), PNG)

    @unittest.skipIf(os.name == "nt", "Windows pins open file and directory handles against replacement")
    def test_temporary_name_replacement_cannot_publish_attacker_bytes(self):
        src = self.source("race.png")
        other = self.source("attacker.png", JPEG)
        folder = self.scm / "game" / "front"
        real_link = os.link
        for replacement in ("symlink", "regular"):
            with self.subTest(replacement=replacement):
                def swap(source, destination, **kwargs):
                    temp = folder / source if isinstance(source, str) else source
                    temp.unlink()
                    if replacement == "symlink":
                        temp.symlink_to(other)
                    else:
                        temp.write_bytes(JPEG)
                    return real_link(source, destination, **kwargs)
                with mock.patch.object(custom_art.os, "link", side_effect=swap):
                    result = custom_art.import_selected("front", [src], server, self.settings)
                self.assertEqual(result["names"], [])
                self.assertEqual(result["failed"][0]["name"], "race.png")
                self.assertFalse((folder / "race.png").exists() and
                                 (folder / "race.png").read_bytes() == PNG)
                # Attacker-owned entries are not removed by our rollback.
                self.assertTrue((folder / "race.png").exists())
                self.assertEqual((folder / "race.png").read_bytes(), JPEG)
                (folder / "race.png").unlink()
                for stale in folder.glob(".wb-custom-art-*"):
                    stale.unlink()

    @unittest.skipIf(os.name == "nt", "Windows pins open file and directory handles against replacement")
    def test_post_link_swap_rolls_back_only_our_publication(self):
        src = self.source("race.png")
        other = self.source("attacker.png", JPEG)
        folder = self.scm / "game" / "front"
        real_link = os.link
        def swap_after_link(source, destination, **kwargs):
            real_link(source, destination, **kwargs)
            temp = folder / source if isinstance(source, str) else source
            temp.unlink()
            temp.symlink_to(other)
        with mock.patch.object(custom_art.os, "link", side_effect=swap_after_link):
            result = custom_art.import_selected("front", [src], server, self.settings)
        self.assertEqual(result["names"], [])
        self.assertFalse((folder / "race.png").exists())
        self.assertEqual(len(list(folder.glob(".wb-custom-art-*"))), 1)

    @unittest.skipIf(os.name == "nt", "Windows pins open file and directory handles against replacement")
    def test_replaced_published_entry_is_not_deleted_on_failed_verification(self):
        src = self.source("race.png")
        other = self.source("attacker.png", JPEG)
        folder = self.scm / "game" / "front"
        real_link = os.link
        def swap_candidate(source, destination, **kwargs):
            real_link(source, destination, **kwargs)
            candidate = folder / destination if isinstance(destination, str) else destination
            candidate.unlink()
            candidate.symlink_to(other)
        with mock.patch.object(custom_art.os, "link", side_effect=swap_candidate):
            result = custom_art.import_selected("front", [src], server, self.settings)
        self.assertEqual(result["names"], [])
        self.assertTrue((folder / "race.png").is_symlink())
        self.assertEqual((folder / "race.png").read_bytes(), JPEG)
        self.assertFalse(list(folder.glob(".wb-custom-art-*")))

    @unittest.skipIf(os.name == "nt", "Windows pins directory handles against rename")
    def test_replaced_destination_is_not_reported_as_success(self):
        src = self.source("race.png")
        folder = self.scm / "game" / "front"
        moved = self.scm / "moved-front"
        real_sync = os.fsync
        def swap_directory(fd):
            real_sync(fd)
            folder.rename(moved)
            folder.mkdir()
        with mock.patch.object(custom_art.os, "fsync", side_effect=swap_directory):
            result = custom_art.import_selected("front", [src], server, self.settings)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    def test_path_based_cleanup_closes_writer_before_unlink(self):
        src = self.source("windows.png")
        folder = self.scm / "game" / "front"
        folder.mkdir(parents=True)
        source_fd = os.open(src, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        real_open, real_unlink = os.open, os.unlink
        writers = []
        def track_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                writers.append(fd)
            return fd
        def unlink_after_close(path, **kwargs):
            for fd in writers:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            return real_unlink(path, **kwargs)
        try:
            with mock.patch.object(server, "_open_windows_artifact_parent", return_value=[]), \
                 mock.patch.object(custom_art, "_entry_stat", side_effect=lambda directory, _fd, entry, _server: os.lstat(directory / entry)), \
                 mock.patch.object(custom_art.os, "open", side_effect=track_open), \
                 mock.patch.object(custom_art.os, "unlink", side_effect=unlink_after_close):
                result = custom_art._copy(source_fd, os.fstat(source_fd), "windows.png", folder, None,
                                          time.monotonic() + 10, server)
        finally:
            os.close(source_fd)
        self.assertEqual(result, "windows.png")
        self.assertEqual((folder / result).read_bytes(), PNG)
        self.assertFalse(list(folder.glob(".wb-custom-art-*")))

    def test_invalid_original_names_are_rejected_and_failures_are_rust_safe(self):
        unsafe = ["name:stream.png", "bad\\name.png", "control\x01.png",
                  "é" * 126 + ".png", ".", "..", "control\x85.png"]
        def never_open():
            self.fail("invalid source name was opened")
        result = custom_art._run("front", [(value, never_open) for value in unsafe],
                                 server, self.settings, time.monotonic() + 10)
        self.assertEqual(result["imported"], 0)
        for row in result["failed"]:
            label = row["name"]
            self.assertTrue(label and label not in (".", ".."))
            self.assertLessEqual(len(label.encode("utf-8")), 255)
            self.assertFalse(any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "/\\:" for c in label))
        self.assertEqual(result["failed"][3]["error"], "unsafe image name")

    def test_start_exception_before_id_and_after_registration(self):
        with mock.patch.object(custom_art, "destination", side_effect=RuntimeError("boom")):
            self.assertFalse(custom_art.start("front", ["/tmp/image.png"], server)["ok"])
        with mock.patch.object(server, "load_settings", side_effect=RuntimeError("boom")):
            self.assertFalse(custom_art.start("front", ["/tmp/image.png"], server)["ok"])
        with custom_art._lock:
            self.assertEqual(custom_art._operations, {})

    def test_atomic_no_replace_collision_race_and_cleanup(self):
        src = self.source("race.png")
        real_link = os.link
        target = self.scm / "game" / "front" / "race.png"
        first = []
        def racing_link(source, destination, **kwargs):
            if (str(destination) == str(target) or (destination == target.name and "dst_dir_fd" in kwargs)) and not first:
                first.append(True)
                target.write_bytes(JPEG)
                raise FileExistsError()
            return real_link(source, destination, **kwargs)
        with mock.patch.object(custom_art.os, "link", side_effect=racing_link):
            result = custom_art.import_selected("front", [src], server, self.settings)
        self.assertEqual(result["names"], ["race (2).png"])
        self.assertEqual(target.read_bytes(), JPEG)
        self.assertFalse(list(target.parent.glob(".wb-custom-art-*")))
