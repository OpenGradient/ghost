"""install.sh, `ghost update` and `ghost uninstall`, with stand-ins for uv, the engine and the service managers.

The real engine and services are covered by CI's install jobs; this is everything around them.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows has install.ps1")

REPO = Path(__file__).resolve().parent.parent
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"   # on a Mac, that's bash 3.2
OLD_AGENTS = ("com.advait.hermes-pii-scrubber", "com.advait.hermes-veil")
OLD_UNITS = ("ghost-scrubber", "ghost-veil")

FORK_ENGINE = r"""#!/bin/bash
echo "fork-engine $* browser=${GHOST_NO_BROWSER:-yes}" >> "$HOME/calls"
case "${1:-}" in
  --check) [ ! -e "$HOME/ghost-open" ] || { echo '!! ghost is open (pid 42). Close those windows and re-run.' >&2; exit 1; }; exit 0 ;;
  --refresh-gateway) exit 0 ;;
esac
echo "compiling the engine"
[ ! -e "$HOME/engine-broken" ] || { echo "the reason it broke"; exit 3; }
mkdir -p "$GHOST_ENGINE/.hermes/bin" "$GHOST_HOME/profiles/uncensored"
[ -e "$GHOST_HOME/profiles/uncensored/SOUL.md" ] || echo "the engine's default" > "$GHOST_HOME/profiles/uncensored/SOUL.md"
printf '{"distribution": "ghost"}' > "$GHOST_ENGINE/install-stamp.json"
printf '#!/bin/sh\necho "engine $*" >> "$HOME/calls"\necho "Ghost v9"\n' > "$GHOST_ENGINE/.hermes/bin/hermes"
chmod +x "$GHOST_ENGINE/.hermes/bin/hermes"
"""


class Machine:
    def __init__(self, root, system, port):
        self.home, self.bin, self.repo = root / "home", root / "stubs", root / "repo"
        self.ghost, self.engine, self.commands = self.home / ".ghost", self.home / ".ghost-engine", self.home / ".local" / "bin"
        self.profile = self.ghost / "profiles" / "uncensored"
        for directory in (self.home, self.bin, self.repo):
            directory.mkdir()
        for name in ("bin", "launchd", "systemd", "privacy", "profile", "scripts"):
            shutil.copytree(REPO / name, self.repo / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("install.sh", "engine.pin", "models.txt"):
            shutil.copy(REPO / name, self.repo / name)
        (self.repo / "scripts" / "fork-engine.sh").write_text(FORK_ENGINE)
        launch = self.repo / "privacy" / "launch.py"
        launch.write_text(launch.read_text(encoding="utf-8").replace("PORT = 8788", f"PORT = {port}"), encoding="utf-8")
        log = 'echo "$(basename "$0") $*" >> "$HOME/calls"\n'
        self.stub("uname", f"echo {system}\n")
        self.stub("uv", log + 'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
                  f'printf \'#!/bin/sh\\nexec "{sys.executable}" "$@"\\n\' > "$UV_PROJECT_ENVIRONMENT/bin/python"\n'
                  'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"\n')
        self.stub("curl", log + "exit 7\n")       # nobody is on the bridge's port
        for name in ("launchctl", "loginctl", "pkill"):
            self.stub(name, log)
        self.stub("systemctl", log + '[ "$2" != show-environment ] || [ ! -e "$HOME/no-systemd" ]\n')

    def stub(self, name, body):
        (self.bin / name).write_text("#!/bin/sh\n" + body)
        (self.bin / name).chmod(0o755)

    def run(self, *cmd, **env):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}", "LANG": "en_US.UTF-8", "SHELL": "/bin/zsh", **env}
        return subprocess.run([str(c) for c in cmd], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)

    def install(self, **env):
        return self.run(BASH, self.repo / "install.sh", **env)

    def command(self, name, *args, **env):
        return self.run(self.commands / name, *args, **env)

    def calls(self):
        path = self.home / "calls"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def machine(tmp_path):
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield lambda system="Linux": Machine(tmp_path, system, server.server_port)
    server.shutdown()
    server.server_close()


# ── installing ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_fresh_install(machine, system):
    m = machine(system)
    done = m.install()
    assert done.returncode == 0, done.stdout + done.stderr
    for step in ("Checking this machine", "Installing the privacy bridge", "Building the engine (Hermes ", "Writing your profile",
                 "Starting the background service", "Installing the ghost command"):
        assert f"✓ {step}" in done.stdout
    assert "It asks you to log in the first time" in done.stdout and "compiling the engine" not in done.stdout
    assert "previous" not in done.stdout and "Ghost" in (m.profile / "SOUL.md").read_text()
    assert "compiling the engine" in (m.ghost / "install.log").read_text()
    for name in ("ghost", "ghost-login", "ghost-update", "ghost-uninstall"):
        assert os.access(m.commands / name, os.X_OK) and "__" not in (m.commands / name).read_text()
    assert (m.ghost / ".src").read_text() == f"{m.repo}\n" and (m.ghost / ".install-env").read_text() == ""
    config = (m.profile / "config.yaml").read_text()
    assert "ollama-local\n  model" not in config and f"{m.ghost}/privacy/uncensored_prefill.json" in config and "__" not in config
    assert (m.profile / ".env").exists() and (m.ghost / "privacy" / "uncensored_prefill.json").exists()


def test_a_new_install_contacts_nobody_until_you_log_in(machine):
    """It used to send a test message to a server that wasn't there yet, and wait for it."""
    m = machine()
    m.install()
    chat = m.command("ghost", "-z", "hi")
    assert (chat.returncode, chat.stdout) == (1, "") and "not logged in. Run: ghost login" in chat.stderr
    assert m.command("ghost", "--version").stdout == "Ghost v9\n"
    assert m.command("ghost-login", "--status").returncode == 1
    assert {c for c in m.calls() if c.startswith("curl")} <= {"curl -s -o /dev/null --max-time 1 http://127.0.0.1:8788/healthz"}


