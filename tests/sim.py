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
import collections
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
    # A Monday: a night spend-down whose reset is still 4h off ends at 07:00 and comes back inside
    # the 2h working-hours window; one somebody is on stays theirs; a later one is spent down too.
    "workburn": dict(start=at(2026, 10, 12, 5), steps=63, live="Eli",
                     creds=[("Eli", "D", 10, 30, 3.0, 90.0), ("Xeno", "X", 0, 70, 2.0, 6.0),
                            ("Ivy", "S", 0, 72, 2.0, 8.0), ("Finn", "N", 0, 75, 2.0, 9.5),
                            ("Ada", "A", 0, 20, 3.0, 120.0)],
                     user=[(at(2026, 10, 12, 5), at(2026, 10, 12, 15, 30))],
                     mate=[("Ivy", at(2026, 10, 12, 10), at(2026, 10, 12, 12), 6, 1)]),
    # Pacing: Finn, past 66% but behind its pace (its week ends Tuesday 13:00), is used once its pace
    # passes it; a week just begun has its slack; nights and the evening do not move a pace.
    "pace": dict(start=at(2026, 10, 12, 7), steps=29 * 6, live="Eli", extra=[],
                 creds=[("Eli", "D", 10, 20, 3.0, 120.0), ("Finn", "N", 0, 80, 2.0, 30.0),
                        ("Moe", "W", 0, 3, 3.0, 167.0), ("Ada", "A", 0, 30, 3.0, 80.0)],
                 user=[(at(2026, 10, 12, 9), at(2026, 10, 12, 18)),
                       (at(2026, 10, 13, 9), at(2026, 10, 13, 12))]),
    # Saturday: Lux, resetting tonight, is spent down until its owner sits down at noon while the
    # user is away — then it is left to them, and taken up again once idle for two hours.
    "foreign": dict(start=at(2026, 10, 17, 8), steps=10 * 6, live="Dora", extra=[],
                    creds=[("Dora", "D", 10, 20, 3.0, 120.0), ("Lux", "L", 0, 70, 2.0, 12.0),
                           ("Ada", "A", 0, 30, 3.0, 100.0)],
                    user=[(at(2026, 10, 17, 9), at(2026, 10, 17, 11, 30)),
                          (at(2026, 10, 17, 14, 30), at(2026, 10, 17, 17))],
                    mate=[("Lux", at(2026, 10, 17, 12), at(2026, 10, 17, 14), 25, 3)]),
}


# Waste-only scenarios (`--waste`): long runs that measure quota lost at weekly resets, no golden.
def _day(d, h, mi=0):
    return at(2026, 10, d, h, mi)


WASTE: dict[str, dict] = {
    # Monday 06:00 to Friday 22:00 with most weekly resets inside working hours; teammates on
    # their own credentials in the mornings and afternoons, the user on 09-12 and 13-18 and two
    # evenings.
    "workweek": dict(start=_day(12, 6), steps=112 * 6, live="Moe",
                     creds=[("Finn", "N", 0, 60, 2.0, 5.0), ("Moe", "W", 20, 55, 3.0, 10.0),
                            ("Ivy", "S", 0, 50, 4.0, 28.0), ("Gus", "K", 0, 45, 1.5, 33.0),
                            ("Ada", "A", 0, 40, 2.5, 51.5), ("Dora", "F", 0, 35, 3.5, 56.0),
                            ("Nell", "Y", 0, 30, 4.5, 77.0), ("Eli", "D", 0, 25, 1.0, 83.0),
                            ("Jude", "T", 0, 20, 2.0, 100.0), ("Kit", "B", 0, 15, 3.0, 105.0)],
                     user=[w for d in range(12, 17) for w in ((_day(d, 9), _day(d, 12)),
                                                              (_day(d, 13), _day(d, 18)))]
                          + [(_day(13, 20), _day(13, 23)), (_day(15, 20), _day(15, 23))],
                     mate=[("Ivy", _day(12, 9), _day(12, 12), 10, 2),
                           ("Gus", _day(13, 9), _day(13, 11), 10, 2),
                           ("Ada", _day(13, 13), _day(13, 17), 10, 2),
                           ("Nell", _day(14, 9), _day(14, 12), 10, 2),
                           ("Eli", _day(15, 13), _day(15, 16), 10, 2),
                           ("Jude", _day(16, 9), _day(16, 11), 10, 2)]),
}


