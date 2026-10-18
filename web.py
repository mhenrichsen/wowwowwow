"""Phone-friendly, read-only status page for the bot.

Serves one HTML page plus /status.json (the file the bot already writes).
Every request must carry the access key (?key=...), so a URL that leaks
without it shows nothing.

    python web.py                 # http://127.0.0.1:8765/?key=<printed key>
    python web.py --host 0.0.0.0  # also reachable from other devices
    python web.py --control       # also show Start / Stop buttons

--control is off by default on purpose. The read-only page is safe to put
behind a public quick tunnel; one that can start processes is a different
thing to leave on the internet, even behind a key. The actions it exposes
are fixed (start, stop, restart, this bot, these arguments) so it cannot be
talked into running anything else.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import time

import control
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "status.json")
KEY_FILE = os.path.join(HERE, "web_key.txt")

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WoW Bot</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--dim:#6b7280;--line:#e5e7eb;
--hp:#2f9e44;--mana:#3b6fd8;--xp:#8b5cf6;--bad:#d9480f;--ok:#2f9e44}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--fg:#e8eaed;
--dim:#9aa0a6;--line:#2a2e35}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 2px}.state{color:var(--dim);margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.bar{height:22px;border-radius:6px;background:var(--line);position:relative;overflow:hidden;margin:6px 0}
.bar i{position:absolute;inset:0 auto 0 0;border-radius:6px}
.bar span{position:relative;display:block;text-align:center;font-size:13px;font-weight:600;line-height:22px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.k{color:var(--dim);font-size:12px}.v{font-size:18px;font-weight:650}
pre{margin:0;white-space:pre-wrap;font:12px/1.45 ui-monospace,Consolas,monospace;color:var(--dim)}
.btns{display:flex;gap:8px}
button{flex:1;padding:11px 8px;font:600 15px system-ui,sans-serif;color:var(--fg);
background:var(--bg);border:1px solid var(--line);border-radius:9px;cursor:pointer}
button:active{transform:translateY(1px)}button[disabled]{opacity:.45;cursor:default}
#bstart{color:var(--ok)}#bstop{color:var(--bad)}
.cols{display:grid;gap:12px;grid-template-columns:minmax(0,1fr);
grid-template-areas:"vit" "guide" "stats" "ctl" "log"}
@media (min-width:900px){.cols{grid-template-columns:minmax(0,5fr) minmax(0,7fr);
grid-template-rows:auto auto auto 1fr;
grid-template-areas:"vit guide" "stats guide" "ctl guide" "log guide"}}
.cols>.card{margin:0}#vit{grid-area:vit}#stats{grid-area:stats}#ctl{grid-area:ctl}
#logc{grid-area:log}#gcard{grid-area:guide;align-self:start}
.gtitle{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.bar.thin{height:6px;margin:6px 0 12px}
.gtext{margin:10px 0 12px;padding:10px 12px;border-left:3px solid var(--xp);
background:var(--bg);border-radius:6px;font-size:14px}
.acts{list-style:none;margin:0;padding:0}
.acts li{padding:9px 11px;border-radius:9px;margin-bottom:7px;border:1px solid var(--line)}
.acts li.current{border-color:var(--xp);background:color-mix(in srgb,var(--xp) 12%,transparent)}
.acts li.current .al{font-weight:650}
.acts li.done .al,.acts li.skipped .al{color:var(--dim)}
.acts li.skipped .al{text-decoration:line-through}
.ic{display:inline-block;width:20px;font-weight:700}
.acts li.done .ic{color:var(--ok)}.acts li.current .ic{color:var(--xp)}
.acts li.todo .ic,.acts li.skipped .ic{color:var(--dim)}
.prog{margin:6px 0 0 20px}.prog .k{margin-top:4px}
.prog .bar{height:18px;margin:5px 0}
.prog .bar span{font-size:12px;line-height:18px}
.next{margin:6px 0 0;padding-left:22px;color:var(--dim);font-size:14px}.next li{margin:4px 0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
</style></head><body><main>
<h1 id="name">WoW Bot</h1><div class="state"><span class="dot" id="dot"></span><span id="state">loading…</span></div>
<div class="cols">
<div class="card" id="vit">
 <div class="bar"><i id="hpb" style="background:var(--hp)"></i><span id="hpt"></span></div>
 <div class="bar"><i id="mpb" style="background:var(--mana)"></i><span id="mpt"></span></div>
 <div class="bar"><i id="xpb" style="background:var(--xp)"></i><span id="xpt"></span></div>
</div>
<div class="card grid" id="stats">
 <div><div class="k">XP / hour</div><div class="v" id="xph">–</div></div>
 <div><div class="k">Next level in</div><div class="v" id="eta">–</div></div>
 <div><div class="k">Kills</div><div class="v" id="kills">–</div></div>
 <div><div class="k">Deaths</div><div class="v" id="deaths">–</div></div>
 <div><div class="k">Money</div><div class="v" id="money">–</div></div>
 <div><div class="k">Running for</div><div class="v" id="up">–</div></div>
</div>
<div class="card" id="ctl" hidden>
 <div class="k" style="margin-bottom:8px">Control</div>
 <div class="btns">
  <button id="bstart">Start</button>
  <button id="bstop">Stop</button>
  <button id="brestart">Restart</button>
 </div>
 <div class="k" id="ctlmsg" style="margin-top:8px"></div>
</div>
<div class="card" id="logc"><div class="k" style="margin-bottom:6px">Recent activity</div><pre id="log"></pre></div>
<div class="card" id="gcard" hidden>
 <div class="gtitle"><div class="v" id="gname"></div><div class="k" id="gchain"></div></div>
 <div class="bar thin"><i id="gchb" style="background:var(--dim)"></i></div>
 <div class="bar"><i id="gstb" style="background:var(--xp)"></i><span id="gstt"></span></div>
 <div class="gtext" id="gtext"></div>
 <ul class="acts" id="gacts"></ul>
 <div class="k" id="gnextk" style="margin-top:12px">Coming up</div>
 <ol class="next" id="gnext"></ol>
</div>
</div>
<div class="k" id="age" style="text-align:center"></div>
</main><script>
const key=new URLSearchParams(location.search).get("key")||"";
const $=id=>document.getElementById(id);
const dur=s=>{s=Math.round(s);if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"m";
 return Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m"};
const money=c=>(c>=10000?Math.floor(c/10000)+"g ":"")+(c>=100?Math.floor(c/100)%100+"s ":"")+c%100+"c";
function bar(b,t,v,m,label){b.style.width=(m?Math.min(100,100*v/m):0)+"%";t.textContent=label+" "+v+" / "+m}
const el=(tag,cls,text)=>{const e=document.createElement(tag);
 if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const ICON={done:"\u2713",current:"\u25B6",todo:"\u25CB",skipped:"\u2933"};
function guide(g){
 $("gcard").hidden=!g; if(!g)return;
 $("gname").textContent=g.guide;
 $("gchain").textContent=g.chain_pos?"guide "+g.chain_pos+" of "+g.chain_len:"";
 $("gchb").style.width=(g.chain_pos?100*g.chain_pos/g.chain_len:0)+"%";
 bar($("gstb"),$("gstt"),g.step,g.steps,"Step");
 $("gtext").textContent=g.text||""; $("gtext").hidden=!g.text;
 $("gacts").replaceChildren(...(g.actions||[]).map(a=>{
  const li=el("li",a.state); li.append(el("span","ic",ICON[a.state]||"\u2022"),el("span","al",a.label));
  if(a.progress&&a.progress.length){const p=el("div","prog");
   for(const o of a.progress){
    if(o.need==null){p.append(el("div","k",o.label));continue}
    const b=el("div","bar"),i=el("i");
    i.style.width=Math.min(100,100*o.have/Math.max(1,o.need))+"%";
    i.style.background=o.have>=o.need?"var(--ok)":"var(--xp)";
    b.append(i,el("span",null,o.label+"  "+o.have+" / "+o.need)); p.append(b)}
   li.append(p)}
  return li}));
 $("gnext").replaceChildren(...(g.next||[]).map(t=>el("li",null,t)));
 $("gnextk").hidden=!(g.next&&g.next.length);
}
async function tick(){
 try{
  const r=await fetch("status.json?key="+encodeURIComponent(key),{cache:"no-store"});
  if(!r.ok){$("state").textContent=r.status==403?"wrong or missing key":"error "+r.status;return}
  const s=await r.json(); const age=Date.now()/1000-s.ts; const live=s.running&&age<10;
  $("name").textContent=(s.name||"?")+" · level "+s.level;
  let st=live?s.state:"not running";
  if(live&&s.state=="fighting"&&s.target)st="fighting "+s.target.name+" ("+s.target.hp_pct+"%)";
  if(s.ghost)st="dead - running back";
  $("state").textContent=st; $("dot").style.background=live?"var(--ok)":"var(--bad)";
  bar($("hpb"),$("hpt"),s.hp,s.max_hp,"HP"); bar($("mpb"),$("mpt"),s.mana,s.max_mana,"Mana");
  bar($("xpb"),$("xpt"),s.xp,s.next_xp,"XP");
  $("xph").textContent=s.xp_per_hour?s.xp_per_hour.toLocaleString():"measuring";
  $("eta").textContent=s.xp_per_hour?dur((s.next_xp-s.xp)/s.xp_per_hour*3600):"–";
  $("kills").textContent=s.kills+(s.looted!=null?" ("+s.looted+" looted)":"");
  $("deaths").textContent=s.deaths; $("money").textContent=money(s.money||0);
  $("up").textContent=dur(s.uptime||0);
  $("log").textContent=(s.log||[]).slice(-12).reverse().join("\n");
  guide(live?s.guide:null);
  $("age").textContent="updated "+dur(Math.max(0,age))+" ago";
 }catch(e){$("state").textContent="PC unreachable"; $("dot").style.background="var(--bad)"}
}
let busy=false;
async function act(what){
 if(busy)return; busy=true; $("ctlmsg").textContent=what+"…";
 for(const b of ["bstart","bstop","brestart"])$(b).disabled=true;
 try{
  const r=await fetch("control?key="+encodeURIComponent(key),{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({action:what})});
  const j=await r.json(); $("ctlmsg").textContent=j.message||("error "+r.status);
 }catch(e){$("ctlmsg").textContent="could not reach the PC"}
 busy=false; tick();
}
$("bstart").onclick=()=>act("start");
$("brestart").onclick=()=>act("restart");
let armed=0;
// tap twice rather than confirm(): a modal dialog is easy to mis-tap past
// on a phone, and it blocks the page while the status keeps refreshing
$("bstop").onclick=()=>{
 if(Date.now()-armed<4000){armed=0;$("bstop").textContent="Stop";act("stop");return}
 armed=Date.now(); $("bstop").textContent="Tap to confirm";
 $("ctlmsg").textContent="stops after the current action";
 setTimeout(()=>{if(armed){armed=0;$("bstop").textContent="Stop"}},4000);
};
async function ctltick(){
 try{
  const r=await fetch("control?key="+encodeURIComponent(key),{cache:"no-store"});
  if(!r.ok)return; const c=await r.json();
  if(!c.enabled)return; $("ctl").hidden=false;
  if(busy)return;
  $("bstart").disabled=c.running; $("bstop").disabled=!c.running;
  $("brestart").disabled=false;
  if(!$("ctlmsg").textContent)$("ctlmsg").textContent=c.args;
 }catch(e){}
}
tick(); ctltick(); setInterval(tick,3000); setInterval(ctltick,3000);
</script></body></html>"""


