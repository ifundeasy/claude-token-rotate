# claude-token-rotate

Keeps Claude Code working across a pool of credentials: watches every account's 5h, weekly and
usage-credit quota, and swaps the token in your shell before the active one runs dry.

One Python file, **standard library only** — no `pip install`, no build step, no third-party
dependencies.

```
╭ CLAUDE QUOTA · 5h + 7d + extra credits ───────────── 00:00:16 · csv · 60s · 7 probes ╮
│ 7 creds · 1 blocked · 5.9/6 5h free · best bob 0%                                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯

    #  NAME    TOKEN                5H            RESET      7D      RESET   EXTRA $  STATE
  ▎ 1  alice   …Kp2-7xQ4mAAA        5%  ░░░░░░░░  3h 09m     84%     1d 6h   ~$41.20  EXTRA spent
    3  carol   …Vt9-3zR8nAAA       13%  █░░░░░░░  1h 09m     27%    3d 22h       off  allowed

 ◆ your shell is using carol — 13% of its 5h window used · auto-swap off T turns it on
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
| `1-9` `c` `p` `x` | copy that row's token · copy any row by number · copy the freshest · copy the table as Markdown |
| `a` `d` `e` | add · delete · edit a credential (name and/or token) |
| **`t`** **`z`** **`T`** | **inject a token into your shell · activate/deactivate · auto-swap on/off** |
| `+` `-` `q` | interval · quit |

---

## Token rotation

One credential is the one your shell actually exports. The dashboard marks it **cyan** in the NAME
column and can change it.

| How | Behaviour |
|---|---|
| **Manual** (`t`) | pick a row, or leave blank for the freshest → confirm → write → offered `claude daemon stop --any` |
| **Auto** (`T`) | cycles three ways — see below |
| **Off** (`z`) | comments the `export` line out *and* writes an explicit `unset`; press again to re-enable |

Auto mode **never** touches the supervisor. Stopping it terminates live sessions — that is a
decision a person makes, not a timer.

**Both windows decide, with separate limits.** A credential at 4% of its five hours and 96% of its
week is fresh by the five-hour number alone and refused on the next request, so each window is
checked on its own:

```
trigger     max(5h, weekly) on the live credential >= --rotate-at   (75%)
            and at least 300s since the last swap
            and the live credential is not pinned

candidate   5h     < 50%
            weekly < 80%, or < 60% while more than 3 WORKING days remain
            max(5h, weekly) < 75%

choose      the lowest max(5h, weekly)
```

**Working days, not calendar days.** The weekly budget is spent on the days you work, so Saturdays
and Sundays inside the remaining window are subtracted before the 3-day test. Being close to a
weekend therefore relaxes the budget rather than tightening it — those are the days least likely
to need the quota.

The tighter 60% band exists because a week with room left to run has to stretch; once the reset is
near, whatever is unspent would be wasted anyway, so the limit returns to 80%.

There is no "beat the incumbent by N points" rule. The per-window limits already say what counts
as a sound replacement, and a margin on top would reject candidates that are plainly fine.

**A hand-injected credential is pinned.** `t` records your choice, and auto-rotate will not swap
it away — not at 80%, not at 95%. The pin lifts only when that credential can no longer serve a
request at all (`EXTRA spent`, rejected, unauthorized), because at that point protecting the choice
protects nothing. Injecting another credential moves the pin; the pin survives a restart.

`t` is also the forced pick in the other sense: choosing "the freshest" from its prompt ignores the
candidate limits entirely, so it still answers when no credential is comfortable. Refusing to name
one would leave you with nothing, when what you asked for was the least bad option.

**`T` cycles three ways**, because "stop rotating my live credential" and "stop tracking which
credential is best" are different wishes:

| Mode | What auto-rotate does past the threshold |
|---|---|
| `off` | nothing — the shell file is left alone entirely |
| `park` | writes the best credential **switched off**: recorded, but nothing uses it |
| `on` | writes it and leaves the file's own on/off state alone |

`park` is the answer to "rotate it, but do not let anything pick it up". The value is written
commented out **and** an `unset` line goes with it, so a new terminal gets nothing at all — not the
rotator's pick, and not the stale value it inherited from the desktop session. Press `z` when you
want to hand the parked credential over.

`--rotate-mode off|park|on` sets it from the command line.

**A switched-off variable is kept fresh, but stays off.** Writing a value and switching it on are
separate decisions, and the second one is yours. With `z` off, auto-rotate still updates the parked
value, so turning it back on hands you the best credential rather than whatever was there hours ago
— but it never re-enables the line. Only `t` does that, and `t` is the deliberate override: it
ignores the threshold and switches the variable on, saying so when that was a change.

### What you need to know

**A rotation does not reach processes that are already running.** A shell reads its rc at startup,
and the Claude Code supervisor hands its own credential to every background session it owns. Until
that supervisor restarts, the swap is invisible to exactly the sessions that matter. That is why
`t` offers to run `claude daemon stop --any`.

**Switching off writes `unset`, not just a `#`.** Commenting the export out removes the assignment
but cannot remove an *inheritance*. A desktop session freezes the variable into its own environment
at login and hands it to every terminal it spawns, so a shell that merely skips the export still
starts with the old value already set. Measured on a GNOME session: 127 processes — `gnome-shell`
and the systemd user manager among them — still carried a token that had been "disabled" in the
file hours earlier, and a brand-new terminal inherited it. The explicit `unset` is what makes a new
shell actually clean; re-enabling removes that line again so it cannot undo the export.

If the variable is also in the systemd user manager, clear it there too — otherwise anything
systemd starts keeps inheriting it:

```bash
systemctl --user unset-environment CLAUDE_CODE_OAUTH_TOKEN
```

