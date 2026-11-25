"""The bridge must never pay twice for one request.

The relay bills every request the enclave accepts, in full, even if we hang up. These tests stand a fake
enclave behind the bridge that counts what it accepted, and check each way a reply can go wrong.
"""
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import httpx
import pytest

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "privacy"), os.path.dirname(__file__)]
pytest.importorskip("opengradient_ohttp")
import bridge as sp  # noqa: E402
import enclaves  # noqa: E402
import fake_dreamfort  # noqa: E402
import outbound  # noqa: E402
import spend  # noqa: E402

REAL_UPSTREAM = sp._upstream
MODEL = "abliteration/abliterated-model-large-v2"
TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {"type": "object", "properties": {}}}}]


def sse(*events, done=True):
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + ("data: [DONE]\n\n" if done else "")
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def closing(finish="stop", out=12, cost="0.001"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "model": "m", "tee_id": "0xabc",
            "usage": {"prompt_tokens": 100, "completion_tokens": out}, "opengradient": {"cost_usd": cost}}


def text(content):
    return {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}


def call(arguments, name="write_file"):
    return {"choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
        {"index": 0, "id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}


class Enclave:
    """Answers with each reply in turn (the last one repeats). `accepted` is what would be billed."""

    def __init__(self):
        self.replies, self.accepted, self.seen = [sse(text("hi"), closing())], [], 0

    def __call__(self, request):
        reply = self.replies[min(self.seen, len(self.replies) - 1)]
        self.seen += 1
        if isinstance(reply, Exception):
            raise reply
        if reply.status_code < 400:
            self.accepted.append(json.loads(request.content))
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)


@pytest.fixture
def enclave(monkeypatch):
    enclave = Enclave()
    client = httpx.Client(transport=httpx.MockTransport(enclave))
    monkeypatch.setattr(sp, "_upstream", lambda refused=None: (client, {"Authorization": "Bearer t"}))
    return enclave


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_backoff", lambda attempt: None)
    monkeypatch.setattr(sp, "_wait", lambda seconds: None)
    monkeypatch.setattr(sp, "_broke", {})
    monkeypatch.setattr(sp, "LOG", str(tmp_path / "log"))
    monkeypatch.setattr(sp.ghost_auth, "BRIDGE_KEY_FILE", tmp_path / "bridge.key")
    for name, value in (("DIR", tmp_path), ("LEDGER", tmp_path / "spend.jsonl"), ("BUDGET_FILE", tmp_path / "budget")):
        monkeypatch.setattr(spend, name, value)
    monkeypatch.setattr(spend, "_entries", None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    ask_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {sp.ghost_auth.bridge_token()}"}

    def ask(stream=True, headers=None, **extra):
        body = {"model": MODEL, "stream": stream, "messages": [{"role": "user", "content": "rewrite these scenes"}], **extra}
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                                     json.dumps(body).encode(), {**ask_headers, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]
    ask.url, ask.port, ask.headers = f"http://127.0.0.1:{server.server_port}", server.server_port, ask_headers
    yield ask
    server.shutdown()
    server.server_close()


# ── what reaches the enclave ──────────────────────────────────────────────────
@pytest.mark.parametrize("sent, expect", [
    ({}, (32000, "low")),                                             # the gateway's own cap is 4096
    ({"max_tokens": 65536}, (32000, "low")),                          # what the v0.18 engine asks for
    ({"max_completion_tokens": 900}, (900, "low")),
    ({"reasoning_effort": "none"}, (32000, "none")),
    ({"reasoning_effort": "high"}, (32000, "high")),
    ({"reasoning_effort": "minimal"}, (32000, "low")),                # the gateway rejects these two
    ({"reasoning_effort": "xhigh"}, (32000, "high")),
    ({"model": "nous/hermes-4-405b", "reasoning_effort": "high"}, (32000, "none")),
])
def test_every_request_carries_its_limits(enclave, bridge, sent, expect):
    assert bridge(**sent)[0] == 200
    got = enclave.accepted[0]
    assert (got["max_tokens"], got["reasoning_effort"]) == expect and "max_completion_tokens" not in got


@pytest.mark.parametrize("stream", [True, False])
def test_the_enclave_is_always_streamed(enclave, bridge, stream):
    # Its buffered form times out on long replies and "repairs" a truncated tool call into valid JSON.
    bridge(stream=stream)
    assert enclave.accepted[0]["stream"] is True


# ── good replies ──────────────────────────────────────────────────────────────
def test_streamed_reply(enclave, bridge):
    status, body = bridge()
    assert status == 200 and '"content": "hi"' in body and body.rstrip().endswith("data: [DONE]")


def test_single_reply_is_assembled(enclave, bridge):
    enclave.replies = [sse(text("two "), text("parts"), call('{"path":'), {"choices": [{"index": 0, "finish_reason": None, "delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": ' "a"}'}}]}}]}, closing("tool_calls"))]
    status, body = bridge(stream=False)
    choice = json.loads(body)["choices"][0]
    assert status == 200 and choice["message"]["content"] == "two parts" and choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"] == {"name": "write_file", "arguments": '{"path": "a"}'}


