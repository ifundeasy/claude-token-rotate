# claude-token-rotate

Keeps Claude Code working across a pool of credentials: watches every account's 5h, weekly and
usage-credit quota, and swaps the token Claude Code uses before the active one runs dry — through
its credentials file, so **sessions that are already open switch on their next request, without a
restart**.

One Python file, **standard library only** — no `pip install`, no build step, no third-party
dependencies.

> **Linux only. Tested on Ubuntu, nowhere else.** macOS is not supported: Claude Code keeps its
> login in the Keychain there, not in `~/.claude/.credentials.json`, so the token swap has nothing
> to write to (the tool skips it on macOS). Windows is not supported at all. The statusline and the
> in-use warning also read `/proc`, which only Linux has.

```
╭ CLAUDE QUOTA · 5h + 7d + extra credits ───────────── 00:00:16 · csv · 60s · 7 probes ╮
│ 7 creds · 1 blocked · 5.9/6 5h free · best bob 0%                                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯

    #  NAME    TOKEN                5H            RESET      7D      RESET   EXTRA $  STATE
  ▎ 1  alice   …Kp2-7xQ4mAAA        5%  ░░░░░░░░  3h 09m     84%     1d 6h   ~$41.20  EXTRA spent
    3  carol   …Vt9-3zR8nAAA       13%  █░░░░░░░  1h 09m     27%    3d 22h       off  allowed

 ◆ Claude Code is using carol — 13% of its 5h window used · auto-swap off T cycles it
 $ team credits: $41.08 spent of $40.00 — over the cap · checked just now
```

---

## Three windows, not two

| Window | What it is |
|---|---|
| `5h` | the five-hour window |
| `7d` | the weekly window |
| `extra` | usage credits — an **org-wide** pool with its own billing cycle |

The first two can read "plenty left" while the third is exhausted, and the third is what actually
refuses premium models. That is why all three are on screen at once.

**Utilization is published in whole percentage points.** A row reading `0%` means unused, not
freshly reset. This is the single most common misreading.

**Dollar amounts come from `/api/oauth/usage`** — the same endpoint the IDE extensions read. That
endpoint refuses a `claude setup-token` credential (it lacks the `user:profile` scope), so the cap
is read from the interactive Claude Code login on this machine instead. That is sound, because the
credit pool belongs to the org. A `~` in front of an amount means it was derived from a rounded
ratio rather than measured.

---

## Run it

```bash
python3 main.py                 # live dashboard
python3 main.py --once          # one snapshot, then exit
python3 main.py --once --json   # machine-readable
python3 main.py --interval 300  # cheaper if you leave it open
```

`token.csv` sits beside the script. Start from the example:

```bash
cp token.example.csv token.csv
chmod 600 token.csv          # it holds credentials
```

```csv
Name,CLAUDE_CODE_OAUTH_TOKEN
alice,sk-ant-oat01-...
bob,sk-ant-oat01-...
```

Mint a token per account with `claude setup-token`. Column order does not matter, extra columns
survive a rewrite, and rows with an empty token are skipped. Use `--csv PATH` for a file elsewhere.

> **Every refresh costs something.** Quota headers only appear on a call that bills inference, so
> one refresh is one `max_tokens=1` call per credential. The counter is always visible in the
> header, and `+` raises the interval without a restart.

### Keys

| | |
|---|---|
| `h` `w` `o` `b` | view 5h / 7d / extra credits / all |
| `r` `s` `i` `D` | refresh · cycle sort · raw headers · diagnose one credential |
| `1-9` `10`… `c` `p` `x` | copy that row's token (type two digits for row 10 and up) · copy any row by number · copy the freshest · copy the table as Markdown |
| `a` `d` `e` | add · delete · edit a credential (name and/or token) |
| **`t`** **`z`** **`T`** | **hand a token to Claude Code · switch to your /login and back · auto-swap off/park/on** |
| `+` `-` `q` | interval · quit |

---

## Token rotation

One credential is the one Claude Code actually uses. The dashboard marks it **cyan** in the NAME
column and can change it — and **a change reaches sessions that are already open, without a
restart.**

That works because running Claude Code sessions **re-read `~/.claude/.credentials.json` before
every request**. It is not documented anywhere; it was measured on Claude Code 2.1.285, in headless
and interactive sessions alike, against a local stub API: the request right after the file changed
already carried the new token. So the tool hands a token over by writing it into that file — and
the shell **never exports it**, because a session that inherited `CLAUDE_CODE_OAUTH_TOKEN` ignores
the file for the rest of its life (measured too: the file swapped twice, every request still
carried the variable). This is **file mode**, the default.

