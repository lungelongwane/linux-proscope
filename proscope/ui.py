"""curses front-end."""

from __future__ import annotations

import curses
import os
import platform
import signal
import time
from typing import List, Optional

from . import procfs
from .fmt import human_bytes, human_uptime
from .monitor import Monitor
from .procfs import Proc, Snapshot

SORT_KEYS = {
    "cpu": lambda p: p.cpu,
    "mem": lambda p: p.rss,
    "pid": lambda p: p.pid,
    "name": lambda p: p.name.lower(),
}

HELP = " q:quit  ↑↓:select  c/m/p/n:sort  r:reverse  /:filter  k:kill  +/-:speed "


class App:
    def __init__(self, scr, interval: float = 1.0, sort: str = "cpu") -> None:
        self.scr = scr
        self.mon = Monitor()
        self.interval = interval
        self.sort = sort
        self.reverse = sort in ("cpu", "mem")
        self.filter = ""
        self.sel = 0
        self.top = 0
        self.msg = ""
        self.msg_until = 0.0
        self.snap: Snapshot = Snapshot()
        self.rows: List[Proc] = []
        self.host = procfs.hostname()
        self.kernel = platform.release()
        self.model = procfs.cpu_model()
        self._colors()

    # ------------------------------------------------------------------ setup
    def _colors(self) -> None:
        curses.curs_set(0)
        self.scr.nodelay(True)
        self.scr.keypad(True)
        self.c = {k: curses.A_NORMAL for k in
                  ("ok", "warn", "crit", "head", "title", "sel", "dim", "accent")}
        if not curses.has_colors():
            self.c["sel"] = curses.A_REVERSE
            self.c["title"] = curses.A_REVERSE
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        pairs = {
            "ok": (curses.COLOR_GREEN, bg),
            "warn": (curses.COLOR_YELLOW, bg),
            "crit": (curses.COLOR_RED, bg),
            "head": (curses.COLOR_BLACK, curses.COLOR_GREEN),
            "title": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "sel": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "dim": (curses.COLOR_BLUE, bg),
            "accent": (curses.COLOR_CYAN, bg),
        }
        for i, (name, (fg, b)) in enumerate(pairs.items(), start=1):
            curses.init_pair(i, fg, b)
            self.c[name] = curses.color_pair(i)
        self.c["title"] |= curses.A_BOLD
        self.c["head"] |= curses.A_BOLD
        self.c["accent"] |= curses.A_BOLD

    # ---------------------------------------------------------------- helpers
    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def level(self, pct: float) -> int:
        return self.c["crit"] if pct >= 85 else self.c["warn"] if pct >= 60 else self.c["ok"]

    def bar(self, y: int, x: int, width: int, label: str, pct: float, text: str) -> None:
        """Draw ``label[|||||      text]`` like htop."""
        self.put(y, x, label, self.c["accent"])
        x += len(label)
        inner = max(1, width - len(label) - 2)
        fill = int(round(inner * min(pct, 100.0) / 100.0))
        self.put(y, x, "[", curses.A_BOLD)
        self.put(y, x + 1, "|" * fill, self.level(pct))
        self.put(y, x + 1 + fill, " " * (inner - fill))
        t = text[:inner]
        self.put(y, x + 1 + inner - len(t), t, curses.A_DIM if fill < inner - len(t) else 0)
        self.put(y, x + 1 + inner, "]", curses.A_BOLD)

    def flash(self, msg: str) -> None:
        self.msg, self.msg_until = msg, time.monotonic() + 3

    # ----------------------------------------------------------------- drawing
    def draw(self) -> None:
        s = self.snap
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 12 or w < 50:
            self.put(0, 0, "Terminal too small (min 50x12)", self.c["crit"])
            self.scr.refresh()
            return

        # Title bar
        l1, l5, l15, run, tot = s.load
        title = (f" proscope │ {self.host} │ Linux {self.kernel} │ up {human_uptime(s.uptime)}"
                 f" │ load {l1:.2f} {l5:.2f} {l15:.2f} │ tasks {run}/{tot} ")
        self.put(0, 0, title.ljust(w), self.c["title"])
        y = 1

        # CPU bars: two columns when there are many cores
        cores = s.cpu_cores
        cols = 1 if len(cores) <= 4 or w < 100 else 2
        colw = (w - 1) // cols
        rows = (len(cores) + cols - 1) // cols
        lw = len(str(max(len(cores) - 1, 0)))
        for i, pct in enumerate(cores):
            r, c = i % rows, i // rows
            self.bar(y + r, c * colw, colw - 1, f"{i:>{lw}} ", pct, f"{pct:5.1f}%")
        y += rows

        m = s.mem
        if m:
            half = (w - 1) // 2
            mp = 100.0 * m.used / m.total if m.total else 0
            sp = 100.0 * m.swap_used / m.swap_total if m.swap_total else 0
            self.bar(y, 0, half - 1, "Mem ", mp,
                     f"{human_bytes(m.used)}/{human_bytes(m.total)}")
            self.bar(y, half, half - 1, "Swp ", sp,
                     f"{human_bytes(m.swap_used)}/{human_bytes(m.swap_total)}")
            y += 1

        # Net + disk summary line(s)
        net = sorted(s.net.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:3]
        parts = [f"{n}: ↓{human_bytes(rx, '/s')} ↑{human_bytes(tx, '/s')}" for n, (rx, tx) in net]
        self.put(y, 0, "Net ", self.c["accent"])
        self.put(y, 4, "   ".join(parts) or "no interfaces")
        y += 1
        dx = 4
        self.put(y, 0, "Dsk ", self.c["accent"])
        for d in s.disks[:4]:
            seg = f"{d.mount} {d.percent:.0f}% ({human_bytes(d.free)} free)"
            self.put(y, dx, seg, self.level(d.percent))
            dx += len(seg) + 3
        y += 1

        # Process table
        self.put(y, 0, f" CPU model: {self.model}  │  CPU total {s.cpu_total:.1f}%", self.c["dim"])
        y += 1
        arrow = "▼" if self.reverse else "▲"
        cols_def = [("PID", 7, "pid"), ("USER", 10, None), ("S", 2, None), ("THR", 4, None),
                    ("CPU%", 6, "cpu"), ("MEM%", 6, "mem"), ("RES", 7, None),
                    ("COMMAND", 0, "name")]
        header = ""
        for name, width, key in cols_def:
            label = name + (arrow if key == self.sort else "")
            if name == "USER":
                header += label.ljust(width) + " "
            elif width:
                header += label.rjust(width) + " "
            else:
                header += " " + label
        self.put(y, 0, header.ljust(w), self.c["head"])
        y += 1

        table_h = h - y - 1
        self._clamp(table_h)
        for i, p in enumerate(self.rows[self.top:self.top + table_h]):
            idx = self.top + i
            line = (f"{p.pid:>7} {p.user[:10]:<10} {p.state:>2} {p.threads:>4} "
                    f"{p.cpu:>6.1f} {p.mem:>6.1f} {human_bytes(p.rss):>7}  {p.cmdline}")
            if idx == self.sel:
                self.put(y + i, 0, line.ljust(w), self.c["sel"])
            else:
                self.put(y + i, 0, line[:44].ljust(w))
                self.put(y + i, 44, line[44:], self.c["ok"] if p.state == "R" else 0)
                if p.cpu >= 1:
                    self.put(y + i, 28, f"{p.cpu:>6.1f}", self.level(p.cpu))

        # Footer
        if self.msg and time.monotonic() < self.msg_until:
            foot = " " + self.msg
        else:
            foot = HELP + (f"│ filter: {self.filter} " if self.filter else "") + \
                f"│ {self.interval:.1f}s "
        self.put(h - 1, 0, foot.ljust(w), self.c["title"])
        self.scr.refresh()

    def _clamp(self, table_h: int) -> None:
        n = len(self.rows)
        self.sel = max(0, min(self.sel, n - 1))
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + table_h:
            self.top = self.sel - table_h + 1
        self.top = max(0, min(self.top, max(0, n - table_h)))

    # ------------------------------------------------------------------ logic
    def refresh_data(self) -> None:
        self.snap = self.mon.sample()
        rows = self.snap.procs
        if self.filter:
            f = self.filter.lower()
            rows = [p for p in rows if f in p.cmdline.lower() or f in p.user.lower()
                    or f == str(p.pid)]
        rows.sort(key=SORT_KEYS[self.sort], reverse=self.reverse)
        self.rows = rows

    def prompt(self, label: str) -> Optional[str]:
        h, w = self.scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        self.scr.nodelay(False)
        self.put(h - 1, 0, label.ljust(w - 1), self.c["title"])
        try:
            raw = self.scr.getstr(h - 1, len(label), 60)
            return raw.decode(errors="ignore").strip()
        except (curses.error, KeyboardInterrupt):
            return None
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.scr.nodelay(True)

    def kill_selected(self) -> None:
        if not self.rows:
            return
        p = self.rows[self.sel]
        ans = self.prompt(f" Send SIGTERM to {p.pid} ({p.name})? [y/N] ")
        if ans and ans.lower().startswith("y"):
            try:
                os.kill(p.pid, signal.SIGTERM)
                self.flash(f"SIGTERM sent to {p.pid}")
            except ProcessLookupError:
                self.flash(f"{p.pid} already exited")
            except PermissionError:
                self.flash(f"Permission denied killing {p.pid} (try sudo)")

    def handle(self, ch: int) -> bool:
        """Return False to quit."""
        page = max(1, self.scr.getmaxyx()[0] - 12)
        if ch in (ord("q"), 27):
            return False
        moves = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page}
        if ch in moves:
            self.sel += moves[ch]
        elif ch == curses.KEY_HOME:
            self.sel = 0
        elif ch == curses.KEY_END:
            self.sel = len(self.rows) - 1
        elif ch in (ord("c"), ord("m"), ord("p"), ord("n")):
            new = {"c": "cpu", "m": "mem", "p": "pid", "n": "name"}[chr(ch)]
            if new == self.sort:
                self.reverse = not self.reverse
            else:
                self.sort, self.reverse = new, new in ("cpu", "mem")
        elif ch == ord("r"):
            self.reverse = not self.reverse
        elif ch == ord("/"):
            f = self.prompt(" Filter (empty to clear): ")
            if f is not None:
                self.filter, self.sel = f, 0
        elif ch == ord("k"):
            self.kill_selected()
        elif ch in (ord("+"), ord("=")):
            self.interval = min(10.0, self.interval + 0.5)
        elif ch in (ord("-"), ord("_")):
            self.interval = max(0.5, self.interval - 0.5)
        else:
            return True
        self.rows.sort(key=SORT_KEYS[self.sort], reverse=self.reverse)
        return True

    def run(self) -> None:
        time.sleep(0.2)  # tiny delay so the first frame has meaningful deltas
        self.refresh_data()
        next_tick = time.monotonic() + self.interval
        dirty = True
        while True:
            if dirty:
                self.draw()
                dirty = False
            curses.napms(30)
            ch = self.scr.getch()
            if ch != -1:
                if ch != curses.KEY_RESIZE and not self.handle(ch):
                    return
                dirty = True
            if time.monotonic() >= next_tick:
                self.refresh_data()
                next_tick = time.monotonic() + self.interval
                dirty = True


def run(interval: float = 1.0, sort: str = "cpu") -> None:
    curses.wrapper(lambda scr: App(scr, interval, sort).run())
