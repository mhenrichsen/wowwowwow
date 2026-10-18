"""Remote bot monitor: a page with live window captures (PrintWindow, no input sent) of the 4 WoW instances, plus
level / hp / mana / position and the last log lines of each bot.

The windows are streamed as MJPEG (/<token>/stream/N.mjpg, multipart/x-mixed-replace: every browser shows it as a
live <img>, and it passes any HTTP tunnel); /<token>/shot/N.jpg stays for single frames.

    python monitor_web.py [port]          -> http://127.0.0.1:8765/<token>/

The token (random, kept in monitor_token.txt) is the only access control: anyone with the full URL can see the page.
"""
from __future__ import annotations

import html
import io
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from PIL import Image                          # noqa: E402

from nb.eye import Eye                         # noqa: E402
from nb.multi import find_instance_window      # noqa: E402
from nb.win import Capture                     # noqa: E402

INSTS = (1, 2, 3, 4)
try:                                     # the bots' current characters (nb/login.py)
    from nb.login import NAMES as _LN
    NAMES = {i: n[0] for i, n in _LN.items()}
except Exception:
    NAMES = {1: "Bot 1", 2: "Bot 2", 3: "Bot 3", 4: "Bot 4"}
LOGS = {1: "nb/nb.log", 2: "nb/inst2/nb.log", 3: "nb/inst3/nb.log", 4: "nb/inst4/nb.log"}
SHOT_TTL = 2.0
STREAM_FPS = 6.0          # frames per second per stream (?fps= overrides, 1..15)
CRLF = bytes((13, 10))
STREAM_MAX_S = 600        # a stream ends after this long (the page reconnects): no connections left over from old tabs
STREAM_W = 960            # px: stream frames are scaled to this width (?w= overrides, 320..1704)
TOKEN_FILE = os.path.join(ROOT, "monitor_token.txt")

_lock = threading.Lock()
_cache: dict = {}          # inst -> (t, jpeg bytes)
_caps: dict = {}           # inst -> (hwnd, Capture, Eye)


def token() -> str:
    try:
        t = open(TOKEN_FILE).read().strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(18)
    open(TOKEN_FILE, "w").write(t)
    return t


def _cap(inst):
    c = _caps.get(inst)
    if c is None:
        h = find_instance_window(inst)
        c = (h, Capture(h), Eye(h))
        _caps[inst] = c
    return c


def _jpeg(inst, width, quality) -> bytes | None:
    _h, cap, _e = _cap(inst)
    a = cap.grab()
    im = Image.fromarray(a[:, :, :3])                   # Capture.grab returns RGB order already
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def frame(inst, width, ttl, quality=60) -> bytes | None:
    """One JPEG of the window, shared by every viewer within ttl s (one capture per window per frame)."""
    key = (inst, width, quality)
    with _lock:
        t, b = _cache.get(key, (0.0, None))
        if b and time.time() - t < ttl:
            return b
        try:
            b = _jpeg(inst, width, quality)
        except Exception:
            _caps.pop(inst, None)
            return None
        _cache[key] = (time.time(), b)
        return b


