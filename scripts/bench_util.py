"""Measurement helpers for the benchmarks in this directory.

Three things the benchmarks need and the standard library does not give
portably:

  * resident memory of *this* process          (Linux /proc, Windows ctypes)
  * disk I/O performed by *this* process      (Linux /proc, Windows ctypes)
  * CPU time, wall time and their ratio        (time.process_time + perf_counter)

Everything degrades to "unavailable" rather than raising: a benchmark that
dies because it cannot read a counter is worse than one that prints a dash.
`psutil` is used when it happens to be installed, but nothing here requires
it — the offline benchmark must run on a fresh clone.

No dependencies outside the standard library.
"""

from __future__ import annotations

import gc
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

# --------------------------------------------------------------------------- #
# process counters
# --------------------------------------------------------------------------- #

try:  # optional, never required
    import psutil  # type: ignore
except Exception:  # pragma: no cover - the normal path is "not installed"
    psutil = None


def _linux_rss() -> int | None:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def _linux_io() -> tuple[int, int] | None:
    try:
        read = write = 0
        with open("/proc/self/io", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("read_bytes:"):
                    read = int(line.split(":")[1])
                elif line.startswith("write_bytes:"):
                    write = int(line.split(":")[1])
        return read, write
    except Exception:
        return None


def _windows_counters() -> tuple[int | None, tuple[int, int] | None]:
    """Working set size and transfer counts via kernel32 / psapi.

    ``GetProcessMemoryInfo`` is exported by psapi.dll (kernel32 only forwards
    it on some Windows builds), so both are tried and the whole thing is
    allowed to give up quietly — a missing counter is a dash, not a crash.
    """
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    psapi = ctypes.WinDLL("psapi.dll")
    k32 = ctypes.WinDLL("kernel32.dll")
    # The current-process pseudo handle is (HANDLE)-1; without restype it comes
    # back as a truncated int and every call fails with "invalid handle" while
    # still returning 0 (so it looks like a counter that simply does not exist).
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    handle = k32.GetCurrentProcess()

    rss = None
    try:
        mem = PROCESS_MEMORY_COUNTERS()
        mem.cb = ctypes.sizeof(mem)
        getter = getattr(psapi, "GetProcessMemoryInfo", None) \
            or getattr(k32, "GetProcessMemoryInfo", None)
        if getter:
            getter.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            getter.restype = wintypes.BOOL
            if getter(handle, ctypes.byref(mem), mem.cb):
                rss = int(mem.WorkingSetSize)
    except (AttributeError, OSError, ValueError):
        pass

    counters = None
    try:
        io = IO_COUNTERS()
        k32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.GetProcessIoCounters.restype = wintypes.BOOL
        if k32.GetProcessIoCounters(handle, ctypes.byref(io)):
            counters = (int(io.ReadTransferCount), int(io.WriteTransferCount))
    except (AttributeError, OSError, ValueError):
        pass
    return rss, counters


def rss_bytes() -> int | None:
    if psutil is not None:
        try:
            return int(psutil.Process().memory_info().rss)
        except Exception:
            pass
    return _linux_rss() if not IS_WINDOWS else _windows_counters()[0]


def io_counters() -> tuple[int, int] | None:
    """(bytes read from disk, bytes written to disk) by this process."""
    if psutil is not None:
        try:
            info = psutil.Process().io_counters()
            return int(info.read_bytes), int(info.write_bytes)
        except Exception:
            pass
    return _linux_io() if not IS_WINDOWS else _windows_counters()[1]


def cpu_seconds() -> float:
    """User + system CPU of this process."""
    t = time.process_time()
    if psutil is not None:
        try:
            c = psutil.Process().cpu_times()
            return float(c.user + c.system)
        except Exception:
            pass
    return t


# --------------------------------------------------------------------------- #
# phase meter
# --------------------------------------------------------------------------- #

class Phase:
    """Measure wall time, CPU, memory growth and disk I/O of one code block.

        with Phase("embed") as ph:
            vectors = embedder.many(texts)
        ph.record          # -> dict, ready for JSON

    ``reset_peaks`` asks the OS to drop the high-water mark first, so the
    peak really belongs to this phase.  GC runs before the baseline snapshot
    for the same reason.
    """

    def __init__(self, name: str):
        self.name = name
        self.record: dict = {"phase": name}

    def __enter__(self) -> "Phase":
        gc.collect()
        self._wall = time.perf_counter()
        self._cpu = cpu_seconds()
        self._rss = rss_bytes()
        self._io = io_counters()
        self._peak_at_start = _peak_rss()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        wall = (time.perf_counter() - self._wall) * 1000.0
        cpu = max(cpu_seconds() - self._cpu, 0.0) * 1000.0
        rss = rss_bytes()
        io = io_counters()
        peak = _peak_rss()
        self.record.update({
            "wall_ms": round(wall, 3),
            "cpu_ms": round(cpu, 3),
            "cpu_pct_of_wall": round(100.0 * cpu / wall, 1) if wall > 0 else None,
            "rss_after_mb": _mb(rss),
            "rss_delta_mb": _mb(rss - self._rss) if (rss and self._rss) else None,
            "rss_peak_mb": _mb(peak),
            "read_bytes": (io[0] - self._io[0]) if (io and self._io) else None,
            "write_bytes": (io[1] - self._io[1]) if (io and self._io) else None,
            "error": f"{exc_type.__name__}: {exc}" if exc_type else None,
        })


def _peak_rss() -> int | None:
    if psutil is not None:
        try:
            return int(psutil.Process().memory_info().rss)
        except Exception:
            pass
    if IS_WINDOWS:
        return _windows_counters()[0]
    try:  # Linux: VmHWM is the high-water mark, which is what "peak" means
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def _mb(value: int | None) -> float | None:
    return round(value / 1024 / 1024, 2) if value is not None else None


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #

def stats(samples_ms: list[float]) -> dict:
    if not samples_ms:
        return {"n": 0}
    ordered = sorted(samples_ms)
    return {
        "n": len(ordered),
        "mean_ms": round(statistics.fmean(ordered), 3),
        "p50_ms": round(_pct(ordered, 50), 3),
        "p95_ms": round(_pct(ordered, 95), 3),
        "max_ms": round(ordered[-1], 3),
        "min_ms": round(ordered[0], 3),
        "qps": round(1000.0 / statistics.fmean(ordered), 1) if ordered else None,
    }


def _pct(ordered: list[float], pct: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def env_fingerprint() -> dict:
    import sys as _sys
    return {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": _sys.version.split()[0],
        "cpu": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "psutil": getattr(psutil, "__version__", None),
        "counters": {
            "rss": rss_bytes() is not None,
            "io": io_counters() is not None,
        },
    }


def write_json(path: str | Path, payload: dict) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def read_json(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def md_table(headers: list[str], rows: list[list], align: list[str] | None = None) -> str:
    align = align or ["---"] * len(headers)
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join(align) + " |"]
    for row in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
    return "\n".join(out)


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
