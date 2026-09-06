"""Exhaustive contracts for the Workbench-owned offset state and projection."""

import io
import json
import math
import os
import subprocess
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


class _PipeProc:
    def __init__(self, output=b"", rc=0):
        self.stdout = io.BytesIO(output)
        self.rc = rc
        self.pid = 999999
        self.terminated = False

    def wait(self):
        return self.rc

    def terminate(self):
        self.terminated = True


class OffsetTests(unittest.TestCase):
    """Each test owns a complete synthetic checkout and restores module state."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-offsets-")
        self.root = Path(self.temp.name)
        self.fixture = Phase0Fixture(self.root)
        layouts_path = self.fixture.scm / "assets" / "layouts.json"
        layouts = json.loads(layouts_path.read_text(encoding="utf-8"))
        layouts["paper_sizes"].update({
            "A4": {"width": "210mm", "height": "297mm"},
            "tabloid": {"width": "11in", "height": "17in"},
        })
        layouts["layouts"].update({
            "A4": {"standard": {"default": {}}},
            "tabloid": {"standard": {"default": {}}},
        })
        layouts_path.write_text(json.dumps(layouts), encoding="utf-8")
        self.data = self.fixture.data
        self.state_path = self.data / "offset_state.json"
        self.upstream = self.fixture.scm / "data" / "offset_data.json"

        names = ("DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
                 "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE", "OFFSET_STATE_FILE",
                 "_INITIAL_OFFSET_DATA_DIR", "JOBS")
        self.old_globals = {name: getattr(server, name) for name in names}
        self.old_env = os.environ.get("SCM_WORKBENCH_DATA")
        self.old_manifest = dict(server.MANIFEST_CACHE)
        self.old_info = dict(server._INFO_SNAP)
        self.old_mtimes = dict(server._REPOS_MTIME)
        os.environ["SCM_WORKBENCH_DATA"] = str(self.data)
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = self.data / "offsets_by_size.json"
        server.UPDATE_STATE_FILE = self.data / "updates.json"
        server.OFFSET_STATE_FILE = self.state_path
        server._INITIAL_OFFSET_DATA_DIR = self.data
        server.JOBS = {}
        server.MANIFEST_CACHE.clear()
        server._INFO_SNAP.clear()
        server._REPOS_MTIME.clear()
        server.save_settings({
            **json.loads(json.dumps(server.DEFAULT_SETTINGS)),
            "scm_dir": str(self.fixture.scm),
            "extras_dir": str(self.fixture.extras),
        })
        # A failed test must not poison the process-wide non-blocking lease.
        if server.OFFSET_LEASE.locked():
            server.OFFSET_LEASE.release()

    def tearDown(self):
        try:
            server.stop_all_jobs(timeout=1)
        except Exception:
            pass
        if server.OFFSET_LEASE.locked():
            server.OFFSET_LEASE.release()
        server.MANIFEST_CACHE.clear()
        server.MANIFEST_CACHE.update(self.old_manifest)
        server._INFO_SNAP.clear()
        server._INFO_SNAP.update(self.old_info)
        server._REPOS_MTIME.clear()
        server._REPOS_MTIME.update(self.old_mtimes)
        for name, value in self.old_globals.items():
            setattr(server, name, value)
        if self.old_env is None:
            os.environ.pop("SCM_WORKBENCH_DATA", None)
        else:
            os.environ["SCM_WORKBENCH_DATA"] = self.old_env
        self.temp.cleanup()

    def native(self, method, params):
        with mock.patch.object(ipc.sys, "stderr", io.StringIO()):
            return ipc.dispatch({"id": "offset-test", "method": method, "params": params})

    def write_upstream(self, x=0, y=0, angle=0):
        self.upstream.parent.mkdir(parents=True, exist_ok=True)
        self.upstream.write_text(json.dumps({
            "x_offset": x, "y_offset": y, "angle_offset": angle,
        }), encoding="utf-8")

    def write_state(self, value):
        self.state_path.write_text(json.dumps(value), encoding="utf-8")

    def reset_state(self):
        for path in (self.state_path, server.PER_SIZE_OFFSETS_FILE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def start_http(self):
        httpd = server.start_http("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread, "http://127.0.0.1:%d" % httpd.server_address[1]

    @staticmethod
    def http_post(base, body):
        request = urllib.request.Request(
            base + "/api/offset", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_native_exact_envelopes_and_strict_param_shapes(self):
        result = self.native("offset.set", {"size": "A4", "x": 12, "y": -3, "angle": 1.5})
        self.assertEqual(result, {"id": "offset-test", "ok": True, "result": {
            "ok": True, "size": "A4", "staged": True,
            "offset": {"x_offset": 12, "y_offset": -3, "angle_offset": 1.5},
        }})
        result = self.native("offset.delete", {"size": "A4"})
        self.assertEqual(result, {"id": "offset-test", "ok": True, "result": {
            "ok": True, "removed": "A4",
        }})
        for method, params in (
            ("offset.set", {"size": "A4", "x": 1, "y": 2, "angle": 3, "extra": 0}),
            ("offset.set", {"size": "A4", "x": 1, "y": 2}),
            ("offset.delete", {"size": "A4", "extra": 0}),
            ("offset.delete", {"size": 3}),
        ):
            response = self.native(method, params)
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "bad_request")

    def test_http_and_native_global_per_size_and_delete_parity(self):
        httpd, thread, base = self.start_http()
        try:
            operations = [
                ({"x": "4", "y": "-2", "angle": "0.5"},
                 {"size": None, "x": 4, "y": -2, "angle": .5}),
                ({"size": "A4", "x": 8, "y": 9, "angle": 2},
                 {"size": "A4", "x": 8, "y": 9, "angle": 2}),
            ]
            for http_body, native_params in operations:
                self.reset_state()
                self.write_upstream()
                status, http_result = self.http_post(base, http_body)
                self.assertEqual(status, 200)
                self.reset_state()
                self.write_upstream()
                native_result = self.native("offset.set", native_params)["result"]
                self.assertEqual(http_result, native_result)

            self.reset_state()
            self.native("offset.set", {"size": "A4", "x": 8, "y": 9, "angle": 2})
            status, http_result = self.http_post(base, {"size": "A4", "delete": True})
            self.assertEqual(status, 200)
            self.reset_state()
            self.native("offset.set", {"size": "A4", "x": 8, "y": 9, "angle": 2})
            native_result = self.native("offset.delete", {"size": "A4"})["result"]
            self.assertEqual(http_result, native_result)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)

    def test_start_job_blank_paper_uses_global_baseline_before_child_and_records_pending(self):
        class BlockingReader:
            def __init__(self):
                self.release = threading.Event()

            def readline(self):
                self.release.wait(5)
                return b""

        class ControlledProc:
            def __init__(self, reader):
                self.stdout = reader
                self.pid = 999999

            def wait(self):
                return 0

            def terminate(self):
                self.reader.release.set()

        baseline = {"x": 10, "y": 11, "angle": 1.5}
        row = {"x": 20, "y": 21, "angle": 2.5}
        for save, global_row in ((False, None), (True, baseline)):
            self.write_state({"version": 1, "global": global_row,
                              "rows": {"A4": row}, "staged_size": "A4"})
            self.write_upstream(row["x"], row["y"], row["angle"])
            reader = BlockingReader()
            process = ControlledProc(reader)
            process.reader = reader
            seen_by_spawn = []
            expected_projection = (None if global_row is None else {
                "x_offset": global_row["x"], "y_offset": global_row["y"],
                "angle_offset": global_row["angle"],
            })

            def spawn(*args, **kwargs):
                # Popen is the child boundary: inspect the projection and
                # pending record at the exact instant before it is launched.
                seen_by_spawn.append((
                    json.loads(self.upstream.read_text(encoding="utf-8")) if self.upstream.exists() else None,
                    server.load_offset_state(),
                ))
                return process

            try:
                with mock.patch.object(server.subprocess, "Popen", side_effect=spawn):
                    job, errors = server.start_job("offset_pdf", {
                        "paper_size": "", "x_offset": 7, "y_offset": "", "angle": "",
                        "save": save,
                    })
                self.assertFalse(errors)
                # The blank selection must undo the preceding A4 projection
                # synchronously, before the controlled child can finish.
                self.assertEqual(server.load_offset_state()["staged_size"], None)
                self.assertEqual(seen_by_spawn[0][0], expected_projection)
                projected = json.loads(self.upstream.read_text(encoding="utf-8")) if self.upstream.exists() else None
                self.assertEqual(projected, expected_projection)
                pending = seen_by_spawn[0][1].get("pending")
                if save:
                    self.assertEqual(pending["target"], "global")
                    self.assertEqual(pending["prior_projection"], global_row)
                else:
                    self.assertIsNone(pending)
                self.assertTrue(server.OFFSET_LEASE.locked())
            finally:
                reader.release.set()
                if 'job' in locals() and job and job.get("pump_thread"):
                    job["pump_thread"].join(timeout=3)
                self.assertFalse(server.OFFSET_LEASE.locked())
                if 'job' in locals():
                    del job

    def test_bounds_nonfinite_bool_unknown_and_invalid_inputs_do_not_mutate(self):
        self.write_upstream(21, 22, 2.25)
        self.write_state({"version": 1, "global": {"x": 21, "y": 22, "angle": 2.25},
                          "rows": {}, "staged_size": None})
        before = (self.state_path.read_bytes(), self.upstream.read_bytes())
        bad = [
            {"size": "A4", "x": True, "y": 0, "angle": 0},
            {"size": "A4", "x": 100001, "y": 0, "angle": 0},
            {"size": "A4", "x": 0, "y": -100001, "angle": 0},
            {"size": "A4", "x": 0, "y": 0, "angle": float("nan")},
            {"size": "A4", "x": 0, "y": 0, "angle": float("inf")},
            {"size": "A4", "x": 0, "y": 0, "angle": True},
            {"size": "not-a-paper", "x": 0, "y": 0, "angle": 0},
            {"size": "A" * (server.OFFSET_NAME_MAX_BYTES + 1), "x": 0, "y": 0, "angle": 0},
            {"size": "bad\nname", "x": 0, "y": 0, "angle": 0},
        ]
        for params in bad:
            response = self.native("offset.set", params)
            if params["size"] == "not-a-paper":
                self.assertTrue(response["ok"], params)
                self.assertFalse(response["result"]["ok"])
                continue
            self.assertFalse(response["ok"], params)
            self.assertEqual(response["error"]["code"], "bad_request")
        self.assertEqual((self.state_path.read_bytes(), self.upstream.read_bytes()), before)
        response = self.native("offset.delete", {"size": "not-a-paper"})
        self.assertTrue(response["ok"])
        self.assertFalse(response["result"]["ok"])
        self.assertEqual((self.state_path.read_bytes(), self.upstream.read_bytes()), before)

    def test_known_papers_and_missing_scm_are_application_errors(self):
        self.assertTrue(self.native("offset.set", {"size": "tabloid", "x": 1, "y": 2, "angle": 3})["result"]["ok"])
        self.assertEqual(self.native("offset.set", {"size": "made-up", "x": 1, "y": 2, "angle": 3})["result"]["errors"],
                         ["unknown paper size: “made-up”"])
        with mock.patch.object(server, "effective_dirs", return_value=(None, None)):
            response = server.offset_set(None, 1, 2, 3)
        self.assertEqual(response, {"ok": False, "errors": ["SCM repo not found — set it in Settings."]})

    def test_legacy_migration_and_canonical_sanitization_are_read_only_until_save(self):
        self.write_upstream(5, -6, 1.25)
        legacy = {
            "A4": {"x": 7, "y": 8, "angle": 2},
            "bad": {"x": True, "y": 0, "angle": 0},
            "too-big": {"x": 100001, "y": 0, "angle": 0},
        }
        server.PER_SIZE_OFFSETS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = server.load_offset_state()
        self.assertEqual(loaded, {"version": 1, "global": {"x": 5, "y": -6, "angle": 1.25},
                                   "rows": {"A4": {"x": 7, "y": 8, "angle": 2.0}}, "staged_size": None})
        self.assertFalse(self.state_path.exists())
        server.save_per_size_offsets(legacy)
        canonical = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(canonical["rows"], {"A4": {"x": 7, "y": 8, "angle": 2.0}})
        self.write_state({"global": {"x": 1, "y": 2, "angle": 3, "ignored": True},
                          "rows": {"A4": {"x": 4, "y": 5, "angle": 6, "extra": "x"},
                                   "bad": {"x": 1, "y": 2, "angle": math.nan}},
                          "staged_size": "bad", "unexpected": 9})
        self.assertEqual(server.load_offset_state(), {
            "version": 1, "global": {"x": 1, "y": 2, "angle": 3.0},
            "rows": {"A4": {"x": 4, "y": 5, "angle": 6.0}}, "staged_size": None})
        self.assertIn("unexpected", json.loads(self.state_path.read_text(encoding="utf-8")))

    def test_legacy_upstream_without_angle_migrates_and_survives_row_save_delete(self):
        # SCM releases before angle support wrote only the two translations;
        # that is a valid zero-angle baseline, not a malformed file.
        legacy_upstream = b'{"x_offset": 12, "y_offset": -13}\n'
        self.upstream.parent.mkdir(parents=True, exist_ok=True)
        self.upstream.write_bytes(legacy_upstream)
        server.PER_SIZE_OFFSETS_FILE.write_text(json.dumps({
            "A4": {"x": 20, "y": 21, "angle": 2},
        }), encoding="utf-8")
        self.assertEqual(server.load_offset_state()["global"], {"x": 12, "y": -13, "angle": 0.0})

        # The compatibility save migrates both the old row table and the
        # two-field upstream baseline into one strict canonical document.
        server.save_per_size_offsets(server._legacy_rows())
        state = server.load_offset_state()
        self.assertEqual(state["global"], {"x": 12, "y": -13, "angle": 0.0})
        self.assertEqual(state["rows"]["A4"], {"x": 20, "y": 21, "angle": 2.0})
        self.assertEqual(server.offset_delete("A4"), {"ok": True, "removed": "A4"})
        self.assertEqual(server.load_offset_state()["global"], {"x": 12, "y": -13, "angle": 0.0})
        self.assertEqual(server.load_offset_state()["rows"], {})
        self.assertEqual(self.upstream.read_bytes(), legacy_upstream)

    def test_upstream_requires_x_and_y_but_accepts_missing_legacy_angle(self):
        pending = {"target": "global", "intended": {"x": 7, "y": 8, "angle": 9.0},
                   "prior_projection": {"x": 1, "y": 2, "angle": 3.0}}
        canonical = {"version": 1, "global": {"x": 1, "y": 2, "angle": 3.0},
                     "rows": {}, "staged_size": None, "pending": pending}
        for upstream in (
            {"y_offset": 2, "angle_offset": 3},
            {"x_offset": 1, "angle_offset": 3},
        ):
            raw = json.dumps(upstream, separators=(",", ":")).encode()
            self.upstream.parent.mkdir(parents=True, exist_ok=True)
            self.upstream.write_bytes(raw)
            self.state_path.write_bytes(json.dumps(canonical, separators=(",", ":")).encode())
            state_before, upstream_before = self.state_path.read_bytes(), self.upstream.read_bytes()
            self.assertIsNone(server.read_global_offset(self.fixture.scm))
            projection, error = server._read_upstream_projection(self.fixture.scm)
            self.assertIsNone(projection)
            self.assertIn("malformed", error)
            recovery_error = server.recover_offset_projection(self.fixture.scm)
            self.assertIn("malformed", recovery_error)
            result = server.offset_set(None, 4, 5, 6)
            self.assertFalse(result["ok"])
            self.assertEqual(self.state_path.read_bytes(), state_before)
            self.assertEqual(self.upstream.read_bytes(), upstream_before)

        # The historical two-field shape is valid and normalizes angle to zero
        # across both direct readers and the legacy state view.
        self.state_path.unlink()
        self.upstream.write_text('{"x_offset": 4, "y_offset": 5}', encoding="utf-8")
        self.assertEqual(server.read_global_offset(self.fixture.scm), {"x": 4, "y": 5, "angle": 0.0})
        projection, error = server._read_upstream_projection(self.fixture.scm)
        self.assertIsNone(error)
        self.assertEqual(projection, {"x": 4, "y": 5, "angle": 0.0})
        self.assertEqual(server.load_offset_state()["global"], {"x": 4, "y": 5, "angle": 0.0})

    def test_global_baseline_staging_and_delete_restore_remove_semantics(self):
        self.write_upstream(10, 11, 1)
        self.assertTrue(server.offset_set(None, 10, 11, 1)["ok"])
        self.assertTrue(server.offset_set("A4", 20, 21, 2)["ok"])
        self.assertEqual(server.read_global_offset(self.fixture.scm), {"x": 20, "y": 21, "angle": 2.0})
        self.assertEqual(server.load_offset_state()["global"], {"x": 10, "y": 11, "angle": 1.0})
        self.assertEqual(server.offset_delete("A4"), {"ok": True, "removed": "A4"})
        self.assertEqual(server.read_global_offset(self.fixture.scm), {"x": 10, "y": 11, "angle": 1.0})
        self.assertIsNone(server.load_offset_state()["staged_size"])
        before = (self.state_path.read_bytes(), self.upstream.read_bytes())
        self.assertEqual(server.offset_delete("A4"), {"ok": True, "removed": "A4"})
        self.assertEqual((self.state_path.read_bytes(), self.upstream.read_bytes()), before)
        server.offset_set("A4", 30, 31, 3)
        server.offset_set("tabloid", 40, 41, 4)
        projected = self.upstream.read_bytes()
        self.assertEqual(server.offset_delete("A4"), {"ok": True, "removed": "A4"})
        self.assertEqual(self.upstream.read_bytes(), projected)
        self.assertEqual(server.load_offset_state()["staged_size"], "tabloid")
        self.assertEqual(server.offset_delete("tabloid"), {"ok": True, "removed": "tabloid"})
        self.assertEqual(server.read_global_offset(self.fixture.scm), {"x": 10, "y": 11, "angle": 1.0})

    def test_offset_commit_invalidates_info_and_manifest_caches(self):
        server.MANIFEST_CACHE.update(stale={"x": 1})
        server._INFO_SNAP.update(t=1, v={"stale": True})
        server._REPOS_MTIME["t"] = 1
        self.write_upstream()
        self.assertTrue(server.offset_set(None, 1, 2, 3)["ok"])
        self.assertEqual(server.MANIFEST_CACHE, {})
        self.assertEqual(server._INFO_SNAP, {})

    def test_atomic_commit_uses_sibling_temp_and_leaves_no_temp_files(self):
        self.write_upstream()
        real_replace = os.replace
        with mock.patch.object(server.os, "replace", wraps=real_replace) as replace:
            self.assertTrue(server.offset_set(None, 1, 2, 3)["ok"])
        self.assertEqual(replace.call_count, 2)
        targets = {call.args[1] for call in replace.call_args_list}
        self.assertEqual(targets, {self.state_path, self.upstream})
        self.assertFalse(list(self.data.glob("*.tmp")))
        self.assertFalse(list(self.upstream.parent.glob("*.tmp")))
        json.loads(self.state_path.read_text(encoding="utf-8"))
        json.loads(self.upstream.read_text(encoding="utf-8"))

    def test_upstream_second_write_failure_rolls_back_canonical_and_projection(self):
        self.write_upstream(9, 8, 7)
        self.write_state({"version": 1, "global": {"x": 9, "y": 8, "angle": 7},
                          "rows": {}, "staged_size": None})
        before = (self.state_path.read_bytes(), self.upstream.read_bytes())
        real_atomic = server._atomic_json
        calls = []

        def injected(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("injected projection failure")
            return real_atomic(path, value)

        with mock.patch.object(server, "_recover_offset_projection_locked", return_value=None), \
             mock.patch.object(server, "_atomic_json", side_effect=injected):
            result = server.offset_set(None, 1, 2, 3)
        self.assertFalse(result["ok"])
        self.assertIn("injected projection failure", result["errors"][0])
        self.assertEqual((self.state_path.read_bytes(), self.upstream.read_bytes()), before)

    def test_crash_recovery_projects_canonical_selection_forward(self):
        self.write_upstream(1, 2, 3)
        self.write_state({"version": 1, "global": {"x": 4, "y": 5, "angle": 6},
                          "rows": {"A4": {"x": 7, "y": 8, "angle": 9}}, "staged_size": "A4"})
        self.assertIsNone(server.recover_offset_projection(self.fixture.scm))
        self.assertEqual(server.read_global_offset(self.fixture.scm), {"x": 7, "y": 8, "angle": 9.0})
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["staged_size"], "A4")

    def test_pending_save_recovery_before_after_write_and_ambiguous_is_safe(self):
        prior = {"x": 1, "y": 2, "angle": 3.0}
        intended = {"x": 11, "y": 12, "angle": 13.0}
        third = {"x": 21, "y": 22, "angle": 23.0}

        # A restart before SCM wrote its file drops the durable intent and
        # leaves both the previous projection and the baseline untouched.
        self.write_upstream(**{"x": prior["x"], "y": prior["y"], "angle": prior["angle"]})
        self.write_state({"version": 1, "global": prior, "rows": {}, "staged_size": None,
                          "pending": {"target": "global", "intended": intended,
                                      "prior_projection": prior}})
        old_projection = self.upstream.read_bytes()
        self.assertIsNone(server.recover_offset_projection(self.fixture.scm))
        recovered = server.load_offset_state()
        self.assertNotIn("pending", recovered)
        self.assertEqual(recovered["global"], prior)
        self.assertEqual(self.upstream.read_bytes(), old_projection)

        # A restart after SCM's write adopts a global pending value.
        self.write_upstream(intended["x"], intended["y"], intended["angle"])
        self.write_state({"version": 1, "global": prior, "rows": {}, "staged_size": None,
                          "pending": {"target": "global", "intended": intended,
                                      "prior_projection": prior}})
        self.assertIsNone(server.recover_offset_projection(self.fixture.scm))
        recovered = server.load_offset_state()
        self.assertEqual(recovered["global"], intended)
        self.assertIsNone(recovered["staged_size"])
        self.assertNotIn("pending", recovered)

        # The same post-write rule installs a per-size row and selection.
        self.write_upstream(intended["x"], intended["y"], intended["angle"])
        self.write_state({"version": 1, "global": prior, "rows": {}, "staged_size": None,
                          "pending": {"target": "A4", "intended": intended,
                                      "prior_projection": prior}})
        self.assertIsNone(server.recover_offset_projection(self.fixture.scm))
        recovered = server.load_offset_state()
        self.assertEqual(recovered["rows"]["A4"], intended)
        self.assertEqual(recovered["staged_size"], "A4")
        self.assertNotIn("pending", recovered)

        # If a third value is present, guessing would overwrite an unrelated
        # user's save. Recovery must leave the canonical bytes exactly intact.
        canonical = {"version": 1, "global": prior, "rows": {}, "staged_size": None,
                     "pending": {"target": "global", "intended": intended,
                                 "prior_projection": prior}}
        canonical_bytes = json.dumps(canonical, separators=(",", ":")).encode()
        self.state_path.write_bytes(canonical_bytes)
        self.write_upstream(third["x"], third["y"], third["angle"])
        projection_bytes = self.upstream.read_bytes()
        error = server.recover_offset_projection(self.fixture.scm)
        self.assertIn("cannot be reconciled safely", error)
        self.assertEqual(self.state_path.read_bytes(), canonical_bytes)
        self.assertEqual(self.upstream.read_bytes(), projection_bytes)

    def test_invalid_canonical_bytes_block_recovery_and_mutation_without_rewrite(self):
        malformed_documents = [
            b'{"version":1,"global":null,"rows":{',  # truncated JSON
            json.dumps({"version": 1, "global": None, "rows": {}, "staged_size": None,
                        "unexpected": True}).encode(),  # wrong schema
            json.dumps({"version": 1, "global": None, "rows": {}}).encode(),  # missing field
            json.dumps({"version": 1, "global": None,
                        "rows": {"A4": {"x": 1, "y": 2, "angle": 3, "extra": 4}},
                        "staged_size": None}).encode(),  # malformed strict row
        ]
        self.write_upstream(4, 5, 6)
        for raw in malformed_documents:
            self.state_path.write_bytes(raw)
            upstream_before = self.upstream.read_bytes()
            self.assertIsNotNone(server.recover_offset_projection(self.fixture.scm))
            self.assertEqual(self.state_path.read_bytes(), raw)
            self.assertEqual(self.upstream.read_bytes(), upstream_before)
            result = server.offset_set(None, 7, 8, 9)
            self.assertFalse(result["ok"])
            self.assertEqual(self.state_path.read_bytes(), raw)
            self.assertEqual(self.upstream.read_bytes(), upstream_before)

    def test_projection_rejects_symlinked_data_and_file_without_touching_escape(self):
        outside = self.root / "outside-offset.json"
        outside.write_text("outside", encoding="utf-8")
        data = self.fixture.scm / "data"
        try:
            data.rmdir()
            data.symlink_to(outside.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable: %s" % error)
        before = outside.read_bytes()
        result = server.offset_set(None, 1, 2, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(outside.read_bytes(), before)

        data.unlink()
        data.mkdir()
        target = data / "offset_data.json"
        target.symlink_to(outside)
        result = server.offset_set(None, 4, 5, 6)
        self.assertFalse(result["ok"])
        self.assertEqual(outside.read_bytes(), before)

    def test_get_info_does_not_report_staged_row_as_global_baseline(self):
        self.write_upstream(7, 8, 9)
        self.write_state({"version": 1, "global": None,
                          "rows": {"A4": {"x": 7, "y": 8, "angle": 9}},
                          "staged_size": "A4"})
        info = server.get_info()
        self.assertIsNone(info["scm"]["saved_offset"])
        self.assertEqual(info["per_size_offsets"]["A4"], {"x": 7, "y": 8, "angle": 9.0})

    def test_concurrent_disjoint_mutations_eventually_keep_both_rows(self):
        self.write_upstream()
        results = []
        barrier = threading.Barrier(2)

        def worker(size, x):
            barrier.wait()
            for _ in range(200):
                result = server.offset_set(size, x, x + 1, x + 2)
                if result["ok"]:
                    results.append(size)
                    return
                time.sleep(0.001)
            self.fail("offset operation remained busy")

        threads = [threading.Thread(target=worker, args=("A4", 1)),
                   threading.Thread(target=worker, args=("tabloid", 10))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(sorted(results), ["A4", "tabloid"])
        self.assertEqual(set(server.load_offset_state()["rows"]), {"A4", "tabloid"})

    def test_busy_lease_is_nonblocking_for_native_domain_operations(self):
        self.write_upstream()
        server.OFFSET_LEASE.acquire()
        try:
            self.assertEqual(server.offset_set(None, 1, 2, 3), {
                "ok": False, "errors": ["offset operations are busy; try again after the running offset job finishes"]})
            self.assertEqual(server.offset_delete("A4"), {
                "ok": False, "errors": ["offset operations are busy; try again after the running offset job finishes"]})
        finally:
            server.OFFSET_LEASE.release()

    def test_two_offset_sensitive_jobs_cannot_overlap_and_release_lease(self):
        class BlockingReader:
            def __init__(self):
                self.released = threading.Event()

            def readline(self):
                self.released.wait(5)
                return b""

        reader = BlockingReader()
        process = _PipeProc()
        process.stdout = reader
        with mock.patch.object(server.subprocess, "Popen", return_value=process):
            first, errors = server.start_job("offset_pdf", {
                "paper_size": "A4", "x_offset": 1, "y_offset": 2, "angle": 3,
                "pdf_path": "game/output/game.pdf",
            })
            self.assertFalse(errors)
            second, errors = server.start_job("offset_pdf", {
                "paper_size": "tabloid", "x_offset": 4, "y_offset": 5, "angle": 6,
            })
        self.assertIsNone(second)
        self.assertIn("busy", errors[0])
        self.assertTrue(server.OFFSET_LEASE.locked())
        reader.released.set()
        first["pump_thread"].join(timeout=3)
        self.assertFalse(first["pump_thread"].is_alive())
        self.assertFalse(server.OFFSET_LEASE.locked())

    def test_spawn_failure_releases_offset_lease(self):
        with mock.patch.object(server.subprocess, "Popen", side_effect=OSError("spawn failed")):
            job, errors = server.start_job("offset_pdf", {
                "paper_size": "A4", "x_offset": 1, "y_offset": 2, "angle": 3,
            })
        self.assertFalse(errors)
        self.assertEqual(job["status"], "fail")
        self.assertFalse(server.OFFSET_LEASE.locked())
        self.assertTrue(server.OFFSET_LEASE.acquire(blocking=False))
        server.OFFSET_LEASE.release()

    def pump_job(self, proc, **extra):
        job = {"id": "pump-test", "ts": time.time(), "kind": "offset_pdf",
               "title": "Offset", "cmd": "offset", "args": {}, "status": "running",
               "exit_code": None, "log_file": str(self.data / "logs" / "pump.log"),
               "log_lines": [], "first_seq": 0, "subs": [], "warnings": [],
               "started": time.time(), "ended": None, "duration": None,
               "proc": proc, "pump_thread": None, "offset_lease": False}
        job.update(extra)
        server.JOBS[job["id"]] = job
        job["log_file"] = str(self.data / "logs" / (job["id"] + ".log"))
        Path(job["log_file"]).parent.mkdir(parents=True, exist_ok=True)
        with open(job["log_file"], "w", encoding="utf-8") as log:
            server._pump(job, proc, log)
        return job

    def test_pump_log_failure_terminates_and_reaps_child_before_releasing_lease(self):
        class LiveProc:
            def __init__(self):
                self.stdout = io.BytesIO(b"line before logging fails\\n")
                self.pid = 999999
                self.terminated = False
                self.waited = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                self.assert_live_reaped_order()
                self.waited = True
                return 1

            def assert_live_reaped_order(self):
                # _terminate_and_reap must wait while the offset lease is still
                # held; releasing first could overlap a still-live SCM writer.
                if not self.terminated or not server.OFFSET_LEASE.locked():
                    raise AssertionError("child was not terminated/reaped before lease release")

            def kill(self):
                self.terminated = True

        proc = LiveProc()
        server.OFFSET_LEASE.acquire()
        try:
            with mock.patch.object(server, "_append_job_line", side_effect=RuntimeError("log pump failure")):
                job = self.pump_job(proc, id="log-failure", offset_lease=True)
            self.assertEqual(job["status"], "fail")
            self.assertTrue(proc.terminated)
            self.assertTrue(proc.waited)
            self.assertTrue(server.OFFSET_LEASE.acquire(blocking=False))
            server.OFFSET_LEASE.release()
        finally:
            if server.OFFSET_LEASE.locked():
                server.OFFSET_LEASE.release()

    def test_pump_rc_zero_ambiguous_pending_reconcile_is_failed_with_zero_exit(self):
        prior = {"x": 1, "y": 2, "angle": 3.0}
        intended = {"x": 11, "y": 12, "angle": 13.0}
        third = {"x": 21, "y": 22, "angle": 23.0}
        self.write_upstream(third["x"], third["y"], third["angle"])
        self.write_state({"version": 1, "global": prior, "rows": {}, "staged_size": None,
                          "pending": {"target": "global", "intended": intended,
                                      "prior_projection": prior}})
        job = self.pump_job(_PipeProc(b"renderer completed\\n", 0), id="ambiguous-zero",
                            scm_path=str(self.fixture.scm), offset_save=True,
                            offset_sync=None)
        self.assertEqual(job["status"], "fail")
        self.assertEqual(job["exit_code"], 0)

        # A normal post-write mirror commit failure is also a failed job, while
        # preserving the child's successful exit code for diagnostics.
        self.write_upstream(31, 32, 33)
        self.write_state({"version": 1, "global": prior, "rows": {}, "staged_size": None})
        with mock.patch.object(server, "_commit_offset_state", return_value="injected commit failure"):
            job = self.pump_job(_PipeProc(b"renderer completed\\n", 0), id="commit-zero",
                                scm_path=str(self.fixture.scm), offset_save=True,
                                offset_sync=None)
        self.assertEqual(job["status"], "fail")
        self.assertEqual(job["exit_code"], 0)

    def test_pump_lifecycle_statuses_normal_nonzero_kill_and_stop(self):
        self.assertEqual(self.pump_job(_PipeProc(b"normal\n", 0))["status"], "ok")
        self.assertEqual(self.pump_job(_PipeProc(b"error\n", 7), id="nonzero")["status"], "fail")
        self.assertEqual(self.pump_job(_PipeProc(b"killed\n", 1), id="killed", kill_requested=True)["status"], "killed")
        proc = mock.Mock()
        job = self.pump_job
        stop_job = {"id": "stop", "ts": time.time(), "kind": "offset_pdf", "title": "Offset",
                    "cmd": "offset", "args": {}, "status": "running", "exit_code": None,
                    "log_file": str(self.data / "logs/stop.log"), "log_lines": [], "first_seq": 0,
                    "subs": [], "warnings": [], "started": time.time(), "ended": None,
                    "duration": None, "proc": proc, "pump_thread": mock.Mock(), "offset_lease": True}
        server.JOBS["stop"] = stop_job
        with mock.patch.object(server, "kill_job", return_value=True) as kill:
            server.stop_all_jobs(timeout=.01)
        kill.assert_called_once_with("stop")
        proc.wait.assert_called()
        stop_job["pump_thread"].join.assert_called()
        self.assertFalse(server.OFFSET_LEASE.locked())

    def test_stop_all_does_not_force_unlock_live_offset_child(self):
        proc = mock.Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("fixture", 0)
        pump = mock.Mock()
        pump.is_alive.return_value = True
        job = {"id": "still-live", "ts": time.time(), "kind": "offset_pdf",
               "title": "Offset", "cmd": "offset", "args": {}, "status": "running",
               "exit_code": None, "log_file": str(self.data / "logs/live.log"),
               "log_lines": [], "first_seq": 0, "subs": [], "warnings": [],
               "started": time.time(), "ended": None, "duration": None,
               "proc": proc, "pump_thread": pump, "offset_lease": True}
        server.JOBS[job["id"]] = job
        server.OFFSET_LEASE.acquire()
        try:
            with mock.patch.object(server, "kill_job", return_value=True):
                server.stop_all_jobs(timeout=.01)
            # The child/pump did not report completion, so the lease remains
            # owned by it; a later operation cannot overlap this live writer.
            self.assertTrue(server.OFFSET_LEASE.locked())
            self.assertTrue(job["offset_lease"])
        finally:
            server.OFFSET_LEASE.release()

    def test_pump_uses_snapshotted_scm_and_saves_valid_values_on_nonzero(self):
        other = self.root / "other-scm"
        other.mkdir()
        (other / "data").mkdir()
        other_offset = other / "data" / "offset_data.json"
        other_offset.write_text(json.dumps({"x_offset": 99, "y_offset": 98, "angle_offset": 97}), encoding="utf-8")
        self.write_upstream(31, 32, 33)
        self.write_state({"version": 1, "global": {"x": 1, "y": 2, "angle": 3},
                          "rows": {}, "staged_size": None})
        server.save_settings({**server.load_settings(), "scm_dir": str(other)})
        # The fake upstream has completed its save before the renderer fails;
        # the pump must read the checkout captured on the job, not Settings.
        job = self.pump_job(_PipeProc(b"render failed\n", 4), id="saved", scm_path=str(self.fixture.scm),
                            offset_save=True, offset_sync="A4")
        self.assertEqual(job["status"], "fail")
        self.assertEqual(server.load_offset_state()["rows"]["A4"], {"x": 31, "y": 32, "angle": 33.0})
        self.assertEqual(json.loads(other_offset.read_text(encoding="utf-8")),
                         {"x_offset": 99, "y_offset": 98, "angle_offset": 97})

        # A blank paper selection mirrors the same valid save into the global
        # baseline, also after a nonzero renderer exit.
        self.write_upstream(41, 42, 43)
        global_job = self.pump_job(_PipeProc(b"render failed\n", 9), id="saved-global",
                                   scm_path=str(self.fixture.scm), offset_save=True,
                                   offset_sync=None)
        self.assertEqual(global_job["status"], "fail")
        self.assertEqual(server.load_offset_state()["global"], {"x": 41, "y": 42, "angle": 43.0})
        self.assertIsNone(server.load_offset_state()["staged_size"])


if __name__ == "__main__":
    unittest.main()
