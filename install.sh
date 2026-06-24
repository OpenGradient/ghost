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
# The scrubber + og-veil talk to chat-api directly: content is private (og-veil OHTTP-encrypts
# it and the TEE enclave separates identity), reached over your normal connection.
#
# Optional config via env (all optional -- plain `./install.sh` does the full private setup):
#   GHOST_LOCAL=1        also install Ollama + a local model for an offline / true-incognito
#                        fallback (DEFAULT is hosted-only -- no Ollama, fallback is hosted 70B)
#   GHOST_LOCAL_32B=1    pull the stronger 32B local model too (26GB; implies GHOST_LOCAL)
#   GHOST_SCRUB=1        opt in to OUTBOUND PII + secret redaction (OFF by default -- ghost is a
#                        full-fidelity agent; og-veil's OHTTP+TEE provides the privacy regardless)
#   GHOST_CHAT_APP_URL=  override the website used for `ghost-login` (default chat.opengradient.ai)
set -euo pipefail

# macOS only: the privacy stack runs as launchd LaunchAgents and uses BSD tooling. Fail fast
# with a clear message rather than part-installing on Linux/WSL and erroring confusingly later.
if [ "$(uname -s)" != "Darwin" ]; then
  echo "!! ghost's installer currently supports macOS only (it uses launchd). Detected: $(uname -s)." >&2
  exit 1
fi

# Resolve where this script lives. When run via `curl ... | bash` there is no checkout, so
# self-bootstrap: clone (or fast-forward) the repo into ~/.ghost-src and re-exec from there. This
# makes ONE deterministic command both INSTALL and UPDATE ghost -- no manual clone, no LLM needed.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$REPO" ] || [ ! -f "$REPO/profile/config.yaml" ]; then
  command -v git >/dev/null 2>&1 || { echo "!! ghost needs git to fetch itself; install it (xcode-select --install) and re-run." >&2; exit 1; }
  SRC="${GHOST_SRC_DIR:-$HOME/.ghost-src}"
  if [ -d "$SRC/.git" ]; then
    echo "==> Updating ghost source ($SRC)"; git -C "$SRC" pull --ff-only || git -C "$SRC" pull
  else
    echo "==> Fetching ghost into $SRC"; rm -rf "$SRC"; git clone https://github.com/OpenGradient/ghost.git "$SRC"
  fi
  exec bash "$SRC/install.sh" "$@"
fi
ENGINE_HOME="${ENGINE_HOME:-$HOME/.hermes}"   # where the Hermes engine installs (official installer default)
GHOST_HOME="${GHOST_HOME:-$HOME/.ghost}"      # ghost's ISOLATED state (profiles, privacy, auth)
PROFILE="$GHOST_HOME/profiles/uncensored"
PRIV="$GHOST_HOME/privacy"
LA="$HOME/Library/LaunchAgents"
ENG="${GHOST_ENGINE:-$HOME/.ghost-engine}"
PYTHON="${GHOST_PYTHON:-}"   # resolved to an isolated uv venv (Python 3.11) in the Dependencies step
SCRUBBER="http://127.0.0.1:8788"
# Local models (Ollama) are OPT-IN. Default = hosted-only: no Ollama, and the fallback +
# auxiliary tasks route to a hosted model (nous/hermes-4-70b) over the same private og-veil
# path. Set GHOST_LOCAL=1 to also install Ollama + a local model for an offline / incognito
# fallback. GHOST_LOCAL_32B implies GHOST_LOCAL. (GHOST_NO_LOCAL is still accepted as a no-op
# since hosted-only is now the default.)
WANT_LOCAL="${GHOST_LOCAL:-}"; [ -n "${GHOST_LOCAL_32B:-}" ] && WANT_LOCAL=1

# Record the source path + the chosen install options so `ghost update` can re-pull and
# re-install the exact same way (see bin/ghost-update).
mkdir -p "$GHOST_HOME"
echo "$REPO" > "$GHOST_HOME/.src"
{
  [ -n "${GHOST_LOCAL:-}" ] && echo "GHOST_LOCAL=1"
  [ -n "${GHOST_LOCAL_32B:-}" ] && echo "GHOST_LOCAL_32B=1"
  [ -n "${GHOST_SCRUB:-}" ] && echo "GHOST_SCRUB=1"
  [ -n "${GHOST_CHAT_APP_URL:-}" ] && echo "GHOST_CHAT_APP_URL=$GHOST_CHAT_APP_URL"
  :
} > "$GHOST_HOME/.install-env"

