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
survive a rewrite, and rows with an empty token are skipped. Use `CTR_CSV` or `--csv PATH` for a
file elsewhere.

Settings live in `.env` beside the script — start from the documented template, which lists every
key with its default (see [Configuration](#configuration)):

```bash
cp .env.example .env
```

> **Every refresh costs something.** Quota headers only appear on a call that bills inference, so
> one refresh is one `max_tokens=1` call per credential. The counter is always visible in the
> header, and `+` raises the interval without a restart.

### Keys

One line of keys sits under the table; `?` shows them all. Details, diagnosis, settings and
the key list each take the whole screen — nothing stacks under the table — and `Esc` (or `←`)
goes back.

| | |
|---|---|
| `↑` `↓` (`j` `k`, `Home` `End`, click a row) · `Esc` | select a row — highlighted — and `p` `n` `c` `e` `d` `D` act on it without asking for a row number · clear the selection |
| `⏎` (or `i`) | **details** of the selected credential: live / pinned / disabled, both windows with their resets, its idle clock, and the raw headers. `↑` `↓` step to the next credential, `←` / `Esc` back |
| `,` | **settings**: every `.env` key, grouped. `↑` `↓` move, `⏎` / `Space` toggle or edit, `←` `→` step a number or cycle a choice. A change **applies at once** and is saved to `.env` in place (comments kept); the few that need a restart (file paths, write modes, `CTR_ONLY`, the cap) say so. A key also set by a flag or exported `CTR_` is marked, since that still wins on the next start |
| `?` | all keys |
| `h` `w` `o` `b` | view 5h / 7d / extra credits / all |
| `r` `s` `D` | refresh · cycle sort · diagnose one credential (its own screen) |
| `1-9` `10`… `c` `f` `x` | copy that row's token (type two digits for row 10 and up) · copy any row by number · copy the freshest · copy the table as Markdown |
| `a` `d` `e` | add · delete · edit a credential (name and/or token) |
| `n` | disable / enable a credential: a disabled one is **never used at all** — never probed, never picked by auto-rotate, rebalance, the spend-down or `f`, refused by `p`, and moved off at once if it is live (even pinned). Stored as a `Disabled` column (`yes`) in `token.csv`, which you can also edit by hand |
| **`p`** **`z`** **`T`** | **pin a token live for Claude Code (`t`, the old key, still works) · switch to your /login and back · auto-swap off/park/on** |
| `u` | lift the pin `p` made — until then auto-rotate keeps its hands off it and no spend-down starts |
| `+` `-` `q` | interval · quit |
| click a header | sort by that column; click it again to reverse (▼ highest or latest first, ▲ lowest or soonest first) |

Clicking needs mouse reporting, and while it is on the terminal's own drag-to-select needs `Shift`
held. `--no-mouse` turns it off. The two `RESET` headers sort by the soonest reset of their own window.
The 7D one is close to the order auto-rotate spends quota in, though auto-rotate only weighs usable
credentials and looks at the 5h reset just to break near ties. Arrow keys are ignored rather than
read as letters.

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
| **Manual** (`p`) | pick a row, or leave blank for the freshest → confirm → the credentials file holds it, open sessions switch on their next request · **pinned** |
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
one with days of week left over another whose week ended in hours. `p`'s "freshest" pick still
ranks on headroom, since its job is the least bad option when nothing is usable.

There is no "beat the incumbent by N points" rule. The limits already say what counts as a sound
replacement, and a margin on top would reject candidates that are plainly fine. When nothing is
usable, auto-rotate stays put and says so once.

### Rebalance: the week that resets first goes first

The limits decide when the live credential **has** to go. Rebalance decides when it **should**:
while it is fine, a usable credential whose weekly reset comes more than an hour
(`CTR_RESET_TIE`) sooner takes over — whatever it has left is lost sooner. It skips a credential whose
readings moved in the last 30 minutes (`CTR_REBALANCE_IDLE`) — somebody is on it; this machine's
own use does not count. Every replacement, here and in ordinary rotation, prefers one with 5 points
to spare below each limit (`CTR_MIN_HEADROOM`), so a swap is not followed by another. Only with `T` on, never
over a pin, never more often than `CTR_ROTATE_GAP`. `CTR_REBALANCE=false` turns it off, leaving
rotation to the limits alone.

### Pinned credentials

`p` pins the credential you picked; the pin lifts by itself when that credential's week resets,
or with `u`. Auto-rotate still moves the live credential — pinned or not — with one allowance:
when its 5h quota is about to come back, it is worth riding out (rotating away only for rebalance
to bring it back after the 5h reset was the commonest swap of all):

| Live credential | Auto-rotate |
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

`p` is also the forced pick in the other sense: choosing "the freshest" from its prompt ignores the
limits entirely, so it still answers when nothing is usable. Refusing to name one would leave you
with nothing, when what you asked for was the least bad option.

### Spending a week down before it resets

Whatever is left of a weekly window at its reset is lost. In **off hours** — a weekend, an
Indonesian national holiday, or any night — auto-rotate makes a credential live with **both limits waived** when all of these hold:

| Condition | How it is checked |
|---|---|
| off hours | Saturday, Sunday, a date in `holidays.txt`, or **any night 22:00–07:00** (local clock) — so a weekend runs Friday 22:00 to Monday 07:00 |
| not in use on this machine | not the live credential, and no running session carries it |
| nobody else on it either | its 5h and 7d readings have not moved for **2 hours** while watched — one not watched yet (just after a start) counts as idle, so it can be picked at once (`CTR_BURN_UNSEEN`) |
| its weekly window resets in **under 7 hours** | the `r7d` header |
| quota left | weekly under 100% and not `rejected` |
| worth two swaps | at least 15 minutes left before the reset |
| not about to be refused | its 5h window under 95% — it qualifies again once that window resets |

The live credential itself gets the same treatment, minus the idle test (it is the one in use):
in off hours, with its week worth spending, it is **kept** instead of being rotated
away only to come back as an idle candidate two hours later.

Among several, the soonest weekly reset goes first — also during a spend-down: a credential that
became eligible since and loses its week more than an hour earlier takes over. It stays live past 66% weekly and past 61% 5h,
and the footer says so, only for as long as **all** of these still hold:

- it is still off hours — a night ends at 07:00, so a weekend hold ends at Monday 07:00;
- its weekly window has not reset — at the reset it was chosen for, or an earlier one;
- no window is refused and the weekly window is not full;
- auto-rotate is still `on` and the rule still enabled;
- nobody swapped by hand (`p`, `z`, or `e` on the live row end it).

Then the ordinary rules take over. A pin made with `p` outranks it. It survives a restart
(`data.json`, as a digest). The idle clock is kept in `data.json` too. A credential not watched
yet, or not watched for a while (a restart, a sleep, failed probes), counts as idle at once
(`CTR_BURN_UNSEEN`; `false` makes it wait the full 2 hours) — unless its readings moved across that
gap, which is use: then it waits like any other. A drop in 5h counts as a reset only when it reads
about 0%. It only acts with `T` on — in `park` and `off` it does nothing. `p`, `z` or `e` end a
hold; it starts again only if the rules pick that credential afresh.
`CTR_BURN_WINDOW` and `CTR_BURN_IDLE` (minutes) and `CTR_BURN` in `.env` — or `--burn-window`,
`--burn-idle`, `--no-burn` — change or disable it.

`holidays.txt` beside the binary lists the national holidays (libur nasional — **cuti bersama is
not included**), one `YYYY-MM-DD` per line, anything after the date is its name, `#` starts a
comment. A missing file means weekends only. It is read on every check, so a new year can be added
without a rebuild or a restart.

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
hours ago — but it never switches it on. Only `p` does that, and `p` is the deliberate override.

### How the two files carry it

| Trigger | `~/.zshenv` — a record, always switched off | `~/.claude/.credentials.json` — what Claude Code uses |
|---|---|---|
| `p` pin | records the token | `claudeAiOauth` → that token |
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
inherits it and needs a restart per swap (`p` then offers `claude daemon stop --any`).
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

- `--no-env-write` disables all writing, credentials file included; `p` `z` `T` become read-only
- `--no-creds-write` leaves `.credentials.json` alone and falls back to exporting the variable
- Refuses to write when more than one `export` line is active — guessing would be worse
- Refuses to rewrite a `.credentials.json` that is not valid JSON
- Atomic writes (temp file then `os.replace`), original file mode preserved; the credentials file
  and the parked login are always 0600
- Symlinks are followed to their target, so a dotfiles repo is not detached
- Token values are never printed to the screen
- **One writer per machine.** The first dashboard takes a lock (`data.json.lock`); a second one
  only watches — no rotation, `p` `z` `T` read-only — so two cannot swap the live credential back
  and forth or undo each other's pins. The pin and spend-down state are re-read from `data.json`
  on every check, and `data.json` is written atomically too
- **A prompt cannot freeze the rules.** Nothing refreshes while a prompt (`p`, `a`, `e`, `d`, `D`)
  waits for an answer, so one with no answer for 2 minutes cancels itself
- **A blip is not a verdict.** One timed-out or 5xx probe keeps the live credential (and its pin)
  until the next refresh; a refusal still rotates it

---

## Statusline

`plugin/statusline_command.md` draws the Claude Code status line — two rows, four columns:

```
Opus 5 (1M context) xhigh │ Dirs +0 · 14:30:00   │ Context 414.2k/1M · 41% │ Hourly 13% · 12 Sep 01:10
carol sk...3zR8nAAA       │ Session 414.2k (99%) │  ⚠ $12.80 · $0.04/min   │ Weekly 27% · 15 Sep 23:00
```

`plugin/statusline_simple.md` is the one-row alternative:

```
Opus 5.5 xhigh │ Nico ...rlMiTwAA │ 88% 2h 41m │ 81% 4h 31m
```

model and effort, the credential (name and the last 8 characters of its token), then the 5h and
the weekly window — always in that order — each as use and the time to its reset. It names the credential by the same rule as the grid below. Point
`statusLine.command` at whichever of the two you want.

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

## Configuration

Every setting has a key in `.env` beside the binary (or `main.py`). `.env.example` is the tracked,
commented template: it lists **every** key with its built-in default, so a fresh copy behaves
exactly like no `.env` at all. `.env` itself is git-ignored.

```bash
cp .env.example .env     # then edit what you need
```

Highest wins:

```
command-line flag  >  CTR_* exported in the environment  >  .env  >  built-in default
```

`CTR_LIMIT_7D=80 claude-token-rotate` overrides the file for one run. An empty value means the
default. An unknown `CTR_` key, a bad number or a bad boolean stops the program with the key and
where it came from — a typo cannot silently do nothing. Relative paths are taken beside the binary.

| `.env` key | Flag | Default | What it does |
|---|---|---|---|
| `CTR_CSV` | `--csv PATH` | `token.csv` | credential list |
| `CTR_ONLY` | `--only NAMES` | all | watch a subset (comma-separated names) |
| `CTR_HOLIDAYS` | `--holidays PATH` | `holidays.txt` | national holidays for the spend-down rule |
| `CTR_ENV_FILE` | `--env-file PATH` | `~/.zshenv` | shell file that records the live credential |
| `CTR_CREDS_FILE` | `--creds-file PATH` | `~/.claude/.credentials.json` (or under `$CLAUDE_CONFIG_DIR`) | the file Claude Code reads, kept in step |
| `CTR_LOG` | `--log CSV` | off | append every reading for later analysis |
| `CTR_INTERVAL` | `--interval SEC` | `60` | seconds between refreshes |
| `CTR_TIMEOUT` | `--timeout SEC` | `30` | per-probe HTTP timeout |
| `CTR_VIEW` | `--view b\|h\|w\|o` | `b` | starting view |
| `CTR_SORT` | `--sort csv\|5h\|7d\|ov\|name\|r5h\|r7d` | `r7d` | starting order — by default the week that resets first is on top (`r5h`/`r7d`: soonest reset first) |
| `CTR_ALERT` | `--alert PCT` | off | ring the bell when a window crosses this |
| `CTR_CAP` | `--cap USD\|auto\|off` | `auto` | extra-credit cap; `auto` reads it from `/api/oauth/usage` |
| `CTR_COLOR` | `--[no-]color` | `true` | ANSI colour |
| `CTR_TITLE` | `--[no-]title` | `true` | live credential in the terminal title |
| `CTR_MOUSE` | `--[no-]mouse` | `true` | header clicks; while on, drag-to-select needs `Shift` |
| `CTR_NOTIFY` | `--[no-]notify` | `true` | desktop notification when the live credential is past its limits and still in use |
| `CTR_ROTATE_MODE` | `--rotate-mode off\|park\|on` | last `T` | what auto-rotate may do (`--auto-rotate` = `on`) |
| `CTR_LIMIT_5H` | `--limit-5h PCT` | `61` | usable while 5h is below this (at most 60%) |
| `CTR_LIMIT_7D` | `--limit-7d PCT` | `66` | usable while weekly is below this (at most 65%) |
| `CTR_ROTATE_GAP` | `--rotate-gap SEC` | `15` | least time between two automatic swaps |
| `CTR_RESET_TIE` | `--reset-tie MIN` | `60` | weekly resets this close tie, and the 5h reset decides |
| `CTR_PIN_GRACE` | `--pin-grace MIN` | `60` | the live credential (pinned or not) over 5h is kept when 5h resets within this… |
| `CTR_PIN_CEILING` | `--pin-ceiling PCT` | `95` | …until its 5h reaches this |
| `CTR_BURN` | `--[no-]burn` | `true` | spend a week down before it resets, on days off |
| `CTR_BURN_WINDOW` | `--burn-window MIN` | `420` | …when the weekly reset is under this far away (7h) |
| `CTR_BURN_IDLE` | `--burn-idle MIN` | `120` | …and its readings have not moved for this long (2h) |
| `CTR_BURN_MIN` | `--burn-min MIN` | `15` | …but not with less than this left before the reset |
| `CTR_BURN_UNSEEN` | `--[no-]burn-unseen` | `true` | a credential not watched yet counts as idle (no 2h wait after a start) |
| `CTR_REBALANCE` | `--[no-]rebalance` | `true` | move to a usable credential whose week resets sooner |
| `CTR_REBALANCE_IDLE` | `--rebalance-idle MIN` | `30` | …but not onto one whose readings moved this recently (somebody is on it) |
| `CTR_MIN_HEADROOM` | `--min-headroom PCT` | `5` | a replacement needs this many points below each limit, if any has them |
| `CTR_NIGHT_FROM` | `--night-from HOUR` | `22` | every night from this hour counts as off hours (`24` = no nights) |
| `CTR_NIGHT_UNTIL` | `--night-until HOUR` | `7` | …until this hour in the morning (`0` = nights end at midnight) |
| `CTR_ENV_WRITE` | `--[no-]env-write` | `true` | write the shell file at all (false: `p`/`T`/`z` read-only) |
| `CTR_CREDS_WRITE` | `--[no-]creds-write` | `true` | write the credentials file (false implies the export way) |
| `CTR_EXPORT_ENV` | `--[no-]export-env` | `false` | also export the token from the shell file (a restart per swap) |

One-off actions are flags only:

| Flag | What it does |
|---|---|
| `--once` `--json` | one snapshot, then exit · as JSON |
| `--diagnose NAME` `--no-cli` | test both paths (API and `claude -p`) for one credential · skip the `claude -p` half |
| `--from-env [FILE]` | read `CLAUDE_CODE_OAUTH_TOKEN*` from the environment or a file instead of a CSV (read-only list) |

The status-line scripts are run by Claude Code, not by this program, so they do not read `.env`;
they take their own `STATUSLINE_*` variables (see [Statusline](#statusline)).

---

## Files it touches

| File | When |
|---|---|
| `token.csv` (+ `.bak`) | on `a` / `d` / `e` |
| `data.json` | cap cache, auto-swap setting, pin and spend-down state (SHA-256 prefixes, **not** tokens) |
| `.env` | read only — settings (copied from `.env.example`) |
| `holidays.txt` | read only — national holidays for the spend-down rule |
| `~/.zshenv` (+ `.claude_token_rotate.bak`, `.claude_token_rotate.orig`) | the record, on `p` / auto-swap / `e`, and once at startup to move an exported token over |
| `~/.claude/.credentials.json` — the `claudeAiOauth` block only | on `p` / `z` / auto-swap / `e` — this is what Claude Code uses (Linux only) |
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