| How | Behaviour |
|---|---|
| **Manual** (`t`) | pick a row, or leave blank for the freshest → confirm → the credentials file holds it, open sessions switch on their next request · **pinned** |
| **Auto** (`T`) | cycles three ways — see below |
| **Off / on** (`z`) | off: your `/login` goes back into the credentials file · on: the recorded token again |

### The limits

```
usable      5h < 61%  (at most 60%)   and   weekly < 66%
trigger     the live credential is not usable, and 15s have passed since the last swap
            (checked after every refresh — `--interval`, default 60s)
choose      among the usable ones, the soonest weekly reset;
            weekly resets under 1h apart: the soonest 5h reset
```

**Both windows decide.** A credential at 4% of its five hours and 96% of its week is fresh by the
five-hour number alone and refused on the next request, so each window is checked on its own. The
weekly limit is flat — no weekend or working-day arithmetic. `--limit-5h` and `--limit-7d` move
them.

**Spend first what resets first.** Whatever is left of a quota at its reset is lost, so among the
usable credentials the one whose weekly window resets soonest is picked — the weekly quota is the
scarce one. When two weekly resets are less than an hour apart, the sooner 5h reset decides.
Ranking on headroom alone used to tie credentials at the same busiest window and let CSV order pick
one with days of week left over another whose week ended in hours. `t`'s "freshest" pick still
ranks on headroom, since its job is the least bad option when nothing is usable.

There is no "beat the incumbent by N points" rule. The limits already say what counts as a sound
replacement, and a margin on top would reject candidates that are plainly fine. When nothing is
usable, auto-rotate stays put and says so once.

### Pinned credentials

`t` pins the credential you picked. Auto-rotate still moves a pinned credential, with one
allowance — when its quota is about to come back, it is worth riding out:

| Pinned credential | Auto-rotate |
|---|---|
| under both limits | keeps it |
| over the 5h limit, window resets **within 1 hour** | keeps it **until 5h reaches 95%** |
| over the 5h limit, reset **more than 1 hour** away | rotates |
| over the weekly limit | rotates — the weekly reset is days away |
| spent (`5h rejected`, `7d rejected`, `EXTRA spent`, unauthorized) | rotates |

While a pin is being held the footer says so (`held until 95%`), for as long as it holds. Rotating
away clears the pin. It survives a restart, recorded in `data.json` as a digest, never as the
token. A spent window is easy to miss — its 429 still carries the quota headers, so the only sign is
the status — and the pin used to overlook it and hold a spent credential indefinitely.

`t` is also the forced pick in the other sense: choosing "the freshest" from its prompt ignores the
limits entirely, so it still answers when nothing is usable. Refusing to name one would leave you
with nothing, when what you asked for was the least bad option.

### `T` cycles three ways

"Stop rotating my live credential" and "stop tracking which credential is best" are different
wishes:

| Mode | What auto-rotate does when the live credential is past its limits |
|---|---|
| `off` | nothing — no file is written |
| `park` | records the best credential **switched off**: your `/login` stays live, `z` hands the pick over |
| `on` | records it, and makes it live if a token was live |

`--rotate-mode off|park|on` sets it from the command line.

**A switched-off credential is kept fresh, but stays off.** Writing a value and switching it on are
separate decisions, and the second one is yours. With `z` off, auto-rotate still updates the
recorded value, so turning it back on hands you the best credential rather than whatever was there
hours ago — but it never switches it on. Only `t` does that, and `t` is the deliberate override.

### How the two files carry it

| Trigger | `~/.zshenv` — a record, always switched off | `~/.claude/.credentials.json` — what Claude Code uses |
|---|---|---|
| `t` inject | records the token | `claudeAiOauth` → that token |
| `z` off | unchanged | `claudeAiOauth` → your `/login`, exactly as it was |
| `z` on | unchanged | `claudeAiOauth` → the recorded token |
| auto-rotate `on` | records the pick | follows it, if a token was live |
| auto-rotate `park` | records the pick | your `/login` |
| `e` edit the live token | records the new value | follows it |

`~/.zshenv` keeps the token as a commented-out `export` **plus an `unset`**: the value is there for
`z` to switch back to, and the `unset` keeps every shell clean — including one that inherited the
variable from the desktop session. Only the `claudeAiOauth` block of the credentials file changes;
MCP logins and every other key are written back untouched.

**Moving over from an exported token.** If the shell still exports a token when the dashboard
starts, it is moved into the credentials file once (the file first, so there is no moment with
neither), and the footer counts the open sessions that still carry the old variable. Restart those
once with `claude -r`; every swap after that reaches them live.

