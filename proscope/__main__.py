"""Command-line entry point: ``proscope`` or ``python -m proscope``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict

from . import __version__, procfs
from .fmt import human_bytes, human_uptime
from .monitor import Monitor


def snapshot(sort: str, limit: int, as_json: bool) -> int:
    mon = Monitor()
    time.sleep(0.5)
    s = mon.sample()
    key = {"cpu": lambda p: -p.cpu, "mem": lambda p: -p.rss,
           "pid": lambda p: p.pid, "name": lambda p: p.name.lower()}[sort]
    s.procs.sort(key=key)
    s.procs = s.procs[:limit]
    if as_json:
        data = asdict(s)
        data["host"] = procfs.hostname()
        print(json.dumps(data, indent=2, default=str))
        return 0

    m = s.mem
    l1, l5, l15, run, tot = s.load
    print(f"host {procfs.hostname()}  up {human_uptime(s.uptime)}  "
          f"load {l1:.2f} {l5:.2f} {l15:.2f}  tasks {run}/{tot}")
    print(f"cpu  {s.cpu_total:5.1f}%  cores: " +
          " ".join(f"{c:.0f}%" for c in s.cpu_cores))
    if m:
        print(f"mem  {human_bytes(m.used)}/{human_bytes(m.total)}  "
              f"swap {human_bytes(m.swap_used)}/{human_bytes(m.swap_total)}")
    for name, (rx, tx) in s.net.items():
        print(f"net  {name}: rx {human_bytes(rx, '/s')}  tx {human_bytes(tx, '/s')}")
    for d in s.disks:
        print(f"disk {d.mount} ({d.fstype}) {d.percent:.0f}% used, {human_bytes(d.free)} free")
    print(f"\n{'PID':>7} {'USER':<10} {'CPU%':>6} {'MEM%':>6} {'RES':>7}  COMMAND")
    for p in s.procs:
        print(f"{p.pid:>7} {p.user[:10]:<10} {p.cpu:>6.1f} {p.mem:>6.1f} "
              f"{human_bytes(p.rss):>7}  {p.cmdline[:80]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="proscope",
        description="A lightweight terminal system monitor that reads /proc directly.")
    ap.add_argument("-d", "--delay", type=float, default=1.0,
                    help="refresh interval in seconds (default: 1.0)")
    ap.add_argument("-s", "--sort", choices=["cpu", "mem", "pid", "name"], default="cpu",
                    help="initial sort column (default: cpu)")
    ap.add_argument("--once", action="store_true",
                    help="print a single text snapshot and exit (no TUI)")
    ap.add_argument("--json", action="store_true", help="print a JSON snapshot and exit")
    ap.add_argument("-n", "--limit", type=int, default=15,
                    help="processes to show with --once/--json (default: 15)")
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    if not sys.platform.startswith("linux"):
        print("proscope reads /proc and only runs on Linux.", file=sys.stderr)
        return 1
    if args.once or args.json or not sys.stdout.isatty():
        return snapshot(args.sort, args.limit, args.json)

    from .ui import run
    try:
        run(max(0.2, args.delay), args.sort)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
