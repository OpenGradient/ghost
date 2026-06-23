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

SECRET = "sk-ant-api03-DEADBEEF1234567890abcdefGHIJKLmnopqrstuv"
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
