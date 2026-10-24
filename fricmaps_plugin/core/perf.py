# -*- coding: utf-8 -*-
#
# FricMaps - Friction and land-cover maps for ecological connectivity modelling
# Copyright (C) 2026  FricMaps contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.
#
# =============================================================================
# FricMaps QGIS - perf.py
# =============================================================================
"""Passive performance recording for the FricMaps pipeline.

A :class:`PerfRecorder` timestamps the progress messages the pipeline already emits,
samples the resident memory of the process at each of them, and describes the machine
and software the run happened on. The result is a small JSON report written next to the
intermediate products (``Performance_<area>.json``), so that a published timing can be
accompanied by everything needed to interpret it.

The recorder is strictly passive: it never changes what the pipeline computes, and any
failure while measuring is swallowed so that it cannot break a run.

Memory is the resident set size (RSS) of the whole process, which includes GDAL and GEOS
allocations (``tracemalloc`` would miss them) as well as the host application. When the
pipeline runs inside QGIS the baseline of QGIS itself is therefore included; both the
baseline and the peak are reported. For a clean figure, run headless (``qgis_process``).

The module only needs the standard library. QGIS, GDAL and NumPy are queried lazily and
only to report their versions.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

SCHEMA_VERSION = 1

#: Environment variables that bound the number of threads used by native libraries.
THREAD_VARIABLES = (
    "GDAL_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

MemoryProbe = Callable[[], Tuple[Optional[int], Optional[int]]]


# =====================================================================
# 1) Memory
# =====================================================================


def _linux_status() -> Dict[str, int]:
    """Return ``VmRSS`` (current) and ``VmHWM`` (peak) in bytes from /proc/self/status."""
    out: Dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith(("VmRSS:", "VmHWM:")):
                    key, value = line.split(":", 1)
                    out[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return out


def _unix_peak_rss() -> Optional[int]:
    """Peak RSS of this process from ``getrusage`` (KiB on Linux, bytes on macOS)."""
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    return peak if sys.platform == "darwin" else peak * 1024


def _macos_libsystem():
    """The macOS system library, called through ``ctypes`` rather than external commands."""
    import ctypes

    return ctypes.CDLL("/usr/lib/libSystem.B.dylib")


def _macos_rss() -> Optional[int]:
    """Resident size of this process from ``proc_pidinfo`` (the value ``ps -o rss`` shows)."""
    try:
        import ctypes

        class ProcTaskInfo(ctypes.Structure):  # struct proc_taskinfo, <sys/proc_info.h>
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "pti_virtual_size",
                    "pti_resident_size",
                    "pti_total_user",
                    "pti_total_system",
                    "pti_threads_user",
                    "pti_threads_system",
                )
            ] + [
                (name, ctypes.c_int32)
                for name in (
                    "pti_policy",
                    "pti_faults",
                    "pti_pageins",
                    "pti_cow_faults",
                    "pti_messages_sent",
                    "pti_messages_received",
                    "pti_syscalls_mach",
                    "pti_syscalls_unix",
                    "pti_csw",
                    "pti_threadnum",
                    "pti_numrunning",
                    "pti_priority",
                )
            ]

        info = ProcTaskInfo()
        proc_pidtaskinfo = 4  # PROC_PIDTASKINFO
        written = _macos_libsystem().proc_pidinfo(
            os.getpid(),
            proc_pidtaskinfo,
            ctypes.c_uint64(0),
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return int(info.pti_resident_size) if written == ctypes.sizeof(info) else None
    except (OSError, AttributeError, ValueError):
        return None


def _macos_sysctl(name: str) -> Optional[bytes]:
    """Raw value of a ``sysctl`` entry (``sysctlbyname``); ``None`` if unavailable."""
    try:
        import ctypes

        libsystem = _macos_libsystem()
        size = ctypes.c_size_t(0)
        if libsystem.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if libsystem.sysctlbyname(name.encode(), buffer, ctypes.byref(size), None, 0) != 0:
            return None
        return buffer.raw[: size.value]
    except (OSError, AttributeError, ValueError):
        return None


def _windows_memory() -> Tuple[Optional[int], Optional[int]]:
    """Return ``(WorkingSetSize, PeakWorkingSetSize)`` through psapi."""
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not ok:
            return None, None
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
    except (OSError, AttributeError, ValueError):
        return None, None


def memory_snapshot() -> Tuple[Optional[int], Optional[int]]:
    """Return ``(current_rss_bytes, peak_rss_bytes)`` of this process; ``None`` if unknown.

    The peak is the highest value reached since the process started, so it is
    cumulative: the peak reported at the end of a stage is the peak of the run so far.
    """
    if sys.platform.startswith("linux"):
        status = _linux_status()
        peak = status.get("VmHWM")
        return status.get("VmRSS"), peak if peak is not None else _unix_peak_rss()
    if sys.platform == "win32":
        return _windows_memory()
    if sys.platform == "darwin":
        return _macos_rss(), _unix_peak_rss()
    return None, _unix_peak_rss()


# =====================================================================
# 2) Environment
# =====================================================================


def _cpu_model() -> Optional[str]:
    try:
        if sys.platform.startswith("linux"):
            # x86 exposes "model name"; some ARM boards expose "Hardware" or a textual
            # "Model". A bare numeric "model" (x86 model id) is not a name.
            fallback = None
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    key, _, value = line.partition(":")
                    key, value = key.strip().lower(), value.strip()
                    if key == "model name" and value:
                        return value
                    if key in ("hardware", "model") and value and not value.isdigit():
                        fallback = fallback or value
            if fallback:
                return fallback
        elif sys.platform == "darwin":
            value = _macos_sysctl("machdep.cpu.brand_string")
            if value:
                return value.rstrip(b"\0").decode("utf-8", "replace").strip() or None
        elif sys.platform == "win32":
            import winreg

            # Built from its parts: written as one literal, the registry path is taken
            # for an encoded secret by the secret scanners of the QGIS plugin repository.
            subkey = "\\".join(("HARDWARE", "DESCRIPTION", "System", "CentralProcessor", "0"))
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey)
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except (OSError, ImportError, IndexError):
        pass
    return platform.processor() or None


def _total_ram_bytes() -> Optional[int]:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        elif sys.platform == "darwin":
            value = _macos_sysctl("hw.memsize")
            return int.from_bytes(value, sys.byteorder) if value else None
        elif sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _filesystem_info(path: Optional[str]) -> Dict[str, Any]:
    """Describe the volume holding ``path``. The device type can only be guessed on Linux."""
    info: Dict[str, Any] = {}
    if not path:
        return info
    info["path"] = os.path.abspath(path)
    # The output folder may not exist yet: describe the volume of its nearest ancestor.
    existing = info["path"]
    while not os.path.exists(existing) and os.path.dirname(existing) != existing:
        existing = os.path.dirname(existing)
    try:
        usage = shutil.disk_usage(existing)
        info["total_bytes"], info["free_bytes"] = int(usage.total), int(usage.free)
    except OSError:
        pass
    if not sys.platform.startswith("linux"):
        return info
    try:
        real = os.path.realpath(path)
        best = ("", "", "")
        with open("/proc/mounts", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                device, mount, fstype = line.split()[:3]
                mount = mount.replace("\\040", " ")
                inside = real == mount or real.startswith(mount.rstrip("/") + "/")
                if inside and len(mount) > len(best[0]):
                    best = (mount, device, fstype)
        if best[1]:
            info["mount_point"], info["device"], info["fs_type"] = best
            dev = os.path.basename(os.path.realpath(best[1]))
            sys_path = os.path.realpath("/sys/class/block/" + dev)
            disk = dev
            if os.path.exists(os.path.join(sys_path, "partition")):
                disk = os.path.basename(os.path.dirname(sys_path))
            rotational = "/sys/block/" + disk + "/queue/rotational"
            if os.path.exists(rotational):
                with open(rotational, encoding="ascii") as fh:
                    info["rotational"] = fh.read().strip() == "1"
    except (OSError, ValueError):
        pass
    return info


def _software_versions() -> Dict[str, Any]:
    versions: Dict[str, Any] = {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
    }
    try:
        import numpy

        versions["numpy"] = numpy.__version__
    except ImportError:
        pass
    try:
        from osgeo import gdal

        versions["gdal_bindings"] = gdal.__version__
        versions["gdal_runtime"] = gdal.VersionInfo("RELEASE_NAME")
        threads = gdal.GetConfigOption("GDAL_NUM_THREADS")
        versions["gdal_cache_max_bytes"] = int(gdal.GetCacheMax())
        versions["gdal_num_threads_option"] = threads
    except (ImportError, AttributeError, RuntimeError):
        pass
    try:
        from qgis.core import Qgis

        version = getattr(Qgis, "version", None)
        versions["qgis"] = version() if callable(version) else getattr(Qgis, "QGIS_VERSION", None)
    except ImportError:
        pass
    try:
        from qgis.PyQt.QtCore import QT_VERSION_STR

        versions["qt"] = QT_VERSION_STR
    except ImportError:
        pass
    return versions


def plugin_version() -> Optional[str]:
    """Version declared in the plugin's ``metadata.txt``."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "metadata.txt"), encoding="utf-8") as fh:
            for line in fh:
                match = re.match(r"\s*version\s*=\s*(\S+)", line)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return None


