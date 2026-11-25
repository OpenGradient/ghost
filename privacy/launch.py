#!/usr/bin/env python3
"""ghost: the launcher. The `ghost` command hands everything here except `update` and `uninstall`.

Runs the engine on ghost's own profile. Hosted models go through the local bridge (127.0.0.1:8788), which
seals every request to a TEE enclave; `ghost --local` never leaves this machine. Nothing is contacted
before you log in.
"""
import hmac
import http.client
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PRIV = Path(__file__).resolve().parent
HOME = Path(os.environ.get("GHOST_HOME") or PRIV.parent)
ENGINE = Path(os.environ.get("GHOST_ENGINE") or Path.home() / ".ghost-engine")
os.environ["GHOST_HOME"] = os.environ["HERMES_HOME"] = str(HOME)   # ours, and the engine's isolated state

import ghost_auth  # noqa: E402
import spend  # noqa: E402

PROFILE = HOME / "profiles" / "uncensored"
PORT = 8788
SERVICE = "ai.dreamfort.ghost" if platform.system() == "Darwin" else "ghost-bridge"

# Vendors the engine would turn to if it found their key (or address) in your environment: a model in its picker, a
# search, a picture, a browser, unsealed and on your own bill. Put one in ghost's own .env if that's what you want.
# tests/test_engine.py fails when an engine release knows one that isn't here.
VENDORS = """
ACTUAL AI_GATEWAY ALIBABA_CODING_PLAN ALIBABA_CODING_PLAN_CN ALIBABA_TOKEN_PLAN ALIBABA_TOKEN_PLAN_CN ANTHROPIC ARCEE ARCEEAI AZURE_FOUNDRY BEDROCK
COHERE COMMANDCODE COMMANDCODE_ANTHROPIC DASHSCOPE DASHSCOPE_CN DEEPINFRA DEEPSEEK FIREWORKS GEMINI GLM GMI GOOGLE GROQ HELICONE HF KILOCODE KIMI
KIMI_CN KIMI_CODING LM META META_MODEL MINIMAX MINIMAX_CN MISTRAL MODEL NEBIUS NEBIUS_TOKEN_FACTORY NOUS NOVITA NVIDIA OLLAMA OPENAI OPENCODE_GO
OPENCODE_ZEN OPENROUTER PERPLEXITY QWEN RAMP_ROUTER ROUTER STEPFUN TOGETHER TOKENHUB TOKENPLAN UPSTAGE XAI XIAOMI ZAI Z_AI
BRAVE_SEARCH BROWSERBASE BROWSER_USE BRV CONNECTOR_GATEWAY ELEVENLABS EXA FIRECRAWL HINDSIGHT HONCHO KEENABLE KREA MEM0 OPENVIKING PARALLEL
RETAINDB SUPERMEMORY TAVILY
""".split()
VENDOR_ENV = {f"{vendor}_{what}" for vendor in VENDORS for what in ("API_KEY", "BASE_URL", "API_BASE", "API_URL", "TOKEN", "URL")} | {
    "AWS_BEARER_TOKEN_BEDROCK", "BROWSERBASE_PROJECT_ID", "FAL_KEY", "FIRECRAWL_BROWSER_TTL", "FIRECRAWL_GATEWAY_URL", "GOOGLE_APPLICATION_CREDENTIALS",
    "HERMES_ANON_API_SECRET", "HERMES_INFERENCE_MODEL", "HERMES_INFERENCE_PROVIDER", "HERMES_MODEL", "HERMES_QWEN_BASE_URL", "HERMES_TUI_PROVIDER",
    "LLM_MODEL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENVIKING_ENDPOINT", "PORCUPINE_ACCESS_KEY", "SEARXNG_URL", "TOOL_GATEWAY_DOMAIN",
    "TOOL_GATEWAY_SCHEME", "TOOL_GATEWAY_URL", "TOOL_GATEWAY_USER_TOKEN", "VERTEX_CREDENTIALS_PATH", "VOICE_TOOLS_OPENAI_KEY"}
