#!/usr/bin/env python3
"""Regression tests for the parts of claude-token-rotate that write to disk.

Standard library only and no framework, matching the tool itself: `python3 tests/run.py`.

NOTHING HERE TOUCHES A REAL FILE. Every case builds its own CSV and shell rc in a temp directory,
because the code under test edits a shell rc that holds live credentials and a mistake in a test
would be indistinguishable from a mistake in the tool.

Shell behaviour is checked by actually running zsh with a value already in the environment. That
is the only way to catch the failure this suite exists for: commenting an `export` out looks
correct in the file and does nothing to a shell that inherited the variable from its parent.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m                                                        # noqa: E402

RESULTS: list[tuple[bool, str]] = []
INHERITED = "sk-ant-oat01-INHERITED-FROM-A-PARENT-PROCESS"


def check(label: str, cond: object) -> None:
    RESULTS.append((bool(cond), label))


def tok(ch: str) -> str:
    return "sk-ant-oat01-" + ch * 90 + "AAA"


def reading(p5: float, p7: float) -> dict[str, object]:
    return {"u5h": f"{p5 / 100:.2f}", "u7d": f"{p7 / 100:.2f}",
            "s5h": "allowed", "s7d": "allowed", "ok": True, "code": 200, "ts": 0}


def shell_sees(rc: str) -> str:
    """What a zsh that ALREADY has the variable set ends up with after sourcing `rc`."""
    out = subprocess.run(
        ["zsh", "-c", f"source {rc}; echo ${{CLAUDE_CODE_OAUTH_TOKEN:-<EMPTY>}}"],
        env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": INHERITED},
        capture_output=True, text=True).stdout.strip()
    if out == "<EMPTY>":
        return "EMPTY"
    return "INHERITED" if "INHERITED" in out else out


def store_with(d: str, **rows: str) -> "m.Store":
    path = os.path.join(d, "token.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Name,CLAUDE_CODE_OAUTH_TOKEN\n")
        for name, value in rows.items():
            fh.write(f"{name},{value}\n")
    return m.Store.from_csv(path)


def test_disable_beats_inheritance(d: str) -> None:
    """Switching off must clear a value the shell inherited, not merely stop setting it."""
    store = store_with(d, live=tok("L"))
    rc = os.path.join(d, "rc1")
    with open(rc, "w", encoding="utf-8") as fh:
        fh.write(f'export {m.TOKEN_COL}="{tok("L")}"\n')
    check("on: the shell gets the token", shell_sees(rc) == tok("L"))
    m.env_toggle(rc)
    check("off: the shell is clean despite inheritance", shell_sees(rc) == "EMPTY")
    check("off: state agrees", m.env_state(rc, store)["active"] is False)
    m.env_toggle(rc)
    check("on again: the token is back", shell_sees(rc) == tok("L"))
    check("on again: the unset line is gone", not m.env_state(rc, store)["unsets"])


def test_parked_injection(d: str) -> None:
    """A switched-off variable may be kept fresh, but automation may not switch it back on."""
    store = store_with(d, live=tok("L"), fresh=tok("G"))
    rc = os.path.join(d, "rc2")
    with open(rc, "w", encoding="utf-8") as fh:
        fh.write(f'export {m.TOKEN_COL}="{tok("L")}"\n')
    m.env_toggle(rc)                                  # the user switches it off

    m.env_set(rc, store, tok("G"), activate=None)     # auto-rotate refreshes it
    st = m.env_state(rc, store)
    check("parked: stays off", st["active"] is False and st["unsets"])
    check("parked: shell stays clean", shell_sees(rc) == "EMPTY")
    check("parked: value was still updated", st["token"] == tok("G"))

    m.env_set(rc, store, tok("G"), activate=True)     # a person injects it
    check("manual: switched on", m.env_state(rc, store)["active"] is True)
    check("manual: reaches the shell", shell_sees(rc) == tok("G"))
    m.env_set(rc, store, tok("L"), activate=None)
    check("preserve keeps ON as well", m.env_state(rc, store)["active"] is True)


def test_both_windows_decide(d: str) -> None:
    """A credential is usable only while BOTH windows are under their limits."""
    check("5h 60% is usable", m.usable(reading(60, 10)))
    check("5h 61% is not", not m.usable(reading(61, 10)))
    check("weekly 74% is usable", m.usable(reading(10, 74)))
    check("weekly 75% is not", not m.usable(reading(10, 75)))
    check("a missing window is never usable", not m.usable({"u5h": "0.10"}))
    check("a spent credential is never usable",
          not m.usable({**reading(10, 10), "err": "unauthorized"}))
    # No weekend or working-day arithmetic: the weekly limit is the same whenever it resets.
    soon = {**reading(10, 70), "r7d": str(time.time() + 3600)}
    later = {**reading(10, 70), "r7d": str(time.time() + 6 * 86400)}
    check("weekly limit ignores how far off the reset is", m.usable(soon) and m.usable(later))

    store = store_with(d, live=tok("L"), weekly_spent=tok("W"), good=tok("G"), hot=tok("H"))
    res = {tok("L"): reading(80, 10), tok("W"): reading(4, 96), tok("G"): reading(20, 20),
           tok("H"): reading(61, 5)}
    pick = m.rotate_pick(store, store.rows, res, exclude=tok("L"), strict=True)
    check("skips the 4%/96% trap and the 61% 5h", pick is not None and store.name(pick[0]) == "good")
    check("ranks on the busiest window", pick is not None and pick[1] == 20.0)
    spent = {tok("L"): reading(80, 10), tok("W"): reading(90, 10), tok("G"): reading(10, 88),
             tok("H"): reading(70, 70)}
    check("a strict pick refuses when nothing is usable",
          m.rotate_pick(store, store.rows, spent, exclude=tok("L"), strict=True) is None)
    check("a forced pick still answers when nothing is usable",
          m.rotate_pick(store, store.rows, spent, exclude=tok("L")) is not None)


def test_file_handling(d: str) -> None:
    """Surrounding lines, permissions and backups survive a write."""
    store = store_with(d, live=tok("L"), other=tok("G"))
    rc = os.path.join(d, "rc3")
    body = f'# line A\n# line B\nexport {m.TOKEN_COL}="{tok("L")}"\n# line C\n'
    with open(rc, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(rc, 0o644)
    before = body.splitlines()
    m.env_set(rc, store, tok("G"))
    after = open(rc, encoding="utf-8").read().splitlines()
    check("only the assignment line changed",
          [i for i, (a, b) in enumerate(zip(before, after)) if a != b] == [2])
    check("file mode preserved, not forced to 0600", os.stat(rc).st_mode & 0o777 == 0o644)
    check("both backups written",
          os.path.exists(rc + m.ENV_BAK) and os.path.exists(rc + m.ENV_ORIG))

    for label, line in (("double quotes", f'export {m.TOKEN_COL}="{tok("L")}"'),
                        ("single quotes", f"export {m.TOKEN_COL}='{tok('L')}'"),
                        ("unquoted", f'export {m.TOKEN_COL}={tok("L")}'),
                        ("indented", f'   export {m.TOKEN_COL}="{tok("L")}"'),
                        ("commented out", f'# export {m.TOKEN_COL}="{tok("L")}"')):
        p = os.path.join(d, "v" + label.replace(" ", ""))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"# doc\n{line}\n")
        check(f"parses {label}", m.env_state(p, store)["token"] == tok("L"))


def test_refusals(d: str) -> None:
    """Refusing is better than guessing when the file or the value is wrong."""
    store = store_with(d, live=tok("L"))
    two = os.path.join(d, "rc4")
    with open(two, "w", encoding="utf-8") as fh:
        fh.write(f'export {m.TOKEN_COL}="a"\nexport {m.TOKEN_COL}="b"\n')
    try:
        m.env_set(two, store, tok("G"))
        check("refuses a file with two live lines", False)
    except RuntimeError:
        check("refuses a file with two live lines", True)

    rc = os.path.join(d, "rc5")
    with open(rc, "w", encoding="utf-8") as fh:
        fh.write(f'export {m.TOKEN_COL}="{tok("L")}"\n')
    try:
        m.env_set(rc, store, 'abc"; rm -rf /')
        check("refuses a token that would break quoting", False)
    except RuntimeError:
        check("refuses a token that would break quoting", True)

    check("rejects a duplicate token",
          m.validate_token(store, tok("L")) != "")
    check("a row's own token is not a duplicate",
          m.validate_token(store, tok("L"), skip=store.rows[0]) == "")


def test_session_detection(d: str) -> None:
    """Only real sessions count, and this tool must never count itself.

    The executable name is the whole test, deliberately. CLAUDE_CODE_ENTRYPOINT and CLAUDECODE
    look like tidier markers but every child a session spawns inherits them, so a hook or a
    notification helper would be reported as a session still holding the credential.
    """
    for name, want in (("claude", True), ("2.1.282", True), ("2.1.281", True),
                       ("claude-token-rotate", False), ("claude-alert", False),
                       ("notify-send", False), ("node", False), ("zsh", False)):
        check(f"exe {name!r} {'counts' if want else 'is ignored'}",
              bool(m.CLAUDE_EXE_RE.match(name)) is want)

    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": tok("S")}
    marked = subprocess.Popen(["sleep", "20"],
                              env={**env, "CLAUDE_CODE_ENTRYPOINT": "claude-vscode",
                                   "CLAUDECODE": "1"})
    plain = subprocess.Popen(["sleep", "20"], env=env)
    try:
        time.sleep(0.4)
        pids = [pid for pid, _ in m.sessions_on(tok("S"))]
        check("a token-carrying helper is not a session", plain.pid not in pids)
        check("an inherited CLAUDE_CODE_* marker does not promote a helper",
              marked.pid not in pids)
    finally:
        for q in (marked, plain):
            q.kill()
            q.wait()
    check("empty for a token nothing holds", m.sessions_on(tok("Z")) == [])
    check("empty for an empty token", m.sessions_on("") == [])


def test_pick_soonest_reset(d: str) -> None:
    """Among usable credentials, spend first the quota that resets soonest — it is lost otherwise.

    Weekly decides, because the weekly quota is the scarce one; the 5h reset breaks a tie between
    weekly resets less than an hour apart. Numbers from a real dashboard where Farhan won on CSV
    order alone — tied with Bernard on max(5h, weekly) = 60 — while Bernard's week ended in 10h.
    """
    now = time.time()
    H = 3600

    def row(p5: float, p7: float, r5: float | None, r7: float | None) -> dict[str, object]:
        r = reading(p5, p7)
        if r5 is not None:
            r["r5h"] = str(now + r5)
        if r7 is not None:
            r["r7d"] = str(now + r7)
        return r

    store = store_with(d, trias=tok("T"), farhan=tok("F"), nico=tok("N"), bernard=tok("B"))
    res = {tok("T"): row(61, 35, 0.4 * H, 132 * H), tok("F"): row(30, 60, 3.55 * H, 89 * H),
           tok("N"): row(10, 73, 1.7 * H, 42 * H), tok("B"): row(2, 60, 3.2 * H, 10 * H)}
    pick = m.rotate_pick(store, store.rows, res, exclude=tok("T"), strict=True)
    check("soonest weekly reset wins: bernard, not farhan by CSV order",
          pick is not None and store.name(pick[0]) == "bernard")
    check("weekly beats a nearer 5h reset (nico)", pick is not None and store.name(pick[0]) != "nico")

    s2 = store_with(d, x=tok("X"), y=tok("Y"))
    near = {tok("X"): row(10, 10, 3 * H, 10 * H), tok("Y"): row(10, 10, 1 * H, 10.5 * H)}
    check("weekly resets under 1h apart: the nearer 5h reset decides",
          store.name(m.rotate_pick(s2, s2.rows, near, strict=True)[0]) == "y")
    far = {tok("X"): row(10, 10, 3 * H, 10 * H), tok("Y"): row(10, 10, 0.2 * H, 11.1 * H)}
    check("weekly resets over 1h apart: weekly still decides",
          store.name(m.rotate_pick(s2, s2.rows, far, strict=True)[0]) == "x")
    blind = {tok("X"): row(10, 10, None, None), tok("Y"): row(40, 40, 3 * H, 50 * H)}
    check("an unknown reset ranks last",
          store.name(m.rotate_pick(s2, s2.rows, blind, strict=True)[0]) == "y")
    check("a forced pick still takes the most headroom",
          store.name(m.rotate_pick(s2, s2.rows, blind)[0]) == "x")


def test_rotation_rules(d: str) -> None:
    """When auto-rotate moves off the live credential — pinned or not.

    A pin is a person's choice, so it gets slack an unpinned credential does not: when its 5h
    window resets within the hour it is kept until 95%, because the quota is about to come back.
    Further out than that, or over on the weekly window, it is rotated like any other.
    """
    now = 1_800_000_000.0

    def live(p5: float, p7: float, reset_in: float) -> dict[str, object]:
        return {**reading(p5, p7), "r5h": str(now + reset_in)}

    rot = (lambda r, pinned: m.needs_rotate(r, pinned, now=now)[0])
    check("unpinned: under both limits stays", not rot(live(60, 74, 7200), False))
    check("unpinned: 5h 61% rotates", rot(live(61, 10, 7200), False))
    check("unpinned: weekly 75% rotates", rot(live(10, 75, 7200), False))
    check("unpinned: a near 5h reset changes nothing", rot(live(61, 10, 600), False))
    check("pinned: under both limits stays", not rot(live(50, 10, 7200), True))
    check("pinned: 5h over, reset more than 1h away -> rotated", rot(live(70, 10, 7200), True))
    check("pinned: 5h over, reset in 45 min -> held", not rot(live(70, 10, 2700), True))
    check("pinned: 5h over, reset in exactly 1h -> held", not rot(live(70, 10, 3600), True))
    check("pinned: 5h over, reset in 20 min -> held", not rot(live(94, 10, 1200), True))
    check("pinned: held only until 95%", rot(live(95, 10, 1200), True))
    check("pinned: weekly over -> rotated even near a 5h reset", rot(live(10, 80, 1200), True))
    check("pinned: weekly spent -> rotated",
          rot({**live(10, 100, 7200), "s7d": "rejected", "ok": False, "code": 429}, True))
    check("pinned: spent -> rotated",
          rot({**live(100, 10, 1200), "s5h": "rejected", "ok": False, "code": 429}, True))
    check("pinned: an unknown reset counts as far off", rot(reading(70, 10), True))
    why = m.needs_rotate(live(70, 10, 2700), True, now=now)[1]
    check("a held pin says why", "95" in why)


def test_park_mode(d: str) -> None:
    """Parked rotation must leave a new terminal with nothing at all.

    Writing the value commented out is not enough on its own: a terminal inherits the variable
    from the desktop session before it ever reads the file, so the `unset` is what makes "off"
    actually mean off. Without it, a new session would quietly keep using whichever token was
    live when the desktop started.
    """
    store = store_with(d, old=tok("O"), picked=tok("P"))
    rc = os.path.join(d, "park")
    with open(rc, "w", encoding="utf-8") as fh:
        fh.write(f'# doc\nexport {m.TOKEN_COL}="{tok("O")}"\n')

    check("active to begin with", shell_sees(rc) == tok("O"))
    m.env_set(rc, store, tok("P"), activate=False)          # what `park` does
    st = m.env_state(rc, store)
    check("park: a new terminal gets nothing", shell_sees(rc) == "EMPTY")
    check("park: the rotator's pick is still recorded", st["token"] == tok("P"))
    check("park: recorded but not live", st["active"] is False)
    check("park: an unset line is what enforces it", bool(st["unsets"]))

    m.env_toggle(rc)
    check("z then hands over the parked pick", shell_sees(rc) == tok("P"))


def test_pin_lifts_when_spent(d: str) -> None:
    """A pinned credential is used until it is spent — and then auto-rotate must move off it.

    A spent window answers 429 WITH its quota headers, so the probe sets no `err` and only the
    status says so ("5h rejected"). Missing that is how a pin outlived its credential.
    """
    at75 = reading(75, 40)
    spent5 = {**reading(100, 40), "ok": False, "code": 429, "s5h": "rejected"}
    spent7 = {**reading(20, 100), "ok": False, "code": 429, "s7d": "rejected"}
    check("pin: holds at 75% — not spent yet", m.pin_holds(at75))
    check("pin: lifts when the 5h window is spent", not m.pin_holds(spent5))
    check("pin: lifts when the weekly window is spent", not m.pin_holds(spent7))
    check("pin: lifts on unauthorized", not m.pin_holds({"err": "unauthorized"}))

    # data.json must never hold a credential — the pin is recorded as a digest.
    check("pin id is not the token", m.pin_id(tok("A")) != tok("A") and "sk-ant" not in m.pin_id(tok("A")))
    check("pin id of a pin id is itself (a file already migrated stays put)",
          m.pin_id(m.pin_id(tok("A"))) == m.pin_id(tok("A")))
    check("pin ids tell tokens apart", m.pin_id(tok("A")) != m.pin_id(tok("B")))


LOGIN = {"accessToken": "sk-ant-oat01-LOGIN-ACCESS", "refreshToken": "sk-ant-ort01-LOGIN-REFRESH",
         "expiresAt": 1, "scopes": ["user:inference", "user:profile"],
         "subscriptionType": "team", "rateLimitTier": "default_claude_max"}
MCP = {"linear|x": {"serverName": "linear", "accessToken": ""}}


def creds_with(d: str, name: str, block: object) -> str:
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"mcpOAuth": MCP, "claudeAiOauth": block}, fh)
    return path


def creds_block(path: str) -> dict[str, object]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_creds_follow(d: str) -> None:
    """The credentials file carries the injected token while on, and the exact /login while off."""
    path = creds_with(d, "creds1.json", LOGIN)
    parked = path + m.CREDS_LOGIN

    m.creds_inject(path, tok("A"))
    doc = creds_block(path)
    o = doc["claudeAiOauth"]
    check("inject: the token is in claudeAiOauth", o["accessToken"] == tok("A"))
    check("inject: no refresh token, so nothing refreshes it back", o["refreshToken"] is None)
    check("inject: expiry far enough out that it is never refreshed",
          o["expiresAt"] > (time.time() + 300 * 86400) * 1000)
    check("inject: plan fields carried over from the login", o.get("subscriptionType") == "team")
    check("inject: other keys untouched", doc["mcpOAuth"] == MCP)
    check("inject: the login is parked verbatim", creds_block(parked)["claudeAiOauth"] == LOGIN)
    check("inject: both files are 0600",
          os.stat(path).st_mode & 0o777 == 0o600 and os.stat(parked).st_mode & 0o777 == 0o600)

    m.creds_inject(path, tok("B"))
    check("re-inject: swaps the token", creds_block(path)["claudeAiOauth"]["accessToken"] == tok("B"))
    check("re-inject: the parked login is not replaced by an injected token",
          creds_block(parked)["claudeAiOauth"] == LOGIN)

    m.creds_restore(path)
    doc = creds_block(path)
    check("restore: the /login block is back exactly", doc["claudeAiOauth"] == LOGIN)
    check("restore: other keys untouched", doc["mcpOAuth"] == MCP)
    check("restore: nothing left parked", not os.path.exists(parked))
    before = open(path, encoding="utf-8").read()
    m.creds_restore(path)
    check("restore on a login is a no-op", open(path, encoding="utf-8").read() == before)

    # Someone runs /login while a token is injected: that login is newer than the parked one.
    m.creds_inject(path, tok("A"))
    fresh = {**LOGIN, "accessToken": "sk-ant-oat01-NEWER-LOGIN", "refreshToken": "sk-ant-ort01-N"}
    doc = creds_block(path)
    doc["claudeAiOauth"] = fresh
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    m.creds_restore(path)
    check("a newer /login is kept, not overwritten by the parked one",
          creds_block(path)["claudeAiOauth"] == fresh and not os.path.exists(parked))

    lone = creds_with(d, "creds2.json", {"accessToken": tok("S"), "refreshToken": None})
    before = open(lone, encoding="utf-8").read()
    try:
        m.creds_restore(lone)
        check("no parked login: refuses rather than signing everything out", False)
    except RuntimeError:
        check("no parked login: refuses rather than signing everything out",
              open(lone, encoding="utf-8").read() == before)

    bad = os.path.join(d, "creds3.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{half a file")
    try:
        m.creds_inject(bad, tok("A"))
        check("refuses to rewrite a file it cannot parse", False)
    except RuntimeError:
        check("refuses to rewrite a file it cannot parse",
              open(bad, encoding="utf-8").read() == "{half a file")

    fresh_file = os.path.join(d, "creds4.json")
    m.creds_sync(fresh_file, tok("A"), active=True)
    check("sync on: creates the file when there is none",
          creds_block(fresh_file)["claudeAiOauth"]["accessToken"] == tok("A"))
    synced = creds_with(d, "creds5.json", LOGIN)
    m.creds_sync(synced, tok("A"), active=True)
    m.creds_sync(synced, tok("A"), active=False)
    check("sync off: back to the login", creds_block(synced)["claudeAiOauth"] == LOGIN)


def test_file_mode(d: str) -> None:
    """File mode: the shell carries no token at all, and the credentials file carries the live one.

    That is what makes a swap reach sessions already open: a session that inherited the variable
    ignores the file for the rest of its life, so the variable must never be handed out.
    """
    store = store_with(d, a=tok("A"), b=tok("B"))
    rc = os.path.join(d, "fm_rc")
    with open(rc, "w", encoding="utf-8") as fh:
        fh.write(f'# doc\nexport {m.TOKEN_COL}="{tok("A")}"\n')
    creds = creds_with(d, "fm_creds.json", LOGIN)
    cblock = (lambda: creds_block(creds)["claudeAiOauth"])

    m.migrate_to_file(rc, creds, store)
    st = m.env_state(rc, store)
    check("migrate: the shell no longer gets the token", shell_sees(rc) == "EMPTY")
    check("migrate: the token is still recorded", st["token"] == tok("A") and not st["active"])
    check("migrate: the credentials file carries it", cblock()["accessToken"] == tok("A"))
    check("migrate: nothing to do the second time", m.migrate_to_file(rc, creds, store) == "")

    m.apply_live(rc, creds, store, tok("B"), on=True, file_mode=True)
    check("inject: the credentials file switches", cblock()["accessToken"] == tok("B"))
    check("inject: the shell stays clean", shell_sees(rc) == "EMPTY")
    check("inject: recorded for z", m.env_state(rc, store)["token"] == tok("B"))

    msg, on = m.toggle_live(rc, creds, store, file_mode=True)
    check("z off: back on the /login", not on and cblock() == LOGIN)
    check("z off: the shell stays clean", shell_sees(rc) == "EMPTY")
    m.apply_live(rc, creds, store, tok("A"), on=None, file_mode=True)
    check("auto while off: the record is refreshed", m.env_state(rc, store)["token"] == tok("A"))
    check("auto while off: the /login stays", cblock() == LOGIN)
    msg, on = m.toggle_live(rc, creds, store, file_mode=True)
    check("z on: the recorded token goes live", on and cblock()["accessToken"] == tok("A"))
    m.apply_live(rc, creds, store, tok("B"), on=None, file_mode=True)
    check("auto while on: the live token follows", cblock()["accessToken"] == tok("B"))
    m.apply_live(rc, creds, store, tok("A"), on=False, file_mode=True)
    check("park: back on the /login, pick recorded",
          cblock() == LOGIN and m.env_state(rc, store)["token"] == tok("A"))
    check("creds_on reads the switch", not m.creds_on(creds))

    rc2 = os.path.join(d, "fm_rc2")
    with open(rc2, "w", encoding="utf-8") as fh:
        fh.write(f'export {m.TOKEN_COL}="{tok("A")}"\n')
    creds2 = creds_with(d, "fm_creds2.json", LOGIN)
    m.apply_live(rc2, creds2, store, tok("B"), on=True, file_mode=False)
    check("env mode: the shell gets the token", shell_sees(rc2) == tok("B"))
    check("env mode: the credentials file follows", creds_block(creds2)["claudeAiOauth"]["accessToken"] == tok("B"))


STATUSLINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "plugin", "statusline_command.md")


def account_cell(cfg: str, csv: str | None, env_tok: str = "", script: str = STATUSLINE,
                 path_dir: str = "") -> str:
    """The account cell (row 2, column 1) for a session configured by `cfg`.

    Run under a bash that has no token in its own environment, because the script falls back to
    its parent's /proc environ — and this suite's parent may well be a shell exporting a real one.
    `csv=None` leaves STATUSLINE_TOKEN_CSV unset, so the script has to find the CSV itself.
    """
    env = {"PATH": (path_dir + ":" if path_dir else "") + "/usr/local/bin:/usr/bin:/bin",
           "HOME": cfg, "CLAUDE_CONFIG_DIR": cfg}
    if csv is not None:
        env["STATUSLINE_TOKEN_CSV"] = csv
    if env_tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = env_tok
    payload = json.dumps({"model": {"display_name": "Opus"}})
    out = subprocess.run(["bash", "-c", f"{script!r}; true"], input=payload, env=env,
                         capture_output=True, text=True).stdout
    rows = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in out.splitlines()]
    return rows[1].split("│")[0].strip() if len(rows) > 1 else ""


def test_statusline_account(d: str) -> None:
    """The account cell names the credential in use: its CSV name, or the /login email."""
    cfg = os.path.join(d, "slcfg")
    os.makedirs(cfg)
    with open(os.path.join(cfg, ".claude.json"), "w", encoding="utf-8") as fh:
        json.dump({"oauthAccount": {"emailAddress": "me@example.com"}}, fh)
    csv = os.path.join(d, "sl.csv")
    with open(csv, "w", encoding="utf-8") as fh:
        fh.write(f"Note,CLAUDE_CODE_OAUTH_TOKEN,Name\nx,{tok('A')},alice\ny,{tok('B')},bob\n")
    creds = os.path.join(cfg, ".credentials.json")

    with open(creds, "w", encoding="utf-8") as fh:
        json.dump({"claudeAiOauth": LOGIN}, fh)
    check("statusline: /login shows the email", account_cell(cfg, csv) == "me@example.com")

    m.creds_inject(creds, tok("A"))
    check("statusline: an injected token shows its CSV name",
          account_cell(cfg, csv) == f"alice sk...{tok('A')[-8:]}")

    m.creds_inject(creds, tok("Q"))
    check("statusline: an injected token outside the CSV keeps the Token label",
          account_cell(cfg, csv) == f"Token sk...{tok('Q')[-8:]}")

    check("statusline: the session's own variable wins over the file",
          account_cell(cfg, csv, env_tok=tok("B")) == f"bob sk...{tok('B')[-8:]}")

    # Installed as a copy away from the repo, the CSV is found beside the rotator on PATH.
    m.creds_inject(creds, tok("A"))
    # Two levels down: the copy looks at ../token.csv first, and store_with() leaves one in `d`.
    repo, bindir, away = (os.path.join(d, x) for x in ("slrepo", "slbin", "slaway/sub"))
    for x in (repo, bindir, away):
        os.makedirs(x)
    shutil.copy(csv, os.path.join(repo, "token.csv"))
    rotator = os.path.join(repo, "claude-token-rotate")
    with open(rotator, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(rotator, 0o755)
    os.symlink(rotator, os.path.join(bindir, "claude-token-rotate"))
    copy = os.path.join(away, "statusline-command.sh")
    shutil.copy(STATUSLINE, copy)
    check("statusline: a copy away from the repo finds the CSV beside the rotator",
          account_cell(cfg, None, script=copy, path_dir=bindir) == f"alice sk...{tok('A')[-8:]}")
    check("statusline: no rotator on PATH keeps the Token label",
          account_cell(cfg, None, script=copy) == f"Token sk...{tok('A')[-8:]}")

    os.unlink(creds)
    check("statusline: no credentials file falls back to the email",
          account_cell(cfg, csv) == "me@example.com")


def main() -> int:
    d = tempfile.mkdtemp(prefix="ctr-tests-")
    try:
        for fn in (test_disable_beats_inheritance, test_parked_injection,
                   test_both_windows_decide, test_file_handling, test_refusals,
                   test_session_detection, test_rotation_rules, test_pick_soonest_reset,
                   test_park_mode,
                   test_creds_follow, test_statusline_account, test_pin_lifts_when_spent,
                   test_file_mode):
            fn(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for good, label in RESULTS:
        print(f"  {'PASS' if good else 'FAIL'}  {label}")
    passed = sum(1 for good, _ in RESULTS if good)
    print(f"\n  {passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
