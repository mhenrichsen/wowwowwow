"""Offline tests for the RXP follower (nb/rxp_follow.py, nb/rxp_policy.py) with fake State.rxp objects and a fake
world (a virtual clock, keys that act on it). Touches no game window, sends nothing.

Run: python -m nb.test_rxp_follow   (from the wowbot folder).
"""

from __future__ import annotations

import dataclasses
import math
import os
import sys
import tempfile
import time as _time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import bot as botmod             # noqa: E402
from nb import rxp_follow as rf          # noqa: E402
from nb import rxp_policy as pol         # noqa: E402
from nb.bot import Bot                   # noqa: E402
from nb.eye import (Rxp, Plate, Quest, K_ACCEPT, K_TURNIN, K_COMPLETE, K_COLLECT, K_GOTO, K_WANDER,   # noqa: E402
                    K_MOB, K_TARGET, K_USE, K_TRAIN, K_VENDOR, K_FLY, K_HS, K_XP, K_DEATHSKIP, K_MANUAL,
                    K_OTHER, Q_ACCEPT, Q_TURNIN, Q_COMPLETE)

LOGS: list[str] = []
botmod.log = lambda m: LOGS.append(m)


class Clock:
    """Virtual time for rxp_follow / bot: sleep() advances it, so the waits run instantly."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, d):
        self.now += max(0.0, d)

    def strftime(self, *a):
        return _time.strftime(*a)

    def monotonic(self):
        return self.now


CLOCK = Clock()
rf.time = CLOCK
botmod.time = CLOCK


def rxp(**kw) -> Rxp:
    d = dict(present=True, loaded=True, hidden=False, qauto=True, arrow=False, done=False, loadnext=False,
             nonext=False, gamepad=True, invmgr=True, taxi=False, trainauto=False, stepctr=1,
             guide_ord=1, guide="01-05 Coldridge Valley", index=1, line=18, nactive=1, sticky=False, flag=True,
             kinds=0, skinds=0, wp=None, quests=[], item=0, item_btn=0, item_ready=False, train_id=0,
             train_known=False, xp_level=0, n_mobs=0, n_targets=0, n_scan=0, tgtctr=1, text="T:|M:", pts=[])
    d.update(kw)
    return Rxp(**d)


STEN = dict(kinds=K_ACCEPT | K_WANDER | K_TARGET, wp=(-6214.85, 328.18, None, 0, 1), quests=[(179, Q_ACCEPT, False)],
            n_targets=1, text="T:Sten Stoutarm|M:")
WOLVES = dict(line=24, kinds=K_COMPLETE | K_MOB | K_WANDER, quests=[(179, Q_COMPLETE, False)], n_mobs=1,
              text="T:|M:Ragged Young Wolf", wp=(-6100.0, 300.0, None, 0, 1),
              pts=[(-6100.0, 300.0, None, 1), (-6120.0, 320.0, None, 1), (-6000.0, 0.0, None, 3)])


def plate(npc, **kw):
    d = dict(sx=900, sy=500, npc=npc, level=2, hp=100, hostile=True, dead=False, on_me=False, is_target=False,
             tapped=False, combat=False, player=False, elite=False, friendly=False, token=1, ghash=npc)
    d.update(kw)
    return Plate(**d)


class FS:
    """A strip State with only what the follower reads."""

    def __init__(self, **kw):
        self.t = CLOCK.now
        self.x, self.y, self.z = -6200.0, 330.0, 400.0
        self.level, self.hp_pct, self.mana_pct = 2, 100.0, 100.0
        self.plates, self.quests, self.nquests = [], [], 0
        self.combat = self.dead = self.ghost = self.moving = self.casting_now = False
        self.has_target = self.t_dead = self.t_rxp_npc = self.t_rxp_mob = self.t_within10 = False
        self.t_attackable = self.t_tapped = self.t_player = False
        self.t_npc, self.t_level, self.t_hash = 0, 0, 0
        self.msg, self.err, self.errn, self.ui = "", 0, 0, 0
        self.rxp = None
        self.__dict__.update(kw)

    def quest(self, qid):
        return next((q for q in self.quests if q.qid == qid), None)

    def ui_open(self, bit):
        return bool(self.ui >> bit & 1)


class World:
    """Current state + scheduled changes + key handlers. state() hands out a fresh snapshot each read."""

    def __init__(self, r, **kw):
        self.st = FS(rxp=r, **kw)
        self.events: list = []           # (time, fn)
        self.keys: list[str] = []
        self.chat: list[str] = []
        self.walks: list = []
        self.walk_kw: list = []
        self.walk_res: list = []          # scripted travel_to results ("ok" when empty)
        self.on_key: dict = {}

    def at(self, dt, fn):
        self.events.append((CLOCK.now + dt, fn))

    def state(self):
        due = [e for e in self.events if e[0] <= CLOCK.now]
        self.events = [e for e in self.events if e[0] > CLOCK.now]
        for _, fn in sorted(due, key=lambda e: e[0]):
            fn(self.st)
        s = FS(**{k: v for k, v in self.st.__dict__.items()})
        s.t = CLOCK.now
        s.plates = list(self.st.plates)
        s.quests = list(self.st.quests)
        if self.st.rxp is not None:
            s.rxp = dataclasses.replace(self.st.rxp, quests=list(self.st.rxp.quests))
        return s

    def press(self, k, hold=0.04):
        self.keys.append(k)
        CLOCK.sleep(hold)
        if k == "esc":                   # one window per press, as close_windows assumes
            for b in range(8):
                if self.st.ui >> b & 1:
                    self.st.ui &= ~(1 << b)
                    break
        elif k == "f11":
            self.st.has_target = self.st.t_rxp_npc = self.st.t_rxp_mob = False
            self.st.t_npc = 0
        fn = self.on_key.get(k)
        if fn:
            fn(self.st)

    def do_chat(self, line):
        self.chat.append(line)
        self.keys.append("chat:" + line)

    def walk(self, x, y, z=None, tol=2.0, **kw):
        self.walks.append((round(x, 1), round(y, 1), tol))
        self.walk_kw.append(dict(kw, z=z))
        res = self.walk_res.pop(0) if self.walk_res else "ok"
        if res == "ok":
            self.st.x, self.st.y = x, y
        elif isinstance(res, tuple):          # (result, where the walk ended)
            res, (self.st.x, self.st.y) = res
        return res


class FakeMover:
    def __init__(self):
        self.zones = []
        self.nm = SimpleNamespace(ground_z=lambda x, y, hint=None: 401.0)
        self.stops = 0
        self.legs = []

    def stop(self, *a, **k):
        self.stops += 1

    def padding(self):
        return False


def make_bot(w: World, qdb=None):
    b = Bot.__new__(Bot)
    b.rxp_mode = True
    b.eye = None
    b.rxp_init()
    b._rx_gaveup = set()                        # given-up quests: in memory, never the live bot's file
    b._rx_gaveup_file = os.path.join(tempfile.gettempdir(), "nb_test_rx_gaveup.json")
    b.mv = FakeMover()
    b.s = w.state
    b.keys = SimpleNamespace(press=w.press, chat=w.do_chat)
    b.travel_to = w.walk
    b.approach = lambda *a, **k: None
    b.indoors = lambda p: False
    b.roams = []
    b.roam = lambda pts=None, indoor_ok=False: b.roams.append((pts, indoor_ok, b.rxp_hunting, set(b.wanted)))
    b.gathered, b.used = [], []
    b.gather_object = lambda *a: b.gathered.append(a)
    b.use_object = lambda *a: b.used.append(a)
    b.wanted, b.grind_ok, b.want_seen_t = set(), False, 0.0
    b.qdb = qdb or SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={},
                                   go_src={}, skip_gather={}, gathered={}, npc_pos=lambda n, near: None)
    b.train_need = 0
    b._rxp_spawns = lambda: []                 # no DB in the tests: each test hands in the spawns it needs
    b._rxp_db_npcs = lambda name: []
    b._rxp_guide_steps = lambda: {}
    b._rxp_qobjs = lambda qid: []              # no DB: tests that need quest_objectives rows hand them in
    return b


def reset():
    LOGS.clear()
    CLOCK.now += 10_000.0
    pol.LEVEL_GATE.clear()
    pol.LEVEL_GATE.update({313: 8})        # the live table is empty (no gates); the tests keep the mechanism working


# ------------------------------------------------------------ policy (pure)
def test_classify():
    assert pol.classify(rxp(**STEN)) == "talk"
    sapta = rxp(kinds=K_OTHER | K_USE | K_WANDER, item=6635, item_btn=1, item_ready=True)
    assert pol.classify(sapta) == "use"
    assert pol.classify(sapta, uses=pol.USE_MAX) == "manual"          # pressed USE_MAX times: leave it
    assert pol.classify(rxp(**WOLVES)) == "kill"
    # Powder Keg (file L1984): kill + use + accept, no .target. Not in the bags (no button) -> kill; looted -> use
    keg = dict(kinds=K_MOB | K_USE | K_ACCEPT, item=268548, n_mobs=1, quests=[(400, Q_ACCEPT, False)])
    assert pol.classify(rxp(**keg)) == "kill"
    assert pol.classify(rxp(item_btn=2, **keg)) == "use"
    assert pol.classify(rxp(kinds=K_XP, xp_level=5)) == "xp"
    assert pol.classify(rxp(kinds=K_GOTO | K_WANDER)) == "goto"
    assert pol.classify(rxp(kinds=K_DEATHSKIP | K_TARGET)) == "deathskip"
    assert pol.classify(rxp(kinds=K_MANUAL | K_WANDER)) == "manual"
    assert pol.classify(rxp(kinds=0)) == "manual"
    assert pol.classify(rxp(kinds=K_HS, item=6948)) == "hs"
    assert pol.classify(rxp(kinds=K_TRAIN | K_TARGET, n_targets=1)) == "talk"
    assert pol.classify(rxp(kinds=K_TRAIN | K_TARGET, n_targets=1, train_known=True)) == "manual"
    assert pol.classify(rxp(kinds=K_VENDOR | K_TARGET | K_WANDER, n_targets=1)) == "talk"


def test_vendor_collect_is_talk():
    # Adlin: .collect 159,10 + .target Adlin Pridedrift, no mob (file L119-129): talk, BotEye buys (buy_collect)
    assert pol.classify(rxp(kinds=K_COLLECT, n_targets=1, text="T:Adlin Pridedrift|M:")) == "talk"
    assert pol.classify(rxp(kinds=K_COLLECT | K_MOB, n_mobs=1, quests=[(183, pol.Q_COLLECT, False)])) == "kill"


def test_step_policy():
    reset()
    assert pol.step_policy(rxp(**STEN), 1) is None
    a = pol.step_policy(rxp(kinds=K_ACCEPT, quests=[(86614, Q_ACCEPT, False)]), 3)
    assert a[0] == "skip" and "86614" in a[1] and a[2] is False
    a = pol.step_policy(rxp(kinds=K_ACCEPT, quests=[(86614, Q_ACCEPT, False), (400, Q_ACCEPT, False)]), 3)
    assert a[0] == "skip" and a[2] is True                                  # talk once first for 400
    assert pol.step_policy(rxp(kinds=K_ACCEPT, quests=[(86614, Q_ACCEPT, True)]), 3) is None   # done already
    a = pol.step_policy(rxp(kinds=K_ACCEPT | K_TARGET, text="T:Eldrun Stormbreaker::258098|M:",
                            quests=[(94449, Q_ACCEPT, False)]), 10)
    assert a[0] == "skip" and "Eldrun" in a[1]
    a = pol.step_policy(rxp(guide="05-11 Dun Morogh", kinds=K_FLY | K_TARGET), 10)
    assert a == ("load", pol.GUIDE_LOCH, a[2])
    assert pol.step_policy(rxp(guide=pol.GUIDE_LOCH, kinds=K_FLY), 13) is None     # only on 05-11's flight
    assert pol.step_policy(rxp(kinds=K_COMPLETE, quests=[(313, Q_COMPLETE, False)]), 7) == ("gate", 313, 8)
    assert pol.step_policy(rxp(kinds=K_COMPLETE, quests=[(313, Q_COMPLETE, False)]), 8) is None
    assert pol.step_policy(rxp(kinds=K_ACCEPT, quests=[(313, Q_ACCEPT, False)]), 7) is None     # accept is fine


def test_guide_action_and_names():
    assert pol.guide_action(rxp(**STEN), 1) is None
    assert pol.guide_action(rxp(guide="11-12 Elwynn (Dwarf/Gnome)", kinds=K_ACCEPT, nactive=1), 11)[:2] == \
        ("load", pol.GUIDE_LOCH)
    assert pol.guide_action(rxp(guide="other", guide_ord=255, kinds=K_ACCEPT), 6)[:2] == ("load", pol.GUIDE_DUN)
    end = rxp(line=0, nactive=0, kinds=0)                                 # past the last step
    assert pol.guide_action(end, 5)[:2] == ("load", pol.GUIDE_DUN)
    # 12-14 Loch Modan goes on in Westfall (flown to: nb/flight.py); Darkshore needs the boat
    assert pol.guide_action(rxp(guide=pol.GUIDE_LOCH, line=0, nactive=0), 14)[:2] == ("load", pol.GUIDE_WEST)
    assert pol.guide_action(rxp(guide="13-15 Westfall", kinds=K_ACCEPT), 14) is None
    assert pol.guide_action(rxp(guide="14-16 Darkshore", kinds=K_ACCEPT), 14)[0] == "grind"
    assert pol.target_name(rxp(text="T:Eldrun Stormbreaker::258098|M:")) == "Eldrun Stormbreaker"
    assert pol.target_name(rxp(text="T:|M:Ragged Young Wolf")) == ""
    assert pol.kind_names(K_ACCEPT | K_TARGET) == "accept|target"
    assert [pol.pick_guide(lv) for lv in (1, 4, 5, 10, 11)] == [pol.GUIDE_COLD] * 2 + [pol.GUIDE_DUN] * 2 + [pol.GUIDE_LOCH]


def test_mode_flag():
    old_env, old_inst, old_file = os.environ.get("NB_RXP"), rf.inst, rf.MODE_FILE
    d = tempfile.mkdtemp()
    try:
        rf.inst = SimpleNamespace(path=lambda n: os.path.join(d, "inst_" + n))
        rf.MODE_FILE = os.path.join(d, "RXP_MODE")
        os.environ.pop("NB_RXP", None)
        assert rf.rxp_mode() is False                                      # default: the old driver
        open(rf.MODE_FILE, "w").write("on\n")
        assert rf.rxp_mode() is True
        open(os.path.join(d, "inst_RXP_MODE"), "w").write("off")
        assert rf.rxp_mode() is False                                      # the per-instance file wins
        os.environ["NB_RXP"] = "1"
        assert rf.rxp_mode() is True                                       # env wins over files
        os.environ["NB_RXP"] = "0"
        open(os.path.join(d, "inst_RXP_MODE"), "w").write("on")
        assert rf.rxp_mode() is False
    finally:
        rf.inst, rf.MODE_FILE = old_inst, old_file
        if old_env is None:
            os.environ.pop("NB_RXP", None)
        else:
            os.environ["NB_RXP"] = old_env


# ------------------------------------------------------------ the follower
def test_absent_and_switch_on():
    reset()
    w = World(None)
    b = make_bot(w)
    assert b.rxp_step() is False and b.grind_ok                           # old addon: grind
    w.st.rxp = rxp(present=False)
    assert b.rxp_step() is False
    w.st.rxp = rxp(flag=False, loaded=False, guide=None, guide_ord=0, line=0, nactive=0)
    assert b.rxp_step() is True and w.chat == ["/be rxp on"]
    assert b.rxp_step() is True and len(w.chat) == 1                      # autoload is still retrying
    CLOCK.sleep(13)
    b.rxp_step(); CLOCK.sleep(13); b.rxp_step()
    assert w.chat == ["/be rxp on"] * 3
    CLOCK.sleep(13)
    assert b.rxp_step() is False and b.grind_ok and len(w.chat) == 3      # 3 tries: grind, retry in 2 min
    CLOCK.sleep(120)
    b.rxp_step()
    assert len(w.chat) == 4


def test_hidden_and_taxi():
    reset()
    w = World(rxp(hidden=True, **STEN))
    b = make_bot(w)
    assert b.rxp_step() is True and w.chat == ["/be rxp on"]
    assert b.rxp_step() is True and len(w.chat) == 1                      # once per 30 s
    w.st.rxp = rxp(taxi=True, **STEN)
    assert b.rxp_step() is True and len(w.chat) == 1 and not w.walks


def sten_world(key="pageup"):
    w = World(rxp(**STEN), nquests=0)

    def target(st):
        st.has_target, st.t_rxp_npc, st.t_npc, st.t_within10 = True, True, 658, True

    def interact(st):
        if not st.t_rxp_npc:
            return
        st.ui |= 2                                                         # the quest detail frame

        def accept(st2):                                                   # RXP accepts 179 and moves on
            st2.ui &= ~2
            st2.quests.append(Quest(179, False, False, (0, 0, 0)))
            st2.nquests = 1
            st2.rxp = rxp(**dict(WOLVES, stepctr=st2.rxp.stepctr + 1, index=2))
        w.at(0.5, accept)
    w.on_key[key] = target
    w.on_key["m"] = interact
    return w


def test_talk_sten():
    reset()
    w = sten_world()
    b = make_bot(w)
    assert b.rxp_step() is True
    assert w.walks and w.walks[0][:2] == (round(-6214.85, 1), 328.2) and w.walks[0][2] == 5.0, w.walks   # talk() tol
    assert w.keys[:2] == ["pageup", "m"], w.keys
    assert w.chat == []                                                    # no chat line on a talk
    assert any("rxp talk Sten Stoutarm: advanced" in m for m in LOGS), LOGS
    assert any(m.startswith("rxp step 01-05 Coldridge Valley L18") for m in LOGS), LOGS
    # next tick: the wolf step -> roam its open points with the wolf wanted
    w.st.plates = [plate(299, rxp_mob=True), plate(705, rxp_mob=False)]
    assert b.rxp_step() is True
    pts, indoor, hunting, wanted = b.roams[-1]
    assert [p[:2] for p in pts] == [(-6100.0, 300.0), (-6120.0, 320.0)], pts          # completed point left out
    assert pts[0][2] == 401.0                                                          # ground_z
    assert hunting and wanted == {299} and not b.grind_ok


def test_npc_key_fallback_is_remembered():
    reset()
    w = sten_world(key="delete")                                           # PAGEUP does not fire on this account
    b = make_bot(w)
    b.rxp_step()
    assert w.keys[:3] == ["pageup", "delete", "m"], w.keys
    assert b._rx_npc_key == "delete"
    assert any("step NPC key delete works" in m for m in LOGS)
    w.keys.clear()
    w.st.rxp = rxp(**dict(STEN, line=30, quests=[(180, Q_ACCEPT, False)]))
    w.st.has_target = w.st.t_rxp_npc = False
    b.rxp_step()
    assert w.keys[0] == "delete", w.keys                                  # the working key first


def test_plate_fallback():
    reset()
    w = World(rxp(**STEN))
    w.st.plates = [plate(658, hostile=False, friendly=True, rxp_npc=True, token=3)]

    def f3(st):
        st.has_target, st.t_rxp_npc, st.t_npc = True, True, 658
    w.on_key["f3"] = f3
    b = make_bot(w)
    assert b._rxp_target_npc(w.st.rxp)
    assert w.keys[:4] == ["pageup", "delete", "pagedown", "f3"], w.keys


def test_window_left_open_is_closed():
    reset()
    w = World(rxp(**dict(STEN, kinds=K_VENDOR | K_TARGET | K_WANDER, quests=[], text="T:Grundel Harkin|M:")))

    def target(st):
        st.has_target, st.t_rxp_npc, st.t_within10 = True, True, True

    def interact(st):
        st.ui |= 4                                                         # the merchant window, nobody closes it
    w.on_key["pageup"], w.on_key["m"] = target, interact
    b = make_bot(w)
    t0 = CLOCK.now
    b.rxp_step()
    assert "esc" in w.keys and w.st.ui == 0, (w.keys, w.st.ui)
    assert any("left open" in m for m in LOGS), LOGS
    assert CLOCK.now - t0 < 25


def test_talk_that_changes_nothing_skips():
    reset()
    w = World(rxp(**STEN))

    def target(st):
        st.has_target, st.t_rxp_npc, st.t_within10 = True, True, True

    def interact(st):
        st.ui |= 1                                                         # gossip opens, RXP has nothing to pick
        w.at(0.4, lambda st2: setattr(st2, "ui", st2.ui & ~1))
    w.on_key["pageup"], w.on_key["m"] = target, interact
    b = make_bot(w)
    for _ in range(pol.TALK_TRIES):
        b.rxp_step()
    assert w.chat == ["/be rxp skip"], (w.chat, LOGS[-5:])
    assert any(m.startswith("rxp skip:") and "changed nothing" in m for m in LOGS), LOGS


def test_nowindow_is_not_a_refusal():
    # inst3 20:28-20:29: 4 talks x 4 presses at Sten opened no window, counted as refusals -> 179 skipped, chain lost.
    # Now: its own count (NOWIN_TRIES_QUEST on an accept step), each miss steps closer, re-walk to NOWIN_TOL yd
    reset()
    w = World(rxp(**STEN))
    w.st.plates = [plate(658, hostile=False, friendly=True, rxp_npc=True, token=3)]

    def target(st):
        st.has_target, st.t_rxp_npc, st.t_within10, st.t_npc = True, True, True, 658
    w.on_key["pageup"] = target                                            # M: nothing at all
    w.walk_res = [("ok", (-6211.0, 328.18))]                               # the tol-5 walk ends 3.9 yd off
    b = make_bot(w)
    closer = []
    b.approach = lambda t=1.5, force=False: closer.append(round(t, 1))
    b.rxp_step()
    assert w.keys.count("m") == 4 and "f3" in w.keys, w.keys               # 4 presses, plate key from the 3rd
    assert closer == [0.8, 1.2, 1.6], closer                               # 0.4 s x the try, before presses 2-4
    assert any("presses [3.9yd/err0" in m for m in LOGS), LOGS
    assert b._rx["npc_tol"] == pol.NOWIN_TOL and b._rx["nowin"] == 1 and b._rx["talks"] == 0
    b.rxp_step()
    assert w.walks[-1][2] == pol.NOWIN_TOL, w.walks                        # re-walked to 2 yd of the spawn
    assert w.chat == [], (w.chat, LOGS[-5:])                               # 2 no-windows: not a skip yet
    assert any("interact dead on this client?" in m for m in LOGS), LOGS
    b.rxp_step()                                                           # the 3rd beside the spawn (NOWIN_NEAR):
    assert w.chat == ["/be rxp skip"], (w.chat, LOGS[-3:])                 # no "closer next"
    # (inst4 00:28-00:31: 8 x 4 presses at 1.4 yd from Mountaineer Gretchen, 171 s)
    assert any(m.startswith("rxp skip:") and "opened no window" in m for m in LOGS), LOGS
    # the escaped accept is remembered for the steps after it (guide_state.json via save_state)
    assert b.rx_lost == {179: {"giver": "Sten Stoutarm", "where": "01-05 Coldridge Valley L18 #1", "tries": 0}}, \
        b.rx_lost
    # a plain vendor step gives up after NOWIN_TRIES
    reset()
    w = World(rxp(**dict(STEN, kinds=K_VENDOR | K_TARGET | K_WANDER, quests=[], text="T:Grundel Harkin|M:")))
    w.on_key["pageup"] = target
    b = make_bot(w)
    for _ in range(pol.NOWIN_TRIES):
        b.rxp_step()
        if w.chat:
            break                                                          # 3 beside the spawn (NOWIN_NEAR) suffice
    assert w.chat == ["/be rxp skip"] and b.rx_lost == {}, (w.chat, b.rx_lost)


def _giver_world(r, accepts=True):
    """Sten at his spawn as the quest DB's giver of 179; M accepts 179 only when BotEye was armed (/be rxp accept)."""
    w = World(r, quests=[], nquests=0)
    sten = (-6214.85, 328.18, 401.0)
    q = SimpleNamespace(starters={179: [658]}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={},
                        go_src={}, skip_gather={}, gathered={}, npc_pos=lambda n, near: sten if n == 658 else None,
                        objective_mobs=lambda qid: {299}, _hunt_pts=lambda qid: [(-6080.0, 290.0, 399.0)])

    def target(st):
        st.has_target, st.t_npc, st.t_rxp_npc, st.t_within10 = True, 658, True, True

    def interact(st):
        if accepts and "/be rxp accept 179" in w.chat and st.quest(179) is None:
            st.ui |= 2
            w.at(0.3, lambda st2: (setattr(st2, "ui", 0), st2.quests.append(Quest(179, False, False, (0, 0, 0))),
                                   setattr(st2, "nquests", 1)))
    w.on_key["pageup"], w.on_key["m"] = target, interact
    b = make_bot(w, qdb=q)
    b.target_npc = lambda npc, tries=3: (target(w.st), True)[1]
    return w, b


