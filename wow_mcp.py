"""MCP server (stdio, no dependencies) for driving the WoW Forever client.

Tools: wow_state, wow_screenshot (WoW window only), wow_press, wow_eval,
bot_start / bot_stop / bot_status, bot_log. Everything goes through the nb
package: the BotEye pixel strip for reading, posted keys for input.

Register:  claude mcp add wow -- <python> C:/Users/MadsG/Desktop/wowbot/wow_mcp.py
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
_real_stdout = sys.stdout
sys.stdout = sys.stderr            # stray prints must not corrupt the protocol

PY = sys.executable
STOP = os.path.join(HERE, "nb", "STOP")
LOG = os.path.join(HERE, "nb", "nb.log")
PIDFILE = os.path.join(HERE, "nb", "mcp_bot.pid")

_eye = None
_keys = None


def eye():
    global _eye, _keys
    if _eye is None:
        from nb.eye import Eye
        from nb.win import Keys
        _eye = Eye()
        _keys = Keys(_eye.cap.hwnd, _eye.in_world)
    return _eye


def keys():
    eye()
    return _keys


def _state_dict(s):
    plates = [{"npc": p.npc, "lvl": p.level, "hp": p.hp, "hostile": p.hostile, "friendly": p.friendly,
               "dead": p.dead, "on_me": p.on_me, "target": p.is_target, "token": p.token,
               "sx": p.sx, "sy": p.sy} for p in s.plates]
    return {
        "level": s.level, "xp": f"{s.xp}/{s.xpmax}", "pos": [round(s.x, 1), round(s.y, 1)],
        "facing": round(s.facing, 3), "hp": f"{s.hp}/{s.hpmax}", "mana": f"{s.mana}/{s.manamax}",
        "combat": s.combat, "dead": s.dead, "ghost": s.ghost, "moving": s.moving, "casting": s.casting_now,
        "autoattack": s.autoattack, "has_target": s.has_target,
        "target": {"npc": s.t_npc, "lvl": s.t_level, "hp_pct": round(s.t_hp_pct), "dead": s.t_dead,
                   "on_me": s.t_on_me, "within10": s.t_within10, "bolt_range": s.in_range(2)},
        "bags_free": s.bags_free, "money": s.money, "err": [s.err, s.errn], "msg": s.msg,
        "ui_bits": s.ui, "actions": s.actions, "luaerrs": s.luaerrs,
        "quests": [{"id": q.qid, "complete": q.complete, "counts": list(q.counts)} for q in s.quests],
        "plates": plates,
    }


def _png(rgb) -> bytes:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def t_state(a):
    s = eye().read()
    if s is None:
        return text("BotEye strip not visible (login/loading screen?)")
    return text(json.dumps(_state_dict(s), indent=1))


def t_screenshot(a):
    import io
    from PIL import Image
    e = eye()
    div = int(a.get("downscale", 2)) or 1
    full = e.cap.grab()
    im = Image.fromarray(full[::div, ::div, :3])
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return [{"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/jpeg"}]


def t_press(a):
    k = keys()
    for key in a["keys"] if isinstance(a.get("keys"), list) else [a["key"]]:
        k.press(key, float(a.get("hold", 0.04)))
        time.sleep(float(a.get("gap", 0.1)))
    return text("pressed")


def t_eval(a):
    from nb.ev import ev
    return text(ev(a["lua"], eye(), keys(), wait=float(a.get("wait", 1.5))))


def _bot_pid():
    try:
        pid = int(open(PIDFILE).read())
    except Exception:
        return None
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return pid if str(pid) in out else None


def t_bot_start(a):
    if _bot_pid():
        return text("bot already running")
    if os.path.exists(STOP):
        os.remove(STOP)
    goal = str(int(a.get("goal_level", 10)))
    p = subprocess.Popen([PY, "-u", "-m", "nb.bot", goal], cwd=HERE,
                         stdout=open(os.path.join(HERE, "nb", "run.out"), "w"),
                         stderr=open(os.path.join(HERE, "nb", "run.err"), "w"),
                         creationflags=0x08000000)
    open(PIDFILE, "w").write(str(p.pid))
    return text(f"bot started pid {p.pid}")


def t_bot_stop(a):
    open(STOP, "w").close()
    return text("STOP file written; the bot exits at the next loop")


def t_bot_status(a):
    pid = _bot_pid()
    tail = ""
    try:
        tail = "".join(open(LOG, encoding="utf-8", errors="replace").readlines()[-5:])
    except Exception:
        pass
    return text(f"running pid {pid}\n{tail}" if pid else f"not running (by this server)\n{tail}")


def t_bot_log(a):
    n = int(a.get("lines", 40))
    pat = a.get("grep")
    lines = open(LOG, encoding="utf-8", errors="replace").readlines()
    if pat:
        lines = [l for l in lines if pat in l]
    return text("".join(lines[-n:]))


def text(s):
    return [{"type": "text", "text": s}]


TOOLS = {
    "wow_state": (t_state, "Decoded BotEye strip: position, hp/mana, combat, target, quests, plates, last message/error.", {}),
    "wow_screenshot": (t_screenshot, "Screenshot of the WoW window only (not the monitor). downscale=2 by default.",
                       {"downscale": {"type": "integer"}}),
    "wow_press": (t_press, "Press key(s) in the WoW window (names from nb/win.py VK: w, tab, f1..f12, num+, 1-0, esc, ...). Only sent while the strip is visible.",
                  {"key": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}},
                   "hold": {"type": "number"}, "gap": {"type": "number"}}),
    "wow_eval": (t_eval, "Evaluate a Lua expression in the client via BotEye (/be eval); returns the printed result. Use single-quoted Lua strings.",
                 {"lua": {"type": "string"}, "wait": {"type": "number"}}),
    "bot_start": (t_bot_start, "Start the nb bot (goal_level default 10).", {"goal_level": {"type": "integer"}}),
    "bot_stop": (t_bot_stop, "Ask the nb bot to stop (writes nb/STOP).", {}),
    "bot_status": (t_bot_status, "Is the bot running + last 5 log lines.", {}),
    "bot_log": (t_bot_log, "Tail nb/nb.log (lines=40, grep=substring).", {"lines": {"type": "integer"}, "grep": {"type": "string"}}),
}


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    _real_stdout.write(json.dumps(msg) + "\n")
    _real_stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method, id_, params = req.get("method"), req.get("id"), req.get("params") or {}
        if id_ is None:
            continue                      # notification
        try:
            if method == "initialize":
                reply(id_, {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                            "capabilities": {"tools": {}}, "serverInfo": {"name": "wow", "version": "0.1"}})
            elif method == "tools/list":
                reply(id_, {"tools": [{"name": n, "description": d,
                                       "inputSchema": {"type": "object", "properties": props}}
                                      for n, (_, d, props) in TOOLS.items()]})
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if name not in TOOLS:
                    reply(id_, error={"code": -32602, "message": f"unknown tool {name}"})
                    continue
                try:
                    reply(id_, {"content": TOOLS[name][0](args)})
                except Exception as e:
                    reply(id_, {"content": text(f"error: {type(e).__name__}: {e}"), "isError": True})
            elif method == "ping":
                reply(id_, {})
            else:
                reply(id_, error={"code": -32601, "message": f"unknown method {method}"})
        except Exception as e:
            reply(id_, error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()
