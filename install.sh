#!/usr/bin/env bash
# ghost -- the installer and updater for macOS, Linux and WSL2 (on Windows: install.ps1). Safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/OpenGradient/ghost/main/install.sh | bash
#
# Sets up the engine (the Hermes release pinned in engine.pin, debranded, with its own runtime), the local
# bridge that seals requests to the TEE gateway, and the `ghost` commands. It contacts no model and needs no
# login: ghost asks you to log in the first time you run it. A normie Hermes install is never touched.
#
# Options, as environment variables. `ghost update` remembers them; set one to 0 to turn it off again.
#   GHOST_LOCAL=1        also install Ollama and a local model, for `ghost --local` and as the offline fallback
#   GHOST_LOCAL_32B=1    the stronger 32B local model too (26GB; implies GHOST_LOCAL)
#   GHOST_SCRUB=1        redact your PII and secrets from everything sent (off by default: requests are sealed
#                        to the enclave either way, and ghost works best seeing what you see)
#   GHOST_NO_BROWSER=1   skip Chromium and the computer-use driver (a smaller, faster install)
#   GHOST_VERBOSE=1      show everything as it runs, instead of keeping it in ~/.ghost/install.log
set -euo pipefail

main() {
  OS="$(uname -s)"
  case "$OS" in
    Darwin|Linux) ;;
    *) echo "ghost runs on macOS, Linux and WSL2 (and on Windows with install.ps1). This is: $OS." >&2; exit 1 ;;
  esac

  # Run as `curl ... | bash` there is no checkout: get one, and carry on from there.
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -z "$REPO" ] || [ ! -f "$REPO/profile/config.yaml" ]; then
    need_git
    SRC="${GHOST_SRC_DIR:-$HOME/.ghost-src}"
    if [ ! -d "$SRC/.git" ]; then
      rm -rf "$SRC"; git clone --quiet https://github.com/OpenGradient/ghost.git "$SRC"
    else   # always the released branch, whatever was checked out before
      git -C "$SRC" fetch --quiet origin main
      git -C "$SRC" checkout --quiet main && git -C "$SRC" merge --quiet --ff-only origin/main || {
        echo "Your copy of ghost in $SRC has changes of yours in it. Set them aside (git -C $SRC stash) and run this again." >&2
        exit 1
      }
    fi
    exec bash "$SRC/install.sh" "$@"
  fi

  GHOST_HOME="${GHOST_HOME:-$HOME/.ghost}"
  ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
  PROFILE="$GHOST_HOME/profiles/uncensored"
  PRIV="$GHOST_HOME/privacy"
  BIN="$HOME/.local/bin"
  PYTHON="$GHOST_HOME/venv/bin/python"
  LOG="$GHOST_HOME/install.log"
  HERMES_VERSION=""; . "$REPO/engine.pin"
  LOCAL_MODEL="" NOTES=()
  mkdir -p "$GHOST_HOME"; : > "$LOG"
  options
  ui

  printf '\n  %s👻 ghost%s\n\n' "$BOLD" "$OFF" >&3
  step "Checking this machine"                                   check_machine
  step "Installing the privacy bridge"                           install_bridge
  step "Building the engine (Hermes $HERMES_VERSION, a few minutes)" build_engine
  [ -z "$GHOST_LOCAL" ] || step "Getting the local model"        local_model
  step "Writing your profile"                                    write_profile
  step "Starting the background service"                         start_service
  step "Installing the ghost command"                            install_commands
  finish
}

# ── options ───────────────────────────────────────────────────────────────────
options() {
  local name
  for name in GHOST_LOCAL GHOST_LOCAL_32B GHOST_SCRUB GHOST_NO_BROWSER; do
    local value="${!name-$(sed -n "s/^$name=//p" "$GHOST_HOME/.install-env" 2>/dev/null | tail -1)}"
    case "$value" in ""|0) value="" ;; *) value=1 ;; esac
    printf -v "$name" '%s' "$value"
    export "${name?}"
  done
  [ -z "$GHOST_LOCAL_32B" ] || GHOST_LOCAL=1
}

