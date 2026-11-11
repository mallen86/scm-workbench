"""Request-local Windows 10 TLS compatibility for the packaged worker.

Use Windows' certificate-chain builder, including retrieval of missing roots,
without shipping a CA list or changing process-global ssl behavior. All other
platforms (including Windows 11 and source development) keep urllib's defaults.
"""

import os
import ssl
import sys
from pathlib import Path


def _is_windows_bundle() -> bool:
    # The source launcher also sets SCM_WORKBENCH_PACKAGED=1. Require the
    # release layout so source/browser and cargo development stay stdlib-only.
    app = Path(__file__).resolve().parent.parent
    return app.name == "app" and (app.parent / "SCM Workbench.exe").is_file()


def _windows_build() -> int | None:
    # Both Windows 10 and 11 report 10.0. Read the installed OS build directly:
    # getwindowsversion() can be manifest-adjusted, and its platform_version
    # describes kernel32.dll rather than being a supported feature detector.
    # CurrentBuildNumber also exists on early Windows 10 versions, without
    # depending on newer CurrentMajorVersionNumber registry values.
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
    except (OSError, ValueError, TypeError):
        # An unknown OS must not opt into a Windows 10-specific change.
        # urllib's existing, verified TLS behavior remains in effect.
        return None


def _uses_windows10_truststore() -> bool:
    if (sys.platform != "win32" or os.environ.get("SCM_WORKBENCH_PACKAGED") != "1"
            or not _is_windows_bundle()):
        return False
    if sys.getwindowsversion().product_type != 1:
        return False
    build = _windows_build()
    return build is not None and 10240 <= build < 22000


def urlopen_kwargs() -> dict:
    """Only override the HTTPS context for packaged Windows 10 requests.

    No retry with another verifier: a native verification failure is final.
    A missing bundled dependency is also an error, not an insecure fallback.
    """
    if not _uses_windows10_truststore():
        return {}
    try:
        import truststore
    except ImportError as exc:
        raise RuntimeError(
            "Windows 10 HTTPS support is missing; reinstall the Windows app package."
        ) from exc
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # urllib only sets this automatically when no explicit context is supplied.
    context.set_alpn_protocols(["http/1.1"])
    return {"context": context}