LOCAL_TOOLS = "clarify,code_execution,file,session_search,skills,terminal,todo"   # none that reach the web by themselves

HELP = """\
ghost -- a private, unrestricted agent in your terminal

  ghost                     start chatting
  ghost -z "question"       ask once and print the answer
  ghost -c                  continue your last chat (ghost --resume ID for another)
  ghost --local             use the offline model (what you type stays on this machine)

  ghost login | logout      connect your DreamFort account or API key
  ghost doctor              check that everything works
  ghost usage               what hosted models have cost you, and your credits
  ghost budget [N|off]      the hourly spending limit
  ghost update              get the latest release
  ghost uninstall           remove ghost

Everything else goes to the engine, for example: ghost sessions, ghost gateway, ghost cron, ghost config
(each takes --help)."""


def say(text):
    print(f"👻 {text}", file=sys.stderr, flush=True)


# ── the profile ───────────────────────────────────────────────────────────────
def _config():
    try:
        return (PROFILE / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""


def _setting(config, section, key, indent=2):
    """One scalar out of config.yaml: `key` directly under `section`. (The launcher has no YAML parser.)"""
    found = re.search(rf"(?m)^ *{section}:\n(?:(?: {{{indent},}}.*| *#.*)\n)*? {{{indent}}}{key}: *(\S+)", config)
    return found.group(1) if found else None


def default_model(config):
    model = _setting(config, "model", "default")
    return model.rsplit("/", 1)[-1] if model else None


def local_model(config):
    return _setting(config, "ollama-local", "default_model", indent=4) or "ghost-tool:latest"


def local_fallback(config):
    return _setting(config, "fallback_model", "provider") == "ollama-local"


def pin_route():
    """A token refresh can put a Nous login's own inference route back; keep it on the bridge."""
    path, bridge = PROFILE / "auth.json", f"http://127.0.0.1:{PORT}/v1"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = False

    def walk(node):
        nonlocal changed
        for key, value in list(node.items()) if isinstance(node, dict) else list(enumerate(node)):
            if key == "inference_base_url" and value != bridge:
                node[key], changed = bridge, True
            elif isinstance(value, (dict, list)):
                walk(value)

    if isinstance(data, (dict, list)):
        walk(data)
    if changed:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def forget_typed(config):
    """Chats are deleted a day after they end (sessions.retention_days); so is the list of what you typed."""
    if _setting(config, "sessions", "auto_prune") != "true":
        return
    path, keep = PROFILE / ".hermes_history", []
    try:
        oldest = time.time() - float(_setting(config, "sessions", "retention_days") or 1) * 86400
        entries = re.split(r"(?m)^(?=# \d{4}-\d\d-\d\d )", path.read_text(encoding="utf-8"))
        for entry in entries[1:]:
            if time.mktime(time.strptime(entry[2:21], "%Y-%m-%d %H:%M:%S")) >= oldest:
                keep.append(entry)
        if len(keep) < len(entries) - 1:
            path.write_text("\n" + "".join(keep), encoding="utf-8")
    except (OSError, ValueError):
        pass


# ── the bridge ────────────────────────────────────────────────────────────────
def bridge_up():
    """Is the bridge answering, and is it yours?"""
    nonce = secrets.token_hex(8)
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
    try:
        conn.request("GET", f"/healthz?{nonce}")
        resp = conn.getresponse()
        return resp.status == 200 and hmac.compare_digest(resp.headers.get("X-Ghost-Proof") or "", ghost_auth.bridge_proof(nonce))
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def port_taken():
    """Something answers on the bridge's port (and, when the bridge isn't up, it isn't the bridge)."""
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=2).close()
        return True
    except OSError:
        return False


