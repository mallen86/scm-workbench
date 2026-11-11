"""Windows 10-only, packaged-worker native TLS selection and secure defaults."""

import os
import ssl
import sys
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

from scm_workbench import tls


def assert_untrusted_local_server_rejected(test, context):
    import socket
    import threading
    import urllib.error
    import urllib.request
    from pathlib import Path

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(Path(__file__).parent / "fixtures" / "localhost-untrusted.pem"))
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve_once():
            try:
                connection, _ = listener.accept()
                with connection:
                    with server_context.wrap_socket(connection, server_side=True) as secured:
                        # Native truststore verifies after the TLS handshake.
                        # Read once so a client's rejection alert is consumed.
                        secured.settimeout(5)
                        secured.recv(1)
            except (ssl.SSLError, ConnectionError):
                # Native verification runs after the handshake. Rejecting the
                # peer can close/reset the socket without sending a TLS alert.
                pass
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve_once, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        try:
            with test.assertRaises(urllib.error.URLError) as caught:
                opener.open(f"https://127.0.0.1:{listener.getsockname()[1]}/", timeout=5)
            test.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
        finally:
            worker.join(6)
        test.assertFalse(worker.is_alive(), "local TLS server did not stop")
        test.assertEqual(errors, [])


def windows_version(build=19045, *, product_type=1):
    # The gate uses the registry build, not a manifest-adjusted OS version.
    return types.SimpleNamespace(registry_build=build, product_type=product_type)


