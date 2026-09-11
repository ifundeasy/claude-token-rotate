#!/bin/bash
# Claude Code status line: a fixed 2-row x 4-column labeled grid.
#
#   <model> (<modes>)  | Dirs +N · HH:MM:SS  | Context <used>/<size> · <pct> | Hourly <pct> · <reset>
#   <account>          | Session <tok> (<%>) |        <warn> <price> · <rate> | Weekly <pct> · <reset>
#
# No meter bars: every cell is a label plus a number, so the display costs two
# terminal rows and spends no width on decoration.
#
# ALIGNMENT IS THE POINT. Both rows always print every surviving column, padded
# to that column's widest cell, so a separator on the top row always has one
# directly beneath it. A column is dropped only when BOTH of its rows are empty
# — never when just one is — because dropping it on a single row is exactly what
# makes the pipes disagree. Cells measure their PLAIN text; measuring the colored
# text would count ANSI bytes as width and misalign. Two cells break the plain
# left-aligned mould on purpose, both visible in the layout above: the price is
# right-aligned under Context (it carries no label of its own), and the two
# rate-limit percentages are right-aligned against each other so "109%" and
# " 56%" line up on their last digit.
#
# Nearly every field comes straight from the statusLine JSON on stdin — nothing
# here is estimated or faked. Two things are sourced elsewhere because that JSON
# does not carry them (verified against a live payload: it has no auth fields at
# all): the account identity (CLAUDE_CODE_OAUTH_TOKEN when the environment sets
# one, else the login email from the CLI's own config file) and the caveman mode
# badge (read from the plugin's own flag file, same as it would render if it
# owned the statusline).
#
# One caveat on the Session cell, so nobody reads it as something it isn't: the
# payload carries NO cumulative per-session token counter. Its total_input_tokens
# is the current request's whole input (it equals cache_read + cache_creation +
# input exactly), not a running sum. So Session reports the most recent request's
# full token volume across all four buckets, and its percentage is how much of
# that volume was served from cache — real numbers, and distinct from Context,
# which is the window occupancy.
#
# Forks are the whole performance story here, not arithmetic: a render is a few
# hundred string operations, and every $( ) around one of them costs more than
# all of them together. So one jq call formats every numeric/time value, the grid
# below assembles each cell exactly once, and padding uses `printf -v` rather
# than command substitution. The account block's second jq is the deliberate
# exception: it reads a file other tooling rewrites in place, so it must be able
# to fail on its own without taking the render down with it. Measured 5x on this
# machine: ~23ms per render on the token path, ~34ms on the email path (the
# email path pays for both the /proc scan and the config read; the token path
# stops as soon as it has the token).

input=$(cat)

# \033[2m ("faint") renders barely-visible gray on several terminals — contrast
# depends on the terminal's own faint implementation. \033[90m (bright black) is
# an explicit gray that reads consistently instead. printf -v, not $( ): nine
# colors would otherwise be nine forks before the first field is even parsed.
printf -v DIM '\033[90m'
printf -v BOLD '\033[1m'
printf -v CYAN '\033[36m'
printf -v OK_GREEN '\033[1;32m'
printf -v WARN_YELLOW '\033[1;33m'
printf -v HOT_RED '\033[1;31m'
printf -v REV '\033[7m'
printf -v CAVEMAN_COLOR '\033[38;5;172m'
printf -v RESET '\033[0m'

# Both set REPLY rather than printing, so callers need no subshell to read them.
pct_color() { # $1 = integer percentage used
  if [ "$1" -ge 80 ]; then REPLY="$HOT_RED"
  elif [ "$1" -ge 50 ]; then REPLY="$WARN_YELLOW"
  else REPLY="$OK_GREEN"
  fi
}
spaces() { # $1 = count (a count <= 0 yields "")
  REPLY=""
  [ "$1" -gt 0 ] && printf -v REPLY '%*s' "$1" ''
}

# --- caveman mode badge ---
# The caveman plugin writes this flag file whether or not it renders its own
# statusline; a custom statusLine (this file) suppresses that render, so this
# reads the same file instead of losing the badge. Refuse a symlink (a hostile
# flag file swap shouldn't get followed) and strip anything outside [a-z0-9-]
# before the value reaches a printf, so a poisoned flag can't inject ANSI/OSC
# escapes into the terminal.
caveman_plain="" caveman_colored=""
CAVEMAN_FLAG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.caveman-active"
if [ -f "$CAVEMAN_FLAG" ] && [ ! -L "$CAVEMAN_FLAG" ]; then
  cm=$(head -c 64 "$CAVEMAN_FLAG" 2>/dev/null)
  cm="${cm//[^a-zA-Z0-9-]/}"
  cm="${cm,,}"
  case "$cm" in
    off|lite|full|ultra|wenyan-lite|wenyan|wenyan-full|wenyan-ultra|commit|review|compress)
      if [ "$cm" = "full" ]; then caveman_plain="[CAVEMAN]"; else caveman_plain="[CAVEMAN:${cm^^}]"; fi
      caveman_colored="${CAVEMAN_COLOR}${caveman_plain}${RESET} "
      caveman_plain="${caveman_plain} "
      ;;
  esac