def _quiet(*cmd):
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _start_service():
    system = platform.system()
    if system == "Darwin":
        domain, plist = f"gui/{os.getuid()}", Path.home() / "Library" / "LaunchAgents" / f"{SERVICE}.plist"
        return plist.exists() and (_quiet("launchctl", "kickstart", f"{domain}/{SERVICE}")
                                   or _quiet("launchctl", "bootstrap", domain, str(plist)))
    if system == "Windows":
        return _quiet("schtasks", "/run", "/tn", SERVICE)
    return _quiet("systemctl", "--user", "start", f"{SERVICE}.service")


def _start_process():
    """Where no service manager will have it: a background process of our own."""
    python = Path(sys.executable)
    extra = {"start_new_session": True}
    if os.name == "nt":
        python = python.with_name("pythonw.exe") if python.with_name("pythonw.exe").exists() else python
        extra = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    with open(PRIV / "bridge.log", "ab") as out:
        subprocess.Popen([str(python), "-Es", str(PRIV / "bridge.py")], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, cwd=str(HOME), **extra)


def leave_proxies():
    """The bridge is a background service, and doesn't see the proxies your shell sets."""
    if urllib.request.getproxies_environment() or (PRIV / "proxies.json").exists() or os.name == "nt":
        import outbound   # (not for nothing: it brings an HTTP client along)
        outbound.note()


def ensure_bridge():
    """It runs as a background service; start it if something stopped it."""
    if bridge_up():
        return True
    if port_taken() and not bridge_up():
        return False
    for start in (_start_service, _start_process):   # no service manager here, or none that has it, or it won't
        try:
            if start() is False:
                continue
        except OSError:
            return False
        for _ in range(40):
            time.sleep(0.2)
            if bridge_up():
                return True
    return False


def bridge_problem():
    return (f"another program is using port {PORT}, which ghost's bridge needs (or someone else on this machine is running ghost)"
            if port_taken() else f"it didn't start; its log is {PRIV / 'bridge.log'}")


# ── the engine ────────────────────────────────────────────────────────────────
def engine_binary():
    bin_dir = ENGINE / ".hermes" / "bin"
    names = ("hermes.exe", "hermes.cmd") if os.name == "nt" else ("hermes",)
    return next((bin_dir / n for n in names if (bin_dir / n).exists()), None)


def engine_proxies():
    """What an engine needs to reach the world through this machine's proxy, and the bridge without it."""
    found = urllib.request.getproxies()   # the environment's, or the system's settings
    if os.name == "nt" and not found:
        import outbound
        found = outbound._noted()
    env = {f"{scheme}_proxy": found[scheme] for scheme in ("http", "https", "all") if found.get(scheme)}
    if env:
        direct = [h.strip() for h in (found.get("no") or "").split(",") if h.strip()]
        env["no_proxy"] = ",".join(direct + [h for h in ("127.0.0.1", "localhost", "::1") if h not in direct])
    return {name: value for lower, value in env.items() for name in (lower, lower.upper())}


def run_engine(args, provider="opengradient"):
    engine = engine_binary()
    if engine is None:
        say(f"the engine isn't installed ({ENGINE}). Put it back with: ghost update")
        return 1
    for name in VENDOR_ENV:
        os.environ.pop(name, None)
    os.environ["HERMES_INFERENCE_PROVIDER"] = provider   # whatever becomes of config.yaml
    os.environ.update({"UV_SYSTEM_CERTS": "1", "UV_NATIVE_TLS": "1"})   # for what it installs later on
    os.environ.update(engine_proxies())
    command = [str(engine), "-p", "uncensored", *args]
    sys.stdout.flush()
    if os.name != "nt":
        os.execv(command[0], command)
    signal.signal(signal.SIGINT, lambda *_: None)   # Ctrl-C belongs to the engine
    return subprocess.call(command)