def test_a_long_answer_that_hits_the_limit_says_so(enclave, bridge):
    enclave.replies = [sse(text("first half"), closing("stop", out=500))]   # the gateway says "stop" even here
    assert '"finish_reason": "length"' in bridge(max_tokens=500)[1]


# ── replies that are billed but useless: paid for once, and said out loud ─────
def test_empty_reply(enclave, bridge):
    enclave.replies = [sse(closing("stop", out=32000, cost="0.16"))]
    status, message = bridge()
    assert status == 422 and "hidden reasoning" in message and len(enclave.accepted) == 1


def test_tool_call_cut_off_by_the_limit(enclave, bridge):
    enclave.replies = [sse(call('{"path": "scene.txt", "content": "Mara sai'), closing("tool_calls", out=32000))]
    status, message = bridge(tools=TOOLS)
    assert status == 422 and "nothing was run" in message and len(enclave.accepted) == 1


def test_connection_lost_after_acceptance_is_not_retried_here(enclave, bridge):
    enclave.replies = [sse(text("partial"), done=False)]
    first, second = bridge(), bridge()
    assert first[0] == 502 and second[0] == 422 and "twice in a row" in second[1]
    assert len(enclave.accepted) == 2   # one per request the engine made


def test_each_request_has_its_own_streak(enclave, bridge):
    """Three windows and one blip: none of them is on its second break. And one window's luck is not another's."""
    enclave.replies = [sse(text("partial"), done=False)]
    windows = [{"messages": [{"role": "user", "content": f"window {i}"}]} for i in range(3)]
    assert [bridge(**w)[0] for w in windows] == [502] * 3
    enclave.replies, enclave.seen = [sse(text("hi"), closing())], 0
    assert bridge(messages=[{"role": "user", "content": "window 4"}])[0] == 200
    enclave.replies, enclave.seen = [sse(text("partial"), done=False)], 0
    assert [bridge(**w)[0] for w in windows] == [422] * 3


def test_a_login_that_is_refused_is_renewed_once(monkeypatch, enclave, bridge):
    """This machine's clock may be behind, so an access token is only known to be old when it's turned away."""
    asked = []

    def upstream(refused=None):
        asked.append(refused)
        return httpx.Client(transport=httpx.MockTransport(enclave)), {"Authorization": "Bearer renewed" if refused else "Bearer old"}
    monkeypatch.setattr(sp, "_upstream", upstream)
    monkeypatch.setattr(sp.ghost_auth, "api_key", lambda: None)
    refusal = httpx.Response(401, json={"error": {"message": "x"}})
    enclave.replies = [refusal, sse(text("hi"), closing())]
    assert bridge()[0] == 200 and asked == [None, "old"]
    enclave.replies, enclave.seen, asked[:] = [refusal], 0, []
    assert bridge()[0] == 401 and asked == [None, "old"] and enclave.seen == 2