**What an injected block looks like.** The token, **no refresh token**, and an expiry a year out
(what `claude setup-token` mints). Without a refresh token Claude Code never tries to refresh it —
and a refresh is exactly what would quietly swap your `/login` back in behind the tool's back. The
plan fields (`subscriptionType`, `rateLimitTier`) are carried over from the login.

**Your `/login` is parked, not lost.** Before the first injection the login block is copied to
`~/.claude/.credentials.json.claude_token_rotate.login` (0600 — it holds the refresh token), and
switching off copies it back verbatim and deletes the parked copy. Injecting over an injected
token keeps the login parked earlier; it is never replaced by a token that is already in the CSV.

Two cases where switching off does **not** restore:

- **You ran `/login` while a token was injected.** That login is newer than the parked one, so it
  is kept and the parked copy is dropped.
- **There is no parked login** (the file held an injected token before this tool ever saw a
  login). Switching off then refuses and says so, and the injected token stays: removing the only
  working credential would sign every running session out with nothing to fall back on. Run
  `/login` in Claude Code once and the next injection parks it.

**`--export-env`** brings back the old behaviour: the shell file exports the variable as well, so
scripts that need `CLAUDE_CODE_OAUTH_TOKEN` get it — and every session started from that shell
inherits it and needs a restart per swap (`t` then offers `claude daemon stop --any`).
`--no-creds-write` implies it, `--creds-file PATH` points at another credentials file, and
`--no-env-write` blocks every write. On macOS there is no credentials file to write, so it is env
mode there.

### What you need to know

**A swap does not reach a process that inherited the variable.** Its environment was copied at exec
and nothing outside it can change that copy — Node reads it into `process.env` at startup, so even
writing to `/proc/<pid>/environ` would change nothing the session looks at. Tested both other
routes as well: neither the credentials file nor `env` in `~/.claude/settings.json` moves such a
session. In file mode that only ever means a session started before the switch.

**Switching off writes `unset`, not just a `#`.** Commenting the export out removes the assignment
but cannot remove an *inheritance*. A desktop session freezes the variable into its own environment
at login and hands it to every terminal it spawns, so a shell that merely skips the export still
starts with the old value already set. Measured on a GNOME session: 127 processes — `gnome-shell`
and the systemd user manager among them — still carried a token that had been "disabled" in the
file hours earlier, and a brand-new terminal inherited it. The explicit `unset` is what makes a new
shell actually clean.

If the variable is also in the systemd user manager, clear it there too — otherwise anything
systemd starts keeps inheriting it:

```bash
systemctl --user unset-environment CLAUDE_CODE_OAUTH_TOKEN
```

**Only the assignment line is rewritten.** The file is read as lines, exactly one is replaced, and
the rest are written back untouched — so a comment block explaining why the variable is there
survives a rotation.

**Two backups** land beside the shell file:

| File | Contents |
|---|---|
| `<rc>.claude_token_rotate.bak` | the previous version, rewritten every time |
| `<rc>.claude_token_rotate.orig` | what the file looked like before this tool ever touched it — written **once**, never again |

The second matters because auto-swap can write several times an hour; a single rolling backup
would not be enough to recover the original.

**It tells you when a session is stuck on the credential.** When the live token goes past its
limits while a Claude Code session on this machine still carries it in its environment, you get one
desktop notification naming the credential, the window and how many sessions hold it. In file mode
that only happens for a session left over from before. `--no-notify` turns it off.

Nothing is interrupted, and that is not a limitation to work around. There is no supported way to
put a message into a running interactive session: the kernel refuses keystroke injection into
another terminal (`dev.tty.legacy_tiocsti=0`), writing to its pts would only paint bytes over the
display, and a signal would discard whatever the turn had in flight.

Sessions are identified by executable name (`claude`, or the versioned launcher). Markers like
`CLAUDE_CODE_ENTRYPOINT` look tidier but every child a session spawns inherits them, so a hook or
a notification helper would be miscounted as a session.

### Keeping running shells clean

`~/.zshenv` is read once, when a shell starts. `shell/zsh-autoreload.zsh` re-reads it just before
the next prompt, but only when it has actually changed:

```bash
# ~/.zshrc
source /path/to/claude-token-rotate/shell/zsh-autoreload.zsh
```

In file mode what it re-runs is the `unset`, so a shell still holding the variable from before
drops it at its next prompt, and the next `claude` started there follows every swap. Under
`--export-env` it picks up the new export instead.

