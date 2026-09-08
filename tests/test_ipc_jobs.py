"""Deterministic native job protocol and bounded-stream checks."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from scm_workbench import ipc, server


class NativeJobsTests(unittest.TestCase):
    def setUp(self):
        self.old = server.JOBS
        server.JOBS = {
            "a": {
                "id": "a", "ts": 1, "kind": "fixture", "title": "Fixture",
                "status": "running", "exit_code": None, "cmd": "fixture",
                "log_lines": ["zero", "one", "two"], "first_seq": 0,
                "subs": [], "warnings": [],
            },
        }

    def tearDown(self):
        server.JOBS = self.old

    def call(self, method, params):
        return ipc.dispatch({"id": "test", "method": method, "params": params})

    def test_list_log_poll_and_missing_are_structured(self):
        listed = self.call("jobs.list", {})
        self.assertEqual(listed["result"]["jobs"][0]["id"], "a")
        log = self.call("jobs.log", {"job_id": "a", "after": 1, "max_lines": 1})
        self.assertEqual(log["result"]["lines"], ["one"])
        self.assertEqual(log["result"]["next_seq"], 2)
        polled = self.call("jobs.poll", {
            "cursors": [{"job_id": "a", "after": 0}, {"job_id": "gone", "after": 4}],
            "max_events": 2,
        })["result"]["jobs"]
        self.assertEqual([line["i"] for line in polled[0]["lines"]], [0, 1])
        self.assertEqual(polled[0]["next_seq"], 2)
        self.assertEqual((polled[1]["status"], polled[1]["complete"]), ("missing", True))

    def test_server_error_is_internal_not_bad_request(self):
        with mock.patch.object(server, "list_jobs", side_effect=ValueError("server bug")), \
                mock.patch.object(ipc.traceback, "print_exc") as trace:
            result = self.call("jobs.list", {})
        self.assertEqual(result, {"id": "test", "ok": False,
                                  "error": {"code": "internal", "message": "request handler failed"}})
        trace.assert_called_once()

    def test_strict_params_and_start_kill_contracts(self):
        bad = self.call("jobs.list", {"unexpected": True})
        self.assertEqual(bad["error"]["code"], "bad_request")
        bad = self.call("jobs.poll", {"cursors": [], "max_events": 257})
        self.assertEqual(bad["ok"], False)
        self.assertEqual(bad["error"]["code"], "bad_request")
        bad = self.call("jobs.start", {"kind": "fixture", "args": {}, "extra": 1})
        self.assertEqual(bad["error"]["code"], "bad_request")

        started = {"id": "new", "title": "Fixture", "status": "running",
                   "cmd": "fixture", "warnings": ["warn"]}
        with mock.patch.object(server, "start_job", return_value=(started, [])):
            result = self.call("jobs.start", {"kind": "fixture", "args": {}})
        self.assertEqual(result["result"], {"ok": True, "job": started})
        with mock.patch.object(server, "start_job", return_value=(None, ["bad args"])):
            result = self.call("jobs.start", {"kind": "fixture", "args": {}})
        self.assertEqual(result["result"], {"ok": False, "errors": ["bad args"]})
        with mock.patch.object(server, "kill_job", return_value=True) as kill:
            result = self.call("jobs.kill", {"job_id": "a"})
        self.assertEqual(result["result"], {"ok": True})
        kill.assert_called_once_with("a")

        server.JOBS["a"]["proc"] = None
        self.assertFalse(server.kill_job("a"))
        self.assertNotIn("kill_requested", server.JOBS["a"])

    def test_long_lines_and_poll_frame_stay_bounded_and_fair(self):
        long_lines = ["x" * (server.JOB_LINE_MAX_BYTES * 2)] * 256
        for i in range(32):
            server.JOBS[str(i)] = dict(server.JOBS["a"], id=str(i), log_lines=long_lines)
        response = self.call("jobs.poll", {
            "cursors": [{"job_id": str(i), "after": 0} for i in range(32)],
            "max_events": 256,
        })
        encoded = (json.dumps(response, ensure_ascii=False) + "\n").encode()
        self.assertLess(len(encoded), server.IPC_POLL_MAX_BYTES)
        batches = response["result"]["jobs"]
        self.assertTrue(all(batch["lines"] for batch in batches), "later cursors must not be starved")
        for batch in batches:
            for line in batch["lines"]:
                self.assertLessEqual(len(line["s"].encode()), server.JOB_LINE_MAX_BYTES)
                self.assertTrue(batch["truncated"])

    def test_terminal_sse_replays_final_lines_before_done(self):
        server.JOBS["a"]["status"] = "ok"
        server.JOBS["a"]["exit_code"] = 0
        events = list(server.sse_stream("a", 0))
        self.assertEqual([event for event, _ in events], ["line", "line", "line", "done"])

    def test_slow_subscriber_wakeup_is_bounded_and_terminal_survives(self):
        q = __import__("queue").Queue(maxsize=1)
        q.put(("line", 0, "old"))
        server._notify_subscribers(server.JOBS["a"], [q], ("line", 1, "new"))
        self.assertLessEqual(q.qsize(), 1)
        server._notify_subscribers(server.JOBS["a"], [q], ("done", "ok", 0), terminal=True)
        self.assertEqual(q.get_nowait()[0], "done")

    def test_stop_all_reaps_processes_and_joins_pumps(self):
        proc = mock.Mock()
        pump = mock.Mock()
        server.JOBS["a"].update(proc=proc, pump_thread=pump)
        with mock.patch.object(server, "kill_job", return_value=True) as kill:
            server.stop_all_jobs(timeout=0.01)
        kill.assert_called_once_with("a")
        proc.wait.assert_called()
        pump.join.assert_called()

    @unittest.skipIf(os.name == "nt", "POSIX process-group fallback")
    def test_force_kill_targets_the_jobs_fixed_process_group(self):
        proc = mock.Mock(pid=12345)
        with mock.patch.object(server.os, "killpg") as killpg:
            server._kill_process_group(proc)
        killpg.assert_called_once_with(12345, signal.SIGKILL)
        proc.kill.assert_not_called()

    def test_windows_force_kill_targets_exact_managed_pid_tree(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        completed = mock.Mock(returncode=0)
        with mock.patch.object(server.os, "name", "nt"), \
             mock.patch.object(server.subprocess, "run", return_value=completed) as run:
            server._kill_process_group(proc)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "4321", "/T", "/F"])
        self.assertFalse(run.call_args.kwargs["shell"])
        proc.kill.assert_not_called()

        reaped = mock.Mock(pid=4321)
        reaped.poll.return_value = 0
        with mock.patch.object(server.os, "name", "nt"), \
             mock.patch.object(server.subprocess, "run") as stale_run:
            server._kill_process_group(reaped)
        stale_run.assert_not_called()
        reaped.kill.assert_not_called()

    def test_kill_job_never_signals_an_already_reaped_pid(self):
        proc = mock.Mock(pid=777)
        proc.poll.return_value = 0
        server.JOBS["a"].update(
            proc=proc, status="running", proc_lock=threading.Lock(),
            proc_reaped=True,
        )
        with mock.patch.object(server, "_kill_process_group") as force:
            self.assertFalse(server.kill_job("a"))
        force.assert_not_called()
        self.assertNotIn("kill_requested", server.JOBS["a"])

    @unittest.skipIf(os.name == "nt", "POSIX process-group integration")
    def test_kill_job_reaps_a_term_resistant_grandchild_group(self):
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
            "print(p.pid,flush=True); time.sleep(60)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        grandchild = int(proc.stdout.readline().strip())
        server.JOBS["a"].update(proc=proc, status="running")
        try:
            self.assertTrue(server.kill_job("a"))
            proc.wait(timeout=3)
            for _ in range(40):
                state = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(grandchild)],
                    text=True, capture_output=True,
                ).stdout.strip()
                if not state or state.startswith("Z"):
                    break
                time.sleep(0.05)
            else:
                self.fail("grandchild survived the managed job-group kill")
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