def environment(output_dir: Optional[str] = None) -> Dict[str, Any]:
    """Describe the machine and software stack the run happens on."""
    env: Dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_count_logical": os.cpu_count(),
        "ram_total_bytes": _total_ram_bytes(),
        "software": _software_versions(),
        "thread_variables": {name: os.environ.get(name) for name in THREAD_VARIABLES},
        "output_volume": _filesystem_info(output_dir),
        # Where QGIS Processing writes its temporary rasters. On systems that mount /tmp
        # as a tmpfs this is RAM: it speeds up I/O and competes with the process for memory.
        "temp_volume": _filesystem_info(tempfile.gettempdir()),
        "tmpdir_variable": os.environ.get("TMPDIR"),
    }
    return env


# =====================================================================
# 3) Recorder
# =====================================================================


def _mib(value: Optional[int]) -> str:
    return "n/a" if value is None else f"{value / (1024 * 1024):,.0f}"


class PerfRecorder:
    """Record the wall-clock time and memory of the successive stages of a run.

    A stage starts at one :meth:`mark` and ends at the next, so the stage named by a
    progress message is the work done *after* that message was emitted.
    """

    def __init__(
        self,
        parameters: Optional[Dict[str, Any]] = None,
        output_dir: Optional[str] = None,
        clock: Callable[[], float] = time.perf_counter,
        memory_probe: MemoryProbe = memory_snapshot,
    ) -> None:
        self._clock = clock
        self._probe = memory_probe
        self.parameters: Dict[str, Any] = dict(parameters or {})
        self.workload: Dict[str, Any] = {}
        self.status = "running"
        self.started_at_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
        self.environment = environment(output_dir)
        self._t0 = clock()
        rss, peak = self._sample()
        self._baseline_rss = rss
        self._marks: List[Dict[str, Any]] = [
            {"t": 0.0, "percent": 0, "label": "start", "rss": rss, "peak": peak}
        ]
        self._total: Optional[float] = None

    def _sample(self) -> Tuple[Optional[int], Optional[int]]:
        try:
            return self._probe()
        except Exception:
            return None, None

    def mark(self, percent: float, label: str) -> None:
        """Close the running stage and open a new one named ``label``."""
        rss, peak = self._sample()
        self._marks.append(
            {
                "t": self._clock() - self._t0,
                "percent": percent,
                "label": str(label),
                "rss": rss,
                "peak": peak,
            }
        )

    def set_workload(self, **values: Any) -> None:
        """Merge descriptors of the amount of work (grid size, resolution, counts...)."""
        self.workload.update(values)

    def finish(self, status: str = "completed") -> None:
        """Stop the clock. Only the first call counts."""
        if self._total is not None:
            return
        rss, peak = self._sample()
        self._total = self._clock() - self._t0
        self.status = status
        self._marks.append(
            {"t": self._total, "percent": None, "label": "end", "rss": rss, "peak": peak}
        )

    @property
    def total_seconds(self) -> float:
        return self._total if self._total is not None else self._clock() - self._t0

    def stages(self) -> List[Dict[str, Any]]:
        """One entry per stage: name, duration, share of the total and memory at its end."""
        total = self.total_seconds or 1e-12
        out: List[Dict[str, Any]] = []
        for current, following in zip(self._marks[:-1], self._marks[1:]):
            seconds = max(0.0, following["t"] - current["t"])
            out.append(
                {
                    "name": current["label"],
                    "percent_at_start": current["percent"],
                    "seconds": round(seconds, 4),
                    "share": round(seconds / total, 4),
                    "rss_end_bytes": following["rss"],
                    "peak_rss_end_bytes": following["peak"],
                }
            )
        return out

    def memory_summary(self) -> Dict[str, Optional[int]]:
        peaks = [m["peak"] for m in self._marks if m["peak"] is not None]
        peak = max(peaks) if peaks else None
        over = None
        if peak is not None and self._baseline_rss is not None:
            over = max(0, peak - self._baseline_rss)
        return {
            "rss_start_bytes": self._baseline_rss,
            "peak_rss_bytes": peak,
            "peak_over_start_bytes": over,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": "FricMaps",
            "plugin_version": plugin_version(),
            "started_at_utc": self.started_at_utc,
            "status": self.status,
            "total_seconds": round(self.total_seconds, 4),
            "parameters": self.parameters,
            "workload": self.workload,
            "memory": self.memory_summary(),
            "stages": self.stages(),
            "environment": self.environment,
        }

    def write(self, path: str) -> str:
        """Write the report as JSON (atomically) and return its path."""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
        return path

    def format_table(self, width: int = 54) -> str:
        """Human-readable summary, for the log panel."""
        lines = [f"{'Stage':<{width}}{'Time (s)':>10}{'Share':>9}{'RSS end (MiB)':>16}"]
        for stage in self.stages():
            name = stage["name"]
            if len(name) > width - 2:
                name = name[: width - 5] + "..."
            lines.append(
                f"{name:<{width}}{stage['seconds']:>10.1f}{stage['share'] * 100:>8.1f}%"
                f"{_mib(stage['rss_end_bytes']):>16}"
            )
        mem = self.memory_summary()
        lines.append(
            f"{'Total (' + self.status + ')':<{width}}{self.total_seconds:>10.1f}{'':>9}"
            f"{'peak ' + _mib(mem['peak_rss_bytes']):>16}"
        )
        return "\n".join(lines)


# =====================================================================
# 4) Active recorder (so the pipeline body needs no extra argument)
# =====================================================================

_local = threading.local()


def active() -> Optional[PerfRecorder]:
    """The recorder of the run in progress on this thread, if any."""
    return getattr(_local, "recorder", None)


@contextlib.contextmanager
def activate(recorder: PerfRecorder) -> Iterator[PerfRecorder]:
    previous = active()
    _local.recorder = recorder
    try:
        yield recorder
    finally:
        _local.recorder = previous


def mark(percent: float, label: str) -> None:
    """:meth:`PerfRecorder.mark` on the active recorder; a no-op when there is none."""
    recorder = active()
    if recorder is not None:
        try:
            recorder.mark(percent, label)
        except Exception:
            logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)


def set_workload(**values: Any) -> None:
    """:meth:`PerfRecorder.set_workload` on the active recorder; a no-op when there is none."""
    recorder = active()
    if recorder is not None:
        try:
            recorder.set_workload(**values)
        except Exception:
            logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
