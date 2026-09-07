"""Bounded native app-update and release-notes IPC contracts."""

import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import ipc, server, updater


class UpdateIpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-update-ipc-")
        self.old = {name: getattr(server, name) for name in
                    ("DATA_DIR", "UPDATE_STATE_FILE", "_UPDATE_OPS",
                     "_UPDATE_OP_QUEUE", "_UPDATE_OP_WORKERS_STARTED")}
        server.DATA_DIR = Path(self.temp.name)
        server.UPDATE_STATE_FILE = server.DATA_DIR / "update-state.json"
        server._UPDATE_OPS = {}
        server._UPDATE_OP_QUEUE = queue.Queue(maxsize=16)
        server._UPDATE_OP_WORKERS_STARTED = False
        state = server._default_update_state()
        state.update(status="up-to-date", latest="v2.0.0", checked_at=time.time())
        server.save_update_state(state)

    def tearDown(self):
        deadline = time.time() + 2
        while time.time() < deadline:
            with server._UPDATE_OP_LOCK:
                if not any(op.get("status") == "running" for op in server._UPDATE_OPS.values()):
                    break
            time.sleep(.01)
        for name, value in self.old.items():
            setattr(server, name, value)
        self.temp.cleanup()

    @staticmethod
    def dispatch(method, params, request_id="x"):
        return ipc.dispatch({"id": request_id, "method": method, "params": params})

    def poll_done(self, operation_id):
        for _ in range(300):
            result = self.dispatch("updates.poll", {"id": operation_id})
            self.assertTrue(result["ok"], result)
            if result["result"]["status"] == "done":
                return result["result"]["result"]
            time.sleep(.005)
        self.fail("update operation did not finish")

    def test_get_shape_and_strict_methods(self):
        result = self.dispatch("updates.get", {})["result"]
        self.assertEqual(set(result), {"current", "repo", "packaged", "bundle", "state"})
        self.assertIs(result["state"]["checking"], False)
        self.assertNotIn("checking", json.loads(server.UPDATE_STATE_FILE.read_text()))
        for method, params in (("updates.get", {"x": 1}),
                               ("updates.check", {}),
                               ("updates.check", {"force": 1}),
                               ("updates.start", {"x": 1}),
                               ("updates.poll", {"id": "x", "extra": 1}),
                               ("updates.poll", {"id": "x" * 65}),
                               ("updates.notes", {"tag": "x" * 129})):
            response = self.dispatch(method, params)
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "bad_request")

    def test_updates_get_projects_transient_checking_from_queued_or_running_ops(self):
        with server._UPDATE_OP_LOCK:
            server._UPDATE_OPS["check"] = {"kind": "check", "status": "running"}
        self.assertTrue(self.dispatch("updates.get", {})["result"]["state"]["checking"])
        with server._UPDATE_OP_LOCK:
            server._UPDATE_OPS["check"]["status"] = "done"
        self.assertFalse(self.dispatch("updates.get", {})["result"]["state"]["checking"])
        self.assertNotIn("checking", json.loads(server.UPDATE_STATE_FILE.read_text()))

    def test_check_start_is_immediate_and_reader_does_not_wait_for_network(self):
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            self.assertTrue(release.wait(2))
            return server.load_update_state()

        try:
            with mock.patch.object(server, "run_update_check", side_effect=blocked):
                started = time.monotonic()
                response = self.dispatch("updates.check", {"force": True})
                self.assertLess(time.monotonic() - started, .5)
                operation_id = response["result"]["operation"]["id"]
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.dispatch("updates.get", {})["result"]["state"]["checking"])
                self.assertEqual(self.dispatch("updates.poll", {"id": operation_id})["result"],
                                 {"ok": True, "status": "running"})
                release.set()
                result = self.poll_done(operation_id)
                self.assertEqual(result["ok"], True)
                self.assertFalse(self.dispatch("updates.get", {})["result"]["state"]["checking"])
        finally:
            release.set()

    def test_notes_are_bound_to_tag_before_and_after_worker(self):
        stale = self.dispatch("updates.notes", {"tag": "v1.0.0"})
        self.assertFalse(stale["ok"])
        with mock.patch.object(server, "release_notes_view", return_value={
                "ok": True, "tag": "v2.0.0", "name": "two", "published": "", "url": "", "body": ""}) as notes:
            operation = self.dispatch("updates.notes", {"tag": "v2.0.0"})["result"]["operation"]["id"]
            self.assertEqual(self.poll_done(operation)["tag"], "v2.0.0")
            notes.assert_called_once_with(expected_tag="v2.0.0")

    def test_unknown_poll_and_caps_are_bounded(self):
        missing = self.dispatch("updates.poll", {"id": "missing"})
        self.assertFalse(missing["ok"])
        with server._UPDATE_OP_LOCK:
            server._UPDATE_OPS = {str(i): {"status": "running"} for i in range(server._UPDATE_OP_ACTIVE_MAX)}
        self.assertEqual(server._start_update_operation("check", {"force": False})["errors"],
                         ["too many update operations"])
        with server._UPDATE_OP_LOCK:
            server._UPDATE_OPS = {str(i): {"status": "done", "ended": time.monotonic(), "result": {"ok": True}}
                                  for i in range(server._UPDATE_OP_TOTAL_MAX)}
        self.assertEqual(server._start_update_operation("check", {"force": False})["errors"],
                         ["too many update operations"])

    def test_start_uses_existing_transactional_helper_without_native_network(self):
        expected = {"ok": True, "job": {"id": "job", "title": "Update", "status": "running", "cmd": "update"}}
        with mock.patch.object(server, "start_update_job", return_value=(expected["job"], [])) as start:
            response = self.dispatch("updates.start", {})
        self.assertEqual(response["result"], expected)
        start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
