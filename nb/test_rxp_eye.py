"""Offline tests for strip row 4 (RestedXP state) and the RXP name bits: python -m nb.test_rxp_eye

Part 1 decodes synthetic block lists (with tag, without tag, edge coordinates, plate and target bits).
Part 2 runs the real BotEye.lua in Lua 5.1 (lupa) against stubbed WoW API and a fake RXP addon, then decodes the
painted strip with eye.decode: the Lua encoder and the Python decoder are checked against each other, and so are
/be rxp on|load|skip|next|dump|off and the PLAYER_ENTERING_WORLD autoload. Part 2 is skipped without lupa or
without the addon folder. Touches no game window, sends no keys.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import eye                                  # noqa: E402
from nb.eye import (K_ACCEPT, K_COMPLETE, K_GOTO, K_MANUAL, K_MOB, K_TARGET, K_WANDER, Q_ACCEPT,  # noqa: E402
                    RXP_TAG, decode, decode_rxp)

BOTEYE = r"C:\Program Files (x86)\World of Warcraft\_classic_beta_\Interface\AddOns\BotEye\BotEye.lua"
BARS = [0.5] * eye.NBARS


def coord(x: float) -> int:
    """BotEye.lua coord() + put(): floor((x + 20000) * 256 + 0.5), clamped to 24 bits."""
    import math
    v = math.floor((x + 20000) * 256 + 0.5)
    return max(0, min(16777215, v))


def blank() -> list[int]:
    v = [0] * eye.NBLOCKS
    v[0] = eye.MARKER
    return v


def row4(v, *, st=0, guide=0, line=0, kinds=0, skinds=0, wp=None, quests=(), item=0, train=0, xp=0,
         tgtctr=0, text="", pts=()):
    """Python mirror of BotEye fill_rxp's packing (RXP_PLAN.md section 2 table)."""
    v[192] = RXP_TAG
    v[193], v[194], v[195], v[196], v[197] = st, guide, line, kinds, skinds
    if wp:
        x, y, r, inst, fl = wp
        v[198], v[199] = coord(x), coord(y)
        v[200] = (255 if r is None else r) + inst * 256 + fl * 65536
    for k, (qid, tag, done) in enumerate(quests):
        v[201 + k] = qid + tag * 2 ** 20 + (2 ** 23 if done else 0)
    v[205], v[206], v[207], v[208] = item, train, xp, tgtctr
    b = text.encode("latin-1")[:45].ljust(45, b"\0")
    for i in range(15):
        v[209 + i] = b[3 * i] << 16 | b[3 * i + 1] << 8 | b[3 * i + 2]
    for k, (x, y, r, fl) in enumerate(pts):
        v[224 + 3 * k], v[225 + 3 * k] = coord(x), coord(y)
        v[226 + 3 * k] = (255 if r is None else r) + fl * 256
    return v


def near(a, b, tol=1.0 / 256 + 1e-9):
    return abs(a - b) <= tol


# ---------------------------------------------------------------- part 1: synthetic block lists
def test_without_tag():
    v = blank()
    assert decode(v, BARS).rxp is None
    v[192] = 0xFFFFFF                    # an old addon: block 192 sits on the white player-hp bar
    assert decode(v, BARS).rxp is None
    assert decode_rxp(v[:192]) is None   # a 3-row block list
    assert eye.bar_y0(0xFFFFFF) == 9 and eye.bar_y0(RXP_TAG) == 12


