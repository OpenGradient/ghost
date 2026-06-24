#!/usr/bin/env bash
# ghost -- one-command installer for the incognito, uncensored agentic harness.
#
#   ./install.sh
#
# Installs EVERYTHING, idempotently (safe to re-run): Ollama, the Hermes engine, the local
# uncensored models, the forked + debranded engine, the privacy stack, and the `ghost` +
# `ghost-login` commands.
#
# Hosted (non-local) models run through ghost's local PII scrubber and then og-veil
# (the opengradient-veil package), which encrypts each request over Oblivious-HTTP to
# the OpenGradient chat-api TEE gateway -- the same enclave path the chat.opengradient.ai
# website uses. After install, run `ghost-login` once to connect your account (a browser
# login that hands a session token to og-veil).
#
# By default ghost runs DIRECT: the scrubber + og-veil talk to chat-api directly (content is
# still private -- og-veil OHTTP-encrypts it and the TEE enclave separates identity). No
# rotating-proxy setup is needed. IP-masking is opt-in (see GHOST_PROXY below).
#
# Optional config via env (all optional -- plain `./install.sh` does the full private setup):
#   GHOST_PROXY=1        opt in to the Webshare rotating proxy: masks your IP from the chat-api
#                        relay (og-veil egress) + carries the engine's web-search egress
#   GHOST_LOCAL=1        also install Ollama + a local model for an offline / true-incognito
#                        fallback (DEFAULT is hosted-only -- no Ollama, fallback is hosted 70B)
#   GHOST_LOCAL_32B=1    pull the stronger 32B local model too (26GB; implies GHOST_LOCAL)
#   GHOST_CHAT_APP_URL=  override the website used for `ghost-login` (default chat.opengradient.ai)
set -euo pipefail

# macOS only: the privacy stack runs as launchd LaunchAgents and uses BSD tooling. Fail fast
# with a clear message rather than part-installing on Linux/WSL and erroring confusingly later.
if [ "$(uname -s)" != "Darwin" ]; then
  echo "!! ghost's installer currently supports macOS only (it uses launchd). Detected: $(uname -s)." >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_HOME="${ENGINE_HOME:-$HOME/.hermes}"   # where the Hermes engine installs (official installer default)
GHOST_HOME="${GHOST_HOME:-$HOME/.ghost}"      # ghost's ISOLATED state (profiles, privacy, auth)
PROFILE="$GHOST_HOME/profiles/uncensored"
PRIV="$GHOST_HOME/privacy"
LA="$HOME/Library/LaunchAgents"
ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
PYTHON="${GHOST_PYTHON:-$(command -v python3 || true)}"
SCRUBBER="http://127.0.0.1:8788"
# Direct is the default. Opt in to the Webshare rotating proxy with GHOST_PROXY=1.
# (GHOST_DIRECT is still honored for back-compat, but it's now the default anyway.)
USE_PROXY="${GHOST_PROXY:-}"
# Local models (Ollama) are OPT-IN. Default = hosted-only: no Ollama, and the fallback +
# auxiliary tasks route to a hosted model (nous/hermes-4-70b) over the same private og-veil
# path. Set GHOST_LOCAL=1 to also install Ollama + a local model for an offline / incognito
# fallback. GHOST_LOCAL_32B implies GHOST_LOCAL. (GHOST_NO_LOCAL is still accepted as a no-op
# since hosted-only is now the default.)
WANT_LOCAL="${GHOST_LOCAL:-}"; [ -n "${GHOST_LOCAL_32B:-}" ] && WANT_LOCAL=1

say(){ printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }

# ---------- 0. dependencies (auto-installed) ----------
say "Dependencies"
[ -n "$PYTHON" ] || { echo "!! need python3 (3.11+); install it and re-run."; exit 1; }

if [ -n "$WANT_LOCAL" ]; then
  if ! have ollama && have brew; then echo "   installing Ollama (brew --cask)"; brew install --cask ollama || true; fi
  have ollama || { echo "!! GHOST_LOCAL set but Ollama is missing -- install it from https://ollama.com (or drop GHOST_LOCAL for hosted-only) then re-run."; exit 1; }
  pgrep -xq ollama || open -a Ollama 2>/dev/null || true ; sleep 1
fi

if [ ! -d "$ENGINE_HOME/hermes-agent" ] && ! have hermes; then
  say "Installing the Hermes Agent engine (official one-liner)"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash \
    || { echo "!! engine install failed; install manually (https://hermes-agent.nousresearch.com) then re-run."; exit 1; }
fi
SRC="$ENGINE_HOME/hermes-agent"; [ -d "$SRC" ] || SRC="$(cd "$(dirname "$(command -v hermes)")/.." 2>/dev/null && pwd)"
[ -d "$SRC" ] || { echo "!! can't locate the Hermes engine to fork."; exit 1; }

