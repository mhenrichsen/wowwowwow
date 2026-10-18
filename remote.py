"""Start the phone status page and a Cloudflare quick tunnel to it.

Writes the current public link (it changes on every start) to phone_link.txt
and keeps both running until this window is closed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LINK_FILE = os.path.join(HERE, "phone_link.txt")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def main():
    web = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "web.py")],
                           cwd=HERE, stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
    local = web.stdout.readline().strip()                 # "status page: http://...?key=..."
    key = local.split("key=", 1)[1]
    tunnel = subprocess.Popen([os.path.join(HERE, "cloudflared.exe"), "tunnel", "--no-autoupdate",
                               "--url", "http://127.0.0.1:8765"],
                              cwd=HERE, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              text=True, creationflags=NO_WINDOW)
    url = None
    t0 = time.time()
    for line in tunnel.stderr:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
        if time.time() - t0 > 60:
            break
    if not url:
        print("could not start the Cloudflare tunnel - is the internet connection up?")
        web.terminate()
        tunnel.terminate()
        return 1
    link = f"{url}/?key={key}"
    open(LINK_FILE, "w").write(link + "\n")
    print("\n  Open this on your phone (it is also saved in phone_link.txt):\n")
    print("  " + link + "\n")
    print("  Keep this window open. Closing it stops the phone page.")
    try:
        for _ in tunnel.stderr:        # keep draining cloudflared's log
            pass
    except KeyboardInterrupt:
        pass
    finally:
        web.terminate()
        tunnel.terminate()
        try:
            os.remove(LINK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main() or 0)
