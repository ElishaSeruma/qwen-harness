"""
tools/sysinfo.py — machine stats for the resource monitor, per platform.

The Control Room's metrics panel needs four things: total RAM, RAM in use,
system CPU, and the per-process footprint of whatever Ollama has loaded. On
macOS those came from `sysctl hw.memsize`, `vm_stat`, `ps -axo` and
`sysctl vm.loadavg`. None of those commands exist on Windows.

The Windows path here is pure `ctypes` against Win32 — no `psutil`
dependency, and no subprocess of any kind. That is not a style preference, it
is the same lesson the macOS side already learned the hard way: this endpoint
is polled every few seconds *while a local model is generating at single-digit
tokens per second*, and the macOS version had to drop `top -l 1` because it
cost 25.8s under load and starved the inference it was monitoring.

The first Windows implementation used `tasklist` for the per-process rows on
the assumption that one small subprocess was cheap. Measured on this machine:

    GlobalMemoryStatusEx      0.0 ms
    GetSystemTimes            0.1 ms
    tasklist /fo csv       1503.7 ms      <- 40% duty cycle on a 3s poll
    ollama /api/ps           90.8 ms

`tasklist` enumerates and formats every process on the machine. The snapshot
below walks the same list through CreateToolhelp32Snapshot and only opens a
handle for the two or three processes whose name actually matches, which is
three orders of magnitude cheaper. Measure before assuming a subprocess is
cheap; on Windows, process creation alone is not.

Two honest notes about the numbers:

  * Windows CPU is a *delta between calls* (GetSystemTimes), so it is a real
    instantaneous utilisation. macOS `ps %cpu` is a per-process lifetime
    average. Both are labelled the same in the UI; the Windows one is the more
    truthful of the two.
  * "Memory used" is total minus available on Windows (what Task Manager's
    "In use" shows), and active+wired+compressed on macOS. Neither is
    comparable to the other across platforms, only against itself over time.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
from typing import Any

from tools.platform_support import IS_WINDOWS, IS_MAC, quiet_kwargs

# Process names that mean "a local model is resident". Ollama shells out to a
# separate runner process per loaded model; it was `llama-server` historically
# and is `ollama.exe runner` on current Windows builds, so both are matched.
INFERENCE_PROCESS_MARKERS = ("llama-server", "ollama_llama_server", "ollama-runner")


# ── Windows: Win32 via ctypes ─────────────────────────────────────────────

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def _filetime_to_int(ft: _FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _windows_memory() -> tuple[float | None, float | None]:
    """(total_mb, used_mb) from GlobalMemoryStatusEx."""
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None, None
        total = status.ullTotalPhys / (1024 * 1024)
        used = (status.ullTotalPhys - status.ullAvailPhys) / (1024 * 1024)
        return round(total, 1), round(used, 1)
    except (AttributeError, OSError):
        return None, None


# GetSystemTimes is cumulative since boot, so a single reading says nothing.
# Keep the previous sample and report the delta — which is why this is module
# state rather than a pure function.
_CPU_LOCK = threading.Lock()
_CPU_PREV: dict[str, int] = {}


def _windows_cpu_percent() -> float | None:
    """System-wide CPU utilisation since the previous call to this function."""
    try:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
    except (AttributeError, OSError):
        return None

    idle_t = _filetime_to_int(idle)
    # Kernel time includes idle time; total busy is (kernel + user) - idle.
    total_t = _filetime_to_int(kernel) + _filetime_to_int(user)

    with _CPU_LOCK:
        prev_idle = _CPU_PREV.get("idle")
        prev_total = _CPU_PREV.get("total")
        _CPU_PREV["idle"], _CPU_PREV["total"] = idle_t, total_t

    if prev_idle is None or prev_total is None:
        return None  # first call establishes the baseline; honest None beats a fake 0
    total_delta = total_t - prev_total
    idle_delta = idle_t - prev_idle
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta)), 1)


# CreateToolhelp32Snapshot / Process32 walk. MAX_PATH-sized name buffer,
# matching the Win32 PROCESSENTRY32W layout exactly — a wrong field order here
# reads garbage rather than failing, so it is spelled out rather than packed.
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# QUERY_LIMITED_INFORMATION rather than QUERY_INFORMATION: it is the minimum
# right that GetProcessMemoryInfo needs, and it succeeds against processes at a
# higher integrity level where the broader right is denied.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _bind_win32():
    """Declare argtypes/restypes for every Win32 call used below.

    Not optional pedantry on 64-bit Windows. ctypes defaults a function's
    restype to `c_int` — 32 bits — so a HANDLE returned by
    CreateToolhelp32Snapshot or OpenProcess is silently TRUNCATED. The
    truncated value then usually still looks truthy, so the failure is not an
    exception: it is a handle that refers to nothing, passed to Process32FirstW
    and CloseHandle. That is a wrong answer and a leaked handle every poll,
    on a function polled every few seconds forever.

    Called once at import; harmless if the DLL is missing (non-Windows imports
    never reach here).
    """
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k.Process32FirstW.restype = ctypes.c_int
    k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k.Process32NextW.restype = ctypes.c_int
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.OpenProcess.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.CloseHandle.restype = ctypes.c_int
    k.K32GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), ctypes.c_ulong]
    k.K32GetProcessMemoryInfo.restype = ctypes.c_int


if IS_WINDOWS:
    try:
        _bind_win32()
    except (AttributeError, OSError):  # pragma: no cover - not reachable off Windows
        pass


def _working_set_mb(pid: int) -> float | None:
    """Resident memory of one process, or None if it cannot be queried."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        # K32GetProcessMemoryInfo lives in kernel32 on Windows 7+, which avoids
        # a separate psapi.dll load.
        if not kernel32.K32GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return round(counters.WorkingSetSize / (1024 * 1024), 1)
    finally:
        kernel32.CloseHandle(handle)