def test_skipped_accept_goes_back_to_giver():
    # the 179 complete step after the escaped accept: "not in the log" for 10 s -> to Sten, accept, then hunt
    reset()
    w, b = _giver_world(rxp(**WOLVES))
    b.rx_lost[179] = {"giver": "Sten Stoutarm", "where": "01-05 Coldridge Valley L18 #1", "tries": 0}
    b.rxp_step()
    assert w.chat == [] and b.roams                                        # 10 s of the same read first
    CLOCK.sleep(11)
    b.rxp_step()
    assert w.chat == ["/be rxp accept 179"], w.chat                        # no skip: armed, walked, talked
    assert w.walks[-1][:2] == (round(-6214.85, 1), 328.2), w.walks
    assert w.st.quest(179) is not None and b.rx_lost == {}, b.rx_lost
    assert any("accepted again" in m for m in LOGS), LOGS
    b.rxp_step()
    assert len(b.roams) == 2 and b.rxp_hunting                            # hunting 179's wolves now
    # bot 3 now: no record (the skip ran before this code) -> implied from the quest DB's giver, same walk
    reset()
    w, b = _giver_world(rxp(**WOLVES))
    b.rxp_step()
    CLOCK.sleep(11)
    b.rxp_step()
    assert w.chat == ["/be rxp accept 179"] and w.st.quest(179) is not None, (w.chat, LOGS[-5:])


