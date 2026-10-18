"""nb/kills.py - kill evidence from the BotEye strip. Hook it into Eye.read()."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Kill:
    t: float          # State.t of the sample that proved it
    npc: int
    ghash: int        # State.t_hash of the victim (24 bit): loot() uses it to verify the corpse
    level: int
    by: str           # "xpmsg" | "xp" | "dmsg" | "dead" | "hp" (weak) | "xp?"
    xp: int = 0       # xp gained, 0 = unknown / gray mob
    hp: float = 0.0   # last seen target hp %
    x: float = 0.0    # where we stood (and which way we faced) when it died: the corpse lies ahead
    y: float = 0.0
    face: float = 0.0
    fight: int = 0    # Bot.fight_no of the fight it died in: loot() judges a corpse by its neighbours from that fight


# CanLootUnit memory. BotEye's target bits 19/20 carry CanLootUnit(target) for a dead target, but a corpse is only
# targetable while it is the target or the last target: of a 2+ kill fight the older corpses were never checked, and
# 161 loot attempts on 2026-10-14 opened nothing (10.5 min; inst2 17:13:11-17:13:36 six soft interacts on the three
# other npc 1718 corpses after the first one looted). Every dead target the strip shows is remembered by guid hash, so
# loot() can drop a corpse that had nothing for us when it died without walking or targeting it again.
LOOT_SETTLE = 0.3        # s a target must have read dead before its 'no loot' answer counts (the flag can lag the death)
LOOT_KEEP = 300.0        # s a record is kept (corpses are looted within 120 s)


def note_loot(seen: dict, s) -> None:
    """Eye-hook body: remember CanLootUnit of the dead target in sample s (by guid hash, in place)."""
    h = getattr(s, "t_hash", 0)
    if not h or not s.has_target:
        return
    if not s.t_dead:
        seen.pop(h, None)                  # alive (a respawn can reuse the guid): an old answer means nothing now
        return
    if not s.t_loot_known:
        return
    r = seen.get(h)
    if r is None or s.t - r["t"] > 30.0:   # first sight of this death (or a new death with a reused guid)
        r = seen[h] = {"dead": s.t, "v": None, "t": s.t}
    r["v"], r["t"] = bool(s.t_has_loot), s.t
    if len(seen) > 64:
        for k in [k for k, v in seen.items() if s.t - v["t"] > LOOT_KEEP]:
            del seen[k]


def loot_verdict(seen: dict, h: int):
    """True: the corpse holds loot for us, False: it does not (read LOOT_SETTLE s after it died), None: not known."""
    r = seen.get(h) if h else None
    if r is None or r["v"] is None:
        return None
    if r["v"]:
        return True
    return False if r["t"] - r["dead"] >= LOOT_SETTLE else None


def fresh_corpses(corpses: list, now: float, max_age: float = 120.0) -> tuple:
    """(corpses younger than max_age with one record per guid hash, number of duplicates dropped). The same death
    can be credited twice (inst2 17:12:39 KILL by=xp, then 17:12:47 by=dead off the still-targeted corpse); the
    first record is kept (the tracker's, with the spot and heading at the kill)."""
    out, seen, dup = [], set(), 0
    for c in corpses:
        if now - c.t >= max_age:
            continue
        if c.ghash and c.ghash in seen:
            dup += 1
            continue
        if c.ghash:
            seen.add(c.ghash)
        out.append(c)
    return out, dup


def soft_allowed(fight: int, opened: set, soft_fail: dict) -> bool:
    """May an untargetable corpse of this fight get a soft interact? The first one may; after one that opened
    nothing a single retry, and none once a neighbour from that fight opened. The soft target takes only a lootable
    corpse within 12 yd: in the four logs of 2026-10-14 a soft try <30 s after a failed one opened 1 of 35 (an empty
    window), a first one after a neighbour opened 2 of 24."""
    n = soft_fail.get(fight, 0)
    return n == 0 or (n == 1 and fight not in opened)


def retry_loot(res: str, tries: int, fight: int, opened: set, has_loot=None) -> bool:
    """Try this corpse again? 'toofar' up to 3 tries; 'noopen' once, and only when CanLootUnit said the corpse
    holds loot for us (has_loot True) and no neighbour from the same fight opened (then this one had nothing for
    us: 2nd noopen tries opened 0 of 12). One interact per corpse otherwise: 58 of 190 kills on 2026-10-14 paid a
    retry, the human loots in 1.1 s and walks on (nb/human/REPORT.md change 6)."""
    if res == "toofar":
        return tries < 3
    if res == "noopen":
        return tries < 2 and fight not in opened and has_loot is True
    return False


class KillTracker:
    WEAK_HP = 20.0    # target vanished at <= this hp% and nothing says otherwise -> weak kill
    GRACE = 0.8       # s to wait for xp / event evidence after the target vanished

    def __init__(self):
        self.kills: list[Kill] = []
        self.pending: list[Kill] = []     # not yet consumed by the bot (take())
        self.lost = 0                     # hostile targets that vanished without kill evidence
        self.tgt = None                   # last live hostile target {npc, hash, level, hp}
        self.wait = None                  # (tgt, deadline) while waiting for evidence
        self.xp = self.xpmax = self.lvl = self.kn = self.dn = None

    def take(self) -> list[Kill]:
        out, self.pending = self.pending, []
        return out

    def waiting(self) -> bool:
        return self.wait is not None

    def _credit(self, s, tgt, by, xp=0) -> Kill:
        k = Kill(s.t, tgt["npc"], tgt["hash"], tgt["level"], by, xp, tgt["hp"], s.x, s.y, s.facing)
        self.kills.append(k)
        self.pending.append(k)
        self.tgt = self.wait = None
        return k

    def update(self, s):
        """Call with every decoded State. Returns a Kill when this sample proves one."""
        if self.xp is None:
            self.xp, self.xpmax, self.lvl = s.xp, s.xpmax, s.level
            self.kn, self.dn = getattr(s, 'kills_n', 0), getattr(s, 'deaths_n', 0)
        now = s.t
        gain = 0                                   # xp rise (a level-up wraps xp)
        if s.level > self.lvl:
            gain = max(1, self.xpmax - self.xp) + s.xp
        elif s.xp > self.xp:
            gain = s.xp - self.xp
        kn, dn, kf = getattr(s, 'kills_n', 0), getattr(s, 'deaths_n', 0), getattr(s, 'kill_flags', 0)
        xmsg = kn != self.kn                # addon saw "<mob> dies, you gain N experience"
        dmsg = dn != self.dn and bool(kf & 2)   # "<mob> dies." for our last target
        xmsg_match = xmsg and bool(kf & 1)
        live = (s.has_target and not s.t_dead and s.t_attackable
                and not s.t_friend and not s.t_player)
        tgt, k = self.tgt, None            # evidence is judged against the target seen BEFORE this sample
        recent = bool(self.kills) and now - self.kills[-1].t < 2.0
        if gain or xmsg_match or dmsg:
            by = "xpmsg" if xmsg_match else "xp" if gain else "dmsg"
            if tgt is not None:
                k = self._credit(s, tgt, by, gain)
            elif recent:                           # second piece of evidence for the same death
                if gain and not self.kills[-1].xp:
                    self.kills[-1].xp = gain
            elif s.has_target and s.t_attackable and s.t_dead:
                k = self._credit(s, dict(npc=s.t_npc, hash=s.t_hash, level=s.t_level, hp=0.0), "xp?", gain)
            # else: turn-in / exploration xp, not a kill
        elif s.has_target and s.t_dead and tgt is not None and tgt["hash"] == s.t_hash:
            k = self._credit(s, tgt, "dead")       # gray mob: dead flag, no xp
        elif tgt is not None and not live and self.wait is None and not (s.has_target and s.t_dead):
            self.wait = (tgt, now + self.GRACE)    # target vanished: wait for evidence
        if k is None and self.wait is not None and now > self.wait[1]:
            t = self.wait[0]
            if t["hp"] <= self.WEAK_HP:
                k = self._credit(s, t, "hp")
            else:
                self.lost += 1
                self.tgt = self.wait = None
        if live:                                   # now start / refresh tracking of the current target
            if self.tgt is None or self.tgt["hash"] != s.t_hash:
                if self.tgt is not None and self.wait is None:
                    self.lost += 1                 # switched away from a live mob without evidence
                self.tgt = dict(npc=s.t_npc, hash=s.t_hash, level=s.t_level, hp=s.t_hp_pct)
            self.tgt["hp"] = s.t_hp_pct
            self.wait = None
        self.xp, self.xpmax, self.lvl = s.xp, s.xpmax, s.level
        self.kn, self.dn = getattr(s, 'kills_n', 0), getattr(s, 'deaths_n', 0)
        return k
