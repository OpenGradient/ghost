#!/usr/bin/env bash
# Build ghost's engine from the upstream Hermes release pinned in ../engine.pin.
#
# Fetches that exact commit, debrands it, seals it (a sealed engine never tries to update itself),
# and installs its runtime -- Python, Node, ripgrep, the locked dependencies -- under ~/.ghost with
# the engine's own package manager. Result: a self-contained engine that `ghost` runs on. A normie
# `hermes` install is neither needed nor touched.
#
#   fork-engine.sh                    build the engine, unless the one there is already this build
#                                     (the previous one is restored if it fails)
#   fork-engine.sh --check            fail if ghost is open, so nothing changes under a live session
#   fork-engine.sh --refresh-gateway  point an installed background gateway at this engine
#
# Env (all optional):
#   GHOST_ENGINE=       where the engine goes             (default ~/.ghost-engine)
#   GHOST_HOME=         ghost's state + managed runtime   (default ~/.ghost)
#   GHOST_PYTHON=       a Python 3.9+ to run debrand.py   (default python3)
#   GHOST_NO_BROWSER=1  skip Chromium and the computer-use driver
#   GHOST_FORCE=1       build the engine even if it's current, and even while ghost is open
#   HERMES_SRC=         build from a local Hermes tree instead of the pin (development)
set -euo pipefail

ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
GHOST_HOME="${GHOST_HOME:-$HOME/.ghost}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${GHOST_PYTHON:-$(command -v python3 || true)}"
REPO_URL="https://github.com/NousResearch/hermes-agent.git"
NEW="$ENG.new"; OLD="$ENG.old"
LAUNCH="$ENG/.hermes/bin/hermes"
RESTART="$GHOST_HOME/.restart-gateway"   # present while a gateway this script stopped is owed a start
REFRESH="$GHOST_HOME/.refresh-gateway"   # present while an installed gateway still points at the previous engine
# The background gateway, when it's installed as a service. It's wanted when it runs, and when it's waiting to: it keeps
# trying for as long as a chat's own gateway is in its way.
case "$(uname -s)" in
  Darwin) UNIT="$HOME/Library/LaunchAgents/ai.hermes.gateway-uncensored.plist"
          service_wanted() {
            case "$(launchctl list ai.hermes.gateway-uncensored 2>/dev/null || true)" in
              *'"PID" = '*) ;; *'"LastExitStatus" = 0;'*) false ;; *'"LastExitStatus" = '*) ;; *) false ;;
            esac
          } ;;
  *)      UNIT="$HOME/.config/systemd/user/hermes-gateway-uncensored.service"
          service_wanted() {
            case "$(systemctl --user is-active hermes-gateway-uncensored.service 2>/dev/null || true)" in
              active|activating|reloading) ;; *) false ;;
            esac
          } ;;
esac
# Ours, whichever engine it runs: an old ghost on Linux ran its gateway from ~/.hermes, with ghost's state.
service_installed() { [ -f "$UNIT" ] && grep -qF -e "$ENG/" -e "$GHOST_HOME/" "$UNIT"; }

# The engine's package manager must see only its own toolchain: install.sh exports
# UV_PROJECT_ENVIRONMENT for ghost's privacy venv, and uv would happily sync the engine into it.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_NAMESPACE   # some other repository's
unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV PYTHONHOME PYTHONPATH HERMES_RUNTIME_DIR HERMES_INSTALL_DIR \
      HERMES_INSTALL_ROOT HERMES_REVISION HERMES_PROFILE

HERMES_VERSION="" HERMES_COMMIT=""
. "$HERE/../engine.pin"
[ -n "$HERMES_VERSION" ] && [ -n "$HERMES_COMMIT" ] || { echo "!! engine.pin is missing HERMES_VERSION / HERMES_COMMIT" >&2; exit 1; }