def test_skipped_accept_gives_up_after_lost_tries():
    reset()
    w, b = _giver_world(rxp(**WOLVES), accepts=False)                     # the giver never offers it
    b.rxp_step()
    CLOCK.sleep(11)
    for _ in range(pol.LOST_TRIES):
        b.rxp_step()
        assert "/be rxp skip" not in w.chat, w.chat
    assert b.rx_lost[179]["tries"] == pol.LOST_TRIES
    CLOCK.sleep(6)
    b.rxp_step()
    assert w.chat == ["/be rxp accept 179", "/be rxp skip"], w.chat       # the old skip, after LOST_TRIES visits
    # the turn-in step (L132 at Sten, turn-in only): same detour, then the step's own skip
    reset()
    r = rxp(kinds=K_TURNIN | K_TARGET, quests=[(179, Q_TURNIN, False)], n_targets=1, text="T:Sten Stoutarm|M:",
            wp=STEN["wp"])
    w, b = _giver_world(r)
    b.rxp_step()
    CLOCK.sleep(11)
    b.rxp_step()
    assert w.chat == ["/be rxp accept 179"] and w.st.quest(179) is not None, w.chat


def test_turnin_with_accepts_arms_the_lost_quest():
    # L132 at Sten: accepts + turn-in 179 (other -> the step talks). 179 not in the log: BotEye takes it this visit
    reset()
    r = rxp(kinds=K_ACCEPT | K_TURNIN | K_TARGET, quests=[(180, Q_ACCEPT, False), (179, Q_TURNIN, False)], n_targets=1,
            text="T:Sten Stoutarm|M:", wp=STEN["wp"])
    w, b = _giver_world(r)
    b.rxp_step()
    assert w.chat == ["/be rxp accept 179"], w.chat
    assert w.st.quest(179) is not None and b.rx_lost == {}, (b.rx_lost, LOGS[-5:])


ADLIN = dict(kinds=K_COLLECT | K_TARGET | K_WANDER, quests=[], n_targets=1, text="T:Adlin Pridedrift|M:",
             wp=(-6226.7, 320.3, None, 0, 1))


def _merchant_world(**buy):
    w = World(rxp(**ADLIN, **buy))

    def target(st):
        st.has_target, st.t_rxp_npc, st.t_within10 = True, True, True

    def interact(st):
        st.ui |= 4                                                         # merchant window, the bot closes it
    w.on_key["pageup"], w.on_key["m"] = target, interact
    return w


def test_vendor_collect_retries_while_affordable():
    # 20:32 bots 1+2: 4 visits to Adlin "changed nothing" -> skip. Affordable: keep visiting up to BUY_TALKS
    reset()
    w = _merchant_world(buy_item=159, buy_seen=True, buy_need=10, buy_price=25)
    b = make_bot(w)
    for _ in range(pol.TALK_TRIES):
        b.rxp_step()
    assert w.chat == [], (w.chat, LOGS[-5:])
    assert any(m.startswith("rxp buy:") and "affordable" in m for m in LOGS), LOGS
    for _ in range(pol.BUY_TALKS - pol.TALK_TRIES):
        b.rxp_step()
    assert w.chat == ["/be rxp skip"], (w.chat, LOGS[-5:])
    assert any(m.startswith("rxp skip:") and "affordable but not bought" in m for m in LOGS), LOGS


def test_vendor_collect_skips_when_it_cannot_buy():
    for buy, why in ((dict(buy_seen=True, buy_short=True, buy_need=6, buy_price=25), "short of money"),
                     (dict(buy_seen=False, buy_need=10), "does not sell"),
                     (dict(buy_seen=True, buy_full=True, buy_need=10), "bags full")):
        reset()
        w = _merchant_world(buy_item=159, **buy)
        b = make_bot(w)
        b.rxp_step()
        assert w.chat == ["/be rxp skip"], (why, w.chat, LOGS[-5:])
        assert any(m.startswith("rxp skip:") and why in m for m in LOGS), (why, LOGS)
    # an old BotEye without blocks 248-249: the TALK_TRIES rule as before
    reset()
    w = _merchant_world()
    b = make_bot(w)
    for _ in range(pol.TALK_TRIES):
        b.rxp_step()
    assert w.chat == ["/be rxp skip"] and any("changed nothing" in m for m in LOGS), LOGS


def test_stale_boteye_is_named():
    # inst3 21:12: client 3 ran the BotEye from before buy_collect (no /reload): blocks 248-250 read 0, 4 visits,
    # skip "changed nothing" with no hint. Now the step start and the skip say which client needs the /reload
    assert pol.stale_boteye(rxp(**ADLIN)) is not None                      # no block-250 stamp
    assert pol.stale_boteye(rxp(**ADLIN, build=pol.BUY_BUILD)) is None
    assert "on disk" in pol.stale_boteye(rxp(**ADLIN, build=pol.BUY_BUILD), disk=pol.BUY_BUILD + 1)
    reset()
    w = _merchant_world()
    b = make_bot(w)
    for _ in range(pol.TALK_TRIES):
        b.rxp_step()
    assert w.chat == ["/be rxp skip"], w.chat
    assert any(m.startswith("rxp: BotEye build 0") and "/reload" in m for m in LOGS), LOGS
    assert any(m.startswith("rxp skip:") and "changed nothing" in m and "/reload" in m for m in LOGS), LOGS
    # an up-to-date BotEye: no warning
    from nb.eye import disk_build
    reset()
    w = _merchant_world(build=disk_build() or pol.BUY_BUILD, buy_item=159, buy_seen=True, buy_need=10, buy_price=5)
    b = make_bot(w)
    b.rxp_step()
    assert not any("/reload" in m for m in LOGS), LOGS


