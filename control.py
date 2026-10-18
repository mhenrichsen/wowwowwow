"""Start and stop the bot without a terminal.

Starting it by hand is four commands that have to be in the right order,
and the one everybody forgets is clearing a stale bot.lock - which fails
with "another bot is already driving the client" and looks like a real
conflict. Put the sequence in one place so the web page and the command
line both get it right.

Nothing here takes a command from the caller: the actions are fixed and
the arguments come from launch_args.txt, so the worst a request can do is
start or stop this bot.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "bot.lock")
STOP = os.path.join(HERE, "STOP")
LOG = os.path.join(HERE, "bot.log")
PIDFILE = os.path.join(HERE, "bot.pid")
ARGS_FILE = os.path.join(HERE, "launch_args.txt")
LOGS_DIR = os.path.join(HERE, "logs")

DEFAULT_ARGS = "--minutes 720"


def launch_args() -> list[str]:
    """The arguments a restart should use.

    They live in a file rather than in this code because a restart that
    hardcoded the human Elwynn guide once put a dwarf on it.
    """
    try:
        line = open(ARGS_FILE, encoding="utf-8").read().strip()
    except OSError:
        line = ""
    return (line or DEFAULT_ARGS).split()


def running() -> int | None:
    """PID of the live bot, or None.

    bot.lock is an exclusive OS lock held for the life of the process and
    released even on a kill, so "can I take it?" is the one liveness test
    that a crashed run cannot fake. The file itself holds the PID.
    """
    import msvcrt
    try:
        f = open(LOCK, "a+")
    except OSError:
        return None
    try:
        # seek(0) first: msvcrt.locking locks a range from the CURRENT file
        # position, and a file opened "a+" does not start there. Locking
        # some other byte never collided with the bot's lock on byte 0, so
        # this reported "stopped" over a live bot and would have let the
        # page start a second one.
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return _recorded_pid() or -1       # held; -1 if we cannot say by whom
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    f.close()
    return None


def _recorded_pid() -> int | None:
    """The pid we last launched, from bot.lock or our own note of it.

    bot.lock is the bot's own record but it is written through a handle it
    keeps open, so it can read back empty while the run is live.
    """
    for path in (LOCK, PIDFILE):
        try:
            v = int(open(path, encoding="utf-8").read().strip() or 0)
            if v > 0:
                return v
        except (OSError, ValueError):
            continue
    return None


def start() -> tuple[bool, str]:
    """Launch the bot detached, the way safe_restart.sh would."""
    pid = running()
    if pid:
        return False, f"already running (pid {pid})"
    # a STOP left over from the last shutdown makes the new run exit at once
    for f in (STOP, LOCK):
        try:
            os.remove(f)
        except OSError:
            pass
    os.makedirs(LOGS_DIR, exist_ok=True)
    if os.path.exists(LOG) and os.path.getsize(LOG):
        # launching with "> bot.log" truncates it, and every restart used to
        # destroy the evidence for whatever was being measured.
        # Retry: the old process can still hold the handle for a moment
        # after it releases the lock, and Windows refuses the rename while
        # it does - a silently skipped archive left two runs interleaved in
        # one file, which is exactly the thing this is meant to prevent.
        dest = os.path.join(LOGS_DIR, f"bot_{time.strftime('%Y%m%d_%H%M%S')}.log")
        for _ in range(20):
            try:
                os.replace(LOG, dest)
                break
            except OSError:
                time.sleep(0.25)
    args = launch_args()
    out = open(LOG, "ab", buffering=0)
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    p = subprocess.Popen([sys.executable, "-u", "bot.py", *args],
                         cwd=HERE, stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=flags,
                         close_fds=True)
    try:
        open(PIDFILE, "w").write(str(p.pid))
    except OSError:
        pass
    # it takes a moment to claim the lock; report a start that died at once
    for _ in range(20):
        time.sleep(0.25)
        if p.poll() is not None:
            return False, f"exited immediately (code {p.returncode}) - see bot.log"
        if running():
            break
    return True, f"started (pid {p.pid}): bot.py {' '.join(args)}"


def stop(force: bool = False) -> tuple[bool, str]:
    """Ask the bot to finish the current action and exit.

    The graceful path is a STOP file, which the bot notices between ticks.
    A hard kill leaves the character standing wherever it was, in combat if
    it was in combat, so it is only what "force" is for.
    """
    pid = running()
    if not pid:
        return False, "not running"
    open(STOP, "w").close()
    if not force:
        return True, "stop requested - it exits after the current action"
    for _ in range(40):
        time.sleep(0.25)
        if not running():
            return True, "stopped"
    if pid > 0:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True, f"killed pid {pid} (it did not stop on its own)"
    return False, "still running and its pid is unknown"


def restart() -> tuple[bool, str]:
    if running():
        ok, msg = stop(force=True)
        if not ok:
            return False, msg
    return start()


def tail(n: int = 15) -> list[str]:
    try:
        with open(LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    return lines[-n:]


def state() -> dict:
    pid = running()
    return {"running": bool(pid), "pid": pid or None,
            "args": " ".join(launch_args()),
            "stop_pending": os.path.exists(STOP)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        s = state()
        print(f"{'running (pid %s)' % s['pid'] if s['running'] else 'stopped'}"
              f"  args: {s['args']}")
        for line in tail(10):
            print("  " + line)
    elif cmd in ("start", "stop", "restart", "kill"):
        ok, msg = (start() if cmd == "start" else
                   restart() if cmd == "restart" else
                   stop(force=(cmd == "kill")))
        print(msg)
        sys.exit(0 if ok else 1)
    else:
        print(__doc__)
        print("usage: python control.py [status|start|stop|kill|restart]")
        sys.exit(1)