def test_services_on_a_mac(machine):
    m = machine("Darwin")
    agents = m.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in OLD_AGENTS:
        (agents / f"{label}.plist").write_text("old")
    assert m.install().returncode == 0
    assert [p.name for p in agents.iterdir()] == ["ai.dreamfort.ghost.plist"]
    text = (agents / "ai.dreamfort.ghost.plist").read_text()
    assert f"<string>{m.ghost}/venv/bin/python</string>" in text and f"<string>{m.ghost}/privacy/bridge.py</string>" in text
    calls = [c for c in m.calls() if c.startswith("launchctl")]
    domain = f"gui/{os.getuid()}"
    assert calls == [*(f"launchctl bootout {domain}/{label}" for label in OLD_AGENTS),
                     f"launchctl bootstrap {domain} {agents}/ai.dreamfort.ghost.plist"]
    m.install()   # again: the running one is replaced
    assert [c for c in m.calls() if c.startswith("launchctl")][3:] == [f"launchctl bootout {domain}/ai.dreamfort.ghost", calls[-1]]


def test_services_on_linux(machine):
    m = machine()
    units = m.home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    for unit in OLD_UNITS:
        (units / f"{unit}.service").write_text("old")
    assert m.install().returncode == 0
    assert [p.name for p in units.iterdir()] == ["ghost-bridge.service"]
    assert f"Environment=GHOST_HOME={m.ghost}\nExecStart={m.ghost}/venv/bin/python -Es {m.ghost}/privacy/" in (units / "ghost-bridge.service").read_text()
    calls = m.calls()
    for call in (*(f"systemctl --user disable --now {u}.service" for u in OLD_UNITS), "systemctl --user restart ghost-bridge.service"):
        assert call in calls


def test_without_systemd_it_still_installs(machine):
    m = machine()
    (m.home / "no-systemd").touch()
    assert m.install().returncode == 0
    assert not (m.home / ".config" / "systemd").exists() and any(c.startswith("pkill") for c in m.calls())