# Privacy-stack deps: og-veil (the opengradient-veil package) owns the whole
# hosted-inference protocol -- on-chain TEE registry discovery, Oblivious-HTTP/HPKE
# encryption, response verification, and the Supabase session -- so ghost no longer
# hand-rolls any of it (cryptography/web3 come in transitively via the SDK). The
# scrubbing bridge only needs httpx to forward to og-veil's local server.
say "Privacy-stack Python deps (opengradient-veil + httpx)"
# Pinned: og-veil is the load-bearing OHTTP/HPKE/TEE-verify boundary, so we don't silently
# --upgrade into a behavior/verification change. Bump the pin in requirements.txt deliberately.
"$PYTHON" -m pip install -q -r "$REPO/requirements.txt" \
  || { echo "!! failed to install the privacy stack (opengradient-veil + httpx); check pip/network and re-run."; exit 1; }
echo "   og-veil $("$PYTHON" -m pip show opengradient-veil 2>/dev/null | awk '/^Version:/{print $2}')"
# Optional NER PII scrubber (Presidio + spaCy en_core_web_md). Best-effort: if it installs,
# ghost enables it; if not, the proven regex scrubber stays in use. ~40MB model.
if ! "$PYTHON" -c "import presidio_analyzer, presidio_anonymizer" 2>/dev/null; then
  echo "   installing Presidio (NER PII scrubber)"; "$PYTHON" -m pip install -q presidio-analyzer presidio-anonymizer 2>/dev/null || true
fi
"$PYTHON" -c "import en_core_web_md" 2>/dev/null || "$PYTHON" -m spacy download en_core_web_md 2>/dev/null || true

# ---------- 1. local models (OPT-IN via GHOST_LOCAL; 32B also needs GHOST_LOCAL_32B) ----------
LOCAL_MODEL="ghost-tool:latest"
if [ -z "$WANT_LOCAL" ]; then
  say "Hosted-only (default) -- skipping Ollama + local models. Set GHOST_LOCAL=1 for an offline / incognito local fallback."
else
  say "Local models (GHOST_LOCAL)"
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
if [ -n "$USE_PROXY" ]; then   # opt-in: route the engine's own egress (web search/fetches) through the rotating proxy
  "$PYTHON" - "$PROFILE/.env" <<'PYEOF'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"(?m)^#\s*((?:HTTPS_PROXY|HTTP_PROXY|ALL_PROXY|DDGS_PROXY)=\S+)\s*$", r"\1", s)
open(p, "w").write(s)
PYEOF
fi
if [ -z "$WANT_LOCAL" ]; then   # hosted-only (default) -> route auxiliary + fallback to a hosted model via og-veil
  "$PYTHON" - "$PROFILE/config.yaml" <<'PYEOF'
import sys, re
p = sys.argv[1]; s = open(p).read()
# Handle both key orders (provider-then-model and model-then-provider in the auxiliary blocks).
s = re.sub(r"provider: ollama-local\n(\s*)model: \S+",
           r"provider: opengradient\n\1model: nous/hermes-4-70b", s)
s = re.sub(r"model: ghost-tool:latest\n(\s*)provider: ollama-local",
           r"model: nous/hermes-4-70b\n\1provider: opengradient", s)
s = s.replace("provider: ollama-local", "provider: opengradient")
open(p, "w").write(s); print("   hosted-only: auxiliary + fallback routed to hosted nous/hermes-4-70b (via og-veil)")
PYEOF
fi

