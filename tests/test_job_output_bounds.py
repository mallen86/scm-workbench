"""Ordinary job transcripts and numeric form values stay bounded and total."""

import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import server


class FinishedProcess:
    def __init__(self, output: bytes):
        self.stdout = io.BytesIO(output)

    @staticmethod
    def poll():
        return 0


class RetainedLog(io.StringIO):
    """The pump closes its log; keep the written transcript inspectable."""

    def close(self):
        pass


class JobOutputBoundsTests(unittest.TestCase):
    def run_pump(self, output: bytes):
        job = {"id": "bounded", "kind": "create_pdf", "started": time.time(),
               "subs": [], "log_lines": []}
        log = RetainedLog()
        with mock.patch.object(server, "_persist_jobs"), \
                mock.patch.object(server, "_notify_subscribers"):
            server._pump(job, FinishedProcess(output), log)
        return job, log.getvalue()

    def test_line_without_newline_is_clipped_instead_of_buffered(self):
        progress = b"\r".join(b"progress %d%%" % i for i in range(200_000))
        job, log = self.run_pump(progress + b"\nfinished\n")
        self.assertEqual(job["status"], "ok")
        first, last = job["log_lines"][0], job["log_lines"][1]
        self.assertTrue(first.endswith("… [line truncated]"))
        self.assertLessEqual(len(first.encode("utf-8")), server.JOB_LINE_MAX_BYTES + 64)
        self.assertEqual(last, "finished")
        self.assertLess(len(log), 2 * server.JOB_LINE_MAX_BYTES)

    def test_ordinary_job_log_file_has_a_size_budget(self):
        job = {"id": "budget", "kind": "create_pdf", "subs": [], "log_lines": []}
        log = io.StringIO()
        with mock.patch.object(server, "JOB_LOG_FILE_MAX_BYTES", 100), \
                mock.patch.object(server, "_notify_subscribers"):
            for index in range(50):
                server._append_job_line(job, f"line {index:03d}", log_f=log)
        written = log.getvalue()
        self.assertLessEqual(len(written.split("(further output")[0].encode()), 100)
        self.assertEqual(written.count("size limit reached"), 1)
        # The live transcript is unaffected by the on-disk budget.
        self.assertEqual(len(job["log_lines"]), 50)

    def test_persisted_log_reads_only_a_bounded_tail_with_stable_indexes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "job.log"
            body = "".join(f"line {i}\n" for i in range(10))
            body += "x" * (server.JOB_LINE_MAX_BYTES * 3) + "\nlast\rafter-cr\n"
            path.write_text(body, encoding="utf-8")
            first, lines = server._read_log_tail(str(path), 4)
            self.assertEqual(first, 9)
            self.assertEqual(lines[0], "line 9")
            self.assertTrue(lines[1].endswith("… [line truncated]"))
            self.assertEqual(lines[2:], ["last", "after-cr"])

            record = {"id": "old", "status": "ok", "exit_code": 0, "cmd": "x",
                      "log_file": str(path)}
            with mock.patch.object(server, "_job_record", return_value=None), \
                    mock.patch.object(server, "_persisted_record", return_value=record), \
                    mock.patch.object(server, "JOB_LOG_MAX_LINES", 4):
                log = server.get_job_log("old", 0, 4)
            self.assertEqual(log["first_seq"], 9)
            self.assertTrue(log["gap"])
            self.assertEqual(log["lines"][0], "line 9")
            self.assertEqual(log["next_seq"], 13)


class NumericNormalizationTests(unittest.TestCase):
    SPEC = {"groups": [{"options": [
        {"key": "n", "label": "N", "type": "number", "default": 5},
        {"key": "r", "label": "R", "type": "range", "min": 0, "max": 10, "default": 3},
    ]}]}

    def test_non_finite_numbers_are_validation_errors_not_crashes(self):
        for value in ("1e999", "-1e999", "inf", "nan", float("inf")):
            args, errors, _warnings = server.normalize_args(self.SPEC, {"n": value, "r": value})
            self.assertEqual(args, {"n": 5, "r": 3}, value)
            self.assertEqual(errors, [f"N: “{value}” is not a number."], value)

    def test_finite_values_are_unchanged(self):
        args, errors, warnings = server.normalize_args(self.SPEC, {"n": "7", "r": "12"})
        self.assertEqual((args, errors), ({"n": 7, "r": 12}, []))
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
