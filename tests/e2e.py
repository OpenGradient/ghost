"""The ghost installed on this machine, end to end, against the stand-in DreamFort (fake_dreamfort.py).

CI runs it after installing. It takes the bridge's place, so don't run it on a machine whose ghost you use:

    ~/.ghost/venv/bin/python tests/e2e.py
"""
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE, HOME = Path(__file__).resolve().parent, Path.home()
GHOST, BIN, PORT, LOG = HOME / ".ghost", HOME / ".local" / "bin", 38990, HOME / "e2e-requests.jsonl"
WINDOWS = os.name == "nt"
ENV = {**os.environ, "GHOST_CHAT_API_URL": f"http://127.0.0.1:{PORT}", "OPENROUTER_API_KEY": "sk-or-not-yours", "PYTHONUTF8": "1"}


def run(name, *args, stdin="", timeout=240):
    command = [str(BIN / (name + (".cmd" if WINDOWS else ""))), *args]
    done = subprocess.run(command, input=stdin, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return done.returncode, done.stdout + done.stderr


def requests():
    rows = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()]
    LOG.write_text("", encoding="utf-8")
    return rows


def up():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8788/healthz", timeout=2) as resp:
            return int(resp.headers["X-Ghost-Pid"])
    except OSError:
        return None


def service(verb):
    """Stop or start the real bridge."""
    if platform.system() == "Darwin":
        domain, plist = f"gui/{os.getuid()}", HOME / "Library/LaunchAgents/ai.dreamfort.ghost.plist"
        command = ["launchctl", "bootout", f"{domain}/ai.dreamfort.ghost"] if verb == "stop" else ["launchctl", "bootstrap", domain, str(plist)]
    elif WINDOWS:
        command = ["schtasks", "/end" if verb == "stop" else "/run", "/tn", "ghost-bridge"]
    else:
        command = ["systemctl", "--user", verb, "ghost-bridge.service"]
    subprocess.run(command, capture_output=True)
    pid = up()
    if verb == "stop" and pid:   # one ghost started itself
        subprocess.run(["taskkill", "/f", "/pid", str(pid)] if WINDOWS else ["kill", str(pid)], capture_output=True)


def check(what, ok, shown=""):
    print(f"  {'ok ' if ok else 'BAD'}  {what}" + ("" if ok else f"\n{shown}"), flush=True)
    return ok


def main():
    service("stop")
    deadline = time.time() + 30
    while up() and time.time() < deadline:
        time.sleep(0.5)
    LOG.write_text("", encoding="utf-8")
    fake = [sys.executable, str(HERE / "fake_dreamfort.py"), str(PORT)]
    started = [subprocess.Popen([*fake, str(LOG)]),
               subprocess.Popen([*fake, "--bridge", str(GHOST / "privacy" / "bridge.py")], env={**os.environ, "GHOST_HOME": str(GHOST)})]
    good = True
    try:
        deadline = time.time() + 60
        while not up():
            assert time.time() < deadline, "the bridge didn't start"
            time.sleep(0.5)
        code, out = run("ghost-login", "--with-api-key", stdin="df_sk_wrong\n")
        good &= check("a bad key is turned away", code != 0 and "doesn't accept that key" in out, out)
        code, out = run("ghost-login", "--with-api-key", stdin="df_sk_good0000\n")
        good &= check("a good one isn't", code == 0, out)

        requests()
        code, out = run("ghost", "-z", "[[say hello from the stand-in]]")
        asked = requests()
        good &= check("it answers", code == 0 and "hello from the stand-in" in out, out)
        good &= check("for one request, with its limits", [(r["accepted"], r["max_tokens"], r["effort"]) for r in asked][:1]
                      == [("abliterated-model-large-v2", 32000, "low")] and len(asked) <= 2, asked)
        good &= check("which doesn't name you", not any(r["named"] for r in asked), asked)

        code, out = run("ghost", "-z", "[[run echo made-by-ghost]]", "--yolo")
        good &= check("it runs a command", "ran: " in out and "made-by-ghost" in out, out)
        requests()
        code, out = run("ghost", "-z", "[[fail cut]]")
        good &= check("a reply that breaks off is paid for twice at most", sum(r.get("billed", False) for r in requests()) <= 2 and "stopped retrying" in out, out)
        code, out = run("ghost", "-z", "[[fail 402]]")
        good &= check("no credits is said plainly", "credits" in out and not requests()[0].get("billed"), out)

        code, out = run("ghost", "sessions", "list")
        good &= check("the chat is there", code == 0 and "hello from the stand-in" in out, out)
        code, out = run("ghost", "doctor")
        good &= check("doctor is content", code == 0 and "login accepted" in out and "1 in the registry" in out, out)
        code, out = run("ghost", "usage")
        good &= check("usage counts", code == 0 and "Last hour" in out and "0 replies" not in out.splitlines()[0], out)
        code, out = run("ghost", "logout")
        good &= check("logged out", code == 0 and run("ghost-login", "--status")[0] != 0, out)
    finally:
        for process in started:
            process.kill()
            process.wait()
        LOG.unlink(missing_ok=True)
        service("start")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
