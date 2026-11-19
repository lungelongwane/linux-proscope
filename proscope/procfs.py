"""Low-level readers for the Linux /proc and /sys pseudo-filesystems.

Every function takes an optional ``root`` argument (default ``/proc``) so the
parsers can be unit-tested against a fake directory tree.
"""

from __future__ import annotations

import os
import pwd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

PROC = os.environ.get("PROSCOPE_PROC", "/proc")

# Filesystem types worth showing in the disk panel.
REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "nfs", "nfs4", "cifs", "overlay",
}

try:
    PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
    CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError, AttributeError):  # pragma: no cover
    PAGE_SIZE, CLK_TCK = 4096, 100


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# CPU
# --------------------------------------------------------------------------- #
def read_cpu_times(root: str = PROC) -> Dict[str, Tuple[int, int]]:
    """Return ``{name: (idle, total)}`` jiffies for ``cpu`` and each ``cpuN``."""
    out: Dict[str, Tuple[int, int]] = {}
    for line in _read(os.path.join(root, "stat")).splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        vals = [int(v) for v in parts[1:]]
        # user nice system idle iowait irq softirq steal (guest is already in user)
        vals = (vals + [0] * 8)[:8]
        idle = vals[3] + vals[4]
        out[parts[0]] = (idle, sum(vals))
    return out


def cpu_percent(prev: Dict[str, Tuple[int, int]],
                cur: Dict[str, Tuple[int, int]]) -> Dict[str, float]:
    """Utilisation % per CPU between two :func:`read_cpu_times` samples."""
    res: Dict[str, float] = {}
    for name, (idle, total) in cur.items():
        if name not in prev:
            continue
        p_idle, p_total = prev[name]
        dt = total - p_total
        di = idle - p_idle
        res[name] = 0.0 if dt <= 0 else max(0.0, min(100.0, 100.0 * (dt - di) / dt))
    return res


def cpu_model(root: str = PROC) -> str:
    try:
        for line in _read(os.path.join(root, "cpuinfo")).splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown CPU"


# --------------------------------------------------------------------------- #
# Memory / load / uptime
# --------------------------------------------------------------------------- #
@dataclass
class MemInfo:
    total: int
    available: int
    swap_total: int
    swap_free: int
    buffers: int = 0
    cached: int = 0

    @property
    def used(self) -> int:
        return self.total - self.available

    @property
    def swap_used(self) -> int:
        return self.swap_total - self.swap_free


def read_meminfo(root: str = PROC) -> MemInfo:
    """Parse /proc/meminfo; values are returned in **bytes**."""
    kv: Dict[str, int] = {}
    for line in _read(os.path.join(root, "meminfo")).splitlines():
        key, _, rest = line.partition(":")
        num = rest.strip().split()
        if num:
            kv[key] = int(num[0]) * 1024
    total = kv.get("MemTotal", 0)
    avail = kv.get("MemAvailable")
    if avail is None:  # kernels < 3.14
        avail = kv.get("MemFree", 0) + kv.get("Buffers", 0) + kv.get("Cached", 0)
    return MemInfo(total, avail, kv.get("SwapTotal", 0), kv.get("SwapFree", 0),
                   kv.get("Buffers", 0), kv.get("Cached", 0))


def read_loadavg(root: str = PROC) -> Tuple[float, float, float, int, int]:
    """Return (1m, 5m, 15m, running_tasks, total_tasks)."""
    p = _read(os.path.join(root, "loadavg")).split()
    running, total = p[3].split("/")
    return float(p[0]), float(p[1]), float(p[2]), int(running), int(total)


def read_uptime(root: str = PROC) -> float:
    return float(_read(os.path.join(root, "uptime")).split()[0])


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
def read_net_dev(root: str = PROC) -> Dict[str, Tuple[int, int]]:
    """Return ``{iface: (rx_bytes, tx_bytes)}`` excluding loopback."""
    res: Dict[str, Tuple[int, int]] = {}
    for line in _read(os.path.join(root, "net", "dev")).splitlines()[2:]:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        f = data.split()
        res[name] = (int(f[0]), int(f[8]))
    return res


# --------------------------------------------------------------------------- #
# Disks
# --------------------------------------------------------------------------- #
@dataclass
class DiskUsage:
    mount: str
    device: str
    fstype: str
    total: int
    used: int
    free: int

    @property
    def percent(self) -> float:
        return 0.0 if self.total == 0 else 100.0 * self.used / self.total


def read_disks(root: str = PROC) -> List[DiskUsage]:
    disks: List[DiskUsage] = []
    seen = set()
    try:
        mounts = _read(os.path.join(root, "mounts")).splitlines()
    except OSError:
        return disks
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype not in REAL_FS or dev in seen:
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total == 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        seen.add(dev)
        disks.append(DiskUsage(mnt, dev, fstype, total, used, free))
    return disks


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #
_user_cache: Dict[int, str] = {}


def _username(uid: int) -> str:
    if uid not in _user_cache:
        try:
            _user_cache[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _user_cache[uid] = str(uid)
    return _user_cache[uid]


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    state: str
    user: str
    threads: int
    rss: int            # bytes
    cpu_ticks: int      # utime + stime
    cmdline: str
    cpu: float = 0.0    # filled in by the Monitor
    mem: float = 0.0


def parse_stat(text: str) -> Tuple[str, List[str]]:
    """Split /proc/<pid>/stat into (comm, fields-after-comm).

    ``comm`` may contain spaces and parentheses, so we split on the *last* ')'.
    """
    lpar = text.index("(")
    rpar = text.rindex(")")
    return text[lpar + 1:rpar], text[rpar + 2:].split()


def read_process(pid: int, root: str = PROC) -> Optional[Proc]:
    base = os.path.join(root, str(pid))
    try:
        comm, f = parse_stat(_read(os.path.join(base, "stat")))
        uid = os.stat(base).st_uid
        try:
            raw = _read(os.path.join(base, "cmdline"))
            cmd = raw.replace("\x00", " ").strip()
        except OSError:
            cmd = ""
    except (OSError, ValueError, IndexError):
        return None  # process vanished or is unreadable
    return Proc(
        pid=pid,
        ppid=int(f[1]),
        name=comm,
        state=f[0],
        user=_username(uid),
        threads=int(f[17]),
        rss=int(f[21]) * PAGE_SIZE,
        cpu_ticks=int(f[11]) + int(f[12]),
        cmdline=cmd or f"[{comm}]",
    )


def list_processes(root: str = PROC) -> List[Proc]:
    procs = []
    for entry in os.listdir(root):
        if entry.isdigit():
            p = read_process(int(entry), root)
            if p is not None:
                procs.append(p)
    return procs


def hostname(root: str = PROC) -> str:
    try:
        return _read(os.path.join(root, "sys", "kernel", "hostname")).strip()
    except OSError:
        return os.uname().nodename


@dataclass
class Snapshot:
    """Everything the UI needs for one frame."""
    cpu_total: float = 0.0
    cpu_cores: List[float] = field(default_factory=list)
    mem: Optional[MemInfo] = None
    load: Tuple[float, float, float, int, int] = (0.0, 0.0, 0.0, 0, 0)
    uptime: float = 0.0
    net: Dict[str, Tuple[float, float]] = field(default_factory=dict)  # B/s
    disks: List[DiskUsage] = field(default_factory=list)
    procs: List[Proc] = field(default_factory=list)
