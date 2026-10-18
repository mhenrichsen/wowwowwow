"""Decode the BotEye addon's pixel strip (layout must match BotEye.lua)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from .win import Capture, find_window

BLOCK, PER_ROW, NBLOCKS = 3, 64, 256    # 4 full rows (was 192): row 4 = RestedXP state (nb/RXP_PLAN.md section 2)
BAR_PX, NBARS = 128, 16          # 15: the pet's health (BotEye build 29+)
MARKER = 0xA55A0F
# block 145: the addon paints this when it reports 20 quests (BotEye.lua QUEST20_TAG). An older addon leaves 145
# black and blocks 160+ unpainted (game pixels), so quests 11-20 are read only with the tag
QUEST20_TAG = 0x5A0F14
NQUESTS = 20
# block 192: BotEye.lua RXP_TAG. With it the strip has 4 rows and the bars start at y = 12; without it (an addon from
# before row 4) the bars start at y = 9 where row 4 would be, and state.rxp is None. So both addon versions decode.
RXP_TAG = 0x5A0F16
RXP_BLOCK0 = 192
# block 250: BUILD_TAG * 65536 + BotEye.lua BOTEYE_BUILD. Compared with the file on disk (disk_build) to tell a client
# that still runs an older BotEye (not /reloaded: inst3 21:12, blocks 248-249 read 0 and Adlin's water was skipped)
BUILD_TAG = 0xBE
BOTEYE_LUA = r"C:\Program Files (x86)\World of Warcraft\_classic_beta_\Interface\AddOns\BotEye\BotEye.lua"
# block 194 ordinal -> guide name (1-based, BotEye.lua RXP_GUIDES; 0 = no guide, 255 = another guide or group)
RXP_GROUP = "RestedXP Forever Guide (A)"
RXP_GUIDES = ("01-05 Coldridge Valley", "05-11 Dun Morogh", "11-12 Elwynn (Dwarf/Gnome)",
              "12-14 Loch Modan (Dwarf/Gnome)", "13-15 Westfall", "14-16 Darkshore", "16-19 Darkshore",
              "19-20 Redridge", "19-21 Darkshore/Ashenvale", "20-21 Darkshore/Ashenvale", "11-13 Loch Modan (Hunter)",
              "01-06 Northshire", "06-11 Elwynn Forest", "11-13 Loch Modan")
# kind-mask bits of blocks 196 / 197 (BotEye.lua RXP_KIND, rxp_kinds)
K_ACCEPT, K_TURNIN, K_COMPLETE, K_COLLECT, K_GOTO, K_WANDER, K_MOB, K_TARGET = (1 << b for b in range(8))
K_USE, K_TRAIN, K_TRAINER, K_VENDOR, K_HOME, K_FP, K_FLY, K_HS = (1 << b for b in range(8, 16))
K_XP, K_DEATHSKIP, K_BUY, K_MANUAL, K_GOSSIP, K_UNITSCAN, K_OTHER = (1 << b for b in range(16, 23))
# quest element tags of blocks 201-204
Q_ACCEPT, Q_TURNIN, Q_COMPLETE, Q_COLLECT = 1, 2, 3, 4


def quest_base(k: int) -> int:
    """First of the 2 blocks of quest slot k (0-based): 80.. for slots 0-9, 146.. for 10-19."""
    return 80 + 2 * k if k < 10 else 146 + 2 * (k - 10)


def _coord(v: int) -> float:
    return v / 256.0 - 20000.0


@dataclass
class Plate:
    sx: int; sy: int; npc: int; level: int; hp: int
    hostile: bool; dead: bool; on_me: bool; is_target: bool; tapped: bool
    combat: bool; player: bool; elite: bool; friendly: bool; token: int; ghash: int
    trivial: bool = False; threat: bool = False      # UnitIsTrivial (gray); on its threat list
    # RXP name bits (BotEye fill_plates: 1 + 2*mob + 4*target + 8*unitscan): in RXP's mob list, in the current
    # step's .target list (the NPC to talk to), in the unitscan list. Matched by name, cached by NPC ID in combat
    rxp_mob: bool = False; rxp_npc: bool = False; rxp_scan: bool = False
    on_pet: bool = False                             # its target is our pet (BotEye build 30+: token bit 7)


@dataclass
class Quest:
    qid: int; complete: bool; failed: bool; counts: tuple


@dataclass
class Rxp:
    """Strip row 4 (BotEye.lua fill_rxp, layout in nb/RXP_PLAN.md section 2). Coordinates are in the bot's frame
    (x = UnitPosition 1st value, as State.x); RXP has no z."""
    # block 193 status bits
    present: bool; loaded: bool; hidden: bool; qauto: bool; arrow: bool; done: bool
    loadnext: bool; nonext: bool; gamepad: bool; invmgr: bool; taxi: bool; trainauto: bool
    stepctr: int                     # bumped whenever (guide, step index) changes, mod 4096
    # 194 / 195
    guide_ord: int                   # 1.. = RXP_GUIDES position, 0 = none, 255 = other
    guide: str | None                # name from RXP_GUIDES, "other" for 255, None without a guide
    index: int                       # step index (differs per race/class: log `line` instead)
    line: int                        # step.stepId - guide.guideId: RXP's count of non-empty lines from the guide start
    nactive: int                     # #RXPFrame.activeSteps (current + sticky)
    sticky: bool                     # the current step itself is sticky
    flag: bool                       # BotEyeDB.rxp (/be rxp on)
    # 196 / 197
    kinds: int; skinds: int          # K_* masks: current step, union over the sticky steps
    wp: tuple | None                 # (x, y, radius|None, inst, flags) flags b0 step, b1 sticky, b2 arrow, b3 lowPrio, b4 dynamic
    quests: list                     # [(qid, Q_* tag, done)] up to 4; done = completed or skipped
    item: int; item_btn: int; item_ready: bool    # 205 (item_btn 0 = not on an RXP item button)
    train_id: int; train_known: bool              # 206
    xp_level: int; n_mobs: int; n_targets: int; n_scan: int   # 207
    tgtctr: int                      # 208: bumped whenever the name lists change
    text: str                        # 209-223 "T:<first .target>|M:<first mob>" (logs only)
    pts: list                        # [(x, y, radius|None, flags)] b0 wander, b1 completed/skipped, b2 hidePin
    # 248-249: the step's vendor .collect that BotEye buys (RXP's .collect never buys; BotEye.lua buy_collect)
    buy_item: int = 0                # item of the first open .collect (or the one last seen at a merchant)
    buy_seen: bool = False           # a merchant this step sold it
    buy_short: bool = False          # ... but the money did not cover all of it
    buy_done: bool = False           # BotEye bought some of it this step
    buy_full: bool = False           # bags full: nothing bought
    buy_need: int = 0                # items still needed (live, capped 255)
    buy_price: int = 0               # unit price in copper at that merchant
    # 250: BotEye.lua BOTEYE_BUILD (0 = a copy from before the stamp, i.e. without the buy blocks either)
    build: int = 0

    def has(self, mask: int, sticky: bool = False) -> bool:
        """Any of the K_* bits in `mask` in the current step (or, with sticky=True, also in the sticky steps)."""
        return bool((self.kinds | (self.skinds if sticky else 0)) & mask)


@dataclass
class State:
    t: float
    frame: int
    x: float; y: float; z: float; facing: float
    hp: int; hpmax: int; mana: int; manamax: int
    level: int; inst: int; xp: int; xpmax: int
    flags: int
    speed: float
    casting: int
    t_hp: int; t_hpmax: int; t_level: int; t_class: int; t_reaction: int
    t_npc: int; t_flags: int; t_hash: int; t_x: float | None; t_y: float | None
    bags_free: int; nquests: int; money: int; dur: int
    slots_ready: int; auras: int; err: int; errn: int; ui: int; gtime: float
    plates: list = field(default_factory=list)
    quests: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    msg: str = ""; msgn: int = 0; luaerrs: int = 0
    rxp: Rxp | None = None           # None: addon without row 4 (no RXP_TAG in block 192)
    # 166-169 (BotEye build 13, fill_supplies): the best usable water / food in the bags (item id, 0 none), how many
    # usable ones the bags hold and how many of the current tier (required level within stock.SPAN of ours), and
    # whether the character uses that kind at all (water: a mana user). Older builds paint 0 (nb/stock.py known())
    drink_id: int = 0; food_id: int = 0
    drinks: int = 0; foods: int = 0; drinks_cur: int = 0; foods_cur: int = 0
    drink_want: bool = False; food_want: bool = False
    # 170 (BotEye fill_ammo): arrows / bullets the bags hold for the ranged weapon, b21 bullets, b22 it shoots ammo
    ammo: int = 0; ammo_bullets: bool = False; ammo_want: bool = False
    pet: int = 0                 # block 171 (BotEye pet_bits)
    pet_exists = property(lambda s: bool(s.pet & 1))
    pet_dead = property(lambda s: bool(s.pet >> 1 & 1))
    pet_hpb: int = -1            # bar 15 (secret health, painted); -1 = none
    pet_hp = property(lambda s: s.pet_hpb if s.pet_hpb >= 0 and s.pet & 1 else s.pet >> 2 & 127)
    pet_happy = property(lambda s: s.pet >> 9 & 3)              # 1 unhappy .. 3 happy, 0 unknown
    pet_combat = property(lambda s: bool(s.pet >> 11 & 1))
    pet_on_target = property(lambda s: bool(s.pet >> 12 & 1))   # its target is ours
    pet_known = property(lambda s: bool(s.pet >> 13 & 1))       # the class's pet spell is known

    # player flags
    combat = property(lambda s: bool(s.flags & 1))
    dead = property(lambda s: bool(s.flags & 2))
    ghost = property(lambda s: bool(s.flags & 4))
    casting_now = property(lambda s: bool(s.flags & 8))
    moving = property(lambda s: bool(s.flags & 16))
    swimming = property(lambda s: bool(s.flags & 32))
    falling = property(lambda s: bool(s.flags & 64))
    autoattack = property(lambda s: bool(s.flags & 256))
    has_target = property(lambda s: bool(s.flags & 512) or s.t_npc != 0)
    chat_open = property(lambda s: bool(s.flags & 16384))
    threatened = property(lambda s: bool(s.flags & (1 << 17)))     # some mob has us on its threat list
    no_control = property(lambda s: bool(s.flags & (1 << 18)))     # stunned / feared / charmed
    on_gcd = property(lambda s: bool(s.flags & (1 << 19)))         # global cooldown running
    autoshot = property(lambda s: bool(s.flags & (1 << 20)))       # the class ranged attack auto-repeating
    new_mail = property(lambda s: bool(s.flags & (1 << 21)))
    mounted = property(lambda s: bool(s.flags & (1 << 12)))        # IsMounted       # unread mail (BotEye HasNewMail)
    # target flags
    t_dead = property(lambda s: bool(s.t_flags & 1))
    t_attackable = property(lambda s: bool(s.t_flags & 2))
    t_player = property(lambda s: bool(s.t_flags & 4))
    t_tapped = property(lambda s: bool(s.t_flags & 8))
    t_on_me = property(lambda s: bool(s.t_flags & 16))
    t_friend = property(lambda s: bool(s.t_flags & 32))
    t_combat = property(lambda s: bool(s.t_flags & 64))
    t_within10 = property(lambda s: bool(s.t_flags & 128))
    t_within11 = property(lambda s: bool(s.t_flags & 256))
    t_within28 = property(lambda s: bool(s.t_flags & 512))
    t_in_ranged = property(lambda s: bool(s.t_flags & 2048))     # in range of the class ranged attack (BotEye CLASS_CFG)
    t_trivial = property(lambda s: bool(s.t_flags & (1 << 14)))    # gray to us
    t_threat = property(lambda s: bool(s.t_flags & (1 << 15)))     # it has us on its threat list
    t_casting = property(lambda s: bool(s.t_flags & (1 << 16)))    # it is casting a spell
    t_visible = property(lambda s: bool(s.t_flags & (1 << 17)))
    t_pvp = property(lambda s: bool(s.t_flags & (1 << 18)))
    t_has_loot = property(lambda s: bool(s.t_flags & (1 << 19)))   # CanLootUnit: the corpse has loot for us
    t_loot_known = property(lambda s: bool(s.t_flags & (1 << 20))) # ... and that API exists in this client
    t_rxp_mob = property(lambda s: bool(s.t_flags >> 21 & 1))      # in RXP's mob list (BotEye fill_target)
    t_rxp_npc = property(lambda s: bool(s.t_flags >> 22 & 1))      # in the current step's .target list
    weapon_buff = property(lambda s: bool(s.auras & 2))       # main-hand enchant (the class imbue, an oil, a poison)
    res_sick = property(lambda s: bool(s.auras & 16))         # Resurrection Sickness (spirit healer revive, level 11+)
    weapon_secs = property(lambda s: s.auras >> 8)           # seconds it has left
    # camping (BotEye build 4): the 60 s campfire rest 1289723 (sitting by a campfire), Boosted Rest / Camp Benefits
    campfire_rest = property(lambda s: bool(s.auras & 32))
    camp_buff = property(lambda s: bool(s.auras & 64))
    eating = property(lambda s: bool(s.auras & 4))            # the "Food" aura: sitting with a meal
    drinking = property(lambda s: bool(s.auras & 8))          # the "Drink" aura

    @property
    def hp_pct(self): return 100.0 * self.hp / self.hpmax if self.hpmax else 0.0
    POWER_IS_MANA = True     # nb/classes.py Profile.mana: False (rage / energy) -> mana_pct reads full
    @property
    def mana_pct(self):
        if not State.POWER_IS_MANA:
            return 100.0     # the power bar is rage / energy: no mana line may hold the bot back
        return 100.0 * self.mana / self.manamax if self.manamax else 0.0
    @property
    def t_hp_pct(self): return 100.0 * self.t_hp / self.t_hpmax if self.t_hpmax else 0.0

    def ready(self, slot: int) -> bool:
        return bool(self.slots_ready >> (2 * (slot - 1)) & 1)

    def in_range(self, slot: int) -> bool:
        return bool(self.slots_ready >> (2 * (slot - 1) + 1) & 1)

    def ui_open(self, bit: int) -> bool:
        return bool(self.ui >> bit & 1)

    def quest(self, qid: int):
        return next((q for q in self.quests if q.qid == qid), None)


class Eye:
    def __init__(self, hwnd: int | None = None):
        self.cap = Capture(hwnd or find_window())
        rows = math.ceil(NBLOCKS / PER_ROW)
        self.bar_y0 = rows * BLOCK          # per frame in raw(): 3 rows up when the addon has no row 4
        self.rows = self.bar_y0 + ((NBARS + 1) // 2) * 3
        idx = np.arange(NBLOCKS)
        self.py = (idx // PER_ROW) * BLOCK + BLOCK // 2
        self.px = (idx % PER_ROW) * BLOCK + BLOCK // 2
        self.last: State | None = None
        self.ok_t = 0.0
        self.hooks = []                 # called with every decoded State (kill tracker ...)
        self.gap = 0.0                  # length (s) of the last stretch without a strip (login / loading screen)

    def in_world(self) -> bool:
        """Strip seen within 0.5 s, else look again now."""
        if time.time() - self.ok_t < 0.5:
            return True
        return self.raw(full=False) is not None

    def raw(self, full: bool = True):
        a8 = self.cap.grab(None if full else self.rows)
        self.marks = find_markers(a8) if full else {}
        self.frame_rgb = a8
        a = a8[:self.rows].astype(np.int64)
        p = a[self.py, self.px]
        v = (p[:, 0] << 16) | (p[:, 1] << 8) | p[:, 2]
        if v[0] != MARKER:
            return None
        now = time.time()
        if self.ok_t and now - self.ok_t > 5:
            self.gap = now - self.ok_t
        self.ok_t = now
        self.bar_y0 = bar_y0(int(v[RXP_BLOCK0]))
        bars = []
        for i in range(NBARS):
            y = self.bar_y0 + (i // 2) * 3 + 1
            x0 = (i % 2) * BAR_PX
            row = a[y, x0:x0 + BAR_PX].sum(axis=1)
            bars.append(float((row > 382).sum()) / BAR_PX)
        return v.tolist(), bars

    def read(self, retries: int = 5) -> State | None:
        for _ in range(retries):
            r = self.raw()
            if r:
                self.last = decode(*r)
                for k, pl in enumerate(self.last.plates):
                    pl.sx, pl.sy = self.marks.get(k, (-1, -1))
                for h in self.hooks:
                    h(self.last)
                return self.last
            time.sleep(0.05)
        return None


def bar_y0(v192: int) -> int:
    """First pixel row of the status bars: under 4 block rows with RXP_TAG in block 192, else under 3 (old addon)."""
    return (4 if v192 == RXP_TAG else 3) * BLOCK


def find_markers(a: np.ndarray) -> dict[int, tuple[int, int]]:
    """Plate markers: 3x3 (254,1,k+1) with a 3x3 (1,254,k+1) to its right."""
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    ys, xs = np.nonzero((r == 254) & (g == 1) & (b >= 1) & (b <= 12))
    out: dict[int, list] = {}
    w = a.shape[1]
    for y, x in zip(ys.tolist(), xs.tolist()):
        k = int(b[y, x]) - 1
        x2 = x + 3
        if x2 < w and r[y, x2] == 1 and g[y, x2] == 254 and b[y, x2] == k + 1:
            out.setdefault(k, []).append((x, y))
    return {k: (int(sum(p[0] for p in v) / len(v)) + 3, int(sum(p[1] for p in v) / len(v)))
            for k, v in out.items()}


def decode(v: list[int], bars: list[float]) -> State:
    plates = []
    for k in range(12):
        b = 32 + 4 * k
        if not v[b]:
            continue
        c = v[b + 2]
        fl = c >> 14
        plates.append(Plate(v[b] & 4095, v[b] >> 12, v[b + 1], c & 127, round(100 * bars[3 + k]),
                            bool(fl & 1), bool(fl & 2), bool(fl & 4), bool(fl & 8), bool(fl & 16),
                            bool(fl & 32), bool(fl & 64), bool(fl & 128), bool(fl & 256),
                            v[b + 3] & 127, v[b + 3] >> 8, bool(c & 128), bool(fl & 512),
                            rxp_mob=bool(v[b] >> 1 & 1), rxp_npc=bool(v[b] >> 2 & 1), rxp_scan=bool(v[b] >> 3 & 1),
                            on_pet=bool(v[b + 3] & 128)))
    quests = []
    nq = NQUESTS if len(v) > quest_base(NQUESTS - 1) + 1 and v[145] == QUEST20_TAG else 10
    for k in range(nq):
        b = quest_base(k)
        a, c = v[b], v[b + 1]
        if a:
            quests.append(Quest(a & 0x3FFFFF, bool(a >> 23 & 1), bool(a >> 22 & 1),
                                (c & 255, c >> 8 & 255, c >> 16 & 255)))
    text = bytearray()
    for i in range(32):
        x = v[112 + i]
        text += bytes((x >> 16, x >> 8 & 255, x & 255))
    msg = text.split(b"\0")[0].decode("latin-1", "replace")
    return State(
        t=time.time(), frame=v[1],
        x=_coord(v[2]), y=_coord(v[3]), z=_coord(v[4]), facing=v[5] / 2097152.0,
        hp=round(bars[0] * v[7]), hpmax=v[7], mana=round(bars[1] * v[9]), manamax=v[9], level=v[10], inst=v[11],
        xp=v[12], xpmax=v[13], flags=v[14], speed=v[30] / 1000.0, casting=v[26],
        t_hp=round(bars[2] * 100), t_hpmax=100, t_level=v[17] & 255, t_class=v[17] >> 8 & 15,
        t_reaction=v[17] >> 12, t_npc=v[18], t_flags=v[19], t_hash=v[20],
        t_x=_coord(v[28]) if v[28] else None, t_y=_coord(v[29]) if v[29] else None,
        bags_free=v[21] & 255, nquests=v[21] >> 8 & 255, dur=(v[21] >> 16) - 1, money=v[22], slots_ready=v[23],
        auras=v[24], err=v[25] >> 16, errn=v[25] & 65535, ui=v[27], gtime=v[31] / 10.0,
        plates=plates, quests=quests, actions=v[100:112], msg=msg, msgn=v[144] & 65535,
        luaerrs=v[144] >> 16, rxp=decode_rxp(v),
        pet_hpb=round(100 * bars[15]) if len(bars) > 15 else -1, **_supplies(v))


def _supplies(v: list[int]) -> dict:
    """Blocks 166-169 (BotEye.lua fill_supplies): count + current count * 2048 + b22 wanted."""
    if len(v) < 170:
        return {}
    d, f = v[168], v[169]
    return dict(drink_id=v[166], food_id=v[167], drinks=d & 2047, drinks_cur=d >> 11 & 2047,
                drink_want=bool(d >> 22 & 1), foods=f & 2047, foods_cur=f >> 11 & 2047, food_want=bool(f >> 22 & 1),
                ammo=v[170] & 65535, ammo_bullets=bool(v[170] >> 21 & 1), ammo_want=bool(v[170] >> 22 & 1),
                pet=v[171] if len(v) > 171 else 0)


def _text(v: list[int], first: int, n: int) -> str:
    """n blocks of 3 latin-1 chars each (BotEye fill_text / put_text), up to the first NUL."""
    raw = bytearray()
    for i in range(n):
        x = v[first + i]
        raw += bytes((x >> 16 & 255, x >> 8 & 255, x & 255))
    return raw.split(b"\0")[0].decode("latin-1", "replace")


def _radius(r: int) -> int | None:
    return None if r == 255 else r


def decode_rxp(v: list[int]) -> Rxp | None:
    """Row 4 (blocks 192-255), only with RXP_TAG in block 192 (BotEye.lua fill_rxp)."""
    if len(v) < 256 or v[RXP_BLOCK0] != RXP_TAG:
        return None
    st, g, ln = v[193], v[194], v[195]
    ord_ = g & 255
    if ord_ == 0:
        name = None
    elif 1 <= ord_ <= len(RXP_GUIDES):
        name = RXP_GUIDES[ord_ - 1]
    else:
        name = "other"
    wp = None
    if v[198] or v[199]:
        w = v[200]
        wp = (_coord(v[198]), _coord(v[199]), _radius(w & 255), w >> 8 & 255, w >> 16)
    quests = []
    for k in range(4):
        q = v[201 + k]
        if q:
            quests.append((q & 0xFFFFF, q >> 20 & 7, bool(q >> 23 & 1)))
    pts = []
    for k in range(8):
        b = 224 + 3 * k
        if v[b] or v[b + 1]:
            pts.append((_coord(v[b]), _coord(v[b + 1]), _radius(v[b + 2] & 255), v[b + 2] >> 8))
    it, tr, xp = v[205], v[206], v[207]
    return Rxp(
        present=bool(st & 1), loaded=bool(st >> 1 & 1), hidden=bool(st >> 2 & 1), qauto=bool(st >> 3 & 1),
        arrow=bool(st >> 4 & 1), done=bool(st >> 5 & 1), loadnext=bool(st >> 6 & 1), nonext=bool(st >> 7 & 1),
        gamepad=bool(st >> 8 & 1), invmgr=bool(st >> 9 & 1), taxi=bool(st >> 10 & 1), trainauto=bool(st >> 11 & 1),
        stepctr=st >> 12,
        guide_ord=ord_, guide=name, index=g >> 8,
        line=ln & 0xFFFF, nactive=ln >> 16 & 15, sticky=bool(ln >> 20 & 1), flag=bool(ln >> 21 & 1),
        kinds=v[196], skinds=v[197], wp=wp, quests=quests,
        item=it & 0xFFFFF, item_btn=it >> 20 & 7, item_ready=bool(it >> 23 & 1),
        train_id=tr & 0xFFFFF, train_known=bool(tr >> 23 & 1),
        xp_level=xp & 255, n_mobs=xp >> 8 & 15, n_targets=xp >> 12 & 15, n_scan=xp >> 16 & 15,
        tgtctr=v[208], text=_text(v, 209, 15), pts=pts,
        buy_item=v[248] & 0xFFFFF, buy_seen=bool(v[248] >> 20 & 1), buy_short=bool(v[248] >> 21 & 1),
        buy_done=bool(v[248] >> 22 & 1), buy_full=bool(v[248] >> 23 & 1), buy_need=v[249] & 255,
        buy_price=v[249] >> 8, build=v[250] & 0xFFFF if v[250] >> 16 == BUILD_TAG else 0)


def disk_build(path: str = BOTEYE_LUA) -> int | None:
    """BOTEYE_BUILD of the BotEye.lua on disk (what a /reload would load); None if unreadable. A client whose strip
    shows a lower build still runs an older copy: it was not /reloaded after the last addon change."""
    import re
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"^local BUILD_TAG, BOTEYE_BUILD = 0x[0-9A-Fa-f]+, (\d+)", f.read(), re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


if __name__ == "__main__":
    import sys
    # python -m nb.eye [inst]: one instance's client (nb.multi, as nb.evi), else the first WoW window
    if len(sys.argv) > 1:
        from .multi import find_instance_window
        e = Eye(find_instance_window(int(sys.argv[1])))
    else:
        e = Eye()
    s = e.read()
    if not s:
        print("no strip")
    else:
        for k, val in s.__dict__.items():
            if k not in ("plates", "quests", "rxp"):
                print(f"{k:12} {val}")
        for p in s.plates:
            print(p)
        for q in s.quests:
            print(q)
        if s.rxp is None:
            print("rxp          None (addon without row 4)")
        else:
            for k, val in s.rxp.__dict__.items():
                print(f"rxp.{k:10} {val}")