# Is ghost open on this engine? A process keeps importing from the engine's path for as long as it
# runs, so swapping the tree underneath one would mix two releases in it. The background gateway
# doesn't count: it is ours to stop and start again.
processes() {   # each one's pid, parent and command (minimal systems have no `ps`)
  if command -v ps >/dev/null 2>&1; then ps axww -o pid=,ppid=,command=; return; fi
  local dir parent
  for dir in /proc/[0-9]*; do
    parent="$(sed -n 's/^PPid:[^0-9]*//p' "$dir/status" 2>/dev/null || true)"
    printf '%s %s %s\n' "${dir#/proc/}" "${parent:-0}" "$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null)"
  done
}
check_closed() {
  [ -d "$ENG" ] && [ -z "${GHOST_FORCE:-}" ] || return 0
  # Launched by path (through a link, perhaps), or as `python -c "... '<engine>' ..."`. What the gateway started isn't a
  # window either, nor is the reaper that goes with every engine process.
  local pids
  pids="$( (processes 2>/dev/null || true) | awk -v e="$ENG" -v r="$(cd "$ENG" && pwd -P)" -v h="$GHOST_HOME/tools/" -v hr="$(cd "$GHOST_HOME" 2>/dev/null && pwd -P)/tools/" '
    { parent[$1] = $2; engine[$1] = index($0, e "/") || index($0, e "\047") || index($0, r "/") || index($0, r "\047")
      gateway[$1] = (engine[$1] || index($0, h) || index($0, hr)) && /gateway run/; reaper[$1] = /process_supervisor[.]sh/ }
    END {
      for (p in engine) {
        if (!engine[p] || reaper[p]) continue
        q = p; hops = 0
        while ((q in parent) && !gateway[q] && hops++ < 64) q = parent[q]
        if (!gateway[q]) print p
      }
    }')"
  [ -z "$pids" ] && return 0
  echo "!! ghost is open (pid $(echo $pids)). Close those windows and re-run, or set GHOST_FORCE=1." >&2
  return 1
}

# What this build is made from. The engine there is current if it was made from the same, and still launches.
RECIPE="$(cat "$HERE/../engine.pin" "$HERE/debrand.py" "$HERE/fork-engine.sh" | git hash-object --stdin)${GHOST_NO_BROWSER:+-no-browser}"
launches() { HERMES_HOME="$GHOST_HOME/profiles/uncensored" "$LAUNCH" --version </dev/null; }
current() {
  [ -z "${GHOST_FORCE:-}${HERMES_SRC:-}" ] && [ "$(cat "$ENG/.ghost-recipe" 2>/dev/null)" = "$RECIPE" ] && launches >/dev/null 2>&1
}

case "${1:-}" in
  --check) check_closed; exit ;;
  --refresh-gateway)
    [ -e "$REFRESH" ] || exit 0
    service_installed || { rm -f "$REFRESH"; exit 0; }
    echo "==> moving the background gateway onto the new engine"
    start=--no-start-now; [ ! -e "$RESTART" ] || start=--start-now   # one you had stopped stays stopped
    if HERMES_HOME="$GHOST_HOME" "$LAUNCH" -p uncensored gateway install --force "$start" --start-on-login </dev/null
    then rm -f "$RESTART" "$REFRESH"
    else echo "   (could not refresh it -- run: ghost gateway install --force)"; fi
    exit 0 ;;
  "") ;;
  *) echo "usage: fork-engine.sh [--check | --refresh-gateway]" >&2; exit 2 ;;
esac
# A build that was killed part-way left the engine it was replacing set aside: put that one back first.
ours() { [ -f "$1/install-stamp.json" ] || [ -d "$1/hermes_cli" ] || [ -e "$1/venv/bin/hermes" ]; }
if [ -d "$OLD" ] && ours "$OLD"; then
  if ! launches >/dev/null 2>&1; then rm -rf "$ENG"; mv "$OLD" "$ENG"; else rm -rf "$OLD"; fi
fi
if current; then echo "==> the engine is already this build: $ENG"; exit 0; fi
if [ -e "$ENG" ] && ! ours "$ENG" && [ -n "$(ls -A "$ENG" 2>/dev/null)" ]; then
  echo "!! $ENG is there already, and it isn't an engine ghost built. Move it away (or set GHOST_ENGINE) and run this again." >&2
  exit 1
fi
check_closed

# ---------- 1. fetch ----------
trap 'exit 130' INT TERM HUP   # so that the traps below run, whatever stops this
trap 'rm -rf "$NEW"' EXIT   # a build that stops here leaves the current engine untouched
rm -rf "$NEW"; mkdir -p "$(dirname "$NEW")" "$GHOST_HOME"
if [ -n "${HERMES_SRC:-}" ]; then
  [ -d "$HERMES_SRC" ] || { echo "!! HERMES_SRC=$HERMES_SRC not found" >&2; exit 1; }
  echo "==> copying Hermes from $HERMES_SRC"
  mkdir -p "$NEW"
  tar -C "$HERMES_SRC" --exclude=./.git --exclude=node_modules --exclude=./venv --exclude=./.venv \
      --exclude=__pycache__ -cf - . | tar -C "$NEW" -xf -
else
  echo "==> fetching Hermes v$HERMES_VERSION"
  for attempt in 1 2 3; do
    git -c advice.detachedHead=false clone --quiet --depth 1 --branch "v$HERMES_VERSION" "$REPO_URL" "$NEW" && break
    rm -rf "$NEW"
    [ "$attempt" = 3 ] && { echo "!! could not fetch $REPO_URL" >&2; exit 1; }
    sleep $((attempt * 3))
  done
  GOT="$(git -C "$NEW" rev-parse HEAD)"
  if [ "$GOT" != "$HERMES_COMMIT" ]; then
    echo "!! tag v$HERMES_VERSION is commit $GOT, but engine.pin expects $HERMES_COMMIT -- refusing it" >&2
    exit 1
  fi