def test_a_login_that_cannot_be_renewed_just_now_is_not_called_expired(monkeypatch, enclave, bridge):
    def upstream(refused=None):
        raise sp.ghost_auth.Unavailable("couldn't reach DreamFort to renew your login (ConnectError)")
    monkeypatch.setattr(sp, "_upstream", upstream)
    status, message = bridge()
    assert status == 503 and "couldn't reach DreamFort" in message   # which the engine tries again, and 401 not


def test_nothing_is_paid_for_someone_who_has_gone(monkeypatch, enclave, bridge):
    """Ctrl-C while the bridge waits for a busy relay."""
    arrived, gone = threading.Event(), threading.Event()
    monkeypatch.setattr(sp, "_wait", lambda seconds: (arrived.set(), gone.wait(5)))
    enclave.replies = [httpx.Response(429, json={"error": "Rate limit exceeded"}), sse(text("hi"), closing())]
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "x"}]}).encode()
    sock = socket.create_connection(("127.0.0.1", bridge.port))
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + "".join(f"{k}: {v}\r\n" for k, v in bridge.headers.items()).encode()
                 + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    assert arrived.wait(5)
    sock.close()
    time.sleep(0.2)
    gone.set()
    time.sleep(0.5)
    assert enclave.seen == 1 and enclave.accepted == []
    with open(sp.LOG) as fh:
        assert "whoever asked has gone" in fh.read()


def test_a_login_page_is_not_an_enclave(enclave):
    """A hotel's, say: it answers 200 for everyone, and that's not a reply that was paid for."""
    for answer in (httpx.Response(200, html="<html>Welcome</html>"), httpx.Response(302, headers={"Location": "https://hotel.example"}),
                   httpx.Response(200, json={"ok": True})):
        relay = sp.Relay(httpx.MockTransport(lambda request, answer=answer: answer))
        got = relay.handle_request(httpx.Request("POST", "https://relay.example/x"))
        assert got.status_code == 502 and "answers instead of DreamFort" in got.json()["detail"]
    sealed = httpx.Response(200, headers={"content-type": "message/ohttp-chunked-res"}, content=b"x")
    assert sp.Relay(httpx.MockTransport(lambda request: sealed)).handle_request(httpx.Request("POST", "https://relay.example/x")) is sealed


# ── your name stays here ──────────────────────────────────────────────────────
def test_the_engines_own_prompt_does_not_name_you(monkeypatch, enclave, bridge):
    monkeypatch.setattr(sp, "_HOME", re.compile("(?:/Users/jane)(?![\\w-])(?!\\.\\w)"))
    prompt = "Home: /Users/jane\nFolder: /Users/jane/work, data /Users/jane/.ghost/x. Not /Users/janet/y or /Users/jane.doe/z. In /Users/jane."
    bridge(messages=[{"role": "system", "content": prompt}, {"role": "user", "content": "is /Users/jane/x mine?"}])
    system, user = enclave.accepted[0]["messages"]
    assert system["content"] == "Home: ~\nFolder: ~/work, data ~/.ghost/x. Not /Users/janet/y or /Users/jane.doe/z. In ~."
    assert user["content"] == "is /Users/jane/x mine?"   # yours, as you wrote it


def test_home_is_this_users(monkeypatch):
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", os.path.join(os.sep, "home", "mara"))
    assert os.path.expanduser("~") in sp._homes()


# ── not accepted, so not billed: safe to retry ────────────────────────────────
@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.Response(503, json={"error": {"message": "busy"}})])
def test_retries_what_was_never_accepted(enclave, bridge, failure):
    enclave.replies = [failure, failure, sse(text("hi"), closing())]
    assert bridge()[0] == 200 and enclave.seen == 3 and len(enclave.accepted) == 1


@pytest.mark.parametrize("failure, status, words", [
    (httpx.Response(401, json={"error": {"message": "x"}}), 401, "ghost-login"),
    (httpx.Response(402, json={"error": {"message": "x"}}), 402, "out of credits"),
    (httpx.Response(400, json={"error": {"message": "bad field"}}), 400, "bad field"),
    (httpx.ConnectError("refused"), 502, "couldn't reach DreamFort"),
])
def test_refusals_are_passed_on(enclave, bridge, failure, status, words):
    enclave.replies = [failure]
    got = bridge()
    assert got[0] == status and words in got[1] and enclave.accepted == []


