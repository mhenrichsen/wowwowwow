"""Overlay on the WoW window: what the bot is doing, and what comes next.

    python overlay.py

A click-through, always-on-top, transparent window laid over the top-left
corner of the WoW window. It only reads status.json (written by bot.py
twice a second), so it can run while the bot runs and never touches the
game. Close it with the small x in its corner or Ctrl+C in the console.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "status.json")
u32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x80000, 0x20, 0x80
TRANSPARENT = "#010203"


def wow_hwnd():
    for cls in ("GxWindowClassD3d", "GxWindowClass"):
        h = u32.FindWindowW(cls, None)
        if h:
            return h
    return u32.FindWindowW(None, "World of Warcraft")


def wow_rect(h):
    r = wt.RECT()
    if not h or not u32.GetWindowRect(h, ctypes.byref(r)):
        return None
    if u32.IsIconic(h):
        return None
    return r.left, r.top, r.right, r.bottom


def read_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.configure(bg=TRANSPARENT)
        self.frame = tk.Frame(self.root, bg="#000000")
        self.frame.pack(anchor="nw")
        # two columns: text on the left, the radar on the right - the radar
        # under the text left a wide empty band next to it
        self.text = tk.Frame(self.frame, bg="#000000")
        self.text.grid(row=0, column=0, sticky="nw", padx=(6, 8), pady=4)
        self.line1 = tk.Label(self.text, font=("Consolas", 12, "bold"), fg="#ffd866", bg="#000000",
                              anchor="w", justify="left")
        self.line1.pack(anchor="w")
        self.plan = tk.Label(self.text, font=("Consolas", 10), fg="#a9dc76", bg="#000000",
                             anchor="w", justify="left")
        self.plan.pack(anchor="w")
        self.stats = tk.Label(self.text, font=("Consolas", 10), fg="#78dce8", bg="#000000",
                              anchor="w", justify="left")
        self.stats.pack(anchor="w")
        self.logl = tk.Label(self.text, font=("Consolas", 9), fg="#c0c0c0", bg="#000000",
                             anchor="w", justify="left")
        self.logl.pack(anchor="w")
        # top-down sketch: player at the centre, north up, path and mobs
        self.MAP = 150
        self.SCALE = 1.2          # px per yard -> ~60 yd radius
        self.map = tk.Canvas(self.frame, width=self.MAP, height=self.MAP, bg="#000000",
                             highlightthickness=1, highlightbackground="#333333")
        self.map.grid(row=0, column=1, sticky="ne", padx=(0, 6), pady=6)
        self.root.update_idletasks()
        self.hwnd = int(self.root.winfo_id())
        # the label frame is a child; the toplevel is its parent
        self.top = u32.GetParent(self.hwnd) or self.hwnd
        style = u32.GetWindowLongW(self.top, GWL_EXSTYLE)
        u32.SetWindowLongW(self.top, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
        self.last_stamp = 0.0
        self.tick()
        self.root.mainloop()

    def place(self):
        r = wow_rect(wow_hwnd())
        if r is None:
            self.root.withdraw()
            return False
        x, y = r[0] + 12, r[1] + 40      # below the title bar, left edge
        self.root.geometry(f"+{x}+{y}")
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        return True

    def tick(self):
        try:
            if self.place():
                self.render(read_status())
        except Exception as e:
            self.line1.config(text=f"overlay error: {e}")
        self.root.after(250, self.tick)

    def render(self, s):
        if not s:
            self.line1.config(text="bot: no status.json")
            self.plan.config(text="")
            self.stats.config(text="")
            self.logl.config(text="")
            return
        age = time.time() - s.get("ts", 0)
        alive = age < 5 and s.get("running")
        state = s.get("state", "?")
        detail = s.get("detail")
        head = f"{'BOT' if alive else 'BOT (stale %.0fs)' % age}: {state}" + (f" - {detail}" if detail else "")
        t = s.get("target")
        if t:
            head += f"   [{t['name']} L{t['level']} {t['hp_pct']}% @ {t['dist']} yd]"
        self.line1.config(text=head, fg="#ffd866" if alive else "#ff6188")
        plan = s.get("plan") or []
        self.plan.config(text="\n".join(("next: " if i == 0 else "      ") + p for i, p in enumerate(plan)))
        hp = f"{100 * s['hp'] / s['max_hp']:.0f}%" if s.get("max_hp") else "?"
        mana = f"{100 * s['mana'] / s['max_mana']:.0f}%" if s.get("max_mana") else "?"
        xp = f"{s.get('xp', 0)}/{s.get('next_xp', 0)}"
        money = s.get("money", 0)
        stats = (f"L{s.get('level')}  hp {hp}  mana {mana}  xp {xp} ({s.get('xp_per_hour', 0)}/h)\n"
                 f"kills {s.get('kills')}  deaths {s.get('deaths')}  "
                 f"{money // 10000}g {money % 10000 // 100}s")
        if "bags" in s:
            stats += f"  bags {s['bags']}"
        if "durability" in s:
            stats += f"  dur {s['durability']}%"
        self.stats.config(text=stats)
        self.logl.config(text="\n".join(l[9:][:70] for l in s.get("log", [])[-5:]))
        self.draw_map(s)

    def draw_map(self, s):
        c, m, k = self.map, self.MAP, self.SCALE
        c.delete("all")
        cx = cy = m / 2

        def pt(dx, dy):
            # WoW: +x north (up), +y west (left)
            return cx - dy * k, cy - dx * k

        # range rings every 20 yd
        for r in (20, 40, 60):
            c.create_oval(cx - r * k, cy - r * k, cx + r * k, cy + r * k, outline="#222222")
        path = s.get("path") or []
        if path:
            pts = [pt(0, 0)] + [pt(*p) for p in path]
            flat = [v for p in pts for v in p]
            c.create_line(*flat, fill="#ffffff", width=2)
            for p in pts[1:]:
                c.create_rectangle(p[0] - 1.5, p[1] - 1.5, p[0] + 1.5, p[1] + 1.5, fill="#ffffff", outline="")
        goal = s.get("goal")
        if goal:
            gx, gy = pt(*goal)
            d = (goal[0] ** 2 + goal[1] ** 2) ** 0.5
            if d * k > m / 2 - 6:            # off the sketch: arrow at the rim
                f = (m / 2 - 8) / (d * k)
                gx, gy = cx + (gx - cx) * f, cy + (gy - cy) * f
            c.create_oval(gx - 4, gy - 4, gx + 4, gy + 4, outline="#4fd6ff", width=2)
            c.create_text(gx + 6, gy - 6, text=f"{d:.0f}y", fill="#4fd6ff", font=("Consolas", 8), anchor="w")
        colors = {"mob": "#ff5555", "target": "#ffaa00", "friend": "#55dd55", "dead": "#666666"}
        for dx, dy, kind in s.get("units", []):
            x, y = pt(dx, dy)
            if 0 <= x <= m and 0 <= y <= m:
                r = 3 if kind != "target" else 4
                c.create_oval(x - r, y - r, x + r, y + r, fill=colors.get(kind, "#aaaaaa"), outline="")
        # the player: a small triangle pointing along the facing
        import math
        f = s.get("facing", 0.0)
        # facing 0 = north (+x), increases toward west (+y)
        tip = pt(6 * math.cos(f), 6 * math.sin(f))
        l = pt(-3 * math.cos(f) + 3 * math.sin(f), -3 * math.sin(f) - 3 * math.cos(f))
        r_ = pt(-3 * math.cos(f) - 3 * math.sin(f), -3 * math.sin(f) + 3 * math.cos(f))
        c.create_polygon(*tip, *l, *r_, fill="#4cf0ff", outline="")
        c.create_text(3, m - 3, text="N", fill="#555555", font=("Consolas", 8), anchor="sw")


if __name__ == "__main__":
    Overlay()
