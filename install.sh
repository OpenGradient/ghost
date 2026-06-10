#!/usr/bin/env bash
# ghost -- one-command installer for the incognito, uncensored agentic harness.
#
#   ./install.sh
#
# Installs EVERYTHING, idempotently (safe to re-run): Ollama, the Hermes engine, the local
# uncensored models, the forked + debranded engine, the privacy stack, and the `ghost` +
# `ghost-login` commands.
#
# Hosted (non-local) models run through ghost's local OHTTP bridge to the OpenGradient
# chat-api TEE gateway -- the same oblivious-HTTP + enclave path the chat.opengradient.ai
# website uses. After install, run `ghost-login` once to connect your account (a browser
# login that hands a session token back to your machine).
#
# Optional config via env (all optional -- plain `./install.sh` does the full private setup):
#   GHOST_DIRECT=1       skip the Webshare rotating proxy + personal PII denylist; the OHTTP
#                        bridge still runs and talks to chat-api directly (for a shared box)
#   GHOST_NO_LOCAL=1     skip Ollama + all local models (hosted-only, lightest)
#   GHOST_LOCAL_32B=1    also pull the stronger 32B local model (26GB)
#   GHOST_CHAT_APP_URL=  override the website used for `ghost-login` (default chat.opengradient.ai)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_HOME="${ENGINE_HOME:-$HOME/.hermes}"   # where the Hermes engine installs (official installer default)
GHOST_HOME="${GHOST_HOME:-$HOME/.ghost}"      # ghost's ISOLATED state (profiles, privacy, auth)
PROFILE="$GHOST_HOME/profiles/uncensored"
PRIV="$GHOST_HOME/privacy"
LA="$HOME/Library/LaunchAgents"
ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
PYTHON="${GHOST_PYTHON:-$(command -v python3 || true)}"
SCRUBBER="http://127.0.0.1:8788"
DIRECT="${GHOST_DIRECT:-}"
NO_LOCAL="${GHOST_NO_LOCAL:-}"   # skip Ollama + all local models (hosted-only)

say(){ printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }

# ---------- 0. dependencies (auto-installed) ----------
say "Dependencies"
[ -n "$PYTHON" ] || { echo "!! need python3 (3.11+); install it and re-run."; exit 1; }

if [ -z "$NO_LOCAL" ]; then
  if ! have ollama && have brew; then echo "   installing Ollama (brew --cask)"; brew install --cask ollama || true; fi
  have ollama || { echo "!! Install Ollama from https://ollama.com (or set GHOST_NO_LOCAL=1 for hosted-only) then re-run."; exit 1; }
  pgrep -xq ollama || open -a Ollama 2>/dev/null || true ; sleep 1
fi

if [ ! -d "$ENGINE_HOME/hermes-agent" ] && ! have hermes; then
  say "Installing the Hermes Agent engine (official one-liner)"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash \
    || { echo "!! engine install failed; install manually (https://hermes-agent.nousresearch.com) then re-run."; exit 1; }
fi
SRC="$ENGINE_HOME/hermes-agent"; [ -d "$SRC" ] || SRC="$(cd "$(dirname "$(command -v hermes)")/.." 2>/dev/null && pwd)"
[ -d "$SRC" ] || { echo "!! can't locate the Hermes engine to fork."; exit 1; }

# Privacy-stack deps for the OHTTP bridge: httpx (HTTP), cryptography (HPKE +
# RSA-PSS verify), web3 (on-chain TEE registry read + keccak).
say "Privacy-stack Python deps (httpx, cryptography, web3)"
"$PYTHON" -c "import httpx, cryptography, web3" 2>/dev/null \
  || "$PYTHON" -m pip install -q --upgrade httpx cryptography web3

# ---------- 1. local models (skipped with GHOST_NO_LOCAL; 32B optional via GHOST_LOCAL_32B) ----------
LOCAL_MODEL="ghost-tool:latest"
if [ -n "$NO_LOCAL" ]; then
  say "GHOST_NO_LOCAL -- skipping Ollama + all local models (hosted-only)"
else
  say "Local models"
  while IFS=$'\t' read -r src alias opt; do
    case "$src" in \#*|"") continue;; esac
    if [ "$opt" = "optional" ] && [ -z "${GHOST_LOCAL_32B:-}" ]; then
      echo "   skipping optional $alias (26GB) -- set GHOST_LOCAL_32B=1 to include the stronger local model"; continue
    fi
    if ollama show "$alias" >/dev/null 2>&1; then echo "   $alias present"; continue; fi
    echo "   $src  ->  $alias"; ollama pull "$src"; ollama cp "$src" "$alias"
  done < "$REPO/models.txt"
  if ollama show uncensored-local >/dev/null 2>&1; then LOCAL_MODEL="uncensored-local:latest"; else LOCAL_MODEL="ghost-tool:latest"; fi
  echo "   local model = $LOCAL_MODEL"
fi

