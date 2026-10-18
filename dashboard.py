"""Live control panel for the WoW bot: start/stop it and watch what it does.

Run:  pythonw dashboard.py      (or double-click Dashboard.bat)
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "status.json")
STOP = os.path.join(HERE, "STOP")
LOG = os.path.join(HERE, "bot.log")

BG, PANEL, FG, DIM = "#14161a", "#1d2026", "#e6e6e6", "#8a8f98"
HP, MANA, XP, ACCENT = "#3fb950", "#4c8dff", "#b48cff", "#f0b429"
MOB, FRIEND, DEAD, TARGET = "#ff5c5c", "#3fb950", "#5a5f68", "#f0b429"
STATE_COLOUR = {"fighting": MOB, "resting": MANA, "searching": FG, "corpse run": DIM,
                "dead": MOB, "looting": ACCENT, "buffing": XP, "stopped": DIM}


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


class Bar(tk.Canvas):
    def __init__(self, master, colour, **kw):
        super().__init__(master, height=18, bg=PANEL, highlightthickness=0, **kw)
        self.colour = colour

    def set(self, value, maximum, text):
        self.delete("all")
        w = self.winfo_width() or 300
        frac = max(0.0, min(1.0, value / maximum)) if maximum else 0.0
        self.create_rectangle(0, 0, w, 18, fill="#2a2e36", width=0)
        self.create_rectangle(0, 0, w * frac, 18, fill=self.colour, width=0)
        self.create_text(w / 2, 9, text=text, fill="white", font=("Segoe UI", 9, "bold"))


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WoW Bot")
        self.configure(bg=BG)
        self.geometry("420x720")
        self.attributes("-topmost", True)
        self.proc: subprocess.Popen | None = None
        self.big = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self.mid = tkfont.Font(family="Segoe UI", size=10)
        self.small = tkfont.Font(family="Consolas", size=8)

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=12, pady=(12, 4))
        self.name = tk.Label(top, text="-", font=self.big, fg=FG, bg=BG)
        self.name.pack(side="left")
        self.state = tk.Label(top, text="not running", font=self.mid, fg=DIM, bg=BG)
        self.state.pack(side="right")

        bars = tk.Frame(self, bg=BG)
        bars.pack(fill="x", padx=12)
        self.hp, self.mana, self.xp = Bar(bars, HP), Bar(bars, MANA), Bar(bars, XP)
        for b in (self.hp, self.mana, self.xp):
            b.pack(fill="x", pady=2)

        grid = tk.Frame(self, bg=BG)
        grid.pack(fill="x", padx=12, pady=6)
        self.stats = {}
        for i, key in enumerate(("XP / hour", "Level in", "Kills", "Deaths", "Money", "Uptime")):
            cell = tk.Frame(grid, bg=PANEL)
            cell.grid(row=i // 3, column=i % 3, sticky="nsew", padx=2, pady=2)
            grid.columnconfigure(i % 3, weight=1)
            tk.Label(cell, text=key, font=("Segoe UI", 8), fg=DIM, bg=PANEL).pack(anchor="w", padx=6, pady=(4, 0))
            v = tk.Label(cell, text="-", font=("Segoe UI", 11, "bold"), fg=FG, bg=PANEL)
            v.pack(anchor="w", padx=6, pady=(0, 4))
            self.stats[key] = v

        self.radar = tk.Canvas(self, width=396, height=250, bg=PANEL, highlightthickness=0)
        self.radar.pack(padx=12, pady=4)

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", padx=12, pady=6)
        self.start_btn = tk.Button(btns, text="Start bot", command=self.start, bg=HP, fg="black",
                                   relief="flat", font=("Segoe UI", 10, "bold"), padx=10)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.stop_btn = tk.Button(btns, text="Stop bot", command=self.stop, bg=MOB, fg="black",
                                  relief="flat", font=("Segoe UI", 10, "bold"), padx=10)
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.log = tk.Text(self, height=12, bg=PANEL, fg=FG, font=self.small, relief="flat",
                           wrap="none", state="disabled")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.after(200, self.refresh)

    # ---------------------------------------------------------- control
    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if os.path.exists(STOP):
            os.remove(STOP)
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        self.proc = subprocess.Popen(
            [exe, "-u", os.path.join(HERE, "bot.py")], cwd=HERE,
            stdout=open(LOG, "a", encoding="utf-8"), stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.state.config(text="starting...", fg=ACCENT)

    def stop(self):
        open(STOP, "w").close()
        self.state.config(text="stopping...", fg=ACCENT)

    # ---------------------------------------------------------- refresh
    def refresh(self):
        try:
            st = json.load(open(STATUS, encoding="utf-8"))
        except Exception:
            st = None
        running_proc = self.proc is not None and self.proc.poll() is None
        bot_alive = bool(st and st.get("running") and time.time() - st.get("ts", 0) < 3)
        if st:
            self.render(st, running_proc)
        self.start_btn.config(state="disabled" if running_proc or bot_alive else "normal")
        self.after(500, self.refresh)

    def render(self, st, running_proc):
        fresh = time.time() - st.get("ts", 0) < 3 and st.get("running")
        self.name.config(text=f"{st.get('name', '-')}  ·  level {st.get('level', '?')}")
        state = st.get("state", "?") if fresh else "stopped"
        t = st.get("target")
        label = state
        if state == "fighting" and t:
            label = f"fighting {t['name']} {t['hp_pct']}%"
        self.state.config(text=label, fg=STATE_COLOUR.get(state, FG))
        self.hp.set(st["hp"], st["max_hp"], f"HP {st['hp']} / {st['max_hp']}")
        self.mana.set(st["mana"], st["max_mana"], f"Mana {st['mana']} / {st['max_mana']}")
        self.xp.set(st["xp"], st["next_xp"], f"XP {st['xp']} / {st['next_xp']}")
        xph = st.get("xp_per_hour", 0)
        self.stats["XP / hour"].config(text=f"{xph:,}" if xph else "measuring")
        left = st["next_xp"] - st["xp"]
        self.stats["Level in"].config(text=fmt_dur(left / xph * 3600) if xph else "-")
        self.stats["Kills"].config(text=str(st.get("kills", 0)))
        self.stats["Deaths"].config(text=str(st.get("deaths", 0)))
        self.stats["Uptime"].config(text=fmt_dur(st.get("uptime", 0)))
        m = st.get("money", 0)
        self.stats["Money"].config(text=(f"{m // 10000}g " if m >= 10000 else "") +
                                   (f"{m // 100 % 100}s " if m >= 100 else "") + f"{m % 100}c")
        self.stats["Kills"].config(text=f"{st.get('kills', 0)}  ({st.get('looted', 0)} looted)")
        self.draw_radar(st)
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(st.get("log", [])))
        self.log.see("end")
        self.log.config(state="disabled")

    def draw_radar(self, st):
        c = self.radar
        c.delete("all")
        w, h = 396, 250
        cx, cy = w / 2, h / 2
        scale = (h / 2 - 8) / 60.0                      # 60 yd to the edge
        for r in (20, 40, 60):
            c.create_oval(cx - r * scale, cy - r * scale, cx + r * scale, cy + r * scale,
                          outline="#2c313a")
        # leash circle around home. WoW: +x north, +y west -> screen up / left
        hx, hy = st["home"]
        R = st["radius"] * scale
        c.create_oval(cx - hy * scale - R, cy - hx * scale - R,
                      cx - hy * scale + R, cy - hx * scale + R, outline="#3a3f2a", dash=(3, 3))
        colours = {"mob": MOB, "friend": FRIEND, "dead": DEAD, "target": TARGET}
        for dx, dy, kind in st.get("units", []):
            px, py = cx - dy * scale, cy - dx * scale
            r = 5 if kind == "target" else 3
            c.create_oval(px - r, py - r, px + r, py + r, fill=colours[kind], outline="")
        f = st["facing"]
        tip = (cx - math.sin(f) * 10, cy - math.cos(f) * 10)
        left = (cx - math.sin(f + 2.5) * 6, cy - math.cos(f + 2.5) * 6)
        right = (cx - math.sin(f - 2.5) * 6, cy - math.cos(f - 2.5) * 6)
        c.create_polygon(*tip, *left, *right, fill="white")
        c.create_text(8, 8, anchor="nw", text="60 yd", fill=DIM, font=("Segoe UI", 8))


if __name__ == "__main__":
    Dashboard().mainloop()