# ---------- 3. privacy stack (PII scrubber + og-veil always; rotating proxy only with GHOST_PROXY) ----------
say "Privacy stack (PII/secret scrubber -> og-veil${USE_PROXY:+ + rotating proxy})"
mkdir -p "$PRIV"
cp "$REPO"/privacy/*.py "$PRIV/"
# Enable the NER PII scrubber when Presidio + the spaCy model are present; else leave it off
# (the bridge falls back to the regex scrubber). Toggle anytime: touch/rm $PRIV/.presidio.
if "$PYTHON" -c "import presidio_analyzer, en_core_web_md" 2>/dev/null; then
  : > "$PRIV/.presidio"; echo "   NER PII scrubber (Presidio + spaCy) enabled"
else
  rm -f "$PRIV/.presidio"; echo "   Presidio not available -- using the regex scrubber"
fi
[ -f "$PRIV/pii_denylist.txt" ] || cp "$REPO/profile/pii_denylist.example.txt" "$PRIV/pii_denylist.txt"
cp "$REPO/profile/uncensored_prefill.json" "$PRIV/uncensored_prefill.json"
mkdir -p "$LA"

# The scrubber always runs. The Webshare rotating proxy is OPT-IN (GHOST_PROXY=1):
# by default ghost is direct -- og-veil talks to chat-api itself (content is still
# private via OHTTP/TEE; only IP-masking is skipped).
BASE_SERVICES="hermes-pii-scrubber"
if [ -n "$USE_PROXY" ]; then
  if [ ! -s "$GHOST_HOME/webshare_proxies.txt" ]; then
    echo "   Paste your Webshare proxy-list download URL (ip:port:user:pass), or Enter to skip:"
    read -r WS_URL || true
    [ -n "${WS_URL:-}" ] && curl -fsSL "$WS_URL" -o "$GHOST_HOME/webshare_proxies.txt" && echo "   $(wc -l <"$GHOST_HOME/webshare_proxies.txt"|tr -d ' ') proxies"
  fi
  BASE_SERVICES="hermes-proxy $BASE_SERVICES"
  : > "$PRIV/.proxy"   # marker: egress is IP-masked through the rotating proxy (banner reads this)
else
  rm -f "$PRIV/.proxy"
  say "Direct mode (default) -- og-veil talks to chat-api directly; no rotating proxy. Set GHOST_PROXY=1 to IP-mask."
fi

for svc in $BASE_SERVICES; do
  sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" "$REPO/launchd/com.advait.$svc.plist" > "$LA/com.advait.$svc.plist"
  launchctl unload "$LA/com.advait.$svc.plist" 2>/dev/null || true
  launchctl load -w "$LA/com.advait.$svc.plist"
done

# og-veil service (port 11435, to avoid colliding with Ollama on 11434). It owns the
# OHTTP/TEE/verification + auth. Default: direct egress to chat-api. With GHOST_PROXY=1
# its egress is routed through the rotating proxy so the relay never sees your real IP.
if [ -n "$USE_PROXY" ]; then
  VEIL_PROXY_ENV=$'        <key>HTTPS_PROXY</key>\n        <string>http://127.0.0.1:8899</string>\n        <key>HTTP_PROXY</key>\n        <string>http://127.0.0.1:8899</string>\n        <key>NO_PROXY</key>\n        <string>127.0.0.1,localhost,::1</string>'
else
  VEIL_PROXY_ENV=""
fi
VEIL_PLIST="$LA/com.advait.hermes-veil.plist"
GP_PYTHON="$PYTHON" GP_HOME="$HOME" GP_PROXY_ENV="$VEIL_PROXY_ENV" \
  "$PYTHON" - "$REPO/launchd/com.advait.hermes-veil.plist" "$VEIL_PLIST" <<'PYEOF'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
s = s.replace("__PYTHON__", os.environ["GP_PYTHON"]).replace("__HOME__", os.environ["GP_HOME"])
s = s.replace("__VEIL_PROXY_ENV__", os.environ.get("GP_PROXY_ENV", ""))
open(dst, "w").write(s)
PYEOF
launchctl unload "$VEIL_PLIST" 2>/dev/null || true
launchctl load -w "$VEIL_PLIST"

printf "   waiting for the scrubbing bridge"
for _ in $(seq 1 15); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$SCRUBBER/healthz" 2>/dev/null)" = 200 ] && break; printf "."; sleep 1; done; echo " up"
printf "   waiting for og-veil"
for _ in $(seq 1 20); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:11435/health" 2>/dev/null)" = 200 ] && break; printf "."; sleep 1; done; echo " up (or pending ghost-login)"

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
GL="$HOME/.local/bin/ghost-login"   # thin wrapper over `og-veil login` (installed above)
if "$GL" --status >/dev/null 2>&1; then
  echo "   already connected: $("$GL" --status)"
else
  echo "   Hosted models (the default Hermes 405B + Claude/GPT/Gemini/Grok) need a one-time login."
  if [ -t 0 ]; then
    printf "   Run the browser login now? [Y/n] "; read -r ANS || true
    case "${ANS:-Y}" in [Nn]*) echo "   Skipped -- run 'ghost-login' anytime.";; *) "$GL" || echo "   (login skipped/failed -- run 'ghost-login' anytime)";; esac
  else
    echo "   Non-interactive install -- run 'ghost-login' once you're done."
  fi
fi

# ---------- 7. smoke test ----------
say "Smoke test"
"$HOME/.local/bin/ghost" --yolo -z "Reply with one word: hi" 2>&1 | tail -2 || true

say "ghost installed -- run:  ghost"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "   (add ~/.local/bin to your PATH first)";; esac
echo "   Hosted default = deepseek/deepseek-v4-pro via og-veil -> the OpenGradient TEE gateway (OHTTP-private)."
echo "   Inside ghost, /model switches between hosted models and the local model (true incognito)."
echo "   Personalize $PRIV/pii_denylist.txt with your name/email/handles for the hosted-path scrubber."
[ -n "$USE_PROXY" ] || echo "   Direct mode (default). For IP-masking from the relay, reinstall with GHOST_PROXY=1."
echo "   Not connected yet? Run:  ghost-login"