def load_key() -> str:
    if os.path.exists(KEY_FILE):
        return open(KEY_FILE).read().strip()
    key = secrets.token_urlsafe(18)
    open(KEY_FILE, "w").write(key)
    return key


class Handler(http.server.BaseHTTPRequestHandler):
    key = ""
    control = False

    def _authed(self, u) -> bool:
        return secrets.compare_digest(
            parse_qs(u.query).get("key", [""])[0], self.key)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed(u):
            return self._send(403, "text/plain", b"forbidden")
        if u.path != "/control":
            return self._send(404, "text/plain", b"not found")
        if not self.control:
            return self._json(403, {"ok": False,
                                    "message": "controls are off (start web.py --control)"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            action = json.loads(self.rfile.read(n) or b"{}").get("action")
        except Exception:
            return self._json(400, {"ok": False, "message": "bad request"})
        # a fixed table, not a lookup by name: the request picks one of
        # three things to do, never what to run
        if action == "start":
            ok, msg = control.start()
        elif action == "stop":
            ok, msg = control.stop()
        elif action == "restart":
            ok, msg = control.restart()
        else:
            return self._json(400, {"ok": False, "message": "unknown action"})
        return self._json(200, {"ok": ok, "message": msg, **control.state()})

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def do_GET(self):
        u = urlparse(self.path)
        if not self._authed(u):
            return self._send(403, "text/plain", b"forbidden")
        if u.path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())
        if u.path == "/control":
            st = control.state() if self.control else {"running": False}
            return self._json(200, {"enabled": self.control, **st})
        if u.path == "/status.json":
            try:
                st = json.load(open(STATUS, encoding="utf-8"))
            except Exception:
                st = {"running": False, "ts": time.time(), "state": "no status yet",
                      "level": "?", "hp": 0, "max_hp": 0, "mana": 0, "max_mana": 0,
                      "xp": 0, "next_xp": 0, "kills": 0, "deaths": 0}
            st.pop("units", None)          # positions of mobs: not needed on the phone
            st.pop("pos", None)
            return self._send(200, "application/json", json.dumps(st).encode())
        self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--control", action="store_true",
                    help="show Start/Stop buttons (off by default)")
    a = ap.parse_args()
    Handler.key = load_key()
    Handler.control = a.control
    print(f"status page: http://{a.host}:{a.port}/?key={Handler.key}", flush=True)
    print("controls: " + ("ON" if a.control else "off (--control to enable)"), flush=True)
    http.server.ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