Processes that are already running keep their copy regardless; only a restart (or a full logout)
clears those.

**Only the assignment line is rewritten.** The file is read as lines, exactly one is replaced, and
the rest are written back untouched — so a comment block explaining why the variable is there
survives a rotation.

**Two backups** land beside the file:

| File | Contents |
|---|---|
| `<rc>.claude_token_rotate.bak` | the previous version, rewritten every time |
| `<rc>.claude_token_rotate.orig` | what the file looked like before this tool ever touched it — written **once**, never again |

The second matters because auto-swap can write several times an hour; a single rolling backup
would not be enough to recover the original.

**It tells you when the credential is still in use.** When the live token crosses the threshold
while a Claude Code session on this machine is still running on it, you get one desktop
notification naming the credential, the window and how many sessions hold it. It fires once per
crossing and only when a session genuinely has that token — a warning about a credential nothing
is using is noise. `--no-notify` turns it off.

Nothing is interrupted, and that is not a limitation to work around. There is no supported way to
put a message into a running interactive session: the kernel refuses keystroke injection into
another terminal (`dev.tty.legacy_tiocsti=0`), writing to its pts would only paint bytes over the
display, and a signal would discard whatever the turn had in flight. Telling you is both the
safest option and the only one that lets the turn finish first.

Sessions are identified by executable name (`claude`, or the versioned launcher). Markers like
`CLAUDE_CODE_ENTRYPOINT` look tidier but every child a session spawns inherits them, so a hook or
a notification helper would be miscounted as a session.

### Picking up a rotation without a new terminal

`~/.zshenv` is read once, when a shell starts, so a shell that is already running keeps the token
it began with. `shell/zsh-autoreload.zsh` re-reads the file just before the next prompt, but only
when it has actually changed:

```bash
# ~/.zshrc
source /path/to/claude-token-rotate/shell/zsh-autoreload.zsh
```

It keys on the file's **inode**, not only its mtime. `mtime` has one-second resolution, so two
rotations inside the same second would look identical; every write goes through a temp file and a
rename, so the inode changes without fail.

Re-sourcing is safe by construction: the PATH blocks are guarded against duplicates, and a parked
credential re-runs its `unset`, which is exactly what should happen to a shell still holding the
old value.

### What still has to restart

| | Needs a restart? |
|---|---|
| Your shell | **No** — the hook re-reads the file in place |
| A new `claude` started from that shell | **No** — it inherits the current value |
| The supervisor and its background sessions | Yes, but only the supervisor: `claude daemon stop --any` |
| The interactive session you are sitting in | **Yes** — nothing can change it |

The last row is an operating-system guarantee, not a limitation of this tool. A process receives a
*copy* of the environment when it is exec'd, and nothing outside it can alter that copy. Node reads
it into `process.env` at startup as well, so even writing to `/proc/<pid>/environ` would change
nothing the session looks at.

So the shortest path after a rotation, without closing a single terminal:

```bash
claude daemon stop --any     # the supervisor picks up the new token
claude -r                    # same terminal, same shell, fresh credential
```

### Using bash

`CLAUDE_CODE_OAUTH_TOKEN` lives in `~/.zshenv`, which **bash does not read**. Point the tool
elsewhere:

```bash
python3 main.py --env-file ~/.bashrc
```

### Safeguards

- `--no-env-write` disables all writing; `t` `z` `T` become read-only
- Refuses to write when more than one `export` line is active — guessing would be worse
- Atomic writes (temp file then `os.replace`), original file mode preserved
- Symlinks are followed to their target, so a dotfiles repo is not detached
- Token values are never printed to the screen

---

## Statusline

`plugin/statusline_command.md` draws the Claude Code status line — two rows, four columns:

```
Opus 5 (1M context) xhigh │ Dirs +0 · 14:30:00   │ Context 414.2k/1M · 41% │ Hourly 13% · 12 Sep 01:10
Token sk...3zR8nAAA       │ Session 414.2k (99%) │  ⚠ $12.80 · $0.04/min   │ Weekly 27% · 15 Sep 23:00
```

The **Token** cell is what ties this to the dashboard: it shows the tail of the live
`CLAUDE_CODE_OAUTH_TOKEN`, so you can see which credential is in use without opening anything.

The file is named `.md` but contains bash. Execution is decided by the `#!/bin/bash` shebang, not
the extension, so it runs — the trade-off is that editors treat it as Markdown and shell syntax
highlighting is lost.

### How to run it

Needs `jq` and `sed`. The script reads its JSON payload from **stdin**, so to try it:

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

To install it, point `statusLine` at it in `~/.claude/settings.json`:

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
| `--rotate-at PCT` | auto-swap threshold, applied to both the 5h and the weekly window (default 75) |
| `--auto-rotate` | start with auto-swap on |
| `--no-env-write` | never write a shell file |
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
| `~/.zshenv` (+ `.claude_token_rotate.bak`, `.claude_token_rotate.orig`) | on `t` / `z` / auto-swap |
| `dist/`, `.build/` | only on `./build.sh` |
| `~/.claude/settings.json` | only if you install the statusline yourself |

Tokens are never printed: the table shows a truncated tail, and copy actions put the full value on
the clipboard.

---

## Tests

```bash
python3 tests/run.py
```

Standard library only, no framework. Every case builds its own CSV and shell rc in a temp
directory — nothing touches a real file. Shell behaviour is verified by running `zsh` with the
variable already set in the environment, which is the only way to catch the failure the suite
exists for: commenting an `export` out looks right in the file and does nothing to a shell that
inherited the value from its parent.

---

## License

MIT — see [LICENSE](LICENSE).