remember() {   # what `ghost update` and `ghost uninstall` need to know
  local name
  echo "$REPO" > "$GHOST_HOME/.src"
  for name in GHOST_LOCAL GHOST_LOCAL_32B GHOST_SCRUB GHOST_NO_BROWSER; do
    [ -z "${!name}" ] || echo "$name=1"
  done > "$GHOST_HOME/.install-env"
}

# ── what you see ──────────────────────────────────────────────────────────────
# Each step's own output goes to the log; on a failure the end of it is shown.
ui() {
  exec 3>&1
  BOLD="" DIM="" GOLD="" GREEN="" RED="" OFF="" FANCY="" STEP="" SPIN="" STARTED=0
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
    FANCY=1 BOLD=$'\033[1m' DIM=$'\033[2m' GOLD=$'\033[38;5;220m' GREEN=$'\033[32m' RED=$'\033[31m' OFF=$'\033[0m'
  fi
  trap on_exit EXIT
  trap 'exit 130' INT TERM
}

step() {   # step "what it does" command...
  STEP="$1"; shift
  STARTED=$SECONDS
  if [ -n "${GHOST_VERBOSE:-}" ]; then
    printf '\n%s==>%s %s\n' "$GOLD" "$OFF" "$STEP" >&3
    "$@" </dev/null
  else
    printf '\n==> %s\n' "$STEP" >> "$LOG"
    spin
    "$@" </dev/null >> "$LOG" 2>&1
    unspin
  fi
  printf '  %s✓%s %s%s\n' "$GREEN" "$OFF" "$STEP" "$(took)" >&3
  STEP=""
}

took() { local s=$((SECONDS - STARTED)); [ "$s" -lt 5 ] || printf ' %s%ss%s' "$DIM" "$s" "$OFF"; }

spin() {
  [ -n "$FANCY" ] || return 0
  ( trap - EXIT INT TERM
    frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏) i=0
    while kill -0 $$ 2>/dev/null; do
      printf '\r\033[2K  %s%s%s %s%s' "$GOLD" "${frames[i % 10]}" "$OFF" "$STEP" "$(took)" >&3
      i=$((i + 1)); sleep 0.1
    done ) &
  SPIN=$!
}

unspin() {
  [ -n "$SPIN" ] || return 0
  kill "$SPIN" 2>/dev/null || true
  wait "$SPIN" 2>/dev/null || true
  SPIN=""
  printf '\r\033[2K' >&3
}

die() {   # a failure we can explain: no need to show the log
  unspin
  printf '  %s✗%s %s\n\n' "$RED" "$OFF" "$1" >&3
  STEP=""
  exit 1
}

on_exit() {
  local code=$?
  unspin
  [ "$code" -ne 0 ] && [ -n "$STEP" ] || return 0
  printf '  %s✗%s %s\n\n' "$RED" "$OFF" "$STEP" >&3
  [ "$code" -ne 130 ] || { printf '  Stopped. Run the installer again to carry on.\n\n' >&3; return 0; }
  if [ -z "${GHOST_VERBOSE:-}" ]; then
    tail -n 15 "$LOG" | sed 's/^/      /' >&3
    printf '\n  The whole log is in %s\n' "$LOG" >&3
  fi
  printf '  Nothing is half-installed: run the installer again once that is fixed.\n\n' >&3
}

# ── steps ─────────────────────────────────────────────────────────────────────
have() { command -v "$1" >/dev/null 2>&1; }

need_git() {   # (on a Mac without the command line tools, `git` exists but only offers to install them)
  git --version >/dev/null 2>&1 && return
  local how="install it with your package manager, then run this again"
  [ "$(uname -s)" != Darwin ] || how="finish the install macOS is offering (or run: xcode-select --install), then run this again"
  if declare -F die >/dev/null && [ -n "${LOG:-}" ]; then die "ghost needs git: $how."; fi
  echo "ghost needs git: $how." >&2; exit 1
}

