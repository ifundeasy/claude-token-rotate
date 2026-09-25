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
import os
import shutil
import subprocess
import sys
import tempfile

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
    check("a forced pick ignores the ceiling",
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


def main() -> int:
    d = tempfile.mkdtemp(prefix="ctr-tests-")
    try:
        for fn in (test_disable_beats_inheritance, test_parked_injection,
                   test_both_windows_decide, test_file_handling, test_refusals):
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
