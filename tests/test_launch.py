"""The launcher: what runs the engine, what asks for a login first, and what must contact nobody."""
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "privacy"))
import launch  # noqa: E402

HOSTED = """\
model:
  base_url: http://127.0.0.1:8788/v1
  # the default
  default: abliteration/abliterated-model-large-v2
fallback_model:
  provider: opengradient
  model: nous/hermes-4-405b
providers:
  opengradient:
    default_model: something/else
  ollama-local:
    base_url: http://localhost:11434/v1
    api_key: ollama
    default_model: ghost-tool:latest
"""
OFFLINE = HOSTED.replace("  provider: opengradient\n  model: nous/hermes-4-405b", "  provider: ollama-local\n  model: ghost-tool:latest")


class Box:
    """A ghost install with a stand-in for the engine, the bridge's address and the login."""

    def __init__(self, root, monkeypatch):
        self.root, self.ran, self.started, self.logins = root, [], [], []
        self.profile = root / "profiles" / "uncensored"
        self.profile.mkdir(parents=True)
        (root / "privacy").mkdir()
        self.configure(HOSTED)
        for name, value in (("HOME", root), ("PRIV", root / "privacy"), ("PROFILE", self.profile), ("ENGINE", root / "engine")):
            monkeypatch.setattr(launch, name, value)
        monkeypatch.setattr(launch, "run_engine", lambda args: self.ran.append(list(args)) or 0)
        monkeypatch.setattr(launch, "ensure_bridge", lambda: self.started.append(1) or True)
        monkeypatch.setattr(launch.ghost_auth, "KEY_FILE", root / "privacy" / "dreamfort_api_key")
        monkeypatch.setattr(launch.ghost_auth, "SESSION_FILE", root / "veil" / "session.json")
        monkeypatch.setattr(launch.ghost_auth, "_cached", {})
        monkeypatch.setattr(launch.spend, "_entries", [])

    def configure(self, text):
        (self.profile / "config.yaml").write_text(text)

    def log_in(self):
        (self.root / "privacy" / "dreamfort_api_key").write_text("df_sk_abcd\n")


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def terminal(monkeypatch, answer=None):
    """Pretend to be at a terminal, where logging in answers `answer`."""
    import ghost_login
    calls = []

    def main(args):
        calls.append(args)
        return answer
    monkeypatch.setattr(ghost_login, "main", main)
    for stream in (sys.stdin, sys.stdout):
        monkeypatch.setattr(stream, "isatty", lambda: True, raising=False)
    return calls


# ── before you log in ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("args", [[], ["-z", "hi"], ["chat"], ["-c"], ["--resume", "x"]])
def test_a_chat_needs_a_login_and_says_so_at_once(box, capsys, args):
    started = time.time()
    assert launch.main(args) == 1
    assert "not logged in. Run: ghost login" in capsys.readouterr().err
    assert time.time() - started < 2 and (box.ran, box.started) == ([], [])


@pytest.mark.parametrize("args", [["--version"], ["-V"], ["sessions", "list"], ["gateway", "status"], ["cron", "list"]])
def test_everything_else_works_without_one(box, args):
    assert launch.main(args) == 0
    assert (box.ran, box.started) == ([args], [])   # and contacts nobody, not even the bridge


def test_a_terminal_is_asked_to_log_in_first(box, monkeypatch):
    calls = terminal(monkeypatch, answer=0)
    assert launch.main(["-z", "hi"]) == 0
    assert (calls, box.ran) == ([["--first-run"]], [["-z", "hi"]])


@pytest.mark.parametrize("code", [1, 130])
def test_no_login_no_chat(box, monkeypatch, code):
    terminal(monkeypatch, answer=code)
    assert launch.main([]) == code and box.ran == []


def test_with_an_offline_model_it_is_offered_and_answers_scripts(box, monkeypatch):
    box.configure(OFFLINE)
    assert launch.main(["-z", "hi"]) == 0 and box.ran == [["-z", "hi"]]
    calls = terminal(monkeypatch, answer=launch_local())
    monkeypatch.setattr(launch, "run_local", lambda args, config: box.ran.append(["local", *args]) or 0)
    assert launch.main(["-z", "hi"]) == 0
    assert (calls, box.ran[-1]) == ([["--first-run", "--local-model"]], ["local", "-z", "hi"])


def launch_local():
    import ghost_login
    return ghost_login.LOCAL