# ---------- 2. uncensored profile ----------
say "Writing the uncensored profile"
mkdir -p "$PROFILE"
sed -e "s#__HOME__#$HOME#g" -e "s#__LOCAL_MODEL__#$LOCAL_MODEL#g" "$REPO/profile/config.yaml" > "$PROFILE/config.yaml"
cp "$REPO/profile/SOUL.md" "$PROFILE/SOUL.md"
[ -f "$PROFILE/.env" ] || cp "$REPO/profile/.env.example" "$PROFILE/.env"
if [ -n "$NO_LOCAL" ]; then   # no local model -> route auxiliary + fallback to a hosted model via the OHTTP bridge
  "$PYTHON" - "$PROFILE/config.yaml" <<'PYEOF'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"provider: ollama-local\n(\s*)model: \S+",
           r"provider: opengradient\n\1model: nous/hermes-4-70b", s)
open(p, "w").write(s); print("   no-local: auxiliary + fallback routed to hosted nous/hermes-4-70b (via OHTTP bridge)")
PYEOF
fi

# ---------- 3. privacy stack (OHTTP bridge always; rotating proxy unless GHOST_DIRECT) ----------
say "Privacy stack (OHTTP bridge + PII/secret scrubber + rotating proxy)"
mkdir -p "$PRIV/searxng"
cp "$REPO"/privacy/*.py "$PRIV/"
[ -f "$PRIV/pii_denylist.txt" ] || cp "$REPO/profile/pii_denylist.example.txt" "$PRIV/pii_denylist.txt"
cp "$REPO/profile/uncensored_prefill.json" "$PRIV/uncensored_prefill.json"
mkdir -p "$LA"

if [ -z "$DIRECT" ]; then
  if [ ! -s "$GHOST_HOME/webshare_proxies.txt" ]; then
    echo "   Paste your Webshare proxy-list download URL (ip:port:user:pass), or Enter to skip:"
    read -r WS_URL || true
    [ -n "${WS_URL:-}" ] && curl -fsSL "$WS_URL" -o "$GHOST_HOME/webshare_proxies.txt" && echo "   $(wc -l <"$GHOST_HOME/webshare_proxies.txt"|tr -d ' ') proxies"
  fi
  rm -f "$GHOST_HOME/.ghost-direct"
  SERVICES="hermes-proxy hermes-pii-scrubber"
else
  say "GHOST_DIRECT set -- the OHTTP bridge will talk to chat-api directly (no Webshare rotation, no personal denylist)"
  : > "$GHOST_HOME/.ghost-direct"   # marker -> the bridge skips the rotating proxy
  SERVICES="hermes-pii-scrubber"
fi

for svc in $SERVICES; do
  sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" "$REPO/launchd/com.advait.$svc.plist" > "$LA/com.advait.$svc.plist"
  launchctl unload "$LA/com.advait.$svc.plist" 2>/dev/null || true
  launchctl load -w "$LA/com.advait.$svc.plist"
done
printf "   waiting for the OHTTP bridge"
for _ in $(seq 1 15); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$SCRUBBER/healthz" 2>/dev/null)" = 200 ] && break; printf "."; sleep 1; done; echo " up"

# ---------- 4. fork + debrand the engine ----------
say "Forking + debranding the engine -> $ENG"
GHOST_PYTHON="$PYTHON" GHOST_ENGINE="$ENG" HERMES_SRC="$SRC" bash "$REPO/scripts/fork-engine.sh"

# ---------- 5. the ghost + ghost-login commands ----------
say "Installing the ghost + ghost-login commands"
mkdir -p "$HOME/.local/bin"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" -e "s#__ENG__#$ENG#g" -e "s#__GHOST_HOME__#$GHOST_HOME#g" "$REPO/bin/ghost" > "$HOME/.local/bin/ghost"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" -e "s#__GHOST_HOME__#$GHOST_HOME#g" "$REPO/bin/ghost-login" > "$HOME/.local/bin/ghost-login"
chmod +x "$HOME/.local/bin/ghost" "$HOME/.local/bin/ghost-login"

# ---------- 6. connect your account (hosted models) ----------
say "Connect your OpenGradient Chat account (for hosted models)"
if "$PYTHON" "$PRIV/chat_login.py" --status >/dev/null 2>&1; then
  echo "   already connected: $("$PYTHON" "$PRIV/chat_login.py" --status)"
else
  echo "   Hosted models (the default Hermes 405B + Claude/GPT/Gemini/Grok) need a one-time login."
  if [ -t 0 ]; then
    printf "   Run the browser login now? [Y/n] "; read -r ANS || true
    case "${ANS:-Y}" in [Nn]*) echo "   Skipped -- run 'ghost-login' anytime.";; *) GHOST_CHAT_APP_URL="${GHOST_CHAT_APP_URL:-}" "$PYTHON" "$PRIV/chat_login.py" || echo "   (login skipped/failed -- run 'ghost-login' anytime)";; esac
  else
    echo "   Non-interactive install -- run 'ghost-login' once you're done."
  fi
fi

# ---------- 7. smoke test ----------
say "Smoke test"
"$HOME/.local/bin/ghost" --yolo -z "Reply with one word: hi" 2>&1 | tail -2 || true

say "ghost installed -- run:  ghost"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "   (add ~/.local/bin to your PATH first)";; esac
echo "   Hosted default = nous/hermes-4-405b via the OpenGradient TEE gateway (OHTTP-private)."
echo "   Inside ghost, /model switches between hosted models and the local 32B (true incognito)."
[ -z "$DIRECT" ] && echo "   Personalize $PRIV/pii_denylist.txt with your name/email/handles for the hosted-path scrubber."
echo "   Not connected yet? Run:  ghost-login"
