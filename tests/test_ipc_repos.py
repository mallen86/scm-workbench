"""Async repository IPC contracts and HTTP/native parity tests."""

import io
import json
from contextlib import ExitStack
import os
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, repo_sync, server


SHA = "a" * 40
TARGET = {"sha": SHA, "ref": "main", "date": None}
REFS = {"default_branch": "main", "tags": [], "releases": []}


class RepoIpcTests(unittest.TestCase):
    """Use a private data area and operation executor for every test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-repo-ipc-")
        self.data = Path(self.temp.name)
        self.old_env = os.environ.get("SCM_WORKBENCH_DATA")
        os.environ["SCM_WORKBENCH_DATA"] = str(self.data)
        names = ("DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR",
                 "PER_SIZE_OFFSETS_FILE", "UPDATE_STATE_FILE")
        self.old_globals = {name: getattr(server, name) for name in names}
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        server.PER_SIZE_OFFSETS_FILE = self.data / "offsets.json"
        server.UPDATE_STATE_FILE = self.data / "update-state.json"

        # The worker threads are intentionally daemon threads and have no stop
        # API. Give this test a fresh executor and restore the old one below;
        # this also makes tests independent when run in any order.
        op_names = ("_REPO_OPS", "_REPO_OP_QUEUE", "_REPO_OP_WORKERS_STARTED")
        self.old_ops = {name: getattr(server, name) for name in op_names}
        self.old_refs = dict(server._refs_cache)
        self.old_inflight = dict(server._REFS_INFLIGHT)
        self.old_views = (dict(server.MANIFEST_CACHE), dict(server._INFO_SNAP),
                          dict(server._REPOS_MTIME))
        server._REPO_OPS = {}
        server._REPO_OP_QUEUE = queue.Queue(maxsize=16)
        server._REPO_OP_WORKERS_STARTED = False
        server._refs_cache.clear()
        server._REFS_INFLIGHT.clear()
        server.MANIFEST_CACHE.clear()
        server._INFO_SNAP.clear()
        server._REPOS_MTIME.clear()
        self.httpd = None
        server.save_settings(json.loads(json.dumps(server.DEFAULT_SETTINGS)))

    def tearDown(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        # All tests release their gates before returning. Waiting here prevents
        # a worker from touching a registry after it has been restored.
        try:
            self._drain_operations(timeout=3)
        except Exception:
            pass
        server._REPO_OPS = self.old_ops["_REPO_OPS"]
        server._REPO_OP_QUEUE = self.old_ops["_REPO_OP_QUEUE"]
        server._REPO_OP_WORKERS_STARTED = self.old_ops["_REPO_OP_WORKERS_STARTED"]
        server._refs_cache.clear()
        server._refs_cache.update(self.old_refs)
        server._REFS_INFLIGHT.clear()
        server._REFS_INFLIGHT.update(self.old_inflight)
        for target, saved in zip(
                (server.MANIFEST_CACHE, server._INFO_SNAP, server._REPOS_MTIME), self.old_views):
            target.clear()
            target.update(saved)
        for name, value in self.old_globals.items():
            setattr(server, name, value)
        if self.old_env is None:
            os.environ.pop("SCM_WORKBENCH_DATA", None)
        else:
            os.environ["SCM_WORKBENCH_DATA"] = self.old_env
        self.temp.cleanup()

    def _drain_operations(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with server._REPO_OP_LOCK:
                pending = [op for op in server._REPO_OPS.values()
                           if op.get("status") != "done"]
            if not pending and server._REPO_OP_QUEUE.empty():
                return
            time.sleep(0.01)

    def native_start(self, method, params, request_id="request"):
        response = ipc.dispatch({"id": request_id, "method": method, "params": params})
        self.assertEqual(set(response), {"id", "ok", "result"})
        self.assertTrue(response["ok"])
        operation = response["result"].get("operation")
        self.assertIsInstance(operation, dict)
        self.assertEqual(set(operation), {"id", "status"})
        self.assertIsInstance(operation["id"], str)
        self.assertTrue(operation["id"])
        self.assertEqual(operation["status"], "running")
        return operation["id"]

    def native_final(self, operation_id, request_id="poll"):
        deadline = time.time() + 5
        while time.time() < deadline:
            response = ipc.dispatch({
                "id": request_id, "method": "repos.poll",
                "params": {"operation_id": operation_id},
            })
            self.assertEqual(set(response), {"id", "ok", "result"})
            self.assertEqual(response["id"], request_id)
            self.assertTrue(response["ok"])
            if response["result"].get("status") == "done":
                self.assertEqual(set(response["result"]), {"ok", "status", "result"})
                self.assertTrue(response["result"]["ok"])
                return response["result"]["result"]
            self.assertEqual(response["result"], {"ok": True, "status": "running"})
            time.sleep(0.01)
        self.fail("repository operation did not finish")

    def _start_http(self):
        if self.httpd is None:
            self.httpd = server.start_http("127.0.0.1", 0)
            thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            thread.start()
            self.http_thread = thread
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def http_post(self, path, body):
        request = urllib.request.Request(
            self._start_http() + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_native_envelopes_and_strict_validation_for_all_four_methods(self):
        with mock.patch.object(repo_sync, "list_refs", return_value=REFS), \
             mock.patch.object(repo_sync, "resolve_target", return_value=TARGET), \
             mock.patch.object(server, "run_repo_check", return_value={
                 "repo": "scm", "ok": True, "cached": False,
                 "target": TARGET, "deployed": None, "source": "main",
                 "up_to_date": False}):
            ids = [
                self.native_start("repos.refs", {"repo": "scm"}, "refs"),
                self.native_start("repos.source.set", {"repo": "scm", "source": "main"}, "source"),
                self.native_start("repos.check", {"repo": "scm", "force": True}, "check"),
            ]
            for operation_id in ids:
                result = self.native_final(operation_id)
                self.assertIsInstance(result, dict)

        invalid = [
            ("repos.refs", {}, "repos.refs requires exactly repo"),
            ("repos.refs", {"repo": "scm", "extra": 1}, "repos.refs requires exactly repo"),
            ("repos.refs", {"repo": 1}, "repos.refs requires exactly repo"),
            ("repos.source.set", {"repo": "scm"}, "repos.source.set requires exactly repo and source"),
            ("repos.source.set", {"repo": "scm", "source": "../bad"}, "invalid source form"),
            ("repos.source.set", {"repo": "unknown", "source": "main"}, "unknown repository"),
            ("repos.check", {"repo": "scm", "force": 1}, "repos.check force must be boolean"),
            ("repos.check", {"repo": "scm", "force": False, "extra": 1}, "repos.check requires exactly repo and force"),
            ("repos.check", {"repo": "unknown", "force": False}, "unknown repository"),
            ("repos.poll", {}, "repos.poll requires exactly operation_id"),
            ("repos.poll", {"operation_id": 1}, "repos.poll requires exactly operation_id"),
            ("repos.poll", {"operation_id": ""}, "repos.poll requires exactly operation_id"),
            ("repos.poll", {"operation_id": "x" * 65}, "repos.poll requires exactly operation_id"),
        ]
        for method, params, message in invalid:
            with self.subTest(method=method, params=params):
                response = ipc.dispatch({"id": "bad", "method": method, "params": params})
                self.assertEqual(response, {
                    "id": "bad", "ok": False,
                    "error": {"code": "bad_request", "message": message},
                })
        self.assertEqual(ipc.dispatch({"id": "u", "method": "repos.nope", "params": {}}), {
            "id": "u", "ok": False,
            "error": {"code": "unknown_method", "message": "unknown method: repos.nope"},
        })

    def test_start_is_immediate_while_network_is_blocked_and_poll_is_running_then_done(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked(_key):
            entered.set()
            self.assertTrue(release.wait(3))
            return REFS

        try:
            with mock.patch.object(repo_sync, "list_refs", side_effect=blocked):
                started = time.monotonic()
                operation_id = self.native_start("repos.refs", {"repo": "scm"})
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(entered.wait(2))
                running = ipc.dispatch({"id": "p", "method": "repos.poll",
                                        "params": {"operation_id": operation_id}})
                self.assertEqual(running, {"id": "p", "ok": True,
                                           "result": {"ok": True, "status": "running"}})
                release.set()
                self.assertEqual(self.native_final(operation_id), {
                    "ok": True, "repo": "scm", "refs": REFS})
        finally:
            release.set()

    def test_queued_operations_have_running_poll_and_unique_ids(self):
        started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}
        lock = threading.Lock()

        def blocked(_key):
            with lock:
                calls["n"] += 1
                if calls["n"] == 2:
                    started.set()
            self.assertTrue(release.wait(3))
            return REFS

        try:
            with mock.patch.object(server, "repo_refs_result", side_effect=blocked):
                first = self.native_start("repos.refs", {"repo": "scm"}, "one")
                second = self.native_start("repos.refs", {"repo": "scm"}, "two")
                self.assertTrue(started.wait(2))
                queued = self.native_start("repos.refs", {"repo": "scm"}, "three")
                self.assertGreaterEqual(server._REPO_OP_QUEUE.qsize(), 1)
                self.assertEqual(len({first, second, queued}), 3)
                self.assertEqual(ipc.dispatch({"id": "q", "method": "repos.poll",
                                                "params": {"operation_id": queued}})["result"],
                                 {"ok": True, "status": "running"})
                release.set()
                for operation_id in (first, second, queued):
                    self.assertEqual(self.native_final(operation_id), REFS)
        finally:
            release.set()

    def test_operation_ids_unknown_and_expired_ttl_are_bounded(self):
        with mock.patch.object(server, "repo_refs_result", return_value=REFS):
            first = self.native_start("repos.refs", {"repo": "scm"}, "a")
            second = self.native_start("repos.refs", {"repo": "scm"}, "b")
            self.native_final(first)
            self.native_final(second)
        unknown = ipc.dispatch({"id": "u", "method": "repos.poll",
                                "params": {"operation_id": "does-not-exist"}})
        self.assertEqual(unknown, {
            "id": "u", "ok": False,
            "error": {"code": "bad_request", "message": "operation not found"},
        })
        with server._REPO_OP_LOCK:
            server._REPO_OPS["expired"] = {
                "status": "done", "ended": time.time() - server._REPO_OP_TTL,
                "result": {"ok": True},
            }
        expired = ipc.dispatch({"id": "e", "method": "repos.poll",
                                "params": {"operation_id": "expired"}})
        self.assertEqual(expired["error"]["code"], "bad_request")
        self.assertNotIn("expired", server._REPO_OPS)

    def test_active_total_caps_and_executor_has_exactly_two_concurrent_workers(self):
        release = threading.Event()
        entered = threading.Event()
        count = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def blocked(_key):
            with lock:
                count["active"] += 1
                count["peak"] = max(count["peak"], count["active"])
                if count["active"] == 2:
                    entered.set()
            self.assertTrue(release.wait(3))
            with lock:
                count["active"] -= 1
            return REFS

        try:
            with mock.patch.object(server, "repo_refs_result", side_effect=blocked):
                ids = [self.native_start("repos.refs", {"repo": "scm"}, str(i))
                       for i in range(server._REPO_OP_ACTIVE_MAX)]
                capped_active = server._start_repo_operation("refs", {"repo": "scm"})
                self.assertEqual(capped_active, {"ok": False,
                                                 "errors": ["too many repository operations"]})
                self.assertTrue(entered.wait(2))
                release.set()
                for operation_id in ids:
                    self.native_final(operation_id)
            with server._REPO_OP_LOCK:
                server._REPO_OPS.clear()
                now = time.time()
                for i in range(server._REPO_OP_TOTAL_MAX):
                    server._REPO_OPS[f"done-{i}"] = {
                        "status": "done", "ended": now, "result": {"ok": True}}
            self.assertEqual(server._start_repo_operation("refs", {"repo": "scm"}), {
                "ok": False, "errors": ["too many repository operations"]})
            with server._REPO_OP_LOCK:
                server._REPO_OPS.clear()
            self.assertEqual(count["peak"], 2)
        finally:
            release.set()

    def test_worker_exception_error_list_and_operation_result_limits(self):
        cases = [
            (RuntimeError("secret"), {"ok": False, "errors": ["operation failed"]}),
            ("not a result", {"ok": False, "errors": ["operation returned an invalid result"]}),
            ({"ok": False, "errors": ["x" * 1000] * 20},
             {"ok": False, "errors": ["x" * 256] * 8}),
        ]
        for value, expected in cases:
            with self.subTest(value=type(value).__name__):
                side_effect = value if isinstance(value, BaseException) else None
                kwargs = {"side_effect": side_effect} if side_effect else {"return_value": value}
                with mock.patch.object(server, "repo_refs_result", **kwargs):
                    operation_id = self.native_start("repos.refs", {"repo": "scm"})
                    self.assertEqual(self.native_final(operation_id), expected)

        oversized = {"ok": True, "payload": "x" * server._REPO_OP_RESULT_MAX}
        with mock.patch.object(server, "repo_refs_result", return_value=oversized):
            operation_id = self.native_start("repos.refs", {"repo": "scm"})
            self.assertEqual(self.native_final(operation_id), {
                "ok": False, "errors": ["operation result exceeds 1 MiB"]})

    def test_http_and_native_final_results_have_parity_for_success_and_failures(self):
        # Each case compares the HTTP response body with the completed native
        # operation, rather than comparing the native start acknowledgement.
        cases = [
            ("refs-success", "/api/repos/refs", {"repo": "scm"},
             "repos.refs", {"repo": "scm"}, 200,
             {"repo_sync.list_refs": REFS}),
            ("refs-failure", "/api/repos/refs", {"repo": "scm"},
             "repos.refs", {"repo": "scm"}, 400,
             {"repo_sync.list_refs": repo_sync.RepoError("network unavailable")}),
            ("source-success", "/api/repos/save", {"repo": "scm", "source": "feature/topic"},
             "repos.source.set", {"repo": "scm", "source": "feature/topic"}, 200,
             {"repo_sync.resolve_target": TARGET}),
            ("source-failure", "/api/repos/save", {"repo": "scm", "source": "feature/topic"},
             "repos.source.set", {"repo": "scm", "source": "feature/topic"}, 400,
             {"repo_sync.resolve_target": repo_sync.RepoError("ref unavailable")}),
            ("check-success", "/api/repos/check", {"repo": "scm", "force": True},
             "repos.check", {"repo": "scm", "force": True}, 200,
             {"server.run_repo_check": {"repo": "scm", "ok": True, "cached": False,
                                         "target": TARGET, "deployed": None,
                                         "source": "main", "up_to_date": False}}),
            ("check-failure", "/api/repos/check", {"repo": "scm", "force": True},
             "repos.check", {"repo": "scm", "force": True}, 400,
             {"server.run_repo_check": {"repo": "scm", "ok": False,
                                         "error": "network unavailable"}}),
        ]
        for name, http_path, http_body, method, native_params, status, behavior in cases:
            with self.subTest(name=name):
                server._refs_cache.clear()
                repo_sync.save_state({})
                patches = [mock.patch.object(server.time, "time", return_value=1234.0),
                           mock.patch.object(repo_sync.time, "time", return_value=1234.0)]
                if "repo_sync.list_refs" in behavior:
                    value = behavior["repo_sync.list_refs"]
                    patches.append(mock.patch.object(
                        repo_sync, "list_refs",
                        side_effect=value if isinstance(value, BaseException) else None,
                        return_value=None if isinstance(value, BaseException) else value))
                if "repo_sync.resolve_target" in behavior:
                    value = behavior["repo_sync.resolve_target"]
                    patches.append(mock.patch.object(
                        repo_sync, "resolve_target",
                        side_effect=value if isinstance(value, BaseException) else None,
                        return_value=None if isinstance(value, BaseException) else value))
                if "server.run_repo_check" in behavior:
                    patches.append(mock.patch.object(server, "run_repo_check",
                                                     return_value=behavior["server.run_repo_check"]))
                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    http_status, http_result = self.http_post(http_path, http_body)
                    operation_id = self.native_start(method, native_params, name)
                    native_result = self.native_final(operation_id, name + "-poll")
                self.assertEqual(http_status, status)
                self.assertEqual(native_result, http_result)

    def test_source_state_is_canonical_mirror_preserves_unrelated_data_and_reports_mirror_failure(self):
        settings = json.loads(json.dumps(server.DEFAULT_SETTINGS))
        settings.update({"theme": "light", "scm_dir": "/kept/path"})
        server.save_settings(settings)
        repo_sync.save_state({"extras": {"source": "main", "marker": "keep"},
                              "scm": {"marker": "also keep"}})
        with mock.patch.object(repo_sync, "resolve_target", return_value=TARGET):
            result = server.repo_source_result("scm", "feature/topic")
        self.assertTrue(result["ok"])
        self.assertEqual(repo_sync.load_source("scm"), "feature/topic")
        self.assertEqual(json.loads(server.SETTINGS_FILE.read_text())["repos"]["scm"], {
            "source": "pinned", "pin": "feature/topic"})
        self.assertEqual(json.loads(server.SETTINGS_FILE.read_text())["theme"], "light")
        self.assertEqual(repo_sync.load_state()["extras"], {"source": "main", "marker": "keep"})
        self.assertEqual(repo_sync.load_state()["scm"]["marker"], "also keep")

        before_settings = server.SETTINGS_FILE.read_bytes()
        with mock.patch.object(repo_sync, "resolve_target", return_value={
                "sha": "b" * 40, "ref": "next", "date": None}), \
             mock.patch.object(server, "save_settings", side_effect=OSError("disk unavailable")):
            failed = server.repo_source_result("scm", "next")
        self.assertEqual(failed["ok"], True)
        self.assertEqual(failed["repo"], "scm")
        self.assertEqual(failed["source"], "next")
        self.assertEqual(failed["canonical"], True)
        self.assertEqual(failed["warnings"], ["source mirror failed: disk unavailable"])
        self.assertEqual(repo_sync.load_source("scm"), "next")
        self.assertEqual(server.SETTINGS_FILE.read_bytes(), before_settings)
        self.assertEqual(repo_sync.load_state()["extras"], {"source": "main", "marker": "keep"})

    def test_mirror_failure_is_success_warning_and_http_native_parity(self):
        # Canonical state is committed first. A failed settings mirror is a
        # successful source change with an explicit bounded warning, and both
        # transports must expose the same final body/status.
        repo_sync.save_state({})
        with mock.patch.object(server.time, "time", return_value=1234.0), \
             mock.patch.object(repo_sync.time, "time", return_value=1234.0), \
             mock.patch.object(repo_sync, "resolve_target", return_value=TARGET), \
             mock.patch.object(server, "save_settings", side_effect=OSError("disk unavailable")):
            http_status, http_result = self.http_post(
                "/api/repos/save", {"repo": "scm", "source": "feature/topic"})
            operation_id = self.native_start(
                "repos.source.set", {"repo": "scm", "source": "feature/topic"})
            native_result = self.native_final(operation_id)

        self.assertEqual(http_status, 200)
        self.assertEqual(native_result, http_result)
        self.assertTrue(http_result["ok"])
        self.assertTrue(http_result["canonical"])
        self.assertEqual(http_result["warnings"], [
            "source mirror failed: disk unavailable"])
        self.assertEqual(repo_sync.load_source("scm"), "feature/topic")

    def test_refs_singleflight_ttl_and_error_retry(self):
        entered = threading.Event()
        release = threading.Event()
        calls = {"n": 0}
        lock = threading.Lock()

        def slow_refs(_key):
            with lock:
                calls["n"] += 1
            entered.set()
            self.assertTrue(release.wait(3))
            return REFS

        results = []
        with mock.patch.object(repo_sync, "list_refs", side_effect=slow_refs):
            threads = [threading.Thread(target=lambda: results.append(server.repo_refs_result("scm")))
                       for _ in range(8)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(2))
            release.set()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(results, [{"ok": True, "repo": "scm", "refs": REFS}] * 8)

        with mock.patch.object(repo_sync, "list_refs", return_value=REFS) as refreshed:
            server._refs_cache["scm"] = (time.time() - server._REFS_CACHE_TTL - 1, REFS)
            self.assertEqual(server.repo_refs_result("scm")["ok"], True)
            refreshed.assert_called_once_with("scm")

        with mock.patch.object(repo_sync, "list_refs",
                               side_effect=[repo_sync.RepoError("temporary"), REFS]) as retry:
            server._refs_cache.clear()
            self.assertFalse(server.repo_refs_result("scm")["ok"])
            self.assertEqual(server.repo_refs_result("scm"), {
                "ok": True, "repo": "scm", "refs": REFS})
            self.assertEqual(retry.call_count, 2)

    def test_concurrent_check_source_and_deploy_lock_paths_do_not_deadlock(self):
        repo_sync.save_state({})
        barrier = threading.Barrier(3)
        errors = []

        def run(fn):
            try:
                barrier.wait()
                fn()
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(repo_sync, "resolve_target", return_value=TARGET), \
             mock.patch.object(repo_sync, "_cmd_update_locked",
                               return_value={"ok": True, "noop": True}):
            threads = [
                threading.Thread(target=run, args=(lambda: repo_sync.check_repo("scm", force=True),)),
                threading.Thread(target=run, args=(lambda: server.repo_source_result("scm", "feature"),)),
                threading.Thread(target=run, args=(lambda: repo_sync.cmd_update("scm"),)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive(), "repository lock deadlock")
        self.assertEqual(errors, [])

    def test_native_repo_operations_never_write_stdout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(server, "repo_refs_result", return_value=REFS), \
             mock.patch.object(ipc.sys, "stdout", stdout), \
             mock.patch.object(ipc.sys, "stderr", stderr):
            operation_id = self.native_start("repos.refs", {"repo": "scm"})
            self.assertEqual(self.native_final(operation_id), REFS)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("[ipc] served repos.refs", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
