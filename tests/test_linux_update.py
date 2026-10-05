from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scm_workbench import linux_update as linux, updater


class LinuxUpdateTests(unittest.TestCase):
    def test_package_versions_match_builders(self):
        from scripts.build_linux_arch import arch_version
        from scripts.build_linux_deb import debian_version
        for version in ("0.9.1", "0.9.1-beta.2", "1.2.3-1.2", "1.2.3-rc-one.2+build.7"):
            self.assertEqual(linux.package_version("v" + version, "deb"), debian_version(version))
            self.assertEqual(linux.package_version(version, "arch"), arch_version(version) + "-1")
        for value in ("../../foo", "1.2", "--option", "1.2.3\n", "a" * 129):
            with self.assertRaises(linux.InstallError):
                linux.package_version(value, "deb")

    def test_availability_fails_closed(self):
        for error in (OSError("missing pkexec"), ValueError("outside root"), linux.InstallError("wrong owner")):
            with patch.object(linux, "context", side_effect=error):
                self.assertFalse(linux.available())
        with patch.object(linux, "context", return_value=(1, 2, 3, "deb")):
            self.assertTrue(linux.available())
        with patch.object(linux, "available", return_value=True):
            self.assertEqual(updater.install_mode("linux"), "package")
            self.assertEqual(updater.install_mode("manjaro"), "package")
            self.assertEqual(updater.install_mode("darwin"), "automatic")

    def test_untrusted_installed_helper_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "helper.py"
            path.write_text("malicious")
            path.chmod(0o666)
            with self.assertRaises(linux.InstallError):
                linux.trusted(path)
            with self.assertRaises(ValueError):
                linux.trusted(path, under=Path(temp) / "elsewhere")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "POSIX file boundary")
    def test_copy_is_private_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.deb"
            target = Path(temp) / "copy.deb"
            source.write_bytes(b"verified package")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            linux.copy_verified(source, target, digest, os.getuid())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(linux.InstallError):
                linux.copy_verified(source, Path(temp) / "bad", "0" * 64, os.getuid())
            with self.assertRaises(linux.InstallError):
                linux.copy_verified(source, Path(temp) / "owner", digest, os.getuid() + 1)
            with patch.object(linux, "MAX_PACKAGE_BYTES", 1):
                with self.assertRaises(linux.InstallError):
                    linux.copy_verified(source, Path(temp) / "large", digest, os.getuid())
            source.unlink()
            source.symlink_to(target)
            with self.assertRaises(OSError):
                linux.copy_verified(source, Path(temp) / "symlink", digest, os.getuid())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "POSIX file boundary")
    def test_hardlink_empty_and_low_disk_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.write_bytes(b"bytes")
            digest = hashlib.sha256(b"bytes").hexdigest()
            os.link(source, Path(temp) / "hardlink")
            with self.assertRaises(linux.InstallError):
                linux.copy_verified(source, Path(temp) / "copy", digest, os.getuid())
            (Path(temp) / "hardlink").unlink()
            with patch.object(linux.shutil, "disk_usage", return_value=Mock(free=1)):
                with self.assertRaises(linux.InstallError):
                    linux.copy_verified(source, Path(temp) / "copy", digest, os.getuid())
            source.write_bytes(b"")
            with self.assertRaises(linux.InstallError):
                linux.copy_verified(source, Path(temp) / "copy", digest, os.getuid())
            source.unlink()
            os.mkfifo(source)
            with self.assertRaises(linux.InstallError):
                linux.copy_verified(source, Path(temp) / "copy", digest, os.getuid())

    def test_exact_manager_commands(self):
        with patch.object(linux, "trusted", side_effect=lambda p: p):
            apt = linux.manager_command("deb", Path("/var/tmp/private/update.deb"))
            self.assertEqual(apt[0], "/usr/bin/apt-get")
            self.assertEqual(apt[-2:], ["install", "/var/tmp/private/update.deb"])
            self.assertIn("DPkg::Lock::Timeout=120", apt)
            self.assertEqual(linux.manager_command("arch", Path("/var/tmp/private/update.pkg.tar.zst")),
                             ["/usr/bin/pacman", "--noconfirm", "-U", "/var/tmp/private/update.pkg.tar.zst"])
            with self.assertRaises(linux.InstallError):
                linux.manager_command("unknown", Path("--exec"))

    def test_package_metadata_bound_to_name_version_architecture(self):
        for kind, raw, expected in (
            ("deb", "Package: scm-workbench\nVersion: 0.9.1~beta.2\nArchitecture: amd64\n", "0.9.1~beta.2"),
            ("arch", "pkgname = scm-workbench\npkgver = 0.9.1beta.2-1\narch = x86_64\ndepend = gtk3\ndepend = polkit\n", "0.9.1beta.2-1"),
        ):
            for changed in (raw, raw.replace("scm-workbench", "other-app"),
                            raw.replace(expected, "0.0.1"), raw.replace("amd64", "arm64").replace("x86_64", "aarch64"),
                            raw + ("Package: other\n" if kind == "deb" else "pkgname = other\n")):
                with tempfile.TemporaryDirectory() as temp:
                    def inspect(command, **kwargs):
                        kwargs["stdout"].write(changed.encode())
                        self.assertEqual(kwargs["timeout"], 30)
                        self.assertIs(kwargs["preexec_fn"], linux.inspect_limits)
                        return subprocess.CompletedProcess(command, 0)
                    with patch.object(linux, "trusted", side_effect=lambda p: p), patch.object(linux.subprocess, "run", side_effect=inspect):
                        if changed == raw:
                            linux.inspect_package(Path(temp) / "package", kind, expected, Path(temp))
                        else:
                            with self.assertRaises(linux.InstallError):
                                linux.inspect_package(Path(temp) / "package", kind, expected, Path(temp))

    def test_authentication_denial_and_fixed_isolated_helper(self):
        context = (Path("/usr/lib/scm-workbench/python"), Path("/usr/lib/scm-workbench/helper.py"), Path("/usr/bin/pkexec"), "deb")
        with patch.object(linux, "context", return_value=context), patch.object(linux.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 126, b"", b"cancelled")
            with self.assertRaisesRegex(linux.InstallError, "approval was cancelled"):
                linux.install(Path("/home/user/package.deb"), "a" * 64, "v0.9.1", "deb")
            command = run.call_args.args[0]
            self.assertEqual(command[:4], [str(context[2]), str(context[0]), "-I", str(context[1])])
            self.assertNotIn("shell", run.call_args.kwargs)
            run.reset_mock()
            with self.assertRaises(linux.InstallError):
                linux.install(Path("/home/user/package"), "a" * 64, "v0.9.1", "arch")
            run.assert_not_called()
            with self.assertRaises(linux.InstallError):
                linux.install(Path("/home/user/package"), "malicious", "v0.9.1", "deb")
            run.assert_not_called()

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX privilege boundary")
    def test_root_and_authorized_caller_required(self):
        with patch.object(linux.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(linux.InstallError, "approval"):
                linux.install_privileged(Path("/tmp/package"), "a" * 64, "v1.2.3", "deb")
        with patch.object(linux.os, "geteuid", return_value=0), patch.dict(os.environ, {"PKEXEC_UID": "0"}):
            with self.assertRaisesRegex(linux.InstallError, "OS prompt"):
                linux.install_privileged(Path("/tmp/package"), "a" * 64, "v1.2.3", "deb")

    def test_package_job_no_candidate_swap_and_only_restarts_after_success(self):
        for outcome in (None, linux.InstallError("administrator approval cancelled")):
            with tempfile.TemporaryDirectory() as temp:
                digest = "a" * 64
                asset = {"name": updater.LINUX_DEB_ASSET, "tag": "v0.9.1", "digest": "sha256:" + digest,
                         "url": "https://github.com/mallen86/scm-workbench/releases/download/v0.9.1/" + updater.LINUX_DEB_ASSET, "size": 10}
                rel = {"tag": "v0.9.1", "assets": [asset], "prerelease": False}
                begin = Mock()
                plan = {"current": "0.9.0", "latest": "v0.9.1", "channel": "stable", "bundle": str(linux.ROOT),
                        "work": Path(temp), "begin_package_install": begin}
                job = {"log_lines": [], "subs": [], "started": time.time()}
                with patch.object(updater, "install_mode", return_value="package"), \
                     patch.object(updater, "package_format", return_value="deb"), \
                     patch.object(updater, "latest_release", return_value=rel), \
                     patch.object(updater, "pick_asset", return_value=asset), \
                     patch.object(updater, "download") as download, \
                     patch.object(updater, "prepare_asset") as prepare, \
                     patch.object(updater, "_begin_handoff") as handoff, \
                     patch.object(linux, "install", side_effect=outcome) as install:
                    download.side_effect = lambda url, dest, **kwargs: dest.write_bytes(b"downloaded")
                    updater.run_job(job, plan, io.StringIO())
                    begin.assert_called_once()
                    prepare.assert_not_called()
                    handoff.assert_not_called()
                    install.assert_called_once()
                    self.assertEqual(job["status"], "ok" if outcome is None else "fail")
                    self.assertEqual(job["progress"].get("restart_required", False), outcome is None)

    def test_privileged_release_verification_rejects_forged_hash_draft_and_duplicate_assets(self):
        asset = {"name": linux.ASSETS["deb"], "digest": "sha256:" + "a" * 64, "size": 5, "state": "uploaded"}
        good = {"tag_name": "v0.9.1", "draft": False, "assets": [asset]}
        cases = [good, {**good, "draft": True}, {**good, "tag_name": "v0.9.0"},
                 {**good, "assets": [asset, asset]},
                 {**good, "assets": [{**asset, "digest": "sha256:" + "b" * 64}]},
                 {**good, "assets": [{**asset, "size": True}]},
                 {**good, "assets": [{**asset, "name": linux.ASSETS["arch"]}]}]
        for release in cases:
            response = io.BytesIO(json.dumps(release).encode())
            response.status = 200
            opener = Mock()
            opener.open.return_value = response
            with patch.object(Path, "is_file", return_value=True), \
                 patch.object(linux, "trusted", side_effect=lambda p: p), \
                 patch.object(linux.ssl, "create_default_context"), \
                 patch.object(linux.urllib.request, "build_opener", return_value=opener):
                if release == good:
                    self.assertEqual(linux.verify_release("v0.9.1", "deb", "a" * 64), 5)
                    request = opener.open.call_args.args[0]
                    self.assertEqual(request.full_url, "https://api.github.com/repos/mallen86/scm-workbench/releases/tags/v0.9.1")
                else:
                    with self.assertRaises(linux.InstallError):
                        linux.verify_release("v0.9.1", "deb", "a" * 64)
        with self.assertRaises(linux.InstallError):
            linux.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/")

    def test_root_release_response_bounded(self):
        response = io.BytesIO(b"x" * (1024 * 1024 + 1))
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(linux, "trusted", side_effect=lambda p: p), \
             patch.object(linux.ssl, "create_default_context"), \
             patch.object(linux.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(linux.InstallError, "size or time limit"):
                linux.verify_release("v0.9.1", "deb", "a" * 64)

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX privilege boundary")
    def test_root_transaction_verifies_before_install_and_checks_installed_version(self):
        for failed in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                manager = Mock()
                manager.__enter__ = Mock(return_value=temp)
                manager.__exit__ = Mock(return_value=False)
                with patch.object(linux.os, "geteuid", return_value=0), \
                     patch.dict(os.environ, {"PKEXEC_UID": "1000"}), \
                     patch.object(linux, "family", return_value="deb"), \
                     patch.object(linux.tempfile, "TemporaryDirectory", return_value=manager), \
                     patch.object(linux, "verify_release", return_value=5) as release, \
                     patch.object(linux, "copy_verified") as copied, \
                     patch.object(linux, "inspect_package") as inspect, \
                     patch.object(linux, "installed_version", return_value="0.9.0"), \
                     patch.object(linux, "trusted", side_effect=lambda p: p), \
                     patch.object(linux, "run_manager", side_effect=linux.InstallError("package failed") if failed else None) as run, \
                     patch.object(linux, "verify_installed") as verified:
                    if failed:
                        with self.assertRaises(linux.InstallError):
                            linux.install_privileged(Path("/tmp/download.deb"), "a" * 64, "v0.9.1", "deb", downgrade=True)
                        verified.assert_not_called()
                    else:
                        linux.install_privileged(Path("/tmp/download.deb"), "a" * 64, "v0.9.1", "deb", downgrade=True)
                        verified.assert_called_once()
                    release.assert_called_once()
                    copied.assert_called_once()
                    self.assertEqual(copied.call_args.kwargs["expected_size"], 5)
                    inspect.assert_called_once()
                    self.assertIn("--allow-downgrades", run.call_args.args[0])
                    self.assertEqual(run.call_args.args[1]["DEBIAN_FRONTEND"], "noninteractive")

    @unittest.skipUnless(os.name == "posix", "POSIX pipe and process-group boundary")
    def test_manager_output_and_failure_are_bounded(self):
        with self.assertRaises(linux.InstallError) as caught:
            linux.run_manager([sys.executable, "-c", "import sys; print('x'*200000); sys.exit(1)"], dict(os.environ))
        self.assertLess(len(str(caught.exception)), 1100)
        linux.run_manager([sys.executable, "-c", "print('installed')"], dict(os.environ))

    def test_installed_package_version_required_before_success(self):
        for kind, text, expected in (
            ("deb", "installed\n0.9.1~beta.2\namd64\n", "0.9.1~beta.2"),
            ("arch", "Name : scm-workbench\nVersion : 0.9.1beta.2-1\nArchitecture : x86_64\n", "0.9.1beta.2-1"),
        ):
            with tempfile.TemporaryDirectory() as temp:
                def query(command, **kwargs):
                    kwargs["stdout"].write(text.encode())
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(linux, "trusted", side_effect=lambda p: p), patch.object(linux.subprocess, "run", side_effect=query):
                    linux.verify_installed(kind, expected, Path(temp))
                    with self.assertRaises(linux.InstallError):
                        linux.verify_installed(kind, "0.0.0", Path(temp))

    def test_checksum_required_before_download_or_prompt(self):
        asset = {"tag": "v0.9.1", "name": updater.LINUX_DEB_ASSET, "digest": None}
        job = {"log_lines": [], "subs": [], "started": time.time()}
        with patch.object(updater, "install_mode", return_value="package"), \
             patch.object(updater, "latest_release", return_value={"tag": "v0.9.1", "assets": [asset]}), \
             patch.object(updater, "pick_asset", return_value=asset), patch.object(updater, "download") as download:
            updater.run_job(job, {"current": "0.9.0"}, io.StringIO())
            self.assertEqual(job["status"], "fail")
            self.assertIn("checksum", " ".join(job["log_lines"]))
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
