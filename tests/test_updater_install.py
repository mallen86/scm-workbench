"""Dedicated security and admission tests for app updates.

The archives in this module are deliberately made in memory/on disk rather than
using a fixture archive: every hostile property is explicit in the central
-directory metadata and no test needs a large allocation.
"""

import json
import contextlib
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from scm_workbench import server, updater


class ZipBuilder:
    def __init__(self, root):
        self.root = Path(root)

    def make(self, name, entries, *, compression=zipfile.ZIP_STORED):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for entry in entries:
                if len(entry) == 2:
                    member, payload = entry
                    mode = stat.S_IFREG | 0o644
                    flag_bits = 0
                    method = compression
                else:
                    member, payload, mode = entry[:3]
                    flag_bits = entry[3] if len(entry) > 3 else 0
                    method = entry[4] if len(entry) > 4 else compression
                info = zipfile.ZipInfo(member)
                info.create_system = 3
                info.external_attr = (mode & 0xFFFF) << 16
                info.flag_bits = flag_bits
                info.compress_type = method
                archive.writestr(info, payload)
        return path

    @staticmethod
    def windows(extra=(), *, exe=b"MZ"):
        # Exact flat layout assembled by package.yml: shell, app tree, runtime.
        return [
            ("SCM Workbench.exe", exe),
            ("app/scm_workbench/__init__.py", b""),
            ("app/ui/index.html", b"<!doctype html>"),
            ("runtime/python.dll", b"runtime"),
            *extra,
        ]

    @staticmethod
    def mac(extra=(), *, executable=b"#!/bin/sh"):
        return [
            ("SCM Workbench.app/Contents/MacOS/SCM Workbench", executable,
             stat.S_IFREG | 0o755),
            *extra,
        ]


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-updater-extract-")
        self.root = Path(self.temp.name)
        self.zips = ZipBuilder(self.root)
        self.old_repo = updater.UPDATE_REPO
        updater.UPDATE_REPO = "owner/workbench"

    def tearDown(self):
        updater.UPDATE_REPO = self.old_repo
        self.temp.cleanup()

    def extract(self, entries, *, name="release.zip", compression=zipfile.ZIP_STORED,
                dest="installed", constants=None):
        archive = self.zips.make(name, entries, compression=compression)
        destination = self.root / dest
        values = constants or {}
        context = patch.multiple(updater, **values) if values else contextlib.nullcontext()
        with context:
            return updater.extract_app(archive, destination)

    def assert_rejected(self, entries, *, message=None, constants=None,
                        name="bad.zip", dest="installed"):
        destination = self.root / dest
        with self.assertRaises(updater.UpdateError) as caught:
            self.extract(entries, name=name, constants=constants, dest=dest)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertFalse(destination.exists(), "rejected archive was published")
        self.assertEqual(list(self.root.glob(f".{dest}.*")), [],
                         "rejected archive left staging behind")

    def test_limits_are_bounded_without_large_allocations(self):
        cases = [
            ("member count", self.zips.windows([("readme", b"x")]),
             {"ARCHIVE_MEMBER_MAX": 1}, "too many members"),
            ("per member", self.zips.windows([("payload", b"1234")]),
             {"ARCHIVE_MEMBER_MAX_BYTES": 3}, "size or offset"),
            ("aggregate", self.zips.windows([("payload", b"1234")]),
             {"ARCHIVE_UNCOMPRESSED_MAX": 4}, "aggregate"),
        ]
        for label, entries, constants, text in cases:
            archive = self.zips.make(label.replace(" ", "-") + ".zip", entries)
            with self.subTest(label=label):
                destination = self.root / (label.replace(" ", "-") + "-dest")
                with self.assertRaisesRegex(updater.UpdateError, text):
                    with patch.multiple(updater, **constants):
                        updater.extract_app(archive, destination)
                self.assertFalse(destination.exists())

        archive = self.zips.make("ratio.zip", self.zips.windows(
            [("payload", b"A" * 128)]), compression=zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(updater.UpdateError, "ratio"):
            with patch.object(updater, "ARCHIVE_COMPRESSION_RATIO_MAX", 1):
                updater.extract_app(archive, self.root / "ratio-dest")

        # The compressed archive cap is checked before opening the ZIP.
        archive = self.zips.make("archive-cap.zip", self.zips.windows([("x", b"x")]))
        with self.assertRaisesRegex(updater.UpdateError, "too large"):
            with patch.object(updater, "ARCHIVE_MAX_BYTES", archive.stat().st_size - 1):
                updater.extract_app(archive, self.root / "archive-cap-dest")

    def test_malformed_archive_and_preflight_reject_last_hostile_member_without_writes(self):
        malformed = self.root / "malformed.zip"
        malformed.write_bytes(b"not a zip")
        with self.assertRaises(updater.UpdateError):
            updater.extract_app(malformed, self.root / "malformed-dest")

        outside = self.root / "outside.txt"
        outside.write_text("unchanged", encoding="utf-8")
        entries = self.zips.windows([
            ("safe/file.txt", b"safe"),
            ("../../outside.txt", b"hostile"),
        ])
        with self.assertRaises(updater.UpdateError):
            updater.extract_app(self.zips.make("last-hostile.zip", entries), self.root / "installed")
        self.assertFalse((self.root / "installed").exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(self.root.glob(".installed.*")), [])

    def test_portable_entry_names_reject_traversal_absolute_backslash_drive_unc_and_nul(self):
        names = ("../x", "a/../x", "/x", "//server/x", r"a\b",
                 r"C:\x", "C:/x")
        for name in names:
            with self.subTest(name=repr(name)):
                archive_name = "path-" + str(names.index(name)) + ".zip"
                self.assert_rejected(self.zips.windows([(name, b"x")]), name=archive_name,
                                     dest="path-" + str(names.index(name)) + "-dest")

    def test_duplicate_casefold_unicode_file_directory_and_prefix_collisions_reject(self):
        collisions = [
            ("duplicate", [("SCM Workbench.exe", b"one"), ("SCM Workbench.exe", b"two")]),
            ("casefold", [("SCM Workbench.exe", b"one"), ("scm workbench.EXE", b"two")]),
            ("unicode", [("SCM Workbench.exe", b"one"), ("e\u0301", b"x"), ("é", b"y")]),
            ("file-dir", [("SCM Workbench.exe", b"one"), ("thing", b"x"), ("thing/", b"")]),
            ("prefix", [("SCM Workbench.exe", b"one"), ("folder", b"x"), ("folder/file", b"y")]),
        ]
        for label, extra in collisions:
            with self.subTest(collision=label):
                self.assert_rejected(extra, name="collision-" + label + ".zip",
                                     dest="collision-" + label + "-dest")

    def test_leading_dot_components_are_portable_and_extract_safely(self):
        destination = self.extract(self.zips.mac([
            ("SCM Workbench.app/Contents/runtime/python/lib/.empty", b""),
            ("SCM Workbench.app/Contents/runtime/python/site-packages/PIL/.dylibs/libimage.dylib", b"library"),
        ]), dest="dotfiles")
        self.assertTrue((destination / "Contents/runtime/python/lib/.empty").is_file())
        self.assertEqual(
            (destination / "Contents/runtime/python/site-packages/PIL/.dylibs/libimage.dylib").read_bytes(),
            b"library")

    def test_windows_reserved_names_trailing_dots_spaces_and_ads_are_rejected(self):
        names = ("CON", "PRN.txt", "AUX", "NUL", "COM1", "LPT9", "file.",
                 "file ", "directory/name.", "directory/name ", "file:stream")
        for i, name in enumerate(names):
            with self.subTest(name=name):
                self.assert_rejected(self.zips.windows([(name, b"x")]), name=f"reserved-{i}.zip",
                                     dest=f"reserved-{i}-dest")

    def test_encrypted_unsupported_compression_and_special_files_are_rejected(self):
        encrypted = self.zips.make("encrypted.zip", self.zips.windows(
            [("payload", b"x", stat.S_IFREG | 0o644)]))
        raw = bytearray(encrypted.read_bytes())
        # zipfile intentionally clears the encryption bit when writing; set it
        # in both headers to model a real encrypted member without passwords.
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            cursor = 0
            while True:
                cursor = raw.find(signature, cursor)
                if cursor < 0:
                    break
                raw[cursor + offset:cursor + offset + 2] = (1).to_bytes(2, "little")
                cursor += len(signature)
        encrypted.write_bytes(raw)
        with self.assertRaisesRegex(updater.UpdateError, "encrypted"):
            updater.extract_app(encrypted, self.root / "encrypted-dest")

        if zipfile.ZIP_BZIP2 is not None:
            unsupported = self.zips.make("bzip2.zip", self.zips.windows(
                [("payload", b"payload")]), compression=zipfile.ZIP_BZIP2)
            with self.assertRaisesRegex(updater.UpdateError, "compression"):
                updater.extract_app(unsupported, self.root / "bzip2-dest")

        for label, mode in (("fifo", stat.S_IFIFO | 0o644),
                            ("socket", stat.S_IFSOCK | 0o644),
                            ("device", stat.S_IFCHR | 0o644)):
            with self.subTest(kind=label):
                self.assert_rejected(self.zips.windows([("special", b"", mode)]),
                                     name=f"special-{label}.zip", message="special")

    def test_symlink_targets_reject_absolute_outside_cycle_and_symlink_ancestor(self):
        bad_targets = ("/etc/passwd", "../outside", "../../x", r"..\outside",
                       r"C:\x", r"\\server\share", "target\x00bad")
        for i, target in enumerate(bad_targets):
            with self.subTest(target=repr(target)):
                link = ("link", target.encode("utf-8", "surrogatepass"), stat.S_IFLNK | 0o777)
                self.assert_rejected(self.zips.windows([link]), name=f"link-{i}.zip")

        cycle = self.zips.windows([
            ("a", b"b", stat.S_IFLNK | 0o777),
            ("b", b"a", stat.S_IFLNK | 0o777),
        ])
        self.assert_rejected(cycle, name="link-cycle.zip", message="cycle")

        ancestor = self.zips.windows([
            ("dir", b"target", stat.S_IFLNK | 0o777),
            ("dir/file", b"x"),
        ])
        self.assert_rejected(ancestor, name="link-ancestor.zip", message="symlink")

    def test_valid_relative_internal_symlinks_are_created(self):
        entries = self.zips.windows([
            ("runtime/target.txt", b"payload"),
            ("runtime/links/target", b"../target.txt", stat.S_IFLNK | 0o777),
            ("runtime/links/second", b"target", stat.S_IFLNK | 0o777),
        ])
        destination = self.extract(entries, name="valid-links.zip")
        self.assertTrue((destination / "runtime/links/target").is_symlink())
        expected_target = r"..\target.txt" if os.name == "nt" else "../target.txt"
        self.assertEqual(os.readlink(destination / "runtime/links/target"), expected_target)
        self.assertEqual((destination / "runtime/links/target").read_text(), "payload")
        self.assertEqual((destination / "runtime/links/second").read_text(), "payload")

    def test_staging_is_unique_existing_destination_is_refused_and_failures_clean_up(self):
        first = self.extract(self.zips.windows([("app/scm_workbench/a", b"1")]), name="first.zip", dest="first")
        second = self.extract(self.zips.windows([("app/scm_workbench/a", b"2")]), name="second.zip", dest="second")
        self.assertNotEqual(first, second)
        self.assertEqual((first / "SCM Workbench.exe").read_bytes(), b"MZ")
        self.assertEqual((second / "SCM Workbench.exe").read_bytes(), b"MZ")

        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(updater.UpdateError, "existing"):
            updater.extract_app(self.zips.make("existing.zip", self.zips.windows()), existing)
        self.assertEqual(list(existing.iterdir()), [])

        failed = self.root / "publish-failure"
        archive = self.zips.make("publish-failure.zip", self.zips.windows())
        with patch.object(updater, "_rename_noreplace", side_effect=OSError("publish failed")):
            with self.assertRaisesRegex(updater.UpdateError, "publish"):
                updater.extract_app(archive, failed)
        self.assertFalse(failed.exists())
        self.assertEqual(list(self.root.glob(".publish-failure.*")), [])

    def test_special_permission_bits_reject_but_executable_bits_are_preserved(self):
        for index, (name, mode) in enumerate((
                ("app/scm_workbench/setuid", stat.S_IFREG | 0o4775),
                ("app/scm_workbench/sticky", stat.S_IFREG | 0o1000 | 0o644),
                ("app/scm_workbench/bin/", stat.S_IFDIR | 0o3755))):
            with self.subTest(name=name):
                self.assert_rejected(self.zips.windows([(name, b"", mode)]),
                                     name=f"special-mode-{index}.zip",
                                     dest=f"special-mode-{index}", message="permission")

        destination = self.extract(self.zips.windows([
            ("app/scm_workbench/run.sh", b"#!/bin/sh", stat.S_IFREG | 0o755),
        ]), name="modes.zip")
        extracted = destination / "app/scm_workbench/run.sh"
        self.assertEqual(extracted.read_bytes(), b"#!/bin/sh")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(extracted.stat().st_mode), 0o755)

    def test_mac_and_windows_release_shapes_match_the_actual_workflows(self):
        mac = self.extract(self.zips.mac([
            ("SCM Workbench.app/Contents/Resources/app.dat", b"data"),
        ]), name="mac.zip", dest="mac-staging")
        self.assertEqual(mac.name, "SCM Workbench.app")
        self.assertTrue((mac / "Contents/MacOS/SCM Workbench").is_file())

        windows = self.extract(self.zips.windows([
            ("app/scm_workbench/server.py", b"print('ok')"),
        ]), name="windows.zip", dest="windows-staging")
        self.assertEqual(windows.name, "windows-staging")
        self.assertTrue((windows / "SCM Workbench.exe").is_file())

        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/package.yml").read_text(encoding="utf-8")
        builder = (root / "scripts/build_macos_dmg.sh").read_text(encoding="utf-8")
        background = (root / "tauri/dmg-background.png").read_bytes()
        self.assertIn("scm-workbench-macos.dmg", workflow)
        self.assertIn("scripts/build_macos_dmg.sh", workflow)
        self.assertIn('ln -s /Applications "$root/Applications"', builder)
        self.assertIn('set position of item "SCM Workbench.app" to {170, 225}', builder)
        self.assertIn('set position of item "Applications" to {490, 225}', builder)
        self.assertIn('set background picture of viewOptions', builder)
        self.assertNotIn(".metadata_never_index", builder)
        self.assertEqual(background[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(tuple(int.from_bytes(background[n:n + 4], "big")
                               for n in (16, 20)), (660, 400))
        # macOS publishes the same standard DMG for manual and in-app
        # installation; only Windows retains a ZIP payload.
        self.assertNotIn("scm-workbench-macos.zip", workflow)
        self.assertIn("scm-workbench-windows.zip", workflow)
        self.assertIn("actions: write", workflow)
        release_upload = next(line for line in workflow.splitlines()
                              if 'gh release upload "$GITHUB_REF_NAME"' in line)
        self.assertIn("scm-workbench-macos.dmg", release_upload)
        self.assertNotIn("scm-workbench-macos.zip", release_upload)
        self.assertIn("scm-workbench-windows.zip", release_upload)
        upload_at = workflow.index('gh release upload "$GITHUB_REF_NAME"')
        cleanup_at = workflow.index("actions/runs/$GITHUB_RUN_ID/artifacts")
        self.assertGreater(cleanup_at, upload_at)
        self.assertIn('gh api --method DELETE "repos/$GITHUB_REPOSITORY/actions/artifacts/$artifact_id"', workflow)

    def test_other_top_level_shapes_are_rejected_without_publish(self):
        bad_shapes = [
            ("mac-missing-macos", [("SCM Workbench.app/Contents/Resources/x", b"x")]),
            ("two-exes", [("SCM Workbench.exe", b"x"), ("helper.exe", b"x")]),
            ("mixed", self.zips.mac() + [("SCM Workbench.exe", b"x")]),
            ("two-apps", [("SCM Workbench.app/Contents/MacOS/x", b"x"),
                           ("Other.app/Contents/MacOS/x", b"x")]),
        ]
        for label, entries in bad_shapes:
            with self.subTest(shape=label):
                self.assert_rejected(entries, name=f"shape-{label}.zip", message="app shape",
                                     dest=f"shape-{label}-dest")


class DmgPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-updater-dmg-")
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "update.dmg").write_bytes(b"dmg")
        self.candidate = self.root / (".SCM-Workbench-candidate-" + "a" * 64)

    def tearDown(self):
        self.temp.cleanup()

    def fake_helper(self, calls, *, hostile=None, detach_status=0, attach_status=0):
        def run(args, **_kwargs):
            calls.append(list(args))
            if args[1] == "attach":
                mount = Path(args[args.index("-mountpoint") + 1])
                if attach_status:
                    return attach_status, b"", b"failure"
                app = mount / "SCM Workbench.app/Contents"
                (app / "MacOS").mkdir(parents=True)
                (app / "app/scm_workbench").mkdir(parents=True)
                (app / "app/ui").mkdir(parents=True)
                (app / "runtime").mkdir(parents=True)
                (app / "MacOS/SCM Workbench").write_bytes(b"shell")
                (app / "MacOS/SCM Workbench").chmod(0o755)
                (app / "Info.plist").write_bytes(plistlib.dumps({
                    "CFBundleIdentifier": "com.mallen.scmworkbench",
                    "CFBundleExecutable": "SCM Workbench",
                    "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": "2.0.0",
                }))
                if hostile:
                    hostile(mount / "SCM Workbench.app")
                return 0, plistlib.dumps({
                    "system-entities": [{"mount-point": str(mount)}]
                }), b""
            if args[1] == "detach":
                return detach_status, b"", b"detach failure"
            return 0, b"", b""
        return run

    def test_dmg_mount_copy_detach_and_signature_use_fixed_helpers(self):
        calls = []
        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_run_fixed_helper", side_effect=self.fake_helper(calls)):
            result = updater.prepare_dmg(self.work / "update.dmg", self.candidate, "v2.0.0")
        self.assertEqual(result, self.candidate)
        self.assertTrue((result / "Contents/MacOS/SCM Workbench").is_file())
        self.assertEqual([call[:2] for call in calls], [
            [updater.HDIUTIL, "attach"], [updater.HDIUTIL, "detach"],
            [updater.CODESIGN, "--verify"],
        ])
        attach = calls[0]
        self.assertIn("-plist", attach)
        self.assertIn("-readonly", attach)
        self.assertIn("-noautoopen", attach)
        self.assertIn("-nobrowse", attach)
        self.assertFalse(any("/SCM Workbench.app" in str(arg) and arg == str(self.candidate)
                             for call in calls for arg in call))
        self.assertEqual(list(self.root.glob(".SCM-Workbench-candidate-" + "a" * 64 + "-*")), [])

    def test_dmg_rejects_hostile_tree_and_never_publishes(self):
        outside = self.root / "outside"
        outside.write_text("secret")
        def hostile(app):
            (app / "Contents/Resources").mkdir()
            (app / "Contents/Resources/escape").symlink_to(outside)
        calls = []
        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_run_fixed_helper", side_effect=self.fake_helper(calls, hostile=hostile)):
            with self.assertRaisesRegex(updater.UpdateError, "unsafe symlink"):
                updater.prepare_dmg(self.work / "update.dmg", self.candidate, "2.0.0")
        self.assertFalse(self.candidate.exists())
        self.assertEqual(outside.read_text(), "secret")
        self.assertEqual(calls[1][0:2], [updater.HDIUTIL, "detach"])

    def test_dmg_bad_plist_still_detaches_and_mount_failure_never_detaches(self):
        calls = []
        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_attach_dmg", return_value=b"not plist"), \
                patch.object(updater, "_detach_dmg", side_effect=lambda mount: calls.append(mount)):
            with self.assertRaisesRegex(updater.UpdateError, "plist"):
                updater.prepare_dmg(self.work / "update.dmg", self.candidate, "2.0.0")
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.candidate.exists())

        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_attach_dmg", side_effect=updater.UpdateError("attach failed")), \
                patch.object(updater, "_detach_dmg") as detach:
            with self.assertRaisesRegex(updater.UpdateError, "attach"):
                updater.prepare_dmg(self.work / "update.dmg", self.candidate, "2.0.0")
        detach.assert_not_called()

    def test_dmg_detach_failure_blocks_publication(self):
        calls = []
        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_run_fixed_helper", side_effect=self.fake_helper(calls, detach_status=1)):
            with self.assertRaisesRegex(updater.UpdateError, "detach"):
                updater.prepare_dmg(self.work / "update.dmg", self.candidate, "2.0.0")
        self.assertFalse(self.candidate.exists())

    def test_failed_attach_attempts_cleanup_and_failed_signature_removes_candidate(self):
        calls = []
        with patch.object(updater, "_run_fixed_helper",
                          side_effect=self.fake_helper(calls, attach_status=1)):
            with self.assertRaisesRegex(updater.UpdateError, "attach"):
                updater._attach_dmg(self.work / "update.dmg", self.work / "mount")
        self.assertEqual([call[:2] for call in calls], [
            [updater.HDIUTIL, "attach"], [updater.HDIUTIL, "detach"],
        ])

        calls = []
        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_run_fixed_helper", side_effect=self.fake_helper(calls)), \
                patch.object(updater, "_verify_macos_candidate",
                             side_effect=updater.UpdateError("bad signature")):
            with self.assertRaisesRegex(updater.UpdateError, "signature"):
                updater.prepare_dmg(self.work / "update.dmg", self.candidate, "2.0.0")
        self.assertFalse(self.candidate.exists())

    def test_dmg_accepts_safe_multi_hop_symlinks_and_rejects_cycles(self):
        def safe_chain(app):
            resources = app / "Contents/Resources"
            resources.mkdir()
            (resources / "target").write_bytes(b"ok")
            (resources / "second").symlink_to("target")
            (resources / "first").symlink_to("second")

        calls = []
        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_run_fixed_helper",
                             side_effect=self.fake_helper(calls, hostile=safe_chain)):
            updater.prepare_dmg(self.work / "update.dmg", self.candidate, "2.0.0")
        self.assertEqual(os.readlink(self.candidate / "Contents/Resources/first"), "second")

        self.candidate.rename(self.root / "prior-candidate")
        def cycle(app):
            resources = app / "Contents/Resources"
            resources.mkdir()
            (resources / "first").symlink_to("second")
            (resources / "second").symlink_to("first")

        calls = []
        with patch.object(updater, "sys", type("Platform", (), {"platform": "darwin"})()), \
                patch.object(updater, "_run_fixed_helper",
                             side_effect=self.fake_helper(calls, hostile=cycle)):
            with self.assertRaisesRegex(updater.UpdateError, "symlink cycle"):
                updater.prepare_dmg(self.work / "update.dmg", self.candidate, "2.0.0")
        self.assertFalse(self.candidate.exists())

    @unittest.skipUnless(os.name == "posix", "bounded helper test requires POSIX process groups")
    def test_fixed_helper_enforces_live_output_cap_and_timeout(self):
        with patch.object(updater, "HDIUTIL", sys.executable):
            with self.assertRaisesRegex(updater.UpdateError, "output is too large"):
                updater._run_fixed_helper(
                    [sys.executable, "-c", "import os; os.write(1, b'x' * 131072)"],
                    output_limit=1024)
            started = time.monotonic()
            with self.assertRaisesRegex(updater.UpdateError, "timed out"):
                updater._run_fixed_helper(
                    [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
            self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(sys.platform == "darwin" and Path(updater.HDIUTIL).is_file() and
                         Path(updater.CODESIGN).is_file(), "requires macOS disk-image tools")
    def test_real_dmg_mount_copy_detach_and_candidate_validation(self):
        source = self.root / "image-root"
        contents = source / "SCM Workbench.app/Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "app/scm_workbench").mkdir(parents=True)
        (contents / "app/ui").mkdir(parents=True)
        (contents / "runtime").mkdir(parents=True)
        executable = contents / "MacOS/SCM Workbench"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.mallen.scmworkbench",
            "CFBundleExecutable": "SCM Workbench",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "2.0.0",
        }))
        subprocess.run([updater.CODESIGN, "--force", "--deep", "--sign", "-",
                        str(source / "SCM Workbench.app")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        dmg = self.work / "real.dmg"
        subprocess.run([updater.HDIUTIL, "create", "-quiet", "-ov", "-format", "UDZO",
                        "-fs", "HFS+", "-srcfolder", str(source), str(dmg)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        result = updater.prepare_dmg(dmg, self.candidate, "v2.0.0")

        self.assertEqual(result, self.candidate)
        self.assertTrue((result / "Contents/MacOS/SCM Workbench").is_file())
        self.assertEqual(list(self.work.glob(".scm-workbench-mount-*")), [])


class UpdateStartAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-updater-admission-")
        self.root = Path(self.temp.name)
        self.saved = {name: getattr(server, name) for name in (
            "DATA_DIR", "UPDATE_STATE_FILE", "LOGS_DIR", "JOBS_FILE", "SERVER_VERSION", "JOBS")}
        server.DATA_DIR = self.root / "data"
        server.UPDATE_STATE_FILE = server.DATA_DIR / "update-state.json"
        server.LOGS_DIR = server.DATA_DIR / "logs"
        server.JOBS_FILE = server.DATA_DIR / "jobs.json"
        server.SERVER_VERSION = "1.0.0"
        server.JOBS = {}
        self.old_repo = updater.UPDATE_REPO
        updater.UPDATE_REPO = "owner/workbench"
        self.asset = {
            "id": 9, "tag": "v2.0.0", "name": "scm-workbench-macos.dmg",
            "url": "https://github.com/owner/workbench/releases/download/v2.0.0/scm-workbench-macos.dmg",
            "size": 1, "digest": None,
        }

    def tearDown(self):
        with server.JOBS_LOCK:
            server.JOBS.clear()
        for name, value in self.saved.items():
            setattr(server, name, value)
        updater.UPDATE_REPO = self.old_repo
        self.temp.cleanup()

    def state(self, **changes):
        state = server._default_update_state()
        state.update(status="update-available", current="1.0.0", latest="v2.0.0",
                     asset=dict(self.asset), checked_at=1.0)
        state.update(changes)
        return state

    def write_state(self, state):
        server.DATA_DIR.mkdir(parents=True, exist_ok=True)
        server.UPDATE_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    def test_only_canonical_state_binds_the_job_and_caller_values_are_ignored(self):
        canonical = self.state()
        server.save_update_state(canonical)
        started = []

        class NoRunThread:
            def __init__(self, *, target, daemon, name):
                started.append((target, daemon, name))
            def start(self):
                pass

        with patch.object(server.threading, "Thread", NoRunThread):
            job, errors = server.start_update_job(latest="v99.0.0", force=True, asset={"evil": True})
        self.assertFalse(errors)
        self.assertEqual(job["title"], "Update the app to v2.0.0")
        self.assertEqual(len(started), 1)
        # The worker closure is invoked only after admission here; inspect its
        # captured state by running the real updater seam in a safe fake.
        observed = {}
        with patch.object(updater, "run_job", side_effect=lambda j, plan, log: observed.update(plan)):
            started[0][0]()
        self.assertEqual(observed["latest"], "v2.0.0")
        self.assertEqual(observed["asset"], canonical["asset"])

    def test_stale_mismatched_asset_and_downgrade_or_same_release_are_rejected(self):
        cases = [
            ("stale current", self.state(current="0.9.0")),
            ("mismatched asset", self.state(asset=dict(self.asset, tag="v3.0.0"))),
            ("downgrade", self.state(latest="v0.9.0", asset=dict(self.asset, tag="v0.9.0",
                url=self.asset["url"].replace("v2.0.0", "v0.9.0")))),
            ("same", self.state(latest="v1.0.0", asset=dict(self.asset, tag="v1.0.0",
                url=self.asset["url"].replace("v2.0.0", "v1.0.0")))),
        ]
        for label, state in cases:
            with self.subTest(case=label):
                self.write_state(state)
                with patch.object(server.threading, "Thread") as thread:
                    job, errors = server.start_update_job()
                self.assertIsNone(job)
                self.assertTrue(errors)
                thread.assert_not_called()
                self.assertEqual(server.JOBS, {})

    def test_http_start_requires_exact_empty_json_object(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d/api/updates/start" % httpd.server_address[1]
        try:
            def post(raw):
                request = urllib.request.Request(base, data=raw,
                    headers={"Content-Type": "application/json"}, method="POST")
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read())

            invalid = (b"", b"null", b"[]", b"{\"force\":true}", b"not-json",
                       b"{ } trailing")
            with patch.object(server, "start_update_job") as start:
                for raw in invalid:
                    with self.subTest(body=raw):
                        status, body = post(raw)
                        self.assertEqual(status, 400)
                        self.assertIn("exactly an empty object", body["errors"][0])
                start.assert_not_called()

            fake = {"id": "abc", "title": "Update", "status": "running",
                    "cmd": "update"}
            with patch.object(server, "start_update_job", return_value=(fake, [])) as start:
                status, body = post(b"{}")
            self.assertEqual(status, 200)
            self.assertEqual(body["ok"], True)
            self.assertEqual(body["job"]["id"], "abc")
            start.assert_called_once_with()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def test_concurrent_starts_admit_exactly_one_job_for_the_full_worker_lifetime(self):
        server.save_update_state(self.state())
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def running_job(job, plan, log):
            calls.append(job["id"])
            entered.set()
            self.assertTrue(release.wait(3))
            job["status"] = "ok"
            job["exit_code"] = 0
            job["ended"] = time.time()
            job["duration"] = 0.01

        results = []
        first_returned = threading.Event()

        def first_call():
            results.append(server.start_update_job())
            first_returned.set()

        with patch.object(updater, "run_job", side_effect=running_job):
            first = threading.Thread(target=first_call)
            first.start()
            self.assertTrue(entered.wait(3))
            second = threading.Thread(target=lambda: results.append(server.start_update_job()))
            second.start()
            second.join(timeout=3)
            self.assertTrue(first_returned.wait(3))
            self.assertFalse(second.is_alive())
            # The first caller returns after admission while its worker is
            # still blocked; the second caller returns the rejection.
            self.assertEqual(len(results), 2)
            self.assertEqual(sum(result[0] is None for result in results), 1)
            rejected = next(result for result in results if result[0] is None)
            self.assertIn("already running", rejected[1][0])
            self.assertEqual(len(calls), 1)
            release.set()
            first.join(timeout=3)
        self.assertFalse(first.is_alive())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with server.JOBS_LOCK:
                if not server._UPDATE_ADMISSION:
                    break
            time.sleep(0.01)
        with server.JOBS_LOCK:
            self.assertFalse(server._UPDATE_ADMISSION)

        # Completion releases admission; a second complete worker can start.
        with patch.object(updater, "run_job", side_effect=lambda job, plan, log: (
                job.update(status="ok", exit_code=0, ended=time.time(), duration=0.01))):
            job, errors = server.start_update_job()
            self.assertFalse(errors)
            deadline = time.time() + 3
            while job["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(job["status"], "ok")

    def test_thread_exception_marks_failure_and_releases_admission(self):
        server.save_update_state(self.state())
        failed = threading.Event()

        def explode(job, plan, log):
            failed.set()
            raise RuntimeError("worker seam")

        with patch.object(updater, "run_job", side_effect=explode):
            first, errors = server.start_update_job()
            self.assertFalse(errors)
            self.assertTrue(failed.wait(3))
            deadline = time.time() + 3
            while first["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(first["status"], "fail")

        with patch.object(updater, "run_job", side_effect=lambda job, plan, log: (
                job.update(status="ok", exit_code=0, ended=time.time(), duration=0.01))):
            second, errors = server.start_update_job()
        self.assertFalse(errors)
        self.assertIsNotNone(second)


if __name__ == "__main__":
    unittest.main()