def test_npc_not_found_uses_quest_db_object():
    reset()
    r = rxp(kinds=K_TURNIN, quests=[(419, Q_TURNIN, False)], n_targets=0)
    w = World(r, quests=[Quest(419, True, False, (1, 0, 0))], nquests=1)
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={419: [(2059, (1.0, 2.0, 3.0))]},
                        skip_go=set(), q={}, go_src={}, skip_gather={}, gathered={}, npc_pos=lambda n, near: None)
    b = make_bot(w, qdb=q)
    b.rxp_step()
    assert b.used and b.used[0][0] == 2059, b.used


def test_turnin_checks():
    reset()
    r = rxp(kinds=K_TURNIN | K_TARGET, quests=[(179, Q_TURNIN, False)], n_targets=1, text="T:Sten Stoutarm|M:")
    w = World(r, quests=[], nquests=0)
    b = make_bot(w)
    b.rxp_step()
    assert not w.chat and not w.keys                                       # waits 10 s first (log may be re-read)
    CLOCK.sleep(11)
    b.rxp_step()
    assert w.chat == ["/be rxp skip"], w.chat
    # in the log but not complete: its objectives are hunted with the quest DB's spawns
    reset()
    w = World(r, quests=[Quest(179, False, False, (3, 0, 0))], nquests=1)
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={}, go_src={},
                        skip_gather={}, gathered={}, npc_pos=lambda n, near: None,
                        objective_mobs=lambda qid: {299}, _hunt_pts=lambda qid: [(-6080.0, 290.0, 399.0)])
    b = make_bot(w, qdb=q)
    b.rxp_step()
    pts, _, hunting, wanted = b.roams[-1]
    assert pts == [(-6080.0, 290.0, 399.0)] and 299 in wanted and hunting, b.roams


def test_kill_object_and_hunt_wait():
    reset()
    r = rxp(line=40, kinds=K_COLLECT | K_WANDER, quests=[(3361, 4, False)], pts=[(-5900.0, 400.0, None, 1)])
    w = World(r, quests=[Quest(3361, False, False, (0, 0, 0))], nquests=1)
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(),
                        q={3361: {"title": "A Refugee's Quandary", "items": [3080]}},
                        go_src={3080: [(18, (-5901.0, 401.0, 400.0))]}, skip_gather={}, gathered={},
                        filled=lambda counts, i: False, obj_idx=lambda qid, k, o: 0,
                        npc_pos=lambda n, near: None, objective_mobs=lambda qid: set(), _hunt_pts=lambda qid: [])
    b = make_bot(w, qdb=q)
    b.rxp_step()
    assert b.gathered and b.gathered[0][0] == 18 and b.gathered[0][3] == 3361, b.gathered
    # a mob step: standing at its points with nothing seen for HUNT_WAIT -> skip
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    w.st.x, w.st.y = -6100.0, 300.0
    b = make_bot(w)
    b.rxp_step()
    assert b.roams and not w.chat
    CLOCK.sleep(pol.HUNT_WAIT - 10)
    b.rxp_step()
    assert not w.chat
    CLOCK.sleep(20)
    b.rxp_step()
    assert w.chat == ["/be rxp skip"], w.chat


def test_kill_quest_not_in_log_skips():
    reset()
    w = World(rxp(**WOLVES), quests=[], nquests=0)
    b = make_bot(w)
    b.rxp_step()
    assert b.roams and not w.chat
    CLOCK.sleep(11)
    b.rxp_step()
    assert w.chat == ["/be rxp skip"]


def test_use_item():
    reset()
    r = rxp(line=50, kinds=K_OTHER | K_USE | K_WANDER, wp=(-5907.8, 582.1, None, 0, 1), item=6635, item_btn=1,
            item_ready=True, quests=[], text="T:|M:")
    w = World(r)
    w.st.casting_now = False
    b = make_bot(w)
    b.rxp_step()
    assert w.walks and w.walks[0][:2] == (-5907.8, 582.1)
    assert w.keys.count("insert") == 1 and not w.chat
    b.rxp_step()
    assert w.keys.count("insert") == 1                                    # 8 s before the next press
    CLOCK.sleep(9)
    b.rxp_step()
    assert w.keys.count("insert") == 2
    CLOCK.sleep(9); b.rxp_step()
    assert w.keys.count("insert") == 3
    CLOCK.sleep(9); b.rxp_step()                                          # USE_MAX: now a manual step
    assert w.keys.count("insert") == 3
    assert any("manual step" in m for m in LOGS), LOGS


def test_hs_on_cooldown_skips():
    reset()
    w = World(rxp(kinds=K_HS, item=6948, item_ready=False))
    b = make_bot(w)
    b.rxp_step()
    assert w.chat == ["/be rxp skip"] and "insert" not in w.keys


def test_goto():
    reset()
    w = World(rxp(kinds=K_GOTO, wp=(-6000.0, 400.0, 10, 0, 1)))
    b = make_bot(w)
    b.rxp_step()
    assert w.walks == [(-6000.0, 400.0, 9.0)], w.walks                   # tol = 0.9 x radius (RXP completes inside)
    assert w.walk_kw[0].get("stop") is False and not w.walk_kw[0].get("direct")   # first leg: pathed
    t0 = CLOCK.now
    for _ in range(20):
        b.rxp_step()
        if w.chat:
            break
    assert w.chat == ["/be rxp skip"], w.chat                             # there GOTO_WAIT s, RXP did not complete it
    assert CLOCK.now - t0 < rf.GOTO_WAIT + 2, CLOCK.now - t0


def test_goto_chain_runs_on():
    """Review 2026-10-14: goto steps were walked one by one with a stop and a 1 s sleep at each. A chain is one walk:
    the next leg starts at once (pathed), and the character is not stopped in between."""
    reset()
    a = dict(kinds=K_GOTO, wp=(-6000.0, 400.0, 10, 0, 1))
    w = World(rxp(**a))
    b = make_bot(w)
    # RXP completes the first goto 0.2 s after we enter its radius and shows the next one
    w.walk_res = ["ok"]
    orig = w.walk

    def walk(x, y, z=None, tol=2.0, **kw):
        res = orig(x, y, z, tol, **kw)
        if len(w.walks) == 1:
            w.at(0.2, lambda st: setattr(st, "rxp", rxp(**dict(a, wp=(-5950.0, 420.0, 10, 0, 1), stepctr=2,
                                                                line=19, index=2))))
        return res
    b.travel_to = walk
    stops0 = b.mv.stops
    b.rxp_step()
    assert b.mv.stops == stops0, "stopped between two goto legs"
    b.rxp_step()
    assert w.walks[1] == (-5950.0, 420.0, 9.0), w.walks
    # pathed, not straight: a 'direct' leg between RXP waypoints is no hand-checked trail (review 2026-10-14)
    assert not w.walk_kw[1].get("direct") and w.walk_kw[1].get("stop") is False, w.walk_kw
    # a tick that is not a goto leg stops the run-on first
    b._rx_running = True
    stops0 = b.mv.stops
    w.st.rxp = rxp(**dict(a, kinds=0, wp=None, stepctr=3, line=20, index=3))
    try:
        b.rxp_step()
    except Exception:
        pass
    assert b.mv.stops > stops0, "a run-on into a non-goto step was not stopped"


def test_manual_then_next():
    reset()
    w = World(rxp(kinds=K_MANUAL, text="T:|M:"))
    b = make_bot(w)
    b.rxp_step()
    assert not w.chat
    CLOCK.sleep(pol.MANUAL_WAIT)
    b.rxp_step()
    assert w.chat == ["/be rxp skip"]
    CLOCK.sleep(pol.MANUAL_WAIT)
    b.rxp_step()
    assert w.chat == ["/be rxp skip", "/be rxp next"], w.chat             # the same step again: next


