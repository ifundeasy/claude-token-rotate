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
import datetime
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
    check("weekly 65% is usable", m.usable(reading(10, 65)))
    check("weekly 66% is not", not m.usable(reading(10, 66)))
    check("a missing window is never usable", not m.usable({"u5h": "0.10"}))
    check("a spent credential is never usable",
          not m.usable({**reading(10, 10), "err": "unauthorized"}))
    # No weekend or working-day arithmetic: the weekly limit is the same whenever it resets.
    soon = {**reading(10, 60), "r7d": str(time.time() + 3600)}
    later = {**reading(10, 60), "r7d": str(time.time() + 6 * 86400)}
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


def test_rows_added_while_running(d: str) -> None:
    """A credential added while the dashboard runs must be as known as one loaded at startup.

    The live credential is NAMED from the full list, and auto-rotate refuses to judge a token it
    cannot name. That list used to be a copy taken at startup, so a credential added with `a`
    never entered it: once auto-rotate picked it, the live token had no name and auto-rotate went
    silent — and the credential was ridden past 100% of its 5h window.
    """
    store = store_with(d, a=tok("A"), b=tok("B"), c=tok("C"))
    store.add("new", tok("N"))
    check("added while running: the live token is named", m.token_name(store, tok("N")) == "new")
    rc = os.path.join(d, "rows_rc")
    with open(rc, "w", encoding="utf-8") as fh:
        fh.write(f'export {m.TOKEN_COL}="{tok("N")}"\n')
    check("added while running: the shell file's token is named too",
          m.env_state(rc, store)["name"] == "new")

    # --only narrows the VIEW. Adding, removing and saving still work on the whole file.
    store.only({"a"})
    check("--only: the view is narrowed", [store.name(r) for r in store.rows] == ["a"])
    store.add("late", tok("L"))
    check("--only: a credential added in the view is named", m.token_name(store, tok("L")) == "late")
    check("--only: a hidden credential is still named", m.token_name(store, tok("B")) == "b")
    store.remove(store.rows[0])
    store.save()
    saved = [r["Name"] for r in csv_rows(store.path)]
    check("--only: saving keeps the credentials the view hides",
          saved == ["b", "c", "new", "late"])
    check("removed: no longer named", m.token_name(store, tok("A")) is None)


def csv_rows(path: str) -> list[dict[str, str]]:
    import csv as _csv
    with open(path, encoding="utf-8", newline="") as fh:
        return list(_csv.DictReader(fh))


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
    check("unpinned: under both limits stays", not rot(live(60, 65, 7200), False))
    check("unpinned: 5h 61% rotates", rot(live(61, 10, 7200), False))
    check("unpinned: weekly 66% rotates", rot(live(10, 66, 7200), False))
    check("unpinned: a near 5h reset holds it too, like a pin", not rot(live(61, 10, 600), False))
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


