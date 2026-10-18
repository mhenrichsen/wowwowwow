"""level_timer.py NAME... : every 60 s read each character's level from tcf_characters and log a line per level-up
with the time since the character was first seen at level 1 (nb/run_levels.log). Budget check: level 10 by
BUDGET_10_S."""
import os, sys, time, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import worlddb
BUDGET_10_S = 3.5 * 3600
LOG = os.path.join(ROOT, "nb", "run_levels.log")
STATE = os.path.join(ROOT, "nb", "run_levels.json")
names = sys.argv[1:]
st = json.load(open(STATE)) if os.path.exists(STATE) else {}


def out(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


while True:
    try:
        q = ",".join("'%s'" % n for n in names if n.isalpha())
        rows = worlddb._query_db("tcf_characters", f"SELECT name, level, totaltime FROM characters WHERE name IN ({q})")
        now = time.time()
        for name, lvl, played in rows:
            lvl = int(lvl)
            c = st.setdefault(name, {"t0": now, "level": lvl, "ups": {}})
            if lvl > c["level"]:
                el = now - c["t0"]
                c["ups"][str(lvl)] = round(el)
                over = " OVER BUDGET" if lvl >= 10 and el > BUDGET_10_S and lvl == 10 else ""
                out(f"{name} level {lvl} at {el / 3600:.2f} h (played {int(played) / 3600:.2f} h){over}")
                c["level"] = lvl
            elif c["level"] < 10 and now - c["t0"] > BUDGET_10_S and not c.get("over"):
                c["over"] = True
                out(f"{name} OVER BUDGET: level {c['level']} after {(now - c['t0']) / 3600:.2f} h")
        json.dump(st, open(STATE, "w"))
    except Exception as e:
        out(f"error {e!r}")
    time.sleep(60)