def test_with_tag_sten():
    st = 1 | 2 | 8 | 16 | (1 << 9) | (37 << 12)             # present loaded qauto arrow invmgr, stepctr 37
    v = row4(blank(), st=st, guide=1 + 1 * 256, line=15 + 2 * 65536 + 2 ** 21,
             kinds=K_ACCEPT | K_WANDER | K_TARGET, skinds=K_WANDER | K_COMPLETE | K_MOB | K_MANUAL,
             wp=(-6214.85, 328.18, None, 0, 1), quests=[(179, Q_ACCEPT, False)], item=6948 + 2 * 2 ** 20 + 2 ** 23,
             train=8017 + 2 ** 23, xp=5 + 1 * 256 + 1 * 4096, tgtctr=9, text="T:Sten Stoutarm|M:Ragged Young Wolf",
             pts=[(-6214.85, 328.18, None, 1), (-6000.5, 400.25, 50, 1 | 4)])
    r = decode(v, BARS).rxp
    assert r is not None
    assert (r.present, r.loaded, r.hidden, r.qauto, r.arrow, r.done) == (True, True, False, True, True, False)
    assert (r.loadnext, r.nonext, r.gamepad, r.invmgr, r.taxi, r.trainauto) == (False,) * 3 + (True,) + (False,) * 2
    assert r.stepctr == 37
    assert (r.guide_ord, r.guide, r.index) == (1, "01-05 Coldridge Valley", 1)
    assert (r.line, r.nactive, r.sticky, r.flag) == (15, 2, False, True)
    assert r.has(K_ACCEPT) and r.has(K_TARGET) and not r.has(K_MOB) and r.has(K_MOB, sticky=True)
    x, y, rad, inst, fl = r.wp
    assert near(x, -6214.85) and near(y, 328.18) and rad is None and inst == 0 and fl == 1
    assert r.quests == [(179, Q_ACCEPT, False)]
    assert (r.item, r.item_btn, r.item_ready) == (6948, 2, True)
    assert (r.train_id, r.train_known) == (8017, True)
    assert (r.xp_level, r.n_mobs, r.n_targets, r.n_scan) == (5, 1, 1, 0)
    assert r.tgtctr == 9 and r.text == "T:Sten Stoutarm|M:Ragged Young Wolf"
    assert len(r.pts) == 2 and r.pts[1][2] == 50 and r.pts[1][3] == 5 and near(r.pts[1][0], -6000.5)


def test_guide_ordinals_and_packing_limits():
    v = row4(blank(), guide=255 + 65535 * 256, line=65535 + 15 * 65536 + 2 ** 20)
    r = decode_rxp(v)
    assert (r.guide_ord, r.guide, r.index) == (255, "other", 65535)
    assert (r.line, r.nactive, r.sticky, r.flag) == (65535, 15, True, False)
    assert decode_rxp(row4(blank(), guide=0)).guide is None
    assert decode_rxp(row4(blank(), guide=len(eye.RXP_GUIDES))).guide == eye.RXP_GUIDES[-1]
    # quest ids up to 20 bits, every tag, done bit; 4 slots max; empty slots dropped
    q = [(97277, 4, True), (1048575, 3, False), (1, 2, True)]
    assert decode_rxp(row4(blank(), quests=q)).quests == q
    assert decode_rxp(row4(blank(), st=4095 + (4095 << 12))).stepctr == 4095


def test_edge_coords():
    for x, y in [(-19999.0, 45535.0), (0.0, 0.0), (-6214.85, 328.18), (1.0 / 256, -1.0 / 256), (12345.678, -9876.543)]:
        r = decode_rxp(row4(blank(), wp=(x, y, 254, 255, 31), pts=[(x, y, 0, 7)] * 8))
        wx, wy, rad, inst, fl = r.wp
        assert near(wx, x) and near(wy, y), (x, y, wx, wy)
        assert (rad, inst, fl) == (254, 255, 31)
        assert len(r.pts) == 8 and all(near(p[0], x) and near(p[1], y) and p[2:] == (0, 7) for p in r.pts)
    # no waypoint: blocks 198/199 black
    assert decode_rxp(row4(blank())).wp is None and decode_rxp(row4(blank())).pts == []
    # a coordinate near the encoding floor (x = -20000 would be block value 0 = "none": RXP never sends that)
    assert near(decode_rxp(row4(blank(), wp=(-19999.99, 5.0, None, 0, 4))).wp[0], -19999.99)


