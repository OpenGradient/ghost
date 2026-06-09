#!/usr/bin/env bash
# ghost -- installer for the incognito, uncensored agentic harness.
#
# Sets ghost up as an isolated Hermes Agent profile:
#   - local-by-default models (32B abliterated chat + 7B abliterated tool/aux), offline
#   - rotating Webshare privacy proxy on all hosted egress
#   - PII + secret-exfiltration scrubber on the hosted path
#   - private web search (ddgs routed through your proxy)
#   - picker locked to {local, hermes-4-405b, hermes-4-70b}; no telemetry
# Uncensored by default. The hardening governs what leaks OUT, never what ghost can do.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PROFILE="$HERMES_HOME/profiles/uncensored"
PRIV="$HERMES_HOME/privacy"
LA="$HOME/Library/LaunchAgents"
PYTHON="${GHOST_PYTHON:-$(command -v python3)}"

echo "==> ghost installer  (engine home: $HERMES_HOME, python: $PYTHON)"

# --- deps ---
command -v ollama >/dev/null || { echo "!! Install Ollama first: https://ollama.com"; exit 1; }
command -v hermes >/dev/null || { echo "!! Install the Hermes Agent engine first (ghost rides on it)."; exit 1; }
"$PYTHON" -c "import httpx" 2>/dev/null || { echo "==> installing httpx (scrubber dep)"; "$PYTHON" -m pip install -q httpx; }

# --- local models (pull + short alias) ---
while IFS=$'\t' read -r src alias; do
  case "$src" in \#*|"") continue;; esac
  echo "==> pulling $src  ->  $alias"
  ollama pull "$src"
  ollama cp "$src" "$alias"
done < "$REPO/models.txt"

# --- privacy infra ---
mkdir -p "$PRIV/searxng"
cp "$REPO"/privacy/*.py "$PRIV/"
[ -f "$PRIV/pii_denylist.txt" ] || cp "$REPO/profile/pii_denylist.example.txt" "$PRIV/pii_denylist.txt"
cp "$REPO/profile/uncensored_prefill.json" "$PRIV/uncensored_prefill.json"

# --- Webshare proxy list (the rotating exits) ---
if [ ! -s "$HERMES_HOME/webshare_proxies.txt" ]; then
  echo "==> Paste your Webshare proxy-list download URL (ip:port:user:pass lines), or Enter to skip:"
  read -r WS_URL || true
  if [ -n "${WS_URL:-}" ]; then
    curl -fsSL "$WS_URL" -o "$HERMES_HOME/webshare_proxies.txt"
    echo "   $(wc -l < "$HERMES_HOME/webshare_proxies.txt" | tr -d ' ') proxies saved"
  fi
fi

# --- profile (config + identity + env) ---
mkdir -p "$PROFILE"
sed "s#__HOME__#$HOME#g" "$REPO/profile/config.yaml" > "$PROFILE/config.yaml"
cp "$REPO/profile/SOUL.md" "$PROFILE/SOUL.md"
[ -f "$PROFILE/.env" ] || cp "$REPO/profile/.env.example" "$PROFILE/.env"
# Hosted models (opt-in) need Nous OAuth; reuse the top-level login if present.
if [ -f "$HERMES_HOME/auth.json" ] && [ ! -f "$PROFILE/auth.json" ]; then
  cp "$HERMES_HOME/auth.json" "$PROFILE/auth.json"
fi

# --- launchd services: rotating proxy + scrubber ---
mkdir -p "$LA"
for svc in hermes-proxy hermes-pii-scrubber; do
  sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" \
      "$REPO/launchd/com.advait.$svc.plist" > "$LA/com.advait.$svc.plist"
  launchctl unload "$LA/com.advait.$svc.plist" 2>/dev/null || true
  launchctl load -w "$LA/com.advait.$svc.plist"
done

# --- the `ghost` command ---
mkdir -p "$HOME/.local/bin"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" "$REPO/bin/ghost" > "$HOME/.local/bin/ghost"
chmod +x "$HOME/.local/bin/ghost"

echo
echo "==> ghost installed. Ensure ~/.local/bin is on PATH, then run:  ghost"
echo "    Default = LOCAL 32B (fully offline). Use /model to switch to hosted Hermes (scrubbed + proxied)."
echo "    Edit $PRIV/pii_denylist.txt with your own name/email/handles to scrub on the hosted path."
