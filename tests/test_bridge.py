"""The bridge must never pay twice for one request.

The relay bills every request the enclave accepts, in full, even if we hang up. These tests stand a fake
enclave behind the bridge that counts what it accepted, and check each way a reply can go wrong.
"""
import json
import os
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
    monkeypatch.setattr(sp, "_upstream", lambda: (client, {"Authorization": "Bearer t"}))
    return enclave


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_backoff", lambda attempt: None)
    monkeypatch.setattr(sp, "_wait", lambda seconds: None)
    monkeypatch.setattr(sp, "_last_break", 0.0)
    monkeypatch.setattr(sp, "LOG", str(tmp_path / "log"))
    for name, value in (("DIR", tmp_path), ("LEDGER", tmp_path / "spend.jsonl"), ("BUDGET_FILE", tmp_path / "budget")):
        monkeypatch.setattr(spend, name, value)
    monkeypatch.setattr(spend, "_entries", None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def ask(stream=True, origin=None, **extra):
        body = {"model": MODEL, "stream": stream, "messages": [{"role": "user", "content": "rewrite these scenes"}], **extra}
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                                     json.dumps(body).encode(), {"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]
    ask.url = f"http://127.0.0.1:{server.server_port}"
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


def test_a_good_reply_clears_the_streak(enclave, bridge):
    broken = sse(text("partial"), done=False)
    enclave.replies = [broken, sse(text("hi"), closing()), broken]
    assert [bridge()[0] for _ in range(3)] == [502, 200, 502]


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
    def nobody():
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
    monkeypatch.setattr(sp.ghost_auth, "credentials", lambda: (relay, token))
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
def dream(monkeypatch):
    service = fake_dreamfort.DreamFort()
    server = fake_dreamfort.serve(service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(outbound, "transport", lambda url, public_only=False: fake_dreamfort.Redirected(server.server_port))
    monkeypatch.setattr(sp.ghost_auth, "credentials", lambda: (url + sp.ghost_auth.API_KEY_RELAY, fake_dreamfort.API_KEY))
    monkeypatch.setattr(sp.ghost_auth, "api_key", lambda: fake_dreamfort.API_KEY)
    monkeypatch.setattr(sp, "_clients", {})
    yield service
    server.shutdown()
    server.server_close()


def says(content):
    return {"messages": [{"role": "user", "content": content}]}


def test_a_sealed_request_is_answered(dream, bridge):
    status, body = bridge(stream=False, **says("[[say hello there]]"))
    assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "hello there" and len(dream.billed) == 1
    assert dream.accepted[0]["messages"] == [{"role": "user", "content": "[[say hello there]]"}] and spend.spent(3600) == pytest.approx(0.001)


BROKE = ("the connection to the enclave broke", "twice in a row")
@pytest.mark.parametrize("kind, statuses, billed, words", [
    # refused: not billed, and said plainly
    ("402", (402, 402), 0, ("out of credits",)), ("429", (429, 429), 0, ("Rate limit exceeded: 40 per 1 minute",)),
    ("451", (451, 451), 0, ("Content policy violation",)), ("503", (503, 503), 0, ("TEE OHTTP request failed",)),
    ("page", (502, 502), 0, ("DreamFort isn't answering properly (502)",)), ("retired", (502, 502), 0, ("isn't active any more",)),
    # billed, maybe, but never delivered: the engine may try once more, and that's all
    ("504", (502, 422), 2, BROKE), ("silent", (502, 422), 2, BROKE), ("empty", (502, 422), 2, BROKE), ("cut", (502, 422), 2, BROKE),
    ("drop", (502, 422), 2, BROKE), ("forged", (502, 422), 2, ("without the enclave's signature", "twice in a row")), ("unsigned", (502, 422), 2, BROKE),
    # the model's host failed: not billed, and it says why (a prompt that's too long is the engine's to shorten)
    ("host400", (400, 400), 0, ("maximum context length",)), ("host503", (502, 502), 0, ("overloaded",)),
])
def test_however_a_request_fails(dream, bridge, kind, statuses, billed, words):
    got = [bridge(**says(f"[[fail {kind}]] {i}")) for i in range(2)]
    assert tuple(status for status, _ in got) == statuses and len(dream.billed) == billed
    assert all(any(word in message for _, message in got) for word in words)
    assert len(dream.accepted) <= 2 and (spend.spent(3600) > 0) == bool(billed)   # what may have been billed counts


def test_when_the_registry_cannot_be_read(monkeypatch, dream, bridge):
    """Its chain has gone down; DreamFort's apps fall back to enclaves they know, and so does ghost."""
    ours = fake_dreamfort.Enclave()
    pem = ours.rsa.public_key().public_bytes(fake_dreamfort.serialization.Encoding.PEM, fake_dreamfort.serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    dream.registry_up, listed, dream.enclave = False, dream.enclave, ours
    monkeypatch.setattr(enclaves, "BUILT_IN", (enclaves._enclave("https://ours.test", pem, ours.public.hex()),))
    assert bridge(**says("[[say one]]"))[0] == bridge(**says("[[say two]]"))[0] == 200
    with open(sp.LOG) as fh:
        assert fh.read().count("the TEE registry can't be read") == 1   # and it isn't asked again each time
    dream.registry_up, dream.enclave = True, listed   # back, and the built-in one retired: the registry decides again
    assert bridge(**says("[[say three]]"))[0] == 200 and len(dream.billed) == 3


def test_the_built_in_enclaves_are_the_apps_own():
    """chat-app's lib/api/backupTees.ts and chat-api's src/core/backup_tees.py list the same one."""
    assert [t.tee_id for t in enclaves.BUILT_IN] == ["0xe1443d7ed558fbd19117c4dc70e6245fc71d6063d64af3bc3f790081bc94e9aa"]
    assert all(len(t.ohttp_public_key) == 32 for t in enclaves.BUILT_IN)


# ── what else the bridge answers ──────────────────────────────────────────────
@pytest.mark.parametrize("body", [b"", b"[]", b'"hi"', b"{", b"null"])
def test_nonsense_is_turned_away(enclave, bridge, body):
    req = urllib.request.Request(f"{bridge.url}/v1/chat/completions", body, {"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as failed:
        urllib.request.urlopen(req, timeout=5)
    assert failed.value.code == 400 and enclave.seen == 0


@pytest.mark.parametrize("asked", ["lots", -1, 0, 1.5, [], None])
def test_a_nonsense_limit_is_the_usual_one(enclave, bridge, asked):
    assert bridge(max_tokens=asked)[0] == 200 and enclave.accepted[0]["max_tokens"] == 32000


def test_the_bridge_says_which_process_it_is(bridge):
    with urllib.request.urlopen(bridge.url + "/healthz", timeout=5) as resp:
        assert (resp.read(), resp.headers["X-Ghost-Pid"]) == (b"ok", str(os.getpid()))


def test_a_web_page_cannot_spend_your_credits(enclave, bridge):
    """Any site can make a browser POST to localhost; only the engine (which sends no Origin) is answered."""
    assert bridge(origin="https://evil.example")[0] == 403 and enclave.seen == 0


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
    assert spend.spent(3600) == pytest.approx(0.25)
    assert json.loads(spend.LEDGER.read_text())["usd"] == 0.25 and "rewrite" not in spend.LEDGER.read_text()


def test_budget_stops_a_runaway(enclave, bridge):
    enclave.replies = [sse(text("hi"), closing(cost="2.0"))]
    statuses = [bridge()[0] for _ in range(5)]
    assert statuses == [200, 200, 200, 422, 422] and len(enclave.accepted) == 3   # $5 default, checked before each
    assert "ghost paused: $6.00" in bridge()[1]


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