say(){ printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }

# ---------- 0. dependencies (auto-installed) ----------
say "Dependencies"
# uv manages the Python toolchain + an isolated venv for ghost's privacy stack, so we never depend
# on the system python (Apple ships 3.9) -- uv fetches CPython 3.11 itself if the machine lacks it.
if ! have uv; then
  echo "   installing uv (Astral's Python package/venv manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
have uv || { echo "!! ghost needs uv (https://docs.astral.sh/uv/getting-started/installation/); install it and re-run." >&2; exit 1; }

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

# Privacy stack: og-veil (the opengradient-veil package) owns the whole hosted-inference protocol
# -- on-chain TEE registry discovery, OHTTP/HPKE encryption, response verification, the Supabase
# session. Declared in pyproject.toml + uv.lock, installed into an ISOLATED venv at ~/.ghost/venv
# (Python 3.11, fetched by uv if the system lacks it). Reproducible; never touches system python.
say "Privacy stack (isolated uv venv, Python 3.11)"
export UV_PROJECT_ENVIRONMENT="$GHOST_HOME/venv"
SYNC_EXTRAS=""
# The NER scrubber (Presidio + spaCy) is only needed if you opt into redaction -- skip it otherwise.
[ -n "${GHOST_SCRUB:-}" ] && SYNC_EXTRAS="--extra presidio"
( cd "$REPO" && uv sync --python 3.11 --frozen $SYNC_EXTRAS ) \
  || ( cd "$REPO" && uv sync --python 3.11 $SYNC_EXTRAS ) \
  || { echo "!! failed to provision the privacy venv (uv sync); check network and re-run." >&2; exit 1; }
PYTHON="$UV_PROJECT_ENVIRONMENT/bin/python"
echo "   venv: Python $("$PYTHON" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))') · og-veil $("$PYTHON" -c 'import importlib.metadata as m;print(m.version("opengradient-veil"))' 2>/dev/null)"

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
# Chat fallback -> the stronger 405B (aux tasks stay on 70B for speed/cost).
s = re.sub(r"(fallback_model:\n  provider: opengradient\n  model: )nous/hermes-4-70b",
           r"\g<1>nous/hermes-4-405b", s, count=1)
open(p, "w").write(s); print("   hosted-only: fallback -> nous/hermes-4-405b, auxiliary -> nous/hermes-4-70b (via og-veil)")
PYEOF
fi

