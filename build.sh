#!/usr/bin/env bash
# Freeze claude_token_rotate.py into a single executable that needs no Python on the target machine.
#
# WHY THIS IS CHEAP HERE. claude_token_rotate.py imports nothing outside the standard library, so there is
# no dependency graph to get wrong and no C extension to compile — the whole job is stapling a
# CPython runtime to one file. That is also why the result stays around 11 MB instead of the
# hundreds a scientific stack would drag in.
#
# WHAT YOU GET
#   dist/claude-token-rotate          one file; run it anywhere with a compatible libc
#
# The binary looks for token.csv and writes .claude_token_rotate.json BESIDE ITSELF, not
# beside wherever you invoked it from — see app_dir() in claude_token_rotate.py. Symlinking it onto PATH
# works: sys.executable is already resolved, so the CSV stays next to the real file.
#
# PORTABILITY. The build is per-platform and per-libc: a Linux binary does not run on macOS, and a
# glibc binary does not run on Alpine. Build on the OLDEST system you intend to support — glibc is
# backward compatible, not forward. This build needs glibc 2.14+ (roughly anything since 2011).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
SRC=claude_token_rotate.py
NAME=claude-token-rotate
VENV=.build/venv
MODE=${1:-onefile}

[[ -f $SRC ]] || { echo "$SRC not found beside this script" >&2; exit 1; }
[[ $MODE == onefile || $MODE == onedir ]] || { echo "usage: $0 [onefile|onedir]" >&2; exit 1; }

PY=$(command -v python3 || true)
[[ -n $PY ]] || { echo "python3 is needed to BUILD (never to run the result)" >&2; exit 1; }

if [[ ! -x $VENV/bin/pyinstaller ]]; then
    echo "── preparing build environment (one time, ~60 MB in .build/)"
    "$PY" -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip pyinstaller
fi

echo "── building $MODE with $("$VENV/bin/pyinstaller" --version)"
rm -rf dist .build/work .build/spec
# tkinter/unittest/pydoc/pdb are dead weight here. Nothing else is safe to drop — in particular
# `email` looks unused but urllib imports it, and excluding it fails at RUNTIME, not at build time.
"$VENV/bin/pyinstaller" "--$MODE" --name "$NAME" --strip --noconfirm \
    --exclude-module tkinter --exclude-module unittest \
    --exclude-module pydoc --exclude-module pdb \
    --distpath dist --workpath .build/work --specpath .build/spec \
    "$SRC" >/dev/null

BIN=dist/$NAME
[[ -d $BIN ]] && BIN=dist/$NAME/$NAME

# The point of the exercise is "runs without Python", so prove it rather than assume it: an empty
# environment with only sh and env on PATH, and no interpreter anywhere.
echo "── verifying it starts with no python on PATH"
rm -rf .build/probe && mkdir -p .build/probe/bin
for c in sh env; do ln -sf "$(command -v "$c")" ".build/probe/bin/$c"; done
if env -i PATH="$PWD/.build/probe/bin" HOME="$HOME" "$PWD/$BIN" --help >/dev/null 2>&1; then
    echo "   ok"
else
    echo "   FAILED — the binary does not start in a bare environment" >&2
    exit 1
fi
rm -rf .build/probe

printf '\n%s  (%s)\n' "$BIN" "$(du -sh "$BIN" | cut -f1)"
cat <<'TIP'

  run it from the repo (token.csv sits beside it):
      cp dist/claude-token-rotate . && ./claude-token-rotate

  expose it to zsh and bash (~/.local/bin is on PATH for both on most systems):
      ln -sf "$PWD/claude-token-rotate" ~/.local/bin/claude-token-rotate

  ./build.sh onedir   builds a folder instead — starts instantly, no unpack step
  rm -rf .build       removes the build venv; the next build recreates it
TIP