def _team() -> dict:
    """The working week, with every owner on their own credential two hours each working day."""
    sc = dict(WASTE["workweek"])
    mates = []
    for i, (name, *_rest) in enumerate(sc["creds"]):
        for d in range(12, 17):
            h = 8 + (i * 37 + d * 13) % 10                # 08:00 to 17:00, spread out
            mates.append((name, _day(d, h), _day(d, h + 2), 25, 3))
    sc["mate"] = mates
    return sc


WASTE["team"] = _team()
# The weekend, with owners at work on their own credentials on Saturday and Sunday.
_d = lambda d, h, mi=0: at(2026, 10, d, h, mi)                          # noqa: E731
WASTE["weekend_owners"] = dict(SCENARIOS["weekend"], mate=[
    ("Gus", _d(10, 7), _d(10, 8, 30), 25, 3), ("Eli", _d(10, 10), _d(10, 12), 25, 3),
    ("Ada", _d(10, 13), _d(10, 16), 25, 3), ("Kit", _d(10, 21), _d(10, 23), 25, 3),
    ("Dora", _d(11, 10), _d(11, 13), 25, 3), ("Nell", _d(11, 15), _d(11, 18), 25, 3),
    ("Jude", _d(11, 20), _d(11, 22), 25, 3)])
# A Sunday with every older week past its pace and four weeks that began over the weekend.
WASTE["sunday"] = dict(start=_d(18, 7), steps=26 * 6, live="Moe",
                       creds=[("Moe", "W", 0, 74, 2.0, 52.0), ("Cal", "C", 0, 69, 2.0, 64.0),
                              ("Eli", "D", 0, 55, 2.0, 81.0), ("Nell", "Y", 0, 96, 2.0, 27.0),
                              ("Finn", "N", 0, 1, 3.0, 142.0), ("Ivy", "S", 0, 1, 3.0, 150.0),
                              ("Gus", "K", 0, 1, 3.0, 152.0), ("Ada", "A", 0, 0, 3.0, 168.0)],
                       user=[(_d(18, 9), _d(18, 12)), (_d(18, 14), _d(18, 18)),
                             (_d(18, 20), _d(18, 23)), (_d(19, 8), _d(19, 9))],
                       mate=[("Moe", _d(19, 8), _d(19, 9), 25, 3), ("Cal", _d(19, 8), _d(19, 9), 25, 3)])
# ...owners who need 12 points of their week every working day: 60 of it by Friday.
WASTE["team_heavy"] = dict(WASTE["team"], mate=[(n, a, b, 25, 6) for n, a, b, _, _ in WASTE["team"]["mate"]])


# --------------------------------------------------------------------------- the world

