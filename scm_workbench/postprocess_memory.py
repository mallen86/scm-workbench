"""Windows memory headroom policy for the app-owned image upscalers.

Job Object limits bound committed memory, not installed RAM. Check physical
availability and system commit headroom independently, without treating disk
space or the calling process's page-file allowance as available system memory.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
from typing import NamedTuple

GIB = 1024 ** 3
MAX_UPSCALER_MEMORY = 16 * GIB
MIN_UPSCALER_MEMORY = GIB


class MemoryBudgetError(Exception):
    """An upscaler cannot run while retaining the required system headroom."""


class MemoryInfo(NamedTuple):
    total_physical: int
    available_physical: int
    available_commit: int


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
        (name, ctypes.c_uint64) for name in (
            "total_physical", "available_physical", "total_page_file",
            "available_page_file", "total_virtual", "available_virtual",
            "available_extended_virtual",
        )
    ]


class _PerformanceInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32)] + [
        (name, ctypes.c_size_t) for name in (
            "commit_total", "commit_limit", "commit_peak", "physical_total",
            "physical_available", "system_cache", "kernel_total", "kernel_paged",
            "kernel_nonpaged", "page_size",
        )
    ] + [(name, ctypes.c_uint32) for name in ("handles", "processes", "threads")]


@lru_cache(maxsize=1)
def _windows_apis():
    # These are system APIs, never DLLs from a processor's dependency tree.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x00000800)
    psapi = ctypes.WinDLL("psapi", use_last_error=True, winmode=0x00000800)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = ctypes.c_int32
    psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(_PerformanceInfo), ctypes.c_uint32]
    psapi.GetPerformanceInfo.restype = ctypes.c_int32
    return kernel, psapi


def read_windows_memory() -> MemoryInfo:
    """Return fresh system-wide physical and committed-memory availability."""
    try:
        kernel, psapi = _windows_apis()
        status = _MemoryStatus(length=ctypes.sizeof(_MemoryStatus))
        performance = _PerformanceInfo(size=ctypes.sizeof(_PerformanceInfo))
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        if not psapi.GetPerformanceInfo(ctypes.byref(performance), performance.size):
            raise OSError("GetPerformanceInfo failed")
        if (status.total_physical <= 0 or status.available_physical > status.total_physical
                or performance.page_size <= 0 or performance.commit_limit <= 0
                or performance.commit_total > performance.commit_limit):
            raise OSError("Windows returned invalid memory information")
        return MemoryInfo(
            status.total_physical, status.available_physical,
            (performance.commit_limit - performance.commit_total) * performance.page_size,
        )
    except Exception as exc:
        raise MemoryBudgetError(
            "Windows memory availability could not be checked. Processing cannot "
            "continue safely; restart Workbench and try again."
        ) from exc


def system_reserve(info: MemoryInfo) -> int:
    """Reserve at least 4 GiB or 20% of installed RAM, rounded up."""
    return max(4 * GIB, (info.total_physical + 4) // 5)


def upscaler_budget(info: MemoryInfo) -> int:
    reserve = system_reserve(info)
    budget = min(MAX_UPSCALER_MEMORY, info.total_physical // 2,
                 info.available_physical - reserve, info.available_commit - reserve)
    if budget < MIN_UPSCALER_MEMORY:
        raise MemoryBudgetError(
            "Not enough available memory to start the upscaler safely. Workbench "
            f"needs at least 1 GiB for processing while leaving {reserve / GIB:.1f} GiB "
            "available for Windows and other applications. Close other applications "
            "and retry, or use smaller original images."
        )
    return budget


def pressure_message(info: MemoryInfo, reserve: int) -> str | None:
    if info.available_physical <= reserve:
        return (
            "Processing stopped to protect Windows memory: only "
            f"{info.available_physical / GIB:.1f} GiB of RAM is available, at or below the "
            f"{reserve / GIB:.1f}-GiB system reserve. Close other applications and "
            "retry, or use smaller original images."
        )
    if info.available_commit <= reserve:
        return (
            "Processing stopped because Windows is running low on committed-memory "
            "capacity (RAM and paging file). Close other applications and retry, "
            "or use smaller original images."
        )
    return None