def _windows_inference_processes() -> list[dict[str, Any]]:
    """Resident model-runner processes, with their working set in MB.

    No per-process CPU: Windows has no cheap equivalent of `ps %cpu` (it would
    mean holding a handle open across two polls to difference GetProcessTimes),
    and a fabricated number would be worse than the `None` the panel already
    renders as a dash on macOS when a row has no matching process.
    """
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return []

    rows: list[dict[str, Any]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            name = (entry.szExeFile or "").lower()
            if any(marker in name for marker in INFERENCE_PROCESS_MARKERS):
                pid = int(entry.th32ProcessID)
                # A handle is opened only for the two or three processes that
                # match, never for all ~300 on the machine.
                rows.append({"pid": pid, "cpu_pct": None, "mem_mb": _working_set_mb(pid)})
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    except OSError:
        return rows
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


# ── macOS / Linux: the original commands, unchanged ───────────────────────

def _posix_total_memory_mb() -> float | None:
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            pages, page = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page > 0:
                return round(pages * page / (1024 * 1024), 1)
    except (ValueError, OSError):
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return round(int(out) / (1024 * 1024), 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _mac_memory_used_mb() -> float | None:
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page_size_m = re.search(r"page size of (\d+) bytes", vm)
        page_size = int(page_size_m.group(1)) if page_size_m else 4096
        used_pages = 0
        for label in ("Pages active", "Pages wired down", "Pages occupied by compressor"):
            m = re.search(rf"{label}:\s+(\d+)", vm)
            if m:
                used_pages += int(m.group(1))
        return round(used_pages * page_size / (1024 * 1024), 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _posix_ps_sweep() -> tuple[list[dict[str, Any]], float | None]:
    """One `ps` sweep yields both the runner rows and the machine CPU total."""
    procs: list[dict[str, Any]] = []
    total_cpu = None
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid,pcpu,rss,comm"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        cpu_sum = 0.0
        for line in out.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            pid, pcpu, rss, comm = parts
            try:
                cpu_sum += float(pcpu)
            except ValueError:
                continue
            if any(marker in comm for marker in INFERENCE_PROCESS_MARKERS):
                procs.append({"pid": int(pid), "cpu_pct": float(pcpu),
                              "mem_mb": round(int(rss) / 1024, 1)})
        cores = os.cpu_count() or 1
        total_cpu = round(min(100.0, cpu_sum / cores), 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return procs, total_cpu


def _load_average() -> float | None:
    """1-minute load average, where the OS has such a concept.

    Windows deliberately returns None: it has no load average, and inventing
    one from CPU percent would put a number in the UI that means nothing.
    """
    if IS_WINDOWS:
        return None
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        raw = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True,
                             text=True, timeout=2).stdout
        return round(float(raw.strip().strip("{}").split()[0]), 2)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


# ── public API ────────────────────────────────────────────────────────────

def total_memory_mb() -> float | None:
    """Physical RAM in MB, or None if it could not be determined."""
    if IS_WINDOWS:
        return _windows_memory()[0]
    return _posix_total_memory_mb()


def total_memory_gb(default: float = 8.0) -> float:
    """Physical RAM in GB, falling back to a conservative default."""
    mb = total_memory_mb()
    return mb / 1024 if mb else default


def system_snapshot(total_mb: float | None = None) -> dict[str, Any]:
    """CPU / memory / load, plus the resident model-runner processes.

    Returns the same shape on every platform. A field the OS cannot answer is
    None, never a guess — the panel renders those as a dash, which is honest.
    """
    if IS_WINDOWS:
        mem_total, mem_used = _windows_memory()
        return {
            "system": {
                "cpu_pct": _windows_cpu_percent(),
                "mem_used_mb": mem_used,
                "mem_total_mb": total_mb if total_mb is not None else mem_total,
                "load_avg": None,
                "cpu_count": os.cpu_count(),
            },
            "processes": _windows_inference_processes(),
        }

    procs, total_cpu = _posix_ps_sweep()
    return {
        "system": {
            "cpu_pct": total_cpu,
            "mem_used_mb": _mac_memory_used_mb() if IS_MAC else None,
            "mem_total_mb": total_mb if total_mb is not None else _posix_total_memory_mb(),
            "load_avg": _load_average(),
            "cpu_count": os.cpu_count(),
        },
        "processes": procs,
    }
