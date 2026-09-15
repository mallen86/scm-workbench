"""Bounded representative first-page PDF preview contracts."""

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
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


VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
)
VALID_JPEG = b"\xff\xd8\xff\xe0" + b"representative" + b"\xff\xd9"


class PdfFrontPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-pdf-front-preview-")
        self.fixture = Phase0Fixture(Path(self.temp.name))
        self.old_globals = {
            name: getattr(server, name)
            for name in (
                "DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
                "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE", "JOBS",
                "_IMAGE_JOB_USERS", "_IMAGE_PREVIEW_USERS", "_REPO_MUTATION_USERS",
            )
        }
        self.old_caches = {
            name: dict(getattr(server, name))
            for name in ("MANIFEST_CACHE", "_INFO_SNAP", "_REPOS_MTIME")
        }
        server.DATA_DIR = self.fixture.data
        server.SETTINGS_FILE = self.fixture.data / "settings.json"
        server.JOBS_FILE = self.fixture.data / "jobs.json"
        server.LOGS_DIR = self.fixture.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = self.fixture.data / "offsets.json"
        server.UPDATE_STATE_FILE = self.fixture.data / "updates.json"
        server.JOBS = {}
        server._IMAGE_JOB_USERS = 0
        server._IMAGE_PREVIEW_USERS = 0
        server._REPO_MUTATION_USERS = 0
        for cache in self.old_caches:
            getattr(server, cache).clear()
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({
            "scm_dir": str(self.fixture.scm),
            "extras_dir": str(self.fixture.extras),
        })
        server.save_settings(settings)
        with server._PDF_PREVIEW_OP_LOCK:
            server._PDF_PREVIEW_OPS.clear()
        (self.fixture.scm / "game/front/card.png").write_bytes(VALID_PNG)

    def tearDown(self):
        server.stop_all_pdf_previews(timeout=2)
        with server._PDF_PREVIEW_OP_LOCK:
            server._PDF_PREVIEW_OPS.clear()
        for name, value in self.old_globals.items():
            setattr(server, name, value)
        for name, value in self.old_caches.items():
            cache = getattr(server, name)
            cache.clear()
            cache.update(value)
        self.temp.cleanup()

    @staticmethod
    def record(**changes):
        value = {
            "id": "a" * 32,
            "deadline": time.monotonic() + 15,
            "cancel": threading.Event(),
            "proc": None,
            "proc_lock": threading.Lock(),
            "temp_dir": None,
            "temp_identity": None,
        }
        value.update(changes)
        return value

    def test_start_poll_finishes_without_jobs_logs_or_artifacts(self):
        payload = {
            "ok": True, "mime": "image/jpeg", "data": base64.b64encode(VALID_JPEG).decode(),
            "width": 10, "height": 12, "sampled": 1, "available": 1,
        }
        jobs_before = dict(server.JOBS)
        with mock.patch.object(server, "_render_pdf_preview", return_value=payload):
            started = server.start_pdf_preview({})
            self.assertTrue(started["ok"])
            operation_id = started["operation"]["id"]
            self.assertRegex(operation_id, r"^[0-9a-f]{32}$")
            for _ in range(100):
                polled = server.poll_pdf_preview(operation_id)
                if polled.get("status") == "done":
                    break
                time.sleep(0.01)
            else:
                self.fail("preview did not finish")
        self.assertEqual(polled["result"], payload)
        self.assertEqual(server.JOBS, jobs_before)
        self.assertFalse(server.LOGS_DIR.exists())
        self.assertFalse((self.fixture.data / "artifacts").exists())

    def test_raw_arguments_reject_nonfinite_oversized_and_nested_values(self):
        invalid = [
            {"ppi": float("nan")},
            {"ppi": float("inf")},
            {"skip": [str(server.PDF_PREVIEW_SCAN_MAX_SCANNED)] * 257},
            {"label": "x" * 9000},
            {"label": "x\x00y"},
            {"bad\nkey": "value"},
            {"unknown": True},
            {"extra": {"nested": {"too": {"deep": True}}}},
            {"ppi": "1e999"},
        ]
        for args in invalid:
            with self.subTest(args=list(args)):
                result = server.start_pdf_preview(args)
                self.assertFalse(result["ok"])
                self.assertTrue(result["unavailable"])
        with server._PDF_PREVIEW_OP_LOCK:
            self.assertFalse(server._PDF_PREVIEW_OPS)

    def test_first_page_slots_follow_verified_layouts_without_a_fixed_card_cap(self):
        info = server.get_info()
        scm_info = info["scm"]
        scm_info["paper_sizes"].extend([
            {"name": "a3", "width": "297mm", "height": "420mm", "aliases": []},
            {"name": "arch_b", "width": "12in", "height": "18in", "aliases": ["poster"]},
        ])
        scm_info["card_sizes"].append({
            "name": "micro", "width": "32mm", "height": "45mm", "aliases": ["tiny"],
        })
        scm_info["layouts"].update({
            "a3": {"standard": {"default": {"num_rows": 3, "num_cols": 6}}},
            "arch_b": {"micro": {
                "default": {"num_rows": 6, "num_cols": 12},
                "borderless": {"num_rows": 9, "num_cols": 9},
            }},
        })
        scm_info["specialty"].append({
            "name": "full-sheet", "paper": "letter", "width": "1in", "height": "1in",
            "rows": 10, "cols": 10,
        })
        settings = server.load_settings()
        self.assertEqual(server._pdf_preview_page_slots(
            info, {"paper_size": "a3", "card_size": "standard"}, settings), 18)
        self.assertEqual(server._pdf_preview_page_slots(
            info, {"paper_size": "poster", "card_size": "tiny"}, settings), 72)
        self.assertEqual(server._pdf_preview_page_slots(
            info, {"paper_size": "arch_b", "card_size": "micro", "borderless": True}, settings), 81)
        self.assertEqual(server._pdf_preview_page_slots(
            info, {"specialty": "full-sheet"}, settings), 100)
        self.assertEqual(server._pdf_preview_page_slots(
            info, {"paper_size": "a3", "card_size": "standard", "skip": [0, "1", 99]}, settings), 16)

    def test_sources_are_limited_to_the_pinned_checkout(self):
        outside = self.fixture.outside / "front"
        outside.mkdir()
        (outside / "card.png").write_bytes(VALID_PNG)
        result = server.start_pdf_preview({"front_dir": str(outside)})
        self.assertFalse(result["ok"])
        self.assertIn("inside the connected SCM checkout", result["errors"][0])

    def test_paper_geometry_and_custom_specialty_are_bounded(self):
        info = server.get_info()
        settings = server.load_settings()
        for specialty in info["scm"].get("specialty", []):
            server._pdf_preview_validate_paper(
                info, {"specialty": specialty["name"]}, settings)
        oversized = json.loads(json.dumps(info))
        oversized["scm"]["paper_sizes"][0]["width"] = "100in"
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_validate_paper(oversized, {"paper_size": "letter"}, settings)
        inline = json.loads(json.dumps(info))
        inline["scm"]["specialty"] = [{
            "name": "custom", "paper": "not-catalogued",
            "width": "1in", "height": "1in", "rows": 1, "cols": 1,
        }]
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_validate_paper(inline, {"specialty": "custom"}, settings)
        excessive = json.loads(json.dumps(info))
        excessive["scm"]["specialty"] = [{
            "name": "custom", "paper": "letter",
            "width": "1in", "height": "1in", "rows": 33, "cols": 33,
        }]
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_validate_paper(excessive, {"specialty": "custom"}, settings)
        tiny_card = json.loads(json.dumps(info))
        card = next(item for item in tiny_card["scm"]["card_sizes"]
                    if item["name"] == "standard")
        card["width"] = "0.01in"
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_validate_paper(
                tiny_card, {"paper_size": "letter", "card_size": "standard"}, settings)
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_validate_paper(
                info, {"specialty": "anything", "borderless": True}, settings)

    def test_scan_rejects_links_and_samples_naturally_without_mutation(self):
        front = self.fixture.scm / "game/front"
        for child in list(front.iterdir()):
            child.unlink()
        expected = {}
        for number in range(1, 21):
            name = f"card{number}.png"
            payload = VALID_PNG + str(number).encode()
            (front / name).write_bytes(payload)
            expected[name] = payload
        (front / ".keep-user-file").write_bytes(b"preserve")
        expected[".keep-user-file"] = b"preserve"
        raw = self.fixture.data / "raw"
        raw.mkdir(parents=True)
        root, identity, parts = server._pdf_preview_source(server.load_settings(), {"front_dir": "game/front"})
        sampled, available = server._pdf_preview_copy_sample(
            self.record(), root, identity, parts, raw)
        self.assertEqual((sampled, available), (16, 20))
        copied = [path.read_bytes() for path in sorted(raw.iterdir())]
        self.assertEqual(copied, [expected[f"card{number}.png"] for number in range(1, 17)])
        self.assertEqual({path.name: path.read_bytes() for path in front.iterdir()}, expected)

        link = front / "linked.png"
        try:
            link.symlink_to(self.fixture.outside / "secret.txt")
        except (OSError, NotImplementedError):
            self.skipTest("links unavailable")
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_copy_sample(
                self.record(), root, identity, parts, self.fixture.data / "unused")

    def test_windows_scan_uses_handle_identity_when_path_timestamp_is_stale(self):
        front = self.fixture.scm / "game/front"
        card = front / "card.png"
        root, identity, parts = server._pdf_preview_source(
            server.load_settings(), {"front_dir": "game/front"})
        real_lstat = os.lstat
        swapped = False

        class CachedPathStat:
            def __init__(self, observed):
                self._observed = observed
                self.st_ctime_ns = observed.st_ctime_ns - 500_000

            def __getattr__(self, name):
                return getattr(self._observed, name)

        def cached_lstat(path):
            observed = real_lstat(path)
            if Path(path) != card:
                return observed
            cached = CachedPathStat(observed)
            if swapped:
                cached.st_ino = observed.st_ino + 1
            return cached

        def portable_stable_open(path):
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            return fd, os.fstat(fd)

        expected_fd, expected_stat = portable_stable_open(card)
        os.close(expected_fd)
        expected_identity = server._pdf_preview_identity(expected_stat)
        with mock.patch.object(server.os, "lstat", side_effect=cached_lstat), \
                mock.patch.object(server, "_open_windows_regular_file",
                                  side_effect=portable_stable_open):
            candidates = server._pdf_preview_scan_windows(root, identity, parts)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["identity"], expected_identity)
            swapped = True
            with self.assertRaisesRegex(server.PdfPreviewError, "changed while"):
                server._pdf_preview_scan_windows(root, identity, parts)

    def test_private_normalized_samples_fill_large_first_page_layouts(self):
        fronts = self.fixture.data / "normalized-fronts"
        fronts.mkdir(parents=True)
        first = VALID_JPEG + b"first\xff\xd9"
        second = VALID_JPEG + b"second\xff\xd9"
        (fronts / "0001.jpg").write_bytes(first)
        (fronts / "0002.jpg").write_bytes(second)
        server._pdf_preview_fill_page(self.record(), fronts, 2, 72)
        files = sorted(fronts.iterdir())
        self.assertEqual(len(files), 72)
        self.assertEqual(files[2].read_bytes(), first)
        self.assertEqual(files[3].read_bytes(), second)
        if os.name != "nt":
            self.assertEqual(len({path.stat().st_ino for path in files}), 72)
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_fill_page(
                self.record(), fronts, 2, server.PDF_PREVIEW_PAGE_SLOT_MAX + 1)

    def test_render_uses_private_overrides_and_returns_only_bounded_jpeg(self):
        source_before = {
            path.relative_to(self.fixture.scm): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.fixture.scm.rglob("*") if path.is_file()
        }
        settings = server.load_settings()
        info = server.get_info()
        spec = server.get_manifest()["create_pdf"]
        args, errors, _warnings = server.normalize_args(spec, {})
        self.assertFalse(errors)
        root, identity, parts = server._pdf_preview_source(settings, args)
        record = self.record(
            args=args, settings=settings, info=info, scm_root=root,
            scm_identity=identity, source_parts=parts,
        )
        calls = []

        def fake_child(_record, argv, cwd, child_env, _failure):
            calls.append((list(argv), Path(cwd), dict(child_env)))
            if "prepare" in argv:
                Path(argv[-1]).write_text(json.dumps({
                    "version": 1, "count": 1, "dimensions": [[2, 2]],
                }), encoding="utf-8")
            elif "encode" in argv:
                Path(argv[-2]).write_bytes(VALID_JPEG)
                Path(argv[-1]).write_text(json.dumps({
                    "version": 1, "width": 100, "height": 120,
                    "bytes": len(VALID_JPEG),
                }), encoding="utf-8")
            else:
                output_index = argv.index("--output_path") + 1
                (Path(argv[output_index]) / "page1.png").write_bytes(VALID_PNG)

        try:
            with mock.patch.object(server, "_pdf_preview_run_child", side_effect=fake_child):
                result = server._render_pdf_preview(record)
            self.assertTrue(result["ok"])
            self.assertEqual(base64.b64decode(result["data"]), VALID_JPEG)
            self.assertEqual((result["sampled"], result["placed"], result["available"]), (1, 1, 1))
            self.assertEqual(len(calls), 3)
            renderer = calls[1][0]
            self.assertEqual(calls[1][2].get("PYTHONDONTWRITEBYTECODE"), "1")
            self.assertEqual(renderer[renderer.index("--ppi") + 1], "75")
            self.assertIn("--output_images", renderer)
            self.assertIn("--only_fronts", renderer)
            self.assertNotIn("--load_offset", renderer)
            for flag in ("--front_dir_path", "--back_dir_path", "--double_sided_dir_path", "--output_path"):
                path = Path(renderer[renderer.index(flag) + 1])
                self.assertTrue(path.is_relative_to(record["temp_dir"]))
            source_after = {
                path.relative_to(self.fixture.scm): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.fixture.scm.rglob("*") if path.is_file()
            }
            self.assertEqual(source_after, source_before)
        finally:
            server._pdf_preview_cleanup_tree(record["temp_dir"], record["temp_identity"])
        self.assertFalse(record["temp_dir"].exists())

    def test_special_files_and_replaced_checkout_fail_closed(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is unavailable")
        front = self.fixture.scm / "game/front"
        fifo = front / "not-an-image.png"
        os.mkfifo(fifo)
        root, identity, parts = server._pdf_preview_source(
            server.load_settings(), {"front_dir": "game/front"})
        with self.assertRaises(server.PdfPreviewError):
            server._pdf_preview_copy_sample(
                self.record(), root, identity, parts, self.fixture.data / "raw-special")
        fifo.unlink()

        moved = self.fixture.root / "old-scm"
        self.fixture.scm.rename(moved)
        self.fixture.scm.mkdir()
        try:
            with self.assertRaises(server.PdfPreviewError):
                server._pdf_preview_copy_sample(
                    self.record(), root, identity, parts, self.fixture.data / "raw-replaced")
        finally:
            self.fixture.scm.rmdir()
            moved.rename(self.fixture.scm)

    def test_start_failure_stale_cleanup_and_terminal_expiry_are_bounded(self):
        parent = server._pdf_preview_parent()
        stale = parent / (server.PDF_PREVIEW_TEMP_PREFIX + "c" * 32)
        stale.mkdir()
        (stale / "private.txt").write_text("discard", encoding="utf-8")
        unrelated = parent / "keep-me"
        unrelated.mkdir()
        if os.name != "nt":
            parent.chmod(0o755)
        server._pdf_preview_cleanup_stale()
        self.assertFalse(stale.exists())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        self.assertTrue(unrelated.exists())

        with mock.patch.object(server.threading.Thread, "start", side_effect=RuntimeError("no thread")):
            rejected = server.start_pdf_preview({})
        self.assertFalse(rejected["ok"])
        with server._PDF_PREVIEW_OP_LOCK:
            self.assertFalse(server._PDF_PREVIEW_OPS)
            server._PDF_PREVIEW_OPS["d" * 32] = {
                "status": "done",
                "ended": time.time() - server.PDF_PREVIEW_OPERATION_TTL - 1,
                "result": {"ok": False},
            }
            server._prune_pdf_preview_operations_locked()
            self.assertNotIn("d" * 32, server._PDF_PREVIEW_OPS)

    def test_preview_lease_is_exclusive_with_jobs_deletion_and_repo_mutation(self):
        self.assertTrue(server._acquire_image_preview_lease())
        try:
            self.assertFalse(server._acquire_image_job_lease())
            self.assertFalse(server._acquire_repo_mutation_lease())
            with self.assertRaises(server.ImageDeleteError):
                with server._image_delete_lock():
                    self.fail("image deletion overlapped a PDF preview")
        finally:
            server._release_image_preview_lease()
        self.assertTrue(server._acquire_image_job_lease())
        try:
            self.assertFalse(server._acquire_image_preview_lease())
        finally:
            server._release_image_job_lease()
        self.assertTrue(server._acquire_repo_mutation_lease())
        try:
            self.assertFalse(server._acquire_image_preview_lease())
        finally:
            server._release_repo_mutation_lease()

    def test_child_deadline_terminates_the_process_tree(self):
        record = self.record(deadline=time.monotonic() + 0.05)
        with self.assertRaises(server.PdfPreviewError) as raised:
            server._pdf_preview_run_child(
                record, [sys.executable, "-c", "import time; time.sleep(30)"],
                Path.cwd(), os.environ.copy(), "failed",
            )
        self.assertTrue(raised.exception.retryable)
        self.assertIsNone(record["proc"])

    def test_cancel_terminates_and_reaps_child_process_group(self):
        record = self.record(deadline=time.monotonic() + 10)
        caught = []

        def run():
            try:
                server._pdf_preview_run_child(
                    record, [sys.executable, "-c", "import time; time.sleep(30)"],
                    Path.cwd(), os.environ.copy(), "failed",
                )
            except server.PdfPreviewError as exc:
                caught.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        for _ in range(100):
            with record["proc_lock"]:
                proc = record.get("proc")
            if proc is not None:
                break
            time.sleep(0.01)
        else:
            self.fail("child did not start")
        server._cancel_pdf_preview_record(record)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(proc.poll())
        self.assertTrue(caught and caught[0].cancelled)

    def test_real_helper_prepare_and_encode_when_pillow_is_available(self):
        if importlib.util.find_spec("PIL") is None:
            self.skipTest("Pillow is intentionally supplied by the selected SCM runtime")
        source = self.fixture.data / "helper-raw"
        fronts = self.fixture.data / "helper-fronts"
        source.mkdir(parents=True)
        fronts.mkdir()
        (source / "0001.img").write_bytes(VALID_PNG)
        prepared = self.fixture.data / "helper-prepared.json"
        helper = Path(server.__file__).with_name("pdf_preview_helper.py")
        subprocess.run([sys.executable, str(helper), "prepare", str(source), str(fronts), str(prepared)],
                       check=True, timeout=5)
        self.assertEqual(json.loads(prepared.read_text())["count"], 1)
        page = self.fixture.data / "page.png"
        page.write_bytes(VALID_PNG)
        jpeg = self.fixture.data / "page.jpg"
        encoded = self.fixture.data / "helper-encoded.json"
        subprocess.run([sys.executable, str(helper), "encode", str(page), str(jpeg), str(encoded)],
                       check=True, timeout=5)
        metadata = json.loads(encoded.read_text())
        self.assertLessEqual(metadata["bytes"], server.PDF_PREVIEW_JPEG_MAX_BYTES)
        self.assertTrue(jpeg.read_bytes().startswith(b"\xff\xd8\xff"))


class PdfFrontPreviewTransportTests(unittest.TestCase):
    @staticmethod
    def call(method, params):
        return ipc.dispatch({"id": "pdf-front", "method": method, "params": params})

    def test_native_methods_are_strict_and_bounded(self):
        invalid = [
            ("pdf_preview.start", {}),
            ("pdf_preview.start", {"args": [], "extra": True}),
            ("pdf_preview.start", {"args": {"value": "x" * ipc.MAX_PDF_PREVIEW_ARGS_SIZE}}),
            ("pdf_preview.poll", {"operation_id": "A" * 32}),
            ("pdf_preview.cancel", {"operation_id": "abc"}),
        ]
        for method, params in invalid:
            with self.subTest(method=method):
                result = self.call(method, params)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "bad_request")

    def test_native_dispatch_and_result_bound(self):
        with mock.patch.object(server, "start_pdf_preview", return_value={"ok": True, "operation": {
                "id": "a" * 32, "status": "running"}}):
            result = self.call("pdf_preview.start", {"args": {"paper_size": "letter"}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["operation"]["id"], "a" * 32)
        missing = {"ok": False, "error": {"code": "bad_request", "message": "operation not found"}}
        with mock.patch.object(server, "poll_pdf_preview", return_value=missing):
            result = self.call("pdf_preview.poll", {"operation_id": "a" * 32})
        self.assertEqual(result, {"id": "pdf-front", "ok": True, "result": missing})

        oversized = {"ok": True, "status": "done", "result": {
            "ok": True, "data": "x" * ipc.MAX_PDF_PREVIEW_RESULT_SIZE}}
        with mock.patch.object(server, "poll_pdf_preview", return_value=oversized):
            result = self.call("pdf_preview.poll", {"operation_id": "a" * 32})
        self.assertEqual(result["error"]["code"], "result_too_large")

    def test_http_start_poll_cancel_match_shared_results(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d/api/pdf-preview" % httpd.server_address[1]

        def request(body):
            value = json.dumps(body).encode()
            req = urllib.request.Request(base, data=value,
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read())
                finally:
                    error.close()

        try:
            shared = {"ok": True, "operation": {"id": "b" * 32, "status": "running"}}
            with mock.patch.object(server, "start_pdf_preview", return_value=shared):
                self.assertEqual(request({"op": "start", "args": {}}), (200, shared))
            with mock.patch.object(server, "poll_pdf_preview", return_value={"ok": True, "status": "running"}):
                self.assertEqual(request({"op": "poll", "operation_id": "b" * 32}),
                                 (200, {"ok": True, "status": "running"}))
            with mock.patch.object(server, "cancel_pdf_preview", return_value={"ok": True, "cancelled": True}):
                self.assertEqual(request({"op": "cancel", "operation_id": "b" * 32}),
                                 (200, {"ok": True, "cancelled": True}))
            self.assertEqual(request({"op": "poll", "operation_id": "bad"})[0], 400)
            self.assertEqual(request({"op": "start", "args": {}, "extra": True})[0], 400)
            self.assertEqual(request({"op": "unknown"})[0], 400)
            with mock.patch.object(server, "_IPC_MODE", True), \
                    mock.patch.object(server, "start_pdf_preview") as start:
                self.assertEqual(request({"op": "start", "args": {}})[0], 403)
                start.assert_not_called()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
