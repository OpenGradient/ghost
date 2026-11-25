"""Logging in, with an account (code, browser, paste) or an API key, and what the bridge then sends as whom."""
import base64
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PRIVACY = Path(__file__).resolve().parent.parent / "privacy"
sys.path.insert(0, str(PRIVACY))
pytest.importorskip("httpx")
import ghost_auth  # noqa: E402
import ghost_login  # noqa: E402

GOOD_KEY, BAD_KEY = "df_sk_good1234", "df_sk_revoked"


class DreamFort:
    """The login endpoints of DreamFort, and Supabase's token renewal. Records every request."""

    def __init__(self):
        self.seen, self.device, self.polls_before_approval, self.credits, self.renewals = [], True, 2, True, 200
        self.errors, self.relay, self.balance = [], 200, 12.5
        dream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, body=None):
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                dream.seen.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path.endswith(("/credits/balance", "/chat/ohttp/config")):
                    ok = self.headers.get("Authorization") in ("Bearer token", "Bearer renewed") and dream.relay == 200
                    return self._answer(200 if ok else dream.relay if dream.relay != 200 else 401, {"balanceUsd": dream.balance})
                good = self.headers.get("Authorization") == f"Bearer {GOOD_KEY}"
                self._answer(200 if self.path.endswith("/developer/ohttp/config") and good else 401)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                dream.seen.append(("POST", self.path, body.decode("utf-8", "replace")))
                if self.path.endswith("/device/code"):
                    return self._answer(*((200, dream.handshake) if dream.device else (404, {"detail": "Not Found"})))
                if self.path.endswith("/device/token"):
                    if dream.errors:
                        return self._answer(400, {"error": dream.errors.pop(0)})
                    dream.polls_before_approval -= 1
                    return self._answer(*((400, {"error": "authorization_pending"}) if dream.polls_before_approval >= 0
                                          else (200, dream.bundle())))
                if self.path.endswith("/developer/ohttp"):
                    return self._answer(400 if dream.credits else 402)
                if "/auth/v1/token" in self.path:
                    return self._answer(dream.renewals, {"access_token": "renewed", "refresh_token": "next", "expires_in": 3600})
                self._answer(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.handshake = {"deviceCode": "d" * 43, "userCode": "WDJD-4FQK", "expiresIn": 600, "interval": 0,
                          "verificationUri": "https://www.dreamfort.ai/cli-auth/device"}

    def bundle(self, **extra):
        return {"type": "opengradient-cli-auth", "version": 1, "access_token": "token", "refresh_token": "refresh",
                "expires_at": int(time.time()) + 3600, "user": {"email": "mara@example.com"},
                "config": {"supabase_url": self.url, "supabase_anon_key": "anon", "chat_api_base_url": self.url,
                           "tee_registry_rpc_url": "https://rpc"}, **extra}


@pytest.fixture
def dream(tmp_path, monkeypatch):
    server = DreamFort()
    monkeypatch.setattr(ghost_auth, "CHAT_API_URL", server.url)
    monkeypatch.setattr(ghost_auth, "KEY_FILE", tmp_path / "home" / "dreamfort_api_key")
    monkeypatch.setattr(ghost_auth, "SESSION_FILE", tmp_path / "veil" / "session.json")
    monkeypatch.setattr(ghost_auth, "_cached", {})
    monkeypatch.setattr(ghost_login, "open_browser", lambda url: None)
    monkeypatch.setattr(ghost_login, "MIN_POLL", 0.05)
    monkeypatch.setattr(ghost_login, "TTY", False)
    monkeypatch.setattr(ghost_login, "COLOR", False)
    yield server
    server.server.shutdown()
    server.server.server_close()


def private(path):
    return os.name == "nt" or stat.S_IMODE(path.stat().st_mode) == 0o600


# ── account ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("flag", ["--account", "--device"])
def test_account_login_with_a_code(dream, capsys, flag):
    assert ghost_login.main([flag]) == 0
    out = capsys.readouterr().out
    assert "WDJD-4FQK" in out and "https://www.dreamfort.ai/cli-auth/device" in out
    assert "Logged in with your DreamFort account (mara@example.com)" in out
    assert ghost_auth.mode() == "account" and private(ghost_auth.SESSION_FILE)
    assert json.loads(dream.seen[-1][2]) == {"deviceCode": "d" * 43}


def test_a_code_nobody_approves_in_time(dream, capsys):
    dream.errors = ["authorization_pending", "expired_token"]
    assert ghost_login.main(["--device"]) == 1
    assert "expired before it was approved" in capsys.readouterr().out and ghost_auth.mode() is None


def test_without_code_logins_the_browser_is_used(dream, monkeypatch, capsys):
    dream.device = False
    assert ghost_login.main(["--device"]) == 1 and "doesn't offer code logins yet" in capsys.readouterr().out
    used = []
    monkeypatch.setattr(ghost_login, "browser_login", lambda **kw: used.append(kw) or dream.bundle())
    assert ghost_login.main(["--account"]) == 0
    assert used == [{}] and ghost_auth.mode() == "account"


def test_the_website_hands_the_login_back(dream, monkeypatch, capsys):
    """As the /cli-auth page does it: a cross-origin JSON POST, which browsers check with OPTIONS first."""
    results = []

    def website(url):
        callback = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["redirect_uri"][0]
        assert url.startswith("https://www.dreamfort.ai/cli-auth?") and callback.startswith("http://127.0.0.1:")

        def post(origin, body):
            req = urllib.request.Request(callback, json.dumps(body).encode(), {"Content-Type": "application/json", "Origin": origin})
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        preflight = urllib.request.urlopen(urllib.request.Request(callback, method="OPTIONS"), timeout=5)
        results.extend([preflight.headers["Access-Control-Allow-Origin"], preflight.headers["Access-Control-Allow-Private-Network"]])
        results.append(post("https://evil.example", dream.bundle(user={"email": "evil@example.com"})))
        results.append(post("https://www.dreamfort.ai", dream.bundle()))

    monkeypatch.setattr(ghost_login, "open_browser", lambda url: threading.Thread(target=website, args=(url,)).start())
    assert ghost_login.main(["--browser"]) == 0
    assert results[:3] == ["https://www.dreamfort.ai", "true", 403]
    assert ghost_auth.account()["user"]["email"] == "mara@example.com"


def test_pasted_logins(dream):
    text = json.dumps(dream.bundle(access_token="x" * 1500), indent=2)
    for pasted in (text, f"\x1b[200~{text}\x1b[201~\r", base64.b64encode(text.encode()).decode(), "  " + text + "\n"):
        assert ghost_login.parse_pasted(pasted)["access_token"] == "x" * 1500
    for unfinished in ("", "hello", text[:700], base64.b64encode(text.encode()).decode()[:701]):
        assert ghost_login.parse_pasted(unfinished) is None


@pytest.mark.parametrize("wrong", [{}, {"type": "something-else"}, {"type": "opengradient-cli-auth"},
                                   {"type": "opengradient-cli-auth", "access_token": "t", "config": {}}, [], None])
def test_only_a_dreamfort_login_is_stored(dream, wrong):
    with pytest.raises(ghost_auth.AuthError):
        ghost_auth.save_account(wrong)
    assert ghost_auth.mode() is None and not ghost_auth.SESSION_FILE.exists()


def test_an_account_is_sent_to_the_consumer_relay(dream):
    ghost_auth.save_account(dream.bundle())
    assert ghost_auth.credentials() == (dream.url + "/api/v1/chat/ohttp", "token")
    assert dream.seen == []   # a token that is still good is not renewed


def test_a_login_cannot_choose_the_relay_or_the_enclaves(dream):
    """Whoever approves a code decides what's in the login. At most that should be an account."""
    ghost_auth.save_account(dream.bundle(config={"supabase_url": dream.url, "chat_api_base_url": "https://evil.example",
                                                 "tee_registry_rpc_url": "https://evil.example", "tee_registry_address": "0xevil"}))
    assert ghost_auth.credentials() == (dream.url + "/api/v1/chat/ohttp", "token")


def test_an_expired_token_is_renewed_once(dream):
    ghost_auth.save_account(dream.bundle(expires_at=int(time.time()) + 30))
    assert [ghost_auth.credentials()[1] for _ in range(3)] == ["renewed"] * 3
    renewals = [s for s in dream.seen if "/auth/v1/token" in s[1]]
    assert len(renewals) == 1 and json.loads(renewals[0][2]) == {"refresh_token": "refresh"}
    kept = json.loads(ghost_auth.SESSION_FILE.read_text())
    assert (kept["refresh_token"], kept["user"]["email"]) == ("next", "mara@example.com") and private(ghost_auth.SESSION_FILE)


def test_a_login_that_can_no_longer_be_renewed_says_so(dream):
    dream.renewals = 400
    ghost_auth.save_account(dream.bundle(expires_at=1))
    with pytest.raises(ghost_auth.AuthError, match="has expired; run `ghost-login`"):
        ghost_auth.credentials()


def test_a_renewal_another_process_made_is_used(dream, monkeypatch):
    ghost_auth.save_account(dream.bundle(expires_at=1))
    stale = ghost_auth.account()
    ghost_auth.save_account(dream.bundle(access_token="theirs"))
    dream.renewals = 400   # ours is retired by theirs
    assert ghost_auth._refresh(stale)["access_token"] == "theirs"


def test_no_connection_is_not_an_expired_login(dream):
    ghost_auth.save_account(dream.bundle(expires_at=1, config={"supabase_url": "http://127.0.0.1:9"}))
    with pytest.raises(ghost_auth.AuthError, match="couldn't reach DreamFort"):
        ghost_auth.credentials()


def test_a_damaged_session_file_is_no_login(dream):
    ghost_auth.SESSION_FILE.parent.mkdir(parents=True)
    for text in ("", "{", "[]", "{}", '"x"'):
        ghost_auth.SESSION_FILE.write_text(text)
        ghost_auth._cached.clear()
        assert ghost_auth.mode() is None
    with pytest.raises(ghost_auth.AuthError, match="not logged in"):
        ghost_auth.credentials()


# ── API key ───────────────────────────────────────────────────────────────────
def test_api_key_from_stdin(dream, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(GOOD_KEY + "\n"))
    assert ghost_login.main(["--with-api-key"]) == 0
    out = capsys.readouterr().out
    assert "Logged in with your DreamFort API key (df_sk_…1234)" in out and GOOD_KEY not in out
    assert private(ghost_auth.KEY_FILE)
    assert ghost_auth.credentials() == (dream.url + "/api/v1/developer/ohttp", GOOD_KEY)


@pytest.mark.parametrize("key, words", [(BAD_KEY, "doesn't accept that key"), ("sk-other", "start with df_sk_"), ("", "start with df_sk_")])
def test_a_key_that_does_not_work_is_not_stored(dream, monkeypatch, capsys, key, words):
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    assert ghost_login.main(["--with-api-key"]) == 1
    assert words in capsys.readouterr().out and ghost_auth.mode() is None


def test_a_key_without_credits_says_so(dream, monkeypatch, capsys):
    dream.credits = False
    monkeypatch.setattr(sys, "stdin", io.StringIO(GOOD_KEY))
    assert ghost_login.main(["--with-api-key"]) == 0
    assert "no credits yet" in capsys.readouterr().out and ghost_auth.mode() == "api_key"


# ── switching, status, logout ─────────────────────────────────────────────────
def test_the_last_login_is_the_one_used(dream, monkeypatch):
    ghost_login.main(["--device"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(GOOD_KEY))
    ghost_login.main(["--with-api-key"])
    assert ghost_auth.mode() == "api_key"
    dream.polls_before_approval = 0
    ghost_login.main(["--device"])
    assert ghost_auth.mode() == "account" and not ghost_auth.KEY_FILE.exists()


def test_status_and_logout_contact_nobody(dream, capsys):
    assert ghost_login.main(["--status"]) == 1 and "Not logged in" in capsys.readouterr().out
    ghost_auth.save_account(dream.bundle(expires_at=1))
    assert ghost_login.main(["--status"]) == 0 and "mara@example.com" in capsys.readouterr().out
    assert ghost_login.main(["--logout"]) == 0 and "Logged out of your DreamFort account" in capsys.readouterr().out
    assert ghost_login.main(["--logout"]) == 0 and "weren't logged in" in capsys.readouterr().out
    assert ghost_auth.mode() is None and dream.seen == []


def test_a_script_is_told_what_to_do(dream, capsys):
    assert ghost_login.main([]) == 1
    assert "--with-api-key" in capsys.readouterr().err and dream.seen == []


def test_output_survives_an_ascii_locale(dream, tmp_path):
    env = {**os.environ, "GHOST_HOME": str(tmp_path), "OG_VEIL_HOME": str(tmp_path / "v"), "GHOST_CHAT_API_URL": dream.url, "PYTHONIOENCODING": "ascii"}
    done = subprocess.run([sys.executable, str(PRIVACY / "ghost_login.py"), "--with-api-key"], input=BAD_KEY.encode(), env=env, capture_output=True, timeout=30)
    assert done.returncode == 1 and "✗ DreamFort doesn't accept that key" in done.stdout.decode("utf-8") and done.stderr == b""


# ── ghost doctor, ghost usage ─────────────────────────────────────────────────
@pytest.fixture
def machine(dream, tmp_path, monkeypatch):
    """Everything on this machine in order, unless a test says otherwise."""
    import doctor
    import launch
    import spend
    binary = tmp_path / "engine.py"
    binary.write_text("print('Ghost v9 (stub)')")
    monkeypatch.setattr(launch, "engine_binary", lambda: binary)
    run = subprocess.run
    monkeypatch.setattr(doctor.subprocess, "run", lambda cmd, **kw: run([sys.executable, *cmd], **kw))
    monkeypatch.setattr(launch, "ensure_bridge", lambda: True)
    monkeypatch.setattr(spend, "_entries", [(time.time(), 0.21)])
    monkeypatch.setattr(spend, "BUDGET_FILE", tmp_path / "budget")
    monkeypatch.setattr(spend, "LEDGER", tmp_path / "spend.jsonl")
    return doctor


def test_doctor_when_all_is_well(dream, machine, capsys):
    ghost_auth.save_account(dream.bundle())
    assert machine.main() == 0
    out = capsys.readouterr().out
    for words in ("✓ Engine     Ghost v9 (stub)", "✓ Login      DreamFort account (mara@example.com)", "✓ Bridge     running", "✓ DreamFort  reachable",
                  "✓ Credits    $12.50 left", "✓ Spending   $0.21 of $5.00"):
        assert words in out
    assert [s[1] for s in dream.seen] == ["/api/v1/chat/ohttp/config", "/api/v1/credits/balance"]   # and no model


def test_doctor_contacts_nobody_before_you_log_in(dream, machine, capsys):
    assert machine.main() == 1
    out = capsys.readouterr().out
    assert "✗ Login      nobody is logged in  ->  ghost login" in out and "DreamFort " not in out and dream.seen == []


@pytest.mark.parametrize("wrong, words", [
    (lambda d, m: setattr(d, "relay", 401), "✗ DreamFort  doesn't accept this login any more  ->  ghost login"),
    (lambda d, m: setattr(d, "relay", 503), "✗ DreamFort  isn't well (503)"),
    (lambda d, m: m.setattr(ghost_auth, "CHAT_API_URL", "http://127.0.0.1:9"), "✗ DreamFort  can't be reached (ConnectError)"),
    (lambda d, m: setattr(d, "balance", 0), "✗ Credits    none left  ->  add some at https://www.dreamfort.ai/dashboard/credits"),
    (lambda d, m: m.setattr("launch.ensure_bridge", lambda: False), "✗ Bridge     isn't running"),
    (lambda d, m: m.setattr("launch.engine_binary", lambda: None), "✗ Engine     not installed  ->  ghost update"),
    (lambda d, m: m.setattr("spend._entries", [(time.time(), 6.0)]), "✗ Spending   $6.00 of $5.00 in the last hour  ->  ghost budget 10"),
])
def test_doctor_says_what_is_wrong_and_what_to_do(dream, machine, monkeypatch, capsys, wrong, words):
    ghost_auth.save_account(dream.bundle())
    wrong(dream, monkeypatch)
    assert machine.main() == 1 and words in capsys.readouterr().out


def test_usage_shows_what_is_left(dream, machine, capsys):
    import spend
    spend.main(["usage"])
    assert "Credits left" not in capsys.readouterr().out and dream.seen == []
    ghost_auth.save_account(dream.bundle())
    spend.main(["usage"])
    assert "Credits left   $   12.50" in capsys.readouterr().out
    dream.relay = 500
    assert spend.main(["usage"]) == 0 and "Credits left" not in capsys.readouterr().out


# ── in a terminal ─────────────────────────────────────────────────────────────
class Terminal:
    def __init__(self, dream, tmp_path, *args):
        import pty
        self.master, slave = pty.openpty()
        env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "TERM": "xterm", "GHOST_NO_OPEN": "1", "LANG": "en_US.UTF-8",
               "GHOST_HOME": str(tmp_path / ".ghost"), "OG_VEIL_HOME": str(tmp_path / "veil"), "GHOST_CHAT_API_URL": dream.url}
        self.proc = subprocess.Popen([sys.executable, str(PRIVACY / "ghost_login.py"), *args], stdin=slave, stdout=slave,
                                     stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        self.text = ""

    def read(self):
        import select
        if select.select([self.master], [], [], 0.2)[0]:
            try:
                chunk = os.read(self.master, 65536).decode("utf-8", "replace")
            except OSError:
                chunk = ""
            self.text += re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", chunk)

    def until(self, words, timeout=20):
        deadline = time.time() + timeout
        while words not in self.text:
            assert time.time() < deadline, f"never saw {words!r} in:\n{self.text}"
            self.read()
        return self

    def send(self, keys):
        os.write(self.master, keys.encode())
        return self

    def exit_code(self):
        deadline = time.time() + 20
        while self.proc.poll() is None and time.time() < deadline:
            self.read()   # a terminal keeps taking output
        os.close(self.master)
        return self.proc.poll()


@pytest.mark.skipif(os.name == "nt", reason="drives a Unix terminal")
def test_choosing_an_account(dream, tmp_path):
    term = Terminal(dream, tmp_path, "--first-run")
    term.until("Select login method").until("2. DreamFort API key").send("\r")
    term.until("WDJD-4FQK").until("Logged in with your DreamFort account (mara@example.com)").until("Press Enter").send("\r")
    assert term.exit_code() == 0 and (tmp_path / "veil" / "session.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="drives a Unix terminal")
def test_choosing_an_api_key(dream, tmp_path):
    term = Terminal(dream, tmp_path)
    term.until("2. DreamFort API key").send("\x1b[B")
    time.sleep(0.5)
    term.send("\r").until("https://api.dreamfort.ai/api-keys").until("DreamFort API key: ").send(BAD_KEY + "\r")
    term.until("doesn't accept that key").send(GOOD_KEY + "\r").until("Logged in with your DreamFort API key")
    assert term.exit_code() == 0 and GOOD_KEY not in term.text and BAD_KEY not in term.text


@pytest.mark.skipif(os.name == "nt", reason="drives a Unix terminal")
@pytest.mark.parametrize("keys, code, words", [("\x1b", 130, "Login cancelled"), ("3", 3, "Local model")])
def test_other_choices(dream, tmp_path, keys, code, words):
    term = Terminal(dream, tmp_path, "--local-model")
    term.until("3. Local model").send(keys)
    assert term.until(words).exit_code() == code and dream.seen == []