It keys on the file's **inode**, not only its mtime. `mtime` has one-second resolution, so two
rotations inside the same second would look identical; every write goes through a temp file and a
rename, so the inode changes without fail. Re-sourcing is safe by construction: the PATH blocks
are guarded against duplicates, and the `unset` is idempotent.

### What still has to restart

| | Needs a restart? |
|---|---|
| An open Claude Code session that did not inherit the variable | **No** — it re-reads `.credentials.json` on its next request |
| A session that inherited the variable (started before file mode, or under `--export-env`) | **Yes, once** — `claude -r` |
| Your shell | **No** — the hook re-reads the file in place |
| The supervisor | only if it inherited the variable: `claude daemon stop --any` |

### Using bash

`CLAUDE_CODE_OAUTH_TOKEN` lives in `~/.zshenv`, which **bash does not read**. Point the tool
elsewhere:

```bash
python3 main.py --env-file ~/.bashrc
```

### Safeguards

- `--no-env-write` disables all writing, credentials file included; `t` `z` `T` become read-only
- `--no-creds-write` leaves `.credentials.json` alone and falls back to exporting the variable
- Refuses to write when more than one `export` line is active — guessing would be worse
- Refuses to rewrite a `.credentials.json` that is not valid JSON
- Atomic writes (temp file then `os.replace`), original file mode preserved; the credentials file
  and the parked login are always 0600
- Symlinks are followed to their target, so a dotfiles repo is not detached
- Token values are never printed to the screen

---

## Statusline

`plugin/statusline_command.md` draws the Claude Code status line — two rows, four columns:

```
Opus 5 (1M context) xhigh │ Dirs +0 · 14:30:00   │ Context 414.2k/1M · 41% │ Hourly 13% · 12 Sep 01:10
carol sk...3zR8nAAA       │ Session 414.2k (99%) │  ⚠ $12.80 · $0.04/min   │ Weekly 27% · 15 Sep 23:00
```

The **account** cell (row two, first column) is what ties this to the dashboard. It names the
credential **this session** is using, and works it out on every render:

| The session authenticates with | The cell shows |
|---|---|
| a token in its own environment, listed in `token.csv` | `carol sk...3zR8nAAA` — its name and tail |
| an injected token in `.credentials.json`, listed in `token.csv` | `carol sk...3zR8nAAA` |
| either of the above, **not** in `token.csv` | `Token sk...3zR8nAAA` |
| your `/login` | `you@example.com` |

The order matches Claude Code's own: a variable in the session's environment wins, then the
credentials file. The file is re-read every render, so after a swap the cell follows within one
`refreshInterval`. An injected block is told from a `/login` by its shape — a `/login` always has a
refresh token behind it, an injected token never does.

The name comes from `token.csv`: `STATUSLINE_TOKEN_CSV` if set, else the one beside the plugin
directory, else the one beside `claude-token-rotate` on `PATH` (the symlink is resolved) — which is
what lets a copy installed elsewhere find it. It is matched on the full token by the header's `Name` and `CLAUDE_CODE_OAUTH_TOKEN` columns in any
order. The token never leaves `jq`: it goes in through the environment, not argv, and only the
name and the masked tail come back out.

The file is named `.md` but contains bash. Execution is decided by the `#!/bin/bash` shebang, not
the extension, so it runs — the trade-off is that editors treat it as Markdown and shell syntax
highlighting is lost.

### How to run it

Needs `jq` 1.6+ (for `--rawfile` and `$ENV`) and `sed`. The script reads its JSON payload from **stdin**, so to try it:

```bash
echo '{"model":{"display_name":"Opus 5"},"context_window":{"context_window_size":200000,
"current_usage":{"input_tokens":2000,"output_tokens":500,"cache_read_input_tokens":40000,
"cache_creation_input_tokens":1000},"total_input_tokens":43500,"used_percentage":22},
"cost":{"total_cost_usd":1.23,"total_duration_ms":600000},
"rate_limits":{"five_hour":{"used_percentage":13,"resets_at":1789160000}}}' \
  | ./plugin/statusline_command.md
```

`resets_at` is an **epoch number**, not an ISO string, and `current_usage` is an **object** of four
token buckets, not a number. Those two are the easiest things to get wrong when assembling a test
payload by hand.