def test_plate_and_target_bits():
    v = row4(blank())
    v[32], v[33], v[34], v[35] = 1 + 2 + 8, 1124, 3 | (1 << 14), 1 + 5 * 256          # mob + unitscan
    v[36], v[37], v[38], v[39] = 1 + 4, 658, 5 | (256 << 14), 2 + 6 * 256              # target-list NPC
    v[40], v[41], v[42], v[43] = 1, 704, 2, 3                                            # old addon / no list
    v[19] = (1 << 21) | (1 << 22) | 2
    s = decode(v, BARS)
    p0, p1, p2 = s.plates
    assert (p0.rxp_mob, p0.rxp_npc, p0.rxp_scan) == (True, False, True) and p0.hostile
    assert (p1.rxp_mob, p1.rxp_npc, p1.rxp_scan) == (False, True, False) and p1.friendly and p1.npc == 658
    assert (p2.rxp_mob, p2.rxp_npc, p2.rxp_scan) == (False, False, False)
    assert s.t_rxp_mob and s.t_rxp_npc and s.t_attackable
    v[19] = 2
    s = decode(v, BARS)
    assert not s.t_rxp_mob and not s.t_rxp_npc


# ---------------------------------------------------------------- part 2: the real BotEye.lua under lupa
STUBS = r"""
TEX, FRAMES, EVFRAMES, TIMERS, UNITS = {}, {}, {}, {}, {}
NOW = 100
local noop = function() return nil end
local stub
stub = setmetatable({}, { __call = function() return nil end, __index = function() return stub end })
local M = {}
M.__index = function(t, k) return M[k] or noop end
function M:SetScript(n, f) self._scripts[n] = f end
function M:GetScript(n) return self._scripts[n] end
function M:HookScript(n, f) end
function M:RegisterEvent(e) EVFRAMES[e] = self end
function M:CreateTexture() local t = setmetatable({ _scripts = {} }, M); table.insert(self._tex, t); return t end
function M:SetColorTexture(r, g, b) self.rgb = { r, g, b } end
function M:IsShown() return self._shown end
function M:Show() self._shown = true end
function M:Hide() self._shown = false end
function M:SetShown(v) self._shown = v and true or false end
function M:SetAttribute(k, v) self._attr[k] = v end
function M:GetAttribute(k) return self._attr[k] end
function M:GetName() return self._name end
function NEWFRAME(name) local f = setmetatable({ _scripts = {}, _tex = {}, _attr = {}, _name = name, _shown = true }, M); return f end
function CreateFrame(kind, name, parent)
    local f = NEWFRAME(name)
    if name then _G[name] = f; FRAMES[name] = f end
    return f
end
setmetatable(_G, { __index = function(_, k) return stub end })
BotEyeDB = {}
SlashCmdList = {}
UIParent = NEWFRAME("UIParent")
issecretvalue = function() return false end
GetTime = function() return NOW end
GetPhysicalScreenSize = function() return 1920, 1080 end
C_Timer = { After = function(d, f) table.insert(TIMERS, f) end, NewTicker = noop }
InCombatLockdown = function() return false end
strsplit = function(sep, s)
    local t = {}
    for p in (s .. sep):gmatch("(.-)" .. sep:gsub("%p", "%%%0")) do t[#t + 1] = p end
    return unpack(t)
end
UnitExists = function(u) return UNITS[u] ~= nil end
UnitName = function(u) return UNITS[u] and UNITS[u].name end
UnitGUID = function(u) return UNITS[u] and UNITS[u].guid end
UnitLevel = function(u) return 1 end
UnitOnTaxi = function() return false end
C_NamePlate = { GetNamePlates = function()
    local t = {}
    for i = 1, 3 do if UNITS["nameplate" .. i] then t[#t + 1] = { namePlateUnitToken = "nameplate" .. i } end end
    return t
end }
function RUN_TIMERS() local t = TIMERS; TIMERS = {}; for _, f in ipairs(t) do f() end end
function TICK() NOW = NOW + 0.3; FRAMES.BotEyeFrame:GetScript("OnUpdate")(FRAMES.BotEyeFrame, 0.3) end
function STRIP()
    local out = {}
    for i, t in ipairs(FRAMES.BotEyeFrame._tex) do
        local c = t.rgb or { 0, 0, 0 }
        out[i] = math.floor(c[1] * 255 + 0.5) * 65536 + math.floor(c[2] * 255 + 0.5) * 256 + math.floor(c[3] * 255 + 0.5)
    end
    return out
end
"""

