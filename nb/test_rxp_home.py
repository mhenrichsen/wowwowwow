"""Offline test for BotEye's .home (hearthstone) handling in RXP mode: python -m nb.test_rxp_home

2026-10-14 21:35 (nb/run.out): step "05-11 Dun Morogh L177 #15 kinds=home" at Innkeeper Belm, 4 talks, the gossip
opened every time (ui=8193) but no option was picked and no CONFIRM_BINDER popup came: RXP's .home element frame never
gets GOSSIP_SHOW on this client (its RegisterEvent loop, GuideWindow.lua:287-299, stops at an unknown event). The human
(human/20261014_1959_inst4.jsonl t=1792002933.8-936.4) went gossip -> StaticPopup1 -> "... is now your home.".
BotEye now picks the binder option, confirms the Binder interaction like RXP (ConfirmationInteraction + Clear) and
completes the step's .home element on the bind message. Runs the real BotEye.lua in lupa; sends no keys.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import test_rxp_eye as T            # noqa: E402
from nb import test_rxp_handlers as H       # noqa: E402

INN = r"""
GOSSIP_OPTS = { { gossipOptionID = 93041504, icon = 132053, name = "Trick or Treat!" },
                { gossipOptionID = 93041505, icon = ICON, name = "Make this inn my home." },
                { gossipOptionID = 93041506, icon = 132060, name = "Let me browse your goods." },
                { gossipOptionID = 93041507, icon = 132053, name = "What can I do at an inn?" } }
HOME_EL = { tag = "home", text = "Set your Hearthstone to Thunderbrew Distillery" }
table.insert(STEN.elements, HOME_EL)
R.SetElementComplete = function(e) e.completed = true; e.skip = true end
"""


def _inn(rt, icon):
    rt.globals().ICON = icon
    rt.execute(INN)


def test_home_pick_confirm_complete():
    rt = H._lua()
    if rt is None:
        return H._skip("test_home_pick_confirm_complete")
    G = rt.globals()
    H._rxp_on(rt)
    _inn(rt, 132052)
    # the gossip opens; RXP's pick did not close it: BotEye picks "Make this inn my home." 0.3 s later
    H._ev(rt, "GOSSIP_SHOW")
    assert H._n(rt, "SelectOption") == 0 and list(G.DELAYS.values()) == [0.3]
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "SelectOption") == 1 and G.LASTARG("SelectOption") == 93041505
    assert "gossip bind" in T._state(rt)[0].msg
    # the Binder interaction (StaticPopup CONFIRM_BINDER): confirmed through the interaction manager, popup hidden
    rt.eval("RESET()")
    H._ev(rt, "PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 5)      # a merchant interaction: not ours
    assert len(G.TIMERS) == 0
    H._ev(rt, "PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 20)
    assert H._n(rt, "ConfirmationInteraction") == 0 and list(G.DELAYS.values()) == [0.3]
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "ConfirmationInteraction") == 1 and G.LASTARG("ConfirmationInteraction") == 20
    assert H._n(rt, "ClearInteraction") == 1 and G.LASTARG("ClearInteraction") == 20
    # the server's message: the step's .home element is complete (the kind bit drops for rxp_follow)
    H._ev(rt, "CHAT_MSG_SYSTEM", "Thunderbrew Distillery is now your home.")
    assert G.HOME_EL.completed and "hearth bound" in T._state(rt)[0].msg
    # the next visit (the step's .vendor): no second bind pick
    rt.eval("RESET()")
    G.HOME_EL.completed = False
    H._ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "SelectOption") == 0
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def test_home_text_fallback_and_rxp_first():
    rt = H._lua()
    if rt is None:
        return H._skip("test_home_text_fallback_and_rxp_first")
    G = rt.globals()
    H._rxp_on(rt)
    _inn(rt, 4675702)                         # another binder icon on this client: the text still finds it
    H._ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "SelectOption") == 1 and G.LASTARG("SelectOption") == 93041505
    # RXP picked first (the gossip closed before our 0.3 s): nothing from BotEye
    rt.eval("RESET()")
    H._ev(rt, "GOSSIP_SHOW")
    G.GossipFrame._shown = False
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "SelectOption") == 0
    G.GossipFrame._shown = True
    # RXP confirmed and the bind landed before our 0.3 s: no second confirm
    H._ev(rt, "PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 20)
    H._ev(rt, "HEARTHSTONE_BOUND")
    G.NOW = G.NOW + 0.3
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "ConfirmationInteraction") == 0 and G.HOME_EL.completed
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def test_home_bind_hearthstone_wording():
    # this client's text (inst2 06:25:24 "I:bind: no binder option in 2 [Bind your hearthstone to this inn.|...]")
    rt = H._lua()
    if rt is None:
        return H._skip("test_home_bind_hearthstone_wording")
    G = rt.globals()
    H._rxp_on(rt)
    _inn(rt, 4675702)
    rt.execute('GOSSIP_OPTS[2].name = "Bind your hearthstone to this inn."')
    H._ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "SelectOption") == 1 and G.LASTARG("SelectOption") == 93041505
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def test_binder_popup_not_ours():
    rt = H._lua()
    if rt is None:
        return H._skip("test_binder_popup_not_ours")
    G = rt.globals()
    H._rxp_on(rt)                             # Sten: no .home in the step, no bind asked
    H._ev(rt, "PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 20)
    H._ev(rt, "CONFIRM_BINDER")
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "ConfirmationInteraction") == 0
    T._slash(rt, "want bind")                 # the bot asked (/be want bind): confirmed
    H._ev(rt, "CONFIRM_BINDER")
    rt.eval("RUN_TIMERS()")
    assert H._n(rt, "ConfirmationInteraction") == 1
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
