#!/bin/bash
# Simple one-row status line: <model> / <effort> │ <name> ...<token tail>  5h X% / 7d Y%
#
# A separate, smaller sibling of statusline_command.md (the 2-row grid), which this
# leaves alone. Same identity rule as the grid, trimmed: the token comes from the
# session process' environ (Claude Code does not hand it to the statusline), else
# from the credentials file, where a refresh token behind the access token means a
# /login. The name is the token.csv row whose token matches. The token is reduced to
# its tail at once and never printed whole.
#
# Without a token (a /login) it shows the email; a window the payload does not carry
# is left out. Colors follow the grid: gray label, green < 50%, yellow < 80%, red.

input=$(cat)
printf -v DIM '\033[90m'; printf -v BOLD '\033[1m'; printf -v RESET '\033[0m'
pct_color() { # $1 = integer percentage
  if [ "$1" -ge 80 ]; then REPLY=$'\033[1;31m'
  elif [ "$1" -ge 50 ]; then REPLY=$'\033[1;33m'
  else REPLY=$'\033[1;32m'; fi
}

secret="$CLAUDE_CODE_OAUTH_TOKEN"
if [ -z "$secret" ]; then
  for pid in "${CLAUDE_PID:-}" "$PPID"; do
    [ -n "$pid" ] && [ -r "/proc/$pid/environ" ] || continue
    while IFS= read -r -d '' kv; do
      case "$kv" in CLAUDE_CODE_OAUTH_TOKEN=*) secret="${kv#*=}"; break ;; esac
    done < "/proc/$pid/environ"
    kv=""; [ -n "$secret" ] && break
  done
fi

src="${BASH_SOURCE[0]}"; [ -L "$src" ] && src=$(readlink -f "$src")
csv="${STATUSLINE_TOKEN_CSV:-${src%/*}/../token.csv}"
if [ -z "${STATUSLINE_TOKEN_CSV:-}" ] && [ ! -r "$csv" ]; then
  bin=$(command -v claude-token-rotate 2>/dev/null)
  [ -n "$bin" ] && bin=$(readlink -f "$bin") && csv="${bin%/*}/token.csv"
fi
[ -r "$csv" ] || csv=/dev/null
creds="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
[ -r "$creds" ] || creds=/dev/null

IFS=$'\x1f' read -r kind name tail8 <<< "$(
  CTR_TOK="$secret" jq -rn --rawfile csv "$csv" --rawfile creds "$creds" '
    (try ($creds | fromjson | .claudeAiOauth) catch null) as $o
    | ($ENV.CTR_TOK // "") as $env
    | (if $env != "" then [$env, "token"]
       elif ($o.accessToken? // "") != "" then
         [$o.accessToken, (if ($o.refreshToken // "") != "" then "login" else "token" end)]
       else ["", ""] end) as [$tok, $kind]
    | [ $csv | split("\n")[] | sub("\r$"; "") | select(length > 0)
        | [ split(",")[] | sub("^\""; "") | sub("\"$"; "") ] ] as $rows
    | ($rows[0] // []) as $head
    | ($head | index("Name")) as $ni
    | ($head | index("CLAUDE_CODE_OAUTH_TOKEN")) as $ti
    | (if $kind == "token" and $ni != null and $ti != null
       then first($rows[1:][] | select(.[$ti] == $tok) | .[$ni]) // "" else "" end) as $name
    | [ $kind, $name, (if $kind == "token" and ($tok | length) > 12 then $tok[-8:] else "" end) ]
    | join("\u001f")' 2>/dev/null)"
secret=""

line=""
if [ "$kind" = "token" ]; then
  name="${name//[^[:print:]]/}"; tail8="${tail8//[^[:print:]]/}"
  line="${BOLD}${name:-Token}${RESET}"
  [ -n "$tail8" ] && line="${line} ${DIM}...${tail8}${RESET}"
else
  f="${CLAUDE_CONFIG_DIR:+${CLAUDE_CONFIG_DIR}/.claude.json}"
  [ -n "$f" ] && [ -f "$f" ] || f="$HOME/.claude.json"
  email=$(jq -r '.oauthAccount.emailAddress // empty' "$f" 2>/dev/null)
  email="${email//[^[:print:]]/}"
  [ -n "$email" ] && line="${BOLD}${email:0:64}${RESET}"
fi

IFS=$'\x1f' read -r p5 p7 model effort _ <<< "$(printf '%s' "$input" | jq -r '
  [ (.rate_limits.five_hour.used_percentage | if . == null then "" else (. | round | tostring) end),
    (.rate_limits.seven_day.used_percentage | if . == null then "" else (. | round | tostring) end),
    (.model.display_name // .model.id // ""),
    (.effort.level // ""),
    "" ] | join("\u001f")' 2>/dev/null)"

# The model block: name bold cyan, then "/ effort" in gray. Claude Code does
# not put the permission mode (auto, plan, ...) in the payload, so it cannot be shown.
model="${model//[^[:print:]]/}"; effort="${effort//[^[:print:]]/}"
head=""
if [ -n "$model" ]; then
  head="${BOLD}"$'\033[36m'"${model:0:40}${RESET}"
  [ -n "$effort" ] && head="${head} ${DIM}/ ${effort}${RESET}"
fi

quota=""
if [ -n "$p5" ]; then pct_color "$p5"; quota="${DIM}5h${RESET} ${REPLY}${p5}%${RESET}"; fi
if [ -n "$p7" ]; then
  pct_color "$p7"
  [ -n "$quota" ] && quota="${quota} ${DIM}/${RESET} "
  quota="${quota}${DIM}7d${RESET} ${REPLY}${p7}%${RESET}"
fi
[ -n "$quota" ] && { [ -n "$line" ] && line="${line}  ${quota}" || line="$quota"; }
[ -n "$head" ] && { [ -n "$line" ] && line="${head} ${DIM}│${RESET} ${line}" || line="$head"; }
printf '%s\n' "$line"