def test_header_sort(d: str) -> None:
    """Clicking a header sorts by it and a second click reverses; keys arrive whole.

    The reader used to take one byte at a time, so an arrow key or a mouse report came through as
    separate characters: the left arrow (ESC [ D) ran diagnose, and a click typed its coordinates
    as row numbers to copy.
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

    store = store_with(d, alice=tok("A"), bob=tok("B"), carol=tok("C"), dave=tok("D"))
    res = {tok("A"): row(10, 60, 2 * H, 72 * H), tok("B"): row(40, 20, 0.5 * H, 24 * H),
           tok("C"): row(5, 5, 3 * H, 144 * H)}            # dave has no reading at all
    names = (lambda how, desc=None: [store.name(r) for r in
                                     m.sort_rows(store, store.rows, res, how, desc)])
    check("sort 7d: busiest week first by default", names("7d") == ["alice", "bob", "carol", "dave"])
    check("sort 7d reversed: idlest first, unread still last",
          names("7d", False) == ["carol", "bob", "alice", "dave"])
    check("sort r5h: soonest 5h reset first", names("r5h") == ["bob", "alice", "carol", "dave"])
    check("sort r7d: soonest weekly reset first", names("r7d") == ["bob", "alice", "carol", "dave"])
    check("sort r7d reversed: latest reset first, unknown still last",
          names("r7d", True) == ["carol", "alice", "bob", "dave"])
    check("sort name reversed: Z to A", names("name", True) == ["dave", "carol", "bob", "alice"])
    check("sort csv reversed: bottom row first", names("csv", True)[0] == "dave")

    check("split_key: plain key", m.split_key("ab") == ("a", "b"))
    check("split_key: arrow is one key", m.split_key("\x1b[Dq") == ("\x1b[D", "q"))
    check("split_key: mouse report is one key",
          m.split_key("\x1b[<0;12;5M1") == ("\x1b[<0;12;5M", "1"))
    check("split_key: half a sequence waits", m.split_key("\x1b[<0;1") is None)
    check("split_key: lone ESC waits", m.split_key("\x1b") is None)
    check("split_key: SS3 key", m.split_key("\x1bOPx") == ("\x1bOP", "x"))
    check("mouse_click: left press", m.mouse_click("\x1b[<0;12;5M") == (12, 5))
    check("mouse_click: release is not a click", m.mouse_click("\x1b[<0;12;5m") is None)
    check("mouse_click: wheel is not a click", m.mouse_click("\x1b[<64;12;5M") is None)
    check("mouse_click: an arrow is not a click", m.mouse_click("\x1b[D") is None)

    hits: dict = {}
    rows = m.sort_rows(store, store.rows, res, "7d")
    frame = m.render(store, rows, res, {}, mode="b", sort="7d", interval=60, last=now, probes=4,
                     flash="", color=False, cols=160, live=True, alert=None, inspect=None,
                     hits=hits)
    lines = frame.split("\n")
    head = lines[hits["line"]]
    hits["row"] = m.header_row(hits["line"], frame, 200, 400)
    at = {}
    for x0, x1, order in hits["spans"]:
        at.setdefault(head[x0 - 1:x1].strip(), order)
    check("header click map: each label sorts by its own column",
          at.get("NAME") == "name" and at.get("5H") == "5h" and at.get("7D▼") == "7d"
          and at.get("#") == "csv" and "EXTRA" not in at)
    resets = [o for x0, x1, o in hits["spans"] if head[x0 - 1:x1].strip() == "RESET"]
    check("header click map: the two RESET columns sort by their own window",
          resets == ["r5h", "r7d"])
    check("header marks the active order", "7D▼" in head and "▲" not in head)
    x_name = head.index("NAME") + 1
    check("a click on NAME in the header row sorts by name",
          m.header_order(hits, (x_name, hits["row"])) == "name")
    check("a click one row below the header does nothing",
          m.header_order(hits, (x_name, hits["row"] + 1)) is None)
    check("a click on TOKEN does nothing",
          m.header_order(hits, (head.index("TOKEN") + 1, hits["row"])) is None)
    n = frame.count("\n") + 1
    check("header row: a frame that fits stays put", m.header_row(4, frame, n + 1, 400) == 5)
    check("header row: a frame 2 rows too tall moves the header up 2",
          m.header_row(4, frame, n - 1, 400) == 3)
    wide = "x\n" + "y" * 25 + "\nHEADER\nz"            # line 1 wraps onto 3 rows at width 10
    check("header row: a wrapped line above pushes the header down",
          m.header_row(2, wide, 50, 10) == 5)
    check("header row: a line exactly as wide as the window is one row",
          m.header_row(2, "x\n" + "y" * 10 + "\nHEADER", 50, 10) == 3)
    check("header row: wrapped rows count toward the scroll",
          m.header_row(2, wide, 6, 10) == 4)

    x10 = "\x1b[M" + chr(32) + chr(32 + 52) + chr(32 + 7)
    check("split_key: a legacy mouse report is one key, its bytes not replayed as T/z/q",
          m.split_key(x10 + "s") == (x10, "s"))
    check("split_key: a legacy report still arriving waits", m.split_key("\x1b[M ") is None)
    check("mouse_click: legacy left press", m.mouse_click(x10) == (52, 7))
    check("mouse_click: legacy release is not a click",
          m.mouse_click("\x1b[M" + chr(35) + chr(32 + 52) + chr(32 + 7)) is None)
    far = b"\x1b[M " + bytes([32 + 140, 32 + 7])          # column past 95: not UTF-8
    check("a legacy report past column 95 keeps all six characters",
          m.split_key(far.decode(errors="surrogateescape") + "q")[1] == "q")
    check("the flash line is there even when empty, so a click cannot scroll the frame",
          frame.split("\n")[-1] == "")


def test_burn_before_weekly_reset(d: str) -> None:
    """On a day off, weekly quota about to reset unused is spent rather than lost.

    Each condition guards something different, so each is checked failing on its own: a working
    day leaves rotation alone; a credential in use here, or whose readings moved within BURN_IDLE,
    belongs to somebody; a refused or 100% credential has nothing left; a reset BURN_WINDOW away or
    further still leaves time for ordinary use. The 5h and 7d LIMITS are ignored on purpose —
    the quota past them is exactly what would otherwise be lost.
    """
    now = 1_800_000_000.0
    H = 3600

    def wk(p5: float, p7: float, reset_in: float) -> dict[str, object]:
        return {**reading(p5, p7), "r7d": str(now + reset_in)}

    def why(r: dict[str, object], **kw: object) -> str:
        args = {"now": now, "still_since": now - m.BURN_IDLE, "on_machine": False, "off": True}
        return m.burn_reason(r, **{**args, **kw})

    hot = wk(90, 95, 1.5 * H)                    # past both limits, weekly resets in 1h30m
    check("burn: 5h 90% / 7d 95% still burns — the limits are not consulted", why(hot) != "")
    check("burn: the reason names the reset and what is left",
          "1h30m" in why(hot) and "5%" in why(hot))
    check("burn: a reset just inside BURN_WINDOW burns", why(wk(10, 50, m.BURN_WINDOW - 60)) != "")
    check("burn: a reset exactly BURN_WINDOW away does not (the window is strict)",
          why(wk(10, 50, m.BURN_WINDOW)) == "")
    check("burn: stillness of exactly BURN_IDLE is enough", why(hot, still_since=now - m.BURN_IDLE) != "")
    # The shipped defaults themselves: a 7h window and 2h of stillness (.env.example documents them).
    check("burn defaults: 7h window, 2h idle", m.BURN_WINDOW == 7 * H and m.BURN_IDLE == 2 * H)
    check("burn defaults: a reset 6h59m away after 2h of stillness burns",
          m.burn_reason(wk(10, 50, 6 * H + 59 * 60), now=now, still_since=now - 2 * H,
                        on_machine=False, off=True) != "")
    check("burn defaults: 1h59m of stillness does not",
          m.burn_reason(wk(10, 50, 6 * H + 59 * 60), now=now, still_since=now - (H + 59 * 60),
                        on_machine=False, off=True) == "")
    check("burn: not on a working day", why(hot, off=False) == "")
    check("burn: not while a session here holds it", why(hot, on_machine=True) == "")
    check("burn: not when its readings moved within BURN_IDLE",
          why(hot, still_since=now - m.BURN_IDLE + 60) == "")
    check("burn: not when it was never observed", why(hot, still_since=None) == "")
    check("burn: not when the reset is further than BURN_WINDOW",
          why(wk(90, 95, m.BURN_WINDOW + 60)) == "")
    check("burn: not when the reset has already passed", why(wk(90, 95, -60)) == "")
    check("burn: not at the very instant of the reset", why(wk(90, 95, 0)) == "")
    check("burn: not with the weekly window at 100%", why(wk(10, 100, H)) == "")
    rej7 = {**wk(20, 90, H), "s7d": "rejected", "ok": False, "code": 429}
    rej5 = {**wk(100, 40, H), "s5h": "rejected", "ok": False, "code": 429}
    check("burn: not when the weekly window is refused", why(rej7) == "")
    check("burn: not when the 5h window is refused", why(rej5) == "")
    check("burn: not when unauthorized", why({**wk(10, 10, H), "err": "unauthorized"}) == "")
    check("burn: not when no weekly reset is known", why(reading(90, 95)) == "")
    check("burn: window and idle can be overridden",
          why(hot, window=H) == "" and why(hot, idle=2 * m.BURN_IDLE) == ""
          and why(wk(10, 50, m.BURN_WINDOW + H), window=m.BURN_WINDOW + 2 * H) != "")

    until = now + 1.5 * H
    check("burn holds: before the weekly reset", m.burn_holds(hot, until, now))
    check("burn holds: lifts at the reset", not m.burn_holds(hot, until, until))
    check("burn holds: lifts after the reset", not m.burn_holds(hot, until, until + 60))
    check("burn holds: lifts when refused",
          not m.burn_holds(rej7, until, now) and not m.burn_holds(rej5, until, now))

    # burn_pick: soon wins; live, held and busy would each have beaten it but are not eligible.
    store = store_with(d, live=tok("L"), late=tok("A"), soon=tok("S"), held=tok("E"), busy=tok("B"))
    res = {tok("L"): wk(90, 95, 0.5 * H), tok("A"): wk(10, 20, 1.8 * H),
           tok("S"): wk(90, 95, 1 * H), tok("E"): wk(50, 50, 0.6 * H),
           tok("B"): wk(10, 20, 0.2 * H)}
    still: dict = {}
    m.track_still(still, res, now - m.BURN_IDLE)          # all unmoved for BURN_IDLE...
    res[tok("B")] = wk(15, 20, 0.2 * H)
    m.track_still(still, res, now - 60)                    # ...except busy, used a minute ago
    asked: list[str] = []

    def held(t: str) -> bool:
        asked.append(t)
        return t == tok("E")

    def pick(live_tok: str = tok("L"), holders=held, off: bool = True):
        return m.burn_pick(store, store.rows, res, now=now, still=still, live_tok=live_tok,
                           holders=holders, off=off)

    got = pick()
    check("burn pick: the soonest eligible weekly reset", got is not None and store.name(got[0]) == "soon")
    check("burn pick: returns its reason and weekly reset",
          got is not None and got[1] != "" and got[2] == now + H)
    check("burn pick: a session is only looked for on an otherwise eligible credential",
          tok("B") not in asked and tok("L") not in asked)
    check("burn pick: the live token is skipped (it would win otherwise)",
          store.name(pick(live_tok="")[0]) == "live")
    check("burn pick: a token a session holds is skipped (it would win otherwise)",
          store.name(pick(holders=lambda t: False)[0]) == "held")
    check("burn pick: nothing on a working day", pick(off=False) is None)

    still = {}
    m.track_still(still, {tok("A"): reading(10, 20)}, 100.0)
    since = (lambda: still[tok("A")][1])
    check("still: first sighting starts the clock now", since() == 100.0)
    m.track_still(still, {tok("A"): reading(10, 20)}, 200.0)
    check("still: unchanged readings keep the clock", since() == 100.0)
    m.track_still(still, {tok("A"): reading(11, 20)}, 300.0)
    check("still: a 5h rise restarts it", since() == 300.0)
    m.track_still(still, {tok("A"): reading(11, 21)}, 400.0)
    check("still: a weekly-only rise restarts it", since() == 400.0)
    m.track_still(still, {tok("A"): reading(0, 21)}, 500.0)
    check("still: a 5h fall with the week unchanged (a 5h reset) is not use", since() == 400.0)
    m.track_still(still, {tok("A"): reading(0, 2)}, 550.0)
    check("still: a weekly fall (a weekly reset) restarts it", since() == 550.0)
    m.track_still(still, {tok("A"): {"err": "unauthorized"}, tok("B"): {"u5h": "0.10"}}, 600.0)
    check("still: a failed probe leaves the entry alone", since() == 550.0)
    check("still: a failed probe does not start a new entry", tok("B") not in still)
    m.track_still(still, {tok("A"): reading(0, 2)}, 700.0)
    check("still: the same readings after a failed probe keep the clock", since() == 550.0)

    path = os.path.join(d, "holidays.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Indonesian national holidays\n"
                 "2026-08-17 Hari Kemerdekaan\n"
                 "2026-12-25   Natal   # Christmas\n"
                 "\n"
                 "   2026-03-20 Idul Fitri\n"
                 "2026-05-01#Hari Buruh\n"
                 "# 2026-01-01 commented out\n"
                 "not-a-date Something\n"
                 "2026-13-01 no such month\n"
                 "2026-02-30\n")
    hol = m.load_holidays(path)
    check("holidays: dates parsed, names, comments, blank and bad lines skipped",
          hol == {"2026-08-17", "2026-12-25", "2026-03-20", "2026-05-01"})
    check("holidays: a missing file means none", m.load_holidays(os.path.join(d, "nope.txt")) == set())

    noon = (lambda y, mo, dd: datetime.datetime(y, mo, dd, 12).timestamp())
    check("day off: a Saturday", m.day_off(noon(2026, 10, 10), set()))
    check("day off: a Sunday", m.day_off(noon(2026, 10, 11), set()))
    check("day off: not a plain Wednesday", not m.day_off(noon(2026, 10, 14), set()))
    check("day off: a Monday listed as a holiday", m.day_off(noon(2026, 8, 17), hol))
    check("day off: that Monday without the list", not m.day_off(noon(2026, 8, 17), set()))

    check("span: hours and minutes", m.span(6000) == "1h40m" and m.span(3600) == "1h00m")
    check("span: minutes alone", m.span(1500) == "25m" and m.span(-5) == "0m")


def test_holiday_file(d: str) -> None:
    """The shipped holidays.txt parses whole, and it holds libur nasional only.

    The list was copied from the SKB 3 Menteri for 2026 and 2027. Cuti bersama is excluded on
    purpose, so a few of those dates are checked to stay out.
    """
    days = m.load_holidays(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), "holidays.txt"))
    check("holidays.txt: 17 dates for 2026", sum(x.startswith("2026") for x in days) == 17)
    check("holidays.txt: 18 dates for 2027", sum(x.startswith("2027") for x in days) == 18)
    check("holidays.txt: Proklamasi and Christmas are in",
          {"2026-08-17", "2026-12-25", "2027-08-17", "2027-12-25"} <= days)
    check("holidays.txt: cuti bersama is not",
          not days & {"2026-02-16", "2026-03-20", "2026-12-24", "2027-03-09", "2027-12-24"})


def test_config(d: str) -> None:
    """Every setting comes from one table: its default, then .env, then CTR_* in the environment.

    A typo must fail loudly instead of silently doing nothing, so an unknown CTR_ key or a value
    that cannot be used is an error. And .env.example must state the real defaults: copying it to
    .env as the README says must change nothing at all.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nowhere = os.path.join(d, "no-such.env")
    default = {k: v for k, _, v in m.CONFIG}

    def at(body: str, name: str = "cfg.env") -> str:
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def load(path: str, env: dict[str, str] | None = None) -> dict[str, object]:
        """load_config, or {"error": message} for the ConfigError it raised."""
        try:
            return m.load_config(path, environ={} if env is None else env)
        except m.ConfigError as exc:
            return {"error": str(exc)}

    # --- read_env_file
    got = m.read_env_file(at("# a comment on its own line\n"
                             "   # an indented one\n"
                             "\n"
                             "PLAIN=value\n"
                             "INLINE=value # trailing comment\n"
                             "GLUED=a#b\n"
                             'DQ="double # kept"\n'
                             "SQ='single # kept'\n"
                             "export EXPORTED=v\n"
                             "  SPACED   =   spaced value  \n"
                             "\n"
                             "EMPTY=\n", "parse.env"))
    check("env file: comments and blank lines are skipped",
          set(got) == {"PLAIN", "INLINE", "GLUED", "DQ", "SQ", "EXPORTED", "SPACED", "EMPTY"})
    check("env file: a plain value", got.get("PLAIN") == "value")
    check("env file: ` # comment` after an unquoted value is dropped", got.get("INLINE") == "value")
    check("env file: a `#` with no space before it is part of the value", got.get("GLUED") == "a#b")
    check("env file: double quotes stripped, the `#` inside kept", got.get("DQ") == "double # kept")
    check("env file: single quotes stripped, the `#` inside kept", got.get("SQ") == "single # kept")
    check("env file: a leading `export ` is allowed", got.get("EXPORTED") == "v")
    check("env file: spaces around `=` and the value are ignored", got.get("SPACED") == "spaced value")
    check("env file: an empty value is empty", got.get("EMPTY") == "")
    check("env file: a missing file is an empty one", m.read_env_file(nowhere) == {})
    try:
        m.read_env_file(at("GOOD=1\nNO_EQUALS_SIGN\n", "bad.env"))
        check("env file: a line without `=` is refused, naming file and line", False)
    except m.ConfigError as exc:
        check("env file: a line without `=` is refused, naming file and line", "bad.env:2" in str(exc))

    # --- load_config: precedence and defaults
    base = load(nowhere)
    check("config: no .env and no CTR_* still loads", "error" not in base)
    check("config: every setting appears, keyed by its argparse dest",
          set(base) == {k[len("CTR_"):].lower() for k in default})
    check("config: every non-path setting has its default",
          all(base.get(k[len("CTR_"):].lower()) == v for k, kind, v in m.CONFIG if kind != "path"))
    check("config: path defaults resolve beside the binary, `~` expanded, empty stays empty",
          base.get("csv") == os.path.join(m.app_dir(), "token.csv")
          and base.get("holidays") == os.path.join(m.app_dir(), "holidays.txt")
          and base.get("env_file") == os.path.expanduser("~/.zshenv")
          and base.get("creds_file") == m.LOCAL_CREDS and base.get("log") == "")

    f = at("CTR_LIMIT_7D=70\nCTR_LIMIT_5H=50\nCTR_INTERVAL=\n")
    cfg = load(f)
    check("config: .env overrides the default", cfg.get("limit_7d") == 70.0 and cfg.get("limit_5h") == 50.0)
    check("config: an empty value in .env means the default", cfg.get("interval") == default["CTR_INTERVAL"])
    cfg = load(f, {"CTR_LIMIT_7D": "80"})
    check("config: CTR_* in the environment overrides .env",
          cfg.get("limit_7d") == 80.0 and cfg.get("limit_5h") == 50.0)
    check("config: an empty value in the environment means the default",
          load(nowhere, {"CTR_TIMEOUT": ""}).get("timeout") == default["CTR_TIMEOUT"])
    cfg = load(nowhere, {"HOME": "/nowhere", "PATH": "/bin", "LIMIT_7D": "1", "ctr_limit_7d": "5",
                         "XCTR_BOGUS": "1"})
    check("config: environment keys without CTR_ are ignored", cfg == base)

    # --- load_config: kinds
    cfg = load(at("CTR_INTERVAL=90\nCTR_TIMEOUT=2.5\nCTR_ALERT=80\n"))
    check("config: an int setting parses to an int",
          cfg.get("interval") == 90 and type(cfg.get("interval")) is int)
    check("config: a float setting parses to a float",
          cfg.get("timeout") == 2.5 and cfg.get("alert") == 80.0 and type(cfg.get("alert")) is float)
    check("config: an optional float left empty is off", base.get("alert") is None)
    spellings = (("true", True), ("TRUE", True), ("1", True), ("yes", True), ("Yes", True),
                 ("on", True), ("ON", True), ("false", False), ("False", False), ("0", False),
                 ("no", False), ("NO", False), ("off", False), ("Off", False))
    check("config: booleans in .env, any case (true/false 1/0 yes/no on/off)",
          all(load(at(f"CTR_COLOR={s}\n")).get("color") is want for s, want in spellings))
    check("config: booleans in the environment too",
          all(load(nowhere, {"CTR_BURN": s}).get("burn") is want for s, want in spellings))
    cfg = load(at("CTR_VIEW=w\nCTR_SORT=r7d\nCTR_ROTATE_MODE=park\n"))
    check("config: a value from a choice list is taken",
          (cfg.get("view"), cfg.get("sort"), cfg.get("rotate_mode")) == ("w", "r7d", "park"))

    # --- load_config: refusals
    err = (lambda body, env=None: load(at(body) if body else nowhere, env).get("error", ""))
    check("config: a bad boolean is refused, naming the key and the file",
          "CTR_COLOR" in err("CTR_COLOR=maybe\n") and "cfg.env" in err("CTR_COLOR=maybe\n"))
    check("config: a bad number is refused",
          err("CTR_LIMIT_7D=seventy\n") != "" and err("CTR_ALERT=high\n") != "")
    check("config: a fraction for an int setting is refused", err("CTR_INTERVAL=1.5\n") != "")
    check("config: a value outside its choices is refused",
          err("CTR_VIEW=x\n") != "" and err("CTR_SORT=bogus\n") != ""
          and err("CTR_ROTATE_MODE=sometimes\n") != "")
    check("config: an unknown CTR_ key in .env is refused",
          "unknown setting CTR_BOGUS" in err("CTR_BOGUS=1\n"))
    check("config: an unknown CTR_ key in the environment is refused, naming where",
          "CTR_LIMIT_7DD" in err("", {"CTR_LIMIT_7DD": "70"})
          and "the environment" in err("", {"CTR_LIMIT_7DD": "70"}))
    check("config: a bad value in the environment is refused, naming where",
          "the environment" in err("", {"CTR_INTERVAL": "abc"}))

    # --- load_config: paths. Run from elsewhere, so "beside the binary" cannot mean the cwd.
    here = os.getcwd()
    os.chdir(d)
    try:
        cfg = load(at(f"CTR_CSV=sub/my.csv\nCTR_ENV_FILE=~/my-rc\nCTR_LOG={d}/log.csv\n"),
                   {"CTR_HOLIDAYS": "days.txt"})
    finally:
        os.chdir(here)
    check("config: a relative path resolves beside the binary, not the cwd",
          cfg.get("csv") == os.path.join(m.app_dir(), "sub/my.csv")
          and cfg.get("holidays") == os.path.join(m.app_dir(), "days.txt"))
    check("config: `~` in a path is expanded", cfg.get("env_file") == os.path.expanduser("~/my-rc")
          and "~" not in str(cfg.get("env_file")))
    check("config: an absolute path is kept", cfg.get("log") == os.path.join(d, "log.csv"))

    # --- .env.example documents exactly the real settings and defaults
    example = os.path.join(repo, ".env.example")
    keys = set(m.read_env_file(example))
    missing, extra = sorted(set(default) - keys), sorted(keys - set(default))
    check(".env.example: names every setting" + (f" (missing {', '.join(missing)})" if missing else ""),
          not missing)
    check(".env.example: names nothing that is not a setting"
          + (f" (unknown {', '.join(extra)})" if extra else ""), not extra)
    check(".env.example: its values are exactly the built-in defaults", load(example) == base)

    # --- the flags take their defaults from the same table
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CTR_")}

    def run_help(**extra: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run([sys.executable, "main.py", "--help"], cwd=repo,
                              env={**clean, **extra}, capture_output=True, text=True, timeout=60)

    def flag_help(text: str, flag: str) -> str:
        """The help paragraph of `flag`, whitespace collapsed so a wrapped line cannot hide it."""
        flat = " ".join(text.split())
        seg = flat[flat.rfind(flag):]                     # the last mention: past the usage line
        end = seg.find(" --", 1)
        return seg if end < 0 else seg[:end]

    h = run_help()
    check("--help: exits 0 and lists --burn-window and CTR_LIMIT_7D",
          h.returncode == 0 and "--burn-window" in h.stdout and "CTR_LIMIT_7D" in h.stdout)
    h = run_help(CTR_LIMIT_7D="70")
    check("--help: CTR_LIMIT_7D=70 in the environment is --limit-7d's default",
          h.returncode == 0 and "default 70.0" in flag_help(h.stdout, "--limit-7d"))
    h = run_help(CTR_BOGUS="1")
    check("--help: a broken config still shows help, and says what is wrong",
          h.returncode == 0 and "unknown setting CTR_BOGUS" in h.stderr and "--burn" in h.stdout)

    qc = os.path.join(d, "quoted-comment.env")
    with open(qc, "w", encoding="utf-8") as fh:
        fh.write('CTR_ONLY="alice # bob"  # who to watch\nCTR_CAP=\'off\' # trailing\n')
    got = m.read_env_file(qc)
    check("read_env_file: a quoted value keeps its # and drops the comment after it",
          got.get("CTR_ONLY") == "alice # bob" and got.get("CTR_CAP") == "off")
    for bad_line in ('CTR_ONLY="alice\n', 'CTR_ONLY="a" b\n'):
        bq = os.path.join(d, "bad-quote.env")
        with open(bq, "w", encoding="utf-8") as fh:
            fh.write(bad_line)
        try:
            m.read_env_file(bq)
            check(f"read_env_file: {bad_line.strip()!r} is rejected", False)
        except m.ConfigError:
            check(f"read_env_file: {bad_line.strip()!r} is rejected", True)


def test_rules_do_not_leak(d: str) -> None:
    """Holes found by review: each one let a rule be skipped or a choice be thrown away.

    A refused window counted as usable at the highest allowed limit; nan in a limit slipped past
    every clamp and switched the weekly limit off; one timed-out probe rotated a pinned credential
    away and dropped the pin; an exported but empty CTR_X masked the .env value.
    """
    now = time.time()
    saved = m.LIMIT_5H, m.LIMIT_7D
    try:
        m.LIMIT_5H = m.LIMIT_7D = 101.0
        spent = {**reading(100, 40), "s5h": "rejected", "ok": False, "code": 429}
        check("a refused window is never usable, even with the limits at 101", not m.usable(spent))
    finally:
        m.LIMIT_5H, m.LIMIT_7D = saved

    blip = {"err": "timed out"}
    check("unpinned: one timed-out probe does not swap the live credential",
          m.needs_rotate(blip, False, now=now)[0] is False)
    check("pinned: one timed-out probe keeps the pin", m.needs_rotate(blip, True, now=now)[0] is False)
    check("a refusal is not a blip: unauthorized still rotates",
          m.needs_rotate({"err": "unauthorized"}, True, now=now)[0] is True)
    half = {**reading(10, 10), "r5h": str(now + 600)}
    del half["u7d"]
    check("pinned with the weekly window unread is not held",
          m.needs_rotate(half, True, now=now)[0] is True)

    for v in ("nan", "inf", "-inf"):
        try:
            m.load_config(os.devnull, {"CTR_LIMIT_7D": v})
            check(f"CTR_LIMIT_7D={v} is refused", False)
        except m.ConfigError:
            check(f"CTR_LIMIT_7D={v} is refused", True)

    envf = os.path.join(d, "fallthrough.env")
    with open(envf, "w", encoding="utf-8-sig") as fh:        # with a BOM, as some editors save
        fh.write("CTR_LIMIT_7D=80\nexport\tCTR_LIMIT_5H=55\n")
    got = m.load_config(envf, {"CTR_LIMIT_7D": ""})
    check("an exported but empty CTR_X falls through to .env", got["limit_7d"] == 80.0)
    check("a BOM and export<TAB> are read", got["limit_5h"] == 55.0)


def test_burn_does_not_leak(d: str) -> None:
    """Holes found by review of the spend-down rule, each reproduced before it was closed.

    A hold chosen on Sunday evening carried on into Monday; it outlived a weekly window that reset
    early; a 7d refusal behind a 5h warning went unseen; equal readings either side of hours
    nobody watched counted as idle.
    """
    now = 1_800_000_000.0
    H = 3600.0
    r = {**reading(90, 95), "r7d": str(now + H)}
    check("hold: not once the day off is over", not m.burn_holds(r, now + H, now, off=False))
    check("hold: still on a day off", m.burn_holds(r, now + H, now, off=True))
    fresh = {**reading(90, 3), "r7d": str(now + 7 * 24 * H)}
    check("hold: ends when the weekly window reset early (a new week)",
          not m.burn_holds(fresh, now + H, now))
    full = {**reading(50, 100), "r7d": str(now + H)}
    check("hold: ends at a full weekly window even if its status says allowed",
          not m.burn_holds(full, now + H, now))
    check("hold: a failed probe keeps it", m.burn_holds({"err": "timed out"}, now + H, now))
    hidden = {**reading(50, 100), "s5h": "allowed_warning", "s7d": "rejected",
              "r7d": str(now + H)}
    check("a 7d refusal behind a 5h warning is still a refusal", m.refused(hidden))
    check("…so it does not hold", not m.burn_holds(hidden, now + H, now))
    check("…and is not chosen", m.burn_reason(hidden, now=now, still_since=now - 3 * H,
                                              on_machine=False, off=True) == "")
    check("…and is not usable", not m.usable(hidden))

    still: dict = {}
    m.track_still(still, {tok("A"): reading(10, 20)}, now - 6 * H, max_gap=200.0)
    m.track_still(still, {tok("A"): reading(10, 20)}, now, max_gap=200.0)
    check("idle clock: a gap nobody watched restarts it", still[tok("A")][1] == now)
    m.track_still(still, {tok("A"): reading(10, 20)}, now + 100, max_gap=200.0)
    check("idle clock: refreshes inside the gap keep it", still[tok("A")][1] == now)


def test_one_writer_and_prompts_time_out(d: str) -> None:
    """Two dashboards fought over the live credential; a prompt left open froze every rule.

    The second dashboard on a machine now only watches, and a prompt with no answer cancels
    itself, so the refreshes — and with them the limits — come back.
    """
    lock = os.path.join(d, "x.lock")
    first = m.take_lock(lock)
    check("lock: the first dashboard gets it", first is not None)
    out = subprocess.run([sys.executable, "-c",
                          "import sys; sys.path.insert(0, sys.argv[1]); import main as m; "
                          "print(m.take_lock(sys.argv[2]) is None)",
                          os.path.dirname(os.path.dirname(os.path.abspath(__file__))), lock],
                         capture_output=True, text=True).stdout.strip()
    check("lock: a second process does not", out == "True")

    check("burn record: one still ahead is read back",
          m.burn_record({"burn": {"id": "x", "until": 2e9}}, 1e9) == ("x", 2e9))
    check("burn record: one already past is dropped",
          m.burn_record({"burn": {"id": "x", "until": 1e9}}, 2e9) is None)
    now, H = 1_800_000_000.0, 3600.0
    wobble = {**reading(90, 95), "r7d": str(now + H + 120)}
    check("hold: two sources disagreeing by minutes do not end it",
          m.burn_holds(wobble, now + H, now))

    import pty
    pid, fd = pty.fork()
    if pid == 0:
        import main as mm
        with mm.Keys() as k:
            t0 = time.time()
            a = mm.ask(k, "row? ", timeout=0.5)
            print("RESULT", repr(a), round(time.time() - t0, 1), flush=True)
        os._exit(0)
    buf = b""
    end = time.time() + 5
    while time.time() < end and b"RESULT" not in buf:
        try:
            buf += os.read(fd, 4096)
        except OSError:
            break
    os.waitpid(pid, 0)
    txt = buf.decode(errors="replace")
    check("ask: no answer within the deadline is a cancel",
          "RESULT ''" in txt and "no answer" in txt)


def test_burn_refinements(d: str) -> None:
    """A spend-down worth two swaps: not minutes before the reset, not into a 5h window about to
    be refused. The live credential and the in-flight hold are covered by the pty runs."""
    now = 1_800_000_000.0
    H = 3600.0
    near = {**reading(50, 80), "r7d": str(now + 10 * 60)}
    check("burn_left: not with 10 minutes left", m.burn_left(near, now) == "")
    ok = {**reading(50, 80), "r7d": str(now + 3 * H)}
    check("burn_left: 3 hours left is worth it", "20% left" in m.burn_left(ok, now))
    hot = {**reading(96, 80), "r7d": str(now + 3 * H)}
    check("burn_left: not into a 5h window at the ceiling", m.burn_left(hot, now) == "")
    check("burn_reason: inherits both", m.burn_reason(hot, now=now, still_since=now - 3 * H,
                                                      on_machine=False, off=True) == "")


def test_eve_counts_as_off(d: str) -> None:
    """Off hours are weekends, holidays, and every night 22:00-07:00: work ends at 17:00, and
    whatever resets at 03:00 on a Thursday is just as lost as on a Sunday."""
    at = (lambda y, mo, dd, h, mi=0: datetime.datetime(y, mo, dd, h, mi).timestamp())
    check("night: Thursday 21:59 is working time", not m.day_off(at(2026, 10, 8, 21, 59), set()))
    check("night: Thursday 22:00 is off", m.day_off(at(2026, 10, 8, 22, 0), set()))
    check("night: Friday 03:00 is still off", m.day_off(at(2026, 10, 9, 3, 0), set()))
    check("night: Friday 07:00 is working time", not m.day_off(at(2026, 10, 9, 7, 0), set()))
    check("night: Friday 12:00 is working time", not m.day_off(at(2026, 10, 9, 12, 0), set()))
    check("weekend: Friday 22:00 to Monday 07:00",
          m.day_off(at(2026, 10, 9, 22, 0), set()) and m.day_off(at(2026, 10, 10, 12, 0), set())
          and m.day_off(at(2026, 10, 12, 6, 59), set())
          and not m.day_off(at(2026, 10, 12, 7, 0), set()))
    xmas = {"2026-12-25"}                                 # a Friday
    check("holiday: off all day", m.day_off(at(2026, 12, 25, 12, 0), xmas))
    check("holiday: the evening before is working time until 22:00",
          not m.day_off(at(2026, 12, 24, 21, 0), xmas) and m.day_off(at(2026, 12, 24, 22, 0), xmas))
    check("night: 24 / 0 switch nights off",
          not m.day_off(at(2026, 10, 8, 23, 0), set(), night_from=24, night_until=0)
          and not m.day_off(at(2026, 10, 9, 3, 0), set(), night_from=24, night_until=0))

def test_unseen_counts_as_idle(d: str) -> None:
    """A credential nobody has watched yet gets the benefit of the doubt, so a spend-down does not
    wait hours after a start; one seen moving while watched still waits."""
    still: dict = {}
    m.track_still(still, {tok("A"): reading(10, 20)}, 1000.0, unseen_idle=7200.0)
    check("unseen: first sighting counts as idle already", still[tok("A")][1] == 1000.0 - 7200.0)
    m.track_still(still, {tok("A"): reading(12, 20)}, 1060.0, unseen_idle=7200.0)
    check("unseen: once seen moving it waits like anyone", still[tok("A")][1] == 1060.0)
    m.track_still(still, {tok("A"): reading(12, 20)}, 99999.0, max_gap=200.0, unseen_idle=7200.0)
    check("unseen: after a gap nobody watched it is unseen again",
          still[tok("A")][1] == 99999.0 - 7200.0)


def test_idle_and_nights_hold(d: str) -> None:
    """Review of the idle clock and the night hours: use across a gap is use, a 5h reset reads
    about 0, a night window that does not cross midnight is not all day, and holidays.txt
    tolerates a BOM and a comma."""
    still: dict = {}
    m.track_still(still, {tok("A"): reading(18, 40)}, 1000.0, max_gap=180.0, unseen_idle=7200.0)
    m.track_still(still, {tok("A"): reading(22, 40)}, 1240.0, max_gap=180.0, unseen_idle=7200.0)
    check("gap: readings that moved across it are use", still[tok("A")][1] == 1240.0)
    m.track_still(still, {tok("A"): reading(22, 40)}, 1500.0, max_gap=180.0, unseen_idle=7200.0)
    check("gap: unchanged readings never get more idle than the benefit of the doubt",
          still[tok("A")][1] == max(1500.0 - 7200.0, 1240.0))
    s2: dict = {}
    m.track_still(s2, {tok("B"): reading(40, 30)}, 0.0)
    m.track_still(s2, {tok("B"): reading(25, 30)}, 60.0)
    check("a 5h drop that lands well above 0 is not a reset", s2[tok("B")][1] == 60.0)

    at = (lambda h, mi=0: datetime.datetime(2026, 10, 8, h, mi).timestamp())   # a Thursday
    check("night 1-5: 03:00 is off, 12:00 is not",
          m.day_off(at(3), set(), night_from=1, night_until=5)
          and not m.day_off(at(12), set(), night_from=1, night_until=5))
    check("night 7-7: no nights at all", not m.day_off(at(3), set(), night_from=7, night_until=7)
          and not m.day_off(at(12), set(), night_from=7, night_until=7))
    check("off_ends: on a Thursday night, off hours end at 07:00",
          datetime.datetime.fromtimestamp(m.off_ends(at(23))).hour == 7)

    hol = os.path.join(d, "bom-holidays.txt")
    with open(hol, "w", encoding="utf-8-sig") as fh:
        fh.write("2026-12-25,Natal\n2026-W52-5\n20261226\n")
    got = m.load_holidays(hol)
    check("holidays: a BOM and a comma are fine, other date forms are not", got == {"2026-12-25"})


def test_fewer_swaps(d: str) -> None:
    """Our own use is not somebody else's; a replacement has room to spare."""
    still: dict = {}
    m.track_still(still, {tok("A"): reading(10, 20)}, 0.0, live=tok("A"))
    m.track_still(still, {tok("A"): reading(30, 25)}, 600.0, live=tok("A"))
    check("own use: rises while it is live here do not restart its clock", still[tok("A")][1] == 0.0)
    m.track_still(still, {tok("A"): reading(31, 25)}, 1200.0, live=tok("B"))
    check("…but a rise once it is not live here does", still[tok("A")][1] == 1200.0)

    store = store_with(d, tight=tok("T"), roomy=tok("R"))
    now = time.time()
    res = {tok("T"): {**reading(10, 64), "r7d": str(now + 3600)},
           tok("R"): {**reading(10, 40), "r7d": str(now + 5 * 86400)}}
    pick = m.rotate_pick(store, store.rows, res, strict=True)
    check("headroom: 64% weekly against 66 loses to one with room, despite the sooner reset",
          pick is not None and store.name(pick[0]) == "roomy")
    only = {tok("T"): res[tok("T")]}
    pick = m.rotate_pick(store, store.rows, only, strict=True)
    check("headroom: a tight one still beats nothing", pick is not None and store.name(pick[0]) == "tight")


def test_disabled_is_never_used(d: str) -> None:
    """A disabled credential is never used: every rule skips it, even a pin does not keep it."""
    store = store_with(d, live=tok("L"), other=tok("O"))
    row = store.rows[0]
    check("disabled: not without the column", not store.disabled(row))
    store.set_disabled(row, True)
    store.save()
    again = m.Store.from_csv(store.path)
    check("disabled: the column is added and survives a reload",
          again.disabled(again.rows[0]) and not again.disabled(again.rows[1]))
    again.set_disabled(again.rows[0], False)
    check("disabled: enabling clears it", not again.disabled(again.rows[0]))

    off = dict(m.DISABLED_R)
    check("disabled: reads as a refusal", m.refused(off) and not m.usable(off))
    check("disabled: live and pinned, it is still moved off",
          m.needs_rotate(off, True)[0] is True and m.needs_rotate(off, False)[0] is True)
    check("disabled: never spent down", m.burn_left(off, time.time()) == "")
    res = {tok("L"): off, tok("O"): {**reading(90, 90), "r7d": str(time.time() + 3600)}}
    check("disabled: never picked, even over a worse one",
          m.rotate_pick(store, store.rows, res) is not None
          and store.name(m.rotate_pick(store, store.rows, res)[0]) == "other")


def main() -> int:
    d = tempfile.mkdtemp(prefix="ctr-tests-")
    try:
        for fn in (test_disable_beats_inheritance, test_parked_injection,
                   test_both_windows_decide, test_file_handling, test_refusals,
                   test_session_detection, test_rotation_rules, test_pick_soonest_reset,
                   test_rows_added_while_running,
                   test_park_mode,
                   test_creds_follow, test_statusline_account, test_pin_lifts_when_spent,
                   test_file_mode, test_header_sort, test_burn_before_weekly_reset, test_holiday_file,
                   test_config, test_rules_do_not_leak, test_burn_does_not_leak, test_one_writer_and_prompts_time_out, test_burn_refinements, test_eve_counts_as_off, test_unseen_counts_as_idle, test_idle_and_nights_hold, test_fewer_swaps, test_disabled_is_never_used):
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
