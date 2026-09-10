"""P0 stdio inheritance checks for external UI helpers."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import server


class ExternalProcessStdioTests(unittest.TestCase):
    def test_macos_helpers_detach_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file.txt"
            path.write_text("fixture", encoding="utf-8")
            opened = server.subprocess.CompletedProcess([], 0)
            with mock.patch.object(server.os, "name", "posix"), \
                 mock.patch.object(server.sys, "platform", "darwin"), \
                 mock.patch.object(server.subprocess, "run", return_value=opened) as run, \
                 mock.patch.object(server.subprocess, "Popen") as popen:
                self.assertIsNone(server.reveal_path(path))
                self.assertIsNone(server.open_path(path))
                self.assertIsNone(server.open_url("https://example.test"))

            self.assertEqual(popen.call_count, 2)
            for call in [*popen.call_args_list, run.call_args]:
                self.assertIs(call.kwargs["stdout"], server.subprocess.DEVNULL)
                self.assertIs(call.kwargs["stderr"], server.subprocess.DEVNULL)
                self.assertTrue(call.kwargs["start_new_session"])

    def test_windows_reveal_detaches_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "folder"
            path.mkdir()
            with mock.patch.object(server.os, "name", "nt"), \
                 mock.patch.object(server.subprocess, "Popen") as popen:
                self.assertIsNone(server.reveal_path(path))

            popen.assert_called_once()
            kwargs = popen.call_args.kwargs
            self.assertIs(kwargs["stdout"], server.subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], server.subprocess.DEVNULL)
            self.assertEqual(kwargs["creationflags"], 0x08000000 | 0x00000200)


if __name__ == "__main__":
    unittest.main()
