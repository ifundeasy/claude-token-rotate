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
| **Auto** (`T`) | once the live token passes `--rotate-at` (default 75% of its 5h window), a fresher credential is swapped in |
| **Off** (`z`) | comments the `export` line out *and* writes an explicit `unset`; press again to re-enable |

Auto mode **never** touches the supervisor. Stopping it terminates live sessions — that is a
decision a person makes, not a timer.

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
| `--rotate-at PCT` | auto-swap threshold (default 75) |
| `--auto-rotate` | start with auto-swap on |
| `--no-env-write` | never write a shell file |
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

## License

MIT — see [LICENSE](LICENSE).
