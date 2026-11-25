#!/usr/bin/env python3
"""ghost: the launcher. The `ghost` command hands everything here except `update` and `uninstall`.

Runs the engine on ghost's own profile. Hosted models go through the local bridge (127.0.0.1:8788), which
seals every request to a TEE enclave; `ghost --local` never leaves this machine. Nothing is contacted
before you log in.
"""
import http.client
import json
import os
import platform
import re
import shutil
import signal
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

HELP = """\
ghost -- a private, unrestricted agent in your terminal

  ghost                     start chatting
  ghost -z "question"       ask once and print the answer
  ghost -c                  continue your last chat (ghost --resume ID for another)
  ghost --local             use the offline model (nothing leaves this machine)

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
    return model.rsplit("/", 1)[-1] if model else "ghost"


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


# ── the bridge ────────────────────────────────────────────────────────────────
def bridge_up():
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
    try:
        conn.request("GET", "/healthz")
        return conn.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


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


def ensure_bridge():
    """It runs as a background service; start it if something stopped it."""
    if bridge_up():
        return True
    if not _start_service():   # no service manager here, or none that has it
        try:
            _start_process()
        except OSError:
            return False
    for _ in range(60):
        time.sleep(0.2)
        if bridge_up():
            return True
    return False


# ── the engine ────────────────────────────────────────────────────────────────
def engine_binary():
    bin_dir = ENGINE / ".hermes" / "bin"
    names = ("hermes.exe", "hermes.cmd") if os.name == "nt" else ("hermes",)
    return next((bin_dir / n for n in names if (bin_dir / n).exists()), None)


def engine_proxies():
    """What an engine needs to reach the world through this machine's proxy, and the bridge without it."""
    found = urllib.request.getproxies()   # the environment's, or the system's settings
    env = {f"{scheme}_proxy": found[scheme] for scheme in ("http", "https", "all") if found.get(scheme)}
    if env:
        direct = [h.strip() for h in (found.get("no") or "").split(",") if h.strip()]
        env["no_proxy"] = ",".join(direct + [h for h in ("127.0.0.1", "localhost", "::1") if h not in direct])
    return {name: value for lower, value in env.items() for name in (lower, lower.upper())}


def run_engine(args):
    engine = engine_binary()
    if engine is None:
        say(f"the engine isn't installed ({ENGINE}). Put it back with: ghost update")
        return 1
    # An ambient ANTHROPIC_API_KEY would make any `claude` the agent runs bill that key instead of your plan.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.update(engine_proxies())
    command = [str(engine), "-p", "uncensored", *args]
    sys.stdout.flush()
    if os.name != "nt":
        os.execv(command[0], command)
    signal.signal(signal.SIGINT, lambda *_: None)   # Ctrl-C belongs to the engine
    return subprocess.call(command)


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
        say(f"{model} · fully offline, nothing leaves this machine")
    return run_engine(["-m", model, "--provider", "ollama-local", *args])


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
        return run_engine(args)
    if ghost_auth.mode() is None:
        stop = log_in_first(config)
        if stop == "local":
            return run_local(args, config)
        if stop is not None:
            return stop
    pin_route()
    if not ensure_bridge():
        say("ghost's background service isn't running, so hosted models won't answer. Fix it with: ghost update")
    if sys.stderr.isatty():
        say(status_line(config, args))
    return run_engine(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