def make_way(args):
    """A chat's own gateway outlives it by ten minutes, and the one that's to stay can't start beside it."""
    if args[:1] != ["gateway"] or args[1:2] not in (["install"], ["start"], ["restart"], ["setup"]) or {"--no-start-now", "-h", "--help"} & set(args):
        return
    try:
        if "--idle-exit" not in json.loads((PROFILE / "gateway.pid").read_text(encoding="utf-8"))["argv"]:
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    engine = engine_binary()
    stop = [str(engine), "-p", "uncensored", "gateway", "stop"]   # (which it won't, in the middle of something)
    if engine and subprocess.run(stop, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode \
            and (PROFILE / "gateway.pid").exists():
        say("a chat is busy, and the background gateway can't start beside it. Run this again when it's done.")


def run_local(args, config):
    model = local_model(config)
    ollama = shutil.which("ollama") or "/opt/homebrew/bin/ollama"
    try:
        models = subprocess.run([ollama, "list"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        models = ""
    if model.split(":")[0] not in models or "\n  ollama-local:\n" not in config:
        say(f"no local model installed ({model}). Reinstall with GHOST_LOCAL=1 to add the offline model.")
        return 1
    if sys.stderr.isatty():
        say(f"{model} · runs on this machine, and its web tools are off")
    if starts_a_chat(args) and not {"-t", "--toolsets"} & set(args):
        args = ["--toolsets", LOCAL_TOOLS, *args]
    return run_engine(["-m", model, "--provider", "ollama-local", *args], provider="ollama-local")


def starts_a_chat(args):
    """Is this run going to talk to a model? (`ghost sessions list`, `ghost --version`, ... are not.)"""
    return not args or args[0] == "chat" or (args[0].startswith("-") and args[0] not in ("-V", "--version"))


def log_in_first(config):
    """Nobody is logged in: ask at a terminal, say so at once elsewhere. Returns an exit code, 'local' or None."""
    offline = local_fallback(config)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        if offline:
            return None   # the offline model answers
        say("not logged in. Run: ghost login")
        return 1
    import ghost_login
    code = ghost_login.main(["--first-run"] + (["--local-model"] if offline else []))
    return "local" if code == ghost_login.LOCAL else code or None


def status_line(config, args):
    parts = [default_model(config) if "-m" not in args and "--model" not in args else "hosted",
             "sealed to a TEE", ghost_auth.label() or "not logged in (ghost login)"]
    today = spend.spent(spend.DAY)
    if today >= 0.005:
        parts.append(f"${today:.2f} in the last 24h")
    return " · ".join(parts)


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args, local = list(argv), False
    command = args[0] if args else ""
    if command in ("help", "--help", "-h"):
        print(HELP)
        return 0
    if command in ("login", "logout", "setup"):   # (the engine's own setup would leave the private path)
        import ghost_login
        return ghost_login.main(["--logout"] if command == "logout" else args[1:])
    if command == "doctor":                          # (the engine's own asks 44 vendors how they are)
        import doctor
        return doctor.main()
    if command in ("usage", "budget"):
        return spend.main(args)
    if command in ("--scrub", "--preview"):   # never let someone believe it's still on
        say(f"redaction was removed: what you send is sealed to a TEE enclave, but as you wrote it. Run ghost without {command}.")
        return 2
    while args and args[0] in ("--local", "--no-scrub", "--noscrub", "--paths", "--code"):
        local |= args.pop(0) == "--local"   # (the others went with redaction)

    config = _config()
    if local:
        return run_local(args, config)
    if not starts_a_chat(args):
        make_way(args)
        return run_engine(args)
    if not default_model(config) or "\n  opengradient:\n" not in config:
        say(f"ghost's settings ({PROFILE / 'config.yaml'}) are missing or damaged. Put them back with: ghost update")
        return 1
    forget_typed(config)
    if ghost_auth.mode() is None:
        stop = log_in_first(config)
        if stop == "local":
            return run_local(args, config)
        if stop is not None:
            return stop
    pin_route()
    leave_proxies()
    if not ensure_bridge():
        if port_taken():   # whoever it is, they'd be sent your prompts
            say(f"{bridge_problem()}. Close it, then start ghost again.")
            return 1
        say("ghost's background service isn't running, so hosted models won't answer. Fix it with: ghost update")
    if sys.stderr.isatty():
        say(status_line(config, args))
    return run_engine(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