# ---------- 3. privacy stack (PII scrubber + og-veil) ----------
say "Privacy stack (PII/secret scrubber -> og-veil)"
mkdir -p "$PRIV"
cp "$REPO"/privacy/*.py "$PRIV/"
# Stage the NER engine for redaction. Redaction is OFF by default, so this only matters once you
# opt in (GHOST_SCRUB / ghost --scrub). When the presidio extra is installed, fetch the spaCy model
# and mark NER available; otherwise redaction (if turned on later) uses the regex engine.
if "$PYTHON" -c "import presidio_analyzer, presidio_anonymizer" 2>/dev/null; then
  "$PYTHON" -c "import en_core_web_md" 2>/dev/null || "$PYTHON" -m spacy download en_core_web_md >/dev/null 2>&1 || true
  if "$PYTHON" -c "import en_core_web_md" 2>/dev/null; then
    : > "$PRIV/.presidio"; echo "   redaction engine ready: Presidio + spaCy NER (used only when redaction is on)"
  else
    rm -f "$PRIV/.presidio"; echo "   redaction engine: regex (spaCy model unavailable)"
  fi
else
  rm -f "$PRIV/.presidio"   # presidio not installed (default) -> regex engine if redaction is turned on
fi
[ -f "$PRIV/pii_denylist.txt" ] || cp "$REPO/profile/pii_denylist.example.txt" "$PRIV/pii_denylist.txt"
cp "$REPO/profile/uncensored_prefill.json" "$PRIV/uncensored_prefill.json"
# Outbound PII + secret redaction is OPT-IN (GHOST_SCRUB=1), OFF by default: ghost is a
# full-fidelity agent and og-veil's OHTTP+TEE already make the hosted path private. The .scrub
# marker drives the bridge; the engine's redact_secrets/redact_pii follow the same default.
if [ -n "${GHOST_SCRUB:-}" ]; then
  : > "$PRIV/.scrub"
  "$PYTHON" - "$PROFILE/config.yaml" <<'PYEOF'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"(?m)^  redact_secrets: false$", "  redact_secrets: true", s)
s = re.sub(r"(?m)^  redact_pii: false$", "  redact_pii: true", s)
open(p, "w").write(s)
PYEOF
  say "Outbound PII + secret redaction ON (GHOST_SCRUB)"
else
  rm -f "$PRIV/.scrub" "$PRIV/.no_scrub"
  say "Full-fidelity mode (default) -- no outbound redaction. Set GHOST_SCRUB=1 to strip your PII/secrets before the gateway."
fi
mkdir -p "$LA"

# The scrubber runs as a launchd service; og-veil talks to chat-api directly (content is
# still private via OHTTP/TEE). Clean up any rotating-proxy marker from an older install.
BASE_SERVICES="hermes-pii-scrubber"
rm -f "$PRIV/.proxy"

for svc in $BASE_SERVICES; do
  sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" "$REPO/launchd/com.advait.$svc.plist" > "$LA/com.advait.$svc.plist"
  launchctl unload "$LA/com.advait.$svc.plist" 2>/dev/null || true
  launchctl load -w "$LA/com.advait.$svc.plist"
done

# og-veil service (port 11435, to avoid colliding with Ollama on 11434). It owns the
# OHTTP/TEE/verification + auth, and talks to chat-api directly.
VEIL_PLIST="$LA/com.advait.hermes-veil.plist"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" "$REPO/launchd/com.advait.hermes-veil.plist" > "$VEIL_PLIST"
launchctl unload "$VEIL_PLIST" 2>/dev/null || true
launchctl load -w "$VEIL_PLIST"

printf "   waiting for the scrubbing bridge"
for _ in $(seq 1 15); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$SCRUBBER/healthz" 2>/dev/null)" = 200 ] && break; printf "."; sleep 1; done; echo " up"
printf "   waiting for og-veil"
for _ in $(seq 1 20); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:11435/health" 2>/dev/null)" = 200 ] && break; printf "."; sleep 1; done; echo " up (or pending ghost-login)"

# ---------- 4. fork + debrand the engine ----------
say "Forking + debranding the engine -> $ENG"
GHOST_PYTHON="$PYTHON" GHOST_ENGINE="$ENG" HERMES_SRC="$SRC" bash "$REPO/scripts/fork-engine.sh"

# ---------- 5. the ghost + ghost-login + ghost-update commands ----------
say "Installing the ghost + ghost-login + ghost-update commands"
mkdir -p "$HOME/.local/bin"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" -e "s#__ENG__#$ENG#g" -e "s#__GHOST_HOME__#$GHOST_HOME#g" "$REPO/bin/ghost" > "$HOME/.local/bin/ghost"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HOME__#$HOME#g" -e "s#__GHOST_HOME__#$GHOST_HOME#g" "$REPO/bin/ghost-login" > "$HOME/.local/bin/ghost-login"
sed -e "s#__HOME__#$HOME#g" -e "s#__GHOST_HOME__#$GHOST_HOME#g" "$REPO/bin/ghost-update" > "$HOME/.local/bin/ghost-update"
chmod +x "$HOME/.local/bin/ghost" "$HOME/.local/bin/ghost-login" "$HOME/.local/bin/ghost-update"

# ---------- 6. connect your account (hosted models) ----------
say "Connect your OpenGradient Chat account (for hosted models)"
GL="$HOME/.local/bin/ghost-login"   # thin wrapper over `og-veil login` (installed above)
if "$GL" --status >/dev/null 2>&1; then
  echo "   already connected: $("$GL" --status)"
else
  echo "   Hosted models (the default DeepSeek V4 Pro + Hermes 4, all open-weight) need a one-time login."
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
echo "   Redaction is OFF by default (full fidelity). Opt in with GHOST_SCRUB=1 (or 'ghost --scrub'), then personalize $PRIV/pii_denylist.txt."
echo "   Not connected yet? Run:  ghost-login"