FAKE_RXP = r"""
GROUP = "RestedXP Forever Guide (A)"
local function frame() return NEWFRAME(nil) end
-- Sten (Guides/forever/Alliance-1-14_DwarfGnome.lua:19-23) and the sticky wolf step after it (:24-34)
STEN = { index = 1, stepId = 7015, elements = {
    { tag = "goto", wx = 328.18, wy = -6214.85, instance = 0, textOnly = true, arrow = true, parent = true },
    { tag = "accept", questId = 179, text = "Accept Dwarven Outfitters" },
    { tag = "target", targets = { "Sten Stoutarm" }, textOnly = true } } }
WOLF = { index = 2, stepId = 7021, sticky = true, completewith = "next", elements = {
    { tag = "goto", wx = 520.1, wy = -6100.2, instance = 0, radius = 50, hidePin = true, textOnly = true, parent = true },
    { text = "Kill and loot Ragged Young Wolves" },
    { tag = "complete", questId = 179, textOnly = true },
    { tag = "mob", mobs = { "Ragged Young Wolf" }, textOnly = true } } }
GUIDE = { name = "01-05 Coldridge Valley", group = GROUP, key = "k1", guideId = 7000, next = "05-11 Dun Morogh",
    steps = { STEN, WOLF } }
R = { settings = { profile = { numMapPins = 0, hideGuideWindow = true } }, guides = {}, isHidden = true,
    RXPFrame = frame(), arrowFrame = frame(), activeItemFrame = { buttonList = {} },
    inventoryManager = { IsFeatureEnabled = function() return true end },
    IsGamePadEnabled = function() return false end, progress = 1, calls = {} }
R.RXPFrame._shown = false
R.RXPFrame.activeSteps = { WOLF, STEN }
R.guides[GROUP .. "||01-05 Coldridge Valley"] = { name = "01-05 Coldridge Valley", group = GROUP }
R.guides[GROUP .. "||05-11 Dun Morogh"] = { name = "05-11 Dun Morogh", group = GROUP }
R.targeting = { GetCurrentTargets = function() return { "Sten Stoutarm" }, { "*Ragged Young Wolf" }, {} end }
function R:LoadGuideTable(g, n) table.insert(self.calls, g .. "||" .. n); self.currentGuide = GUIDE; self.progress = 1 end
R.GetGuideProgress = function() return R.progress end
R.SetStep = function(n) R.progress = n end
R.UpdateMap = function() R.mapUpdated = true end
local ACE = { GetAddon = function(self, n, silent) return n == "RXPGuides" and R or nil end }
LibStub = function(name, silent) if name == "AceAddon-3.0" then return ACE end end
UNITS.nameplate1 = { name = "Ragged Young Wolf", guid = "Creature-0-1-0-1-705-0000AAAA" }
UNITS.nameplate2 = { name = "Sten Stoutarm", guid = "Creature-0-1-0-1-658-0000BBBB" }
UNITS.target = { name = "Sten Stoutarm", guid = "Creature-0-1-0-1-658-0000BBBB" }
"""


def _lua(with_rxp: bool = True):
    try:
        import lupa.lua51 as lua51
    except ImportError:
        return None
    if not os.path.exists(BOTEYE):
        return None
    rt = lua51.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(STUBS)
    if with_rxp:
        rt.execute(FAKE_RXP)
    src = open(BOTEYE, encoding="utf-8").read()
    load = rt.eval("function(s) local f, e = loadstring(s, 'BotEye.lua'); if not f then error(e) end; f('BotEye') end")
    load(src)
    return rt


def _state(rt):
    # two ticks: fill_plates / fill_target (fast group) run before fill_rxp (slow group) in the same OnUpdate, so the
    # plate bits follow a list change one frame later
    rt.eval("TICK()")
    rt.eval("TICK()")
    strip = rt.eval("STRIP()")
    v = [strip[i + 1] for i in range(len(strip))]
    assert len(v) == eye.NBLOCKS == 256
    return decode(v, BARS), v


def _slash(rt, line):
    rt.eval("function(l) SlashCmdList.BOTEYE(l) end")(line)


