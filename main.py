#!/usr/bin/env python3
"""Live quota dashboard and credential manager for a list of CLAUDE_CODE_OAUTH_TOKEN values.

Stdlib only — no third-party packages, no build step. Linux, macOS, Windows.

THREE WINDOWS, NOT TWO. `anthropic-ratelimit-unified-*` response headers are the only quota state
a `claude setup-token` credential can reach, and three separate windows live there. They move
independently, which is the whole reason this tool exists:

    5h-utilization        the five-hour window
    7d-utilization        the weekly window
    overage-utilization   extra credits: an org-wide spend pool, own billing-cycle reset

Neither window is rolling, whatever "5h rolling" suggests. Measured 2026-09-05 across seven
credentials: every 5h reset landed on the same wall-clock instant (08:00 local, ten minutes apart
for one account), and the weekly resets were scattered across seven different days — one per
account, fixed. So a window has a reset INSTANT, not an age, which is what makes a drop
classifiable: utilization that falls while its reset instant stands still did not reset, and the
dashboard says so in as many words.

Measured 2026-09-03 on one credential: 5h at 3%, 7d at 13%, overage at 103%. Reading only the
first two says "plenty left"; the third is what refuses premium models. A credential whose response carries no
`overage-utilization` header was allotted no extra credits — a different state from a pool that
exists and is spent, so the table shows `off` rather than `?` for it.

TWO SOURCES, AND WHY BOTH. The headers report utilization in WHOLE percentage points — measured
2026-09-05, header `0.18` against `GET /api/oauth/usage` reading `18.0` for the same account in the
same second. So anything under 0.5% reads as 0%, and a 0% row means an idle credential, not one
that just reset. `/api/oauth/usage` is the endpoint the IDE extensions read; it bills nothing and
it is the only source for the extra-credit pool in DOLLARS (`monthly_limit` and `used_credits` in
minor units) rather than as a ratio of a cap nobody published. It refuses a `claude setup-token`
credential with a 403 naming `user:profile`, which those tokens are not minted with — so the cap
is auto-detected from this machine's interactive Claude Code login instead, which is sound because
the pool is org-wide. `--cap USD` still overrides, `--cap off` goes back to the bare ratio.

WHY EACH REFRESH COSTS SOMETHING. `POST /v1/messages/count_tokens` returns HTTP 200 with ZERO
rate-limit headers; only an endpoint that bills inference publishes them. So one refresh spends
one `max_tokens=1` call per credential. The footer keeps a running count so the cost is never
invisible, and `+` raises the interval without a restart.

INPUT. `token.csv` beside this script, a CSV with a header row carrying at least these two
columns (see `token.example.csv`):

    Name,CLAUDE_CODE_OAUTH_TOKEN
    work,sk-ant-oat01-...
    backup,sk-ant-oat01-...

Column order does not matter, extra columns are preserved when the file is rewritten, and rows
with an empty token are skipped. `--from-env` reads the variables instead of a file, in which case
the credential list is read-only.

THE LIVE CREDENTIAL. One credential is the one your shell actually exports, and the dashboard both
shows it (cyan in the NAME column) and can change it. `t` writes a chosen token into the shell file,
`z` comments that line out and back, and `T` arms auto-rotate, which swaps in a fresher credential
once the live one passes `--rotate-at` (75% of its 5h window by default).

Only the assignment is ever rewritten. The file is read as lines, exactly one of them is rebuilt,
and the rest are written back untouched — which is why a comment block explaining why the variable
is there survives a rotation. Two backups sit beside the file: `.claude_token_rotate.bak` is the previous
version, rewritten every time, and `.claude_token_rotate.orig` is what the file looked like before this
tool first touched it, written once and never again. Auto-rotate can write several times an hour,
so a single rolling backup would not be enough to get the original back.

WHAT A ROTATION DOES NOT DO. It does not reach processes that are already running. A shell reads
its rc at startup, and the Claude Code supervisor hands its own credential to every background
session it owns, so until that supervisor restarts the swap is invisible to exactly the sessions
that matter. `t` offers to run `claude daemon stop --any` for you; auto-rotate never does, because
stopping the supervisor terminates live sessions and a timer should not make that call.

Tokens are never printed: the table shows a redacted form, and copy actions put the full value on
the clipboard. Every write to the CSV leaves a `.bak` beside it first. A `data.json`
appears beside the script holding SHA-256 prefixes — never tokens — of which credentials /usage
refused, plus the last cap it reported, so a relaunch does not re-ask a settled question against a
rate-limited endpoint. Deleting it costs one extra round of requests, nothing else.

USAGE
    python3 main.py                             # live dashboard
    python3 main.py --interval 300              # cheaper for leaving open
    python3 main.py --view o                    # extra credits, cap auto-detected
    python3 main.py --view o --cap 5            # override the cap by hand
    python3 main.py --only alice,bob            # watch a subset
    python3 main.py --alert 80                  # bell when a window crosses 80%
    python3 main.py --log usage.csv             # append every reading for later analysis
    python3 main.py --once --json               # one machine-readable snapshot
    python3 main.py --auto-rotate                # swap credentials at 75% unattended
    python3 main.py --rotate-at 60 --env-file ~/.bashrc
    python3 main.py --no-env-write               # never touch a shell file
    python3 main.py --from-env ../outline-audit/.env

KEYS
    views    h 5h    w 7d    o overage    b all
    read     r refresh now    s cycle sort    i inspect one credential's raw headers
    copy     1-9 that row's token    c any row by number    p most 5h headroom    x Markdown
    manage   a add credential    d delete    e rename
    shell    t inject into ~/.zshenv    z activate/deactivate    T auto-rotate on/off
    other    +/- interval    q quit
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"
USAGE_API = "https://api.anthropic.com/api/oauth/usage"
PROBE_MODEL = "claude-haiku-4-5"        # cheapest model that still returns the quota headers
API_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"         # /v1/messages refuses an OAuth token without it
UA = "claude-token-rotate/2.0"
TOKEN_COL = "CLAUDE_CODE_OAUTH_TOKEN"
NAME_COL = "Name"
HIST_MAX = 24                           # readings kept per credential for the trend column
REDACT_TAIL = 32                        # trailing characters of a token the table may show
#: Each utilization field with the reset field beside it and the label used in messages. Pairing
#: them is what lets a drop be classified: a window that falls while its reset stands still did
#: not reset.
WINDOWS = {"u5h": ("r5h", "5h"), "u7d": ("r7d", "7d"), "uov": ("rov", "extra")}
#: Where the live credential is exported. `--env-file` points this at another shell file.
ENV_FILE = os.path.expanduser("~/.zshenv")
#: Auto-rotate thresholds. A swap needs a candidate that is meaningfully fresher, not merely
#: fresher by a rounding error, and it needs a floor on how often it may happen — otherwise a
#: table where everything sits near the threshold would rewrite the shell file every refresh.
ROTATE_AT = 75.0                        # --rotate-at: 5h utilization that triggers a swap
ROTATE_GAP = 300.0                      # seconds between automatic swaps
ROTATE_MARGIN = 10.0                    # percentage points the candidate must beat the incumbent by
#: The API's machine-readable reasons, in words someone can actually act on. An untranslated
#: reason is passed through rather than hidden — a new one should look odd, not invisible.
OVERAGE_WORDS = {
    "org_spend_cap_reached": "team spend cap reached",
    "group_zero_credit_limit": "some accounts have no credits",
    "out_of_credits": "credits used up",
    "org_level_disabled_until": "credits switched off",
}

#: Last reading of the shell file — path, token, name, active, auto. Set once per refresh by main
#: so the table and the footer can show it without either of them touching the filesystem.
LIVE: dict[str, object] = {}
#: Diagnose walks one model per tier. Premium tiers are what a spend cap actually refuses.
TIERS = ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5")
#: Extra-credit cap in USD. `overage-utilization` reports a RATIO of the cap and never the amount,
#: so dollars need the cap from somewhere: --cap, or /api/oauth/usage. Set once at startup.
CAP_USD: float | None = None
#: Where CAP_USD came from. A number this tool went and found is worth labelling in the footer.
CAP_SRC = ""
#: Last exact extra-credit reading: {"cap", "used", "src"}. Empty until /usage answers once.
POOL: dict[str, object] = {}
#: The credential that can read /usage for the pool — re-read each refresh, since it costs nothing.
POOL_TOKEN: str | None = None
#: Tokens known to answer /api/oauth/usage, so the 403s are asked for once and not every refresh.
USAGE_OK: dict[str, bool] = {}
#: Earliest wall-clock time the pool may be read again. /usage rate-limits independently of
#: inference and answers 429 when leaned on, so it is polled on a timer, not on every refresh.
POOL_NEXT = 0.0

HEADERS = {
    "u5h": "anthropic-ratelimit-unified-5h-utilization",
    "s5h": "anthropic-ratelimit-unified-5h-status",
    "r5h": "anthropic-ratelimit-unified-5h-reset",
    "u7d": "anthropic-ratelimit-unified-7d-utilization",
    "s7d": "anthropic-ratelimit-unified-7d-status",
    "r7d": "anthropic-ratelimit-unified-7d-reset",
    "uov": "anthropic-ratelimit-unified-overage-utilization",
    "rov": "anthropic-ratelimit-unified-overage-reset",
    "thov": "anthropic-ratelimit-unified-overage-surpassed-threshold",
    "overage": "anthropic-ratelimit-unified-overage-status",
    "overage_reason": "anthropic-ratelimit-unified-overage-disabled-reason",
    "claim": "anthropic-ratelimit-unified-representative-claim",
    "status": "anthropic-ratelimit-unified-status",
    "fallback": "anthropic-ratelimit-unified-fallback-percentage",
}

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
GREEN, YELLOW, RED, CYAN, MAGENTA = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[36m", "\x1b[35m"
ALT_ON, ALT_OFF = "\x1b[?1049h", "\x1b[?1049l"
HIDE, SHOW, HOME = "\x1b[?25l", "\x1b[?25h", "\x1b[H\x1b[2J"
#: OSC 2 sets the window/tab title; 22;2t and 23;2t push and pop it so the shell's own title comes
#: back on exit. Same family as the OSC 52 clipboard escape already used in copy_to_clipboard().
#: A terminal that does not implement the stack ignores both and simply keeps the title we set.
TITLE_SET = "\x1b]2;{}\x07"
TITLE_MAX = 64                          # a tab label truncates long before this anyway
TITLE_PUSH, TITLE_POP = "\x1b[22;2t", "\x1b[23;2t"
SPARKS = "▁▂▃▄▅▆▇█"
SORTS = ("csv", "5h", "7d", "ov", "name")


# --------------------------------------------------------------------------- credential store

class Store:
    """The credential list plus the CSV it came from.

    Rewrites preserve every column the file already had, so a CSV carrying notes or an `owner`
    column survives an add/delete/rename. `readonly` is set for `--from-env`, where there is no
    file to write back to.
    """

    def __init__(self, path: str | None, rows: list[dict[str, str]],
                 fields: list[str], readonly: bool = False) -> None:
        self.path, self.rows, self.fields, self.readonly = path, rows, fields, readonly

    # -- reading ------------------------------------------------------------
    @staticmethod
    def from_csv(path: str) -> "Store":
        if not os.path.exists(path):
            raise SystemExit(f"csv not found: {path}\n"
                             f"create it with a header row: {NAME_COL},{TOKEN_COL}")
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh)
            fields = list(rd.fieldnames or [])
            tok_col = next((c for c in fields if (c or "").lower() == TOKEN_COL.lower()), None)
            name_col = next((c for c in fields if (c or "").lower() == NAME_COL.lower()), None)
            if not tok_col:
                raise SystemExit(f"{path}: no '{TOKEN_COL}' column (found: {fields})")
            rows: list[dict[str, str]] = []
            for i, raw in enumerate(rd, 1):
                tok = (raw.get(tok_col) or "").strip().strip('"').strip("'")
                if not tok:
                    continue
                row = {k: (v or "") for k, v in raw.items() if k is not None}
                row[tok_col] = tok
                if name_col and not (row.get(name_col) or "").strip():
                    row[name_col] = f"row{i}"
                rows.append(row)
        st = Store(path, rows, fields)
        st.tok_col, st.name_col = tok_col, name_col or NAME_COL
        if not name_col:
            st.fields = [NAME_COL] + fields
            for r in st.rows:
                r.setdefault(NAME_COL, "?")
        if not rows:
            raise SystemExit(f"{path}: no rows with a non-empty {TOKEN_COL}")
        return st

    @staticmethod
    def from_env(env_path: str | None) -> "Store":
        found: dict[str, str] = {}
        if env_path and os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip().startswith(TOKEN_COL):
                        found[k.strip()] = v.strip().strip('"').strip("'")
        for k, v in os.environ.items():          # a stale export outranks the file, as it really does
            if k.startswith(TOKEN_COL) and v.strip():
                found[k] = v.strip()
        if not found:
            raise SystemExit(f"no {TOKEN_COL}* found in {env_path or 'the environment'}")

        def order(k: str) -> tuple[int, str]:
            tail = k[len(TOKEN_COL):].lstrip("_")
            return (int(tail), k) if tail.isdigit() else (0, k)

        rows = [{NAME_COL: k, TOKEN_COL: found[k]} for k in sorted(found, key=order)]
        st = Store(None, rows, [NAME_COL, TOKEN_COL], readonly=True)
        st.tok_col, st.name_col = TOKEN_COL, NAME_COL
        return st

    # -- accessors ----------------------------------------------------------
    def name(self, row: dict[str, str]) -> str:
        n = (row.get(self.name_col) or "?").strip()
        if n.startswith(TOKEN_COL):              # an env var name is mostly a shared prefix
            tail = n[len(TOKEN_COL):].lstrip("_")
            return f"slot {tail}" if tail else "slot 1"
        return n

    def token(self, row: dict[str, str]) -> str:
        return (row.get(self.tok_col) or "").strip()

    # -- writing ------------------------------------------------------------
    def save(self) -> str:
        """Rewrite the CSV atomically, keeping a single-generation backup.

        Returns a short message for the status line. Raises RuntimeError when read-only.
        """
        if self.readonly or not self.path:
            raise RuntimeError("credential list came from the environment — nothing to write")
        if os.path.exists(self.path):
            shutil.copy2(self.path, self.path + ".bak")
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".claude_token_rotate.", suffix=".csv")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                wr = csv.DictWriter(fh, fieldnames=self.fields, extrasaction="ignore")
                wr.writeheader()
                for r in self.rows:
                    wr.writerow(r)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)           # the file holds live credentials
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return f"saved {os.path.basename(self.path)} (backup .bak)"

    def add(self, name: str, token: str) -> None:
        row = {f: "" for f in self.fields}
        row[self.name_col], row[self.tok_col] = name, token
        self.rows.append(row)

    def delete(self, idx: int) -> dict[str, str]:
        return self.rows.pop(idx)

    def rename(self, idx: int, name: str) -> None:
        self.rows[idx][self.name_col] = name


# --------------------------------------------------------------------------- probe

def redact(token: str) -> str:
    """The tail of a token — enough to tell two credentials apart, never enough to use one.

    The `sk-ant-oat01-` head is identical on every credential, so it identifies nothing and only
    spends table width; the tail is the part that varies, and REDACT_TAIL says how much of it to
    show. Four was measurably too few — on a seven-credential list two of them both ended `fwAA`.
    A short string never gives up more than a third of itself, so a truncated or malformed entry
    cannot be reconstructed from the screen either.
    """
    return "…" + token[-min(REDACT_TAIL, max(3, len(token) // 3)):]


def net_err(exc: BaseException) -> str:
    """A transport failure named by what went wrong, not by which class was raised.

    `URLError` on every row at once says nothing about what to do; `dns failed` says the machine
    lost its network, which is a different fix from a token that stopped working.
    """
    txt = str(getattr(exc, "reason", None) or exc).strip() or type(exc).__name__
    low = txt.lower()
    if any(k in low for k in ("getaddrinfo", "name or service not known",
                              "temporary failure in name resolution", "nodename nor servname")):
        return "dns failed — no network?"
    if isinstance(exc, TimeoutError) or "timed out" in low or "timeout" in low:
        return "timed out"
    if "certificate" in low or "ssl" in low:
        return f"tls: {txt[:34]}"
    if "connection refused" in low or "unreachable" in low:
        return "network unreachable"
    return txt[:40]


def probe(token: str, timeout: float) -> dict[str, object]:
    """Quota headers for one credential. Never raises — a failure becomes data.

    A rate-limit refusal still carries the headers, so an error response is parsed exactly like a
    success and the row keeps its numbers instead of going blank. `_raw` keeps every `anthropic-*`
    header so the inspect view can show a field this tool does not model yet.
    """
    body = json.dumps({"model": PROBE_MODEL, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "."}]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "authorization": "Bearer " + token,      # a setup-token OAuth token is a Bearer, not x-api-key
        "anthropic-version": API_VERSION,
        "anthropic-beta": OAUTH_BETA,
        "content-type": "application/json",
        "user-agent": UA,
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            hdrs, code = resp.headers, resp.status
            resp.read()
    except urllib.error.HTTPError as exc:
        hdrs, code = exc.headers, exc.code
        try:
            exc.read()
        except Exception:                        # noqa: BLE001 — draining is best effort
            pass
    except Exception as exc:                     # noqa: BLE001 — network/DNS/TLS become a row state
        return {"ok": False, "code": 0, "err": net_err(exc), "ts": time.time(),
                "ms": int((time.time() - t0) * 1000), "_raw": {}}

    out: dict[str, object] = {"ok": 200 <= code < 300, "code": code, "ts": time.time(),
                              "ms": int((time.time() - t0) * 1000)}
    for field, header in HEADERS.items():
        out[field] = hdrs.get(header)
    out["_raw"] = {k.lower(): v for k, v in hdrs.items()
                   if k.lower().startswith("anthropic-") or k.lower() in ("x-should-retry",
                                                                          "retry-after")}
    if out.get("u5h") is None and not out["ok"]:
        out["err"] = {401: "unauthorized", 403: "forbidden",
                      429: "rate limited"}.get(code, f"http {code}")
    return out


def probe_all(store: Store, tokens: list[str], timeout: float) -> dict[str, dict[str, object]]:
    """One probe per credential, concurrently — wall time is one call, not N.

    Keyed by token rather than by index so add, delete and sort cannot mis-align a row with
    somebody else's reading.
    """
    out: dict[str, dict[str, object]] = {}
    lock = threading.Lock()

    def run(tok: str) -> None:
        r = probe(tok, timeout)
        if USAGE_OK.get(tok):            # free, and its numbers are the ones the IDE shows
            r.update({k: v for k, v in usage_probe(tok, timeout).items() if k.startswith("x")})
        with lock:
            out[tok] = r

    threads = [threading.Thread(target=run, args=(t,), daemon=True) for t in tokens]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout + 5)
    return out


# --------------------------------------------------------------------------- exact usage (/usage)

USAGE_SCOPE = "user:profile"            # /api/oauth/usage refuses anything without it
LOCAL_CREDS = os.path.expanduser("~/.claude/.credentials.json")
KEYCHAIN_SVC = "Claude Code-credentials"


def hdr_epoch(val: object) -> float | None:
    """A reset header as a unix epoch, or None when it is absent or unparseable."""
    try:
        return float(str(val))
    except (TypeError, ValueError):
        return None


def iso_epoch(val: object) -> float | None:
    """An ISO-8601 instant from /usage as a unix epoch, so it can share `until()` with a header."""
    try:
        return datetime.datetime.fromisoformat(str(val)).timestamp()
    except (TypeError, ValueError):
        return None


def local_oauth_token() -> str | None:
    """This machine's interactive Claude Code login, when it carries the `user:profile` scope.

    /api/oauth/usage refuses a `claude setup-token` credential — those are minted with
    `user:inference` only, and the refusal is a 403 naming the missing scope. So the amounts have
    to come from a browser login. That is enough for the money column anyway: the extra-credit pool
    is ORG-WIDE, so one token in the org prices the whole table.
    """
    blobs: list[str] = []
    try:
        with open(LOCAL_CREDS, encoding="utf-8") as fh:
            blobs.append(fh.read())
    except OSError:
        pass
    if sys.platform == "darwin":          # macOS keeps the login in the Keychain, not on disk
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SVC, "-w"],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                blobs.append(out.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    for blob in blobs:
        try:
            oauth = (json.loads(blob) or {}).get("claudeAiOauth") or {}
        except (ValueError, AttributeError):
            continue
        tok = oauth.get("accessToken")
        if isinstance(tok, str) and tok and USAGE_SCOPE in (oauth.get("scopes") or []):
            return tok
    return None


def usage_probe(token: str, timeout: float) -> dict[str, object]:
    """Exact quota for one credential from `GET /api/oauth/usage`. Never raises, never bills.

    This is what the IDE extensions read. It beats the rate-limit headers on the one thing they
    cannot express: the extra-credit pool arrives as AMOUNTS in minor units, not as a ratio of an
    unpublished cap. It is a plain GET against no model, so it can be polled every refresh for
    free — unlike the headers, which only exist on a call that bills inference.

    Utilization is a whole percent here exactly as it is in the headers (measured: header 0.18
    against this endpoint's 18.0), so this is not a precision upgrade for 5h/7d — it is a second,
    independent witness for them, and the only source for the dollars.

    Keys are `x`-prefixed so a merge into a header reading cannot overwrite what the headers said.
    """
    req = urllib.request.Request(USAGE_API, headers={
        "authorization": "Bearer " + token,
        "anthropic-beta": OAUTH_BETA,
        "content-type": "application/json",
        "user-agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc, code = json.loads(resp.read() or b"{}"), resp.status
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:                 # noqa: BLE001 — draining is best effort
            pass
        why = ""
        try:
            why = str((json.loads(body or b"{}").get("error") or {}).get("error_code") or "")
        except (ValueError, AttributeError):
            pass
        out: dict[str, object] = {"ok": False, "code": exc.code}
        if exc.code == 429:               # its own limit, unrelated to the inference windows
            out["err"] = "rate limited"
            out["retry"] = hdr_epoch(exc.headers.get("retry-after")) or 0.0
        elif why == "oauth_scope_insufficient":
            out["err"] = f"no {USAGE_SCOPE} scope"
        else:
            out["err"] = f"http {exc.code}"
        return out
    except Exception as exc:              # noqa: BLE001 — network/DNS/TLS become a row state
        return {"ok": False, "code": 0, "err": net_err(exc)}
    if not isinstance(doc, dict):
        return {"ok": False, "code": code, "err": "unexpected payload"}

    out: dict[str, object] = {"ok": True, "code": code}
    for src, u_key, r_key in (("five_hour", "x5h", "xr5h"), ("seven_day", "x7d", "xr7d")):
        win = doc.get(src)
        if not isinstance(win, dict):
            continue
        try:
            out[u_key] = float(win.get("utilization"))     # already a percentage, not a ratio
        except (TypeError, ValueError):
            pass
        out[r_key] = iso_epoch(win.get("resets_at"))
    ex = doc.get("extra_usage")
    if isinstance(ex, dict):
        scale = 10.0 ** int(ex.get("decimal_places") or 2)
        cap, used = ex.get("monthly_limit"), ex.get("used_credits")
        out["xov_cap"] = float(cap) / scale if isinstance(cap, (int, float)) else None
        out["xov_used"] = float(used) / scale if isinstance(used, (int, float)) else None
        out["xov_on"] = bool(ex.get("is_enabled"))
        # `utilization` here is CLAMPED at 100: an overspent pool reports 100 while the amounts
        # say 102.78. Price it from the amounts so the honest number survives.
        if out["xov_cap"] and out["xov_used"] is not None:
            out["xov"] = float(out["xov_used"]) / float(out["xov_cap"]) * 100.0
        else:
            try:
                out["xov"] = float(ex.get("utilization"))
            except (TypeError, ValueError):
                pass
    return out


def app_dir() -> str:
    """The directory the CSV and the state file live beside.

    A frozen build (PyInstaller and friends) unpacks itself into a throwaway temp directory, so
    `__file__` there names a path the user never sees and cannot put a CSV next to. The binary's
    own location is the one stable answer, and that is what `sys.executable` holds once
    `sys.frozen` is set — symlinks included, since it is resolved before we see it. Run as a plain
    script, both spellings agree.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


STATE_PATH = os.path.join(app_dir(), "data.json")
SCOPE_TTL = 7 * 86400                   # how long "this token cannot read /usage" is believed


def _digest(token: str) -> str:
    """A stable id for a token that is not the token. The state file must never hold credentials."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def load_state() -> dict[str, object]:
    """Remembered /usage answers, so a relaunch does not re-ask questions with settled answers.

    Scope is a property of how a token was minted, not a passing condition: a `claude setup-token`
    credential will never grow `user:profile`. Asking all of them again on every start would spend
    the /usage rate limit on a foregone conclusion, and that limit is the one thing standing
    between this tool and the exact numbers.
    """
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, object]) -> None:
    """Best effort — a dashboard that cannot write its cache still works, just more slowly.

    Token digests are not credentials, but the file names which accounts are being watched, so it
    is written owner-only like the CSV beside it.
    """
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.chmod(STATE_PATH, 0o600)
    except OSError:
        pass


def usage_all(tokens: list[str], timeout: float) -> dict[str, dict[str, object]]:
    """/usage for a list of tokens, concurrently, recording which ones may be asked again."""
    out: dict[str, dict[str, object]] = {}
    lock = threading.Lock()

    def run(tok: str) -> None:
        u = usage_probe(tok, timeout)
        with lock:
            out[tok] = u
            USAGE_OK[tok] = bool(u.get("ok"))

    threads = [threading.Thread(target=run, args=(t,), daemon=True) for t in tokens]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout + 5)
    return out


def resolve_cap(explicit: float | None, tokens: list[str], timeout: float) -> None:
    """Find the extra-credit cap instead of asking for it. Sets CAP_USD, CAP_SRC, POOL, POOL_TOKEN.

    Order matters. A credential that can read its own /usage is asked first, because that answer is
    about that credential. Failing that — and with `claude setup-token` credentials it always fails
    — this machine's interactive login is asked, which is the right fallback precisely because the
    pool is org-wide. `--cap` wins when given; `--cap off` leaves the column as a bare ratio.
    """
    global CAP_USD, CAP_SRC, POOL, POOL_TOKEN, POOL_NEXT
    state = load_state()
    scopes = state.setdefault("scopes", {}) if isinstance(state.get("scopes", {}), dict) else {}
    now = time.time()
    cached = state.get("pool") if isinstance(state.get("pool"), dict) else None
    if cached and cached.get("cap") and now - float(cached.get("ts") or 0) < POOL_TTL:
        # Still inside the window this tool would not have re-read anyway. Relaunching a dashboard
        # is not new information about a spend pool, and /usage has a limit worth not spending.
        POOL = {**cached}
        POOL_NEXT = float(cached.get("ts") or 0) + POOL_TTL
        POOL_TOKEN = local_oauth_token()
        for tok in tokens:
            d = scopes.get(_digest(tok))
            if isinstance(d, dict) and d.get("ok") is not None:
                USAGE_OK[tok] = bool(d["ok"])
        if explicit is not None:
            CAP_USD, CAP_SRC = (explicit or None), "--cap"
        else:
            CAP_USD = float(cached["cap"])
            CAP_SRC = str(cached.get("src") or "cache") + " · cached"
        return

    # Ask only the tokens whose answer is not already settled. A remembered "no" is trusted for a
    # week; a remembered "yes" is re-asked, because the pool figures behind it move.
    ask = [t for t in tokens
           if not (isinstance(scopes.get(_digest(t)), dict)
                   and scopes[_digest(t)].get("ok") is False
                   and now - float(scopes[_digest(t)].get("ts") or 0) < SCOPE_TTL)]
    seen = usage_all(ask, timeout) if ask else {}
    for tok in ask:
        u = seen.get(tok) or {}
        if u.get("code") in (0, 429):           # transient: do not record a verdict
            continue
        scopes[_digest(tok)] = {"ok": bool(u.get("ok")), "ts": now}
    for tok in tokens:                          # a remembered "no" is still a "no" this run
        d = scopes.get(_digest(tok))
        if isinstance(d, dict) and d.get("ok") is False:
            USAGE_OK[tok] = False

    found: tuple[float, float | None, str, str] | None = None
    for tok in tokens:
        u = seen.get(tok) or {}
        if u.get("ok") and u.get("xov_cap"):
            found = (float(u["xov_cap"]), u.get("xov_used"), "own /usage", tok)
            break
    why, retry = "", 0.0
    local = local_oauth_token() if found is None else None
    if found is None and not local:
        why = f"no {USAGE_SCOPE} credential here — run `claude` once to log in"
    elif found is None:
        u = usage_probe(local, timeout)
        if u.get("ok") and u.get("xov_cap"):
            found = (float(u["xov_cap"]), u.get("xov_used"), "local login · org pool", local)
        else:
            retry = float(u.get("retry") or 0.0)
            why = f"local login: {u.get('err') or 'no cap in payload'}"
            POOL_TOKEN = local           # worth retrying later; it is the right reader
    if found:
        cap, used, src, tok = found
        POOL_TOKEN = tok
        POOL = {"cap": cap, "used": used, "src": src, "ts": now}
        POOL_NEXT = now + POOL_TTL
    else:
        POOL_NEXT = now + max(retry, POOL_TTL)
        cached = state.get("pool")   # a cap from a previous run beats no cap at all
        if isinstance(cached, dict) and cached.get("cap"):
            POOL = {**cached, "src": str(cached.get("src") or "cache") + " · cached"}
            found = (float(cached["cap"]), cached.get("used"), str(POOL["src"]), "")

    if explicit is not None:
        CAP_USD, CAP_SRC = (explicit or None), "--cap"
    elif found:
        CAP_USD, CAP_SRC = found[0], found[2]
    else:
        CAP_SRC = why
    state["scopes"] = scopes
    if POOL.get("cap"):
        state["pool"] = {k: POOL.get(k) for k in ("cap", "used", "src", "ts")}
    save_state(state)


POOL_TTL = 600                          # /usage answers 429 when polled hard; once per 10min is
                                        # plenty for a figure that moves in cents


def refresh_pool(timeout: float) -> None:
    """Re-read the exact pool, on a timer rather than on every refresh.

    Billing nothing is not the same as being free to hammer: /usage keeps a rate limit of its own
    and starts answering 429. So the reading is refreshed on POOL_TTL, a 429 pushes the next
    attempt out by whatever `retry-after` asks for, and a failure keeps the last good numbers
    rather than blanking a column that was right a minute ago.
    """
    global POOL, POOL_NEXT
    if not POOL_TOKEN or time.time() < POOL_NEXT:
        return
    u = usage_probe(POOL_TOKEN, timeout)
    now = time.time()
    if u.get("ok") and u.get("xov_cap"):
        POOL = {"cap": float(u["xov_cap"]), "used": u.get("xov_used"),
                "src": POOL.get("src") or CAP_SRC, "ts": now}
        POOL_NEXT = now + POOL_TTL
        state = load_state()
        state["pool"] = {k: POOL.get(k) for k in ("cap", "used", "src", "ts")}
        save_state(state)
        return
    POOL_NEXT = now + max(float(u.get("retry") or 0.0), POOL_TTL)


# --------------------------------------------------------------------------- the live credential

#: One `export CLAUDE_CODE_OAUTH_TOKEN=…` line, commented out or not, quoted or not.
EXPORT_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<hash>#[ \t]*)?export[ \t]+"
                       + re.escape(TOKEN_COL) + r"=(?P<val>.*?)[ \t]*$")

#: Characters that would end the double-quoted string or be re-read by the shell. No OAuth token
#: has ever contained one, but a file another program sources is not the place to assume that.
SHELL_UNSAFE = set('"\\$`\n\r\t ')
#: A shell rc past this size is not the file we mean, and rewriting it would be a bad guess.
ENV_MAX_BYTES = 256 * 1024
#: Two backups, because they answer different questions. `.bak` is "undo the last write"; `.orig`
#: is "what did this file look like before this tool ever touched it" — and only the second one
#: survives auto-rotate, which can write several times an hour and would roll `.bak` away.
ENV_BAK, ENV_ORIG = ".claude_token_rotate.bak", ".claude_token_rotate.orig"
#: An explicit `unset`, which is what actually switches the variable OFF.
#:
#: Commenting the export out only removes the ASSIGNMENT — it cannot remove an INHERITANCE. A
#: desktop session freezes the variable into its own environment at login and hands it to every
#: terminal it spawns, so a shell that merely skips the export still starts with the old value
#: already in place. Measured on a GNOME session: 127 processes, gnome-shell and the systemd user
#: manager among them, still carried a token that had been "disabled" in the file hours earlier.
#: `unset` is the only line that clears what the parent already set.
UNSET_RE = re.compile(r"^(?P<indent>[ \t]*)unset[ \t]+" + re.escape(TOKEN_COL)
                      + r"(?P<tail>[ \t]*(?:\#.*)?)$")   # a trailing comment still counts
UNSET_NOTE = "  # claude-token-rotate: clears any value inherited from the desktop session"

#: Any mention of the variable, parseable or not. Appending a second assignment while an
#: unparseable one already exists would silently create two sources of truth.
ENV_MENTION = re.compile(r"\b" + re.escape(TOKEN_COL) + r"\b")


def unquote(raw: str) -> str:
    """The value of a shell assignment, with one layer of matching quotes removed."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def env_state(path: str, store: "Store | None" = None) -> dict[str, object]:
    """What the shell file exports right now, and which credential that turns out to be.

    Returns everything a caller needs in order to decide without reading the file twice: the
    lines, the index of the line that governs, whether it is commented out, the token it carries,
    and — when a store is supplied — the name of the row holding that same value, which is how the
    dashboard can mark the credential that is live.

    WHICH LINE GOVERNS. A shell executes every line, so the LAST assignment is the one that
    survives, and that is the line this edits. When more than one is live the file is ambiguous
    enough that guessing would be worse than refusing, so `ambiguous` is set and every writer
    declines rather than picking one.
    """
    out: dict[str, object] = {"path": path, "exists": False, "lines": [], "idx": -1,
                              "active": False, "token": "", "name": None, "ambiguous": False,
                              "err": ""}
    try:
        if os.path.getsize(path) > ENV_MAX_BYTES:
            out["err"] = f"{os.path.basename(path)} is larger than {ENV_MAX_BYTES // 1024} KB"
            return out
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            body = fh.read()
    except FileNotFoundError:
        return out
    except OSError as exc:
        out["err"] = f"cannot read {os.path.basename(path)}: {exc.strerror or exc}"
        return out
    lines = body.splitlines()
    out["exists"], out["lines"] = True, lines
    out["mentions"] = len(ENV_MENTION.findall(body))

    live: list[int] = []
    dead: list[int] = []
    unsets: list[int] = []
    for i, ln in enumerate(lines):
        m = EXPORT_RE.match(ln)
        if m:
            (dead if m.group("hash") else live).append(i)
        elif UNSET_RE.match(ln):
            unsets.append(i)
    out["unsets"] = unsets
    out["ambiguous"] = len(live) > 1
    idx = live[-1] if live else (dead[-1] if dead else -1)
    if idx < 0:
        return out
    m = EXPORT_RE.match(lines[idx])
    # An export that a later `unset` undoes is not active, whatever the line itself says.
    out["idx"] = idx
    out["active"] = bool(not m.group("hash") and not any(u > idx for u in unsets))
    out["token"] = unquote(m.group("val"))
    if store is not None and out["token"]:
        known = getattr(store, "all_rows", None) or store.rows
        out["name"] = next((store.name(r) for r in known
                            if store.token(r) == out["token"]), None)
    return out


def env_write(path: str, lines: list[str]) -> None:
    """Replace the shell file atomically, preserving its mode and leaving one backup.

    Same shape as Store.save(): copy to `.bak`, write a sibling temp file so os.replace never has
    to cross a filesystem, then swap it in. The one deliberate difference is permissions —
    Store.save() forces 0600 because the CSV holds credentials, but this file belongs to the user
    and may legitimately be 0644, so the original mode is captured and restored rather than
    imposed.
    """
    # Follow the symlink and replace the TARGET. A dotfile is very often a link into a dotfiles
    # repo; os.replace onto the link path would quietly detach it and strand the repo copy.
    real = os.path.realpath(path)
    mode = 0o600
    if os.path.exists(real):
        if not os.path.isfile(real):
            raise RuntimeError(f"{real} is not a regular file")
        mode = os.stat(real).st_mode & 0o7777
        if not os.path.exists(real + ENV_ORIG):
            shutil.copy2(real, real + ENV_ORIG)      # written once, never again
        shutil.copy2(real, real + ENV_BAK)
    d = os.path.dirname(os.path.abspath(real)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".claude_token_rotate.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, mode)                          # set before the rename, never briefly wrong
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def env_guard(st: dict[str, object], path: str) -> None:
    """Raise when the file is in a state no writer should touch."""
    if st["err"]:
        raise RuntimeError(str(st["err"]))
    if st["ambiguous"]:
        raise RuntimeError(f"{os.path.basename(path)} has more than one live {TOKEN_COL} line — "
                           f"fix it by hand; guessing which one wins would be worse")


def env_set(path: str, store: "Store", token: str) -> str:
    """Point the shell file at `token` and make sure the line is active.

    Only the assignment changes. Any comment block explaining why the variable is there survives
    untouched, because the surrounding lines are written back exactly as they were read.
    """
    if not token or set(token) & SHELL_UNSAFE:
        raise RuntimeError("token has characters that cannot be written to a shell file")
    st = env_state(path, store)
    env_guard(st, path)
    lines = list(st["lines"])
    assign = f'export {TOKEN_COL}="{token}"'
    if st["idx"] >= 0:
        i = int(st["idx"])
        indent = EXPORT_RE.match(lines[i]).group("indent") or ""
        lines[i] = indent + assign                      # keeps indentation, drops any comment mark
        # Any `unset` left over from a disable would silently undo the line we just wrote.
        for u in sorted((x for x in st.get("unsets") or [] if x > i), reverse=True):
            del lines[u]
    else:
        if st.get("mentions"):
            raise RuntimeError(f"{os.path.basename(path)} mentions {TOKEN_COL} in a form this "
                               f"tool does not parse — refusing to add a second one")
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"# Claude Code credential — written by claude_token_rotate "
                  f"{time.strftime('%Y-%m-%d %H:%M')}.", assign]
    env_write(path, lines)
    return f"{os.path.basename(path)} → {redact(token)}"


def env_toggle(path: str) -> tuple[str, bool]:
    """Switch the variable off or back on. The value is never discarded.

    Off is TWO edits, not one: the export is commented out so the value survives for later, and an
    explicit `unset` is written after it. The second is what actually does the work — commenting
    the export only stops this file from setting the variable, and a shell started by a desktop
    session already has it set before this file is read. Without the `unset`, "disabled" means
    nothing in exactly the case people hit: a new terminal.
    """
    st = env_state(path)
    env_guard(st, path)
    if st["idx"] < 0:
        raise RuntimeError(f"no {TOKEN_COL} line in {os.path.basename(path)} to toggle")
    lines = list(st["lines"])
    i = int(st["idx"])
    m = EXPORT_RE.match(lines[i])
    indent = m.group("indent") or ""
    rest = lines[i][len(indent) + len(m.group("hash") or ""):]
    was_active = bool(st["active"])
    unsets = sorted(st.get("unsets") or [])

    if was_active:
        lines[i] = indent + "# " + rest.lstrip("# ").lstrip()
        if not any(u > i for u in unsets):
            lines.insert(i + 1, indent + f"unset {TOKEN_COL}" + UNSET_NOTE)
    else:
        lines[i] = indent + rest.lstrip("# ").lstrip()
        for u in sorted((x for x in unsets if x > i), reverse=True):
            del lines[u]
    env_write(path, lines)
    what = "disabled" if was_active else "enabled"
    return f"{TOKEN_COL} {what} in {os.path.basename(path)}", not was_active


def daemon_stop() -> str:
    """Ask the Claude Code supervisor to exit so the next client reads the new credential.

    Editing the shell file changes what a NEW process inherits; it does nothing to one already
    running. The supervisor outlives individual clients and hands its own credential to every
    background session, so until it restarts a rotation is invisible to exactly the sessions that
    matter. Only the interactive key offers this — automation must not cut off live work.
    """
    exe = shutil.which("claude")
    if not exe:
        return "`claude` not on PATH — stop the supervisor yourself"
    try:
        out = subprocess.run([exe, "daemon", "stop", "--any"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"daemon stop failed ({type(exc).__name__})"
    return "supervisor stopped" if out.returncode == 0 else f"daemon stop exited {out.returncode}"


def rotate_pick(store: "Store", rows: list[dict[str, str]], results: dict,
                exclude: str = "") -> "tuple[dict[str, str], float] | None":
    """The credential with the most 5h headroom, skipping one token and anything already refusing.

    This is the `p` key's selector: the lowest 5h utilization among rows that are not blocked. The
    filter matters more here than it does for a clipboard copy — a row whose state starts with
    EXTRA, unauthorized, forbidden or rate cannot serve a request at all, so promoting it would
    trade a busy credential for a dead one.
    """
    cands: list[tuple[float, dict[str, str]]] = []
    for row in rows:
        tok = store.token(row)
        if not tok or tok == exclude:
            continue
        r = results.get(tok, {})
        if state_note(r).startswith(("EXTRA", "unauthorized", "forbidden", "rate")):
            continue
        p = upct(r, "u5h")
        if p is not None:
            cands.append((p, row))
    if not cands:
        return None
    best, row = min(cands, key=lambda t: t[0])
    return row, best


# --------------------------------------------------------------------------- diagnose

def sdk_try(token: str, model: str, timeout: float) -> dict[str, object]:
    """One real completion over the API path, which bills to the API credit / overage pool."""
    body = json.dumps({"model": model, "max_tokens": 16,
                       "messages": [{"role": "user", "content": "say OK"}]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "authorization": "Bearer " + token, "anthropic-version": API_VERSION,
        "anthropic-beta": OAUTH_BETA, "content-type": "application/json", "user-agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return {"ok": True, "code": resp.status}
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode(errors="replace")[:300]
        except Exception:                        # noqa: BLE001
            pass
        msg = ""
        try:
            msg = (json.loads(raw).get("error") or {}).get("message", "")[:100]
        except Exception:                        # noqa: BLE001
            msg = raw[:100].replace("\n", " ")
        return {"ok": False, "code": exc.code, "msg": msg}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "code": 0, "msg": type(exc).__name__}


def cli_try(token: str, model: str, timeout: float) -> dict[str, object]:
    """One real completion over `claude -p`, which bills to the 5h / 7d subscription windows.

    This is the path an interactive `/login` session uses, so it answers "can this account still
    prompt normally" — a question the API path cannot answer.
    """
    if not shutil.which("claude"):
        return {"ok": False, "subtype": "claude-not-installed"}
    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.pop("ANTHROPIC_API_KEY", None)           # an API key would shadow the OAuth token
    t0 = time.time()
    try:
        p = subprocess.run(["claude", "-p", "--model", model, "--allowedTools", "",
                            "--no-session-persistence", "--output-format", "json",
                            "--max-turns", "2"],
                           input="say OK", capture_output=True, text=True, env=env,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "subtype": "timeout", "secs": timeout}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "subtype": type(exc).__name__}
    secs = round(time.time() - t0, 1)
    obj = None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            k = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(k, dict) and "result" in k:
            obj = k                              # take the LAST object carrying a result
    if obj is None:
        return {"ok": False, "subtype": f"no-result(rc={p.returncode})", "secs": secs}
    ok = bool(obj.get("subtype") == "success" and not obj.get("is_error"))
    return {"ok": ok, "subtype": str(obj.get("subtype")), "secs": secs,
            "cost": obj.get("total_cost_usd")}


def diagnose(token: str, timeout: float, with_cli: bool = True) -> dict[str, object]:
    """Which paths still serve this credential, and which window is the one holding it back."""
    out: dict[str, object] = {"quota": probe(token, timeout), "sdk": {}, "cli": {}}
    for m in TIERS:
        out["sdk"][m] = sdk_try(token, m, timeout)          # type: ignore[index]
    if with_cli:
        for m in TIERS:
            out["cli"][m] = cli_try(token, m, max(timeout, 180.0))   # type: ignore[index]
    return out


def when(epoch: object) -> str:
    """Absolute local date plus time remaining — "when can I" needs both to be answered."""
    try:
        e = int(float(str(epoch)))
    except (TypeError, ValueError):
        return "unknown"
    left = e - int(time.time())
    d = datetime.datetime.fromtimestamp(e)
    if left <= 0:
        return f"{d:%Y-%m-%d %H:%M} (due)"
    return f"{d:%Y-%m-%d %H:%M} (in {left // 86400}d {left % 86400 // 3600}h {left % 3600 // 60}m)"


def verdict(d: dict[str, object]) -> list[tuple[str, str]]:
    """Plain conclusions as (severity, text). Severity is 'ok', 'warn' or 'bad'.

    The two paths are reported separately on purpose: a spend cap can close the API path while
    subscription prompting keeps working, and reading only one path gives the wrong answer.
    """
    q = d.get("quota") or {}
    sdk = d.get("sdk") or {}
    cli = d.get("cli") or {}
    lines: list[tuple[str, str]] = []

    cli_prem = [m for m in TIERS if m != TIERS[0] and (cli.get(m) or {}).get("ok")]
    cli_any = [m for m in TIERS if (cli.get(m) or {}).get("ok")]
    if cli_any:
        lines.append(("ok", "subscription prompting WORKS — `/login` and normal prompting are fine"
                            + (f", including {', '.join(m.replace('claude-', '') for m in cli_prem)}"
                               if cli_prem else "")))
    elif cli:
        why = {(cli.get(m) or {}).get("subtype") for m in TIERS} - {None}
        if "claude-not-installed" in why:
            lines.append(("warn", "subscription path untested — the `claude` CLI is not on PATH"))
        else:
            lines.append(("bad", "subscription prompting FAILS too: " + " · ".join(sorted(map(str, why)))))

    api_ok = [m for m in TIERS if (sdk.get(m) or {}).get("ok")]
    api_bad = [m for m in TIERS if m in sdk and not (sdk.get(m) or {}).get("ok")]
    if api_bad and api_ok:
        lines.append(("warn", "API path is PARTIAL — serves "
                      + ", ".join(m.replace("claude-", "") for m in api_ok) + "; refuses "
                      + ", ".join(f"{m.replace('claude-', '')} ({(sdk[m] or {}).get('code')})"
                                  for m in api_bad)))
    elif api_bad:
        lines.append(("bad", "API path refuses every tier: "
                      + ", ".join(f"{m.replace('claude-', '')} ({(sdk[m] or {}).get('code')})"
                                  for m in api_bad)))
    elif api_ok:
        lines.append(("ok", "API path serves every tier tested"))

    po = pct(q.get("uov"))
    if po is not None and po >= ceiling_ov(q):
        reason = str(q.get("overage_reason") or "unknown reason")
        amount = usd(po)
        cap = f"${CAP_USD:,.2f}" if CAP_USD is not None else "the cap"
        spent = f"{amount} of {cap}" if amount else f"{po:.0f}% of the cap"
        lines.append(("bad", f"blocking pool: EXTRA CREDITS spent — {spent} · {reason}"))
        lines.append(("warn", f"the API path recovers when the pool resets: {when(q.get('rov'))}"))
        lines.append(("ok", "or immediately, by raising the org spend cap / adding extra credits — "
                            "it is a billing setting, not a rate limit"))
        lines.append(("warn", "the pool is ORG-WIDE: any credential in the same org spends it, so "
                              "this is not necessarily this account's own usage"))
    else:
        worst = max([(p, w, f) for p, w, f in
                     ((pct(q.get("u5h")), "5h", "r5h"), (pct(q.get("u7d")), "7d", "r7d"))
                     if p is not None] or [(0.0, "", "")])
        if worst[1] and worst[0] >= 80:
            lines.append(("warn", f"closest limit: {worst[1]} window at {worst[0]:.0f}% — "
                                  f"resets {when(q.get(worst[2]))}"))
        else:
            lines.append(("ok", "no window is near its limit"))
    if po is not None and po < ceiling_ov(q):
        lines.append(("ok", f"extra credits at {usd(po) or f'{po:.0f}% of cap'} — "
                            f"resets {when(q.get('rov'))}"))
    return lines


def diagnose_lines(name: str, d: dict[str, object], color: bool) -> list[str]:
    """The diagnose panel: measured rows first, conclusions last."""
    q = d.get("quota") or {}
    sdk = d.get("sdk") or {}
    cli = d.get("cli") or {}
    out = [paint(name, BOLD, color) + paint("   windows: ", DIM, color)
           + f"5h {pct(q.get('u5h')) or 0:.0f}%  7d {pct(q.get('u7d')) or 0:.0f}%  "
           + ("extra credits: none allotted" if no_meter(q)
              else f"extra credits {extra_cell(q)}"), ""]
    out.append(paint("  API path  POST /v1/messages", DIM, color))
    for m in TIERS:
        r = sdk.get(m)
        if not r:
            continue
        ok = bool(r.get("ok"))
        out.append("    " + pad(m.replace("claude-", ""), 14)
                   + paint(f"HTTP {r.get('code')}", GREEN if ok else RED, color)
                   + (paint(f"  {r.get('msg')}", DIM, color) if r.get("msg") else ""))
    if cli:
        out.append("")
        out.append(paint("  subscription path  claude -p", DIM, color))
        for m in TIERS:
            r = cli.get(m)
            if not r:
                continue
            ok = bool(r.get("ok"))
            out.append("    " + pad(m.replace("claude-", ""), 14)
                       + paint(str(r.get("subtype")), GREEN if ok else RED, color)
                       + paint(f"  {r.get('secs')}s" if r.get("secs") else "", DIM, color))
    out.append("")
    for sev, txt in verdict(d):
        mark = {"ok": "✓", "warn": "!", "bad": "✗"}[sev]
        col = {"ok": GREEN, "warn": YELLOW, "bad": RED}[sev]
        out.append("  " + paint(mark, col, color) + " " + txt)
    return out


# --------------------------------------------------------------------------- clipboard

def copy_to_clipboard(text: str) -> str:
    """Copy through whatever the platform provides; report which mechanism was used.

    Falls back to OSC 52, which the terminal itself handles and therefore works over SSH where no
    clipboard binary exists.
    """
    cands: list[list[str]] = []
    if sys.platform == "darwin":
        cands = [["pbcopy"]]
    elif os.name == "nt":
        cands = [["clip"]]
    else:
        if os.environ.get("WAYLAND_DISPLAY"):
            cands.append(["wl-copy"])
        cands += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"],
                  ["wl-copy"]]
    for argv in cands:
        if not shutil.which(argv[0]):
            continue
        try:
            p = subprocess.run(argv, input=text.encode(), timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if p.returncode == 0:
                return argv[0]
        except Exception:                        # noqa: BLE001 — try the next mechanism
            continue
    try:
        import base64
        sys.stdout.write("\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\x07")
        sys.stdout.flush()
        return "OSC 52"
    except Exception:                            # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- formatting

def pct(raw: object) -> float | None:
    """A utilization header ('0.03') as a percentage, or None when absent."""
    try:
        return float(str(raw)) * 100.0
    except (TypeError, ValueError):
        return None


#: Which exact field, if any, supersedes each header field.
EXACT = {"u5h": "x5h", "u7d": "x7d", "uov": "xov"}
XRESET = {"r5h": "xr5h", "r7d": "xr7d"}


def upct(r: dict[str, object], field: str) -> float | None:
    """Utilization for one window as a percentage, preferring the exact figure from /usage.

    For 5h and 7d the two sources agree to the point, because both are published in whole
    percentage points. It matters for the extra-credit pool, where /usage knows the amounts and
    the header carries only a ratio rounded to two decimals.
    """
    x = r.get(EXACT.get(field, ""))
    if isinstance(x, (int, float)):
        return float(x)
    return pct(r.get(field))


def ureset(r: dict[str, object], field: str) -> float | None:
    """A window's reset instant, preferring the one /usage spelled out."""
    x = r.get(XRESET.get(field, ""))
    if isinstance(x, (int, float)):
        return float(x)
    return hdr_epoch(r.get(field))


def money(v: float) -> str:
    """Dollars, with cents while cents still mean something."""
    return f"${v:,.2f}" if v < 1000 else f"${v:,.0f}"


def hue(p: float | None, color: bool, ceiling: float = 100.0) -> str:
    """Severity colour for a utilization percentage, relative to its own ceiling."""
    if not color or p is None:
        return ""
    frac = p / ceiling if ceiling else 0.0
    return GREEN if frac < 0.5 else YELLOW if frac < 0.8 else RED


def bar(p: float | None, width: int, color: bool, ceiling: float = 100.0,
        no_meter: bool = False) -> str:
    """Filled bar for a percentage.

    Three distinct empty states, because they mean different things: a dashed track for a window
    with no meter at all, a dotted track for one that could not be read, and a real bar otherwise.
    """
    if p is None:
        track = "─" * width if no_meter else "·" * width
        return (DIM + track + RESET) if color else track
    filled = max(0, min(width, int(round(p / ceiling * width))))
    body = "█" * filled + "░" * (width - filled)
    c = hue(p, color, ceiling)
    return (c + body + RESET) if c else body


def spark(vals: list[float], color: bool) -> str:
    """Recent readings as a sparkline, scaled to the series' own range.

    A flat series renders flat rather than as noise: when max equals min there is no slope to show.
    """
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        s = SPARKS[0] * len(vals)
    else:
        s = "".join(SPARKS[min(len(SPARKS) - 1, int((v - lo) / (hi - lo) * (len(SPARKS) - 1)))]
                    for v in vals)
    return (DIM + s + RESET) if color else s


def delta(vals: list[float], color: bool) -> str:
    """Change since the previous reading, in percentage points, or blank when there is no pair."""
    if len(vals) < 2:
        return ""
    d = vals[-1] - vals[-2]
    if abs(d) < 0.005:
        return (DIM + "  ·  " + RESET) if color else "  ·  "
    # Whole points for a window the API quantises, cents for the pool where /usage knows better.
    txt = f"{d:+5.0f}" if abs(d) >= 1 else f"{d:+5.2f}"
    if not color:
        return txt
    return (RED if d > 0 else GREEN) + txt + RESET


def until(epoch: object) -> str:
    """Time left until a reset epoch: '3d 2h' / '2h 14m' / '58m' / 'due'."""
    try:
        secs = int(float(str(epoch))) - int(time.time())
    except (TypeError, ValueError):
        return "?"
    if secs <= 0:
        return "due"
    h, m = divmod(secs // 60, 60)
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"


def ago(secs: float) -> str:
    """How long since something happened: 'just now' / '4m' / '2h 10m' / '3d'."""
    secs = max(0.0, secs)
    if secs < 60:
        return "just now"
    h, m = divmod(int(secs) // 60, 60)
    if h >= 24:
        return f"{h // 24}d ago"
    return (f"{h}h {m:02d}m" if h else f"{m}m") + " ago"


def at(epoch: object) -> str:
    """Reset epoch as local wall-clock time, so it can be compared with a clock on the wall."""
    try:
        return time.strftime("%H:%M", time.localtime(int(float(str(epoch)))))
    except (TypeError, ValueError):
        return "--:--"


def title_safe(text: str) -> str:
    """A string that cannot escape the title sequence it is about to be pasted into.

    Credential names come from a CSV the user edits by hand, and OSC has no quoting whatsoever —
    a BEL or ESC in a name would terminate the sequence early and let the rest reach the terminal
    as commands. Non-printables are dropped rather than escaped, because a tab label has no use
    for them either way.
    """
    return "".join(ch for ch in text if ch.isprintable())[:TITLE_MAX]


def title_text(store: "Store", rows: list[dict[str, str]], results: dict) -> str:
    """The terminal title: whose credential the shell exports, and how spent its 5h window is.

    The live credential is the useful number here rather than the worst row, because it is the one
    that decides when a rotation is due — and the tab bar is exactly where you want to see that
    without raising the window.
    """
    tok = str(LIVE.get("token") or "")
    name, r = LIVE.get("name"), results.get(tok, {}) if tok else {}
    p = upct(r, "u5h") if r else None
    if name and p is not None:
        if not LIVE.get("active"):
            return f"claude quota · 5h {p:.0f}% · {name} (off)"
        hot = p >= float(LIVE.get("rotate_at") or ROTATE_AT) or state_note(r).startswith(
            ("EXTRA", "unauthorized", "forbidden", "rate"))
        return f"claude quota · 5h {p:.0f}%{' !' if hot else ''} · {name}"
    worst = max([q for q in (upct(results.get(store.token(x), {}), "u5h") for x in rows)
                 if q is not None] or [0.0])
    who = name or ("unknown token" if tok else "no token")
    return f"claude quota · 5h {worst:.0f}% · {who}"


def usd(p: float | None) -> str | None:
    """A utilization percentage as dollars against the configured cap, or None without one."""
    if p is None or CAP_USD is None:
        return None
    return money(p / 100.0 * CAP_USD)


def extra_cell(r: dict[str, object]) -> str:
    """The extra-credit pool in dollars — measured when /usage answered, priced when it did not.

    A `~` prefix is the difference: without /usage the only input is a ratio rounded to whole
    percent, so `1.03 x $40` reads $41.20 where the pool actually holds $41.08. Marking the
    estimate keeps the two apart instead of dressing one up as the other.
    """
    used = r.get("xov_used")
    if isinstance(used, (int, float)):
        return money(float(used))
    p = upct(r, "uov")
    if p is None:
        return "off" if no_meter(r) else "?"
    priced = usd(p)
    return ("~" + priced) if priced else f"{p:.0f}%"


def no_meter(r: dict[str, object]) -> bool:
    """True when the overage pool was never enabled, so no utilization exists to read."""
    if r.get("xov_on") is False:
        return True
    return (upct(r, "uov") is None and str(r.get("overage")) == "rejected"
            and not r.get("err"))


def ceiling_ov(r: dict[str, object]) -> float:
    """The overage threshold as a percentage; the header reports it as a fraction (1.0 = 100%)."""
    return pct(r.get("thov")) or 100.0


def state_short(r: dict[str, object]) -> str:
    """State in as few characters as the all-windows view can spare.

    The long form is kept for the single-window views, where there is room for the reason.
    """
    if r.get("err"):
        return str(r["err"])
    if not r:
        return "…"
    po = upct(r, "uov")
    if po is not None and po >= ceiling_ov(r):
        return "EXTRA spent"
    for w, f in (("5h", "s5h"), ("7d", "s7d")):
        v = str(r.get(f) or "")
        if v and v != "allowed":
            return f"{w} {v}"
    return "allowed" if (r.get("s5h") or r.get("s7d")) else "?"


def state_note(r: dict[str, object]) -> str:
    """The limit that actually binds this credential, named by which window it is.

    Order matters: the overage window is checked first because a credential can sit at 3% of its
    5h window and 13% of its 7d window while its overage window is past 100% — which is the state
    that refuses premium models. The reason is reported per row because it varies between
    credentials (`org_spend_cap_reached` vs `group_zero_credit_limit`).
    """
    if r.get("err"):
        return str(r["err"])
    if not r:
        return "…"
    po = upct(r, "uov")
    if po is not None and po >= ceiling_ov(r):
        reason = str(r.get("overage_reason") or "")
        return "EXTRA spent" + (f" · {reason}" if reason else "")
    s5, s7 = str(r.get("s5h") or ""), str(r.get("s7d") or "")
    if s5 and s5 != "allowed":
        return f"5h {s5}"
    if s7 and s7 != "allowed":
        return f"7d {s7}"
    if no_meter(r):
        return "allowed · no extra credits"
    if not s5 and not s7:
        return "?"
    return "allowed"


# --------------------------------------------------------------------------- layout primitives

_ESC = re.compile(r"\x1b\[[0-9;]*m")
_TOK = re.compile(r"(\x1b\[[0-9;]*m|\s+|[^\s\x1b]+)")


def wrap_ansi(text: str, width: int, indent: str = "") -> list[str]:
    """Wrap a coloured string at word boundaries, measuring visible width only.

    `textwrap` counts escape sequences as characters and would break lines early and in the
    middle of them. This walks the string instead, and re-opens whatever colour was active at the
    break — a run that crosses a line boundary would otherwise end there and leave the rest of
    the sentence unstyled.
    """
    if width < 8:
        return [text]
    lines: list[str] = []
    cur, w, active = "", 0, ""
    for tok in _TOK.findall(text):
        if tok.startswith("\x1b"):
            active = "" if tok == RESET else active + tok
            cur += tok
            continue
        tw = vlen(tok)
        if not tok.strip():
            if w + tw <= width:
                cur, w = cur + tok, w + tw
            continue                                  # a space at a break is simply dropped
        if w + tw > width and w > 0:
            lines.append(cur + (RESET if active else ""))
            cur, w = indent + active, vlen(indent)
        cur, w = cur + tok, w + tw
    if _ESC.sub("", cur).strip():
        lines.append(cur + (RESET if active else ""))
    return lines or [""]


def vlen(s: str) -> int:
    """Visible width of a string, ignoring ANSI colour sequences.

    Padding on the raw length is the classic way a coloured table loses its alignment: the escape
    bytes count toward `len` but occupy no columns.
    """
    return len(_ESC.sub("", s))


def clip(s: str, w: int) -> str:
    """Truncate to a visible width without cutting an escape in half or losing the colour.

    Stripping the escapes first is the easy way to avoid a half-written sequence, but it turns a
    truncated cell monochrome — which is how the legend lost its highlighting on narrow windows.
    Walking the tokens keeps the styling and closes it properly at the cut.
    """
    if w <= 0:
        return ""
    out, seen, active = "", 0, ""
    for tok in _TOK.findall(s):
        if tok.startswith("\x1b"):
            active = "" if tok == RESET else active + tok
            out += tok
            continue
        for ch in tok:
            if seen >= w - 1:
                return out + "…" + (RESET if active else "")
            out, seen = out + ch, seen + 1
    return out + (RESET if active else "")


def pad(s: str, w: int, align: str = "<") -> str:
    """Pad to a visible width. Over-long text is truncated with an ellipsis, never wrapped."""
    n = vlen(s)
    if n > w:
        return clip(s, w)
    fill = " " * (w - n)
    return fill + s if align == ">" else s + fill if align == "<" else \
        " " * ((w - n) // 2) + s + " " * (w - n - (w - n) // 2)


def paint(s: str, c: str, color: bool) -> str:
    """Wrap in a colour only when colour is on, so padding math stays identical either way."""
    return (c + s + RESET) if (color and c) else s


class Col:
    """One table column: a header, a width, an alignment, and a cell function.

    `drop` orders graceful degradation on narrow terminals — the highest `drop` goes first — so the
    table never wraps and never scrolls sideways.
    """

    __slots__ = ("key", "head", "w", "align", "fn", "drop")

    def __init__(self, key: str, head: str, w: int, align: str, fn, drop: int = 0) -> None:
        self.key, self.head, self.w, self.align, self.fn, self.drop = key, head, w, align, fn, drop


def box(lines: list[str], width: int, color: bool, accent: str = CYAN,
        title: str = "", right: str = "") -> list[str]:
    """A rule-framed panel. The title sits in the top rule, a timestamp on its right.

    Widths are computed on the plain strings before any colour is applied, so the frame stays
    square whatever the title length.
    """
    inner = width - 2
    t = f" {title} " if title else ""
    r = f" {right} " if right else ""
    if len(t) + len(r) > inner - 4:              # keep at least a few rule characters visible
        r = r[:max(0, inner - 4 - len(t)) - 1] + "… " if inner - 4 - len(t) > 2 else ""
    fill = max(0, inner - len(t) - len(r))
    out = [paint("\u256d", accent, color) + paint(t, BOLD, color)
           + paint("\u2500" * fill, accent, color) + paint(r, DIM, color)
           + paint("\u256e", accent, color)]
    for ln in lines:
        out.append(paint("\u2502", accent, color) + " " + pad(ln, max(0, inner - 2)) + " "
                   + paint("\u2502", accent, color))
    out.append(paint("\u2570" + "\u2500" * inner + "\u256f", accent, color))
    return out


# --------------------------------------------------------------------------- table

def build_cols(mode: str, store: Store, rows: list[dict[str, str]], hist: dict, color: bool,
               name_w: int, tok_w: int) -> list[Col]:
    """Columns for one view. A single-window view spends the freed width on the bar and the trend."""
    single = mode != "b"
    # The extra-credit pool moves on a billing cycle, so it needs less bar than a rolling window
    # does; the width goes to STATE, which carries the reason string.
    barw = 10 if mode == "b" else 16 if mode == "o" else 24
    key = {"h": ("u5h", "r5h", "s5h", "5H"), "w": ("u7d", "r7d", "s7d", "7D"),
           "o": ("uov", "rov", None, "EXTRA $" if CAP_USD else "EXTRA")}.get(mode)

    def c_stripe(row, r):
        note = state_note(r)
        if r.get("err") or note.startswith("EXTRA"):
            return paint("▎", RED, color)
        worst = max([p for p in (upct(r, "u5h"), upct(r, "u7d")) if p is not None] or [0])
        po = upct(r, "uov")
        if po is not None:
            worst = max(worst, po / ceiling_ov(r) * 100.0)
        return paint("▎", YELLOW, color) if worst >= 80 else " "

    def c_name(row, r):
        live = LIVE.get("token") and store.token(row) == LIVE.get("token")
        return paint(store.name(row), BOLD + CYAN if live else BOLD, color)

    def c_token(row, r):
        # The tail is the distinguishing part, so a narrow column keeps the end, not the start.
        t = redact(store.token(row))
        return paint(t if len(t) <= tok_w else "…" + t[-(tok_w - 1):], DIM, color)

    def util(field, ceil_fn=lambda r: 100.0, width=6):
        def fn(row, r):
            p = upct(r, field)
            if p is None:
                return paint("off" if (field == "uov" and no_meter(r)) else "?", DIM, color)
            # Whole percent, because that is the granularity the API publishes. `0.4%` shown as
            # `0.0%` is what made an idle credential look like one that had just reset.
            txt = extra_cell(r) if field == "uov" else f"{p:.0f}%"
            return paint(txt, hue(p, color, ceil_fn(r)), color)
        return fn

    def barfn(field, ceil_fn=lambda r: 100.0):
        def fn(row, r):
            return bar(upct(r, field), barw, color, ceil_fn(r),
                       no_meter=(field == "uov" and no_meter(r)))
        return fn

    def resetfn(field, clock=False):
        def fn(row, r):
            when_ = ureset(r, field)
            left = until(when_)
            return paint(f"{left} {at(when_)}" if clock else left, DIM, color)
        return fn

    def trendfn(field):
        def fn(row, r):
            vals = [v for _, v in hist.get(store.token(row), {}).get(field, [])][-8:]
            return spark(vals, color)
        return fn

    def deltafn(field):
        def fn(row, r):
            vals = [v for _, v in hist.get(store.token(row), {}).get(field, [])][-2:]
            return delta(vals, color)
        return fn

    def c_state(row, r):
        note = state_short(r) if mode == "b" else state_note(r)
        c = (RED if (r.get("err") or note.startswith("EXTRA")) else
             GREEN if note.startswith("allowed") else YELLOW)
        return paint(note, c, color)

    def c_idx(row, r):
        return c_stripe(row, r) + " " + paint(str(rows.index(row) + 1), DIM, color)

    cols = [Col("idx", "#", 4, ">", c_idx),
            Col("name", "NAME", name_w, "<", c_name),
            Col("token", "TOKEN", tok_w, "<", c_token, drop=3)]

    if single:
        f_u, f_r, f_s, label = key
        ceil = ceiling_ov if mode == "o" else (lambda r: 100.0)
        cols += [
            Col("u", label, 9 if mode == "o" else 7, ">", util(f_u, ceil)),
            Col("d", "Δpp", 5, ">", deltafn(f_u), drop=6),
            Col("bar", "", barw, "<", barfn(f_u, ceil), drop=4),
            Col("trend", "TREND", 8, "<", trendfn(f_u), drop=5),
            Col("reset", "RESET", 13, ">", resetfn(f_r, clock=True), drop=2),
        ]
    else:
        cols += [
            Col("u5", "5H", 6, ">", util("u5h")),
            Col("d5", "Δpp", 5, ">", deltafn("u5h"), drop=6),
            Col("b5", "", barw, "<", barfn("u5h"), drop=4),
            Col("t5", "TREND", 8, "<", trendfn("u5h"), drop=5),
            Col("r5", "RESET", 7, ">", resetfn("r5h")),
            Col("u7", "7D", 6, ">", util("u7d")),
            Col("b7", "", barw, "<", barfn("u7d"), drop=7),
            Col("r7", "RESET", 7, ">", resetfn("r7d"), drop=2),
            Col("uo", "EXTRA $" if CAP_USD else "EXTRA", 9, ">", util("uov", ceiling_ov), drop=1),
        ]
    cols.append(Col("state", "STATE", 0, "<", c_state))   # width 0 = take what is left
    return cols


def fit(cols: list[Col], cols_avail: int, gap: int = 2, reserve: int = 12) -> list[Col]:
    """Drop optional columns, highest `drop` first, until the table fits the terminal.

    `reserve` is how much room the flexible column still needs. Measuring it from the real STATE
    strings rather than assuming a fixed 12 is what stops a 100-column terminal from throwing
    away bars it had room for.
    """
    keep = list(cols)
    while True:
        fixed = sum(c.w for c in keep if c.w) + gap * (len(keep) - 1)
        if fixed + reserve <= cols_avail or not any(c.drop for c in keep):
            return keep
        victim = max((c for c in keep if c.drop), key=lambda c: c.drop)
        keep.remove(victim)


def table(store: Store, rows: list[dict[str, str]], results: dict, hist: dict, *, mode: str,
          color: bool, cols_avail: int) -> tuple[list[str], int]:
    """Header rule + one line per credential, plus the width it actually needed.

    The flexible column is sized to its CONTENT, not to whatever the terminal happens to offer.
    A window three times wider than the data is not a reason to stretch a table across it — the
    eye then has to travel past empty space to connect a name to its state. It still shrinks when
    the terminal is narrow, which is the direction where fitting genuinely matters.
    """
    names = [store.name(r) for r in rows]
    name_w = max(4, min(20, max((len(n) for n in names), default=4)))
    # How much the STATE column really wants, measured before anything is dropped for it.
    reserve = min(26, max([5] + [len(state_short(results.get(store.token(r), {})) if mode == "b"
                                     else state_note(results.get(store.token(r), {})))
                                 for r in rows]))
    # Shorten the token BEFORE dropping anything. A long tail is a luxury, but the column still
    # tells two credentials apart at nine characters — and losing it entirely to save width the
    # shrink would have found is the wrong trade.
    full = max((len(redact(store.token(r))) for r in rows), default=10)
    probe = build_cols(mode, store, rows, hist, color, name_w, full)
    fixed = sum(c.w for c in probe if c.w) + 2 * (len(probe) - 1)
    tok_w = max(9, min(full, full - (fixed + reserve - cols_avail)))
    cols = fit(build_cols(mode, store, rows, hist, color, name_w, tok_w), cols_avail,
               reserve=reserve)

    # Render every cell once: the flexible column cannot be measured without them, and calling
    # each fn twice would also mean building the same escape sequences twice.
    cells = [[c.fn(row, results.get(store.token(row), {})) for c in cols] for row in rows]
    fixed = sum(c.w for c in cols if c.w) + 2 * (len(cols) - 1)
    for j, c in enumerate(cols):
        if c.w:
            continue
        natural = max([vlen(row[j]) for row in cells] + [len(c.head)])
        c.w = max(6, min(natural, max(10, cols_avail - fixed - 2)))

    used = min(cols_avail, sum(c.w for c in cols) + 2 * (len(cols) - 1) + 1)
    head = "  ".join(pad(paint(c.head, DIM, color), c.w, c.align) for c in cols)
    out = [" " + head, " " + paint("─" * min(used, vlen(head)), DIM, color)]
    for row in cells:
        out.append(" " + "  ".join(pad(row[j], c.w, c.align) for j, c in enumerate(cols)))
    return out, used


def summary(store: Store, rows: list[dict[str, str]], results: dict, color: bool) -> list[str]:
    """Aggregate line: how many credentials, how many blocked, and the headroom that remains.

    'five-hour windows free' sums the unused fraction of every usable credential — the number that
    answers "can I start a run now", which no single row answers on its own.
    """
    usable, blocked, free = 0, 0, 0.0
    best: tuple[float, str] | None = None
    for row in rows:
        r = results.get(store.token(row), {})
        note = state_note(r)
        p5 = upct(r, "u5h")
        if r.get("err") or note.startswith("EXTRA"):
            blocked += 1
            continue
        if p5 is None:
            continue
        usable += 1
        free += max(0.0, (100.0 - p5) / 100.0)
        if best is None or p5 < best[0]:
            best = (p5, store.name(row))
    parts = [paint(f"{len(rows)}", BOLD, color) + " creds"]
    if blocked:
        parts.append(paint(f"{blocked} blocked", RED, color))
    parts.append(paint(f"{free:.1f}", BOLD, color) + paint(f"/{usable}", DIM, color) + " 5h free")
    if best:
        parts.append("best " + paint(best[1], GREEN, color)
                     + paint(f" {best[0]:.0f}%", DIM, color))
    return [paint(" · ", DIM, color).join(parts)]


# --------------------------------------------------------------------------- frame

TITLES = {"b": "CLAUDE QUOTA · 5h + 7d + extra credits",
          "h": "CLAUDE QUOTA · 5h rolling window",
          "w": "CLAUDE QUOTA · 7d rolling window",
          "o": "CLAUDE QUOTA · extra credits (spend cap)"}


def sort_rows(store: Store, rows: list[dict[str, str]], results: dict,
              how: str) -> list[dict[str, str]]:
    """Order rows for display. Unreadable credentials sort last in every numeric order."""
    if how == "csv":
        return list(rows)
    if how == "name":
        return sorted(rows, key=lambda r: store.name(r).lower())
    field = {"5h": "u5h", "7d": "u7d", "ov": "uov"}[how]

    def k(row):
        p = upct(results.get(store.token(row), {}), field)
        return (1, 0.0) if p is None else (0, -p)
    return sorted(rows, key=k)


def human_epoch(val: str) -> tuple[str, bool]:
    """A bare unix epoch rendered as local time plus time remaining.

    Returns (text, converted). Header values are seconds since the epoch; a ten-digit integer in
    a plausible range is treated as one, anything else is passed through untouched.
    """
    v = val.strip()
    if not (v.isdigit() and 9 <= len(v) <= 11):
        return val, False
    n = int(v)
    if not (1_000_000_000 <= n <= 4_000_000_000):
        return val, False
    return f"{datetime.datetime.fromtimestamp(n):%Y-%m-%d %H:%M} (in {until(n)})", True


def inspect_lines(store: Store, row: dict[str, str], r: dict[str, object],
                  color: bool) -> list[str]:
    """Every anthropic-* header for one credential, verbatim.

    This is the view that answers "the dashboard says 13% but the client says 100%": the raw
    headers include fields this tool does not model, and they are shown unedited.
    """
    out = [paint(store.name(row), BOLD, color) + paint(f"  {redact(store.token(row))}", DIM, color)
           + paint(f"  HTTP {r.get('code', '?')} in {r.get('ms', '?')}ms", DIM, color)]
    if any(k in r for k in ("x5h", "x7d", "xov_used")):
        exact = [f"5h {r['x5h']:.0f}%" if isinstance(r.get("x5h"), (int, float)) else "",
                 f"7d {r['x7d']:.0f}%" if isinstance(r.get("x7d"), (int, float)) else "",
                 f"extra {money(float(r['xov_used']))} of {money(float(r['xov_cap']))}"
                 if isinstance(r.get("xov_used"), (int, float))
                 and isinstance(r.get("xov_cap"), (int, float)) else ""]
        out.append(paint("/api/oauth/usage: ", CYAN, color)
                   + " · ".join(x for x in exact if x))
    raw = r.get("_raw") or {}
    if not isinstance(raw, dict) or not raw:
        out.append(paint("no anthropic-* headers on the response"
                         + (f" — {r.get('err')}" if r.get("err") else ""), DIM, color))
        return out
    keep = sorted(raw)
    shorts = {k: k.replace("anthropic-ratelimit-unified-", "") for k in keep}
    w = min(28, max(len(v) for v in shorts.values()))
    for k in keep:
        txt, converted = human_epoch(str(raw[k]))
        cell = paint(txt, BOLD, color) if converted else txt
        out.append("  " + paint(pad(shorts[k], w, "<"), CYAN, color) + "  " + cell)
    return out


def live_note(results: dict, color: bool) -> tuple[str, str, str] | None:
    """Which credential your shell is actually using, said the way you would say it out loud.

    Every idle reason is spelled out rather than left implicit: auto-rotate that silently does
    nothing is indistinguishable from auto-rotate that is broken.
    """
    if not LIVE or not LIVE.get("path"):
        return None
    d = (lambda t: paint(t, DIM, color))
    hi = (lambda t, c=BOLD: paint(t, c, color))
    where = os.path.basename(str(LIVE["path"]))
    at = float(LIVE.get("rotate_at") or ROTATE_AT)
    if not LIVE.get("writable"):
        auto = d("auto-swap is off here (--no-env-write)")
    elif LIVE.get("auto"):
        auto = hi("auto-swap on", GREEN) + d(f" above {at:.0f}%")
    else:
        auto = d("auto-swap off ") + hi("T", CYAN) + d(" turns it on")

    if LIVE.get("err"):
        return "!", RED, d(f"cannot read {where} — {LIVE['err']}")
    if LIVE.get("ambiguous"):
        return "!", RED, d(f"{where} sets the token on more than one line — "
                           f"nothing will be written until you fix it by hand")
    if not LIVE.get("exists") or int(LIVE.get("idx", -1)) < 0:
        return "◆", YELLOW, d(f"{where} has no token yet — ") + hi("t", CYAN) + d(" writes one")
    name, tok = LIVE.get("name"), str(LIVE.get("token") or "")
    if not tok:
        return "◆", YELLOW, d(f"{where} has an empty token — ") + hi("t", CYAN) + d(" fixes it")
    if not name:
        return "◆", YELLOW, d(f"{where} uses a token that is not in this list "
                              f"({redact(tok)}) — auto-swap cannot judge it")
    p5 = upct(results.get(tok, {}), "u5h")
    used = d(" — ") + hi(f"{p5:.0f}%", hue(p5, color)) + d(" of its 5h window used") \
        if p5 is not None else ""
    if not LIVE.get("active"):
        return "◆", DIM, d("your shell has ") + hi(str(name)) + d(" but it is switched off — ") \
            + hi("z", CYAN) + d(" turns it back on")
    return "◆", CYAN, d("your shell is using ") + hi(str(name)) + used + d(" · ") + auto


def render(store: Store, rows: list[dict[str, str]], results: dict, hist: dict, *, mode: str,
           sort: str, interval: int, last: float, probes: int, flash: str, color: bool,
           cols: int, live: bool, alert: float | None, inspect: str | None,
           diag: tuple[str, dict] | None = None, events: list[str] | None = None) -> str:
    """The whole frame as one string, so a redraw cannot tear.

    The table is laid out first because it is what sets the width: the frame follows the data
    rather than the window. On a very wide terminal that leaves the right-hand side empty, which
    is the correct answer — stretching seven rows across 200 columns makes them harder to read,
    not easier.
    """
    avail = max(48, cols - 1)
    tbl, width = table(store, rows, results, hist, mode=mode, color=color, cols_avail=avail)
    width = max(48, min(avail, width))
    stamp = time.strftime("%H:%M:%S", time.localtime(last)) if last else "--:--:--"
    meta = f"{stamp} · {sort} · {interval}s · {probes} probes"
    if alert is not None:
        meta += f" · alert {alert:.0f}%"
    out: list[str] = []
    out += box(summary(store, rows, results, color), width, color,
               title=TITLES[mode], right=meta)
    out.append("")
    out += tbl
    out.append("")

    if inspect:
        row = next((r for r in rows if store.token(r) == inspect), None)
        if row is not None:
            out += box(inspect_lines(store, row, results.get(inspect, {}), color), width, color,
                       accent=MAGENTA, title="RAW HEADERS", right="i clears")
            out.append("")
    if diag:
        tok, d = diag
        row = next((r for r in rows if store.token(r) == tok), None)
        out += box(diagnose_lines(store.name(row) if row else "?", d, color), width, color,
                   accent=YELLOW, title="DIAGNOSE · which paths still work",
                   right="D clears")
        out.append("")

    # Notes carry a glyph and a colour so the eye can sort them without reading: ! needs you,
    # ◆ is about your shell, $ is money, ↺ is something that changed, ? is only an explanation.
    d = (lambda t: paint(t, DIM, color))
    hi = (lambda t, c=BOLD: paint(t, c, color))
    notes: list[tuple[str, str, str]] = []
    for ev in (events or [])[-2:]:
        glyph, _, rest = ev.partition(" ")
        notes.append((glyph, YELLOW if glyph == "⚠" else GREEN, d(rest)))
    ln = live_note(results, color)
    if ln:
        notes.append(ln)

    spent = sorted({OVERAGE_WORDS.get(str(r.get("overage_reason")), str(r.get("overage_reason")))
                    for r in results.values() if r.get("overage_reason")})
    if spent and mode != "o":
        notes.append(("$", YELLOW, d("no extra credits available — ") + d(" and ".join(spent))
                      + d(" · ") + hi("o", CYAN) + d(" shows the pool")))
    if POOL.get("cap") and POOL.get("used") is not None:
        cap_u, used_u = float(POOL["cap"]), float(POOL["used"])
        over = used_u > cap_u
        when_ = float(POOL.get("ts") or 0)
        notes.append(("$", RED if over else GREEN,
                      d("team credits: ") + hi(money(used_u), RED if over else GREEN)
                      + d(" spent of ") + hi(money(cap_u)) + d(" — ")
                      + d("over the cap" if over else f"{used_u / cap_u * 100:.0f}% used")
                      + d(" · checked " + (ago(time.time() - when_) if when_ else "a while ago"))))
    if mode == "o":
        notes.append(("?", DIM, d("this pool is shared by the whole team and resets on its own "
                                  "billing date, not with the 5h or 7d windows")))
        if CAP_USD is None:
            notes.append(("?", DIM, d("only a ratio is published, so the amount is unknown — ")
                          + hi("--cap USD", CYAN) + d(" turns it into money")))
        else:
            notes.append(("?", DIM, hi("~", CYAN) + d(" in front of an amount means it was worked "
                                                      "out from a rounded ratio, not measured")))
    if mode != "o" and any(upct(r, "u5h") == 0 or upct(r, "u7d") == 0
                           for r in results.values()):
        notes.append(("?", DIM, d("a row at ") + hi("0%") + d(" is simply unused — Anthropic "
                                                             "reports whole percents only")))
    notes.append(("·", DIM, d("each refresh costs one tiny call per credential — it is the only way "
                              "to read these numbers")))
    if store.readonly:
        notes.append(("·", DIM, d("credentials came from the environment, so add/delete/rename "
                                  "are off — ") + hi("t z T", CYAN) + d(" still work")))
    for g, c, t in notes:
        for i, ln in enumerate(wrap_ansi(t, max(20, width - 3), indent="")):
            out.append((" " + paint(g, c, color) + " " if i == 0 else "   ") + ln)

    if live:
        out.append("")
        g = (lambda s: paint(s, CYAN, color))
        d = (lambda s: paint(s, DIM, color))
        on = bool(LIVE.get("auto"))
        left = [f"{g('h')}{d(' 5h')}  {g('w')}{d(' 7d')}  {g('o')}{d(' over')}  {g('b')}{d(' all')}",
                f"{g('1-9')}{d('/')}{g('c')}{d(' copy')}  {g('p')}{d(' best')}  "
                f"{g('x')}{d(' markdown')}",
                f"{g('t')}{d(' inject')}  {g('z')}{d(' on/off')}  {g('T')}"
                + paint(" auto-rotate " + ("ON" if on else "off"),
                        GREEN if on else DIM, color)]
        right = [f"{g('r')}{d(' refresh')}  {g('s')}{d(' sort')}  {g('i')}{d(' raw')}  "
                 f"{g('D')}{d(' diagnose')}  {g('+/-')}{d(' interval')}",
                 f"{g('a')}{d(' add')}  {g('d')}{d(' delete')}  {g('e')}{d(' rename')}  "
                 f"{g('q')}{d(' quit')}",
                 ""]
        # The two columns are zipped positionally, so they must be the same length; the old
        # `range(2)` would have silently dropped anything added to one side only.
        n = max(len(left), len(right))
        left += [""] * (n - len(left))
        right += [""] * (n - len(right))
        half = (width - 6) // 2
        out += box([pad(left[i], half) + "  " + right[i] for i in range(n)], width, color)
    if flash:
        out.append(" " + paint("▸ " + flash, MAGENTA, color))
    return "\n".join(out)


def markdown(store: Store, rows: list[dict[str, str]], results: dict) -> str:
    """The current table as Markdown, for pasting into a ticket or a chat. No colour, no tokens."""
    head = "| Name | Token | 5h | 7d | Extra credits | Reset 5h | State |"
    sep = "|---|---|---:|---:|---:|---:|---|"
    lines = [head, sep]
    for row in rows:
        r = results.get(store.token(row), {})
        f = lambda p: "—" if p is None else f"{p:.0f}%"          # noqa: E731 — table-local
        lines.append(f"| {store.name(row)} | `{redact(store.token(row))}` | {f(upct(r, 'u5h'))} "
                     f"| {f(upct(r, 'u7d'))} | {extra_cell(r)} | {until(ureset(r, 'r5h'))} "
                     f"| {state_note(r)} |")
    return "\n".join(lines) + "\n"


def log_readings(path: str, store: Store, rows: list[dict[str, str]], results: dict) -> None:
    """Append one row per credential per refresh, so quota rate can be analysed after the fact."""
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        if new:
            wr.writerow(["ts", "name", "code", "u5h", "u7d", "uov", "s5h", "s7d",
                         "overage_status", "overage_reason", "r5h", "r7d", "ms"])
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for row in rows:
            r = results.get(store.token(row), {})
            wr.writerow([now, store.name(row), r.get("code"), r.get("u5h"), r.get("u7d"),
                         r.get("uov"), r.get("s5h"), r.get("s7d"), r.get("overage"),
                         r.get("overage_reason"), r.get("r5h"), r.get("r7d"), r.get("ms")])


# --------------------------------------------------------------------------- keyboard + prompts

class Keys:
    """Single-keypress reader with no echo, restored on exit and pausable for line input."""

    def __init__(self) -> None:
        self.unix = sys.stdin.isatty() and os.name != "nt"
        self.win = os.name == "nt" and sys.stdin.isatty()
        self._saved = None

    def _raw(self) -> None:
        if self.unix:
            import tty
            tty.setcbreak(sys.stdin.fileno())

    def __enter__(self) -> "Keys":
        if self.unix:
            import termios
            self._saved = termios.tcgetattr(sys.stdin.fileno())
            self._raw()
        return self

    def __exit__(self, *exc: object) -> None:
        self.cooked()

    def cooked(self) -> None:
        """Restore line-editing mode so `input()` and `getpass` behave normally."""
        if self.unix and self._saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)

    def raw(self) -> None:
        self._raw()

    def get(self, wait: float) -> str:
        """One key within `wait` seconds, or '' on timeout."""
        if self.win:
            import msvcrt
            end = time.time() + wait
            while time.time() < end:
                if msvcrt.kbhit():
                    try:
                        return msvcrt.getch().decode(errors="ignore")
                    except Exception:            # noqa: BLE001
                        return ""
                time.sleep(0.05)
            return ""
        if not self.unix:
            time.sleep(wait)
            return ""
        import select
        r, _, _ = select.select([sys.stdin], [], [], wait)
        return sys.stdin.read(1) if r else ""


def ask(keys: Keys, prompt: str, secret: bool = False) -> str:
    """Read a line with the terminal temporarily back in line-editing mode.

    A secret answer goes through `getpass`, so a pasted token never appears on screen or in the
    terminal's scrollback.
    """
    keys.cooked()
    sys.stdout.write(SHOW)
    sys.stdout.flush()
    try:
        if secret:
            import getpass
            return getpass.getpass(prompt).strip()
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return (sys.stdin.readline() or "").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    finally:
        sys.stdout.write(HIDE)
        sys.stdout.flush()
        keys.raw()


def pick_row(keys: Keys, store: Store, rows: list[dict[str, str]], what: str) -> int:
    """Row number from the user, validated against what is on screen. -1 when cancelled."""
    ans = ask(keys, f"\n  {what} — row number (1-{len(rows)}), blank to cancel: ")
    if not ans.isdigit():
        return -1
    i = int(ans) - 1
    return i if 0 <= i < len(rows) else -1


def validate_token(store: Store, token: str) -> str:
    """Empty string when the token is usable, otherwise the reason it is not."""
    if not token:
        return "no token entered"
    if any(ch.isspace() for ch in token):
        return "token contains whitespace — it was probably truncated or wrapped"
    if len(token) < 20:
        return f"token is only {len(token)} characters — that is not a full OAuth token"
    if any(token == store.token(r) for r in store.rows):
        return "that token is already in the list"
    return ""


# --------------------------------------------------------------------------- main

def main() -> int:
    here = app_dir()
    ap = argparse.ArgumentParser(
        description="Live 5h/7d/overage quota dashboard and credential manager.")
    ap.add_argument("--csv", default=os.path.join(here, "token.csv"),
                    help=f"CSV with {NAME_COL},{TOKEN_COL} columns (default: beside this script)")
    ap.add_argument("--from-env", metavar="ENV_FILE", nargs="?", const="",
                    help=f"read {TOKEN_COL}* from the environment instead (read-only list)")
    ap.add_argument("--only", metavar="NAMES", help="comma-separated names to watch")
    ap.add_argument("--interval", type=int, default=60, help="seconds between refreshes (default 60)")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-probe HTTP timeout")
    ap.add_argument("--view", choices=("b", "h", "w", "o"), default="b",
                    help="b=all windows, h=5h, w=7d, o=extra credits")
    ap.add_argument("--sort", choices=SORTS, default="csv",
                    help="initial row order; 'ov' sorts by extra-credit spend")
    ap.add_argument("--alert", type=float, metavar="PCT",
                    help="ring the terminal bell when a window crosses this percentage")
    ap.add_argument("--log", metavar="CSV", help="append every reading to this CSV")
    ap.add_argument("--cap", metavar="USD|auto|off", default="auto",
                    help="extra-credit cap in dollars, so the EXTRA column reads as money. "
                         "Default 'auto' reads it from /api/oauth/usage — which needs one "
                         "interactive Claude Code login on this machine, since a setup-token "
                         "carries no user:profile scope. 'off' leaves the bare ratio")
    ap.add_argument("--diagnose", metavar="NAME",
                    help="test both paths (API and claude -p) for one credential, then exit — "
                         "answers 'can this account still prompt, and when does it recover'")
    ap.add_argument("--no-cli", action="store_true",
                    help="with --diagnose: skip the `claude -p` test (faster, no subscription cost)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--json", action="store_true", help="with --once: JSON instead of a table")
    ap.add_argument("--env-file", metavar="PATH", default=ENV_FILE,
                    help=f"shell file holding the live {TOKEN_COL} "
                         f"(default {ENV_FILE.replace(os.path.expanduser('~'), '~')})")
    ap.add_argument("--rotate-at", type=float, metavar="PCT", default=ROTATE_AT,
                    help=f"5h utilization at which auto-rotate swaps in a fresher credential "
                         f"(default {ROTATE_AT:.0f})")
    ap.add_argument("--auto-rotate", action="store_true",
                    help="start with auto-rotate on (otherwise it resumes its last state)")
    ap.add_argument("--no-env-write", action="store_true",
                    help="never write the shell file — the t/T/z keys become read-only")
    ap.add_argument("--no-title", action="store_true",
                    help="leave the terminal title alone")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    args = ap.parse_args()

    store = Store.from_env(args.from_env or None) if args.from_env is not None \
        else Store.from_csv(args.csv)
    store.all_rows = list(store.rows)        # --only filters the view; naming the live token
    if args.only:                            # must still see every credential in the file
        want = {w.strip().lower() for w in args.only.split(",") if w.strip()}
        store.rows = [r for r in store.rows if store.name(r).lower() in want]
        if not store.rows:
            raise SystemExit(f"--only {args.only}: no matching credential")

    cap_arg = str(args.cap).strip().lower()
    explicit: float | None = None
    if cap_arg in ("off", "none", "0"):
        explicit = 0.0                     # a real 0 means "no cap known", i.e. show the ratio
    elif cap_arg not in ("", "auto"):
        try:
            explicit = float(cap_arg)
        except ValueError:
            raise SystemExit(f"--cap {args.cap}: expected a dollar amount, 'auto' or 'off'")
    if cap_arg not in ("off", "none", "0") and sys.stderr.isatty():
        print("reading /api/oauth/usage for the extra-credit cap …", file=sys.stderr, flush=True)
    resolve_cap(explicit, [store.token(r) for r in store.rows], args.timeout)
    color = not args.no_color and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    interval, mode, sort = max(5, args.interval), args.view, args.sort
    hist: dict[str, dict[str, list[tuple[float, float]]]] = {}
    alerted: set[str] = set()
    #: Last (reset instant, utilization) per credential per window — the pair a drop is judged by.
    marks: dict[str, dict[str, tuple[float | None, float]]] = {}
    events: list[str] = []
    new_events: list[str] = []
    rotate_at = max(1.0, min(100.0, args.rotate_at))
    env_path = os.path.expanduser(args.env_file)
    env_ok = not args.no_env_write
    auto_rotate = bool(args.auto_rotate) or bool(load_state().get("auto_rotate"))
    last_rotate, no_cand_said = 0.0, False

    def read_live() -> None:
        """Refresh the cached view of the shell file. Cheap, and never fatal."""
        try:
            st = env_state(env_path, store)
        except Exception:                        # noqa: BLE001 — a dashboard must keep drawing
            st = {"path": env_path, "err": "unreadable"}
        LIVE.clear()
        LIVE.update(st)
        LIVE["auto"] = auto_rotate
        LIVE["writable"] = env_ok
        LIVE["rotate_at"] = rotate_at

    def record(results: dict) -> None:
        """History for the trend columns, and the reason behind any drop in a window.

        A window falling is either a reset or something worth shouting about, and the two are told
        apart by whether the reset instant moved with it. Naming that difference is the whole
        point: a credential reading 0% with its old reset still ahead is idle, not freshly
        cleared — the reading that looks most like a bug and is not one.
        """
        fresh: list[str] = []
        for tok, r in results.items():
            h = hist.setdefault(tok, {})
            was = marks.setdefault(tok, {})
            for f, (rf, label) in WINDOWS.items():
                p = upct(r, f)
                if p is None:
                    continue
                series = h.setdefault(f, [])
                series.append((r.get("ts") or time.time(), p))
                del series[:-HIST_MAX]
                ep = ureset(r, rf)
                prev, was[f] = was.get(f), (ep, p)
                if prev is None or p >= prev[1] - 0.5:
                    continue
                old_ep, old_p = prev
                name = next((store.name(x) for x in store.rows if store.token(x) == tok), "?")
                moved = ep is not None and old_ep is not None and ep > old_ep + 1
                fresh.append(
                    f"↺ {name} {label} reset: {old_p:.0f}% → {p:.0f}%, next reset {until(ep)}"
                    if moved else
                    f"⚠ {name} {label} fell {old_p - p:.0f}pp with the SAME reset "
                    f"({at(ep)}) — that is not a reset")
        new_events[:] = fresh
        events.extend(fresh)
        del events[:-8]

    def refresh() -> dict:
        results = probe_all(store, [store.token(r) for r in store.rows], args.timeout)
        refresh_pool(args.timeout)         # free, and it keeps the dollar figures exact
        record(results)
        read_live()                        # someone may have edited the shell file behind us
        if args.log:
            log_readings(args.log, store, store.rows, results)
        return results

    def auto_swap(results: dict, rows: list[dict[str, str]]) -> str:
        """Swap in a fresher credential when the live one is spent. Returns a status line or "".

        Deliberately does NOT stop the Claude Code supervisor. A running supervisor keeps the
        credential it started with, so the swap only reaches processes started afterwards — but
        killing it unprompted would cut off whatever session the user is in the middle of, which
        is precisely the moment quota is tight. The manual key offers that; automation does not
        get to make that call.
        """
        nonlocal last_rotate, no_cand_said, auto_rotate
        if not (auto_rotate and env_ok) or LIVE.get("err") or LIVE.get("ambiguous"):
            return ""
        tok = str(LIVE.get("token") or "")
        if not tok or not LIVE.get("active") or not LIVE.get("name"):
            return ""                      # nothing exported, switched off, or a token we cannot judge
        now = upct(results.get(tok, {}), "u5h")
        if now is None or now < rotate_at:
            no_cand_said = False           # back under the line: allow the next warning to speak
            return ""
        if time.time() - last_rotate < ROTATE_GAP:
            return ""
        pick = rotate_pick(store, rows, results, exclude=tok)
        # The replacement has to be under the line as well, or it trips the same trigger on the
        # next cycle and the dashboard spends its life rewriting $HOME.
        if pick is None or pick[1] >= rotate_at or pick[1] > now - ROTATE_MARGIN:
            if not no_cand_said:
                no_cand_said = True
                return (f"auto-rotate: {LIVE.get('name')} at {now:.0f}% but nothing is "
                        f"{ROTATE_MARGIN:.0f}pp fresher — staying put")
            return ""
        row, p = pick
        old = LIVE.get("name")
        try:
            env_set(env_path, store, store.token(row))
        except (RuntimeError, OSError) as exc:
            auto_rotate = False        # a failure that repeats every interval is noise, not news
            read_live()
            return f"auto-rotate OFF — write failed: {exc}"
        last_rotate, no_cand_said = time.time(), False
        read_live()
        return (f"↻ auto-rotate: {old} {now:.0f}% → {store.name(row)} {p:.0f}% · "
                f"restart Claude Code clients to pick it up")

    read_live()

    if args.diagnose:
        want = args.diagnose.strip().lower()
        row = next((r for r in store.rows if store.name(r).lower() == want), None)
        if row is None:
            raise SystemExit(f"--diagnose {args.diagnose}: no such credential "
                             f"(have: {', '.join(store.name(r) for r in store.rows)})")
        n_cli = 0 if args.no_cli else len(TIERS)
        print(f"diagnosing '{store.name(row)}' — {len(TIERS) + 1} API call(s)"
              + (f" and {n_cli} `claude -p` call(s)" if n_cli else "") + ", please wait…\n")
        d = diagnose(store.token(row), args.timeout, with_cli=not args.no_cli)
        for ln in diagnose_lines(store.name(row), d, color):
            print(" " + ln)
        return 0 if any((d["cli"].get(m) or {}).get("ok") for m in TIERS) or \
            any((d["sdk"].get(m) or {}).get("ok") for m in TIERS) else 1

    if args.once:
        results = refresh()
        rows = sort_rows(store, store.rows, results, sort)
        if args.json:
            print(json.dumps([{"name": store.name(r), "token": redact(store.token(r)),
                               **{k: v for k, v in results.get(store.token(r), {}).items()
                                  if k != "_raw"}} for r in rows], indent=2))
        else:
            print(render(store, rows, results, hist, mode=mode, sort=sort, interval=interval,
                         last=time.time(), probes=len(rows), flash="", color=color,
                         cols=shutil.get_terminal_size((110, 24)).columns, live=False,
                         alert=args.alert, inspect=None, events=events))
        return 0 if all(r.get("ok") for r in results.values()) else 1

    flash, probes, inspect = "", 0, None
    diag: tuple[str, dict] | None = None
    results: dict = {}
    last, due = 0.0, 0.0
    live_tty = sys.stdout.isatty()
    title_on, last_title = live_tty and not args.no_title, ""
    if live_tty:
        sys.stdout.write(ALT_ON + HIDE + (TITLE_PUSH if title_on else ""))
    try:
        with Keys() as keys:
            while True:
                swept = False
                if time.time() >= due:
                    results = refresh()
                    swept = True
                    probes += len(store.rows)
                    last, due = time.time(), time.time() + interval
                    if new_events:
                        flash = new_events[-1]
                    if args.alert is not None:
                        for row in store.rows:
                            tok = store.token(row)
                            r = results.get(tok, {})
                            hot = max([p for p in (upct(r, "u5h"), upct(r, "u7d"),
                                                   upct(r, "uov")) if p is not None] or [0])
                            if hot >= args.alert and tok not in alerted:
                                alerted.add(tok)
                                flash = f"{store.name(row)} crossed {args.alert:.0f}% ({hot:.0f}%)"
                                sys.stdout.write("\a")
                            elif hot < args.alert:
                                alerted.discard(tok)

                rows = sort_rows(store, store.rows, results, sort)
                if swept:
                    swap = auto_swap(results, rows)
                    if swap:
                        flash = swap
                        events.append(swap)
                        del events[:-8]
                if title_on:
                    t = title_safe(title_text(store, rows, results))
                    if t != last_title:            # only on change: some terminals redraw the tab
                        sys.stdout.write(TITLE_SET.format(t))
                        last_title = t
                frame = render(store, rows, results, hist, mode=mode, sort=sort,
                               interval=interval, last=last, probes=probes, flash=flash,
                               color=color, cols=shutil.get_terminal_size((110, 24)).columns,
                               live=True, alert=args.alert, inspect=inspect, diag=diag,
                               events=events)
                sys.stdout.write((HOME if live_tty else "\n") + frame + "\n")
                sys.stdout.flush()

                key = keys.get(min(1.0, max(0.2, due - time.time())) if due > time.time() else 0.2)
                if not key:
                    continue
                flash = ""
                if key in ("q", "Q", "\x03", "\x04"):
                    break
                if key in ("h", "w", "b", "o"):
                    mode = key
                elif key == "r":
                    due = 0.0
                elif key == "s":
                    sort = SORTS[(SORTS.index(sort) + 1) % len(SORTS)]
                    flash = f"sorted by {sort}"
                elif key in ("+", "="):
                    interval = min(3600, interval * 2)
                    due = last + interval
                    flash = f"interval {interval}s"
                elif key in ("-", "_"):
                    interval = max(5, interval // 2)
                    due = last + interval
                    flash = f"interval {interval}s"
                elif key == "i":
                    if inspect:
                        inspect = None
                    else:
                        i = pick_row(keys, store, rows, "inspect")
                        inspect = store.token(rows[i]) if i >= 0 else None
                elif key == "D":
                    if diag:
                        diag = None
                        continue
                    i = pick_row(keys, store, rows, "diagnose")
                    if i < 0:
                        flash = "diagnose cancelled"
                        continue
                    row = rows[i]
                    ok = ask(keys, f"  diagnose '{store.name(row)}' — spends {len(TIERS) + 1} API "
                                   f"and {len(TIERS)} `claude -p` calls, takes ~30s. [y/N]: ")
                    if not ok.lower().startswith("y"):
                        flash = "diagnose cancelled"
                        continue
                    sys.stdout.write(HOME + "\n  diagnosing " + store.name(row) + " …\n")
                    sys.stdout.flush()
                    diag = (store.token(row), diagnose(store.token(row), args.timeout))
                    flash = f"diagnosed '{store.name(row)}'"
                    due = 0.0
                elif key == "x":
                    via = copy_to_clipboard(markdown(store, rows, results))
                    flash = f"table copied as Markdown via {via}" if via else "no clipboard found"
                elif key == "p":
                    cands = [(upct(results.get(store.token(r), {}), "u5h"), r) for r in rows
                             if not state_note(results.get(store.token(r), {})).startswith(
                                 ("EXTRA", "unauthorized", "forbidden", "rate"))]
                    cands = [(p, r) for p, r in cands if p is not None]
                    if cands:
                        p, row = min(cands, key=lambda t: t[0])
                        via = copy_to_clipboard(store.token(row))
                        flash = (f"copied '{store.name(row)}' — most 5h headroom at {p:.0f}% "
                                 f"via {via}") if via else "no clipboard found"
                    else:
                        flash = "no usable credential to pick"
                elif key == "c" or (key.isdigit() and key != "0"):
                    # A single keypress only reaches row 9. Past that the row number has to be
                    # typed, so `c` asks for it — the list is not capped at nine credentials.
                    if key == "c":
                        i = pick_row(keys, store, rows, "copy")
                        if i < 0:
                            flash = "copy cancelled"
                            continue
                    else:
                        i = int(key) - 1
                        if i >= len(rows):
                            flash = f"no row {key} — press c to type a row number"
                            continue
                    via = copy_to_clipboard(store.token(rows[i]))
                    flash = (f"copied token for '{store.name(rows[i])}' via {via} "
                             f"({redact(store.token(rows[i]))})") if via else \
                            "no clipboard mechanism available"
                elif key == "t":
                    if not env_ok:
                        flash = "--no-env-write: the shell file is read-only"
                        continue
                    ans = ask(keys, f"\n  inject — row number (1-{len(rows)}), blank for the "
                                    f"most 5h headroom, 'c' to cancel: ")
                    if ans.lower().startswith("c"):
                        flash = "inject cancelled"
                        continue
                    if ans.isdigit():
                        i = int(ans) - 1
                        if not 0 <= i < len(rows):
                            flash = f"no row {ans}"
                            continue
                        row = rows[i]
                        p5 = upct(results.get(store.token(row), {}), "u5h")
                    else:
                        pick = rotate_pick(store, rows, results)
                        if pick is None:
                            flash = "no usable credential to inject"
                            continue
                        row, p5 = pick
                    at5 = f"{p5:.0f}% of 5h" if p5 is not None else "5h unknown"
                    ok = ask(keys, f"  write '{store.name(row)}' ({at5}) to {env_path}? [y/N]: ")
                    if not ok.lower().startswith("y"):
                        flash = "inject cancelled"
                        continue
                    try:
                        msg = env_set(env_path, store, store.token(row))
                    except (RuntimeError, OSError) as exc:
                        flash = f"not written: {exc}"
                        continue
                    read_live()
                    flash = f"injected '{store.name(row)}' — {msg}"
                    stop = ask(keys, "  a running supervisor keeps its old credential — "
                                     "stop it now? [y/N]: ")
                    flash += " · " + (daemon_stop() if stop.lower().startswith("y")
                                      else "restart Claude Code clients to pick it up")
                elif key == "T":
                    auto_rotate = not auto_rotate
                    st = load_state()
                    st["auto_rotate"] = auto_rotate
                    save_state(st)
                    read_live()
                    if not auto_rotate:
                        flash = "auto-rotate OFF"
                    elif not env_ok:
                        flash = "auto-rotate ON, but --no-env-write means nothing will be written"
                    else:
                        flash = f"auto-rotate ON — swaps when the live token passes {rotate_at:.0f}%"
                elif key == "z":
                    if not env_ok:
                        flash = "--no-env-write: the shell file is read-only"
                        continue
                    try:
                        msg, _on = env_toggle(env_path)
                    except (RuntimeError, OSError) as exc:
                        flash = f"not changed: {exc}"
                        continue
                    read_live()
                    flash = msg + " · restart Claude Code clients to pick it up"
                elif key == "a":
                    if store.readonly:
                        flash = "list is read-only (--from-env)"
                        continue
                    name = ask(keys, "\n  add — name: ")
                    if not name:
                        flash = "add cancelled"
                        continue
                    token = ask(keys, f"  add — token for '{name}' (hidden): ", secret=True)
                    why = validate_token(store, token)
                    if why:
                        flash = f"not added: {why}"
                        continue
                    r = probe(token, args.timeout)
                    p5, p7 = upct(r, "u5h"), upct(r, "u7d")
                    verdict = (f"HTTP {r.get('code')} · 5h {p5:.0f}% · 7d {p7:.0f}%"
                               if p5 is not None and p7 is not None
                               else f"HTTP {r.get('code')} · {r.get('err', 'no quota headers')}")
                    ok = ask(keys, f"  verified: {verdict}\n  save '{name}'? [y/N]: ")
                    if ok.lower().startswith("y"):
                        store.add(name, token)
                        results[token] = r
                        record({token: r})
                        flash = f"added '{name}' — {store.save()}"
                    else:
                        flash = "add cancelled"
                elif key == "d":
                    if store.readonly:
                        flash = "list is read-only (--from-env)"
                        continue
                    i = pick_row(keys, store, rows, "delete")
                    if i < 0:
                        flash = "delete cancelled"
                        continue
                    row = rows[i]
                    live_now = (" — this is the credential in "
                                f"{os.path.basename(env_path)}") if LIVE.get("token") == \
                        store.token(row) else ""
                    ok = ask(keys, f"  delete '{store.name(row)}' ({redact(store.token(row))})"
                                   f"{live_now}? [y/N]: ")
                    if ok.lower().startswith("y"):
                        store.rows.remove(row)
                        results.pop(store.token(row), None)
                        hist.pop(store.token(row), None)
                        if inspect == store.token(row):
                            inspect = None
                        flash = f"deleted '{store.name(row)}' — {store.save()}"
                    else:
                        flash = "delete cancelled"
                elif key == "e":
                    if store.readonly:
                        flash = "list is read-only (--from-env)"
                        continue
                    i = pick_row(keys, store, rows, "rename")
                    if i < 0:
                        flash = "rename cancelled"
                        continue
                    row = rows[i]
                    old = store.name(row)
                    new = ask(keys, f"  rename '{old}' to: ")
                    if new:
                        store.rows[store.rows.index(row)][store.name_col] = new
                        flash = f"renamed '{old}' to '{new}' — {store.save()}"
                    else:
                        flash = "rename cancelled"
    except KeyboardInterrupt:
        pass
    finally:
        if live_tty:
            sys.stdout.write(SHOW + ALT_OFF + (TITLE_POP if title_on else ""))
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
