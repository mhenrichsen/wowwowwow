"""Navmesh viewer: pick two points on the map, see the path the bot would take.

    python navview.py            (http://localhost:8765)

Draws the mmap polygons round the view (green ground, blue water, orange
steep), the vmap doodad boxes the path fixer walks round (red), the learned
holes from data/obstacles.json (magenta) and the aggro zones (yellow).
Left-click sets the start, right-click the end; the path is computed with the
same navmesh.py the bot uses, with the settings from the panel applied to the
module before each run (they do not touch the running bot).
"""
import json
import math
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import navmesh  # noqa: E402

try:
    import vmap  # noqa: E402
except Exception:
    vmap = None

PORT = int(os.environ.get("NAVVIEW_PORT", "8765"))
LOCK = threading.Lock()
NAV = None


def nav():
    global NAV
    if NAV is None:
        t = time.time()
        NAV = navmesh.NavMesh()
        print(f"navmesh: {len(NAV.tiles)} tiles in {time.time() - t:.1f}s")
    return NAV


DEFAULTS = {
    "water_cost": navmesh.WATER_COST,
    "steep_cost": navmesh.STEEP_COST,
    "danger_cost": navmesh.DANGER_COST,
    "agent_radius": navmesh.AGENT_RADIUS,
    "max_nodes": 200000,
    "use_holes": True,
    "hole_poly_max": navmesh.HOLE_POLY_MAX,
    "avoid_doodads": True,
    "doodad_max_height": navmesh.DOODAD_MAX_HEIGHT,
    "doodad_clearance": navmesh.DOODAD_CLEARANCE,
    "merge_cap": navmesh.MERGE_CAP,
    "max_climb": navmesh.MAX_CLIMB,
    "uphill_cost": navmesh.UPHILL_COST,
    "max_fixes": 12,
    "use_dangers": False,
    "level": 14,
    "faction": 1,
}


def apply_settings(s):
    navmesh.WATER_COST = float(s.get("water_cost", DEFAULTS["water_cost"]))
    navmesh.STEEP_COST = float(s.get("steep_cost", DEFAULTS["steep_cost"]))
    navmesh.DANGER_COST = float(s.get("danger_cost", DEFAULTS["danger_cost"]))
    navmesh.HOLE_POLY_MAX = float(s.get("hole_poly_max", DEFAULTS["hole_poly_max"]))
    navmesh.DOODAD_MAX_HEIGHT = float(s.get("doodad_max_height", DEFAULTS["doodad_max_height"]))
    navmesh.DOODAD_CLEARANCE = float(s.get("doodad_clearance", DEFAULTS["doodad_clearance"]))
    navmesh.MERGE_CAP = float(s.get("merge_cap", DEFAULTS["merge_cap"]))
    navmesh.MAX_CLIMB = float(s.get("max_climb", DEFAULTS["max_climb"]))
    navmesh.UPHILL_COST = float(s.get("uphill_cost", DEFAULTS["uphill_cost"]))


def holes():
    try:
        return json.load(open(os.path.join(HERE, "data", "obstacles.json")))
    except Exception:
        return []


def polys_in(x0, y0, x1, y1):
    """Polygons whose 2D bounds touch the WoW box, as WoW vertex rings."""
    n = nav()
    out = []
    # recast x = wow y, recast z = wow x
    rx0, rx1 = min(y0, y1), max(y0, y1)
    rz0, rz1 = min(x0, x1), max(x0, x1)
    for key, t in n.tiles.items():
        if t.bmax[0] < rx0 or t.bmin[0] > rx1 or t.bmax[2] < rz0 or t.bmin[2] > rz1:
            continue
        m = ((t.pmax[:, 0] >= rx0) & (t.pmin[:, 0] <= rx1) &
             (t.pmax[:, 2] >= rz0) & (t.pmin[:, 2] <= rz1))
        for i in (m.nonzero()[0]):
            vs = t.verts[t.pverts[i, :t.pcount[i]]]
            ring = [[round(float(v[2]), 2), round(float(v[0]), 2), round(float(v[1]), 2)] for v in vs]
            out.append({"t": list(key), "i": int(i), "f": int(t.pflags[i]), "a": int(t.parea[i]),
                        "type": int(t.ptype[i]), "v": ring,
                        "zmin": round(float(t.pmin[i, 1]), 1), "zmax": round(float(t.pmax[i, 1]), 1)})
            if len(out) > 20000:
                return out
    return out