def mosaic(width, ttl, quality=60) -> bytes | None:
    """All windows in one 2x2 JPEG (one stream, one connection: browsers allow ~6 per site and 4 separate MJPEG
    streams plus leftovers from reloads left windows blank)."""
    key = ("all", width, quality)
    with _lock:
        t, b = _cache.get(key, (0.0, None))
        if b and time.time() - t < ttl:
            return b
        tiles = []
        for inst in INSTS:
            try:
                _h, cap, _e = _cap(inst)
                tiles.append(Image.fromarray(cap.grab()[:, :, :3]))
            except Exception:
                _caps.pop(inst, None)
                tiles.append(None)
        tw = width // 2
        th = max((round(im.height * tw / im.width) for im in tiles if im is not None), default=tw * 657 // 1704)
        out = Image.new("RGB", (tw * 2, th * 2))
        for k, im in enumerate(tiles):
            if im is not None:
                out.paste(im.resize((tw, th), Image.BILINEAR), ((k % 2) * tw, (k // 2) * th))
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=quality)
        b = buf.getvalue()
        _cache[key] = (time.time(), b)
        return b


def shot(inst) -> bytes | None:
    return frame(inst, 1100, SHOT_TTL, 70)


def status(inst) -> str:
    try:
        with _lock:
            _h, _c, eye = _cap(inst)
            s = eye.read()
        if not s:
            return "no strip"
        st = "DEAD" if s.dead else "ghost" if s.ghost else "combat" if s.combat else "ok"
        return (f"level {s.level} | hp {s.hp_pct:.0f}% | mana {s.mana_pct:.0f}% | {st} | "
                f"({s.x:.0f}, {s.y:.0f})")
    except Exception as e:
        _caps.pop(inst, None)
        return f"no window ({e.__class__.__name__})"


def log_tail(inst, n=6) -> list[str]:
    try:
        with open(os.path.join(ROOT, LOGS[inst]), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        lines = [l for l in lines if "steer tele" not in l and "  fight:" not in l]
        return [l[:170] for l in lines[-n:]]
    except OSError:
        return []


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bot Monitor</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mut:#5b6474;--bd:#dde1e7}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--mut:#98a2b3;--bd:#2a313d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0}#t{color:var(--mut);font-size:12px}
#mode{font:inherit;padding:2px 8px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg)}
#v{display:block;width:calc(100% - 32px);margin:0 16px;background:#000;border-radius:8px;aspect-ratio:1704/657;object-fit:contain;cursor:zoom-in}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px;padding:12px 16px 16px}
.c{background:var(--card);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.h{padding:8px 10px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.n{font-weight:600}.s{color:var(--mut);font-size:12px}
pre{margin:0;padding:8px 10px;font:11px/1.35 ui-monospace,Consolas,monospace;color:var(--mut);white-space:pre-wrap;
word-break:break-all;max-height:110px;overflow:auto;border-top:1px solid var(--bd)}
.big{position:fixed;inset:0;background:#000;display:flex;align-items:center;justify-content:center;z-index:9}
.big img{max-width:100vw;max-height:100vh}
</style></head><body>
<header><h1>Bot Monitor</h1><select id="mode" aria-label="video mode">
<option value="live">Live</option><option value="hd">Live HD</option><option value="low">Low bandwidth</option>
<option value="snap">Snapshots (any tunnel)</option>
</select><span id="t"></span></header>
<img id="v" alt="all bots: 1 top left, 2 top right, 3 bottom left, 4 bottom right">
<main>__CARDS__</main>
<script>
const insts=__INSTS__;
const modes={live:'fps=4&w=1280',hd:'fps=3&w=1704',low:'fps=1&w=960'};
let mode='live';
try{mode=localStorage.getItem('mode')||'live'}catch(e){}
const sel=document.getElementById('mode'); sel.value=mode;
const v=document.getElementById('v');
// Snapshots: one JPEG per second, for tunnels that hold back a never-ending response (localhost.run showed only
// black for the MJPEG stream). Live falls back to it when no frame arrives within 6 s.
let snapT=null, seen=false;
function snap(){ const i=new Image(); i.onload=()=>{v.src=i.src}; i.src='shot/all.jpg?w=1280&t='+Date.now(); }
function connect(){
  clearInterval(snapT); snapT=null;
  if(mode==='snap'){ snap(); snapT=setInterval(snap,1000); return; }
  seen=false; v.src='stream/all.mjpg?'+modes[mode]+'&t='+Date.now();
  setTimeout(()=>{ if(!seen && mode!=='snap'){ mode='snap'; sel.value='snap'; connect(); } }, 6000);
}
v.onload=()=>{seen=true};
v.onerror=()=>{ if(mode!=='snap') setTimeout(connect,3000) };
sel.onchange=()=>{mode=sel.value; try{localStorage.setItem('mode',mode)}catch(e){} connect()};
connect(); setInterval(connect, 9*60*1000);          // the server ends a stream after 10 min
async function tick(){
  const now=Date.now();
  try{const r=await fetch('status?'+now); const d=await r.json();
    for(const i of insts){document.getElementById('s'+i).textContent=d[i].status;
      document.getElementById('l'+i).textContent=d[i].log.join('\\n');}
    document.getElementById('t').textContent='updated '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('t').textContent='offline: '+e}
}
document.addEventListener('click',e=>{
  const b=document.querySelector('.big'); if(b){b.remove();return}
  if(e.target.id==='v'){const d=document.createElement('div');d.className='big';
    d.innerHTML='<img src="'+e.target.src+'">';document.body.appendChild(d)}});
tick(); setInterval(tick,4000);
</script></body></html>"""


def page() -> bytes:
    cards = "".join(
        f'<section class="c"><div class="h"><span class="n">Bot {i} - {html.escape(NAMES[i])}</span>'
        f'<span class="s" id="s{i}">...</span></div>'
        f'<pre id="l{i}"></pre></section>' for i in INSTS)
    return PAGE.replace("__CARDS__", cards).replace("__INSTS__", str(list(INSTS))).encode()


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if "/shot/" in self.path or "/status" in self.path or "/stream/" in self.path:
            return
        ua = self.headers.get("User-Agent", "")[:60]
        sys.stdout.write(f"{time.strftime('%H:%M:%S')} {self.command} {self.path[:80]} -> {fmt % a} | {ua}\n")
        sys.stdout.flush()

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _q(self, name, lo, hi, default):
        from urllib.parse import parse_qs, urlparse
        try:
            v = float(parse_qs(urlparse(self.path).query).get(name, [default])[0])
        except ValueError:
            v = default
        return max(lo, min(hi, v))

    def _stream(self, inst):
        """MJPEG: one JPEG part per frame until the viewer goes away."""
        fps = self._q("fps", 1, 15, STREAM_FPS)
        width = int(self._q("w", 320, 1704, STREAM_W))
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        dt = 1.0 / fps
        end = time.time() + STREAM_MAX_S
        try:
            while time.time() < end:
                t0 = time.time()
                b = mosaic(width, dt * 0.9) if inst == 0 else frame(inst, width, dt * 0.9)
                if b:
                    self.wfile.write(CRLF.join((b"--frame", b"Content-Type: image/jpeg",
                                                b"Content-Length: " + str(len(b)).encode(), b"", b)) + CRLF)
                    self.wfile.flush()
                time.sleep(max(0.0, dt - (time.time() - t0)))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        pre = f"/{TOKEN}/"
        if path == pre[:-1]:                     # a link pasted without its trailing slash: send it to the page
            self.send_response(301)
            self.send_header("Location", pre)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not path.startswith(pre):
            return self._send(404, b"not found", "text/plain")
        rest = path[len(pre):]
        if rest == "":
            return self._send(200, page(), "text/html; charset=utf-8")
        if rest == "status":
            import json
            d = {i: {"status": status(i), "log": log_tail(i)} for i in INSTS}
            return self._send(200, json.dumps(d).encode(), "application/json")
        if rest.startswith("stream/") and rest.endswith(".mjpg"):
            if rest[7:-5] == "all":
                return self._stream(0)
            try:
                inst = int(rest[7:-5])
            except ValueError:
                inst = 0
            if inst not in INSTS:
                return self._send(404, b"no such window", "text/plain")
            return self._stream(inst)
        if rest == "shot/all.jpg":
            b = mosaic(int(self._q("w", 320, 1704, STREAM_W)), 0.5)
            if b:
                return self._send(200, b, "image/jpeg")
            return self._send(503, b"no frame", "text/plain")
        if rest.startswith("shot/") and rest.endswith(".jpg"):
            try:
                inst = int(rest[5:-4])
            except ValueError:
                inst = 0
            if inst in INSTS:
                b = shot(inst)
                if b:
                    return self._send(200, b, "image/jpeg")
            return self._send(404, b"no capture", "text/plain")
        return self._send(404, b"not found", "text/plain")


TOKEN = token()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"http://127.0.0.1:{port}/{TOKEN}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