fi

# --- account ---
# Which identity this session actually authenticates as — and this must never be
# a guess: a session running on an env OAuth token must not be labelled with the
# locally stored subscription login, which is a different account entirely.
#
# Reading our own environment is not enough, and that was the bug here. Claude
# Code does not pass CLAUDE_CODE_OAUTH_TOKEN down to the statusline subprocess
# (probed on this machine: the child env carries CLAUDECODE, CLAUDE_PID,
# CLAUDE_CODE_SESSION_ID and friends, but never the token), while the session
# process itself does carry it. CLAUDE_PID names that process, so its /proc
# environ is the authority, with PPID as a fallback guess. The value is reduced
# to its display form and dropped immediately, so the secret lives for the two
# lines it takes to mask it; the read is a `read` builtin over the NUL-separated
# file, which costs no fork and routes the value through no pipe that could
# surface it in an error message.
#
# On a host without /proc an env-token session is indistinguishable from a stored
# login, and this falls back to the email.
#
# The token renders as <first2>...<last STATUSLINE_TOKEN_TAIL>: the tail is what
# actually tells two tokens apart, the head being a fixed public prefix. One too
# short to keep head, tail and elision distinct shows "hidden" instead of
# printing nearly all of itself. Values get a printable-only strip (pure
# parameter expansion, no fork): neither an env var nor a config file should be
# able to push ANSI/OSC escapes into the terminal.
tok_tail="${STATUSLINE_TOKEN_TAIL:-8}"
case "$tok_tail" in ''|*[!0-9]*) tok_tail=8 ;; esac
acct_plain="" acct_colored="" acct_secret=""
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  acct_secret="$CLAUDE_CODE_OAUTH_TOKEN"
else
  for acct_probe in "${CLAUDE_PID:-}" "$PPID"; do
    [ -n "$acct_probe" ] && [ -r "/proc/$acct_probe/environ" ] || continue
    while IFS= read -r -d '' acct_kv; do
      case "$acct_kv" in
        CLAUDE_CODE_OAUTH_TOKEN=*) acct_secret="${acct_kv#*=}"; break ;;
      esac
    done < "/proc/$acct_probe/environ"
    acct_kv=""
    [ -n "$acct_secret" ] && break
  done
fi
if [ -n "$acct_secret" ]; then
  if [ "${#acct_secret}" -gt $(( tok_tail + 4 )) ]; then
    acct_val="${acct_secret:0:2}...${acct_secret: -${tok_tail}}"
  else
    acct_val="hidden"
  fi
  acct_secret=""
  acct_val="${acct_val//[^[:print:]]/}"
  acct_plain="Token ${acct_val:0:64}"
  acct_colored="${DIM}Token${RESET} ${acct_val:0:64}"
else
  # Deliberately a separate guarded jq, not folded into the one big call below:
  # this file is large and gets rewritten in place by other tooling, so a
  # half-written read must cost this one cell, never the whole render.
  acct_file="${CLAUDE_CONFIG_DIR:+${CLAUDE_CONFIG_DIR}/.claude.json}"
  [ -n "$acct_file" ] && [ -f "$acct_file" ] || acct_file="$HOME/.claude.json"
  acct_val=$(jq -r '.oauthAccount.emailAddress // empty' "$acct_file" 2>/dev/null)
  acct_val="${acct_val//[^[:print:]]/}"
  acct_plain="${acct_val:0:64}"
  [ -n "$acct_plain" ] && acct_colored="${acct_plain}"
fi