# ── logged in ─────────────────────────────────────────────────────────────────
def test_a_chat_starts_the_bridge_then_the_engine(box):
    box.log_in()
    assert launch.main(["-z", "hi"]) == 0 and (box.ran, box.started) == ([["-z", "hi"]], [1])


def test_a_dead_bridge_is_reported_and_the_engine_still_runs(box, monkeypatch, capsys):
    box.log_in()
    monkeypatch.setattr(launch, "ensure_bridge", lambda: False)
    monkeypatch.setattr(launch, "port_taken", lambda: False)
    assert launch.main([]) == 0 and box.ran == [[]]
    assert "background service isn't running" in capsys.readouterr().err


def test_another_program_on_the_bridges_port_never_gets_a_prompt(box, monkeypatch, capsys):
    box.log_in()
    monkeypatch.setattr(launch, "ensure_bridge", lambda: False)
    monkeypatch.setattr(launch, "port_taken", lambda: True)
    assert launch.main(["-z", "a secret"]) == 1 and box.ran == []
    assert "another program is using port 8788" in capsys.readouterr().err


def test_status_line(box):
    box.log_in()
    launch.spend._entries.append((time.time(), 0.21))
    assert launch.status_line(HOSTED, []) == ("abliterated-model-large-v2 · sealed to a TEE · DreamFort API key (df_sk_…abcd) · "
                                              "$0.21 in the last 24h")


@pytest.mark.parametrize("flag", ["--scrub", "--preview"])
def test_asking_for_redaction_says_it_is_gone(box, capsys, flag):
    """It was removed; carrying on as if it were still there is the one thing that must not happen."""
    box.log_in()
    assert launch.main([flag, "-z", "hi"]) == 2
    assert "redaction was removed" in capsys.readouterr().err and box.ran == []
    assert launch.main(["--no-scrub", "--paths", "-z", "hi"]) == 0 and box.ran == [["-z", "hi"]]


# ── the offline model ─────────────────────────────────────────────────────────
def test_local_contacts_nobody(box, monkeypatch, capsys):
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "ghost-tool:latest  6GB", ""))
    assert launch.main(["--local", "-z", "hi"]) == 0
    assert box.ran == [["-m", "ghost-tool:latest", "--provider", "ollama-local", "-z", "hi"]] and box.started == []
    box.configure(HOSTED[:HOSTED.index("  ollama-local:")])   # Ollama is there, but ghost wasn't installed with it
    assert launch.main(["--local"]) == 1 and "no local model installed" in capsys.readouterr().err
    box.configure(HOSTED)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert launch.main(["--local"]) == 1 and "no local model installed" in capsys.readouterr().err


# ── config.yaml, without a YAML parser ────────────────────────────────────────
def test_settings_are_read_from_the_shipped_profile():
    shipped = (Path(__file__).resolve().parent.parent / "profile" / "config.yaml").read_text(encoding="utf-8")
    for text, offline in ((shipped, True), (HOSTED, False), (OFFLINE, True)):
        assert launch.default_model(text) == "abliterated-model-large-v2" and launch.local_fallback(text) is offline
    assert launch.local_model(HOSTED) == "ghost-tool:latest"
    assert launch.default_model("") == "ghost" and launch.local_fallback("") is False


def test_a_refreshed_nous_login_stays_on_the_bridge(box):
    path = box.profile / "auth.json"
    path.write_text('{"providers": [{"nous": {"inference_base_url": "https://inference.example/v1", "token": "t"}}]}')
    launch.pin_route()
    assert "https://inference.example" not in path.read_text() and f"http://127.0.0.1:{launch.PORT}/v1" in path.read_text()
    for damaged in ("", "{", "3"):
        path.write_text(damaged)
        launch.pin_route()


# ── the bridge ────────────────────────────────────────────────────────────────
@pytest.fixture
def real(tmp_path, monkeypatch):
    """The real ensure_bridge, with the service manager and the process start replaced."""
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, nonce = self.path.partition("?")
            self.send_response(200 if path == "/healthz" else 404)
            if proves:
                self.send_header("X-Ghost-Proof", launch.ghost_auth.bridge_proof(nonce))
            self.end_headers()

        def log_message(self, *args):
            pass

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    monkeypatch.setattr(launch, "PORT", port)
    monkeypatch.setattr(launch, "PRIV", tmp_path)
    monkeypatch.setattr(launch.ghost_auth, "BRIDGE_KEY_FILE", tmp_path / "bridge.key")
    calls, servers, proves = [], [], [1]

    def listen():
        servers.append(ThreadingHTTPServer(("127.0.0.1", port), Health))
        threading.Thread(target=servers[0].serve_forever, daemon=True).start()

    def serve(name, ok=True):
        def start():
            calls.append(name)
            if ok:
                listen()
            return ok
        return start
    setup = SimpleNamespace(listen=listen, calls=calls, proves=proves,
                            start=lambda **starts: [monkeypatch.setattr(launch, k, serve(k, v)) for k, v in starts.items()])
    yield setup
    for server in servers:
        server.shutdown()
        server.server_close()


