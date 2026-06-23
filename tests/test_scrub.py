"""Privacy-critical scrubber tests for ghost.

The job of this suite is to make a future PII/secret-leak regression fail loudly. The
centerpiece is the GOLDEN LEAK CANARY: build a realistic outbound request body with a real
secret/name in every channel the engine can replay (message content, assistant tool_calls
arguments, tool/function definitions, prompt) and assert that none of those real values
survive into the bytes that would be forwarded to og-veil / the relay / the enclave.

Run:  pytest tests/   (needs presidio-analyzer + spaCy en_core_web_md; skipped otherwise)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "privacy"))

presidio_scrub = pytest.importorskip(
    "presidio_scrub", reason="needs presidio-analyzer + spaCy en_core_web_md"
)
import scrubbing_proxy as sp  # noqa: E402

# Example secrets are assembled from fragments so the source file never contains a contiguous
# string matching a real token format -- otherwise GitHub's push-protection secret scanner
# rejects the push. The runtime values are still valid token shapes for the scrubber to catch.
SECRET = "sk-" + "ant-api03-" + "DEADBEEF1234567890abcdefGHIJKLmnopqrstuv"
NAME = "Zachary Qufflepuff"
EMAIL = "zachary.qufflepuff@example.com"


@pytest.fixture
def presidio_on(tmp_path, monkeypatch):
    """Force the Presidio path on, PII redaction on, path protection on (the strict config)."""
    marker = tmp_path / ".presidio"
    marker.write_text("")
    monkeypatch.setattr(sp, "PRESIDIO_MARKER", str(marker))
    monkeypatch.setattr(sp, "_PRESIDIO_OK", True)
    monkeypatch.setattr(sp, "NO_SCRUB_SENTINEL", str(tmp_path / ".no_scrub"))  # absent -> pii on
    monkeypatch.setattr(sp, "FULL_REDACTION_SENTINEL", str(tmp_path / ".full_redaction"))


@pytest.fixture
def presidio_off(tmp_path, monkeypatch):
    """Force the legacy regex path (Presidio marker absent)."""
    monkeypatch.setattr(sp, "PRESIDIO_MARKER", str(tmp_path / ".presidio"))  # absent
    monkeypatch.setattr(sp, "NO_SCRUB_SENTINEL", str(tmp_path / ".no_scrub"))
    monkeypatch.setattr(sp, "FULL_REDACTION_SENTINEL", str(tmp_path / ".full_redaction"))


def _request_with_secret_everywhere():
    """A request body that smuggles the secret/PII through every replayable channel."""
    return {
        "model": "nous/hermes-4-405b",
        "messages": [
            {"role": "system", "content": f"User is {NAME}."},
            {"role": "user", "content": f"my key is {SECRET}, email {EMAIL}"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            # the critical leak: turn-1 secret replayed in history
                            "arguments": json.dumps({"cmd": f"echo {SECRET} > k.txt"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "wrote 1 file"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "deploy",
                    "description": f"Deploy as {NAME} using token {SECRET}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "who": {"type": "string", "default": EMAIL},
                        },
                    },
                },
            }
        ],
    }


# ── Golden leak canary ────────────────────────────────────────────────────────
def test_canary_secret_never_leaves_box_pii_on(presidio_on):
    obj, _, mapping = sp._anonymize_request(_request_with_secret_everywhere())
    wire = json.dumps(obj)
    assert SECRET not in wire, "SECRET leaked to the outbound body"
    assert NAME not in wire, "NAME leaked to the outbound body"
    assert EMAIL not in wire, "EMAIL leaked to the outbound body"
    # the placeholders must be present and reversible
    assert any(v == SECRET for v in mapping.values())


def test_canary_secret_never_leaves_box_pii_off(presidio_on, monkeypatch):
    # PII off (the user's default): names may pass, but SECRETS must STILL never leave.
    monkeypatch.setattr(sp, "NO_SCRUB_SENTINEL", os.devnull)  # exists -> pii off
    obj, _, _ = sp._anonymize_request(_request_with_secret_everywhere())
    wire = json.dumps(obj)
    assert SECRET not in wire, "SECRET leaked with PII off (secrets must always be scrubbed)"


def test_canary_legacy_path_also_scrubs_whole_body(presidio_off):
    obj, _, _ = sp._anonymize_request(_request_with_secret_everywhere())
    wire = json.dumps(obj)
    assert SECRET not in wire, "SECRET leaked through the legacy regex fallback path"


def test_model_id_preserved(presidio_on):
    obj, _, _ = sp._anonymize_request(_request_with_secret_everywhere())
    assert obj["model"] == "nous/hermes-4-405b"  # model id must never be scrubbed


# ── Reversibility ─────────────────────────────────────────────────────────────
def test_roundtrip_identity():
    txt = f"{NAME} <{EMAIL}> key {SECRET}"
    anon, mapping, _ = presidio_scrub.anonymize(txt, {}, pii=True)
    assert presidio_scrub.deanonymize(anon, mapping) == txt
    assert SECRET not in anon and NAME not in anon


# ── Tool egress classification (M1: default-deny) ─────────────────────────────
@pytest.mark.parametrize("name", ["terminal", "execute_code", "read_file", "patch", "process"])
def test_local_tools_restore(name):
    assert presidio_scrub._is_local_tool(name) is True


@pytest.mark.parametrize(
    "name",
    ["web_search", "browser_navigate", "send_message", "image_generate",
     "computer_use", "delegate_task", "some_new_unknown_tool", "", None],
)
def test_egress_and_unknown_tools_keep_placeholder(name):
    assert presidio_scrub._is_local_tool(name) is False


# ── Stream de-anon: local restores, external keeps placeholder ────────────────
def _tc_frame(idx, name, args_fragment, first=False):
    fn = {"arguments": args_fragment}
    if first:
        fn["name"] = name
    tc = {"index": idx, "function": fn}
    if first:
        tc["id"] = f"c{idx}"
        tc["type"] = "function"
    return "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [tc]}}]}) + "\n\n"


def test_stream_local_tool_restores_secret():
    mapping = {}
    _, mapping, _ = presidio_scrub.anonymize(f"use {SECRET}", mapping, pii=False)
    ph = next(iter(mapping))  # <SECRET_1>
    sd = presidio_scrub.StreamDeanonymizer(mapping)
    out = b""
    out += sd.feed(_tc_frame(0, "terminal", '{"cmd":"echo ' + ph[:4], first=True))
    out += sd.feed(_tc_frame(0, "terminal", ph[4:] + ' > k"}'))
    out += sd.feed("data: [DONE]\n\n")
    out += sd.close()
    text = out.decode()
    assert SECRET in text, "local tool must get the REAL secret restored"
    assert ph not in text


def test_secret_patterns_single_source_no_drift():
    # both scrub paths must use the SAME secret patterns (no drift between Presidio + legacy)
    import scrub_patterns
    assert sp.SECRET_RES is scrub_patterns.SECRET_RES
    assert presidio_scrub.SECRET_RES is scrub_patterns.SECRET_RES


@pytest.mark.parametrize("secret", [
    "sk-" + "ant-api03-DEADBEEF1234567890abcdefGHIJ",          # anthropic
    "sk" + "_live_" + "51HxYzABCDEFGHIJKLMNOPqrst",            # stripe
    "whsec" + "_ABCDEFGHIJKLMNOPQRSTUVWX1234",                 # stripe webhook
    "github" + "_pat_" + "11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890",  # gh fine-grained
    "xapp" + "-1-A012345678-9876543210-abcdef",                # slack app
    "r8" + "_AbCdEf0123456789AbCdEf0123456789",                # replicate
    "fw" + "_abcdef0123456789ABCDEF0123",                      # fireworks
])
def test_expanded_secret_coverage(secret):
    out, _, n = presidio_scrub.anonymize(f"token is {secret} ok", {}, pii=False)
    assert secret not in out, f"secret format not scrubbed: {secret}"
    assert n >= 1


def test_transient_detection():
    assert sp._is_transient(502, "Selected TEE is not active in the registry")
    assert sp._is_transient(500, "Stream setup failed")
    assert sp._is_transient(503, "")
    assert not sp._is_transient(400, "bad request")
    assert not sp._is_transient(200, "ok")


def test_stream_utf8_split_across_chunks():
    # an emoji's bytes split across two feeds must NOT be dropped/corrupted
    sd = presidio_scrub.StreamDeanonymizer({})
    # ensure_ascii=False so the emoji is raw multi-byte UTF-8 in the wire bytes (some upstreams
    # send raw, not \uXXXX-escaped) -- that's the case the incremental decoder must handle.
    frame = ('data: ' + json.dumps({"choices": [{"delta": {"role": "assistant", "content": "hi 😀"}}]},
                                    ensure_ascii=False) + "\n\n")
    raw = frame.encode("utf-8")
    cut = raw.index(b"\xf0")  # first byte of the emoji
    out = sd.feed(raw[: cut + 2])  # split mid-emoji
    out += sd.feed(raw[cut + 2:])
    out += sd.feed(b"data: [DONE]\n\n")
    out += sd.close()
    # reconstruct emitted content (re-emit may \u-escape, which is valid JSON) and assert intact
    content = ""
    for line in out.decode("utf-8").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        p = line[len("data:"):].strip()
        if p in ("", "[DONE]"):
            continue
        try:
            content += json.loads(p)["choices"][0]["delta"].get("content", "") or ""
        except Exception:
            pass
    assert "😀" in content, "split multi-byte char was dropped"


def test_stream_multiline_data_frame_deanonymized():
    mapping = {}
    _, mapping, _ = presidio_scrub.anonymize(f"hi {SECRET}", mapping, pii=False)
    ph = next(iter(mapping))
    sd = presidio_scrub.StreamDeanonymizer(mapping)
    # a tool-call frame for a LOCAL tool spread normally; ensure parse path restores it
    frame = "data: " + json.dumps(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c0", "type": "function",
             "function": {"name": "terminal", "arguments": '{"x":"' + ph + '"}'}}]}}]}
    ) + "\n\n"
    out = sd.feed(frame) + sd.feed("data: [DONE]\n\n") + sd.close()
    assert SECRET in out.decode(), "multi-line/parse path must still de-anon local tool args"


def test_catalog_is_unrestricted_only():
    # ghost must only offer/allow genuinely unrestricted models (Hermes); no closed/refusing ones.
    allowed = sp._ALLOWED_GATEWAY_MODELS
    assert allowed == {"hermes-4-405b", "hermes-4-70b"}
    blob = json.dumps(sp._CATALOG_MODELS).lower()
    for closed in ("claude", "gpt", "gemini", "grok", "openai", "anthropic", "seed"):
        assert closed not in blob, f"closed model '{closed}' must not be in the catalog"


def test_stream_external_tool_keeps_placeholder():
    mapping = {}
    _, mapping, _ = presidio_scrub.anonymize(f"contact {EMAIL}", mapping, pii=True)
    ph = next(iter(mapping))
    sd = presidio_scrub.StreamDeanonymizer(mapping)
    out = b""
    out += sd.feed(_tc_frame(0, "send_message", '{"to":"' + ph + '"}', first=True))
    out += sd.feed("data: [DONE]\n\n")
    out += sd.close()
    text = out.decode()
    assert ph in text and EMAIL not in text, "egress tool must keep the placeholder"
