#!/usr/bin/env bash
# ghost -- one-command installer for the incognito, uncensored agentic harness.
#
#   ./install.sh
#
# Installs EVERYTHING, idempotently (safe to re-run): Ollama, the Hermes engine, the local
# uncensored models, the forked + debranded engine, the privacy stack, and the `ghost` command.
#
# Optional config via env (all optional -- plain `./install.sh` does the full private setup):
#   NOUS_API_KEY=sk-nous-...   authenticate the default 405B with a key (no browser login)
#   GHOST_DIRECT=1             skip the Webshare proxy + PII scrubber and talk to Nous directly
#                              (for a machine without your personal privacy stack, e.g. sharing)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_HOME="${ENGINE_HOME:-$HOME/.hermes}"   # where the Hermes engine installs (official installer default)
GHOST_HOME="${GHOST_HOME:-$HOME/.ghost}"      # ghost's ISOLATED state (profiles, privacy, auth) -- separate from a normie hermes
PROFILE="$GHOST_HOME/profiles/uncensored"
PRIV="$GHOST_HOME/privacy"
LA="$HOME/Library/LaunchAgents"
ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
PYTHON="${GHOST_PYTHON:-$(command -v python3 || true)}"
SCRUBBER="http://127.0.0.1:8788"
DIRECT="${GHOST_DIRECT:-}"
NOUS_API_KEY="${NOUS_API_KEY:-}"
NO_LOCAL="${GHOST_NO_LOCAL:-}"   # skip Ollama + all local models (hosted 405B/70B only)
if [ -n "$DIRECT" ]; then INFER_URL="https://inference-api.nousresearch.com/v1"; else INFER_URL="$SCRUBBER/v1"; fi

say(){ printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
have_nous(){ "$PYTHON" -c "import json,sys;n=json.load(open('$PROFILE/auth.json')).get('providers',{}).get('nous',{});sys.exit(0 if (n.get('access_token') or n.get('api_key')) else 1)" 2>/dev/null; }

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

"$PYTHON" -c "import httpx" 2>/dev/null || { echo "   installing httpx (scrubber dep)"; "$PYTHON" -m pip install -q httpx; }

# ---------- 1. local models (skipped entirely with GHOST_NO_LOCAL; 32B optional via GHOST_LOCAL_32B) ----------
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
  # the local chat/fallback model: 32B if present, else the 7B tool model
  if ollama show uncensored-local >/dev/null 2>&1; then LOCAL_MODEL="uncensored-local:latest"; else LOCAL_MODEL="ghost-tool:latest"; fi
  echo "   local model = $LOCAL_MODEL"
fi

# ---------- 2. uncensored profile ----------
say "Writing the uncensored profile"
mkdir -p "$PROFILE"
sed -e "s#__HOME__#$HOME#g" -e "s#__LOCAL_MODEL__#$LOCAL_MODEL#g" "$REPO/profile/config.yaml" > "$PROFILE/config.yaml"
cp "$REPO/profile/SOUL.md" "$PROFILE/SOUL.md"
[ -f "$PROFILE/.env" ] || cp "$REPO/profile/.env.example" "$PROFILE/.env"
if [ -n "$NO_LOCAL" ]; then   # no local model -> route auxiliary + fallback to the hosted 70B
  "$PYTHON" - "$PROFILE/config.yaml" <<'PYEOF'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"provider: ollama-local\n(\s*)model: \S+",
           r"provider: nous\n\1model: nousresearch/hermes-4-70b", s)
open(p, "w").write(s); print("   no-local: auxiliary + fallback routed to hosted hermes-4-70b")
PYEOF
fi