def test_stall_budget():
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w)
    b.rxp_step()
    for _ in range(pol.STALL_MIN["kill"] * 60 // 25 + 2):
        CLOCK.sleep(25)
        w.st.combat = True                                                # keeps the HUNT_WAIT clock out of it
        b.rxp_step()
    assert any("rxp stall" in m for m in LOGS) and w.chat == ["/be rxp skip"], (w.chat, LOGS[-3:])
    # progress (a counter moving) restarts the clock; long gaps (a corpse run) count only STALL_GAP
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1, combat=True)
    b = make_bot(w)
    for i in range(80):
        CLOCK.sleep(25)
        if i % 30 == 0:
            w.st.quests = [Quest(179, False, False, (3 + i // 30, 0, 0))]
        b.rxp_step()
    CLOCK.sleep(3600)
    b.rxp_step()
    assert not w.chat, (w.chat, b._rx["acc"])


def test_policy_actions():
    reset()
    w = World(rxp(kinds=K_ACCEPT | K_TARGET, quests=[(86614, Q_ACCEPT, False)], n_targets=1, text="T:Grund|M:"))
    b = make_bot(w)
    b.rxp_step()
    assert w.chat == ["/be rxp skip"] and "m" not in w.keys
    reset()
    w = World(rxp(guide="05-11 Dun Morogh", guide_ord=2, kinds=K_FLY | K_TARGET, n_targets=1,
                  text="T:Thorgrum Borrelson|M:"))
    b = make_bot(w)
    b.rxp_step()
    assert w.chat == ["/be rxp load 12-14 Loch Modan (Dwarf/Gnome)"], w.chat
    b.rxp_step()
    assert len(w.chat) == 1 and b.grind_ok                                # no second load within 60 s
    reset()
    w = World(rxp(kinds=K_COMPLETE | K_MOB, quests=[(313, Q_COMPLETE, False)], n_mobs=1), level=7)
    b = make_bot(w)
    assert b.rxp_step() is False and b.grind_ok and not w.chat            # LEVEL_GATE: grind instead
    reset()
    w = World(rxp(kinds=K_XP, xp_level=5))
    b = make_bot(w)
    assert b.rxp_step() is False and b.grind_ok


def test_deaths_skip_kill_step():
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w)
    for _ in range(pol.DEATH_SKIP):
        b.rxp_step()
        b.rxp_note_death(w.state())
        b.rxp_note_death(w.state())                                       # the same death read twice counts once
    assert b._rx_deaths[(w.st.rxp.guide, w.st.rxp.line, w.st.rxp.index)] == pol.DEATH_SKIP
    b.rxp_step()
    assert w.chat == ["/be rxp skip"] and any("died 3 times" in m for m in LOGS), LOGS


def test_no_chat_while_window_open():
    reset()
    w = World(rxp(kinds=K_MANUAL), ui=2)
    b = make_bot(w)
    b._rxp_cmd("skip")
    assert w.keys == ["esc", "chat:/be rxp skip"], w.keys


def test_hook_learns_mob_ids():
    reset()
    w = World(rxp(**WOLVES))
    b = make_bot(w)
    st = w.state()
    st.plates = [plate(299, rxp_mob=True), plate(300, rxp_mob=True, friendly=True), plate(301)]
    b._rxp_hook(st)                       # first sight of the counter: nothing learned yet
    assert b.rxp_ids == set()
    CLOCK.sleep(1.0)
    st.t = CLOCK.now
    b._rxp_hook(st)
    assert b.rxp_ids == {299} and 299 in b.wanted
    w.st.rxp = rxp(**dict(WOLVES, tgtctr=2))
    st = w.state()
    st.plates = [plate(299, rxp_mob=True)]                                # stale bits right after a list change
    b._rxp_hook(st)
    assert b.rxp_ids == set() and 299 not in b.wanted
    CLOCK.sleep(0.3); st.t = CLOCK.now; b._rxp_hook(st)
    assert b.rxp_ids == set()                                             # still inside the 0.6 s settle time


def test_name_target_rxp_mode():
    reset()
    w = World(rxp(**WOLVES), level=2)

    def find(st):
        st.has_target, st.t_npc, st.t_rxp_mob, st.t_attackable, st.t_level, st.t_hash = True, 299, True, True, 2, 7
        st.plates = [plate(299, is_target=True)]
    w.on_key["num*"] = find
    b = make_bot(w)
    b.blacklist, b.W = {}, 1920
    assert b.name_target() == "hit"
    assert w.chat == [] and 299 in b.rxp_ids and 299 in b.wanted
    w.st.rxp = rxp(**dict(WOLVES, n_mobs=0))
    assert b.name_target() is None                                        # empty mob list: nothing to press


def test_train_catch_up():
    reset()
    old = rf.trainers.trained_level
    try:
        rf.trainers.trained_level = lambda: 2
        b = make_bot(World(rxp()))
        assert not b.rxp_train_due(SimpleNamespace(level=6))      # 6 is due, but the guide's trainer step may come
        assert not b.rxp_train_due(SimpleNamespace(level=7))
        assert b.rxp_train_due(SimpleNamespace(level=8))           # 6 missed by 2 levels: catch up
        rf.trainers.trained_level = lambda: 8
        assert not b.rxp_train_due(SimpleNamespace(level=9))
    finally:
        rf.trainers.trained_level = old


def test_escapes_capped():
    """Review 2026-10-14: a step RXP never leaves (last step + an open sticky one) gets 1 skip + 3 nexts, then the bot
    grinds and tries once per ESC_PAUSE s instead of a /be rxp next every 5 s."""
    reset()
    w = World(rxp(kinds=K_MANUAL, text="T:|M:"))
    b = make_bot(w)
    for _ in range(40):
        CLOCK.sleep(pol.MANUAL_WAIT)
        b.rxp_step()
    assert w.chat[:pol.ESC_MAX] == ["/be rxp skip"] + ["/be rxp next"] * (pol.ESC_MAX - 1), w.chat
    n = len(w.chat)
    assert n <= pol.ESC_MAX + (40 * pol.MANUAL_WAIT) // pol.ESC_PAUSE + 1, w.chat
    assert b.grind_ok and any("escapes left" in m for m in LOGS), LOGS[-3:]
    CLOCK.sleep(pol.ESC_PAUSE)
    b.rxp_step()
    assert len(w.chat) == n + 1 and w.chat[-1] == "/be rxp next"          # one more try after the pause


def test_hidden_grinds_after_a_while():
    reset()
    w = World(rxp(hidden=True, **STEN))
    b = make_bot(w)
    assert b.rxp_step() is True
    CLOCK.sleep(pol.HIDDEN_GRIND + 1)
    assert b.rxp_step() is False and b.grind_ok and len(w.chat) == 2      # /be rxp on again (30 s) + grind
    w.st.rxp = rxp(**STEN)
    b.rxp_step()
    assert b._rx_hide_since == 0.0


def test_gate_waits_without_skipping():
    reset()
    w = World(rxp(kinds=K_COMPLETE | K_MOB, quests=[(313, Q_COMPLETE, False)], n_mobs=1), level=7)
    b = make_bot(w)
    for _ in range(pol.STALL_MIN["kill"] * 2 + 5):                       # 50+ min of grinding at level 7
        CLOCK.sleep(60)
        assert b.rxp_step() is False
        b.rxp_note_death(w.state()); b._rx_dead = False                    # deaths in the grind are not the step's
    assert not w.chat and not b._rx_deaths, (w.chat, b._rx_deaths)


def test_old_mode_turns_a_left_on_flag_off():
    reset()
    w = World(rxp(**STEN), ui=1)
    b = make_bot(w)
    b.rxp_mode = False
    b._chat_quiet = w.do_chat
    b.rxp_flag_guard(w.state())
    assert not w.chat                                                     # not with a window open
    w.st.ui = 0
    b.rxp_flag_guard(w.state()); b.rxp_flag_guard(w.state())
    assert w.chat == ["/be rxp off"], w.chat                              # once per run
    b2 = make_bot(World(rxp(flag=False, **STEN)))
    b2.rxp_mode = False
    b2._chat_quiet = w.do_chat
    b2.rxp_flag_guard(b2.s())
    assert w.chat == ["/be rxp off"]


# ------------------------------------------------------------ where / how to walk (2026-10-14 "navigation way worse")
TEO = dict(line=63, index=5, kinds=K_TRAIN | K_TARGET, wp=(-6050.3, 383.9, None, 0, 1), n_targets=1,
           text="T:Teo Hammerstorm|M:")
TEO_DB = (-6050.29, 383.95, 398.96)          # trainers.py: tcf_world spawn, Anvilmar top level


class HintMover(FakeMover):
    """ground_z as the bot mesh answers at Teo's xy: with the player's height 392.8 as the hint it returns the floor
    under the platform (392.8), without a hint 399.06 (offline probe, teo_probe.py)."""

    def __init__(self):
        super().__init__()
        self.nm = SimpleNamespace(ground_z=lambda x, y, hint=None: 392.8 if hint is not None and hint < 396 else 399.06)


def teo_world(**kw):
    w = World(rxp(**TEO), x=-6058.5, y=380.8, z=392.8, level=2, **kw)

    def target(st):
        st.has_target, st.t_rxp_npc, st.t_npc, st.t_within10 = True, True, 257446, True
    w.on_key["pageup"] = target
    return w


def test_teo_walks_to_db_spawn_with_old_tolerance():
    """run.out 19:46-19:49: the train step walked to RXP's waypoint at the player's floor height (392.8) and wedged
    18 times under Teo's platform. Now: his DB spawn (z 398.96), train_step's tol 3 / 480 s / travel_abort."""
    reset()
    w = teo_world()
    b = make_bot(w)
    b.mv = HintMover()
    b.rxp_step()
    x, y, tol = w.walks[0]
    assert (x, y) == (round(TEO_DB[0], 1), round(TEO_DB[1], 1)) and tol == 3.0, w.walks
    kw = w.walk_kw[0]
    assert kw["z"] == TEO_DB[2], kw                                       # the DB height, not 392.8
    assert kw["timeout"] == 480 and kw["abort"] == b.travel_abort, kw     # a trainer trip: gray mobs are outrun
    assert w.keys[:2] == ["pageup", "m"], w.keys
    assert any("rxp goal: Teo Hammerstorm" in m and "DB spawn" in m for m in LOGS), LOGS
    # a plain quest NPC: talk()'s tol 5 / 180 s / combat abort
    reset()
    w = sten_world()
    b = make_bot(w)
    b.rxp_step()
    assert w.walks[0][2] == 5.0 and w.walk_kw[0]["timeout"] == 180 and w.walk_kw[0]["abort"] != b.travel_abort


def test_npc_goal_sources():
    """The quest DB's spawn by name (nearest to RXP's waypoint, within NPC_SNAP), else tcf_world by name, else the
    waypoint (a patrol: Kadrell 181 yd off)."""
    reset()
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={}, go_src={},
                        skip_gather={}, gathered={}, npc_pos=lambda n, near: None,
                        names={658: "Sten Stoutarm"}, spawns={658: [(-6216.0, 329.0, 402.5), (-5000.0, 0.0, 300.0)]})
    w = sten_world()
    b = make_bot(w, qdb=q)
    b.rxp_step()
    assert w.walks[0][:2] == (-6216.0, 329.0) and w.walk_kw[0]["z"] == 402.5, (w.walks, w.walk_kw)
    reset()
    w = sten_world()
    b = make_bot(w)
    b._rxp_db_npcs = lambda name: [(-6213.0, 327.0, 403.1)] if name == "Sten Stoutarm" else []
    b.rxp_step()
    assert w.walks[0][:2] == (-6213.0, 327.0) and w.walk_kw[0]["z"] == 403.1
    reset()
    w = sten_world()
    b = make_bot(w)
    b._rxp_db_npcs = lambda name: [(-6000.0, 100.0, 380.0)]              # 300 yd off: a patrol, not this spot
    b.rxp_step()
    assert w.walks[0][:2] == (round(-6214.85, 1), 328.2) and w.walk_kw[0]["z"] == 401.0


# 05-11 Dun Morogh L286 / L295 (run.out 21:42): multi-target steps whose waypoint is the first NPC's (tcf_world:
# Bellowfiz (-5466.53,-632.14), Beldin (-5488.99,-682.33)); the human talked to Stonegear at (-5472.6,-641.0) and to
# Loslor at (-5499.5,-662.1), at their own DB spawns (human/20261014_1959_inst4.jsonl)
STONEGEAR = dict(line=286, index=21, kinds=K_ACCEPT | K_TARGET, wp=(-5466.54, -632.15, None, 0, 1), n_targets=2,
                 quests=[(317, Q_ACCEPT, True), (313, Q_ACCEPT, False)], text="T:Pilot Stonegear|M:")
STONEGEAR_DB = (-5473.19, -641.798, 393.3)


def _kharanos_qdb(others=True):
    sp = {1377: [STONEGEAR_DB]}
    names = {1377: "Pilot Stonegear"}
    if others:
        sp[1378], names[1378] = [(-5466.53, -632.141, 393.939)], "Pilot Bellowfiz"
    return SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={}, go_src={},
                           skip_gather={}, gathered={}, npc_pos=lambda n, near: None, names=names, spawns=sp)


