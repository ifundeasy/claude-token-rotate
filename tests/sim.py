#!/usr/bin/env python3
"""Run the real dashboard on a fake clock against a deterministic world, and record its decisions.

This is the behaviour fence for the v2 refactor: every scenario below is run against a code
directory (the repo, or a `git worktree` of v1.0.0) and produces a timeline — one line per
refresh with the live credential, the pin and the spend-down, plus one line per swap. The
recorded timelines of v1.0.0 live in tests/golden/; the current code must reproduce them exactly.

    python3 tests/sim.py --golden                 # current repo vs tests/golden (exit 1 on a diff)
    python3 tests/sim.py --record --code DIR      # (re)write tests/golden from the code in DIR
    python3 tests/sim.py --only weekend --show    # one scenario, timeline on stdout

Nothing here touches a real file or the network. The child process is the real main() in a pty;
its clock, probes, session scan, notifications and every path are replaced. The parent owns the
world: it advances the clock one refresh at a time, applies usage to whichever credential is
live, and waits for the dashboard to redraw before it reads the result.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import math
import os
import pty
import select
import shutil
import signal
import sys
import tempfile
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(REPO, "tests", "golden")
H, M = 3600.0, 60.0


def tok(ch: str) -> str:
    return "sk-ant-oat01-" + ch * 90 + "AAA"


def at(y, mo, d, h, mi=0) -> float:
    return dt.datetime(y, mo, d, h, mi).timestamp()


# --------------------------------------------------------------------------- scenarios
#
# Each credential: (name, letter, u5h, u7d, hours to 5h reset, hours to weekly reset).
# `user`: (start, end) stretches when Claude Code is in use; usage lands on the live credential.
# `mate`: (name, start, end, 5h pp/h, 7d pp/h) — somebody else on a credential.
# `blips`: {step: [names]} probes that time out. `keys`: {step: [str]} typed after that refresh.
# `restart`: steps at which the dashboard is quit and started again on the same files.

def _creds_weekend():
    return [("Moe", "W", 36, 50, 3.8, 6.0), ("Finn", "N", 0, 67, 3.8, 5.7),
            ("Ivy", "S", 0, 72, 3.8, 13.7), ("Gus", "K", 0, 77, 3.8, 15.7),
            ("Ada", "A", 0, 66, 3.7, 31.7), ("Dora", "F", 0, 89, 4.2, 52.7),
            ("Nell", "Y", 0, 66, 2.7, 58.7), ("Eli", "D", 0, 38, 3.8, 112.7),
            ("Jude", "T", 0, 31, 3.8, 123.7), ("Kit", "B", 0, 9, 3.8, 135.7)]


SCENARIOS: dict[str, dict] = {
    # Friday afternoon to Monday morning: off hours, spend-down, keep, preempt, rebalance.
    "weekend": dict(start=at(2026, 10, 9, 17), steps=64 * 6, live="Moe", creds=_creds_weekend(),
                    user=[(at(2026, 10, 9, 17), at(2026, 10, 10, 1)),
                          (at(2026, 10, 10, 9), at(2026, 10, 10, 12)),
                          (at(2026, 10, 10, 20), at(2026, 10, 10, 23, 30)),
                          (at(2026, 10, 11, 16), at(2026, 10, 11, 18)),
                          (at(2026, 10, 12, 8), at(2026, 10, 12, 9))],
                    mate=[("Gus", at(2026, 10, 10, 12, 30), at(2026, 10, 10, 14, 30), 8, 2)]),
    # A weekday evening into the next morning: working hours, then a night (5h lifted, burn).
    "weeknight": dict(start=at(2026, 10, 14, 18), steps=16 * 6, live="Eli",
                      creds=[("Eli", "D", 50, 40, 1.0, 90.0), ("Finn", "N", 0, 70, 4.0, 9.0),
                             ("Ivy", "S", 0, 30, 2.0, 60.0), ("Ada", "A", 0, 20, 3.0, 120.0)],
                      user=[(at(2026, 10, 14, 18), at(2026, 10, 15, 2))]),
    # Working hours: a pinned credential over 5h held by the grace, then a weekly overrun.
    "pin": dict(start=at(2026, 10, 14, 9), steps=8 * 6, live="Finn", pin="Finn",
                creds=[("Finn", "N", 55, 60, 0.9, 50.0), ("Ivy", "S", 10, 30, 3.0, 70.0),
                       ("Ada", "A", 5, 20, 2.0, 100.0)],
                user=[(at(2026, 10, 14, 9), at(2026, 10, 14, 17))]),
    # Disable the live credential from the keyboard, then enable it again.
    "disable": dict(start=at(2026, 10, 14, 10), steps=4 * 6, live="Finn",
                    creds=[("Finn", "N", 20, 30, 3.0, 50.0), ("Ivy", "S", 10, 30, 3.0, 70.0),
                           ("Ada", "A", 5, 20, 2.0, 100.0)],
                    user=[(at(2026, 10, 14, 10), at(2026, 10, 14, 14))],
                    keys={3: ["n", "1\r"], 12: ["n", "1\r"]}),
    # Probe blips on the live credential and on the best candidate.
    "blip": dict(start=at(2026, 10, 14, 10), steps=3 * 6, live="Finn",
                 creds=[("Finn", "N", 58, 40, 3.0, 50.0), ("Ivy", "S", 10, 30, 3.0, 30.0),
                        ("Ada", "A", 5, 20, 2.0, 100.0)],
                 user=[(at(2026, 10, 14, 10), at(2026, 10, 14, 13))],
                 blips={2: ["Finn"], 3: ["Finn", "Ivy"], 5: ["Ivy"]}),
    # A restart in the middle of a spend-down must keep holding it.
    "restart": dict(start=at(2026, 10, 10, 10), steps=6 * 6, live="Eli",
                    creds=[("Eli", "D", 10, 30, 3.0, 90.0), ("Finn", "N", 0, 80, 2.0, 5.0),
                           ("Ivy", "S", 0, 30, 2.0, 60.0)],
                    user=[(at(2026, 10, 10, 10), at(2026, 10, 10, 16))], restart=[9, 20]),
    # Weekday rebalance: the sooner week takes over; one somebody is on is skipped.
    "rebalance": dict(start=at(2026, 10, 14, 9), steps=6 * 6, live="Kit",
                      creds=[("Kit", "B", 10, 10, 3.0, 140.0), ("Finn", "N", 0, 30, 2.0, 40.0),
                             ("Ivy", "S", 0, 20, 2.0, 20.0), ("Ada", "A", 0, 50, 2.0, 60.0)],
                      user=[(at(2026, 10, 14, 9), at(2026, 10, 14, 15))],
                      mate=[("Ivy", at(2026, 10, 14, 8), at(2026, 10, 14, 11), 6, 1)]),
    # The hand keys: p pins a row (prompt + confirm), u unpins, T cycles, z switches off and on.
    "keys": dict(start=at(2026, 10, 14, 10), steps=3 * 6, live="Finn",
                 creds=[("Finn", "N", 20, 30, 3.0, 50.0), ("Ivy", "S", 10, 30, 3.0, 70.0),
                        ("Ada", "A", 5, 20, 2.0, 100.0)],
                 user=[(at(2026, 10, 14, 10), at(2026, 10, 14, 13))],
                 keys={2: ["p", "3\r", "y\r"], 5: ["u"], 7: ["T"], 9: ["T"], 10: ["T"],
                       12: ["z"], 14: ["z"]}),
    # Saturday: several spend-down candidates; the soonest goes first and preempts mid-hold.
    "preempt": dict(start=at(2026, 10, 10, 8), steps=10 * 6, live="Eli",
                    creds=[("Eli", "D", 10, 30, 3.0, 90.0), ("Moe", "W", 0, 70, 2.0, 5.0),
                           ("Xeno", "X", 0, 53, 2.0, 3.5), ("Finn", "N", 96, 75, 1.0, 6.0)],
                    user=[(at(2026, 10, 10, 8), at(2026, 10, 10, 18))]),
}


# --------------------------------------------------------------------------- the world

class World:
    """Quota that only moves when somebody spends it, and windows that reset on schedule."""

    def __init__(self, sc: dict):
        t0 = sc["start"]
        self.c = {name: {"tok": tok(ch), "u5": float(u5), "u7": float(u7),
                         "r5": t0 + h5 * H, "r7": t0 + h7 * H}
                  for name, ch, u5, u7, h5, h7 in sc["creds"]}
        self.user, self.mate = sc.get("user", []), sc.get("mate", [])

    def name_of(self, token: str) -> str | None:
        return next((n for n, v in self.c.items() if v["tok"] == token), None)

    def advance(self, t0: float, t1: float, live: str | None, on: bool) -> None:
        t = t0
        while t < t1:
            step = min(M, t1 - t)
            for name, v in self.c.items():
                r5 = r7 = 0.0
                if on and name == live and any(a <= t < b for a, b in self.user):
                    r5, r7 = 15.0, 2.0
                for mn, a, b, m5, m7 in self.mate:
                    if mn == name and a <= t < b:
                        r5, r7 = r5 + m5, r7 + m7
                v["u5"] = min(100.0, v["u5"] + r5 * step / H)
                v["u7"] = min(100.0, v["u7"] + r7 * step / H)
            t += step
            for v in self.c.values():
                while t >= v["r5"]:
                    v["u5"], v["r5"] = 0.0, v["r5"] + 5 * H
                while t >= v["r7"]:
                    v["u7"], v["r7"] = 0.0, v["r7"] + 7 * 24 * H

    def reading(self, name: str) -> dict:
        v = self.c[name]
        u5, u7 = math.floor(v["u5"]), math.floor(v["u7"])     # whole percent, like the API
        out = {"u5h": f"{u5 / 100:.2f}", "u7d": f"{u7 / 100:.2f}",
               "r5h": str(int(v["r5"])), "r7d": str(int(v["r7"])),
               "s5h": "rejected" if u5 >= 100 else "allowed",
               "s7d": "rejected" if u7 >= 100 else "allowed"}
        refused = u5 >= 100 or u7 >= 100
        out.update(ok=not refused, code=429 if refused else 200)
        return out


# --------------------------------------------------------------------------- the child

def child(code: str, d: str, argv: list[str]) -> None:
    """Runs in the pty: the real main() with clock, probes and paths replaced."""
    sys.path.insert(0, code)
    # Never find the real daemon of this machine: a dashboard that did would become its screen.
    os.environ["XDG_RUNTIME_DIR"] = d
    os.environ.pop("CTR_SOCKET", None)
    import main as m                                           # noqa: E402

    clock_p, world_p, log_p = (os.path.join(d, f) for f in ("clock", "world.json", "log.jsonl"))

    class Clock:
        """time with a fake now; everything else is the real module."""
        def __getattr__(self, k):
            return getattr(_time, k)

        def time(self):
            try:
                with open(clock_p) as fh:
                    return float(fh.read() or 0)
            except (OSError, ValueError):
                return 0.0

    def log(rec: dict) -> None:
        with open(log_p, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    m.time = Clock()
    m.STATE_PATH = os.path.join(d, "data.json")
    m.ENV_PATH = os.path.join(d, ".env")
    m.HOLIDAYS_PATH = os.path.join(d, "holidays.txt")

    def probe_all(store, tokens, timeout):
        with open(world_p) as fh:
            w = json.load(fh)
        log({"probe": m.time.time()})
        return {t: w.get(t, {"err": "timed out"}) for t in tokens}

    m.probe_all = probe_all
    m.refresh_pool = lambda timeout: None
    m.resolve_cap = lambda *a, **k: None
    m.sessions_on = lambda token: []
    m.notify = lambda *a, **k: False
    real_apply = m.apply_live

    def apply_live(env_path, creds_path, store, token, on, *a, **k):
        log({"swap": m.time.time(), "to": store.name(next(r for r in store.rows
                                                          if store.token(r) == token)),
             "on": on})
        return real_apply(env_path, creds_path, store, token, on, *a, **k)

    m.apply_live = apply_live
    real_render = m.render

    def render(*a, **k):                         # one marker per redraw: the parent's sync point
        log({"frame": m.time.time()})
        return real_render(*a, **k)

    m.render = render
    # The daemon has no screen: it marks each refresh through its hook instead.
    m._tick_hook = lambda now: log({"frame": m.time.time()})
    m.DAEMON_POLL = 0.02
    sys.argv = ["main.py"] + argv
    try:
        m.main()
    finally:
        os._exit(0)


# --------------------------------------------------------------------------- the parent

class Run:
    def __init__(self, code: str, name: str, sc: dict, d: str, daemon: bool = False):
        self.code, self.name, self.sc, self.d, self.daemon = code, name, sc, d, daemon
        self.world = World(sc)
        self.clock = sc["start"]
        self.lines: list[dict] = []
        self.fd = self.pid = None
        self.seen = 0
        self.names = {v["tok"]: n for n, v in self.world.c.items()}

    # files
    def path(self, f: str) -> str:
        return os.path.join(self.d, f)

    def write_clock(self) -> None:
        with open(self.path("clock"), "w") as fh:
            fh.write(repr(self.clock))

    def write_world(self, blips: list[str]) -> None:
        w = {v["tok"]: ({"err": "timed out"} if n in blips else self.world.reading(n))
             for n, v in self.world.c.items()}
        with open(self.path("world.json"), "w") as fh:
            json.dump(w, fh)

    def setup(self) -> None:
        live = self.world.c[self.sc["live"]]["tok"]
        with open(self.path("token.csv"), "w") as fh:
            fh.write("Name,CLAUDE_CODE_OAUTH_TOKEN\n")
            for n, v in self.world.c.items():
                fh.write(f"{n},{v['tok']}\n")
        with open(self.path("zshenv"), "w") as fh:
            fh.write(f'export CLAUDE_CODE_OAUTH_TOKEN="{live}"\n')
        with open(self.path("creds.json"), "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": live, "refreshToken": "",
                                         "expiresAt": None, "scopes": []}}, fh)
        # The /login the injected token stands in for, parked where the tool keeps it, so `z`
        # has something to switch back to.
        with open(self.path("creds.json") + ".claude_token_rotate.login", "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": "login-access", "refreshToken": "login-r",
                                         "expiresAt": None, "scopes": []}}, fh)
        with open(self.path("holidays.txt"), "w") as fh:
            fh.write("")
        if self.sc.get("pin"):
            sys.path.insert(0, self.code)
            import importlib
            m = importlib.import_module("main")
            with open(self.path("data.json"), "w") as fh:
                json.dump({"pinned": m.pin_id(self.world.c[self.sc["pin"]]["tok"]),
                           "rotate_mode": "on"}, fh)
        self.write_clock()
        self.write_world([])

    def argv(self) -> list[str]:
        return (["daemon"] if self.daemon else []) + ["--csv", self.path("token.csv"), "--env-file", self.path("zshenv"),
                "--creds-file", self.path("creds.json"), "--holidays", self.path("holidays.txt"),
                "--cap", "off", "--rotate-mode", "on", "--interval", "600",
                "--no-title", "--no-mouse", "--no-notify", "--sort", "csv"]

    # child control
    def start(self) -> None:
        sys.stdout.flush()
        pid, fd = pty.fork()
        if pid == 0:
            child(self.code, self.d, self.argv())
        self.pid, self.fd = pid, fd

    def drain(self, secs: float) -> None:
        end = _time.time() + secs
        while _time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    os.read(self.fd, 65536)
                except OSError:
                    return

    def wake(self) -> None:
        """Interrupt the dashboard's wait for a key with one it ignores (F12), so the new clock
        is seen at once instead of after up to a second. The daemon polls on its own."""
        if self.daemon:
            return
        try:
            os.write(self.fd, b"\x1b[24~")
        except OSError:
            pass

    def log_lines(self) -> list[dict]:
        try:
            with open(self.path("log.jsonl")) as fh:
                return [json.loads(x) for x in fh if x.strip()]
        except OSError:
            return []

    def wait_frame(self, t: float, timeout: float = 20.0) -> None:
        """Until the dashboard has probed at `t` and redrawn after it."""
        end = _time.time() + timeout
        while _time.time() < end:
            recs = self.log_lines()
            probed = any(r.get("probe") == t for r in recs)
            if probed and any(r.get("frame") == t for r in recs[[i for i, r in enumerate(recs)
                                                                 if r.get("probe") == t][-1]:]):
                return
            self.drain(0.05)
        raise RuntimeError(f"{self.name}: no redraw at {t} — the dashboard stalled")

    def stop(self) -> None:
        if self.pid is None:
            return
        try:
            if self.daemon:
                os.kill(self.pid, signal.SIGTERM)
            else:
                os.write(self.fd, b"q")
        except OSError:
            pass
        end = _time.time() + 5
        while _time.time() < end:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            if done:
                break
            self.drain(0.05)
        else:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        self.pid = None

    def command(self, seq: list[str]) -> None:
        """The same keys, sent the way a screen of the daemon sends them."""
        if not seq:
            return
        sys.path.insert(0, self.code)
        import importlib
        m = importlib.import_module("main")
        r = m.Remote(os.path.join(self.d, "claude-token-rotate.sock"))
        ids = [m.pin_id(v["tok"]) for v in self.world.c.values()]   # CSV order
        row = (lambda k: ids[int(k.strip()) - 1])
        k0 = seq[0]
        if k0 == "p":
            r.call("pin", id=row(seq[1]))
        elif k0 == "n":
            r.call("disable", id=row(seq[1]))
        elif k0 == "u":
            r.call("unpin")
        elif k0 == "T":
            r.call("rotate_mode", mode="")
        elif k0 == "z":
            r.call("toggle_live")
        r.close()

    # state as files say it
    def state(self) -> dict:
        with open(self.path("creds.json")) as fh:
            blk = json.load(fh).get("claudeAiOauth") or {}
        live = self.names.get(blk.get("accessToken") or "", "/login")
        on = bool(blk.get("accessToken")) and not blk.get("refreshToken")
        try:
            with open(self.path("data.json")) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        sys.path.insert(0, self.code)
        import importlib
        m = importlib.import_module("main")
        by_id = {m.pin_id(v["tok"]): n for n, v in self.world.c.items()}
        burn = st.get("burn") or {}
        return {"live": live, "on": on, "pin": by_id.get(st.get("pinned")),
                "burn": by_id.get(burn.get("id")) if burn.get("until", 0) > self.clock else None}

    def run(self) -> list[dict]:
        self.setup()
        self.start()
        interval = 600.0
        keys, blips, restarts = self.sc.get("keys", {}), self.sc.get("blips", {}), \
            set(self.sc.get("restart", []))
        self.wait_frame(self.clock)
        for step in range(self.sc["steps"]):
            st = self.state()
            when = dt.datetime.fromtimestamp(self.clock).strftime("%a %H:%M")
            self.lines.append({"t": when, **st})
            new_swaps = [r for r in self.log_lines()[self.seen:] if "swap" in r]
            for r in new_swaps:
                self.lines.append({"t": dt.datetime.fromtimestamp(r["swap"]).strftime("%a %H:%M"),
                                   "swap": r["to"], "on": r["on"]})
            self.seen = len(self.log_lines())
            if self.daemon:
                if keys.get(step):
                    self.command(keys[step])
                    self.drain(0.3)          # as a screen does: a refresh it asked for lands now
            else:
                for k in keys.get(step, []):
                    os.write(self.fd, k.encode())
                    self.drain(0.6)
            if step in restarts:
                self.stop()
                self.start()
                self.wait_frame(self.clock)
            nxt = self.clock + interval
            self.world.advance(self.clock, nxt, st["live"], st["on"])
            self.clock = nxt
            self.write_world(blips.get(step + 1, []))
            self.write_clock()
            self.wake()
            self.wait_frame(self.clock)
        self.stop()
        return self.lines


def run_scenario(code: str, name: str, daemon: bool = False) -> list[dict]:
    d = tempfile.mkdtemp(prefix=f"ctr-sim-{name}-")
    try:
        return Run(code, name, SCENARIOS[name], d, daemon).run()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def as_text(lines: list[dict]) -> list[str]:
    return [json.dumps(x, sort_keys=True) for x in lines]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--code", default=REPO, help="directory holding the main.py to run")
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--record", action="store_true", help="write tests/golden from --code")
    ap.add_argument("--golden", action="store_true", help="compare --code against tests/golden")
    ap.add_argument("--show", action="store_true", help="print the timelines")
    ap.add_argument("--daemon", action="store_true",
                    help="run each scenario through `daemon` and its socket instead of a screen")
    a = ap.parse_args()
    names = a.only.split(",") if a.only else list(SCENARIOS)
    bad = 0
    for name in names:
        lines = as_text(run_scenario(os.path.abspath(a.code), name, a.daemon))
        if a.show:
            print(f"== {name}")
            print("\n".join(lines))
        gold = os.path.join(GOLDEN, f"{name}.jsonl")
        if a.record:
            os.makedirs(GOLDEN, exist_ok=True)
            with open(gold, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            print(f"recorded {name}: {len(lines)} lines")
        if a.golden:
            with open(gold) as fh:
                want = fh.read().splitlines()
            if want == lines:
                print(f"  same  {name} ({len(lines)} lines)")
            else:
                bad += 1
                print(f"  DIFF  {name}")
                for ln in list(difflib.unified_diff(want, lines, "golden", "now", lineterm=""))[:40]:
                    print("    " + ln)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
