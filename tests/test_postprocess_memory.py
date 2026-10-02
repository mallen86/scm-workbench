"""Windows system-memory telemetry and adaptive upscaler policy contracts."""
import ctypes
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from scm_workbench import postprocess_memory as memory


GIB = 1024 ** 3


class MemoryPolicyTests(unittest.TestCase):
    def test_memory_info_fields(self):
        info = memory.MemoryInfo(32 * GIB, 24 * GIB, 20 * GIB)
        self.assertEqual(info.total_physical, 32 * GIB)
        self.assertEqual(info.available_physical, 24 * GIB)
        self.assertEqual(info.available_commit, 20 * GIB)

    def test_reserve_is_at_least_four_gib(self):
        for total in (2 * GIB, 8 * GIB, 16 * GIB, 20 * GIB):
            with self.subTest(total=total):
                self.assertEqual(memory.system_reserve(memory.MemoryInfo(total, 0, 0)), 4 * GIB)

    def test_reserve_is_twenty_percent_rounded_up_to_byte(self):
        for total in (20 * GIB + 1, 32 * GIB, 64 * GIB, 64 * GIB + 3):
            with self.subTest(total=total):
                expected = (total + 4) // 5
                self.assertEqual(memory.system_reserve(memory.MemoryInfo(total, 0, 0)), expected)
                self.assertGreaterEqual(expected * 5, total)
                self.assertLess((expected - 1) * 5, total)

    def test_budget_has_sixteen_gib_ceiling(self):
        self.assertEqual(memory.upscaler_budget(memory.MemoryInfo(128 * GIB, 100 * GIB, 100 * GIB)), 16 * GIB)

    def test_budget_is_at_most_half_installed_ram(self):
        total = 16 * GIB + 1
        self.assertEqual(memory.upscaler_budget(memory.MemoryInfo(total, total, 100 * GIB)), total // 2)

    def test_budget_leaves_physical_reserve(self):
        info = memory.MemoryInfo(32 * GIB, 10 * GIB + 7, 100 * GIB)
        self.assertEqual(memory.upscaler_budget(info), info.available_physical - memory.system_reserve(info))

    def test_budget_leaves_system_commit_reserve(self):
        info = memory.MemoryInfo(32 * GIB, 32 * GIB, 9 * GIB + 11)
        self.assertEqual(memory.upscaler_budget(info), info.available_commit - memory.system_reserve(info))

    def test_exactly_one_gib_is_viable(self):
        for field in ("available_physical", "available_commit"):
            with self.subTest(field=field):
                info = memory.MemoryInfo(16 * GIB, 16 * GIB, 20 * GIB)._replace(**{field: 5 * GIB})
                self.assertEqual(memory.upscaler_budget(info), GIB)

    def test_subminimum_budget_is_rejected_not_rounded_up(self):
        for field in ("available_physical", "available_commit"):
            for available in (5 * GIB - 1, 4 * GIB, 0):
                with self.subTest(field=field, available=available):
                    info = memory.MemoryInfo(16 * GIB, 16 * GIB, 20 * GIB)._replace(**{field: available})
                    with self.assertRaises(memory.MemoryBudgetError) as caught:
                        memory.upscaler_budget(info)
                    self.assertIn("1 GiB", str(caught.exception))
                    self.assertIn("Close other applications", str(caught.exception))
                    self.assertIn("smaller original images", str(caught.exception))

    def test_half_physical_below_minimum_is_rejected(self):
        with self.assertRaises(memory.MemoryBudgetError):
            memory.upscaler_budget(memory.MemoryInfo(2 * GIB - 1, 20 * GIB, 20 * GIB))

    def test_pressure_above_both_boundaries_is_clear(self):
        reserve = 4 * GIB
        self.assertIsNone(memory.pressure_message(memory.MemoryInfo(16 * GIB, reserve + 1, reserve + 1), reserve))

    def test_pressure_at_or_below_either_boundary_is_actionable(self):
        reserve = 4 * GIB
        for field in ("available_physical", "available_commit"):
            for available in (reserve, reserve - 1, 0):
                with self.subTest(field=field, available=available):
                    info = memory.MemoryInfo(16 * GIB, 12 * GIB, 12 * GIB)._replace(**{field: available})
                    message = memory.pressure_message(info, reserve)
                    self.assertIsInstance(message, str)
                    self.assertIn("Windows", message)
                    self.assertIn("Close other applications", message)
                    self.assertIn("smaller original images", message)
                    self.assertNotIn("Traceback", message)
                    self.assertNotIn("MemoryBudgetError", message)


class WindowsMemoryTelemetryTests(unittest.TestCase):
    def setUp(self):
        memory._windows_apis.cache_clear()
        self.addCleanup(memory._windows_apis.cache_clear)

    def fake_apis(self, *, status_values=None, performance_values=None):
        status_defaults = dict(total_physical=32 * GIB, available_physical=24 * GIB,
                               available_page_file=123)
        performance_defaults = dict(commit_total=2_000_000, commit_limit=8_000_000, page_size=4096)
        status_defaults.update(status_values or {})
        performance_defaults.update(performance_values or {})

        def status_call(pointer):
            status = ctypes.cast(pointer, ctypes.POINTER(memory._MemoryStatus)).contents
            self.assertEqual(status.length, ctypes.sizeof(memory._MemoryStatus))
            for name, value in status_defaults.items():
                setattr(status, name, value)
            return 1

        def performance_call(pointer, size):
            performance = ctypes.cast(pointer, ctypes.POINTER(memory._PerformanceInfo)).contents
            self.assertEqual(size, ctypes.sizeof(memory._PerformanceInfo))
            self.assertEqual(performance.size, size)
            for name, value in performance_defaults.items():
                setattr(performance, name, value)
            return 1

        return (SimpleNamespace(GlobalMemoryStatusEx=Mock(side_effect=status_call)),
                SimpleNamespace(GetPerformanceInfo=Mock(side_effect=performance_call)))

    def test_memory_status_matches_windows_dword_and_ulonglong_layout(self):
        # Two DWORDs followed by seven DWORDLONGs: the Windows SDK size is 64.
        self.assertEqual(ctypes.sizeof(memory._MemoryStatus), 64)
        fields = dict(memory._MemoryStatus._fields_)
        for name in ("length", "load"):
            self.assertIs(fields[name], ctypes.c_uint32)
            self.assertEqual(ctypes.sizeof(fields[name]), 4)
        for name in fields.keys() - {"length", "load"}:
            self.assertIs(fields[name], ctypes.c_uint64)
        self.assertEqual(memory._MemoryStatus.total_physical.offset, 8)
        self.assertEqual(memory._MemoryStatus.available_extended_virtual.offset,
                         ctypes.sizeof(memory._MemoryStatus) - 8)

    def test_performance_info_matches_pointer_width_size_t_layout(self):
        fields = dict(memory._PerformanceInfo._fields_)
        for name in ("size", "handles", "processes", "threads"):
            self.assertIs(fields[name], ctypes.c_uint32)
        for name in fields.keys() - {"size", "handles", "processes", "threads"}:
            self.assertIs(fields[name], ctypes.c_size_t)
            self.assertEqual(ctypes.sizeof(fields[name]), ctypes.sizeof(ctypes.c_void_p))
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(memory._PerformanceInfo.commit_total.offset, pointer_size)
        self.assertEqual(memory._PerformanceInfo.page_size.offset, 10 * pointer_size)
        self.assertEqual(ctypes.sizeof(memory._PerformanceInfo), 104 if pointer_size == 8 else 56)

    def test_dll_loading_declares_signatures_and_caches_only_apis(self):
        kernel, psapi = self.fake_apis()
        with patch.object(memory.ctypes, "WinDLL", create=True, side_effect=(kernel, psapi)) as load:
            self.assertEqual(memory._windows_apis(), (kernel, psapi))
            self.assertEqual(memory._windows_apis(), (kernel, psapi))
        self.assertEqual(load.call_args_list, [call("kernel32", use_last_error=True, winmode=0x00000800),
                                               call("psapi", use_last_error=True, winmode=0x00000800)])
        self.assertEqual(kernel.GlobalMemoryStatusEx.argtypes, [ctypes.POINTER(memory._MemoryStatus)])
        self.assertIs(kernel.GlobalMemoryStatusEx.restype, ctypes.c_int32)
        self.assertEqual(psapi.GetPerformanceInfo.argtypes,
                         [ctypes.POINTER(memory._PerformanceInfo), ctypes.c_uint32])
        self.assertIs(psapi.GetPerformanceInfo.restype, ctypes.c_int32)

    def test_system_commit_uses_performance_pages_not_process_pagefile_allowance(self):
        for page_size in (4096, 65536):
            with self.subTest(page_size=page_size):
                apis = self.fake_apis(performance_values={"page_size": page_size})
                with patch.object(memory, "_windows_apis", return_value=apis):
                    info = memory.read_windows_memory()
                self.assertEqual(info, memory.MemoryInfo(32 * GIB, 24 * GIB, 6_000_000 * page_size))
                self.assertNotEqual(info.available_commit, 123)
                apis[0].GlobalMemoryStatusEx.assert_called_once()
                apis[1].GetPerformanceInfo.assert_called_once()

    def test_telemetry_is_fresh_even_when_dlls_are_cached(self):
        kernel, psapi = self.fake_apis()
        with patch.object(memory.ctypes, "WinDLL", create=True, side_effect=(kernel, psapi)) as load:
            memory.read_windows_memory()
            memory.read_windows_memory()
        self.assertEqual(load.call_count, 2)
        self.assertEqual(kernel.GlobalMemoryStatusEx.call_count, 2)
        self.assertEqual(psapi.GetPerformanceInfo.call_count, 2)

    def test_api_failure_stops_safely(self):
        for failing_api in ("GlobalMemoryStatusEx", "GetPerformanceInfo"):
            with self.subTest(api=failing_api):
                apis = self.fake_apis()
                function = getattr(apis[0] if failing_api == "GlobalMemoryStatusEx" else apis[1], failing_api)
                function.side_effect = None
                function.return_value = 0
                with patch.object(memory, "_windows_apis", return_value=apis):
                    self.assert_safe_telemetry_failure()
                if failing_api == "GlobalMemoryStatusEx":
                    apis[1].GetPerformanceInfo.assert_not_called()

    def test_loader_exception_stops_safely(self):
        with patch.object(memory, "_windows_apis", side_effect=OSError("private DLL failure")):
            self.assert_safe_telemetry_failure()

    def test_invalid_telemetry_stops_safely(self):
        cases = [({"total_physical": 0}, {}),
                 ({"available_physical": 32 * GIB + 1}, {}),
                 ({}, {"page_size": 0}),
                 ({}, {"commit_limit": 0}),
                 ({}, {"commit_total": 8_000_001})]
        for status, performance in cases:
            with self.subTest(status=status, performance=performance):
                apis = self.fake_apis(status_values=status, performance_values=performance)
                with patch.object(memory, "_windows_apis", return_value=apis):
                    self.assert_safe_telemetry_failure()

    def test_exhausted_commit_is_zero_not_invalid_or_wrapped(self):
        apis = self.fake_apis(performance_values={"commit_total": 8_000_000})
        with patch.object(memory, "_windows_apis", return_value=apis):
            info = memory.read_windows_memory()
        self.assertEqual(info.available_commit, 0)
        with self.assertRaises(memory.MemoryBudgetError):
            memory.upscaler_budget(info)

    def assert_safe_telemetry_failure(self):
        with self.assertRaises(memory.MemoryBudgetError) as caught:
            memory.read_windows_memory()
        message = str(caught.exception)
        self.assertIn("cannot", message)
        self.assertIn("safely", message)
        self.assertIn("restart Workbench", message)
        self.assertNotIn("private DLL failure", message)
        self.assertIsNotNone(caught.exception.__cause__)

    @unittest.skipUnless(sys.platform == "win32", "requires real Windows telemetry")
    def test_real_windows_telemetry_smoke(self):
        info = memory.read_windows_memory()
        self.assertGreater(info.total_physical, 0)
        self.assertGreater(info.available_physical, 0)
        self.assertLessEqual(info.available_physical, info.total_physical)
        self.assertGreater(info.available_commit, 0)
        self.assertLess(info.total_physical, 1024 * 1024 * GIB)
        self.assertLess(info.available_commit, 1024 * 1024 * GIB)


if __name__ == "__main__":
    unittest.main()