def test_npc_goal_db_agrees_or_moved():
    """The DB spawn when it agrees with RXP's waypoint (< NPC_AGREE yd: Teo, 0 yd); 12 yd off with another NPC's DB
    spawn on the waypoint (a multi-target step) -> still the DB spawn; 12 yd off and nobody else there -> the NPC moved
    in this game version: RXP's xy, height from a DB position within 3 yd, else the floor nearest the old spawn's z."""
    reset()
    w = teo_world()
    b = make_bot(w)
    s = w.state()
    g = b._rxp_npc_goal(s.rxp, s)
    assert g == (TEO_DB[0], TEO_DB[1], TEO_DB[2], "db"), g
    # multi-target step: Bellowfiz's spawn sits on the waypoint -> Stonegear's own DB spawn
    reset()
    w = World(rxp(**STONEGEAR), x=-5520.0, y=-600.0, z=395.0)
    b = make_bot(w, qdb=_kharanos_qdb())
    s = w.state()
    g = b._rxp_npc_goal(s.rxp, s)
    assert g == STONEGEAR_DB + ("db",), g
    assert any("another NPC's spot" in m for m in LOGS), LOGS
    # moved: nobody else on the waypoint -> RXP's xy, on the floor nearest the old spawn's height (not the lowest)
    reset()
    w = World(rxp(**STONEGEAR), x=-5520.0, y=-600.0, z=395.0)
    b = make_bot(w, qdb=_kharanos_qdb(others=False))
    b.mv.floors_at = lambda x, y: [380.2, 393.9, 410.0]
    s = w.state()
    g = b._rxp_npc_goal(s.rxp, s)
    assert g == (-5466.54, -632.15, 393.9, "moved"), g
    assert any("moved NPC" in m and "floor nearest the old spawn's height" in m for m in LOGS), LOGS
    b.rxp_step()
    assert w.walks[0][:2] == (-5466.5, -632.1) and w.walk_kw[0]["z"] == 393.9, (w.walks, w.walk_kw)
    # moved, with a DB object / spawn within 3 yd of RXP's xy: its height
    reset()
    w = World(rxp(**STONEGEAR), x=-5520.0, y=-600.0, z=395.0)
    b = make_bot(w, qdb=_kharanos_qdb(others=False))
    b._db_other_near = lambda x, y, own, rad=None: False           # (the spawn below sits on the wp: no "other NPC")
    b._rxp_spawns = lambda: [economy_spawn(1, 1, -5467.5, -633.0, 394.6, "Crate")]
    s = w.state()
    g = b._rxp_npc_goal(s.rxp, s)
    assert g == (-5466.54, -632.15, 394.6, "moved"), g


def test_npc_goal_follows_the_target_name():
    """L295: the step keeps Beldin's waypoint when its .target turns to Loslor Rudge; the goal is cached per (wp,
    name), so Loslor gets his own DB spawn (20 yd off, Beldin's spawn on the wp), not Beldin's (run.out 21:42:30-
    21:43:26: 8 presses 1.3-3.7 yd from Beldin's spot, err 12)."""
    reset()
    wp = (-5488.94, -682.23, None, 0, 1)
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={}, go_src={},
                        skip_gather={}, gathered={}, npc_pos=lambda n, near: None,
                        names={1376: "Beldin Steelgrill", 1694: "Loslor Rudge"},
                        spawns={1376: [(-5488.99, -682.33, 394.436)], 1694: [(-5499.7, -664.575, 395.395)]})
    w = World(rxp(line=295, kinds=K_ACCEPT | K_TURNIN | K_TARGET, wp=wp, n_targets=2, text="T:Beldin Steelgrill|M:",
                  quests=[(400, Q_TURNIN, False), (5541, Q_ACCEPT, False)]), x=-5480.0, y=-670.0, z=394.0)
    b = make_bot(w, qdb=q)
    s = w.state()
    assert b._rxp_npc_goal(s.rxp, s)[:2] == (-5488.99, -682.33)
    b._rx["npc_tol"] = pol.NOWIN_TOL
    w.st.rxp = dataclasses.replace(w.st.rxp, text="T:Loslor Rudge|M:", quests=[(400, Q_TURNIN, True), (5541, Q_ACCEPT, False)])
    s = w.state()
    g = b._rxp_npc_goal(s.rxp, s)
    assert g == (-5499.7, -664.575, 395.395, "db"), g
    assert "npc_tol" not in b._rx                                         # Beldin's no-window tolerance is not his


def test_err12_approaches_the_targeted_unit():
    """A press answered err 12 (too far) at the goal: the targeted unit is elsewhere. The next press first re-targets
    by its plate key and drives at the live unit's plate even within 2.5 yd of the goal; a whole talk of err 12
    gets one more round at the live unit before the no-window counts."""
    reset()
    w = World(rxp(**STEN))
    w.st.plates = [plate(658, hostile=False, friendly=True, rxp_npc=True, token=3)]
    near = {"v": False}

    def target(st):
        st.has_target, st.t_rxp_npc, st.t_within10, st.t_npc = True, True, True, 658

    def interact(st):
        if not near["v"]:
            st.err, st.errn = 12, st.errn + 1                                 # too far
            return
        st.ui |= 2

        def accept(st2):
            st2.ui &= ~2
            st2.quests.append(Quest(179, False, False, (0, 0, 0)))
            st2.nquests = 1
            st2.rxp = rxp(**dict(WOLVES, stepctr=st2.rxp.stepctr + 1, index=2))
        w.at(0.5, accept)
    w.on_key["pageup"], w.on_key["f3"], w.on_key["m"] = target, target, interact
    b = make_bot(w)
    closer = []

    def approach(t=1.5, force=False):
        closer.append(round(t, 1))
        near["v"] = True
    b.approach = approach
    b.rxp_step()                                                          # the walk ends on the goal (0 yd)
    assert closer == [2.3], closer                                        # despite < 2.5 yd from the goal
    assert w.keys.count("m") == 2 and w.keys.index("f3") < len(w.keys) - 1 - w.keys[::-1].index("m"), w.keys
    assert any("rxp talk Sten Stoutarm: advanced after 2" in m for m in LOGS), LOGS
    assert any("err 12 (too far)" in m and "approaching the targeted unit" in m for m in LOGS), LOGS
    # err 12 on every press: one more round at the live unit before the no-window is counted
    reset()
    w = World(rxp(**STEN))
    w.st.plates = [plate(658, hostile=False, friendly=True, rxp_npc=True, token=3)]

    def far(st):
        st.err, st.errn = 12, st.errn + 1
    w.on_key["pageup"], w.on_key["f3"], w.on_key["m"] = target, target, far
    b = make_bot(w)
    closer = []
    b.approach = lambda t=1.5, force=False: closer.append(round(t, 1))
    b.rxp_step()
    assert w.keys.count("m") == 8, w.keys
    assert 3.0 in closer and any("walking to the targeted unit, once more" in m for m in LOGS), (closer, LOGS)
    assert b._rx["nowin"] == 1, b._rx
    b.rxp_step()                                                          # once per step, not per talk
    assert w.keys.count("m") == 12 and b._rx["nowin"] == 2, (w.keys.count("m"), b._rx)


def test_failed_walk_tries_near_then_floor_then_shelves():
    """A stuck walk 4 yd short still targets + interacts (talk()); far off: next tick the goal's other floor, then
    the step is shelved (grind, no stall time) instead of re-walked at once, and only the 3rd failure skips it."""
    reset()
    w = teo_world()
    b = make_bot(w)
    b.mv = HintMover()
    w.walk_res = [("stuck", (-6053.0, 381.0))]                             # 3.7 yd from Teo
    b.rxp_step()
    assert w.keys[:2] == ["pageup", "m"], w.keys
    assert b._rx["walk_fail"] == 0 and any("trying the NPC from here" in m for m in LOGS), LOGS
    # far off: 1st failure -> the other floor at the goal xy, at once
    reset()
    w = teo_world()
    w.on_key.clear()
    b = make_bot(w)
    b.mv = HintMover()
    b.mv.floors_at = lambda x, y: [423.8, 399.4]
    w.walk_res = [("stuck", (-6080.0, 380.0)), ("stuck", (-6080.0, 380.0)), ("stuck", (-6080.0, 380.0))]
    # Teo's goal is his DB spawn: its height IS his floor, so no retry on another one (review 2026-10-14: the
    # "other floor" of a DB goal is the 392.8 under his platform or the 423.8 roof); the 1st failure shelves
    assert b.rxp_step() is True
    assert b._rx["walk_fail"] == 1 and b._rx.get("shelf", 0) > CLOCK.now, b._rx
    n = len(w.walks)
    for _ in range(5):
        CLOCK.sleep(30)
        assert b.rxp_step() is False and b.grind_ok                       # grinding meanwhile, no re-walk
    assert len(w.walks) == n and not w.chat
    assert b._rx["acc"] < 5.0, b._rx["acc"]                                # shelved time is not stall time
    CLOCK.sleep(rf.SHELVE["stuck"])
    b.rxp_step()
    assert w.walk_kw[1]["z"] == 398.96, w.walk_kw                         # the same DB floor again
    assert b._rx["walk_fail"] == 2 and b._rx.get("shelf", 0) > CLOCK.now   # 2nd failure: shelved again
    CLOCK.sleep(rf.SHELVE["stuck"])
    b.rxp_step()
    assert len(w.walks) == n + 2 and w.chat == ["/be rxp skip"], (w.walks, w.chat)   # 3rd failure: skip
    # a guessed height (RXP waypoint, no DB spawn near) does get the other floor on its 1st failure
    reset()
    w = teo_world()
    w.on_key.clear()
    b = make_bot(w)
    b.mv = HintMover()
    b.mv.floors_at = lambda x, y: [423.8, 399.4]
    b._rxp_npc_cands = lambda name: [[], [], []]
    w.walk_res = [("stuck", (-6080.0, 380.0)), ("stuck", (-6080.0, 380.0))]
    b.rxp_step()
    assert b._rx["walk_fail"] == 1 and not b._rx.get("shelf"), b._rx
    z0 = w.walk_kw[0]["z"]
    b.rxp_step()
    assert w.walk_kw[1]["z"] != z0 and w.walk_kw[1]["z"] in (423.8, 399.4), w.walk_kw