check_machine() {
  need_git
  have curl || die "ghost needs curl: install it with your package manager, then run this again."
  local open
  open="$(GHOST_ENGINE="$ENG" GHOST_HOME="$GHOST_HOME" bash "$REPO/scripts/fork-engine.sh" --check 2>&1)" || die "${open#!! }"
  if ! have uv; then   # it brings the bridge's own Python, so the system's never matters
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  fi
  have uv || die "couldn't install uv (https://docs.astral.sh/uv/getting-started/installation/). Install it, then run this again."
  if [ -n "$GHOST_LOCAL" ] && ! have ollama; then
    ! have brew || brew install --cask ollama || true
    have ollama || die "GHOST_LOCAL needs Ollama: install it from https://ollama.com (or drop GHOST_LOCAL), then run this again."
  fi
  remember
}

install_bridge() {
  local extra=()
  [ -z "$GHOST_SCRUB" ] || extra=(--extra presidio)
  ( cd "$REPO" && UV_PROJECT_ENVIRONMENT="$GHOST_HOME/venv" uv sync --python 3.11 --frozen ${extra[@]+"${extra[@]}"} )
  mkdir -p "$PRIV"
  rm -f "$PRIV/ensure_scrubber_route.py" "$PRIV/.proxy" "$PRIV/.no_scrub" "$PRIV/.pass_paths" "$PRIV/.presidio"   # older releases'
  cp "$REPO"/privacy/*.py "$PRIV/"
  cp "$REPO/profile/uncensored_prefill.json" "$PRIV/"
  [ -f "$PRIV/pii_denylist.txt" ] || cp "$REPO/profile/pii_denylist.example.txt" "$PRIV/pii_denylist.txt"
  if [ -z "$GHOST_SCRUB" ]; then rm -f "$PRIV/.scrub"; return; fi
  : > "$PRIV/.scrub"
  "$PYTHON" -c "import en_core_web_md" 2>/dev/null || "$PYTHON" -m spacy download en_core_web_md
  : > "$PRIV/.presidio"
  NOTES+=("Redaction is on. Add your own terms to $PRIV/pii_denylist.txt")
}

build_engine() {
  GHOST_PYTHON="$PYTHON" GHOST_ENGINE="$ENG" GHOST_HOME="$GHOST_HOME" bash "$REPO/scripts/fork-engine.sh"
}

local_model() {
  if [ "$OS" = Darwin ]; then pgrep -xq ollama || open -a Ollama 2>/dev/null || true
  else pgrep -x ollama >/dev/null || (nohup ollama serve >/dev/null 2>&1 &) || true; fi
  sleep 1
  local src alias need modelfile
  while IFS=$'\t' read -r src alias need; do
    case "$src" in \#*|"") continue ;; esac
    [ "$need" != optional ] || [ -n "$GHOST_LOCAL_32B" ] || continue
    ollama show "$alias" >/dev/null 2>&1 || { ollama pull "$src"; ollama cp "$src" "$alias"; }
    # The engine refuses a window under 64K for tool use, and Ollama's own default is far smaller.
    if ! ollama show --parameters "$alias" 2>/dev/null | grep -q '^num_ctx'; then
      modelfile="$(mktemp)"; printf 'FROM %s\nPARAMETER num_ctx 65536\n' "$alias" > "$modelfile"
      ollama create "$alias" -f "$modelfile"; rm -f "$modelfile"
    fi
  done < "$REPO/models.txt"
  LOCAL_MODEL="ghost-tool:latest"
  ! ollama show uncensored-local >/dev/null 2>&1 || LOCAL_MODEL="uncensored-local:latest"
}

write_profile() {
  local kept line
  kept="$("$PYTHON" "$REPO/scripts/write_profile.py" "$PROFILE" --ghost-home "$GHOST_HOME" \
    ${LOCAL_MODEL:+--local-model "$LOCAL_MODEL"} ${GHOST_SCRUB:+--scrub})"
  while IFS= read -r line; do [ -z "$line" ] || NOTES+=("${line/#your/Your}"); done <<< "$kept"
  "$PYTHON" "$REPO/scripts/migrate_skills.py" "$GHOST_HOME" "$PROFILE"
}

free_port() {   # stopping a service finishes in its own time, and two bridges can't share the port
  local _
  for _ in $(seq 1 50); do
    curl -s -o /dev/null --max-time 1 http://127.0.0.1:8788/healthz || return 0
    sleep 0.2
  done
}

fill() { sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__GHOST_HOME__#$GHOST_HOME#g" -e "s#__ENG__#$ENG#g" "$1"; }

# The bridge runs in the background and restarts by itself: launchd on macOS, systemd on Linux. Where neither
# is available (some containers, WSL without systemd) `ghost` starts it when it's needed.
start_service() {
  local label unit
  if [ "$OS" = Darwin ]; then
    local agents="$HOME/Library/LaunchAgents" domain; domain="gui/$(id -u)"
    mkdir -p "$agents"
    for label in com.advait.hermes-pii-scrubber com.advait.hermes-veil ai.dreamfort.ghost; do   # (the first two: older releases')
      [ -f "$agents/$label.plist" ] || continue
      launchctl bootout "$domain/$label" 2>/dev/null || launchctl unload "$agents/$label.plist" 2>/dev/null || true
      rm -f "$agents/$label.plist"
    done
    free_port
    fill "$REPO/launchd/ai.dreamfort.ghost.plist" > "$agents/ai.dreamfort.ghost.plist"
    for _ in 1 2 3 4 5; do
      ! launchctl bootstrap "$domain" "$agents/ai.dreamfort.ghost.plist" || break
      sleep 1
    done
  elif have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
    local units="$HOME/.config/systemd/user"
    mkdir -p "$units"
    for unit in ghost-scrubber ghost-veil; do   # older releases'
      [ -f "$units/$unit.service" ] || continue
      systemctl --user disable --now "$unit.service" || true
      rm -f "$units/$unit.service"
    done
    fill "$REPO/systemd/ghost-bridge.service" > "$units/ghost-bridge.service"
    systemctl --user daemon-reload
    systemctl --user enable ghost-bridge.service
    systemctl --user restart ghost-bridge.service
    loginctl enable-linger "$(id -un)" || true   # so it's there after a reboot, before you log in
  else
    pkill -f "$PRIV/scrubbing_proxy.py" || true
    pkill -f "veil serve --foreground --skip-setup --port 11435" || true   # older releases'
    free_port
  fi
  "$PYTHON" -Es -c 'import sys; sys.path.insert(0, sys.argv[1]); import launch; sys.exit(not launch.ensure_bridge())' "$PRIV" \
    || die "the background service didn't start. Its log is $PRIV/bridge.log"
}

install_commands() {
  local name
  mkdir -p "$BIN"
  for name in ghost ghost-login ghost-update ghost-uninstall; do
    fill "$REPO/bin/$name" > "$BIN/.$name.new"
    chmod +x "$BIN/.$name.new"
    mv -f "$BIN/.$name.new" "$BIN/$name"   # in one move: `ghost update` is running from one of these
  done
  GHOST_ENGINE="$ENG" GHOST_HOME="$GHOST_HOME" bash "$REPO/scripts/fork-engine.sh" --refresh-gateway
}

finish() {
  local who note rc
  printf '\n' >&3
  for note in ${NOTES[@]+"${NOTES[@]}"}; do printf '  %s%s%s\n' "$DIM" "$note" "$OFF" >&3; done
  [ "${#NOTES[@]}" -eq 0 ] || printf '\n' >&3
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) if (cd "$HOME" && grep -qs '\.local/bin' .zshrc .zprofile .zshenv .bashrc .bash_profile .profile); then
         printf '  Open a new terminal first, so that it finds ~/.local/bin.\n\n' >&3
       else
         case "${SHELL:-}" in */zsh) rc="~/.zshrc" ;; */bash) rc="~/.bashrc" ;; *) rc="~/.profile" ;; esac
         printf '  First put ~/.local/bin on your PATH, then open a new terminal:\n\n' >&3
         printf '      echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> %s\n\n' "$rc" >&3
       fi ;;
  esac
  if who="$("$BIN/ghost-login" --status 2>/dev/null)"; then printf '  %sghost is ready.%s %s.\n\n' "$BOLD" "$OFF" "$who" >&3
  else printf '  %sghost is ready.%s It asks you to log in the first time.\n\n' "$BOLD" "$OFF" >&3; fi
  printf '      %sghost%s\n\n' "$GOLD" "$OFF" >&3
}

main "$@"
