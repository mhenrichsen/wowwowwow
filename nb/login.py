"""Login watchdog for the side-by-side clients: start a missing client, put its window in its quadrant,
log in (password from Windows Credential Manager, see nb.cred), pick the character and enter the world -
and do it all again after a disconnect or a client crash.

  python -m nb.login 1 2 3 4        watch these instances (log: nb/login.log)
Screens are told apart by Windows OCR of the client window alone (nb.glue); in world = BotEye strip seen.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time

from nb import cred, glue, multi
from nb.eye import Eye
from nb.move import fake_activate
from nb.win import Keys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "login.log")
STOP = os.path.join(HERE, "STOP_LOGIN")
u32 = ctypes.windll.user32

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
# the character each instance plays (this server wants two names). 2026-10-14 18:30: fresh level-1 dwarf shamans for
# the RestedXP-addon run; the old Shamaa..Shamad stay on the accounts and are simply not selected
# 2026-10-15: instance 4 plays Bob (level 15, "Bob Mana" in the list); Shamah (level 8) on that account is not used
# 2026-10-17: instance 1 plays the human hunter "Hunt man" (made by the user) for the hunter 1->14 run; Shamau stays
NAMES = {1: ("Hunt", "man"), 2: ("Shamav", "Stone"), 3: ("Shamaw", "Stone"), 4: ("Shamax", "Stone")}
# race / class each instance plays and creates - the bot's class comes from here (nb/route.py, nb/classes.py):
# (race label, class label, words that confirm it on the right-hand panel,
# portrait offsets below the "Alliance" heading to try: the dwarf is the 2nd portrait at +137)
RACECLASS = {i: ("dwarf", "shaman", ("dwarf", "stoneform"), (137,)) for i in (2, 3, 4)}
RACECLASS[1] = ("human", "hunter", ("human", "diplomacy", "every man", "perception"), (70, 65, 75, 60, 80, 55, 85))


def log(msg: str):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def click(hwnd: int, x: int, y: int):
    """Posted left click at client pixel (x, y) (works on the glue screens of a background window)."""
    lp = (int(y) << 16) | (int(x) & 0xFFFF)
    u32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.08)
    u32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lp)
    time.sleep(0.08)
    u32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


def find(lines, *words):
    """First OCR line containing any of the words (lower case)."""
    for l in lines:
        t = l["t"].lower()
        if any(w in t for w in words):
            return l
    return None


class Client:
    TRY_RESET_S = 300.0     # s after the 3rd failed login before trying again (a server restart fails them all)
    def __init__(self, inst: int):
        self.inst = inst
        self.hwnd = 0
        self.launched_t = 0.0
        self.placed = False
        self.screen = ""
        self.screen_t = time.time()
        self.tries = 0                 # password submissions since the last time in world
        self.try_t = 0.0               # the last password submission
        self.acted_t = 0.0
        self.in_world_t = 0.0
        self.entered_t = 0.0            # last "enter world" press: the loading screen + first frames come after it
        self.eye = None

    def say(self, msg):
        log(f"[{self.inst}] {msg}")

    def window(self) -> int:
        pid = multi.clients().get(self.inst)
        return multi.window_of(pid) if pid else 0

    def tick(self):
        h = self.window()
        if not h:
            if time.time() - self.launched_t > 120:
                self.say("no client: launching")
                multi.launch(self.inst)
                self.launched_t, self.placed, self.hwnd, self.tries = time.time(), False, 0, 0
            return
        if h != self.hwnd:
            self.hwnd, self.placed, self.eye = h, False, Eye(h)
        if not self.placed:
            time.sleep(1.0)
            multi.place(self.inst)
            self.placed = True
            return
        if self.eye.raw(full=False) is not None:          # BotEye strip: in world
            if self.screen != "world":
                self.say("in world")
                self.screen, self.screen_t = "world", time.time()
                self.check_t = time.time()
            # the right character? A select click that did not take leaves Enter on the last played one (2026-10-16
            # 08:16: Shamae and Shamakmak entered for Shamai / Shamak). The DB marks a login online within seconds.
            if getattr(self, "check_t", 0.0) and time.time() - self.check_t > 20:
                self.check_t = 0.0
                want = NAMES.get(self.inst, (None,))[0]
                on = online_name(want)
                if on and on.lower() != str(want).lower():
                    self.say(f"in world as {on}, not {want}: logging out")
                    Keys(h).chat("/camp")
                    self.screen = ""
            self.tries, self.in_world_t = 0, time.time()
            return
        if self.screen == "world" and time.time() - self.screen_t < 1:
            return
        if time.time() - self.acted_t < 6:
            return                                      # give the last action time to show
        lines = glue.read_text(h)
        scr = glue.screen(lines)
        if scr == "unknown" and find(lines, "shop", "ruleset", "rutcser", "enter w", "ente;"):
            # the character list OCRs badly ("SHOP | ag St0he | Ente; W | or/d | Rutcser", 20:23): it is charselect.
            # The unknown-screen Esc opened the game menu there and looped (client 3, 20:22-20:24)
            scr = "charselect"
        if scr != self.screen:
            self.say(f"screen: {scr}  ({' | '.join(l['t'] for l in lines[:6])})")
            self.screen, self.screen_t = scr, time.time()
        stuck = time.time() - self.screen_t
        keys = Keys(h)                                  # posted keys reach a background glue screen: no fake activation
        if scr == "login":
            c = cred.load()
            if not c:
                if stuck < 8:
                    self.say("no stored password: run  python -m nb.cred set")
                return
            if self.tries >= 3 and time.time() - getattr(self, "try_t", 0.0) > self.TRY_RESET_S:
                self.tries = 0                          # a server restart fails 3 tries in a row: try again later
                self.say(f"login: {self.TRY_RESET_S:.0f} s since the last failed try; trying again")
            if self.tries >= 3:
                if stuck < 8:
                    self.say("3 logins in a row did not get in; not trying again (check the password)")
                return
            # never type the password into the world: only after 15 s without the strip, and the strip still absent
            # right now (the 20:25:14 misread came 14 s after "enter world", strip not yet painted)
            if time.time() - max(self.in_world_t, self.entered_t) < 60 or self.eye.raw(full=False) is not None:
                return
            box = find(lines, "password", "assword")
            if not box:
                if stuck < 8:
                    self.say("login screen without a password box: not typing")
                return
            if box:                                     # the password edit sits right under its label
                click(h, box["x"] + box["w"] // 2, box["y"] + box["h"] + 30)
                time.sleep(0.3)
            keys.press("end")
            for _ in range(40):
                keys.press("backspace", 0.01)
            keys.type_text(c[1])
            time.sleep(0.2)
            keys.press("enter")
            self.tries += 1
            self.try_t = time.time()
            self.say(f"login submitted (try {self.tries})")
        elif scr == "accounts":
            want = find(lines, f"wow{self.inst}")
            if want:
                click(h, want["x"] + want["w"] // 2, want["y"] + want["h"] // 2)
                time.sleep(0.4)
            keys.press("enter")
            self.say("game account picked")
        elif scr == "ruleset":
            n = next((l for l in lines if l["t"].strip().lower() == "normal"), None)
            if n:
                click(h, n["x"] + n["w"] // 2, n["y"] + 120)          # the Normal card
                time.sleep(1.0)
                sel = next((l for l in glue.read_text(h) if l["t"].strip().lower() == "select"), None)
                if sel:
                    click(h, sel["x"] + sel["w"] // 2, sel["y"] + sel["h"] // 2)
                    self.say("ruleset: Normal")
            elif stuck > 30:
                # no Normal card: the realm was offline when the list was fetched (worldserver restart 16:29) and the
                # screen never refreshes by itself; back out to the login screen and log in again
                # neither Esc nor Cancel leaves this screen for good (16:39-16:42, clients 2-4 looped); only a fresh
                # login fetches the realm list again (client 1 got in that way at 16:39): restart the client
                self.say("ruleset: no Normal realm listed (list fetched while the realm was down); restarting the client")
                subprocess.run(["taskkill", "/F", "/PID", str(multi.clients()[self.inst])], capture_output=True)
                self.launched_t, self.screen_t = 0.0, time.time()
        elif scr == "charselect":
            if find(lines, "game menu"):                # an Esc opened the game menu over the list: it eats the clicks
                keys.press("esc")
                self.say("charselect: game menu open; closed it")
                return
            if find(lines, "retrieving character list", "retrieving"):
                return                                  # the list is still loading (17:50:16 an empty-looking list sent
                #                                         client 1 into character creation for its existing Shamau)
            want, last = NAMES[self.inst]
            # the name as a whole word: "shamaj" is inside "Shamajamaj Stone" (a botched creation, 2026-10-16 08:12)
            me = next((l for l in lines if re.search(rf"\b{re.escape(want.lower())}\b", l["t"].lower())), None)
            if not me and last.lower() != "stone":
                # a short first name garbles easily ("B�b mana"): a last name no other character has
                me = next((l for l in lines if last.lower() in l["t"].lower()), None)
            if not me:
                # garbled list text ("ag St0he" for "Shamag Stone"): the name's last two letters + " st"
                tail = want.lower()[-2:] + " st"
                # ... in a word of about our name's length ("ak st" is in "Shamakmak Stone" too: 2026-10-16 08:15)
                def tail_ok(t):
                    m = re.search(r"(\S*)" + re.escape(tail), t.lower().replace("0", "o"))
                    return bool(m) and abs(len(m.group(1)) + 2 - len(want)) <= 1
                me = next((l for l in lines if tail_ok(l["t"])), None)
            if not me and find(lines, "stone", "st0he", "stohe") and char_exists(want) is not False:
                return                                  # a list is shown but unreadable: wait for a clearer read
            if not me:                                  # our character is not on this account yet: make it
                b = find(lines, "create character", "create new character")
                if b and char_exists(want):
                    b = None                            # it exists (the DB says so): never make it again - open the
                    #                                     list instead (below) and read it on the next pass
                if b:
                    click(h, b["x"] + b["w"] // 2, b["y"] + b["h"] // 2)
                    self.say(f"no {want} yet: character creation")
                elif time.time() - getattr(self, "toggle_t", 0.0) > 15:
                    # no create button: the character list is collapsed (only the selected character and "Toggle
                    # Character List" bottom right, which OCR missed; inst3 2026-10-16 12:03-12:10 waited forever)
                    self.toggle_t = time.time()
                    sx, sy = (self.eye.cap.w / 1704.0, self.eye.cap.h / 657.0) if self.eye else (1.0, 1.0)
                    click(h, 1408 * sx, 629 * sy)
                    self.say(f"no {want} yet and no create button: opening the character list")
                return
            click(h, me["x"] + me["w"] // 2, me["y"] + me["h"] // 2)    # select it by name, not "last played"
            time.sleep(1.0)
            keys.press("enter")
            self.entered_t = time.time()
            self.say(f"enter world as {want}")
        elif scr == "race" and char_exists(NAMES[self.inst][0]):
            b = find(lines, "back")                     # the character exists: no new one, back to the list
            if b:
                click(h, b["x"] + b["w"] // 2, b["y"] + b["h"] // 2)
            else:
                keys.press("esc")
            self.say("race page for a character that exists: back to the list")
        elif scr == "race":
            race, cls, race_words, offsets = RACECLASS[self.inst]
            a = find(lines, "alliance")
            sh = next((l for l in lines if l["t"].strip().lower() == cls), None)
            if a and sh:
                # the race portrait: its offset below the heading is tried until the panel names the race (the
                # human's first-portrait offset is not measured; each try is checked, never assumed)
                k = getattr(self, "race_try", 0) % len(offsets)
                self.race_try = k + 1
                click(h, a["x"] + a["w"] // 2, a["y"] + offsets[k])
                time.sleep(1.5)
                click(h, sh["x"] + sh["w"] // 2, sh["y"] - 40)        # the class: icon above its label
                time.sleep(1.5)
                ls = glue.read_text(h)
                right = " ".join(l["t"].lower() for l in ls if l["x"] > 1100)
                if any(w in right for w in race_words) and cls in right:   # the heading can scroll away
                    c = find(ls, "customize")
                    click(h, c["x"] + c["w"] // 2, c["y"] + c["h"] // 2)
                    self.say(f"race/class: {race} {cls} (portrait +{offsets[k]})")
                else:
                    self.say(f"race/class page did not take {race} {cls} at portrait +{offsets[k]}: {right[:80]}")
        elif scr in ("name", "race") and char_exists(NAMES[self.inst][0]):
            # creating a character that already exists (the name is taken: Finish does nothing and this looped,
            # clients 1 and 3 17:50-18:04): back out to the character list. A "That name is taken" dialog sits on
            # top and swallows the Back click: its Okay first
            ok = next((l for l in lines if l["t"].strip().lower() in ("okay", "ok")), None)
            if ok:
                click(h, ok["x"] + ok["w"] // 2, ok["y"] + ok["h"] // 2)
                time.sleep(1.0)
                lines = glue.read_text(h)
            b = find(lines, "back")
            if b:
                click(h, b["x"] + b["w"] // 2, b["y"] + b["h"] // 2)
            else:
                keys.press("esc")
            self.say("character creation for a character that exists: back to the list")
        elif scr == "name":
            first, last = NAMES[self.inst]
            sx, sy = (self.eye.cap.w / 1704.0, self.eye.cap.h / 657.0) if self.eye else (1.0, 1.0)
            for (x, y), text in (((705, 47), first), ((998, 47), last)):    # main / secondary name boxes
                click(h, x * sx, y * sy)
                time.sleep(0.4)
                for _ in range(14):
                    keys.press("backspace", 0.01)
                keys.type_text(text)
                time.sleep(0.4)
            click(h, 1288 * sx, 604 * sy)                                   # Finish
            self.say(f"character {first} {last} submitted")
        elif scr == "dialog":
            ok = next((l for l in lines if l["t"].strip().lower() in ("okay", "ok", "accept", "continue")), None)
            if ok:
                click(h, ok["x"] + ok["w"] // 2, ok["y"] + ok["h"] // 2)
                self.say("dialog dismissed")
            elif stuck > 5 and find(lines, "disconnected"):
                # "You have been disconnected from the server" over the login screen, its Okay button not read by the
                # OCR (a server restart, 2026-10-18 09:08: the watchdog sat on it 5 min). A glue dialog: Esc closes it
                keys.press("esc")
                self.say("disconnect dialog closed (Esc)")
            # no button: never press Enter blind - the loading screen's "Tip: ..." line OCRs as a dialog, and an Enter
            # sent just as the world appears opens the chat box, which then swallows every letter key the bot sends
            # (Shamab stood still 16:45:46-16:48:30, W/S dead, arrows fine)
        elif scr == "busy":
            if stuck > 90:
                keys.press("esc")
                self.say("stuck connecting for 90 s: cancel")
                self.screen_t = time.time()
        else:                                           # intro movie / cinematic / unknown
            if stuck > 4:
                keys.press("esc")
            if stuck > 240:
                self.say("unknown screen for 4 min: restarting the client")
                subprocess.run(["taskkill", "/F", "/PID", str(multi.clients()[self.inst])], capture_output=True)
                self.launched_t = 0.0
        self.acted_t = time.time()


def char_exists(name):
    """Is `name` a character on the server (tcf_characters)? None when the DB cannot tell. (The list read alone
    took the old characters' "... Stone" lines for an unreadable list and never made a new one, 2026-10-16 08:03.)"""
    try:
        import worlddb
        if not str(name).isalpha():
            return None
        return bool(worlddb._query_db("tcf_characters", "SELECT 1 FROM characters WHERE name = '%s'" % name))
    except Exception:
        return None


def online_name(name):
    """The character online on `name`'s account (None when none / the DB cannot tell)."""
    try:
        import worlddb
        if not str(name).isalpha():
            return None
        r = worlddb._query_db("tcf_characters", "SELECT name FROM characters WHERE online = 1 AND account = "
                              "(SELECT account FROM characters WHERE name = '%s')" % name)
        return r[0][0] if r else None
    except Exception:
        return None


def create(inst: int, name: str):
    """Make the instance's configured race / class (RACECLASS) on instance `inst` (character select must be up):
    OCR + posted clicks."""
    race, cls = RACECLASS[inst][:2]
    h = multi.find_instance_window(inst)
    keys = Keys(h)

    def look():
        ls = glue.read_text(h)
        log(f"[{inst}] create: {' | '.join(l['t'] for l in ls[:14])}")
        return ls

    def press_text(*words, wait=2.0):
        ls = look()
        b = find(ls, *words)
        if not b:
            return False
        click(h, b["x"] + b["w"] // 2, b["y"] + b["h"] // 2)
        time.sleep(wait)
        return True

    if not press_text("create character", "create new character", wait=4.0):
        raise SystemExit("no Create Character button")
    press_text("alliance", wait=1.5)
    if not press_text(race, wait=1.5):
        log(f"[{inst}] create: no '{race}' text; see the window")
    if not press_text(cls, wait=1.5):
        log(f"[{inst}] create: no '{cls}' text; see the window")
    for _ in range(2):                                   # race/class page -> customize -> name
        if press_text("next", "customize", wait=3.0):
            continue
        break
    ls = look()
    box = find(ls, "name")
    if box:
        click(h, box["x"] + box["w"] // 2, box["y"] + box["h"] // 2)
        time.sleep(0.3)
    for _ in range(14):
        keys.press("backspace", 0.01)
    keys.type_text(name)
    time.sleep(0.3)
    if not press_text("create character", "finish", "create", wait=5.0):
        keys.press("enter")
    look()


if __name__ == "__main__":
    if sys.argv[1:2] == ["create"]:
        create(int(sys.argv[2]), sys.argv[3])
        sys.exit(0)
    insts = [int(a) for a in sys.argv[1:]] or [1, 2, 3, 4]
    if os.path.exists(STOP):
        os.remove(STOP)
    log(f"login watchdog for instances {insts}")
    cs = [Client(i) for i in insts]
    while not os.path.exists(STOP):
        for c in cs:
            try:
                c.tick()
            except Exception as e:                       # never let one client's hiccup stop the watch
                c.say(f"error: {e!r}")
        time.sleep(2)
    log("login watchdog stopped")