def test_offmesh_wp_is_snapped():
    """An RXP point no floor polygon contains (inside a wall / rock): the nearest polygon centre within 8 yd."""
    reset()
    w = World(rxp(kinds=K_MANUAL, wp=(-5700.0, 300.0, None, 0, 1)), x=-5800.0, y=300.0, z=400.0)
    b = make_bot(w)
    b.mv.floors_at = lambda x, y: []
    calls = []

    def near(start, target, radius=12.0, tries=6, dz=6.0, **kw):
        calls.append((target, radius, tries))
        return (target[0] + 2.5, target[1] - 1.0, 404.5)
    b.mv.nm.reachable_near = near
    b.rxp_step()
    assert w.walks[0][:2] == (-5697.5, 299.0) and w.walk_kw[0]["z"] == 404.5, (w.walks, w.walk_kw)
    assert calls and calls[0][1] == 8.0 and any("off the mesh" in m for m in LOGS)


def test_point_height_lowest_floor_on_our_island():
    """No DB spawn beside the point: never the player's height; the lowest floor at the xy on our own mesh island
    (a roof island / the cellar of another island loses)."""
    reset()
    w = World(rxp(kinds=K_MANUAL, wp=(-5700.0, 300.0, None, 0, 1)), x=-5800.0, y=300.0, z=430.0)
    b = make_bot(w)
    b.mv = HintMover()
    b.mv.floors_at = lambda x, y: [440.0, 430.0, 401.5]
    isl = {401.5: 2, 430.0: 1, 440.0: 1}
    b.mv.nm.nearest_poly = lambda x, y, z: ((x, y, z), None)
    b.mv.nm.components = lambda: SimpleNamespace(get=lambda node: 1 if node[0] == -5800.0 else isl.get(node[2]))
    b.mv.nm.ground_z = lambda x, y, hint=None: 401.5 if hint is None else 430.0
    b.rxp_step()
    assert w.walk_kw[0]["z"] == 430.0, w.walk_kw
    # a DB spawn within 3 yd gives the height outright
    reset()
    w = World(rxp(kinds=K_MANUAL, wp=(-5700.0, 300.0, None, 0, 1)), x=-5800.0, y=300.0, z=430.0)
    b = make_bot(w)
    b._rxp_spawns = lambda: [economy_spawn(1, 1, -5701.0, 301.0, 412.25, "Rock Elemental")]
    b.rxp_step()
    assert w.walk_kw[0]["z"] == 412.25, w.walk_kw


def economy_spawn(guid, entry, x, y, z, name, lo=2, hi=3):
    from nb.economy import Spawn
    return Spawn(guid, entry, x, y, z, lo, hi, 0, 38, name, 300)


def test_kill_step_hunts_db_spawns():
    """Kill steps roam the DB spawns of their mobs (height, guid for respawn waits) round RXP's points, not RXP's
    few hand-placed points (05-11 L686: one Crag Boar point 115 yd from any boar)."""
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w)
    sp = [economy_spawn(11, 299, -6105.0, 310.0, 398.5, "Ragged Young Wolf"),
          economy_spawn(12, 299, -6130.0, 335.0, 399.25, "Ragged Young Wolf"),
          economy_spawn(15, 299, -6120.0, 320.0, 399.0, "Ragged Young Wolf"),
          economy_spawn(13, 299, -6400.0, 300.0, 380.0, "Ragged Young Wolf"),     # 300 yd from RXP's points
          economy_spawn(14, 705, -6101.0, 301.0, 398.0, "Rockjaw Trogg")]          # not the step's mob
    b._rxp_spawns = lambda: sp
    b.rxp_step()
    pts, _, hunting, _ = b.roams[-1]
    assert sorted(p[3] for p in pts) == [11, 12, 15], pts
    assert {p[:3] for p in pts} == {(-6105.0, 310.0, 398.5), (-6130.0, 335.0, 399.25), (-6120.0, 320.0, 399.0)}
    assert hunting and any("DB spawns round RXP's points" in m for m in LOGS), LOGS
    # 1-2 spawns round RXP's points (317's one bear, inst3/inst4 camped it 8-14 min): the ring widens to 2 x HUNT_AREA
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w)
    sp2 = [sp[0], economy_spawn(16, 299, -6300.0, 320.0, 390.0, "Ragged Young Wolf"), sp[4]]
    b._rxp_spawns = lambda: sp2
    b.rxp_step()
    pts, _, _, _ = b.roams[-1]
    assert sorted(p[3] for p in pts) == [11, 16], pts
    assert any("widened" in m for m in LOGS), LOGS
    # quest DB objective spawns count too; nothing within HUNT_AREA of RXP's points -> all within HUNT_NEAR of us
    reset()
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={}, go_src={},
                        skip_gather={}, gathered={}, npc_pos=lambda n, near: None,
                        objective_mobs=lambda qid: {299}, _hunt_pts=lambda qid: [(-6400.0, 600.0, 381.0)])
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w, qdb=q)
    b.rxp_step()
    pts, _, _, wanted = b.roams[-1]
    assert pts == [(-6400.0, 600.0, 381.0)] and 299 in wanted, (pts, wanted)
    # no spawn known at all: RXP's points (heights without the player's hint)
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w)
    b.mv = HintMover()
    b.rxp_step()
    pts = b.roams[-1][0]
    assert [p[:2] for p in pts] == [(-6100.0, 300.0), (-6120.0, 320.0)] and all(p[2] == 399.06 for p in pts), pts
    # same-named spawns only far away (another zone, > HUNT_NEAR from us): RXP's points, not a cross-continent trip
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w)
    b.mv = HintMover()
    b._rxp_spawns = lambda: [economy_spawn(21, 299, -4000.0, -2000.0, 300.0, "Ragged Young Wolf")]
    b.rxp_step()
    pts = b.roams[-1][0]
    assert [p[:2] for p in pts] == [(-6100.0, 300.0), (-6120.0, 320.0)], pts


def test_use_step_walks_the_guide_route():
    """use_item_at's routing: the guide's earlier waypoints (since the last QA / QT) as legs, first pathed, the rest
    straight (walk_via), then the spot (the Spirit Stone trail, 13:32 / 14:14)."""
    reset()
    st = lambda line, actions, gotos: SimpleNamespace(line_no=line, actions=actions, gotos=gotos)
    steps = [st(570, [("QA", "94374")], [("1426/0", 383.9, -6050.3, None)]),
             st(575, [("GO", "x")], [("1426/0", 491.0, -6122.0, None), ("1426/0", 498.0, -6118.0, None)]),
             st(580, [("GO", "x")], [("1426/0", 496.0, -6091.0, None)]),
             st(583, [("CAST", "8202"), ("USE", "6635")], [("1426/0", 582.1, -5907.8, None)])]
    r = rxp(line=50, kinds=K_OTHER | K_USE | K_WANDER, wp=(-5907.8, 582.1, None, 0, 1), item=6635, item_btn=1,
            item_ready=True, quests=[], text="T:|M:")
    w = World(r, x=-6125.0, y=470.0)
    b = make_bot(w)
    b._rxp_guide_steps = lambda: {"1-5 Coldridge Valley": steps}
    legs = []

    def goto(x, y, z=None, tol=2.0, timeout=120.0, abort=None, direct=False, **kw):
        legs.append(((x, y), direct))
        w.st.x, w.st.y = x, y
        return "ok"
    b.mv.goto = goto
    b.rxp_step()
    assert legs == [((-6122.0, 491.0), False), ((-6118.0, 498.0), True), ((-6091.0, 496.0), True)], legs
    assert w.walks[0][:2] == (-5907.8, 582.1) and w.walk_kw[0].get("direct") is True, (w.walks, w.walk_kw)
    assert w.keys.count("insert") == 1
    assert rf.use_route(steps, 6635, -5000.0, 0.0) == []                  # another spot: no route


def test_teo_on_the_real_mesh():
    """Offline regression on the bot mesh (skipped when it cannot be loaded): Teo's goal height is the DB's, the
    height an RXP point gets without the DB is the reachable floor (not 392.8), and the path from where run.out
    wedged climbs the stairs through (-6052.1,378.9,399.1) and ends within 2 yd of his height."""
    try:
        import navmesh
        from nb import move
        nm = navmesh.NavMesh(navmesh.BOT_MMAP_DIR, 0)
    except Exception as e:
        print("  (skipped: no bot mesh:", repr(e), ")")
        return
    reset()
    w = teo_world()
    b = make_bot(w)
    mv = move.Mover.__new__(move.Mover)
    mv.nm, mv.zones = nm, []
    mv.stop = lambda *a, **k: None
    b.mv = mv
    s = w.state()
    assert nm.ground_z(TEO["wp"][0], TEO["wp"][1], hint=392.8) < 394                # the old answer: wrong floor
    g = b._rxp_npc_goal(s.rxp, s)
    assert g[3] == "db" and g[2] == TEO_DB[2], g
    b._db_z = lambda x, y: None
    b._rx.pop("pt", None)
    x, y, z = b._rxp_point(TEO["wp"][0], TEO["wp"][1], s)
    assert 398.0 < z < 400.5, z
    p = nm.find_path((s.x, s.y, s.z), (g[0], g[1], g[2]), dangers=(), budget=10)
    assert p and abs(float(p[-1][2]) - TEO_DB[2]) < 2.0, p
    assert any(math.dist((float(q[0]), float(q[1]), float(q[2])), (-6052.1, 378.9, 399.1)) < 1.5 for q in p), p


def test_bad_spot_promotion_rule():
    """The same goal walked twice within BAD_REPEAT_S is one mistake (the two RXP walks to Teo's xy made
    (-6055.3,382.9) a hole); different goals, or 10+ min apart, are two witnesses; old ids keep the old rule."""
    from nb import move
    assert not move.spot_active({"eps": ["1-1791999992.1@-6050,384,393", "1-1792000075.5@-6050,384,393"]})
    assert move.spot_active({"eps": ["1-1791999992.1@-6050,384,393", "1-1792000992.1@-6050,384,393"]})
    assert move.spot_active({"eps": ["1-1791999992.1@-6050,384,393", "2-1791999999.0@-6100,300"]})
    assert move.spot_active({"eps": ["log1-14:11:57--6055.2,388.7", "log1-22:35:15--6055.2,388.7"]})
    assert not move.spot_active({"eps": ["log1-14:11:57--6055.2,388.7"]})


# ------------------------------------------------------------ emote / wait / visit steps (2026-10-14 bot 1, 96608)
SIT_OBJS = [(14, 227698, "Sit near the campfire"), (14, 227701, "Gain the Boosted Rest buff")]
SIT_TEXT = '"/sit" in chat and wait for one minute around the campfire'
SIT = dict(guide="05-11 Dun Morogh", line=49, index=5, kinds=K_COMPLETE, wp=(-5648.4, -498.4, None, 0, 1),
           quests=[(96608, Q_COMPLETE, False), (96608, Q_COMPLETE, False)], pts=[(-5648.4, -498.4, None, 1)])