class World:
    """Quota that only moves when somebody spends it, and windows that reset on schedule."""

    def __init__(self, sc: dict):
        t0 = sc["start"]
        self.c = {name: {"tok": tok(ch), "u5": float(u5), "u7": float(u7),
                         "r5": t0 + h5 * H, "r7": t0 + h7 * H}
                  for name, ch, u5, u7, h5, h7 in sc["creds"]}
        self.user, self.mate = sc.get("user", []), sc.get("mate", [])
        self.rate = sc.get("rate", (15.0, 2.0))               # the user's pp/h on 5h and 7d
        self.lost: list[tuple[str, float, float]] = []      # (name, when, weekly % unused)
        self.used = 0.0                                       # weekly points the user spent
        self.mate_hot = 0          # owners who sat down to a 5h window at 61% or more
        self.mate_low = 0          # ...or to a week at 90% or more
        self.mate_refused = 0.0    # minutes owners were refused on their own credential
        self.cpu = 0.0             # CPU seconds of the user's Claude Code: 20 a minute at work

    def name_of(self, token: str) -> str | None:
        return next((n for n, v in self.c.items() if v["tok"] == token), None)

    def advance(self, t0: float, t1: float, live: str | None, on: bool) -> None:
        t = t0
        while t < t1:
            step = min(M, t1 - t)
            if any(a <= t < b for a, b in self.user):
                self.cpu += 20 * step / 60
            for name, v in self.c.items():
                r5 = r7 = 0.0
                if on and name == live and any(a <= t < b for a, b in self.user) \
                        and v["u5"] < 100 and v["u7"] < 100:
                    r5, r7 = self.rate
                    self.used += min(r7 * step / H, 100.0 - v["u7"])
                for mn, a, b, m5, m7 in self.mate:
                    if mn == name and t <= a < t + step:
                        self.mate_hot += v["u5"] >= 61
                        self.mate_low += v["u7"] >= 90
                    if mn == name and a <= t < b:
                        r5, r7 = r5 + m5, r7 + m7
                        if v["u5"] >= 100 or v["u7"] >= 100:
                            self.mate_refused += step / 60
                v["u5"] = min(100.0, v["u5"] + r5 * step / H)
                v["u7"] = min(100.0, v["u7"] + r7 * step / H)
            t += step
            for name, v in self.c.items():
                while t >= v["r5"]:
                    v["u5"], v["r5"] = 0.0, v["r5"] + 5 * H
                while t >= v["r7"]:
                    self.lost.append((name, v["r7"], 100.0 - v["u7"]))
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

    def claude_cpu(token):                       # the user's sessions: busy while they work
        with open(world_p) as fh:
            return {4242: json.load(fh).get("_cpu", 0.0)}

    m.claude_cpu = claude_cpu
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
        w["_cpu"] = self.world.cpu
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
        return (["daemon"] if self.daemon else []) + self.sc.get("extra", []) + ["--csv", self.path("token.csv"), "--env-file", self.path("zshenv"),
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
                lines = fh.read().split("\n")
        except OSError:
            return []
        # The last line may be half written; it is read whole on the next call.
        return [json.loads(x) for x in lines[:-1] if x.strip()]

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
        # The v1 timelines judge the other rules against a fixed weekly limit; `pace` has its own.
        sc = {"extra": ["--no-pace", "--no-foreign"], **SCENARIOS[name]}
        return Run(code, name, sc, d, daemon).run()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def as_text(lines: list[dict]) -> list[str]:
    return [json.dumps(x, sort_keys=True) for x in lines]


def m_day_off(when: float, night_from: float = 20, night_until: float = 7) -> bool:
    """Off hours as the user's .env has them (nights 20-07, weekends): for labels only."""
    t = dt.datetime.fromtimestamp(when)
    h = t.hour + t.minute / 60
    return t.weekday() >= 5 or h >= night_from or h < night_until


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--code", default=REPO, help="directory holding the main.py to run")
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--record", action="store_true", help="write tests/golden from --code")
    ap.add_argument("--golden", action="store_true", help="compare --code against tests/golden")
    ap.add_argument("--show", action="store_true", help="print the timelines")
    ap.add_argument("--waste", metavar="FLAGS", nargs="?", const="",
                    help="report quota spent and quota lost at weekly resets, with these extra "
                         "flags (quoted), instead of comparing timelines")
    ap.add_argument("--rate", help="with --waste: the user's usage as '5h,7d' pp per hour")
    ap.add_argument("--detail", action="store_true", help="with --waste: every reset, one per line")
    ap.add_argument("--daemon", action="store_true",
                    help="run each scenario through `daemon` and its socket instead of a screen")
    a = ap.parse_args()
    names = a.only.split(",") if a.only else list(SCENARIOS)
    bad = 0
    if a.waste is not None:
        for name in names:
            sc = {**{**SCENARIOS, **WASTE}[name], "extra": a.waste.split()}
            if a.rate:
                sc["rate"] = tuple(float(x) for x in a.rate.split(","))
            d = tempfile.mkdtemp(prefix=f"ctr-waste-{name}-")
            try:
                run = Run(os.path.abspath(a.code), name, sc, d, a.daemon)
                lines = run.run()
            finally:
                shutil.rmtree(d, ignore_errors=True)
            lost = sum(x[2] for x in run.world.lost)
            swaps = sum(1 for x in lines if "swap" in x)
            w = run.world
            live = collections.Counter(x["live"] for x in lines if x.get("live"))
            print(f"{name:10} used {w.used:6.1f} pts · lost at resets {lost:6.1f} pts "
                  f"over {len(w.lost)} resets · {swaps} swaps · owners: {w.mate_hot} found 5h "
                  f"≥61%, {w.mate_low} found weekly ≥90%, {w.mate_refused:.0f} min refused")
            print("    live (refreshes):", ", ".join(f"{n} {c}" for n, c in live.most_common()))
            if a.detail:
                for who, when, left in run.world.lost:
                    work = "work" if not m_day_off(when) else "off "
                    print(f"    {who:8} {dt.datetime.fromtimestamp(when):%a %H:%M} {work} "
                          f"lost {left:5.1f}")
        return 0
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
