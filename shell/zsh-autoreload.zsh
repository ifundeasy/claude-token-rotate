# claude-token-rotate — pick up a rotated token without opening a new terminal.
#
# ~/.zshenv is read once, when the shell starts. A shell that is already running therefore keeps
# whatever token it began with, which is why a rotation used to mean "open a new terminal". This
# re-reads the file just before the next prompt, but only when it has actually changed.
#
# WHAT THIS DOES AND DOES NOT FIX. Every `claude` you start from this shell afterwards gets the
# current token — no new terminal, no logout. It does NOT reach processes that are already
# running: their environment was copied at exec and cannot be changed from outside. For those,
# see `claude daemon stop --any`.
#
# Re-sourcing is safe because the file is written to be idempotent: the PATH blocks are guarded
# against duplicates, and a parked credential re-runs its `unset`, which is exactly what should
# happen to a shell still holding the old value.
#
# INSTALL — add to ~/.zshrc:
#     source /path/to/claude-token-rotate/shell/zsh-autoreload.zsh
# Set CLAUDE_ENV_FILE first if the tool manages a file other than ~/.zshenv.

zmodload -F zsh/stat b:zstat 2>/dev/null || return
autoload -Uz add-zsh-hook

typeset -g _ctr_env_file=${CLAUDE_ENV_FILE:-$HOME/.zshenv}
typeset -g _ctr_seen=""

_ctr_autoreload() {
  # Keyed on inode as well as mtime and size, and that is the part that matters: mtime has
  # one-second resolution, so two rotations inside the same second would look identical. Every
  # write goes through a temp file and a rename, so the inode changes each time without fail.
  # -H, not repeated +element flags: zstat takes exactly one of those and reads any further
  # ones as filenames, which fails silently in a precmd and leaves the hook doing nothing.
  local -A st
  zstat -H st "$_ctr_env_file" 2>/dev/null || return
  local now="$st[inode]:$st[mtime]:$st[size]"
  [[ $now == $_ctr_seen ]] && return
  # The first prompt only records the timestamp: the shell just sourced this file itself, and
  # sourcing it twice at startup would be work with nothing to show for it.
  [[ -n $_ctr_seen ]] && source "$_ctr_env_file"
  _ctr_seen=$now
}

add-zsh-hook precmd _ctr_autoreload