def sit_steps():
    return {"5-11 Dun Morogh": [
        SimpleNamespace(actions=[("QT", "96628"), ("QA", "96608")], gotos=[("1426/0", -498.4, -5648.4, None)],
                        text="Talk to Brighthammer"),
        SimpleNamespace(actions=[("QC", "96608,1"), ("QC", "96608,2")], gotos=[("1426/0", -498.4, -5648.4, None)],
                        text=SIT_TEXT)]}


def sit_world(counts=(0, 0, 0), at_fire=True):
    w = World(rxp(**SIT), quests=[Quest(96608, False, False, counts)], nquests=1, level=6)
    if at_fire:
        w.st.x, w.st.y = -5648.0, -498.0
    else:
        w.st.x, w.st.y = -5600.0, -540.0
    b = make_bot(w)
    b._rxp_qobjs = lambda qid: SIT_OBJS if qid == 96608 else []
    b._rxp_guide_steps = sit_steps
    return w, b


def test_special_step_policy():
    r = rxp(**SIT)
    lq = {96608: (False, (0, 0, 0))}
    assert pol.special_step(r, {96608: SIT_OBJS}, lq, SIT_TEXT) == ("emote", "SIT", ())
    assert pol.special_step(r, {96608: SIT_OBJS}, lq, "") == ("emote", "SIT", ())       # the objective's verb
    assert pol.special_step(r, {96608: SIT_OBJS}, {96608: (False, (255, 0, 0))}, "") == ("wait", "", ())
    assert pol.special_step(r, {}, lq, SIT_TEXT) == ("emote", "SIT", ())                # DB down: the step text
    assert pol.special_step(r, {}, lq, "") is None
    assert pol.special_step(r, {96608: SIT_OBJS}, {}, SIT_TEXT) is None                 # not in the log: kill's checks
    assert pol.special_step(r, {96608: SIT_OBJS}, {96608: (True, (255, 255, 0))}, SIT_TEXT) is None
    # a kill objective left -> hunt as before (wolves 179 have a type-0 row)
    wolves = rxp(**dict(WOLVES, n_mobs=0, kinds=K_COMPLETE))
    assert pol.special_step(wolves, {179: [(0, 705, "")]}, {179: (False, (2, 0, 0))}) is None
    assert pol.special_step(rxp(**WOLVES), {179: [(14, 1, "Sit")]}, {179: (False, (0, 0, 0))}) is None   # .mob list
    # 98319 "Mountaineer Cornelius found": areatrigger enter
    corn = rxp(guide="05-11 Dun Morogh", kinds=K_COMPLETE | K_GOTO | K_WANDER, quests=[(98319, Q_COMPLETE, False)])
    assert pol.special_step(corn, {98319: [(19, 44117, "Mountaineer Cornelius found")]},
                            {98319: (False, (0, 0, 0))}, "Travel to the corpse of Cornelius") == ("visit", "", (44117,))
    assert pol.emote_token("Talk to |Tinterface/worldmap/chatbubble_64grey.blp:20|t /target Sten") == ""
    assert pol.emote_token('Type "/dance" now') == "DANCE" and pol.emote_token("", "Kneel before the king") == "KNEEL"


def test_sit_step_sits_emotes_and_waits():
    """05-11 L49: at the fire -> X (sit: the server's campfire rest), /be emote sit (the counted text emote), then
    stand still: sit counted at once, Boosted Rest after the 60 s rest, RXP moves on. No hunt, no skip."""
    reset()
    w, b = sit_world()

    def sat(st):
        w.at(1.5, lambda st: setattr(st, "quests", [Quest(96608, False, False, (255, 0, 0))]))
        w.at(61.0, lambda st: (setattr(st, "quests", [Quest(96608, True, False, (255, 255, 0))]),
                               setattr(st, "rxp", rxp(guide="05-11 Dun Morogh", line=54, index=6,
                                                      kinds=K_TURNIN | K_ACCEPT | K_TARGET, n_targets=1,
                                                      quests=[(96608, pol.Q_TURNIN, False), (96629, Q_ACCEPT, False)],
                                                      text="T:Eric Brighthammer|M:", stepctr=2))))
    w.on_key["x"] = sat
    t0 = CLOCK.now
    assert b.rxp_step() is True
    assert w.keys.count("x") == 1 and w.chat == ["/be emote sit"], (w.keys, w.chat)
    assert not b.roams and not b.rxp_hunting
    assert 60 < CLOCK.now - t0 < pol.SPECIAL_WAIT, CLOCK.now - t0
    assert any("rxp emote SIT: step done" in m for m in LOGS), LOGS
    assert not any("/be rxp" in c for c in w.chat)


def test_sit_step_walks_first_and_resits_after_a_fight():
    reset()
    w, b = sit_world(at_fire=False)
    w.on_key["x"] = lambda st: w.at(10.0, lambda st: setattr(st, "combat", True))
    assert b.rxp_step() is True                                           # walk, sit at the fire, attacked at 10 s
    # next to Eric's Basic Campfire itself (tcf gameobject type 8 at -5645.0,-496.7), not RXP's point 3.8 yd off
    assert w.walks and w.walks[0][:2] == (-5645.0, -496.7) and w.walks[0][2] == 1.5, w.walks
    assert w.keys.count("x") == 1 and any("attacked" in m for m in LOGS), LOGS
    w.st.combat = False
    w.on_key["x"] = None
    b.rxp_step()                                                          # after the fight: sit again (standing)
    assert w.keys.count("x") == 2 and w.chat.count("/be emote sit") >= 3, (w.keys, w.chat)


def test_sit_step_gives_up_after_tries():
    reset()
    w, b = sit_world()
    for i in range(pol.SPECIAL_TRIES):
        assert b.rxp_step() is True
        assert not any("/be rxp" in c for c in w.chat), w.chat
    assert w.keys.count("x") == 1                                         # still sitting: X would stand up
    assert w.chat.count("/be emote sit") == 2 * pol.SPECIAL_TRIES         # one + one retry 4 s later per try
    b.rxp_step()
    assert w.chat[-1] == "/be rxp skip", w.chat


def test_kill_step_without_rows_still_hunts():
    """No quest_objectives rows and no guide text (DB down, or a plain kill step): the old hunt, unchanged."""
    reset()
    w = World(rxp(**WOLVES), quests=[Quest(179, False, False, (2, 0, 0))], nquests=1)
    b = make_bot(w)
    b.rxp_step()
    assert b.roams and not w.keys


def test_visit_step_walks_into_the_trigger():
    reset()
    corn = rxp(guide="05-11 Dun Morogh", line=60, kinds=K_COMPLETE | K_GOTO | K_WANDER,
               quests=[(98319, Q_COMPLETE, False)], wp=(-5516.3, -221.8, 20, 0, 1),
               pts=[(-5516.3, -221.8, 20, 0), (-5506.3, -312.5, None, 1)])
    w = World(corn, quests=[Quest(98319, False, False, (0, 0, 0))], nquests=1, level=8)
    w.st.x, w.st.y = -5600.0, -400.0
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(), q={}, go_src={},
                        skip_gather={}, gathered={}, npc_pos=lambda n, near: None, objective_mobs=lambda qid: set(),
                        _hunt_pts=lambda qid: [], trigger_pos={44117: ((-5506.33, -317.14, 352.31), 8.0)})
    b = make_bot(w, qdb=q)
    b._rxp_qobjs = lambda qid: [(19, 44117, "Mountaineer Cornelius found")] if qid == 98319 else []
    b.rxp_step()
    assert w.walks[-1] == (-5506.3, -317.1, 4.0) and w.walk_kw[-1]["z"] == 352.31, (w.walks, w.walk_kw)
    for _ in range(int(pol.VISIT_WAIT) + 2):
        b.rxp_step()
    assert any("hunting the step's points instead" in m for m in LOGS), LOGS
    b.rxp_step()
    assert b.roams, "no completion inside the trigger: back to the hunt of the step's points"


def test_camp_aura_bits():
    from nb.eye import State
    import inspect
    names = [p for p in inspect.signature(State).parameters]
    st = State(**{n: 0 for n in names})
    st.auras = 32 | 2 | (120 << 8)
    assert st.campfire_rest and not st.camp_buff and st.weapon_buff and st.weapon_secs == 120
    st.auras = 64
    assert st.camp_buff and not st.campfire_rest and st.weapon_secs == 0


def test_old_mode_untouched():
    """Without RXP mode the class defaults keep the old paths: no RXP state, roam searches only with wanted mobs."""
    b = Bot.__new__(Bot)
    assert b.rxp_mode is False and b.rxp_hunting is False
    assert botmod.Bot.quest_step is not None and not hasattr(b, "_rx")


def test_given_up_chain_is_skipped_at_once():
    # 98319's kill step skipped -> its turn-in + the accept of 98323 (prev 98319) are left without a talk
    reset()
    q = SimpleNamespace(starters={}, enders={}, go_starters={}, go_enders={}, skip_go=set(),
                        q={98323: {"prev": 98319}, 98324: {"prev": 98323}}, go_src={}, skip_gather={}, gathered={},
                        npc_pos=lambda n, near: None)
    w = World(rxp(line=300, kinds=K_COMPLETE | K_MOB, quests=[(98319, Q_COMPLETE, False)], text="T:|M:Wendigo"),
              quests=[Quest(98319, False, False, (0, 0, 0))], nquests=1)
    b = make_bot(w, qdb=q)
    b._rxp_escape(w.st.rxp, "died 3 times on this step")
    assert b._rx_gaveup == {98319}, b._rx_gaveup
    w.chat.clear()
    w.st.rxp = rxp(line=310, index=2, kinds=K_ACCEPT | K_TURNIN, quests=[(98319, Q_TURNIN, False),
                                                                      (98323, Q_ACCEPT, False)],
                   text="T:Mountaineer Gretchen|M:")
    CLOCK.sleep(6)
    b.rxp_step()
    assert w.chat == ["/be rxp skip"] and "pageup" not in w.keys, (w.chat, w.keys, LOGS[-3:])
    assert any("quest chain given up" in m for m in LOGS), LOGS[-3:]
    # a grand-child too, and a quest that has nothing to do with the chain is not touched
    assert b._rx_quest_dead(98324, Q_ACCEPT, w.state()) and not b._rx_quest_dead(500, Q_ACCEPT, w.state())
    # done after all (the mobs died anyway): its hand-in works again
    w.st.quests = [Quest(98319, True, False, (0, 0, 0))]
    assert b._rx_quest_dead(98319, Q_TURNIN, w.state()) is None


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