def test_options_are_remembered_until_switched_off(machine):
    m = machine()
    saved = m.ghost / ".install-env"
    for env, expect in (({"GHOST_NO_BROWSER": "1"}, "GHOST_NO_BROWSER=1\n"), ({}, "GHOST_NO_BROWSER=1\n"), ({"GHOST_NO_BROWSER": "0"}, ""), ({}, "")):
        assert m.install(**env).returncode == 0 and saved.read_text() == expect
    builds = [c for c in m.calls() if c.startswith("fork-engine  ")]
    assert [c.rpartition("=")[2] for c in builds] == ["1", "1", "yes", "yes"]


def test_what_older_releases_left_behind_is_removed(machine):
    m = machine()
    m.install()
    privacy = m.ghost / "privacy"
    for name in ("scrubbing_proxy.py", "presidio_scrub.py", "scrub_patterns.py", "ensure_scrubber_route.py", ".presidio", ".pass_paths"):
        (privacy / name).touch()
    (privacy / "pii_denylist.txt").write_text("my terms")
    assert "Redaction was removed" not in m.install().stdout
    assert sorted(p.name for p in privacy.iterdir() if p.suffix == ".py") == ["bridge.py", "ghost_auth.py", "ghost_login.py", "launch.py", "spend.py"]
    assert not (privacy / ".presidio").exists() and (privacy / "pii_denylist.txt").read_text() == "my terms"   # yours


def test_someone_who_had_redaction_on_is_told_it_is_gone(machine):
    m = machine()
    m.install()
    (m.ghost / "privacy" / ".scrub").touch()
    assert "Redaction was removed. What you send is sealed to a TEE enclave, but no longer redacted first." in m.install().stdout
    assert "Redaction was removed" not in m.install().stdout   # once


def test_your_edits_are_kept_beside_the_new_files(machine):
    m = machine()
    m.install()
    for name in ("config.yaml", "SOUL.md"):
        with open(m.profile / name, "a") as fh:
            fh.write("# mine\n")
    done = m.install()
    for name in ("config.yaml", "SOUL.md"):
        assert f"Your previous {name} is kept as {m.profile / name}.prev" in done.stdout
        assert "# mine" in (m.profile / f"{name}.prev").read_text() and "# mine" not in (m.profile / name).read_text()
    assert "previous" not in m.install().stdout and "# mine" in (m.profile / "config.yaml.prev").read_text()


@pytest.mark.parametrize("verbose", ["", "1"])
def test_a_failure_says_which_step_and_why(machine, verbose):
    m = machine()
    (m.home / "engine-broken").touch()
    done = m.install(GHOST_VERBOSE=verbose)
    assert done.returncode == 3 and "✗ Building the engine" in done.stdout and "the reason it broke" in done.stdout
    assert "Writing your profile" not in done.stdout and not (m.commands / "ghost").exists()
    if not verbose:
        assert f"The whole log is in {m.ghost}/install.log" in done.stdout


def test_nothing_changes_under_an_open_ghost(machine):
    m = machine()
    m.install()
    (m.home / "ghost-open").touch()
    before = m.calls()
    done = m.install()
    assert done.returncode == 1 and "✗ ghost is open (pid 42). Close those windows" in done.stdout
    assert m.calls() == before + ["fork-engine --check browser=yes"]


def test_a_failed_check_is_not_taken_for_an_open_ghost(machine):
    m = machine()
    (m.repo / "scripts" / "fork-engine.sh").write_text("#!/bin/bash\necho something else broke >&2\nexit 1\n")
    done = m.install()
    assert done.returncode == 1 and "✗ Checking this machine" in done.stdout and "something else broke" in done.stdout


def test_path_hint(machine):
    m = machine()
    assert "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc" in m.install().stdout
    (m.home / ".zshrc").write_text('export PATH="$HOME/.local/bin:$PATH"\n')
    assert "Open a new terminal first" in m.install().stdout
    assert "PATH" not in m.install(PATH=f"{m.commands}:{m.bin}:{os.environ['PATH']}").stdout


