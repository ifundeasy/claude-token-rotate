---
description: Day-by-day Claude Code usage (requests and every token type) from ~/.claude transcripts
argument-hint: "[START YYYY-MM-DD] [END YYYY-MM-DD]"
---

Produce a day-by-day report of my Claude Code usage, read from the local transcripts in
`~/.claude/projects`, and save it under `.cache/usage/` in this repo.

## 1. Dates

Arguments given: `$ARGUMENTS`

- If a start date is given, use it. If an end date is also given, use that; otherwise the end is today.
- If no start date is given, **ask me first** which date to start from (YYYY-MM-DD), and offer
  "7 days ago", "the 1st of this month" and "everything" (the oldest transcript) as quick options.
  Do not run anything until I answer.
- Days are local calendar days (this machine's timezone), not UTC.

## 2. Run this script as is

Run it once with Bash, substituting START and END. Do not rewrite it with jq or a fresh script: the
de-duplication below is what keeps the numbers right.

```bash
OUT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.cache/usage"
python3 - START END "$OUT" <<'PY'
import csv, datetime as dt, json, os, sys
from collections import defaultdict

start, end, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
root = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")

F = ["input", "output", "thinking", "cache_write_5m", "cache_write_1h", "cache_write",
     "cache_read", "web_search", "web_fetch"]
seen: dict = {}
day = defaultdict(lambda: defaultdict(int))
day_sessions = defaultdict(set)
model = defaultdict(lambda: defaultdict(int))
files = 0
for base, _, names in os.walk(root):
    for n in names:
        p = os.path.join(base, n)
        # Every transcript, not only recent ones: a resumed session copies old requests into a
        # new file, and only the original sighting tells the copy apart.
        if not n.endswith(".jsonl"):
            continue
        files += 1
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                msg = e.get("message") or {}
                u = msg.get("usage")
                if e.get("type") != "assistant" or not u or msg.get("model") == "<synthetic>":
                    continue
                key = msg.get("id") or e.get("requestId") or e.get("uuid")
                ts = e.get("timestamp")
                if not key or not ts:
                    continue
                # Resumed and forked sessions copy history into a new transcript, often with a
                # later timestamp: keep the EARLIEST sighting of each request.
                when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if key not in seen or when < seen[key][0]:
                    seen[key] = (when, msg.get("model") or "?", u, bool(e.get("isSidechain")),
                                 e.get("sessionId"))

for when, mname, u, side, sess in seen.values():
    d = when.astimezone().date()
    if not d0 <= d <= d1:
        continue
    cc = u.get("cache_creation") or {}
    stu = u.get("server_tool_use") or {}
    v = {"input": u.get("input_tokens") or 0,
         "output": u.get("output_tokens") or 0,
         "thinking": (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
         "cache_write_5m": cc.get("ephemeral_5m_input_tokens") or 0,
         "cache_write_1h": cc.get("ephemeral_1h_input_tokens") or 0,
         "cache_write": u.get("cache_creation_input_tokens") or 0,
         "cache_read": u.get("cache_read_input_tokens") or 0,
         "web_search": stu.get("web_search_requests") or 0,
         "web_fetch": stu.get("web_fetch_requests") or 0}
    k = d.isoformat()
    row, mrow = day[k], model[mname]
    row["requests"] += 1
    row["subagent" if side else "main"] += 1
    mrow["requests"] += 1
    for f in F:
        row[f] += v[f]
        mrow[f] += v[f]
    tot = v["input"] + v["output"] + v["cache_write"] + v["cache_read"]
    row["total"] += tot
    mrow["total"] += tot
    if sess:
        day_sessions[k].add(sess)

COLS = ["requests", "main", "subagent", "sessions", "input", "output", "thinking",
        "cache_write_5m", "cache_write_1h", "cache_write", "cache_read", "total",
        "web_search", "web_fetch"]
for k in day:
    day[k]["sessions"] = len(day_sessions[k])
days = []
cur = d0
while cur <= d1:
    days.append(cur.isoformat())
    cur += dt.timedelta(days=1)
total = defaultdict(int)
for k in days:
    for c in COLS:
        if c != "sessions":
            total[c] += day[k][c] if k in day else 0
total["sessions"] = len(set().union(*day_sessions.values())) if day_sessions else 0

os.makedirs(out_dir, exist_ok=True)
stem = os.path.join(out_dir, f"{start}_{end}")
with open(stem + ".csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["date"] + COLS)
    for k in days:
        w.writerow([k] + [day[k][c] if k in day else 0 for c in COLS])
    w.writerow(["TOTAL"] + [total[c] for c in COLS])

n = lambda x: f"{x:,}"
H = ["Date", "Req", "Main", "Sub", "Sess", "Input", "Output", "Thinking", "CacheW 5m",
     "CacheW 1h", "CacheW", "CacheRead", "Total tok", "WebSearch", "WebFetch"]
lines = [f"# Claude Code usage {start} → {end}", "",
         f"Source: `{root}` ({files} transcript files, {len(seen)} unique requests scanned). "
         f"Dates are local ({dt.datetime.now().astimezone().tzname()}). All credentials combined.",
         "", "| " + " | ".join(H) + " |", "|" + "|".join(["---"] + ["---:"] * (len(H) - 1)) + "|"]
for k in days:
    lines.append("| " + " | ".join([k] + [n(day[k][c] if k in day else 0) for c in COLS]) + " |")
lines.append("| **TOTAL** | " + " | ".join(f"**{n(total[c])}**" for c in COLS) + " |")
lines += ["", "## Per model", "", "| Model | Req | Input | Output | Thinking | CacheW | CacheRead | Total tok |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
for mname, r in sorted(model.items(), key=lambda t: -t[1]["total"]):
    lines.append(f"| {mname} | " + " | ".join(n(r[c]) for c in
                 ["requests", "input", "output", "thinking", "cache_write", "cache_read", "total"]) + " |")
lines += ["", "Notes: Thinking is already inside Output. CacheW = CacheW 5m + CacheW 1h. "
          "Total tok = Input + Output + CacheW + CacheRead. Synthetic (non-API) messages are excluded; "
          "requests are de-duplicated by message id across all transcripts (resumed/forked sessions "
          "repeat history). Tokens are not quota %, and the credential used per request is not recorded."]
open(stem + ".md", "w").write("\n".join(lines) + "\n")
print(stem + ".md")
print(stem + ".csv")
PY
```

It writes two files, both under `.cache/usage/` (git-ignored):

- `.cache/usage/START_END.md` — the day-by-day table, a TOTAL row and a per-model table
- `.cache/usage/START_END.csv` — the same numbers, raw, for a spreadsheet

## 3. Report back

Read the `.md` it wrote and show me the day-by-day table, the per-model table, and the two file
paths. Then add three or four lines on what stands out: the busiest day, the main/subagent split,
and how much of the total is cache read. Do not invent numbers; quote only what the file says.

Column meanings, for your explanation:

| Column | Meaning |
|---|---|
| Req | API requests (unique message ids); Main = typed in a session, Sub = subagents/workflows |
| Sess | distinct sessions with at least one request that day |
| Input | uncached input tokens |
| Output | generated tokens; **Thinking** is the part of Output spent on extended thinking |
| CacheW 5m / 1h | prompt-cache writes with a 5-minute or 1-hour lifetime; CacheW is their sum |
| CacheRead | input served from the prompt cache |
| Total tok | Input + Output + CacheW + CacheRead |
| WebSearch / WebFetch | server-side tool calls |

Caveats to state plainly: all credentials are combined (transcripts do not record which token served
a request), and tokens cannot be converted into 5h/weekly quota percentages.