def test_a_busy_relay_is_waited_out(enclave, bridge, monkeypatch):
    """It limits requests per minute, and a request it turned away was never accepted."""
    waits = []
    monkeypatch.setattr(sp, "_wait", waits.append)
    enclave.replies = [httpx.Response(429, json={"error": "Rate limit exceeded: 40 per 1 minute"})] * 3 + [sse(text("hi"), closing())]
    assert bridge()[0] == 200 and len(enclave.accepted) == 1 and waits == [5, 10, 20]
    enclave.replies, enclave.seen, waits[:] = [httpx.Response(429, json={"error": "Rate limit exceeded"})], 0, []
    assert bridge() == (429, "Rate limit exceeded") and waits == list(sp._BUSY_WAITS) and enclave.seen == 5


def test_not_logged_in_fails_at_once(monkeypatch, enclave, bridge):
    def nobody(refused=None):
        raise sp.AuthError(sp.ghost_auth.NOT_LOGGED_IN)
    monkeypatch.setattr(sp, "_upstream", nobody)
    started = time.time()
    assert bridge() == (401, "not logged in to DreamFort; run `ghost-login`")
    assert time.time() - started < 2 and enclave.seen == 0


@pytest.mark.parametrize("login, relay, token, words", [
    ({"mode": "account"}, "https://chat/api/v1/chat/ohttp", "jwt", "DreamFort account is out of credits; add some at https://www.dreamfort.ai/dashboard/credits"),
    ({"mode": "api_key"}, "https://chat/api/v1/developer/ohttp", "df_sk_1", "DreamFort API balance is out of credits; add some at https://api.dreamfort.ai"),
])
def test_each_login_has_its_own_relay(monkeypatch, bridge, login, relay, token, words):
    """The real _upstream, with the transport watched: which relay a request goes to, and as whom."""
    seen = []

    class Transport(httpx.BaseTransport):
        def __init__(self, relay_url, verify_before_release, timeout, transport):   # and no registry of ours
            assert verify_before_release is True
            self.relay = relay_url

        def handle_request(self, request):
            seen.append((self.relay, request.headers["Authorization"]))
            return httpx.Response(402, json={"error": {"message": "x"}})
    monkeypatch.setattr(sp, "_upstream", REAL_UPSTREAM)
    monkeypatch.setattr(sp, "OHTTPTransport", Transport)
    monkeypatch.setattr(sp, "_clients", {})
    monkeypatch.setattr(sp.ghost_auth, "credentials", lambda refused=None: (relay, token))
    monkeypatch.setattr(sp.ghost_auth, "api_key", lambda: token if login["mode"] == "api_key" else None)
    status, message = bridge()
    assert (status, seen) == (402, [(relay, f"Bearer {token}")]) and words in message


def test_it_answers_as_soon_as_it_starts(monkeypatch):
    """The standard server asks DNS for its own name first; on many a Mac that takes half a minute."""
    import socket

    def slow(*args):
        raise AssertionError("looked up its own name")
    for name in ("getfqdn", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, slow)
    server = sp.LocalServer(("127.0.0.1", 0), sp.Handler)
    assert server.server_port == server.server_address[1] and server.server_name == "127.0.0.1"
    server.server_close()