def floors_at(x, y):
    """Heights of every polygon under a WoW x,y (one per floor)."""
    n = nav()
    rx, rz = y, x
    t = n.tile_at(rx, rz)
    if t is None:
        return []
    m = ((t.pmin[:, 0] - 0.5 <= rx) & (t.pmax[:, 0] + 0.5 >= rx) &
         (t.pmin[:, 2] - 0.5 <= rz) & (t.pmax[:, 2] + 0.5 >= rz))
    hs = []
    for i in m.nonzero()[0]:
        if not t.usable(i):
            continue
        vs = t.verts[t.pverts[i, :t.pcount[i]]]
        if navmesh._point_in_poly_2d(rx, rz, vs):
            hs.append(round(float(t.center[i, 1]), 1))
    return sorted(set(hs), reverse=True)


def compute_path(start, end, s):
    apply_settings(s)
    hs = holes() if s.get("use_holes", True) else []
    dz = ()
    if s.get("use_dangers"):
        try:
            import dangers
            pad = 300
            bbox = (min(start[0], end[0]) - pad, min(start[1], end[1]) - pad,
                    max(start[0], end[0]) + pad, max(start[1], end[1]) + pad)
            dz = dangers.zones(0, int(s.get("level", 14)), int(s.get("faction", 1)), bbox)
        except Exception as e:
            print("dangers:", e)
    orig_avoid = navmesh._avoid_doodads
    orig_fixes = None
    if not s.get("avoid_doodads", True):
        navmesh._avoid_doodads = lambda start, pts, max_fixes=12, extra_boxes=(): [tuple(p) for p in pts]
    else:
        mf = int(s.get("max_fixes", 12))
        navmesh._avoid_doodads = lambda start, pts, max_fixes=12, extra_boxes=(), _o=orig_avoid: _o(
            start, pts, max_fixes=mf, extra_boxes=extra_boxes)
    try:
        t0 = time.time()
        with LOCK:
            raw_holder = {}
            # capture the straightened path before doodad fixing, for comparison
            def capture(start_, pts, max_fixes=12, extra_boxes=(), _o=orig_avoid):
                raw_holder["raw"] = [tuple(p) for p in pts]
                if not s.get("avoid_doodads", True):
                    return [tuple(p) for p in pts]
                return _o(start_, pts, max_fixes=int(s.get("max_fixes", 12)), extra_boxes=extra_boxes)
            navmesh._avoid_doodads = capture
            path = nav().find_path(tuple(start), tuple(end), dangers=dz,
                                   agent_radius=float(s.get("agent_radius", 0.6)),
                                   max_nodes=int(s.get("max_nodes", 200000)), holes=hs)
        dt = time.time() - t0
    finally:
        navmesh._avoid_doodads = orig_avoid
    length = 0.0
    if path:
        pts = [tuple(start)] + [tuple(p) for p in path]
        length = sum(math.dist(pts[i][:2], pts[i + 1][:2]) for i in range(len(pts) - 1))
    return {"path": [[round(float(v), 2) for v in p] for p in path] if path else None,
            "raw": [[round(float(v), 2) for v in p] for p in raw_holder.get("raw", [])],
            "seconds": round(dt, 3), "length": round(length, 1), "holes": len(hs), "dangers": len(dz),
            "danger_zones": [[round(float(v), 1) for v in z] for z in dz][:500]}


def doodads_in(x0, y0, x1, y1, z, s):
    if vmap is None:
        return []
    apply_settings(s)
    try:
        return [[round(float(v), 2) for v in b] for b in navmesh._doodad_boxes(x0, y0, x1, y1, z)]
    except Exception as e:
        print("doodads:", e)
        return []


