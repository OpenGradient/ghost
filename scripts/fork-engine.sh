#!/usr/bin/env bash
# Fork the Hermes engine into a standalone, debranded ghost engine.
#
# Copies the upstream install, relocates the venv to the fork path (incl. console-script
# shebangs, the editable-install finder/.pth, and the dist-info source path), then runs
# the debrand. Result: a self-contained engine at $GHOST_ENGINE that `ghost` runs on --
# your normie `hermes` install is left completely untouched.
set -euo pipefail

SRC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}"
ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${GHOST_PYTHON:-$(command -v python3)}"

[ -d "$SRC" ] || { echo "!! upstream engine not found at $SRC"; exit 1; }

echo "==> forking engine: $SRC -> $ENG"
rm -rf "$ENG"; mkdir -p "$ENG"
rsync -a --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' "$SRC/" "$ENG/"

echo "==> relocating venv paths ($SRC -> $ENG)"
# LC_ALL=C lets sed tolerate the non-UTF8 bytes some venv files contain.
relocate() { [ -f "$1" ] && file "$1" 2>/dev/null | grep -q text && LC_ALL=C sed -i '' "s#$SRC#$ENG#g" "$1" 2>/dev/null || true; }
# console scripts + activate scripts (shebangs were the gotcha)
for f in "$ENG"/venv/bin/*; do relocate "$f"; done
relocate "$ENG/venv/pyvenv.cfg"
# editable-install pointers + dist-info source path (the "Project:" line)
for f in "$ENG"/venv/lib/python*/site-packages/__editable__* \
         "$ENG"/venv/lib/python*/site-packages/hermes_agent-*.dist-info/direct_url.json; do
  relocate "$f"
done
find "$ENG"/venv/lib -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> debranding the fork"
"$PYTHON" "$HERE/debrand.py" "$ENG"

echo "==> verifying the fork launches (expect 'Ghost vX.Y')"
"$ENG/venv/bin/hermes" --version 2>&1 | head -2 || { echo "!! fork failed to launch"; exit 1; }
echo "==> fork ready: $ENG"