To install it, point `statusLine` at it in `~/.claude/settings.json` — the repo file itself, or a
copy such as `~/.claude/statusline-command.sh` (a copy has to be refreshed after each update, and
finds `token.csv` through `claude-token-rotate` on `PATH`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "/path/to/claude-token-rotate/plugin/statusline_command.md",
    "refreshInterval": 60,
    "padding": 0
  }
}
```

---

## Build a binary (no Python on the target)

```bash
./build.sh              # -> dist/claude-token-rotate
./build.sh onedir       # a folder instead; starts instantly, no unpack step
```

The build verifies itself: the result is run in an empty environment with no interpreter on `PATH`,
and the build fails if it does not start.

| | Measured |
|---|---|
| Size | 11 MB |
| Startup | +0.32 s (onefile unpacks itself; `onedir` does not) |
| Requires | glibc 2.14+ — roughly any Linux since 2011 |

Python is needed **only to build**, never to run the result.

> The binary is per-platform and per-libc. A Linux build will not run on macOS, and a glibc build
> will not run on Alpine. Build on the oldest system you intend to support — glibc is backward
> compatible, not forward.

The CSV and the state file are looked up **beside the binary**, not in the directory you invoked it
from. Symlinking onto `PATH` is safe: `sys.executable` is already resolved, so the CSV stays next to
the real file.

`rm -rf .build` removes the build venv; the next build recreates it.

---

## Put it on PATH

```bash
cp dist/claude-token-rotate .                              # token.csv sits beside it
ln -sf "$PWD/claude-token-rotate" ~/.local/bin/claude-token-rotate
```

`~/.local/bin` is already on `PATH` for both zsh and bash on most systems — zsh via `.zshrc`,
bash/sh via `.profile`. If not:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

To keep using the Python script instead:

```bash
# ~/.zshrc or ~/.bashrc
alias ctr='python3 /path/to/claude-token-rotate/main.py'
```

---

## Flags

| Flag | What it does |
|---|---|
| `--csv PATH` | credential list (default: beside the script) |
| `--from-env [FILE]` | read from the environment instead of a CSV (list is read-only) |
| `--only NAMES` | watch a subset |
| `--interval N` `--timeout N` | seconds between refreshes · per-probe timeout |
| `--view b\|h\|w\|o` `--sort` | initial view · initial order |
| `--alert PCT` | ring the terminal bell when a window crosses this |
| `--log CSV` | append every reading for later analysis |
| `--cap USD\|auto\|off` | extra-credit cap; `auto` reads it from `/api/oauth/usage` |
| `--env-file PATH` | shell file to manage (default `~/.zshenv`) |
| `--limit-5h PCT` | a credential is usable while its 5h window is below this (default 61, i.e. at most 60%) |
| `--limit-7d PCT` | a credential is usable while its weekly window is below this (default 66) |
| `--export-env` | also export `CLAUDE_CODE_OAUTH_TOKEN` from the shell file (the old way — a restart per swap) |
| `--auto-rotate` | start with auto-swap on |
| `--no-env-write` | never write a shell file (nor the credentials file) |
| `--creds-file PATH` | Claude Code credentials file kept in step (default `~/.claude/.credentials.json`, or under `$CLAUDE_CONFIG_DIR`) |
| `--no-creds-write` | leave the credentials file alone; implies `--export-env` |
| `--no-notify` | no desktop notification when the live credential is still in use |
| `--diagnose NAME` | test both paths (API and `claude -p`) for one credential |
| `--once` `--json` | one snapshot · as JSON |
| `--no-title` `--no-color` | leave the terminal title alone · no colour |

---

## Files it touches

| File | When |
|---|---|
| `token.csv` (+ `.bak`) | on `a` / `d` / `e` |
| `data.json` | cap cache and auto-swap setting (SHA-256 prefixes, **not** tokens) |
| `~/.zshenv` (+ `.claude_token_rotate.bak`, `.claude_token_rotate.orig`) | the record, on `t` / auto-swap / `e`, and once at startup to move an exported token over |
| `~/.claude/.credentials.json` — the `claudeAiOauth` block only | on `t` / `z` / auto-swap / `e` — this is what Claude Code uses (Linux only) |
| `~/.claude/.credentials.json.claude_token_rotate.login` | your parked `/login` while a token is injected; removed on restore |
| `dist/`, `.build/` | only on `./build.sh` |
| `~/.claude/settings.json` | only if you install the statusline yourself |

Tokens are never printed: the table shows a truncated tail, and copy actions put the full value on
the clipboard.

---

## Tests

```bash
python3 tests/run.py
```

Standard library only, no framework. Every case builds its own CSV, shell rc and credentials file
in a temp directory — nothing touches a real file. The statusline cases run the real script
against a temp `CLAUDE_CONFIG_DIR`. Shell behaviour is verified by running `zsh` with the
variable already set in the environment, which is the only way to catch the failure the suite
exists for: commenting an `export` out looks right in the file and does nothing to a shell that
inherited the value from its parent.

---

## License

MIT — see [LICENSE](LICENSE).
