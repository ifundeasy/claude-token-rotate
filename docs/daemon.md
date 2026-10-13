# Daemon and clients — specification (v2, draft)

One **daemon** does everything that touches an API or a file: it probes, rotates, spends weeks
down, rebalances, and writes state. Every `claude-token-rotate` opened in a shell is a **client**:
it shows a snapshot and sends commands. Any number of clients, one daemon.

Rotation rules do not change from v1.0.0. The daemon runs the same decision functions
(`usable`, `needs_rotate`, `rotate_pick`, `burn_pick`, `burn_left`, `burn_holds`, `day_off`,
`track_still`, `refused`) through an `Engine` that standalone mode also uses.

## Processes

| Command | What it is |
|---|---|
| `claude-token-rotate daemon` | the engine, headless. Holds `data.json.lock`; a second daemon exits with "already running (pid N)" |
| `claude-token-rotate daemon --dry-run` | shadow: decides and logs, writes nothing (no credentials, shell file, CSV, `.env`, `data.json`) |
| `claude-token-rotate` | client when the daemon answers; otherwise **standalone** (v1.0.0 behaviour, same `Engine` in-process) with a one-line note: "no daemon — `claude-token-rotate install-service`" |
| `claude-token-rotate install-service` / `uninstall-service` | write / remove `~/.config/systemd/user/claude-token-rotate.service`, then `systemctl --user daemon-reload` and `enable --now` / `disable --now` |
| `claude-token-rotate status` | daemon up? pid, version, uptime, last refresh, live credential, rotate mode; exit 0 healthy, 1 down, 2 stale (last refresh older than 3 intervals) |
| `claude-token-rotate shadow-report [DAY]` | compares a `--dry-run` log with what really happened |
| `--once`, `--json`, `--diagnose`, `--standalone` | unchanged, and always standalone: they probe themselves and write nothing (`--standalone` runs a full dashboard engine even with a daemon up) |

Unit file: `ExecStart=<resolved binary> daemon`, `Restart=on-failure`, `RestartSec=5`,
`WorkingDirectory=<app dir>`. The binary's directory stays the app dir (`token.csv`, `.env`,
`data.json`, `holidays.txt` beside it), as today.

## Transport

- Unix socket, `$XDG_RUNTIME_DIR/claude-token-rotate.sock`, else `<app dir>/.ctr.sock`.
  Override: `CTR_SOCKET` (new `.env` key). Created with mode **0600**; the daemon refuses to start
  if the socket's directory is writable by others.
- A stale socket file (nothing listening) is removed by the daemon at start, and only after the
  lock is held.
- **One JSON object per line**, UTF-8, request then response, max 64 KiB per line. A connection
  may carry any number of requests. Unknown fields are ignored; a malformed line gets
  `{"ok": false, "error": "bad request"}` and the connection stays open.
- Every request: `{"v": 1, "id": <int>, "cmd": "<name>", ...args}`.
  Every response: `{"v": 1, "id": <same>, "ok": true|false, "msg": "...", ...}`.
- Version: the daemon answers `hello` with `{"version": "2.x.y", "proto": 1}`. A client with a
  different `proto` prints "daemon speaks protocol N, this client M — restart the daemon
  (`systemctl --user restart claude-token-rotate`)" and runs read-only.

## Snapshot (`cmd: "snapshot"`)

Clients poll it about once a second (cheap: built from memory, no I/O). **No token value ever
appears in a snapshot.** Credentials are addressed by `id` = `pin_id(token)` (16-hex digest,
already used in `data.json`).

```json
{
  "v": 1, "seq": 1234, "now": 1791600000.0, "last_refresh": 1791599990.0,
  "interval": 60, "probes": 3456,
  "rows": [
    {"id": "90dc4f161aae2971", "name": "Finn", "tail": "Xy7Kq2mA", "disabled": false,
     "result": {"u5h": "0.88", "u7d": "0.81", "r5h": "...", "r7d": "...", "s5h": "allowed",
                "s7d": "allowed", "err": null, "...": "every probe field except _raw"},
     "hist": {"u5h": [[ts, pct], "..."], "u7d": [], "uov": []},
     "idle_since": 1791590000.0}
  ],
  "live": {"id": "...", "name": "Finn", "active": true, "mode": "file", "auto": "on",
           "held": "spending its week down — …", "err": null},
  "pinned": "<id>|null", "burning": {"id": "...", "until": 1791612000.0} ,
  "off_hours": true, "lim5": 101.0,
  "events": ["↺ burn: …", "…"], "notes": ["…"],
  "pool": {"cap": 40.0, "used": 41.08, "src": "…", "ts": 1791599000.0},
  "settings": {"CTR_LIMIT_5H": "61", "...": "every CONFIG key as text"},
  "sources": {"CTR_LIMIT_5H": ".env", "...": "default|.env|env|flag"},
  "diag": {"<id>": {"running": false, "result": {"...": "diagnose() output"}}},
  "readonly": false
}
```

