"""Unit tests run against a fake /proc tree so they're deterministic."""

import os
import textwrap

import pytest

from proscope import procfs
from proscope.fmt import human_bytes, human_uptime


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(content).lstrip())


@pytest.fixture
def fakeproc(tmp_path):
    root = str(tmp_path)
    write(root, "stat", """
        cpu  100 0 100 800 0 0 0 0 0 0
        cpu0 50 0 50 400 0 0 0 0 0 0
        cpu1 50 0 50 400 0 0 0 0 0 0
        intr 12345
    """)
    write(root, "meminfo", """
        MemTotal:        8000000 kB
        MemFree:         1000000 kB
        MemAvailable:    6000000 kB
        Buffers:          100000 kB
        Cached:          2000000 kB
        SwapTotal:       2000000 kB
        SwapFree:        1500000 kB
    """)
    write(root, "loadavg", "0.52 0.41 0.30 3/412 9999\n")
    write(root, "uptime", "93784.12 180000.00\n")
    write(root, "net/dev", """
        Inter-|   Receive                                                |  Transmit
         face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
            lo:  5000      10    0    0    0     0          0         0     5000      10    0    0    0     0       0          0
          eth0: 123456     100    0    0    0     0          0         0    654321     90    0    0    0     0       0          0
    """)
    write(root, "cpuinfo", "processor\t: 0\nmodel name\t: Fake CPU 9000\n")
    # A process whose name contains spaces and parentheses
    write(root, "42/stat",
          "42 (my (weird) proc) S 1 42 42 0 -1 4194304 0 0 0 0 "
          "70 30 0 0 20 0 3 0 100 1000000 256 18446744073709551615\n")
    with open(os.path.join(root, "42/cmdline"), "w") as fh:
        fh.write("/usr/bin/weird\x00--flag\x00")
    return root


def test_cpu_times_and_percent(fakeproc):
    prev = procfs.read_cpu_times(fakeproc)
    assert prev["cpu"] == (800, 1000)
    assert set(prev) == {"cpu", "cpu0", "cpu1"}
    cur = {"cpu": (850, 1100), "cpu0": (400, 550), "cpu1": (450, 550)}
    pct = procfs.cpu_percent(prev, cur)
    assert pct["cpu"] == pytest.approx(50.0)
    assert pct["cpu0"] == pytest.approx(100.0)
    assert pct["cpu1"] == pytest.approx(0.0)


def test_cpu_percent_handles_no_change(fakeproc):
    s = procfs.read_cpu_times(fakeproc)
    assert procfs.cpu_percent(s, s)["cpu"] == 0.0


def test_meminfo(fakeproc):
    m = procfs.read_meminfo(fakeproc)
    assert m.total == 8000000 * 1024
    assert m.used == 2000000 * 1024
    assert m.swap_used == 500000 * 1024


def test_meminfo_without_memavailable(tmp_path):
    write(str(tmp_path), "meminfo", """
        MemTotal: 1000 kB
        MemFree: 100 kB
        Buffers: 50 kB
        Cached: 250 kB
    """)
    assert procfs.read_meminfo(str(tmp_path)).available == 400 * 1024


def test_loadavg_and_uptime(fakeproc):
    assert procfs.read_loadavg(fakeproc) == (0.52, 0.41, 0.30, 3, 412)
    assert procfs.read_uptime(fakeproc) == pytest.approx(93784.12)


def test_net_dev_skips_loopback(fakeproc):
    assert procfs.read_net_dev(fakeproc) == {"eth0": (123456, 654321)}


def test_parse_stat_with_tricky_comm():
    comm, f = procfs.parse_stat("7 (a) b) c) R 1 2 3")
    assert comm == "a) b) c"
    assert f[0] == "R"


def test_read_process(fakeproc):
    p = procfs.read_process(42, fakeproc)
    assert p.name == "my (weird) proc"
    assert p.state == "S" and p.ppid == 1
    assert p.cpu_ticks == 100
    assert p.threads == 3
    assert p.rss == 256 * procfs.PAGE_SIZE
    assert p.cmdline == "/usr/bin/weird --flag"


def test_list_processes_ignores_vanished(fakeproc):
    os.makedirs(os.path.join(fakeproc, "999"))  # dir with no stat file
    pids = [p.pid for p in procfs.list_processes(fakeproc)]
    assert pids == [42]


def test_cpu_model(fakeproc):
    assert procfs.cpu_model(fakeproc) == "Fake CPU 9000"


def test_live_proc_smoke():
    """Sanity check against the real /proc of the machine running the tests."""
    if not os.path.exists("/proc/stat"):
        pytest.skip("not Linux")
    from proscope.monitor import Monitor
    snap = Monitor().sample()
    assert snap.mem.total > 0
    assert snap.cpu_cores
    assert any(p.pid == os.getpid() for p in snap.procs)


@pytest.mark.parametrize("n,expected", [
    (512, "512B"), (2048, "2.0K"), (5 * 1024 ** 2, "5.0M"), (3 * 1024 ** 3, "3.0G"),
])
def test_human_bytes(n, expected):
    assert human_bytes(n) == expected


def test_human_uptime():
    assert human_uptime(3 * 3600 + 5 * 60) == "03:05"
    assert human_uptime(2 * 86400 + 60) == "2d 00:01"