fi
# Development-only trees the engine never loads (~150MB), and the git history a sealed tree must not have.
for d in .git .github tests tests-js evals website contributors; do rm -rf "${NEW:?}/$d"; done

# ---------- 2. debrand + seal ----------
echo "==> debranding"
"$PYTHON" "$HERE/debrand.py" "$NEW"
# No .git plus this stamp is the engine's own "a packager owns this tree" contract: it skips every
# self-update, launch-time repair and update-check path, and `hermes update` refuses.
cat > "$NEW/install-stamp.json" <<STAMP
{
  "schemaVersion": 2,
  "commit": "$HERMES_COMMIT",
  "commitDate": null,
  "branch": null,
  "builtAt": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)",
  "dirty": false,
  "source": "local",
  "distribution": "ghost",
  "updateMechanism": "external",
  "baseVersion": "$HERMES_VERSION",
  "displayVersion": "$HERMES_VERSION",
  "distance": 0,
  "payload": "bootstrap",
  "tag": null
}
STAMP

# ---------- 3. stop the background gateway, then swap the trees ----------
check_closed   # (that took a while)
OLD_BIN=""
for bin in "$LAUNCH" "$ENG/venv/bin/hermes"; do [ -x "$bin" ] && { OLD_BIN="$bin"; break; }; done
old_engine() { HERMES_HOME="$GHOST_HOME" "$OLD_BIN" -p uncensored "$@" </dev/null >/dev/null 2>&1; }
if service_installed && service_wanted; then : > "$RESTART"; fi
# (--yes: it won't stop in the middle of something, a cron job say, without being told. Old engines don't know the word.)
[ -z "$OLD_BIN" ] || old_engine gateway stop --yes || old_engine gateway stop || true

rollback() {
  [ $? -eq 0 ] && return
  rm -rf "$NEW"
  [ -d "$OLD" ] || return 0
  rm -rf "$ENG"; mv "$OLD" "$ENG"
  echo "!! engine build failed -- restored the previous engine" >&2
  if [ -e "$RESTART" ]; then old_engine gateway start || true; rm -f "$RESTART"; fi
}
trap rollback EXIT
[ ! -d "$ENG" ] || mv "$ENG" "$OLD"
mv "$NEW" "$ENG"

# ---------- 4. runtime + launcher ----------
echo "==> installing the engine runtime (Python, Node, ripgrep, locked dependencies)"
flags=(--dir "$ENG" --hermes-home "$GHOST_HOME" --stage python-deps --non-interactive)
[ -z "${GHOST_NO_BROWSER:-}" ] || flags+=(--skip-browser --skip-computer-use)
HERMES_HOME="$GHOST_HOME" bash "$ENG/scripts/install.sh" "${flags[@]}"

STORE_PY="$(ls -d "$GHOST_HOME"/tools/python-*/bin/python3 2>/dev/null | sort | tail -1)"
[ -x "$STORE_PY" ] || { echo "!! the engine's Python was not installed under $GHOST_HOME/tools" >&2; exit 1; }
pm() { (cd "$ENG" && HERMES_HOME="$GHOST_HOME" "$STORE_PY" -m pm.cli "$@" </dev/null); }
# It remembers that you did without these, until they're asked for by name.
if [ -z "${GHOST_NO_BROWSER:-}" ] && grep -qs chromium "$GHOST_HOME"/installs/*/declined-packages.json; then pm install chromium cua-driver; fi
# Web search is on by default in ghost, so its backend ships rather than installing mid-conversation.
pm install --extra ddgs >/dev/null

HERMES_HOME="$GHOST_HOME" "$STORE_PY" -I - "$ENG" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from hermes_cli._launchers import ensure_install_launchers
sys.exit(0 if ensure_install_launchers(root, root / ".hermes" / "bin") else "could not write the engine launcher")
PY

echo "==> verifying the engine launches"
VERSION="$(launches)"; VERSION="${VERSION%%$'\n'*}"
echo "    $VERSION"
case "$VERSION" in
  "Ghost v$HERMES_VERSION"*) ;;
  *) echo "!! expected 'Ghost v$HERMES_VERSION'" >&2; exit 1 ;;
esac

trap - EXIT
: > "$REFRESH"
[ -n "${HERMES_SRC:-}" ] || echo "$RECIPE" > "$ENG/.ghost-recipe"
rm -rf "$OLD"
pm gc >/dev/null 2>&1 || true   # drop dependency generations nothing uses any more
echo "==> engine ready: $ENG"