def test_lua_no_rxp_addon():
    rt = _lua(with_rxp=False)
    if rt is None:
        print("skip test_lua_no_rxp_addon (no lupa or no BotEye.lua)")
        return
    s, v = _state(rt)
    assert s.rxp is not None and not s.rxp.present and not s.rxp.loaded and s.rxp.wp is None
    assert all(x == 0 for i, x in enumerate(v[193:256], 193) if i != 250)
    assert v[250] >> 16 == eye.BUILD_TAG and s.rxp.build == eye.disk_build(BOTEYE)   # the build stamp stays
    _slash(rt, "rxp dump")
    assert s.plates == [] or not any(p.rxp_mob or p.rxp_npc for p in s.plates)
    assert "rxp missing" in _state(rt)[0].msg


def test_lua_flag_off_reads_only():
    rt = _lua()
    if rt is None:
        print("skip test_lua_flag_off_reads_only (no lupa or no BotEye.lua)")
        return
    G = rt.globals()
    s, _ = _state(rt)
    r = s.rxp
    assert r.present and not r.loaded and r.hidden and not r.flag and r.guide is None
    rt.eval("EVFRAMES.PLAYER_ENTERING_WORLD:GetScript('OnEvent')(EVFRAMES.PLAYER_ENTERING_WORLD, 'PLAYER_ENTERING_WORLD', true)")
    rt.eval("RUN_TIMERS()")
    assert len(G.R.calls) == 0, "flag off: no guide load"
    assert G.R.settings.profile.enableQuestAutomation is None, "flag off: settings untouched"


