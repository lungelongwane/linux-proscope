"""Stateful sampler: turns raw /proc counters into rates and percentages."""

from __future__ import annotations

import time
from typing import Dict, Tuple

from . import procfs
from .procfs import PROC, Snapshot


class Monitor:
    def __init__(self, root: str = PROC) -> None:
        self.root = root
        self._cpu = procfs.read_cpu_times(root)
        self._net = procfs.read_net_dev(root)
        self._ticks: Dict[int, int] = {}
        self._t = time.monotonic()
        # prime per-process tick counters so the first frame has CPU% values
        for p in procfs.list_processes(root):
            self._ticks[p.pid] = p.cpu_ticks

    def sample(self) -> Snapshot:
        now = time.monotonic()
        dt = max(now - self._t, 1e-6)

        cpu_now = procfs.read_cpu_times(self.root)
        pct = procfs.cpu_percent(self._cpu, cpu_now)
        total_jiffies = cpu_now["cpu"][1] - self._cpu["cpu"][1]
        ncores = sum(1 for k in cpu_now if k != "cpu") or 1
        self._cpu = cpu_now

        net_now = procfs.read_net_dev(self.root)
        rates: Dict[str, Tuple[float, float]] = {}
        for iface, (rx, tx) in net_now.items():
            prx, ptx = self._net.get(iface, (rx, tx))
            rates[iface] = (max(0, rx - prx) / dt, max(0, tx - ptx) / dt)
        self._net = net_now

        mem = procfs.read_meminfo(self.root)
        procs = procfs.list_processes(self.root)
        new_ticks: Dict[int, int] = {}
        # total_jiffies covers all cores, so scale to "% of one core" like top
        per_core = total_jiffies / ncores if total_jiffies > 0 else 0
        for p in procs:
            prev = self._ticks.get(p.pid, p.cpu_ticks)
            delta = p.cpu_ticks - prev
            p.cpu = 100.0 * delta / per_core if per_core else 0.0
            p.mem = 100.0 * p.rss / mem.total if mem.total else 0.0
            new_ticks[p.pid] = p.cpu_ticks
        self._ticks = new_ticks
        self._t = now

        cores = [pct.get(f"cpu{i}", 0.0) for i in range(ncores)]
        return Snapshot(
            cpu_total=pct.get("cpu", 0.0),
            cpu_cores=cores,
            mem=mem,
            load=procfs.read_loadavg(self.root),
            uptime=procfs.read_uptime(self.root),
            net=rates,
            disks=procfs.read_disks(self.root),
            procs=procs,
        )
