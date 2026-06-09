#!/usr/bin/env bash
# ghost -- self-sufficient installer for the incognito, uncensored agentic harness.
#
# One idempotent run sets up EVERYTHING ghost needs:
#   - local models (32B chat + 7B tool)        - the forked + debranded engine
#   - rotating proxy + PII/secret scrubber      - Nous Portal login (default 405B)
#   - the uncensored profile + private search   - scrubber routing + the `ghost` command
#
# Prerequisites it can't install for you: Ollama (https://ollama.com) and the Hermes Agent
# engine (https://hermes-agent.nousresearch.com) -- ghost forks the latter. Everything else
# is automated. Uncensored by default; the hardening governs what leaks OUT, never what ghost does.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PROFILE="$HERMES_HOME/profiles/uncensored"
PRIV="$HERMES_HOME/privacy"
LA="$HOME/Library/LaunchAgents"
ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
PYTHON="${GHOST_PYTHON:-$(command -v python3 || true)}"
SCRUBBER="http://127.0.0.1:8788"

say(){ printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
have_nous(){ "$PYTHON" -c "import json,sys;a=json.load(open('$PROFILE/auth.json'));sys.exit(0 if a.get('providers',{}).get('nous',{}).get('access_token') else 1)" 2>/dev/null; }

# ---------- 0. dependencies ----------
say "Checking dependencies"
[ -n "$PYTHON" ] || { echo "!! python3 not found"; exit 1; }
command -v ollama >/dev/null || { echo "!! Install Ollama first: https://ollama.com  (brew install --cask ollama)"; exit 1; }
pgrep -xq ollama || open -a Ollama 2>/dev/null || true        # make sure the runtime is up (the brew formula lacks llama-server)
if [ ! -d "$HERMES_HOME/hermes-agent" ] && ! command -v hermes >/dev/null; then
  echo "!! The Hermes Agent engine isn't installed (ghost forks it)."
  echo "   Install it from https://hermes-agent.nousresearch.com then re-run this."
  exit 1
fi
"$PYTHON" -c "import httpx" 2>/dev/null || { echo "   installing httpx (scrubber dep)"; "$PYTHON" -m pip install -q httpx; }

# ---------- 1. local models ----------
say "Pulling local models (skips any already present)"
while IFS=$'\t' read -r src alias; do
  case "$src" in \#*|"") continue;; esac
  if ollama show "$alias" >/dev/null 2>&1; then echo "   $alias already present"; continue; fi
  echo "   $src  ->  $alias"; ollama pull "$src"; ollama cp "$src" "$alias"
done < "$REPO/models.txt"

# ---------- 2. privacy infra + services ----------
say "Installing privacy scripts + launchd services"
mkdir -p "$PRIV/searxng"
cp "$REPO"/privacy/*.py "$PRIV/"
[ -f "$PRIV/pii_denylist.txt" ] || cp "$REPO/profile/pii_denylist.example.txt" "$PRIV/pii_denylist.txt"
cp "$REPO/profile/uncensored_prefill.json" "$PRIV/uncensored_prefill.json"

if [ ! -s "$HERMES_HOME/webshare_proxies.txt" ]; then
  echo "   Paste your Webshare proxy-list download URL (ip:port:user:pass lines), or Enter to skip:"
  read -r WS_URL || true
  if [ -n "${WS_URL:-}" ]; then
    curl -fsSL "$WS_URL" -o "$HERMES_HOME/webshare_proxies.txt"
    echo "   $(wc -l < "$HERMES_HOME/webshare_proxies.txt" | tr -d ' ') proxies saved"
  fi
fi

mkdir -p "$LA"
for svc in hermes-proxy hermes-pii-scrubber; do
  sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" \
      "$REPO/launchd/com.advait.$svc.plist" > "$LA/com.advait.$svc.plist"
  launchctl unload "$LA/com.advait.$svc.plist" 2>/dev/null || true
  launchctl load -w "$LA/com.advait.$svc.plist"
done
printf "   waiting for the scrubber"
for _ in $(seq 1 15); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$SCRUBBER/healthz" 2>/dev/null)" = 200 ] && break
  printf "."; sleep 1
done; echo " up"

# ---------- 3. uncensored profile ----------
say "Writing the uncensored profile"
mkdir -p "$PROFILE"
sed "s#__HOME__#$HOME#g" "$REPO/profile/config.yaml" > "$PROFILE/config.yaml"
cp "$REPO/profile/SOUL.md" "$PROFILE/SOUL.md"
[ -f "$PROFILE/.env" ] || cp "$REPO/profile/.env.example" "$PROFILE/.env"

# ---------- 4. fork + debrand the engine ----------
say "Forking + debranding the engine -> $ENG"
GHOST_PYTHON="$PYTHON" GHOST_ENGINE="$ENG" HERMES_SRC="$HERMES_HOME/hermes-agent" \
  bash "$REPO/scripts/fork-engine.sh"
ENGINE="$ENG/venv/bin/hermes"

# ---------- 5. Nous Portal login (default 405B) ----------
say "Setting up Nous Portal (the default 405B model)"
# reuse an existing base-install login if the profile doesn't have one yet
if ! have_nous && [ -f "$HERMES_HOME/auth.json" ]; then cp "$HERMES_HOME/auth.json" "$PROFILE/auth.json"; fi
if ! have_nous; then
  echo "   Launching Nous Portal login (opens your browser). Sign in to enable 405B."
  HERMES_HOME="$HERMES_HOME" "$ENGINE" -p uncensored portal login || true
  # login may write the global auth.json -- mirror it into the profile
  if ! have_nous && [ -f "$HERMES_HOME/auth.json" ]; then cp "$HERMES_HOME/auth.json" "$PROFILE/auth.json"; fi
fi
have_nous && echo "   Nous Portal: authenticated" || echo "   Nous Portal: skipped -- ghost will use the local 32B until you run 'hermes portal login'"

# ---------- 6. route hosted inference through the scrubber ----------
say "Routing hosted inference through the local PII/secret scrubber"
"$PYTHON" "$PRIV/ensure_scrubber_route.py" || true

# ---------- 7. the ghost command ----------
say "Installing the ghost command"
mkdir -p "$HOME/.local/bin"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" -e "s#__ENG__#$ENG#g" \
    "$REPO/bin/ghost" > "$HOME/.local/bin/ghost"
chmod +x "$HOME/.local/bin/ghost"

# ---------- 8. smoke test ----------
say "Smoke test"
"$HOME/.local/bin/ghost" --yolo -z "Reply with one word: hi" 2>&1 | tail -2 || true

say "ghost installed."
echo "   Run:  ghost            (ensure ~/.local/bin is on PATH)"
echo "   Default = Hermes 405B (scrubbed + proxied). /model switches to the local 32B (true incognito)."
echo "   Personalize $PRIV/pii_denylist.txt with your name/email/handles for the hosted-path scrubber."