def test_lua_full_cycle():
    rt = _lua()
    if rt is None:
        print("skip test_lua_full_cycle (no lupa or no BotEye.lua)")
        return
    G = rt.globals()
    # /be rxp on: config + load the level-1 route by name
    _slash(rt, "rxp on")
    assert list(G.R.calls.values()) == ["RestedXP Forever Guide (A)||01-05 Coldridge Valley"]
    p = G.R.settings.profile
    assert p.enableQuestAutomation and not p.enableTrainerAutomation and not p.autoDiscardItems
    assert p.numMapPins == 7 and p.hideGuideWindow is False and G.R.isHidden is False and G.R.RXPFrame._shown
    s, v = _state(rt)
    r = s.rxp
    assert r.present and r.loaded and not r.hidden and r.qauto and r.flag and not r.nonext and r.invmgr
    assert (r.guide, r.index, r.line, r.nactive, r.sticky) == ("01-05 Coldridge Valley", 1, 15, 2, False)
    assert r.kinds == K_ACCEPT | K_WANDER | K_TARGET, bin(r.kinds)
    assert r.skinds == K_WANDER | K_MANUAL | K_COMPLETE | K_MOB, bin(r.skinds)
    x, y, rad, inst, fl = r.wp
    assert near(x, -6214.85) and near(y, 328.18) and rad is None and inst == 0 and fl == 1, r.wp   # RXP_PLAN step 3
    assert r.quests == [(179, Q_ACCEPT, False)]
    assert r.text == "T:Sten Stoutarm|M:Ragged Young Wolf", r.text
    assert (r.n_mobs, r.n_targets, r.n_scan) == (1, 1, 0)
    assert len(r.pts) == 1 and r.pts[0][3] == 1
    wolf, sten = s.plates
    assert wolf.rxp_mob and not wolf.rxp_npc and sten.rxp_npc and not sten.rxp_mob
    assert s.t_rxp_npc and not s.t_rxp_mob
    ctr0, tctr0 = r.stepctr, r.tgtctr
    assert _state(rt)[0].rxp.stepctr == ctr0, "no step change, no bump"

    # in combat the names are secret: the NPC-ID cache keeps the bits
    rt.execute("issecretvalue = function(v) return type(v) == 'string' end")
    s2, _ = _state(rt)
    assert s2.plates[0].rxp_mob and s2.plates[1].rxp_npc and s2.t_rxp_npc
    rt.execute("issecretvalue = function() return false end")

    # /be rxp skip: the accept element is skipped (the textOnly goto / target are not)
    _slash(rt, "rxp skip")
    assert G.STEN.elements[2].skip is True and G.STEN.elements[1].skip is None and G.R.mapUpdated
    r = _state(rt)[0].rxp
    assert r.kinds == K_WANDER | K_TARGET and r.quests == [(179, Q_ACCEPT, True)]

    # /be rxp next: the sticky wolf step becomes current; counters bump; the wander point carries its radius
    _slash(rt, "rxp next")
    assert G.R.progress == 2
    s3, _ = _state(rt)
    r = s3.rxp
    assert (r.index, r.line, r.sticky, r.stepctr) == (2, 21, True, (ctr0 + 1) % 4096)
    assert r.kinds == K_WANDER | K_MANUAL | K_COMPLETE | K_MOB
    assert r.wp[2] == 50 and near(r.wp[0], -6100.2) and near(r.wp[1], 520.1)
    assert r.pts[0][3] == 1 | 4                      # wander + hidePin
    assert r.tgtctr != tctr0 and r.text == "T:|M:Ragged Young Wolf"   # .target list of the new step is empty
    assert not s3.plates[1].rxp_npc

    _slash(rt, "rxp dump")
    assert "L21" in G.BotEyeDB.rxpDump and "complete:179" in G.BotEyeDB.rxpDump, G.BotEyeDB.rxpDump
    _slash(rt, "rxp load 05-11 Dun Morogh")
    assert list(G.R.calls.values())[-1] == "RestedXP Forever Guide (A)||05-11 Dun Morogh"
    _slash(rt, "rxp load No Such Guide")
    assert "no guide" in _state(rt)[0].msg
    _slash(rt, "rxp off")
    assert not _state(rt)[0].rxp.flag

    # a guide of another group: ordinal 255, and autoload (flag on) switches back to our route
    rt.execute("R.currentGuide = { name = 'x', group = 'Other', key = 'k2', guideId = 0, steps = { STEN } }; R.progress = 1")
    assert _state(rt)[0].rxp.guide == "other"
    G.BotEyeDB.rxp = True
    rt.eval("EVFRAMES.PLAYER_ENTERING_WORLD:GetScript('OnEvent')(EVFRAMES.PLAYER_ENTERING_WORLD, 'PLAYER_ENTERING_WORLD', false)")
    rt.eval("RUN_TIMERS()")
    assert list(G.R.calls.values())[-1] == "RestedXP Forever Guide (A)||01-05 Coldridge Valley"
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def _render(v, bars, rows):
    """Pixels as BotEye.lua layout() places them (1 UI unit = 1 px): block i at (col*3, row*3), 3x3; bar i 128x3 at
    x = (i % 2) * 128, y = rows*3 + (i // 2) * 3, the first fraction*128 px white."""
    import numpy as np
    a = np.zeros((rows * 3 + 8 * 3 + 6, 400, 3), np.uint8)
    for i in range(rows * 64):
        r, c = divmod(i, 64)
        a[r * 3:r * 3 + 3, c * 3:c * 3 + 3] = (v[i] >> 16 & 255, v[i] >> 8 & 255, v[i] & 255)
    for i, f in enumerate(bars):
        y, x = rows * 3 + (i // 2) * 3, (i % 2) * 128
        a[y:y + 3, x:x + 128] = 0
        a[y:y + 3, x:x + round(f * 128)] = 255
    return a


def _eye_on(img):
    """eye.Eye with a fake capture of `img` (no window)."""
    class Cap:
        hwnd = 0

        def grab(self, rows=None):
            return img if rows is None else img[:rows]
    old = eye.Capture, eye.find_window
    eye.Capture, eye.find_window = (lambda h: Cap()), (lambda: 1)
    try:
        return eye.Eye()
    finally:
        eye.Capture, eye.find_window = old


def test_pixels_round_trip_both_layouts():
    """Review 2026-10-14: the bars sit under 4 block rows with the tag and under 3 without (old addon); Eye.raw must
    read the same blocks and bar fractions back from the pixels in both layouts."""
    bars = [(i + 1) / 16 for i in range(eye.NBARS)]
    v = row4(blank(), st=1 | 2, guide=1 + 256, line=18, kinds=K_ACCEPT, text="T:Sten Stoutarm|M:")
    v[7], v[9], v[10] = 100, 50, 3
    got_v, got_bars = _eye_on(_render(v, bars, 4)).raw()
    assert got_v == v and all(abs(a - b) < 1.0 / 128 + 1e-9 for a, b in zip(got_bars, bars)), (got_bars, bars)
    s = decode(got_v, got_bars)
    assert s.rxp is not None and s.rxp.guide == "01-05 Coldridge Valley" and s.hp == round(bars[0] * 100)
    old = blank()[:192] + [0] * 64                     # an addon without row 4: 3 rows, the bars right under them
    old[7] = 100
    e = _eye_on(_render(old, bars, 3))
    got_v, got_bars = e.raw()
    assert e.bar_y0 == 9 and got_v[:192] == old[:192]
    assert all(abs(a - b) < 1.0 / 128 + 1e-9 for a, b in zip(got_bars, bars)), (got_bars, bars)
    assert decode(got_v, got_bars).rxp is None


def test_lua_tag_on_first_frame():
    """Review 2026-10-14: block 192 carries the tag before fill_rxp's first (0.25 s) run, so the bars are never read
    from row 4 right after a /reload."""
    rt = _lua()
    if rt is None:
        print("skip test_lua_tag_on_first_frame (no lupa or no BotEye.lua)")
        return
    rt.eval("FRAMES.BotEyeFrame:GetScript('OnUpdate')(FRAMES.BotEyeFrame, 0.01)")
    strip = rt.eval("STRIP()")
    assert strip[193] == RXP_TAG, hex(strip[193])          # Lua table is 1-based: block 192


def test_lua_autoload_waits_for_guides():
    rt = _lua()
    if rt is None:
        print("skip test_lua_autoload_waits_for_guides (no lupa or no BotEye.lua)")
        return
    G = rt.globals()
    rt.execute("SAVED = R.guides; R.guides = {}")
    _slash(rt, "rxp on")
    assert len(G.R.calls) == 0 and len(G.TIMERS) == 1       # retry scheduled
    rt.execute("R.guides = SAVED")
    rt.eval("RUN_TIMERS()")
    assert list(G.R.calls.values()) == ["RestedXP Forever Guide (A)||01-05 Coldridge Valley"]


def test_lua_fly_takes_the_named_node():
    """/be fly <name> (nb/flight.py): on TAXIMAP_OPENED the reachable node whose name holds it is taken; an unknown
    name is reported with the map's node names."""
    rt = _lua()
    if rt is None:
        print("skip test_lua_fly_takes_the_named_node (no lupa or no BotEye.lua)")
        return
    G = rt.globals()
    rt.execute("""
        TAXI = { {"Ironforge, Dun Morogh", "CURRENT"}, {"Stormwind, Elwynn", "REACHABLE"}, {"Menethil Harbor, Wetlands", "REACHABLE"} }
        TOOK = nil
        NumTaxiNodes = function() return #TAXI end
        TaxiNodeName = function(i) return TAXI[i][1] end
        TaxiNodeGetType = function(i) return TAXI[i][2] end
        TakeTaxiNode = function(i) TOOK = i end
    """)
    fire = "EVFRAMES.TAXIMAP_OPENED:GetScript('OnEvent')(EVFRAMES.TAXIMAP_OPENED, 'TAXIMAP_OPENED')"
    _slash(rt, "fly stormwind")
    rt.eval(fire)
    rt.eval("RUN_TIMERS()")
    assert rt.eval("rawget(_G, 'TOOK')") == 2
    assert "fly Stormwind" in _state(rt)[0].msg
    rt.execute("TOOK = nil")
    _slash(rt, "fly booty bay")
    rt.eval(fire)
    rt.eval("RUN_TIMERS()")
    m = _state(rt)[0].msg
    assert rt.eval("rawget(_G, 'TOOK') == nil") and "fly no node 'booty bay'" in m, m
    rt.eval(fire)                                     # asked once: a later map opening takes nothing
    rt.eval("RUN_TIMERS()")
    assert rt.eval("rawget(_G, 'TOOK') == nil")


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