# --- one jq call, formatted output ---
# Joined with the unit separator U+001F, not @tsv: bash's `read` always treats
# tab as "IFS whitespace" no matter what IFS is set to, so consecutive tabs from
# adjacent empty fields collapse into one delimiter and every field after the
# first empty one shifts left. U+001F isn't whitespace to bash, so an empty field
# between two of them reads back as an empty field, not a skip — no per-field
# `// empty` needed, an absent value just stays "". Below it is spelled as a
# six-character jq escape on purpose: written as the raw byte instead, it is
# invisible in an editor and in `cat`, and that join reads as join("") to anyone
# who copies the line. Field order here must match the `read` exactly; the
# trailing "" is a throwaway field so a future added element can't get silently
# absorbed by the last real variable.
IFS=$'\x1f' read -r \
  model badge_str \
  dirs_n dur_hms \
  sess_tok sess_pct \
  ctx_used_t ctx_size_t ctx_pct \
  cost_str rate_str cost_warn \
  five_pct five_reset week_pct week_reset \
  _rest <<< "$(printf '%s' "$input" | jq -r '
    def money2($x): ($x*100|round) as $c
      | ($c/100|floor) as $d
      | ($c - $d*100) as $r
      | "\($d).\($r|tostring|if length==1 then "0"+. else . end)";
    def usd: if .==null then "" elif (.>0 and .<0.01) then "<$0.01" else "$"+money2(.) end;
    def rate($u;$ms): if ($ms==null or $ms==0 or $u==null) then "" else "$"+money2($u/($ms/60000))+"/min" end;
    # Token counts: 2 decimals past a million, 1 past a thousand. jq drops a
    # trailing zero on its own, so a round 1000000 prints "1M", not "1.00M".
    def tok: if .==null then ""
             elif .>=1000000 then "\(((./1000000)*100|round)/100)M"
             elif .>=1000 then "\(((./1000)*10|round)/10)k"
             else "\(.|round)" end;
    def pad2: tostring | if length==1 then "0"+. else . end;
    def hms($ms): if ($ms==null or $ms==0) then "" else
      (($ms/1000|floor) as $t
       | "\($t/3600|floor|pad2):\(($t%3600)/60|floor|pad2):\($t%60|pad2)") end;
    def stamp($r): if $r==null then "" else ($r|strflocaltime("%-d %b %H:%M")) end;
    (.context_window.current_usage // {}) as $u
    | (($u.input_tokens // 0) + ($u.output_tokens // 0)
       + ($u.cache_creation_input_tokens // 0) + ($u.cache_read_input_tokens // 0)) as $sess
    | [
      (.model.display_name // .model.id // "claude"),
      ([ (if .fast_mode then "⚡" else empty end),
         (.effort.level // empty),
         (if (.thinking.enabled == false) then "nothink" else empty end),
         (if (.output_style.name and .output_style.name != "default")
          then .output_style.name else empty end) ] | join(" ")),
      (.workspace.added_dirs // [] | length | tostring),
      (.cost.total_duration_ms | hms(.)),
      (if $sess == 0 then "" else ($sess | tok) end),
      (if $sess == 0 then "" else
        (($u.cache_read_input_tokens // 0) * 100 / $sess | round | tostring) end),
      ((.context_window.total_input_tokens // 0) + (.context_window.total_output_tokens // 0) | tok),
      (.context_window.context_window_size | tok),
      (.context_window.used_percentage | if .==null then "" else (.|round|tostring) end),
      (.cost.total_cost_usd | usd),
      (rate(.cost.total_cost_usd; .cost.total_duration_ms)),
      (if (.cost.total_cost_usd // 0) >= ($warn|tonumber) then "true" else "" end),
      (.rate_limits.five_hour.used_percentage | if .==null then "" else (.|round|tostring) end),
      (.rate_limits.five_hour.resets_at | stamp(.)),
      (.rate_limits.seven_day.used_percentage | if .==null then "" else (.|round|tostring) end),
      (.rate_limits.seven_day.resets_at | stamp(.)),
      ""
    ] | join("\u001f")' --arg warn "${STATUSLINE_COST_WARN:-5.00}")"

# --- cell contents ---
# Eight cells, indexed row*4+col: PT plain (measured), CT colored (printed), AL
# alignment within the column. Pre-filled so an unset cell reads back empty
# rather than unbound and the grid math can treat every slot uniformly.
for i in 0 1 2 3 4 5 6 7; do PT[$i]=""; CT[$i]=""; AL[$i]="l"; done
set_cell() { # row col plain colored [align]
  local i=$(( $1 * 4 + $2 ))
  PT[$i]="$3"; CT[$i]="$4"; AL[$i]="${5:-l}"
}

# col 0: model with its mode badges on top, account beneath. The badges only take
# parentheses when the model name hasn't already ended in a group of its own, so
# "Opus 5 (1M context)" gains a bare "xhigh", not a second bracket pair.
model_plain="${caveman_plain}${model}"
model_colored="${caveman_colored}${BOLD}${CYAN}${model}${RESET}"
if [ -n "$badge_str" ]; then
  case "$model" in
    *\)) model_plain="${model_plain} ${badge_str}"
         model_colored="${model_colored} ${DIM}${badge_str}${RESET}" ;;
    *)   model_plain="${model_plain} (${badge_str})"
         model_colored="${model_colored} ${DIM}(${badge_str})${RESET}" ;;
  esac
fi
set_cell 0 0 "$model_plain" "$model_colored"
set_cell 1 0 "$acct_plain" "$acct_colored"

# col 1: added workspace dirs and wall-clock duration on top, this request's
# token volume beneath. Either half of the top cell can be missing, so it joins
# whichever survives instead of leaving a stray separator behind.
top=""
[ -n "$dirs_n" ] && top="+${dirs_n}"
if [ -n "$dur_hms" ]; then
  [ -n "$top" ] && top="${top} · ${dur_hms}" || top="$dur_hms"
fi
[ -n "$top" ] && set_cell 0 1 "Dirs ${top}" "${DIM}Dirs${RESET} ${top}"
if [ -n "$sess_tok" ]; then
  sess="${sess_tok} (${sess_pct}%)"
  set_cell 1 1 "Session ${sess}" "${DIM}Session${RESET} ${sess}"
fi

# col 2: context occupancy on top, price beneath. The price carries no label and
# is right-aligned under Context. Its badge is reverse video rather than ANSI
# blink (\033[5m): iTerm2/Ghostty/Terminal.app silently ignore blink, and where
# it does work it animates a line that sits on screen the whole session.
if [ -n "$ctx_pct" ]; then
  ctx="${ctx_used_t}/${ctx_size_t} · ${ctx_pct}%"
  pct_color "$ctx_pct"
  set_cell 0 2 "Context ${ctx}" "${DIM}Context${RESET} ${REPLY}${ctx}${RESET}"
fi
if [ -n "$cost_str" ]; then
  price="$cost_str"
  [ -n "$rate_str" ] && price="${price} · ${rate_str}"
  if [ "$cost_warn" = "true" ]; then
    set_cell 1 2 " ⚠ ${price} " "${REV}${HOT_RED} ⚠ ${price} ${RESET}" "r"
  else
    set_cell 1 2 "$price" "$price" "r"
  fi
fi

# col 3: the two rate-limit windows, each as used% and when it resets. The two
# percentages are padded to a common width so their last digits line up, which
# is why they are built together rather than in two independent blocks.
pctw=${#five_pct}
[ ${#week_pct} -gt "$pctw" ] && pctw=${#week_pct}
if [ -n "$five_pct" ]; then
  spaces $(( pctw - ${#five_pct} )); lead="$REPLY"
  pct_color "$five_pct"
  set_cell 0 3 "Hourly ${lead}${five_pct}% · ${five_reset}" \
    "${DIM}Hourly${RESET} ${lead}${REPLY}${five_pct}%${RESET} ${DIM}· ${five_reset}${RESET}"
fi
if [ -n "$week_pct" ]; then
  spaces $(( pctw - ${#week_pct} )); lead="$REPLY"
  pct_color "$week_pct"
  set_cell 1 3 "Weekly ${lead}${week_pct}% · ${week_reset}" \
    "${DIM}Weekly${RESET} ${lead}${REPLY}${week_pct}%${RESET} ${DIM}· ${week_reset}${RESET}"
fi

# --- grid math ---
sep=" ${DIM}│${RESET} "
keep=()
for c in 0 1 2 3; do
  if [ -n "${PT[$c]}" ] || [ -n "${PT[$(( 4 + c ))]}" ]; then keep+=("$c"); fi
done
for c in "${keep[@]}"; do
  w=${#PT[$c]}
  [ ${#PT[$(( 4 + c ))]} -gt "$w" ] && w=${#PT[$(( 4 + c ))]}
  W[$c]=$w
done

# --- render ---
# Every kept column is emitted on BOTH rows, so each separator has a counterpart
# directly above or below it. Trailing blanks are trimmed only after the line is
# assembled, which can never move a separator: it only removes spaces that sit
# past the last visible glyph.
render_row() { # row
  local r="$1" out="" first=1 c i
  for c in "${keep[@]}"; do
    i=$(( r * 4 + c ))
    [ $first -eq 0 ] && out="${out}${sep}"
    if [ "${AL[$i]}" = "r" ]; then
      spaces $(( W[c] - ${#PT[$i]} ))
      out="${out}${REPLY}${CT[$i]}"
    else
      spaces $(( W[c] - ${#PT[$i]} ))
      out="${out}${CT[$i]}${REPLY}"
    fi
    first=0
  done
  out="${out%"${out##*[![:space:]]}"}"
  printf '%s\n' "$out"
}

render_row 0
render_row 1
exit 0