class WindowsTLSSelectionTests(unittest.TestCase):
    def test_only_windows10_workstation_builds_are_selected(self):
        for build, expected in ((10239, False), (10240, True), (17763, True),
                                (19045, True), (21999, True), (22000, False),
                                (22631, False), (26100, False)):
            with self.subTest(build=build), \
                 patch.object(tls.sys, "platform", "win32"), \
                 patch.dict(os.environ, {"SCM_WORKBENCH_PACKAGED": "1"}), \
                 patch.object(tls, "_is_windows_bundle", return_value=True), \
                 patch.object(tls, "_windows_build", return_value=build), \
                 patch.object(tls.sys, "getwindowsversion", create=True,
                              return_value=windows_version(build)):
                self.assertEqual(tls._uses_windows10_truststore(), expected)

    def test_source_non_windows_server_and_other_versions_are_unchanged(self):
        cases = [
            ("darwin", "1", windows_version()),
            ("linux", "1", windows_version()),
            ("win32", "0", windows_version()),
            ("win32", "", windows_version()),
            ("win32", "1", windows_version(product_type=2)),
            ("win32", "1", windows_version(product_type=3)),
            ("win32", "1", windows_version(9600)),
            ("win32", "1", windows_version(30000)),
            ("win32", "1", windows_version(22631)),
        ]
        # Any attempt to import truststore here fails the test, even when the
        # dependency happens to be installed in the developer's environment.
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name == "truststore":
                raise AssertionError("truststore imported outside packaged Windows 10")
            return original_import(name, *args, **kwargs)

        for platform, packaged, version in cases:
            with self.subTest(platform=platform, packaged=packaged, version=version), \
                 patch.object(tls.sys, "platform", platform), \
                 patch.dict(os.environ, {"SCM_WORKBENCH_PACKAGED": packaged}), \
                 patch.object(tls, "_is_windows_bundle", return_value=True), \
                 patch.object(tls, "_windows_build", return_value=version.registry_build), \
                 patch.object(tls.sys, "getwindowsversion", create=True, return_value=version), \
                 patch("builtins.__import__", side_effect=guarded_import):
                self.assertEqual(tls.urlopen_kwargs(), {})

    def test_installed_os_build_uses_the_64bit_registry_view(self):
        registry = MagicMock(KEY_READ=1, KEY_WOW64_64KEY=0x100)
        registry.QueryValueEx.return_value = ("19045", 1)
        with patch.dict(sys.modules, {"winreg": registry}):
            self.assertEqual(tls._windows_build(), 19045)
        registry.OpenKey.assert_called_once_with(
            registry.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", 0, 0x101)
        registry.QueryValueEx.assert_called_once_with(
            registry.OpenKey.return_value.__enter__.return_value, "CurrentBuildNumber")

    def test_unreadable_or_invalid_registry_version_keeps_existing_tls(self):
        for failure in (OSError("blocked"), ValueError("malformed"), TypeError("bad value")):
            registry = MagicMock()
            registry.QueryValueEx.side_effect = failure
            with self.subTest(failure=failure), patch.dict(sys.modules, {"winreg": registry}):
                self.assertIsNone(tls._windows_build())
        with patch.object(tls.sys, "platform", "win32"), \
             patch.dict(os.environ, {"SCM_WORKBENCH_PACKAGED": "1"}), \
             patch.object(tls, "_is_windows_bundle", return_value=True), \
             patch.object(tls.sys, "getwindowsversion", create=True,
                          return_value=windows_version()), \
             patch.object(tls, "_windows_build", return_value=None), \
             patch.dict(sys.modules, {"truststore": None}):
            self.assertEqual(tls.urlopen_kwargs(), {})

    def test_source_launcher_packaged_flag_does_not_enable_native_tls(self):
        with patch.object(tls.sys, "platform", "win32"), \
             patch.dict(os.environ, {"SCM_WORKBENCH_PACKAGED": "1"}), \
             patch.object(tls.sys, "getwindowsversion", create=True,
                          return_value=windows_version()), \
             patch.object(tls, "_is_windows_bundle", return_value=False), \
             patch.dict(sys.modules, {"truststore": None}):
            self.assertEqual(tls.urlopen_kwargs(), {})

    def test_bundle_gate_requires_app_layout_and_shell(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scm_workbench" / "tls.py"
            with patch.object(tls, "__file__", str(source)):
                self.assertFalse(tls._is_windows_bundle())
            bundled = root / "app" / "scm_workbench" / "tls.py"
            with patch.object(tls, "__file__", str(bundled)):
                self.assertFalse(tls._is_windows_bundle())
                (root / "SCM Workbench.exe").touch()
                self.assertTrue(tls._is_windows_bundle())
            with patch.object(tls, "__file__", str(source)):
                self.assertFalse(tls._is_windows_bundle())

    def test_native_context_is_verified_request_local_and_uses_http11(self):
        native = Mock()
        original_context = ssl.SSLContext
        with patch.object(tls, "_uses_windows10_truststore", return_value=True), \
             patch.dict(sys.modules, {"truststore": native}):
            options = tls.urlopen_kwargs()
        native.SSLContext.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        context = options["context"]
        self.assertIs(context, native.SSLContext.return_value)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(context.check_hostname, True)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        context.set_alpn_protocols.assert_called_once_with(["http/1.1"])
        native.inject_into_ssl.assert_not_called()
        self.assertIs(ssl.SSLContext, original_context)

    def test_local_negative_fixture_rejects_with_standard_verified_tls(self):
        assert_untrusted_local_server_rejected(self, ssl.create_default_context())

    def test_missing_dependency_or_native_initialization_failure_is_final(self):
        with patch.object(tls, "_uses_windows10_truststore", return_value=True), \
             patch.dict(sys.modules, {"truststore": None}):
            with self.assertRaisesRegex(RuntimeError, "reinstall the Windows app package"):
                tls.urlopen_kwargs()
        native = Mock()
        native.SSLContext.side_effect = OSError("native verifier unavailable")
        with patch.object(tls, "_uses_windows10_truststore", return_value=True), \
             patch.dict(sys.modules, {"truststore": native}):
            with self.assertRaisesRegex(OSError, "native verifier unavailable"):
                tls.urlopen_kwargs()
        native.SSLContext.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "requires native Windows CryptoAPI")
class NativeWindowsTLSBundleTests(unittest.TestCase):
    def setUp(self):
        try:
            import truststore
        except ImportError:
            self.skipTest("source test environment has no truststore; required in bundle smoke")
        self.truststore = truststore

    def test_real_dependency_can_build_a_verified_context_without_injection(self):
        original_context = ssl.SSLContext
        # CI may run Windows 11. Exercise the Windows 10 context explicitly so
        # packaging still validates the real dependency and Windows DLL APIs.
        with patch.object(tls, "_uses_windows10_truststore", return_value=True):
            context = tls.urlopen_kwargs()["context"]
        self.assertIsInstance(context, self.truststore.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertIs(ssl.SSLContext, original_context)

    def test_real_native_verifier_rejects_an_untrusted_local_certificate(self):
        with patch.object(tls, "_uses_windows10_truststore", return_value=True):
            context = tls.urlopen_kwargs()["context"]
        assert_untrusted_local_server_rejected(self, context)


if __name__ == "__main__":
    unittest.main()
