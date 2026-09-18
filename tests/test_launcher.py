import unittest
from unittest.mock import patch

from scm_workbench import launcher


class RuntimeDownloadTests(unittest.TestCase):
    def runtime_name(self, system, machine):
        with patch.object(launcher.sys, "platform", system), \
                patch("platform.machine", return_value=machine):
            return launcher._runtime_download_name()

    def test_linux_runtime_names_are_explicit_gnu_archives(self):
        release = "20260814"
        version = launcher.RUNTIME_VERSION
        self.assertEqual(
            self.runtime_name("linux", "x86_64"),
            f"cpython-{version}+{release}-x86_64-unknown-linux-gnu-pgo+lto-full.tar.zst",
        )
        self.assertEqual(
            self.runtime_name("linux", "AMD64"),
            f"cpython-{version}+{release}-x86_64-unknown-linux-gnu-pgo+lto-full.tar.zst",
        )
        self.assertEqual(
            self.runtime_name("linux", "aarch64"),
            f"cpython-{version}+{release}-aarch64-unknown-linux-gnu-pgo+lto-full.tar.zst",
        )

    def test_linux_runtime_rejects_unknown_architectures(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported Linux architecture"):
            self.runtime_name("linux", "riscv64")

    def test_existing_macos_and_windows_runtime_names_remain_pinned(self):
        release = "20260814"
        version = launcher.RUNTIME_VERSION
        self.assertEqual(
            self.runtime_name("darwin", "arm64"),
            f"cpython-{version}+{release}-aarch64-apple-darwin-pgo+lto-full.tar.zst",
        )
        self.assertEqual(
            self.runtime_name("win32", "AMD64"),
            f"cpython-{version}+{release}-x86_64-pc-windows-msvc-pgo-full.tar.zst",
        )

    def test_unknown_runtime_platform_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported runtime platform"):
            self.runtime_name("freebsd14", "x86_64")


if __name__ == "__main__":
    unittest.main()