def test_bridge_is_started_by_the_service_manager(real):
    real.start(_start_service=True, _start_process=True)
    assert launch.ensure_bridge() and real.calls == ["_start_service"]
    assert launch.ensure_bridge() and real.calls == ["_start_service"]   # it's up now: nothing to start


def test_bridge_is_started_by_us_where_there_is_no_service_manager(real):
    real.start(_start_service=False, _start_process=True)
    assert launch.ensure_bridge() and real.calls == ["_start_service", "_start_process"]


def test_a_bridge_that_is_not_yours_is_not_the_bridge(real):
    """Another program, or the ghost of someone else on this machine: /healthz says ok, and that proves nothing."""
    real.start(_start_service=True, _start_process=True)
    real.proves.clear()
    real.listen()
    assert not launch.ensure_bridge() and real.calls == [] and "another program" in launch.bridge_problem()
    real.proves.append(1)
    assert launch.ensure_bridge() and real.calls == []
    key = launch.ghost_auth.BRIDGE_KEY_FILE
    assert len(key.read_text()) == 64 and (os.name == "nt" or key.stat().st_mode & 0o77 == 0)


def test_our_own_bridge_process(tmp_path, monkeypatch):
    """The real _start_process, on a stand-in bridge: it must outlive us and leave its output in the log."""
    (tmp_path / "bridge.py").write_text("import sys, time\nprint('up', flush=True)\ntime.sleep(5)\n")
    monkeypatch.setattr(launch, "PRIV", tmp_path)
    monkeypatch.setattr(launch, "HOME", tmp_path)
    launch._start_process()
    deadline = time.time() + 10
    while not (tmp_path / "bridge.log").read_text().startswith("up") and os.name != "nt":   # pythonw has no output
        assert time.time() < deadline
        time.sleep(0.1)


# ── the real thing: the engine is exec'd with ghost's own state ───────────────
@pytest.mark.skipif(os.name == "nt", reason="the engine's Windows launcher is an .exe")
def test_the_engine_gets_ghosts_profile_and_not_your_api_key(tmp_path):
    engine = tmp_path / "engine" / ".hermes" / "bin" / "hermes"
    engine.parent.mkdir(parents=True)
    engine.write_text('#!/bin/sh\necho "$HERMES_HOME|$ANTHROPIC_API_KEY|$*"\nexit 7\n')
    engine.chmod(0o755)
    env = {**os.environ, "GHOST_HOME": str(tmp_path / "state"), "GHOST_ENGINE": str(tmp_path / "engine"),
           "ANTHROPIC_API_KEY": "sk-secret", "HERMES_HOME": "/somewhere/else"}
    done = subprocess.run([sys.executable, "-Es", launch.__file__, "sessions", "list", "a b"], env=env, capture_output=True, text=True, timeout=30)
    assert (done.returncode, done.stdout) == (7, f"{tmp_path / 'state'}||-p uncensored sessions list a b\n")


def test_a_missing_engine_is_explained(tmp_path):
    env = {**os.environ, "GHOST_HOME": str(tmp_path), "GHOST_ENGINE": str(tmp_path / "none")}
    done = subprocess.run([sys.executable, "-Es", launch.__file__, "--version"], env=env, capture_output=True, timeout=30)
    assert done.returncode == 1 and "ghost update" in done.stderr.decode("utf-8", "replace")


def test_commands_of_the_engine_that_would_leave_the_private_path_are_ghosts_own(box, monkeypatch):
    """Its setup offers other providers, and its doctor contacts 44 vendors."""
    import doctor
    import ghost_login
    monkeypatch.setattr(doctor, "main", lambda: 41)
    monkeypatch.setattr(ghost_login, "main", lambda args: 42)
    assert (launch.main(["doctor"]), launch.main(["setup"]), box.ran) == (41, 42, [])


def test_help_names_the_commands(capsys):
    assert launch.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert all(word in out for word in ("ghost login", "ghost usage", "ghost update", "ghost uninstall", "ghost --local"))