# ---------- 3. privacy stack (skipped with GHOST_DIRECT) ----------
if [ -z "$DIRECT" ]; then
  say "Privacy stack (rotating proxy + PII/secret scrubber)"
  mkdir -p "$PRIV/searxng"
  cp "$REPO"/privacy/*.py "$PRIV/"
  [ -f "$PRIV/pii_denylist.txt" ] || cp "$REPO/profile/pii_denylist.example.txt" "$PRIV/pii_denylist.txt"
  cp "$REPO/profile/uncensored_prefill.json" "$PRIV/uncensored_prefill.json"
  if [ ! -s "$GHOST_HOME/webshare_proxies.txt" ]; then
    echo "   Paste your Webshare proxy-list download URL (ip:port:user:pass), or Enter to skip:"
    read -r WS_URL || true
    [ -n "${WS_URL:-}" ] && curl -fsSL "$WS_URL" -o "$GHOST_HOME/webshare_proxies.txt" && echo "   $(wc -l <"$GHOST_HOME/webshare_proxies.txt"|tr -d ' ') proxies"
  fi
  mkdir -p "$LA"
  for svc in hermes-proxy hermes-pii-scrubber; do
    sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" "$REPO/launchd/com.advait.$svc.plist" > "$LA/com.advait.$svc.plist"
    launchctl unload "$LA/com.advait.$svc.plist" 2>/dev/null || true
    launchctl load -w "$LA/com.advait.$svc.plist"
  done
  printf "   waiting for scrubber"
  for _ in $(seq 1 15); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$SCRUBBER/healthz" 2>/dev/null)" = 200 ] && break; printf "."; sleep 1; done; echo " up"
  rm -f "$GHOST_HOME/.ghost-direct"
else
  say "GHOST_DIRECT set -- skipping proxy + scrubber; ghost will talk to Nous directly"
  sed -i '' -E '/^(HTTPS_PROXY|HTTP_PROXY|ALL_PROXY|DDGS_PROXY|NOUS_INFERENCE_BASE_URL)=/d' "$PROFILE/.env" 2>/dev/null || true
  # the model catalog is served by the (absent) scrubber -- disable it so the picker doesn't hang
  "$PYTHON" -c "p='$PROFILE/config.yaml';s=open(p).read();open(p,'w').write(s.replace('model_catalog:\n  enabled: true','model_catalog:\n  enabled: false'))" 2>/dev/null || true
  : > "$GHOST_HOME/.ghost-direct"   # marker -> the `ghost` launcher skips its privacy gate
fi

# ---------- 4. fork + debrand the engine ----------
say "Forking + debranding the engine -> $ENG"
GHOST_PYTHON="$PYTHON" GHOST_ENGINE="$ENG" HERMES_SRC="$SRC" bash "$REPO/scripts/fork-engine.sh"
ENGINE="$ENG/venv/bin/hermes"

# ---------- 5. Nous auth: API key (non-interactive) or browser OAuth ----------
say "Nous Portal auth (the default 405B model)"
if ! have_nous && [ -f "$ENGINE_HOME/auth.json" ]; then cp "$ENGINE_HOME/auth.json" "$PROFILE/auth.json"; fi  # reuse a normie hermes login
if [ -n "$NOUS_API_KEY" ]; then
  echo "   wiring the API key as a custom OpenAI-compatible provider (inference: $INFER_URL)"
  "$PYTHON" - "$PROFILE/config.yaml" "$NOUS_API_KEY" "$INFER_URL" <<'PYEOF'
import sys
p, key, url = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p).read()
s = s.replace("provider: nous\n", "provider: nous-key\n")  # default + (no-local) auxiliary all use the key
if "\n  nous-key:" not in s:
    s = s.replace("providers:\n  ollama-local:",
        "providers:\n  nous-key:\n    base_url: " + url + "\n    api_key: " + key +
        "\n    default_model: nousresearch/hermes-4-405b\n    context_length: 65536\n  ollama-local:")
open(p, "w").write(s)
print("   nous-key provider wired")
PYEOF
elif ! have_nous; then
  echo "   opening Nous Portal browser login (sign in to enable 405B)..."
  HERMES_HOME="$GHOST_HOME" "$ENGINE" -p uncensored portal login || true
  if ! have_nous && [ -f "$GHOST_HOME/auth.json" ]; then cp "$GHOST_HOME/auth.json" "$PROFILE/auth.json"; fi
fi
have_nous && echo "   Nous: authenticated" || echo "   Nous: not set -- ghost will use the local 32B until you authenticate"

# ---------- 6. route hosted inference through the scrubber (privacy mode only) ----------
if [ -z "$DIRECT" ]; then say "Routing hosted inference through the local scrubber"; "$PYTHON" "$PRIV/ensure_scrubber_route.py" || true; fi

# ---------- 7. the ghost command ----------
say "Installing the ghost command"
mkdir -p "$HOME/.local/bin"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" -e "s#__ENG__#$ENG#g" -e "s#__GHOST_HOME__#$GHOST_HOME#g" "$REPO/bin/ghost" > "$HOME/.local/bin/ghost"
chmod +x "$HOME/.local/bin/ghost"

# ---------- 8. smoke test ----------
say "Smoke test"
"$HOME/.local/bin/ghost" --yolo -z "Reply with one word: hi" 2>&1 | tail -2 || true

say "ghost installed -- run:  ghost"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "   (add ~/.local/bin to your PATH first)";; esac
echo "   Default = Hermes 405B; inside ghost, /model switches to the local 32B (true incognito)."
[ -z "$DIRECT" ] && echo "   Personalize $PRIV/pii_denylist.txt with your name/email/handles for the hosted-path scrubber."