`seq` increases on every change; a client re-renders only when it moves (or every second for
clocks). `hist` is capped like today (`HIST_MAX`). `readonly` is true in `--dry-run`.

## Commands

All write through the daemon; each answers `{"ok", "msg"}` where `msg` is the line a client
flashes (the same wording v1.0.0 flashes today).

| `cmd` | args | does |
|---|---|---|
| `hello` | `client` | version handshake |
| `snapshot` | `since_seq?` | the snapshot above (or `{"unchanged": true}`) |
| `refresh` | — | sweep now |
| `interval` | `seconds` | 5–3600; not saved (same as `+`/`-` today) |
| `pin` | `id` or `freshest: true` | `p`: go live, pin, end spend-down; refuses a disabled one |
| `unpin` | — | `u` |
| `toggle_live` | — | `z`; refuses switching a disabled one on |
| `rotate_mode` | `mode`: off\|park\|on | `T` (saved in `data.json` as today) |
| `disable` / `enable` | `id` | `n`; saves `token.csv` |
| `set` | `key`, `value` | settings screen: `convert_setting`, apply live, `write_env_value` |
| `add` | `name`, `token` | verifies with one probe first (as `a` today), then saves |
| `edit` | `id`, `name?`, `token?`, `update_live?` | as `e` today (probe, pin follows, optional live update) |
| `delete` | `id` | as `d` |
| `copy_token` | `id` or `freshest: true` | returns `{"token": "..."}` — the only reply that ever carries a token; the client puts it on its clipboard |
| `diagnose` | `id` | starts `diagnose()` in a daemon thread; result appears in `snapshot.diag` |

Confirmations (`[y/N]`) stay in the client; the daemon receives only the final command.

## Who writes what

| File | v2 writer |
|---|---|
| `~/.claude/.credentials.json`, `~/.zshenv` (+ backups) | daemon only |
| `data.json` (pin, burn, idle clocks, rotate mode, pool, scopes) | daemon only |
| `token.csv` | daemon only (`add`/`edit`/`delete`/`disable`) |
| `.env` | daemon only (`set`) |
| clipboard, terminal title, the screen | each client |

A client never opens those files for writing. In standalone mode the one process is both, exactly
as v1.0.0.

## Failure behaviour

| Situation | What happens |
|---|---|
| Daemon down when a client starts | standalone (v1 behaviour) + the note; it does not start a daemon itself |
| Daemon dies while clients are open | clients show "daemon gone — reconnecting…" and retry every 2 s; nothing is written meanwhile; systemd restarts it in 5 s |
| Daemon restarts | pin, spend-down, idle clocks, rotate mode come back from `data.json` (already persisted today) |
| Second daemon | exits 1 with the holder's pid |
| Client while a standalone instance runs | the standalone holds the lock and has no socket: the client is standalone-read-only (v1 watcher behaviour) |
| Laptop sleep | the gap rule in `track_still` already restarts idle clocks; a refresh happens on wake |
| A command during a refresh | serialised by one engine lock; a refresh never runs twice at once |

## Shadow mode (phase 5)

`daemon --dry-run` runs the full `tick`, but `go_live`, `save_state`, `Store.save`,
`write_env_value` and `notify` become no-ops that log instead. Every refresh appends to
`.cache/shadow/YYYY-MM-DD.jsonl`: `{"ts", "would": "<decision or null>", "live_actual": "<name>",
"pinned_actual", "burn_actual"}`, with the actual values read from the credentials file and
`data.json` that the running v1 dashboard writes. `shadow-report` lists every refresh where
`would` and what the v1 dashboard actually did within the next refresh differ.

## Out of scope for v2.0.0

Remote clients, multi-user machines, a web UI, Windows/macOS service files.