def status():
    try:
        return json.load(open(os.path.join(HERE, "status.json")))
    except Exception:
        return {}


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Navmesh viewer</title>
<style>
 body{margin:0;font:13px system-ui,sans-serif;background:#111;color:#ddd;display:flex;height:100vh}
 #side{width:300px;padding:10px;overflow:auto;background:#1b1b1b;border-right:1px solid #333}
 #wrap{flex:1;position:relative}
 canvas{display:block;background:#0a0a0a;cursor:crosshair}
 label{display:flex;justify-content:space-between;align-items:center;margin:3px 0}
 input[type=number]{width:80px;background:#222;color:#eee;border:1px solid #444;padding:2px 4px}
 input[type=text]{width:170px;background:#222;color:#eee;border:1px solid #444;padding:2px 4px}
 button{background:#2b5;border:0;color:#000;padding:5px 9px;margin:2px 2px 2px 0;cursor:pointer;border-radius:3px}
 button.g{background:#444;color:#eee}
 h4{margin:10px 0 4px;color:#9cf}
 #info{position:absolute;left:8px;top:8px;background:rgba(0,0,0,.7);padding:4px 8px;border-radius:4px;pointer-events:none;white-space:pre}
 #legend{position:absolute;right:8px;top:8px;background:rgba(0,0,0,.7);padding:4px 8px;border-radius:4px;font-size:12px;line-height:1.5}
 .sw{display:inline-block;width:10px;height:10px;margin-right:5px;vertical-align:middle}
 #res{font-family:monospace;white-space:pre-wrap;font-size:12px;color:#bdb}
 small{color:#888}
</style></head><body>
<div id="side">
 <h4>Points</h4>
 <div><small>Left click = start, right click = end. Drag to pan, wheel to zoom.</small></div>
 <label>Start <input type="text" id="start" placeholder="x, y, z"></label>
 <label>End <input type="text" id="end" placeholder="x, y, z"></label>
 <div id="floors"></div>
 <button onclick="computePath()">Compute path</button>
 <button class="g" onclick="useBot()">Start = bot</button>
 <button class="g" onclick="goBot()">Go to bot</button>
 <div style="margin-top:4px">
  <button class="g" onclick="goTo(-9464,62)">Goldshire inn</button>
  <button class="g" onclick="goTo(-10513,1060)">Sentinel Hill</button>
  <button class="g" onclick="goTo(-9719,972)">Goretusk field</button>
  <button class="g" onclick="goTo(-8857,625)">SW Pestle</button>
 </div>
 <h4>Path settings</h4>
 <label>Water step cost <input type="number" step="0.5" id="water_cost"></label>
 <label>Steep step cost <input type="number" step="0.5" id="steep_cost"></label>
 <label>Agent radius <input type="number" step="0.1" id="agent_radius"></label>
 <label>Max nodes <input type="number" step="10000" id="max_nodes"></label>
 <label>Max climb (rise/run) <input type="number" step="0.05" id="max_climb"></label>
 <label>Uphill cost (per yd climbed) <input type="number" step="0.5" id="uphill_cost"></label>
 <label>Use learned holes <input type="checkbox" id="use_holes"></label>
 <label>Hole deletes polys up to (yd) <input type="number" step="0.5" id="hole_poly_max"></label>
 <label>Avoid doodads <input type="checkbox" id="avoid_doodads"></label>
 <label>Doodad max height <input type="number" step="0.5" id="doodad_max_height"></label>
 <label>Doodad clearance <input type="number" step="0.1" id="doodad_clearance"></label>
 <label>Merge cap (yd) <input type="number" step="0.5" id="merge_cap"></label>
 <label>Max detour fixes <input type="number" step="1" id="max_fixes"></label>
 <label>Use danger zones <input type="checkbox" id="use_dangers"></label>
 <label>Danger cost <input type="number" step="5" id="danger_cost"></label>
 <label>Level <input type="number" step="1" id="level"></label>
 <label>Faction <input type="number" step="1" id="faction"></label>
 <button class="g" onclick="resetSettings()">Reset to bot defaults</button>
 <h4>Display</h4>
 <label>Show raw path (before doodad fix) <input type="checkbox" id="show_raw" checked></label>
 <label>Show doodad boxes <input type="checkbox" id="show_doodads" checked onchange="loadDoodads()"></label>
 <label>Show holes <input type="checkbox" id="show_holes" checked onchange="draw()"></label>
 <label>Floor filter z ± <input type="number" step="1" id="zfilter" placeholder="all" onchange="draw()"></label>
 <label>Doodad z <input type="number" step="1" id="doodad_z" onchange="loadDoodads()"></label>
 <h4>Result</h4>
 <div id="res">-</div>
</div>
<div id="wrap"><canvas id="c"></canvas><div id="info"></div>
<div id="legend"><span class="sw" style="background:#2a6"></span>ground<br><span class="sw" style="background:#26a"></span>water<br>
<span class="sw" style="background:#a62"></span>steep<br><span class="sw" style="background:#f44"></span>doodad box<br>
<span class="sw" style="background:#f4f"></span>hole<br><span class="sw" style="background:#ff4"></span>danger zone<br>
<span class="sw" style="background:#fff"></span>path <span class="sw" style="background:#888"></span>raw</div></div>
<script>
const cv=document.getElementById('c'),ctx=cv.getContext('2d');
let view={cx:-9464,cy:62,scale:6}; // wow x -> screen y (north up), wow y -> screen x (west left)
let polys=[],doodads=[],holes=[],dangers=[],path=null,raw=[],start=null,end=null,bot=null,loading=false,lastBox=null;
const S=['water_cost','steep_cost','agent_radius','max_nodes','max_climb','uphill_cost','use_holes','hole_poly_max','avoid_doodads','doodad_max_height','doodad_clearance','merge_cap','max_fixes','use_dangers','danger_cost','level','faction'];
let defaults={};
function getSettings(){const s={};for(const k of S){const e=document.getElementById(k);s[k]=e.type==='checkbox'?e.checked:parseFloat(e.value);}return s;}
function setSettings(s){for(const k of S){const e=document.getElementById(k);if(!(k in s))continue;if(e.type==='checkbox')e.checked=!!s[k];else e.value=s[k];}}
function resetSettings(){setSettings(defaults);}
function w2s(x,y){return [cv.width/2-(y-view.cy)*view.scale, cv.height/2-(x-view.cx)*view.scale];}
function s2w(sx,sy){return [view.cx-(sy-cv.height/2)/view.scale, view.cy-(sx-cv.width/2)/view.scale];}
function resize(){cv.width=cv.parentElement.clientWidth;cv.height=cv.parentElement.clientHeight;draw();}
window.addEventListener('resize',resize);
function color(f){const g=f&1,w=f&8,st=f&16;if(st)return '#a62';if(w&&!g)return '#26a';if(w&&g)return '#2a8';if(g)return '#2a6';return '#555';}
function zok(p){const zf=parseFloat(document.getElementById('zfilter').value);if(isNaN(zf))return true;const ref=(start&&start[2])||(bot&&bot[2]);if(ref==null)return true;return p.zmax>=ref-zf&&p.zmin<=ref+zf;}
function draw(){
 ctx.clearRect(0,0,cv.width,cv.height);
 for(const p of polys){if(!zok(p))continue;ctx.beginPath();p.v.forEach((v,i)=>{const [sx,sy]=w2s(v[0],v[1]);i?ctx.lineTo(sx,sy):ctx.moveTo(sx,sy);});ctx.closePath();ctx.fillStyle=color(p.f);ctx.globalAlpha=0.55;ctx.fill();ctx.globalAlpha=1;ctx.strokeStyle='#000';ctx.lineWidth=0.5;ctx.stroke();}
 if(document.getElementById('show_doodads').checked){ctx.strokeStyle='#f44';ctx.lineWidth=1.5;for(const b of doodads){const [ax,ay]=w2s(b[2],b[3]),[bx,by]=w2s(b[0],b[1]);ctx.strokeRect(Math.min(ax,bx),Math.min(ay,by),Math.abs(bx-ax),Math.abs(by-ay));}}
 if(document.getElementById('show_holes').checked){ctx.strokeStyle='#f4f';ctx.lineWidth=1.5;for(const h of holes){const [sx,sy]=w2s(h[0],h[1]);ctx.beginPath();ctx.arc(sx,sy,h[3]*view.scale,0,7);ctx.stroke();}}
 ctx.strokeStyle='#ff4';ctx.lineWidth=1;for(const d of dangers){const [sx,sy]=w2s(d[0],d[1]);ctx.beginPath();ctx.arc(sx,sy,d[3]*view.scale,0,7);ctx.stroke();}
 const line=(pts,col,w)=>{if(!pts||!pts.length)return;ctx.strokeStyle=col;ctx.lineWidth=w;ctx.beginPath();let first=true;const all=start?[start,...pts]:pts;for(const p of all){const [sx,sy]=w2s(p[0],p[1]);first?ctx.moveTo(sx,sy):ctx.lineTo(sx,sy);first=false;}ctx.stroke();ctx.fillStyle=col;for(const p of pts){const [sx,sy]=w2s(p[0],p[1]);ctx.fillRect(sx-2,sy-2,4,4);}};
 if(document.getElementById('show_raw').checked)line(raw,'#888',1.5);
 line(path,'#fff',2.5);
 const mark=(p,col,t)=>{if(!p)return;const [sx,sy]=w2s(p[0],p[1]);ctx.fillStyle=col;ctx.beginPath();ctx.arc(sx,sy,6,0,7);ctx.fill();ctx.fillStyle='#000';ctx.font='bold 10px sans-serif';ctx.fillText(t,sx-3,sy+4);};
 mark(start,'#4f4','S');mark(end,'#f66','E');mark(bot,'#4cf','B');
 if(path){ctx.font='11px sans-serif';ctx.fillStyle='#fff';let all=start?[start,...path]:path;all.forEach((p,i)=>{if(i===0)return;const [sx,sy]=w2s(p[0],p[1]);ctx.fillText(p[2].toFixed(1),sx+4,sy-4);});}
 const xy=s2w(mouse[0],mouse[1]);document.getElementById('info').textContent=`x ${xy[0].toFixed(1)}  y ${xy[1].toFixed(1)}   scale ${view.scale.toFixed(1)} px/yd   polys ${polys.length}`+(hover?`\npoly flags 0x${hover.f.toString(16)} area ${hover.a} z ${hover.zmin}-${hover.zmax} (${hover.t[0]},${hover.t[1]}#${hover.i})`:'');
}
let mouse=[0,0],hover=null,drag=null,moved=false;
function pip(x,y,v){let c=false;for(let i=0,j=v.length-1;i<v.length;j=i++){const xi=v[i][0],yi=v[i][1],xj=v[j][0],yj=v[j][1];if(((yi>y)!==(yj>y))&&(x<(xj-xi)*(y-yi)/(yj-yi)+xi))c=!c;}return c;}
cv.addEventListener('mousemove',e=>{mouse=[e.offsetX,e.offsetY];if(drag){moved=true;view.cx+=(e.offsetY-drag[1])/view.scale;view.cy+=(e.offsetX-drag[0])/view.scale;drag=[e.offsetX,e.offsetY];}const [x,y]=s2w(e.offsetX,e.offsetY);hover=null;for(const p of polys){if(zok(p)&&pip(x,y,p.v)){hover=p;break;}}draw();});
cv.addEventListener('mousedown',e=>{drag=[e.offsetX,e.offsetY];moved=false;});
cv.addEventListener('mouseup',async e=>{const wasDrag=moved;drag=null;if(wasDrag){loadView();return;}const [x,y]=s2w(e.offsetX,e.offsetY);const fl=await (await fetch(`/floors?x=${x}&y=${y}`)).json();let z=fl.length?fl[0]:(hover?hover.zmax:0);if(fl.length>1){const ref=e.button===2?(start?start[2]:null):(end?end[2]:null);if(ref!=null)z=fl.reduce((a,b)=>Math.abs(b-ref)<Math.abs(a-ref)?b:a);}
 const pt=[+x.toFixed(1),+y.toFixed(1),z];if(e.button===2){end=pt;document.getElementById('end').value=pt.join(', ');}else{start=pt;document.getElementById('start').value=pt.join(', ');}
 document.getElementById('floors').innerHTML=fl.length>1?'floors here: '+fl.map(h=>`<button class="g" onclick="setZ(${h},${e.button===2})">${h}</button>`).join(''):'';
 if(start&&end)computePath();else draw();});
cv.addEventListener('contextmenu',e=>e.preventDefault());
cv.addEventListener('wheel',e=>{e.preventDefault();const [x,y]=s2w(e.offsetX,e.offsetY);view.scale*=e.deltaY<0?1.2:1/1.2;view.scale=Math.max(0.3,Math.min(60,view.scale));const [nx,ny]=s2w(e.offsetX,e.offsetY);view.cx+=x-nx;view.cy+=y-ny;draw();clearTimeout(window._lt);window._lt=setTimeout(loadView,250);});
function setZ(z,isEnd){const p=isEnd?end:start;if(!p)return;p[2]=z;document.getElementById(isEnd?'end':'start').value=p.join(', ');if(start&&end)computePath();}
function parsePt(id){const v=document.getElementById(id).value.split(/[ ,]+/).map(parseFloat);return v.length>=2&&!v.some((n,i)=>i<2&&isNaN(n))?[v[0],v[1],isNaN(v[2])?0:v[2]]:null;}
async function loadView(){const [x0,y0]=s2w(cv.width,cv.height),[x1,y1]=s2w(0,0);const w=Math.abs(x1-x0),h=Math.abs(y1-y0);if(w>900||h>900){polys=[];draw();document.getElementById('res').textContent='zoom in to load polygons (view > 900 yd)';return;}
 lastBox=[Math.min(x0,x1),Math.min(y0,y1),Math.max(x0,x1),Math.max(y0,y1)];const r=await fetch(`/polys?x0=${lastBox[0]}&y0=${lastBox[1]}&x1=${lastBox[2]}&y1=${lastBox[3]}`);polys=await r.json();holes=await (await fetch('/holes')).json();loadDoodads();draw();}
async function loadDoodads(){if(!lastBox||!document.getElementById('show_doodads').checked){doodads=[];draw();return;}let z=parseFloat(document.getElementById('doodad_z').value);if(isNaN(z))z=start?start[2]:(bot?bot[2]:(polys.length?polys[0].zmax:0));const q=new URLSearchParams({x0:lastBox[0],y0:lastBox[1],x1:lastBox[2],y1:lastBox[3],z,s:JSON.stringify(getSettings())});doodads=await (await fetch('/doodads?'+q)).json();draw();}
async function computePath(){start=parsePt('start');end=parsePt('end');if(!start||!end){document.getElementById('res').textContent='need start and end';return;}document.getElementById('res').textContent='computing...';const r=await fetch('/path',{method:'POST',body:JSON.stringify({start,end,settings:getSettings()})});const j=await r.json();path=j.path;raw=j.raw||[];dangers=j.danger_zones||[];
 let txt=j.path?`path: ${j.path.length} points, ${j.length} yd, ${j.seconds}s\nholes: ${j.holes}  dangers: ${j.dangers}\n`+j.path.map(p=>p.map(v=>v.toFixed(1)).join(', ')).join('\n'):`NO PATH (${j.seconds}s, holes ${j.holes})`;document.getElementById('res').textContent=txt;loadDoodads();}
async function loadBot(){const s=await (await fetch('/status')).json();if(s.pos){bot=s.pos;}draw();}
function useBot(){if(!bot)return;start=[+bot[0].toFixed(1),+bot[1].toFixed(1),+bot[2].toFixed(1)];document.getElementById('start').value=start.join(', ');if(end)computePath();else draw();}
function goBot(){if(!bot)return;goTo(bot[0],bot[1]);}
function goTo(x,y){view.cx=x;view.cy=y;if(view.scale<3)view.scale=6;loadView();}
document.getElementById('start').addEventListener('change',()=>{start=parsePt('start');draw();});
document.getElementById('end').addEventListener('change',()=>{end=parsePt('end');draw();});
(async()=>{defaults=await (await fetch('/defaults')).json();setSettings(defaults);resize();await loadBot();setInterval(loadBot,5000);loadView();})();
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                b = HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == "/polys":
                with LOCK:
                    self._json(polys_in(*(float(q[k]) for k in ("x0", "y0", "x1", "y1"))))
            elif u.path == "/floors":
                with LOCK:
                    self._json(floors_at(float(q["x"]), float(q["y"])))
            elif u.path == "/holes":
                self._json(holes())
            elif u.path == "/doodads":
                s = json.loads(q.get("s", "{}"))
                with LOCK:
                    self._json(doodads_in(*(float(q[k]) for k in ("x0", "y0", "x1", "y1")), float(q["z"]), s))
            elif u.path == "/status":
                self._json(status())
            elif u.path == "/defaults":
                self._json(DEFAULTS)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/path":
                self._json(compute_path(body["start"], body["end"], body.get("settings", {})))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    nav()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"navview: http://localhost:{PORT}")
    srv.serve_forever()