def test_a_login_survives_an_update(machine):
    m = machine()
    m.install()
    key = m.ghost / "privacy" / "dreamfort_api_key"
    key.write_text("df_sk_abcd\n")
    assert "Logged in with your DreamFort API key (df_sk_…abcd)" in m.install().stdout and key.exists()


# ── updating ──────────────────────────────────────────────────────────────────
def git(directory, *args):
    subprocess.run(["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.com", *args], check=True, capture_output=True)


@pytest.fixture
def released(machine):
    """A ghost installed from its own copy of the source, with a newer release waiting."""
    m = machine()
    m.install()
    upstream, managed = m.home / "upstream", m.home / ".ghost-src"
    upstream.mkdir()
    (upstream / "install.sh").write_text('echo "old installer"\n')
    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "old")
    subprocess.run(["git", "clone", "-q", str(upstream), str(managed)], check=True)
    (upstream / "install.sh").write_text('echo "new installer $GHOST_HOME $GHOST_ENGINE"\n')
    git(upstream, "commit", "-qam", "new")
    (m.ghost / ".src").write_text(f"{managed}\n")
    return m, managed


@pytest.mark.parametrize("command", [("ghost", "update"), ("ghost-update",)])
def test_update_installs_the_newest_release(released, command):
    m, managed = released
    git(managed, "checkout", "-qb", "somewhere-else")   # left behind on a branch that was merged long ago
    done = m.command(*command)
    assert done.stdout == f"new installer {m.ghost} {m.engine}\n", done.stderr


def test_update_never_throws_away_your_changes(released):
    m, managed = released
    (managed / "install.sh").write_text("mine\n")
    done = m.command("ghost-update")
    assert done.returncode == 1 and "has changes of yours" in done.stderr and (managed / "install.sh").read_text() == "mine\n"


# ── uninstalling ──────────────────────────────────────────────────────────────
@pytest.fixture
def installed(machine):
    m = machine("Darwin" if sys.platform == "darwin" else "Linux")
    m.install()
    (m.profile / "state.db").write_text("chats")
    session = m.home / ".opengradient" / "veil" / "session.json"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"access_token": "t"}))
    return m, session


def test_uninstall_keeps_your_data(installed):
    m, session = installed
    done = m.command("ghost", "uninstall", "--yes")
    assert done.returncode == 0 and "still in" in done.stdout
    assert not m.engine.exists() and not any((m.commands / c).exists() for c in ("ghost", "ghost-login", "ghost-update", "ghost-uninstall"))
    assert not (m.home / "Library/LaunchAgents/ai.dreamfort.ghost.plist").exists()
    assert not (m.home / ".config/systemd/user/ghost-bridge.service").exists()
    assert not (m.ghost / "venv").exists() and not (m.ghost / "privacy").exists()
    assert (m.profile / "state.db").read_text() == "chats" and session.exists() and m.repo.exists()
    assert "engine -p uncensored gateway stop" in m.calls()
    assert m.install().returncode == 0 and (m.profile / "state.db").read_text() == "chats"   # and back again


def test_purge_removes_everything(installed):
    m, session = installed
    assert m.command("ghost-uninstall", "--yes", "--purge").returncode == 0
    assert not m.ghost.exists() and not m.engine.exists() and not session.exists()


def test_uninstall_without_a_terminal_needs_yes(installed):
    m, _ = installed
    done = m.command("ghost", "uninstall")
    assert "Nothing was changed" in done.stdout and m.engine.exists() and (m.commands / "ghost").exists()
    assert m.command("ghost", "uninstall", "--everything").returncode == 2 and m.engine.exists()


def test_uninstall_leaves_what_is_not_ghosts(installed):
    m, _ = installed
    (m.engine / "install-stamp.json").write_text('{"distribution": "someone else"}')
    (m.commands / "ghost-login").write_text("#!/bin/sh\necho not yours\n")
    done = m.command("ghost-uninstall", "--yes")
    assert m.engine.exists() and "not an engine ghost built" in done.stdout and (m.commands / "ghost-login").exists()
    assert "isn't installed here" in m.run(BASH, m.repo / "bin" / "ghost-uninstall", "--yes").stdout
