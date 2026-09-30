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
    """A candidate is only as fresh as its busiest window."""
    store = store_with(d, live=tok("L"), weekly_spent=tok("W"), good=tok("G"))
    check("worst_window takes the higher", m.worst_window(reading(4, 96)) == 96.0)
    res = {tok("L"): reading(80, 10), tok("W"): reading(4, 96), tok("G"): reading(20, 20)}
    pick = m.rotate_pick(store, store.rows, res, exclude=tok("L"), ceiling=75.0)
    check("skips the 4%/96% trap", pick is not None and store.name(pick[0]) == "good")
    check("ranks on the busiest window", pick is not None and pick[1] == 20.0)

    spent = {tok("L"): reading(80, 10), tok("W"): reading(90, 10), tok("G"): reading(10, 88)}
    check("no candidate when every row is past the line",
          m.rotate_pick(store, store.rows, spent, exclude=tok("L"), ceiling=75.0) is None)
    check("a forced pick still answers when nothing is comfortable",
          m.rotate_pick(store, store.rows, spent, exclude=tok("L")) is not None)
    check("a strict pick refuses instead of settling",
          m.rotate_pick(store, store.rows, spent, exclude=tok("L"),
                        ceiling=75.0, strict=True) is None)


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


def test_pick_rules(d: str) -> None:
    """Candidate limits, and the weekend-aware weekly budget.

    Timestamps are fixed rather than taken from the clock: a rule about which weekday it is would
    otherwise pass or fail depending on when the suite runs.
    """
    import datetime as dt

    def span(start: str, end: str) -> float | None:
        fmt = "%Y-%m-%d %H:%M"
        a = dt.datetime.strptime(start, fmt).timestamp()
        b = dt.datetime.strptime(end, fmt).timestamp()
        return m.workdays_left(b, now=a)

    # 2026-09-28 is a Monday.
    check("Mon->Thu is 3 working days", abs(span("2026-09-28 00:00", "2026-10-01 00:00") - 3) < .01)
    check("Fri->Tue drops Sat and Sun",
          abs(span("2026-10-02 00:00", "2026-10-06 00:00") - 2) < .01)
    check("Sat->Sun is not working time at all",
          span("2026-10-03 00:00", "2026-10-04 12:00") <= 0)
    check("a window already past reads zero", m.workdays_left(1.0, now=2.0) == 0.0)
    check("no reset, no answer", m.workdays_left(None) is None)

    now = dt.datetime.strptime("2026-09-28 00:00", "%Y-%m-%d %H:%M").timestamp()

    def row(p5: float, p7: float, reset: str) -> dict[str, object]:
        return {"u5h": f"{p5 / 100:.2f}", "u7d": f"{p7 / 100:.2f}",
                "r7d": str(dt.datetime.strptime(reset, "%Y-%m-%d %H:%M").timestamp()),
                "s5h": "allowed", "s7d": "allowed", "ok": True, "code": 200}

    # Mon -> Fri is 4 working days: more than three, so the tighter budget applies.
    loose, tight = row(10, 70, "2026-10-01 00:00"), row(10, 70, "2026-10-02 12:00")
    check("<=3 working days left: the 80% budget", m.weekly_ceiling(loose) == m.PICK_7D)
    check(">3 working days left: the 60% budget", m.weekly_ceiling(tight) == m.PICK_7D_TIGHT)
    check("70% weekly passes the loose budget", m.eligible(loose))
    check("70% weekly fails the tight budget", not m.eligible(tight))

    check("5h at the limit is out", not m.eligible(row(50, 10, "2026-10-01 00:00")))
    check("5h under the limit is in", m.eligible(row(49, 10, "2026-10-01 00:00")))
    check("a missing window is never eligible", not m.eligible({"u5h": "0.10"}))

    store = store_with(d, live=tok("L"), weekly_heavy=tok("W"), ok=tok("G"))
    res = {tok("L"): row(80, 10, "2026-10-01 00:00"),
           tok("W"): row(4, 96, "2026-10-01 00:00"),     # idle hour, spent week
           tok("G"): row(20, 20, "2026-10-01 00:00")}
    pick = m.rotate_pick(store, store.rows, res, exclude=tok("L"), ceiling=75.0)
    check("the spent week is refused, the sound one chosen",
          pick is not None and store.name(pick[0]) == "ok")
    _ = now


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
                   test_session_detection, test_pick_rules, test_park_mode,
                   test_creds_follow, test_statusline_account, test_pin_lifts_when_spent):
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
