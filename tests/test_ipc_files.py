"""Focused contracts for native managed-artifact export.

These tests deliberately exercise the worker boundary without changing the
export implementation.  The fixture is a synthetic managed SCM checkout; no
real user data or checkout settings are touched.
"""

from __future__ import annotations

import json
import os
import re
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


class ArtifactExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-files-")
        self.root = Path(self.temp.name)
        self.fixture = Phase0Fixture(self.root)
        self.data = self.root / "data"
        self.data.mkdir()
        self.old_paths = {name: getattr(server, name) for name in (
            "DATA_DIR", "UI_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
            "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE",
        )}
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
        self.old_jobs = server.JOBS
        server.JOBS = {}
        with server.ARTIFACT_GRANTS_LOCK:
            server.ARTIFACT_GRANTS.clear()
        with server.EXPORTS_LOCK:
            server.EXPORTS.clear()

    def tearDown(self):
        server.JOBS = self.old_jobs
        with server.ARTIFACT_GRANTS_LOCK:
            server.ARTIFACT_GRANTS.clear()
        with server.EXPORTS_LOCK:
            server.EXPORTS.clear()
        for name, value in self.old_paths.items():
            setattr(server, name, value)
        self.temp.cleanup()

    def job(self, *, kind="create_pdf", output="game/output/result.pdf", status="ok", job_id="job"):
        return {
            "id": job_id, "kind": kind, "status": status,
            "scm_path": str(self.fixture.scm),
            "args": {"output_path": output},
            "ts": 1, "title": "Fixture", "cmd": "fixture", "exit_code": 0,
        }

    def snapshot(self, job=None, content=b"%PDF-1.7 fixture\n"):
        job = job or self.job()
        path = Path(job["scm_path"]) / job["args"]["output_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        result = server._snapshot_artifacts(job)
        self.assertEqual(len(result), 1)
        return job, path, result[0]

    def grant(self, *, job=None, content=b"%PDF-1.7 fixture\n"):
        job, path, snap = self.snapshot(job, content)
        job["artifact_snapshots"] = [snap]
        server.JOBS[job["id"]] = job
        ids = server._grants_for_job(job)
        self.assertEqual(len(ids), 1)
        self.assertRegex(ids[0], r"^[0-9a-f]{64}$")
        return job, path, snap, ids[0]

    @staticmethod
    def wait_operation(operation_id, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = server.export_poll(operation_id)
            if result["done"]:
                return result["result"]
            time.sleep(.005)
        raise AssertionError("artifact export did not finish")

    def test_snapshot_only_successful_expected_managed_pdf(self):
        job, path, snap = self.snapshot()
        self.assertEqual(snap["path"], str(path.resolve()))
        self.assertEqual(snap["root"], str(self.fixture.scm.resolve()))
        self.assertEqual(snap["size"], path.stat().st_size)
        self.assertEqual(server._snapshot_artifacts(self.job(status="fail")), [])
        self.assertEqual(server._snapshot_artifacts(self.job(kind="fetch:mtg")), [])

        external = self.root / "outside.pdf"
        external.write_bytes(b"outside")
        self.assertEqual(server._snapshot_artifacts(self.job(output=str(external))), [])
        self.assertEqual(server._snapshot_artifacts(self.job(output="game/output/missing.pdf")), [])
        self.assertEqual(server._snapshot_artifacts(self.job(output="game/output")), [])
        for name in ("", ".", ".."):
            with self.assertRaises(server.ArtifactExportError):
                server._artifact_name(name)

    def test_snapshot_rejects_symlink_directory_and_oversized_candidates(self):
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"outside")
        link = self.fixture.scm / "game/output/linked.pdf"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(server._snapshot_artifacts(self.job(output="game/output/linked.pdf")), [])

        directory = self.fixture.scm / "game/output/dir.pdf"
        directory.mkdir()
        self.assertEqual(server._snapshot_artifacts(self.job(output="game/output/dir.pdf")), [])

        huge = self.fixture.scm / "game/output/huge.pdf"
        huge.write_bytes(b"x" * 2)
        with mock.patch.object(server, "ARTIFACT_MAX_BYTES", 1):
            self.assertEqual(server._snapshot_artifacts(self.job(output="game/output/huge.pdf")), [])

    def test_snapshot_captures_offset_and_calibration_pdf_families_only(self):
        offset = self.job(kind="offset_pdf")
        offset["args"] = {"pdf_path": "game/output/source.pdf"}
        source = self.fixture.scm / "game/output/source.pdf"
        source.write_bytes(b"source")
        output = self.fixture.scm / "game/output/source_offset.pdf"
        output.write_bytes(b"offset")
        self.assertEqual(len(server._snapshot_artifacts(offset)), 1)

        images = self.job()
        images["args"]["output_images"] = True
        self.assertEqual(server._snapshot_artifacts(images), [])

        calibration = self.job(kind="calibration")
        calibration["args"] = {}
        cal = self.fixture.scm / "calibration"
        cal.mkdir()
        (cal / "letter-calibration.pdf").write_bytes(b"cal")
        (cal / "notes.txt").write_text("not an artifact")
        self.assertEqual([x["name"] for x in server._snapshot_artifacts(calibration)], ["letter-calibration.pdf"])

    def test_snapshot_uses_job_checkout_not_current_settings_and_old_file_baseline(self):
        job, path, snap = self.snapshot()
        other = self.root / "other-scm"
        other.mkdir()
        server.save_settings({**server.load_settings(), "scm_dir": str(other)})
        self.assertEqual(server.job_outputs(job), [str(path)])
        self.assertEqual(server._snapshot_artifacts(job), [snap])

        job["artifact_before"] = {str(path.resolve()): server._artifact_identity(path.stat())}
        self.assertEqual(server._snapshot_artifacts(job), [])

    def test_persisted_snapshots_mint_fresh_grants_but_legacy_rows_do_not(self):
        job, path, snap = self.snapshot()
        job.update(duration=0, started=1, ended=1)
        job["artifact_snapshots"] = [snap]
        server.JOBS[job["id"]] = job
        self.assertTrue(server._persist_jobs(strict=True))
        server.JOBS.clear()
        listed = server.list_jobs()["jobs"]
        self.assertEqual(listed[0]["outputs"], [snap["path"]])
        # Immutable snapshots are persisted for display and are sufficient to
        # mint a fresh process-local handle after restart. The handle itself is
        # not serialized in jobs.json.
        fresh = listed[0]["save_grants"]
        self.assertEqual(len(fresh), 1)
        self.assertNotIn("_save_grants", json.loads(server.JOBS_FILE.read_text())[0])
        self.assertIn(fresh[0], server.ARTIFACT_GRANTS)

        old = dict(job, id="old-without-snapshot")
        old.pop("artifact_snapshots", None)
        old.pop("_save_grants", None)
        persisted = json.loads(server.JOBS_FILE.read_text())
        persisted.append(old)
        server.JOBS_FILE.write_text(json.dumps(persisted), encoding="utf-8")
        old_row = next(row for row in server.list_jobs()["jobs"] if row["id"] == old["id"])
        self.assertIsNone(old_row["save_grants"])

    def test_grants_are_opaque_ttl_bounded_and_memoized(self):
        job, path, snap, grant = self.grant()
        exposed = server.list_jobs()["jobs"][0]
        self.assertEqual(set(exposed), {"id", "ts", "kind", "title", "status", "exit_code", "cmd", "warnings", "outputs", "save_grants"})
        self.assertEqual(exposed["save_grants"], [grant])
        self.assertNotIn(str(path), grant)
        self.assertEqual(server._grants_for_job(job), [grant])
        self.assertEqual(len(server.ARTIFACT_GRANTS), 1)

        with server.ARTIFACT_GRANTS_LOCK:
            server.ARTIFACT_GRANTS[grant]["expires"] = time.time() - 1
        # Expiration invalidates that opaque token, but a fully revalidated
        # immutable snapshot can receive a distinct fresh token.
        replacement = server._grants_for_job(job)
        self.assertEqual(len(replacement), 1)
        self.assertNotEqual(replacement[0], grant)
        with self.assertRaises(server.ArtifactExportError):
            server._resolve_artifact_grant(grant)

        # A fresh terminal job creates a new handle; polling/listing the same
        # object never turns one grant into an unbounded stream.
        _, _, _, new_grant = self.grant(job=self.job(job_id="new"))
        self.assertNotEqual(grant, new_grant)
        for _ in range(4):
            self.assertEqual(server._grants_for_job(server.JOBS["new"]), [new_grant])
        self.assertLessEqual(len(server.ARTIFACT_GRANTS), server.ARTIFACT_GRANT_MAX)

    def test_grant_count_eviction_and_wrong_ids_fail_closed(self):
        jobs = []
        for i in range(server.ARTIFACT_GRANT_MAX):
            job, _, _, grant = self.grant(job=self.job(job_id=f"j{i}", output=f"game/output/{i}.pdf"), content=str(i).encode())
            jobs.append((job, grant))
        self.assertEqual(len(server.ARTIFACT_GRANTS), server.ARTIFACT_GRANT_MAX)
        extra = self.job(job_id="over-capacity", output="game/output/over-capacity.pdf")
        _, _, snap = self.snapshot(extra, b"over")
        extra["artifact_snapshots"] = [snap]
        server.JOBS[extra["id"]] = extra
        self.assertIsNone(server._grants_for_job(extra))
        self.assertLessEqual(len(server.ARTIFACT_GRANTS), server.ARTIFACT_GRANT_MAX)
        for bad in ("", "not-a-grant", "g" * 64, "0" * 63, "0" * 65, "../" + "0" * 61,
                    "f" * 64):
            with self.assertRaises(server.ArtifactExportError):
                server.export_selected(bad, str(self.root / "dest.pdf"))
        # Grants do not evict live tokens merely to make room; the next job
        # gets no grant and the bounded registry stays intact.
        self.assertIs(server._resolve_artifact_grant(jobs[0][1]), server.ARTIFACT_GRANTS[jobs[0][1]])
        self.assertIs(server._resolve_artifact_grant(jobs[-1][1]), server.ARTIFACT_GRANTS[jobs[-1][1]])

    def test_private_ipc_export_params_and_public_allowlist_are_exact(self):
        _, _, _, grant = self.grant()
        destination = str(self.root / "dest.pdf")
        with mock.patch.object(server, "export_selected", return_value={"operation_id": "a" * 32}) as export:
            response = ipc.dispatch({"id": "x", "method": "files.export_selected", "params": {"grant_id": grant, "destination": destination}})
        self.assertEqual(response, {"id": "x", "ok": True, "result": {"operation_id": "a" * 32}})
        export.assert_called_once_with(grant, destination)
        for method, params in (("files.export_poll", {"operation_id": "a" * 32}), ("files.export_cancel", {"operation_id": "a" * 32})):
            with mock.patch.object(server, method.replace("files.", ""), return_value={"done": True}):
                result = ipc.dispatch({"id": "x", "method": method, "params": params})
            self.assertTrue(result["ok"])
        for params in ({}, {"grant_id": grant}, {"grant_id": grant, "destination": destination, "extra": 1}):
            self.assertEqual(ipc.dispatch({"id": "x", "method": "files.export_selected", "params": params})["error"]["code"], "bad_request")

        rust = Path(__file__).parents[1] / "tauri/src/ipc.rs"
        source = rust.read_text(encoding="utf-8")
        self.assertIn('pub(crate) fn export_selected_artifact', source)
        allowlist = source[source.index("fn validate_method"):source.index("fn encode_request")]
        for private in ("files.export_selected", "files.export_poll", "files.export_cancel"):
            self.assertNotIn(private, allowlist)

    def test_source_open_rejects_symlink_directory_and_changed_root(self):
        _, source, snap, _ = self.grant(content=b"source")
        outside = self.root / "outside-source.pdf"
        outside.write_bytes(b"outside")
        link = self.fixture.scm / "game/output/source-link.pdf"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        linked = dict(snap, path=str(link))
        with self.assertRaises(server.ArtifactExportError):
            server._open_artifact_source(linked)
        with self.assertRaises(server.ArtifactExportError):
            server._open_artifact_source(dict(snap, path=str(source.parent)))

        replacement = self.root / "replacement-root"
        replacement.mkdir()
        changed = dict(snap, root=str(replacement), path=str(source))
        with self.assertRaises((server.ArtifactExportError, OSError)):
            server._open_artifact_source(changed)

    def test_persisted_snapshot_root_must_match_jobs_pinned_checkout(self):
        job, _, _ = self.snapshot()
        outside_root = self.root / "outside-root"
        outside_root.mkdir()
        outside = outside_root / "forged.pdf"
        outside.write_bytes(b"forged")
        forged = {
            "path": str(outside),
            "root": str(outside_root),
            "name": outside.name,
            "root_identity": server._artifact_identity(outside_root.stat()),
            **server._artifact_identity(outside.stat()),
        }
        job["artifact_snapshots"] = [forged]
        server.JOBS[job["id"]] = job
        self.assertIsNone(server._grants_for_job(job))

    def test_persisted_snapshot_cannot_traverse_outside_pinned_root(self):
        job, _, snap = self.snapshot()
        outside = self.fixture.scm.parent / "outside.pdf"
        outside.write_bytes(b"outside")
        malicious = dict(
            snap,
            path=str(self.fixture.scm / ".." / outside.name),
            **server._artifact_identity(outside.stat()),
        )
        job["artifact_snapshots"] = [malicious]
        server.JOBS[job["id"]] = job
        self.assertIsNone(server._grants_for_job(job))
        with self.assertRaises(server.ArtifactExportError):
            server._open_artifact_source(malicious)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor cleanup")
    def test_source_root_fd_closes_when_initial_fstat_fails(self):
        _, _, snap = self.snapshot()
        opened = []
        real_open = server.os.open
        real_fstat = server.os.fstat

        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with mock.patch.object(server.os, "open", side_effect=record_open), \
             mock.patch.object(server.os, "fstat", side_effect=OSError("fstat failed")):
            with self.assertRaises(OSError):
                server._open_artifact_source(snap)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            real_fstat(opened[0])

    @unittest.skipIf(os.name == "nt", "portable component seam runs on POSIX")
    def test_windows_component_validator_rejects_parent_reparse(self):
        _, source, snap = self.snapshot()
        real_parent = source.parent
        moved_parent = real_parent.with_name("real-output")
        real_parent.rename(moved_parent)
        real_parent.symlink_to(moved_parent, target_is_directory=True)
        try:
            with self.assertRaises(server.ArtifactExportError):
                server._validate_windows_artifact_components(
                    self.fixture.scm, source.relative_to(self.fixture.scm).parts, snap)
        finally:
            real_parent.unlink()
            moved_parent.rename(real_parent)

    @unittest.skipUnless(os.name == "nt", "Windows parent-handle fence")
    def test_windows_destination_parent_is_pinned_against_rename(self):
        parent = self.root / "windows-parent"
        parent.mkdir()
        handles = server._open_windows_artifact_parent(parent)
        try:
            with self.assertRaises(OSError):
                parent.rename(self.root / "windows-parent-moved")
        finally:
            server._close_windows_handles(handles)
        parent.rename(self.root / "windows-parent-moved")

    def test_export_is_async_with_bounded_poll_shape_and_unpredictable_ids(self):
        _, _, _, grant = self.grant()
        destination = self.root / "dest.pdf"
        started = threading.Event()
        release = threading.Event()
        original = server._copy_artifact

        def blocked(snapshot, dest, cancelled=None):
            started.set()
            release.wait(1)
            return original(snapshot, dest, cancelled)

        with mock.patch.object(server, "_copy_artifact", side_effect=blocked):
            first = server.export_selected(grant, str(destination))
            second_job, _, _, second_grant = self.grant(job=self.job(job_id="second", output="game/output/other.pdf"))
            second = server.export_selected(second_grant, str(self.root / "other.pdf"))
            self.assertRegex(first["operation_id"], r"^[0-9a-f]{32}$")
            self.assertRegex(second["operation_id"], r"^[0-9a-f]{32}$")
            self.assertNotEqual(first["operation_id"], second["operation_id"])
            self.assertEqual(server.export_poll(first["operation_id"]), {"done": False})
            for bad in ("", "x", "0" * 31, "0" * 33):
                with self.assertRaises(server.ArtifactExportError):
                    server.export_poll(bad)
            self.assertEqual(set(server.export_poll(first["operation_id"])), {"done"})
            self.assertTrue(started.wait(1))
            release.set()
            self.wait_operation(first["operation_id"])
            self.wait_operation(second["operation_id"])

    def test_failed_copy_can_retry_but_success_consumes_grant_once(self):
        _, _, _, grant = self.grant()
        destination = self.root / "retry.pdf"
        with mock.patch.object(server, "_copy_artifact", return_value={"ok": False, "errors": ["copy failed"]}):
            op = server.export_selected(grant, str(destination))["operation_id"]
            self.assertEqual(self.wait_operation(op), {"ok": False, "errors": ["copy failed"]})
        self.assertIn(grant, server.ARTIFACT_GRANTS)

        with mock.patch.object(server, "_copy_artifact", return_value={"ok": True, "dest": str(destination), "name": "retry.pdf", "bytes": 1}):
            op = server.export_selected(grant, str(destination))["operation_id"]
            self.assertTrue(self.wait_operation(op)["ok"])
        with self.assertRaises(server.ArtifactExportError):
            server.export_selected(grant, str(self.root / "again.pdf"))

    def test_active_saturation_cancel_and_retained_operation_bounds(self):
        _, _, _, grant = self.grant()
        with server.EXPORTS_LOCK:
            for i in range(server.ARTIFACT_EXPORT_MAX_ACTIVE):
                server.EXPORTS[f"{i:032x}"] = {"done": False, "expires": time.time() + 60}
        with self.assertRaises(server.ArtifactExportError):
            server.export_selected(grant, str(self.root / "full.pdf"))
        with server.EXPORTS_LOCK:
            server.EXPORTS.clear()
        with mock.patch.object(server.EXPORT_EXECUTOR, "submit") as submit:
            op = server.export_selected(grant, str(self.root / "cancel.pdf"))["operation_id"]
            self.assertTrue(submit.called)
            cancelled = server.export_cancel(op)
            self.assertEqual(cancelled, {"done": False})
            self.assertEqual(server.export_poll(op), {"done": False})
            self.assertEqual(server.EXPORTS[op]["result"], {"ok": False, "errors": ["export cancelled"]})
        # Reach the retention path through ordinary completed exports. The
        # grant cap is widened only for this bounded-registry test; production
        # grant admission remains independently capped at 32.
        with mock.patch.object(server, "ARTIFACT_GRANT_MAX", 64), \
                mock.patch.object(server, "_copy_artifact", return_value={"ok": True, "dest": "x", "name": "x.pdf", "bytes": 1}):
            for i in range(server.ARTIFACT_EXPORT_MAX_RETAINED + 5):
                _, _, _, extra_grant = self.grant(job=self.job(job_id=f"retained-{i}", output=f"game/output/retained-{i}.pdf"))
                operation = server.export_selected(extra_grant, str(self.root / f"retained-{i}.pdf"))["operation_id"]
                self.wait_operation(operation)
        with server.EXPORTS_LOCK:
            self.assertLessEqual(len(server.EXPORTS), server.ARTIFACT_EXPORT_MAX_RETAINED)

    def test_poll_and_error_results_are_bounded(self):
        huge = "x" * 10000
        self.assertLessEqual(len(server.ArtifactExportError(huge).message), 256)
        self.assertLessEqual(len(json.dumps(server._grant_error(huge)).encode()), 11000)
        with self.assertRaises(server.ArtifactExportError):
            server.export_poll("0" * 31)
        with server.EXPORTS_LOCK:
            server.EXPORTS["f" * 32] = {"done": True, "result": server._grant_error("done"), "expires": time.time() + 60}
        self.assertEqual(server.export_poll("f" * 32)["done"], True)

    def test_completed_operation_ttl_is_removed_but_active_operation_is_retained(self):
        _, _, _, grant = self.grant()
        with mock.patch.object(server.EXPORT_EXECUTOR, "submit"):
            op = server.export_selected(grant, str(self.root / "pending.pdf"))["operation_id"]
        with server.EXPORTS_LOCK:
            server.EXPORTS[op]["expires"] = time.time() - 1
        # An in-flight operation is retained until its worker publishes a
        # result; expiring it cannot make a queued operation unobservable.
        self.assertEqual(server.export_poll(op), {"done": False})
        with server.EXPORTS_LOCK:
            server.EXPORTS[op].update(done=True, result={"ok": False, "errors": ["failed"]}, expires=time.time() - 1)
        with self.assertRaises(server.ArtifactExportError):
            server.export_poll(op)

    def test_copy_exact_bytes_collision_no_overwrite_and_no_parent_creation(self):
        _, source, snap, _ = self.grant(content=b"exact\x00bytes\n" * 100)
        parent = self.root / "chosen"
        parent.mkdir()
        (parent / "out.pdf").write_bytes(b"old")
        result = server._copy_artifact(snap, str(parent / "out.pdf"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "out (2).pdf")
        self.assertEqual((parent / "out.pdf").read_bytes(), b"old")
        self.assertEqual((parent / "out (2).pdf").read_bytes(), source.read_bytes())
        missing_parent = self.root / "does-not-exist" / "out.pdf"
        failed = server._copy_artifact(snap, str(missing_parent))
        self.assertFalse(failed["ok"])
        self.assertFalse(missing_parent.parent.exists())

    def test_destination_name_path_controls_reserved_parent_and_final_symlink(self):
        _, _, snap, _ = self.grant()
        parent = self.root / "dest"
        parent.mkdir()
        bad_names = ("a/b.pdf", r"a\\b.pdf", "a:b.pdf", "CON.pdf", "report. ", "bad\n.pdf", "é" * 256)
        for name in bad_names:
            result = server._copy_artifact(snap, str(parent / name))
            self.assertFalse(result["ok"], name)
        for destination in (str(parent / ("x" * 4097)),):
            result = server._copy_artifact(snap, destination)
            self.assertFalse(result["ok"])

        final_link = parent / "linked.pdf"
        outside = self.root / "outside-destination.pdf"
        outside.write_bytes(b"must remain")
        try:
            final_link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        result = server._copy_artifact(snap, str(final_link))
        self.assertTrue(result["ok"])
        self.assertEqual(outside.read_bytes(), b"must remain")
        self.assertEqual((parent / "linked (2).pdf").read_bytes(), Path(snap["path"]).read_bytes())

        link_parent = self.root / "link-parent"
        try:
            link_parent.symlink_to(parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        self.assertFalse(server._copy_artifact(snap, str(link_parent / "new.pdf"))["ok"])

    def test_copy_detects_source_shrink_grow_and_mutation_and_cleans_temps(self):
        _, source, snap, _ = self.grant(content=b"a" * (server.ARTIFACT_IO_CHUNK * 2))
        parent = self.root / "copy"
        parent.mkdir()
        original_read = server.os.read

        def run_mutation(action):
            calls = {"n": 0}
            def read(fd, size):
                value = original_read(fd, size)
                if value and calls["n"] == 0:
                    calls["n"] += 1
                    action()
                return value
            with mock.patch.object(server.os, "read", side_effect=read):
                result = server._copy_artifact(snap, str(parent / f"{calls['n']}.pdf"))
            self.assertFalse(result["ok"])
            self.assertEqual(list(parent.glob(server.ARTIFACT_EXPORT_PREFIX + "*")), [])

        run_mutation(lambda: source.write_bytes(b"a"))
        source.write_bytes(b"a" * (server.ARTIFACT_IO_CHUNK * 2))
        run_mutation(lambda: source.write_bytes(source.read_bytes() + b"grow"))
        source.write_bytes(b"a" * (server.ARTIFACT_IO_CHUNK * 2))
        run_mutation(lambda: source.write_bytes(b"b" * (server.ARTIFACT_IO_CHUNK * 2)))

    def test_write_and_publish_failures_leave_destination_and_temps_clean(self):
        _, _, snap, _ = self.grant(content=b"bytes")
        parent = self.root / "failures"
        parent.mkdir()
        real_write = server.os.write
        with mock.patch.object(server.os, "write", side_effect=OSError("write failed")):
            result = server._copy_artifact(snap, str(parent / "write.pdf"))
        self.assertFalse(result["ok"])
        self.assertFalse((parent / "write.pdf").exists())
        self.assertEqual(list(parent.glob(server.ARTIFACT_EXPORT_PREFIX + "*")), [])

        with mock.patch.object(server.os, "link", side_effect=OSError("publish failed")):
            result = server._copy_artifact(snap, str(parent / "publish.pdf"))
        self.assertFalse(result["ok"])
        self.assertFalse((parent / "publish.pdf").exists())
        self.assertEqual(list(parent.glob(server.ARTIFACT_EXPORT_PREFIX + "*")), [])
        self.assertIsNotNone(real_write)

    def test_posix_parent_fd_survives_parent_swap(self):
        if os.name == "nt":
            self.skipTest("POSIX dirfd contract")
        _, _, snap, _ = self.grant(content=b"dirfd bytes")
        parent = self.root / "stable-parent"
        parent.mkdir()
        old_parent = self.root / "renamed-parent"
        replacement = self.root / "stable-parent-replacement"
        original_link = server.os.link
        swapped = {"done": False}

        def swap_then_link(src, dst, **kwargs):
            if not swapped["done"] and dst == "dirfd.pdf":
                swapped["done"] = True
                parent.rename(old_parent)
                replacement.mkdir()
            return original_link(src, dst, **kwargs)

        with mock.patch.object(server.os, "link", side_effect=swap_then_link):
            result = server._copy_artifact(snap, str(parent / "dirfd.pdf"))
        self.assertTrue(result["ok"])
        self.assertEqual((old_parent / "dirfd.pdf").read_bytes(), b"dirfd bytes")
        self.assertFalse((replacement / "dirfd.pdf").exists())

    def test_no_posix_parent_symlink_following_and_external_race_get_suffix(self):
        _, _, snap, _ = self.grant(content=b"race")
        parent = self.root / "race"
        parent.mkdir()
        real_link = server.os.link
        raced = {"done": False}

        def race(src, dst, **kwargs):
            if not raced["done"] and dst == "race.pdf":
                raced["done"] = True
                (parent / "race.pdf").write_bytes(b"external")
                raise FileExistsError
            return real_link(src, dst, **kwargs)

        with mock.patch.object(server.os, "link", side_effect=race):
            result = server._copy_artifact(snap, str(parent / "race.pdf"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "race (2).pdf")
        self.assertEqual((parent / "race.pdf").read_bytes(), b"external")

        outside = self.root / "safe-parent"
        outside.mkdir()
        swapped = self.root / "swapped"
        swapped.mkdir()
        link = self.root / "parent-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertFalse(server._copy_artifact(snap, str(link / "x.pdf"))["ok"])

    def test_http_standalone_parity_and_ipc_mode_fail_close(self):
        source = self.data / "output.pdf"
        source.write_bytes(b"standalone")
        destination = self.root / "http-dest"
        destination.mkdir()
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            request = urllib.request.Request(base + "/api/files/save", data=json.dumps({"src": str(source), "dest": str(destination / "x.pdf")}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=3) as response:
                body = json.loads(response.read())
            self.assertEqual(body["ok"], True)
            self.assertEqual((destination / "x.pdf").read_bytes(), b"standalone")

            with mock.patch.object(server, "_IPC_MODE", True):
                request = urllib.request.Request(base + "/api/files/save", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(raised.exception.code, 403)
                self.assertIn("native artifact export", raised.exception.read().decode())
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(timeout=2)

    def test_jobs_list_parallel_shape_has_exact_outputs_and_grants(self):
        job, path, snap, grant = self.grant()
        server.JOBS["unrelated"] = {"id": "unrelated", "ts": 2, "kind": "other", "title": "Other", "status": "ok", "exit_code": 0, "cmd": "other", "warnings": []}
        listed = server.list_jobs()
        rows = {row["id"]: row for row in listed["jobs"]}
        self.assertEqual(rows["job"]["outputs"], [str(path.resolve())])
        self.assertEqual(rows["job"]["save_grants"], [grant])
        self.assertEqual(rows["unrelated"]["outputs"], [])
        self.assertIsNone(rows["unrelated"]["save_grants"])
        self.assertEqual(set(rows["job"]), {"id", "ts", "kind", "title", "status", "exit_code", "cmd", "warnings", "outputs", "save_grants"})

    def test_grant_invalidates_on_job_status_or_source_change(self):
        job, source, _, grant = self.grant()
        job["status"] = "fail"
        with self.assertRaises(server.ArtifactExportError):
            server.export_selected(grant, str(self.root / "x.pdf"))
        _, source, _, grant = self.grant(job=self.job(job_id="changed", output="game/output/changed.pdf"))
        source.write_bytes(b"mutated")
        with self.assertRaises(server.ArtifactExportError):
            server.export_selected(grant, str(self.root / "changed.pdf"))


if __name__ == "__main__":
    unittest.main()