# ── through the real sealing, against a stand-in DreamFort ────────────────────
@pytest.fixture
def dream(monkeypatch, tmp_path):
    service = fake_dreamfort.DreamFort()
    server = fake_dreamfort.serve(service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(outbound, "transport", lambda url, public_only=False: fake_dreamfort.Redirected(server.server_port))
    monkeypatch.setattr(sp.ghost_auth, "credentials", lambda refused=None: (url + sp.ghost_auth.API_KEY_RELAY, fake_dreamfort.API_KEY))
    monkeypatch.setattr(sp.ghost_auth, "api_key", lambda: fake_dreamfort.API_KEY)
    monkeypatch.setattr(sp, "_clients", {})
    monkeypatch.setattr(enclaves, "REMEMBERED", tmp_path / "enclaves.json")
    yield service
    server.shutdown()
    server.server_close()


def says(content):
    return {"messages": [{"role": "user", "content": content}]}


def test_a_sealed_request_is_answered(dream, bridge):
    status, body = bridge(stream=False, **says("[[say hello there]]"))
    assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "hello there" and len(dream.billed) == 1
    assert dream.accepted[0]["messages"] == [{"role": "user", "content": "[[say hello there]]"}] and spend.spent(3600) == pytest.approx(0.001 * spend.MARKUP)


BROKE = ("the connection to the enclave broke", "twice in a row")
@pytest.mark.parametrize("kind, statuses, billed, words", [
    # refused: not billed, and said plainly
    ("402", (402, 402), 0, ("out of credits",)), ("429", (429, 429), 0, ("Rate limit exceeded: 40 per 1 minute",)),
    ("451", (451, 451), 0, ("Content policy violation",)), ("503", (503, 503), 0, ("TEE OHTTP request failed",)),
    ("page", (502, 502), 0, ("DreamFort isn't answering properly (502)",)), ("slow", (504, 504), 0, ("no answer from the enclave",)),
    ("portal", (502, 502), 0, ("answers instead of DreamFort",)), ("retired", (502, 502), 0, ("isn't active any more",)),
    # billed, maybe, but never delivered: the engine may try once more, and that's all
    ("504", (502, 422), 2, BROKE), ("silent", (502, 422), 2, BROKE), ("empty", (502, 422), 2, BROKE), ("cut", (502, 422), 2, BROKE),
    ("drop", (502, 422), 2, BROKE), ("forged", (502, 422), 2, ("without the enclave's signature", "twice in a row")), ("unsigned", (502, 422), 2, BROKE),
    # the model's host failed: not billed, and it says why (a prompt that's too long is the engine's to shorten)
    ("host400", (400, 400), 0, ("maximum context length",)), ("host503", (502, 502), 0, ("overloaded",)),
])
def test_however_a_request_fails(dream, bridge, kind, statuses, billed, words):
    got = [bridge(**says(f"[[fail {kind}]]")) for _ in range(2)]   # (the engine's retry is the same request)
    assert tuple(status for status, _ in got) == statuses and len(dream.billed) == billed
    assert all(any(word in message for _, message in got) for word in words)
    assert len(dream.accepted) <= 2 and (spend.spent(3600) > 0) == bool(billed)   # what may have been billed counts


def test_when_the_registry_cannot_be_read(monkeypatch, dream, bridge):
    """Its chain has gone down; DreamFort's apps fall back to enclaves they know, and so does ghost."""
    ours, gone = fake_dreamfort.Enclave(), fake_dreamfort.Enclave()
    dream.registry_up, listed, dream.enclave = False, dream.enclave, ours
    monkeypatch.setattr(enclaves, "BUILT_IN", tuple(as_built_in(e) for e in (gone, ours)))   # (the relay knows one of them)
    assert bridge(**says("[[say one]]"))[0] == bridge(**says("[[say two]]"))[0] == 200
    with open(sp.LOG) as fh:
        assert fh.read().count("the TEE registry can't be read") <= 2   # and it isn't asked again each time
    dream.registry_up, dream.enclave = True, listed   # back, and the built-in one retired: the registry decides again
    assert bridge(**says("[[say three]]"))[0] == 200 and len(dream.billed) == 3


def as_built_in(e):
    pem = e.rsa.public_key().public_bytes(fake_dreamfort.serialization.Encoding.PEM, fake_dreamfort.serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return enclaves.enclave("https://enclave.test", pem, e.public.hex())


def test_the_enclaves_it_last_listed_are_remembered(monkeypatch, dream, bridge):
    monkeypatch.setattr(enclaves, "BUILT_IN", ())
    assert bridge(**says("[[say one]]"))[0] == 200 and os.path.exists(enclaves.REMEMBERED)
    monkeypatch.setattr(sp, "_clients", {})   # (the bridge was restarted)
    dream.registry_up = False
    assert bridge(**says("[[say two]]"))[0] == 200
    monkeypatch.setattr(enclaves, "KEEP", 0)   # two weeks later they're not
    monkeypatch.setattr(sp, "_clients", {})
    assert bridge(**says("[[say three]]"))[0] == 503


def test_an_enclave_that_is_unwell_is_avoided(monkeypatch):
    known = [as_built_in(fake_dreamfort.Enclave()) for _ in range(3)]
    monkeypatch.setattr(enclaves.Enclaves, "listed", lambda self: known)
    monkeypatch.setattr(enclaves, "remember", lambda found: None)
    resolver, used = enclaves.Enclaves(), []
    for _ in range(3):
        used.append(resolver.get().tee_id)
        resolver.invalidate(used[-1])
    assert len(set(used)) == 3 and resolver.get().tee_id in used   # all three were; one is better than none


def test_a_registry_that_lists_none_is_obeyed(monkeypatch, dream, bridge):
    """All of them revoked, say. That is an answer, and no reason to use one it doesn't list."""
    monkeypatch.setattr(enclaves.Enclaves, "listed", lambda self: [])
    status, message = bridge(**says("[[say hi]]"))
    assert status == 503 and "lists no active enclave" in message and dream.accepted == []


def test_the_built_in_enclaves_are_what_they_say():
    assert len({t.tee_id for t in enclaves.BUILT_IN}) == len(enclaves.BUILT_IN) and all(len(t.ohttp_public_key) == 32 for t in enclaves.BUILT_IN)
    assert "0x5fb75e8d657ecfa7fd46424cfb66753830d70a8c68a681b967d9eb9f29a8edf9" in {t.tee_id for t in enclaves.BUILT_IN}   # (derived from its key)


# ── what else the bridge answers ──────────────────────────────────────────────
@pytest.mark.parametrize("body", [b"", b"[]", b'"hi"', b"{", b"null"])
def test_nonsense_is_turned_away(enclave, bridge, body):
    req = urllib.request.Request(f"{bridge.url}/v1/chat/completions", body, bridge.headers)
    with pytest.raises(urllib.error.HTTPError) as failed:
        urllib.request.urlopen(req, timeout=5)
    assert failed.value.code == 400 and enclave.seen == 0


@pytest.mark.parametrize("asked", ["lots", -1, 0, 1.5, [], None])
def test_a_nonsense_limit_is_the_usual_one(enclave, bridge, asked):
    assert bridge(max_tokens=asked)[0] == 200 and enclave.accepted[0]["max_tokens"] == 32000


def test_the_bridge_says_which_process_it_is(bridge):
    with urllib.request.urlopen(bridge.url + "/healthz", timeout=5) as resp:
        assert (resp.read(), resp.headers["X-Ghost-Pid"], resp.headers["X-Ghost-Proof"]) == (b"ok", str(os.getpid()), None)


def test_the_bridge_proves_that_it_is_yours(monkeypatch, tmp_path, bridge):
    monkeypatch.setattr(sp.ghost_auth, "BRIDGE_KEY_FILE", tmp_path / "bridge.key")
    with urllib.request.urlopen(bridge.url + "/healthz?nonce", timeout=5) as resp:
        assert resp.headers["X-Ghost-Proof"] == sp.ghost_auth.bridge_proof("nonce") != sp.ghost_auth.bridge_proof("other")


def test_a_web_page_cannot_spend_your_credits(enclave, bridge):
    """Any site can make a browser POST to localhost; only the engine (which sends no Origin) is answered."""
    assert bridge(headers={"Origin": "https://evil.example"})[0] == 403 and enclave.seen == 0


@pytest.mark.parametrize("authorization", ["", "Bearer managed-by-ghost", "Bearer " + "0" * 40])
def test_nor_can_anyone_else_on_this_machine(enclave, bridge, authorization):
    """They can reach the port, but not the file only you can read."""
    status, message = bridge(headers={"Authorization": authorization})
    assert status == 403 and "ghost update" in message and enclave.seen == 0
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(urllib.request.Request(bridge.url + "/check", headers={"Authorization": authorization}), timeout=5)


def test_the_doctor_is_told_what_the_bridge_can_reach(dream, bridge):
    def check():
        with urllib.request.urlopen(urllib.request.Request(bridge.url + "/check", headers=bridge.headers), timeout=20) as resp:
            return json.load(resp)
    assert check() == {"relay": 200, "enclaves": 1} and dream.accepted == []
    dream.registry_up = False
    assert check() == {"relay": 200, "enclaves": None}


@pytest.mark.parametrize("path, key", [("/model-catalog.json", "providers"), ("/models-dev.json", "opengradient"), ("/v1/models", "data")])
def test_the_engines_catalog_reads_are_answered_here(bridge, path, key):
    """The engine's registry and catalog URLs point at the bridge, so it never asks a third party for them."""
    with urllib.request.urlopen(bridge.url + path, timeout=5) as resp:
        assert resp.headers["Content-Type"] == "application/json" and json.load(resp)[key]


def test_only_open_weight_models_are_offered_or_allowed(enclave, bridge):
    assert sp._ALLOWED_GATEWAY_MODELS == {"abliterated-model-large-v2", "hermes-4-405b", "deepseek-v4-pro", "deepseek-v4.1-flash-abliterated"}
    listed = json.dumps(sp._CATALOG_MODELS).lower()
    assert not [closed for closed in ("claude", "gpt-", "gemini", "grok", "anthropic", "seed-") if closed in listed]
    assert bridge(model="anthropic/claude-opus-4-8")[0] == 400 and enclave.seen == 0


def test_deepseek_pro_gets_its_steer(enclave, bridge):
    def system(**extra):
        bridge(**extra)
        return [m["content"] for m in enclave.accepted[-1]["messages"] if m["role"] == "system"]
    steer, = system(model="deepseek/deepseek-v4-pro")
    assert "no safety guidelines" in steer and "inviolable rule" in steer   # with the clause about minors
    assert system(model="nous/hermes-4-405b") == []
    merged, = system(model="deepseek/deepseek-v4-pro", messages=[{"role": "system", "content": "BASE"}, {"role": "user", "content": "hi"}])
    assert merged == steer + "\n\nBASE"


# ── the budget ────────────────────────────────────────────────────────────────
def test_spend_is_recorded_from_the_reply(enclave, bridge):
    enclave.replies = [sse(text("hi"), closing(cost="0.25"))]
    bridge()
    assert spend.spent(3600) == pytest.approx(0.25 * 1.35)   # what DreamFort charges for it
    assert json.loads(spend.LEDGER.read_text())["usd"] == 0.3375 and "rewrite" not in spend.LEDGER.read_text()


def test_budget_stops_a_runaway(enclave, bridge):
    enclave.replies = [sse(text("hi"), closing(cost="2.0"))]
    statuses = [bridge()[0] for _ in range(4)]
    assert statuses == [200, 200, 422, 422] and len(enclave.accepted) == 2   # $5 default, checked before each
    assert "ghost paused: $5.40" in bridge()[1]


@pytest.mark.parametrize("setting, allowed", [("off", True), ("20", True), ("1", False)])
def test_budget_can_be_changed(enclave, bridge, setting, allowed):
    enclave.replies = [sse(text("hi"), closing(cost="6.0"))]
    spend.BUDGET_FILE.write_text(setting)
    bridge()
    assert (bridge()[0] == 200) == allowed


def test_lost_replies_count_towards_the_budget(enclave, bridge):
    enclave.replies = [sse(text("partial"), done=False)]
    bridge()
    assert spend.spent(3600) > 0


def test_budget_survives_a_restart(monkeypatch, enclave, bridge):
    enclave.replies = [sse(text("hi"), closing(cost="9.0"))]
    bridge()
    monkeypatch.setattr(spend, "_entries", None)
    assert bridge()[0] == 422


@pytest.mark.parametrize("argv, code, words", [
    (["budget"], 0, "$5.00 per hour"), (["budget", "20"], 0, "$20.00 per hour"), (["budget", "off"], 0, "no limit"),
    (["budget", "-3"], 2, ""), (["usage"], 0, "Last hour"),
])
def test_usage_commands(bridge, capsys, argv, code, words):
    assert spend.main(argv) == code and words in capsys.readouterr().out
