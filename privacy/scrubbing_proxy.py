#!/usr/bin/env python3
"""PII-scrubbing OpenAI bridge that forwards to og-veil's local server.

This is ghost's hosted-inference path. The forked Hermes engine talks plain
OpenAI HTTP to us over localhost; we scrub PII/secrets out of the outbound
content and strip the provider prefix from the model id, then forward the (still
plain-OpenAI) request to **og-veil**'s local server, which owns the entire
privacy protocol -- on-chain TEE registry discovery, Oblivious-HTTP/HPKE
encryption, the chat-api relay, and per-response verification:

    Hermes (uncensored profile)
      --plaintext localhost-->  THIS (127.0.0.1:8788)        [scrub PII/secrets]
      --plaintext localhost-->  og-veil (127.0.0.1:11435)    [OHTTP + TEE + verify]
      ---------------------->  chat-api relay  -->  TEE gateway

Because Hermes talks to us over plaintext localhost, we read & rewrite the body
*before* it is handed to og-veil for encryption, so your name/email/secrets never
reach the relay or the enclave. og-veil verifies every response against the
enclave's registry signing key before a token is returned (verify-before-emit),
and tags it with an `X-OpenGradient-Verified` header we pass straight through.

Previously ghost hand-rolled the whole OHTTP/HPKE/registry/verification stack
here (privacy/ohttp_client.py) plus the Supabase auth (privacy/chat_auth.py /
chat_login.py). That is now delegated entirely to og-veil (the `opengradient-veil`
package), so this process only scrubs and forwards -- one implementation of the
protocol, shared with the chat-app and the og-veil CLI. Run `ghost-login`
(-> `og-veil login`) once to connect your account; og-veil holds the session and
masks your IP on its own egress.
"""
import json
import os
import random
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

LISTEN = ("127.0.0.1", 8788)
# og-veil's local OpenAI-compatible server. It owns OHTTP/HPKE, the TEE registry,
# response verification, and the Supabase session -- everything ghost used to
# hand-roll. Override the port/host with GHOST_VEIL_URL (default :11435, chosen to
# avoid colliding with Ollama on :11434).
VEIL_URL = os.environ.get("GHOST_VEIL_URL", "http://127.0.0.1:11435/v1").rstrip("/")
LOG = os.path.expanduser("~/.ghost/privacy/scrubber.log")
DENYLIST_FILE = os.path.expanduser("~/.ghost/privacy/pii_denylist.txt")

# NER PII scrubbing (Presidio + spaCy) with reversible placeholders. Enabled by the
# .presidio marker; falls back hard to the legacy regex scrubber on any import/runtime
# error so the bridge never goes down over a scrubber problem.
PRESIDIO_MARKER = os.path.expanduser("~/.ghost/privacy/.presidio")
# Written when NER is EXPECTED (.presidio set) but unavailable/failed, so the degradation to the
# weaker regex scrubber is visible (bin/ghost surfaces it) instead of silent. Cleared on success.
PRESIDIO_FAILED_MARKER = os.path.expanduser("~/.ghost/privacy/.presidio_failed")
try:
    import presidio_scrub
    _PRESIDIO_OK = True
except Exception:
    presidio_scrub = None
    _PRESIDIO_OK = False

# Curated picker whitelist served at /model-catalog.json. ghost is an UNRESTRICTED harness, so it
# only offers OPEN-WEIGHT, steerable models. Closed, safety-tuned refusers (Claude, GPT, Gemini,
# Grok, Seed) are served by the gateway but deliberately excluded -- they refuse/moralize and
# can't be steered. This list is the single source of truth for both the picker and the bridge's
# allow-list (_ALLOWED_GATEWAY_MODELS).
#
# SUPPORTED-MODEL REFERENCE (gateway-verified by probing /v1/chat/completions, 2026-06-24):
# the gateway's open-weight models are hermes-4-405b, hermes-4-70b, deepseek-v4-pro, glm-5.2.
# NOTE: `og-veil models` is INCOMPLETE -- it omits deepseek-v4-pro and glm-5.2 even though the
# gateway serves them. Do NOT trust that list to add a model; probe the endpoint. The gateway
# rejects anything it doesn't serve with `Model '<id>' is not supported`.
_CATALOG_MODELS = [
    ("nous/hermes-4-405b", "Hermes 4 405B — flagship uncensored open model, most steerable (default)"),
    ("deepseek/deepseek-v4-pro", "DeepSeek V4 Pro — strongest open reasoning + coding; best for agentic work"),
    ("zai/glm-5.2", "GLM 5.2 — strong open agentic MoE (Z.ai)"),
    ("nous/hermes-4-70b", "Hermes 4 70B — fast, low-cost open-weight model"),
]

CATALOG_BYTES = json.dumps(
    {
        "version": 1,
        "updated_at": "2026-06-10T00:00:00Z",
        "providers": {
            "opengradient": {
                "metadata": {"display_name": "OpenGradient (TEE, OHTTP-private)"},
                "models": [{"id": mid, "description": desc} for mid, desc in _CATALOG_MODELS],
            }
        },
    }
).encode()


def log(m):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")
    except Exception:
        pass


# ── PII / secret scrubbing ────────────────────────────────────────────────────
# Patterns live in scrub_patterns.py -- the single source shared with presidio_scrub.py so the
# two scrub paths can never drift on the security-critical secret set.
from scrub_patterns import EMAIL_RE, SSN_RE, CC_RE, IP_RE, PHONE_RE, SECRET_RES  # noqa: E402

PASS_PATHS_SENTINEL = os.path.expanduser("~/.ghost/privacy/.pass_paths")
# Filesystem paths are protected from redaction by DEFAULT so agentic file work is
# not blinded: the user's name often appears inside ~/ paths, and redacting it to
# [REDACTED_PII] breaks path navigation. Names/secrets in prose are still scrubbed.
# Create ~/.ghost/privacy/.full_redaction to redact paths too (maximum privacy).
FULL_REDACTION_SENTINEL = os.path.expanduser("~/.ghost/privacy/.full_redaction")
NO_SCRUB_SENTINEL = os.path.expanduser("~/.ghost/privacy/.no_scrub")  # PII redaction is OPTIONAL: this marker (ghost --no-scrub) turns off name/PII
# redaction; secrets (API keys, JWTs, private keys) are ALWAYS scrubbed regardless.
PATH_RE = re.compile(r"(?:~|/[\w.\-]+)(?:/[\w.\-]+)+")


def load_denylist():
    seed = []
    try:
        with open(DENYLIST_FILE) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    seed.append(ln)
    except FileNotFoundError:
        pass
    return sorted(set(seed), key=len, reverse=True)


DENY = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in load_denylist()]


def scrub(text, pass_paths=False, pii=True):
    if not isinstance(text, str) or not text:
        return text, 0
    n = 0
    held = []
    if pass_paths:
        def _hold(m):
            held.append(m.group(0))
            return f"\x00P{len(held)-1}\x00"

        text = PATH_RE.sub(_hold, text)
    if pii:  # name/PII redaction is optional; secrets below are always scrubbed
        for _term, rx in DENY:
            text, c = rx.subn("[REDACTED_PII]", text)
            n += c
        text, c = EMAIL_RE.subn("[REDACTED_EMAIL]", text); n += c
        text, c = SSN_RE.subn("[REDACTED_SSN]", text); n += c
        text, c = CC_RE.subn("[REDACTED_CC]", text); n += c
        text, c = IP_RE.subn("[REDACTED_IP]", text); n += c
        text, c = PHONE_RE.subn("[REDACTED_PHONE]", text); n += c
    for rx in SECRET_RES:
        text, c = rx.subn("[REDACTED_SECRET]", text); n += c
    for i, p in enumerate(held):
        text = text.replace(f"\x00P{i}\x00", p)
    return text, n


# Structural / identifier keys whose VALUES must stay verbatim (model id, role/type enums,
# tool & function names used for dispatch, ids/indexes, schema structure) and media blobs we
# must not run NER over (base64 images, urls -- Presidio omits URLs anyway). Everything else
# is a candidate string to scrub. Dict KEYS are never scrubbed, so JSON-schema property names
# are safe automatically.
_SKIP_KEYS = frozenset({
    "model", "role", "type", "name", "id", "tool_call_id", "index", "object", "finish_reason",
    "required", "enum", "format",
    "url", "image_url", "data", "b64_json", "input_audio",
})


def _walk_strings(node, fn):
    """Recursively apply fn(str)->str to every string VALUE in a JSON-like structure, skipping
    structural keys (`_SKIP_KEYS`). Returns a new structure. This is what makes the outbound
    scrub COMPLETE -- it reaches messages[].content, tool_calls[].function.arguments, tool /
    function definitions, prompt, and any nested field, not just top-level content."""
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, list):
        return [_walk_strings(x, fn) for x in node]
    if isinstance(node, dict):
        return {k: (v if k in _SKIP_KEYS else _walk_strings(v, fn)) for k, v in node.items()}
    return node


def scrub_body(obj):
    """Legacy regex scrubber over the WHOLE request body (used when Presidio is off / fails)."""
    if not isinstance(obj, dict):
        return obj, 0
    pp = not os.path.exists(FULL_REDACTION_SENTINEL)  # default: protect filesystem paths
    pii = not os.path.exists(NO_SCRUB_SENTINEL)  # PII redaction optional; secrets always scrubbed
    total = 0

    def fn(s):
        nonlocal total
        out, k = scrub(s, pp, pii)
        total += k
        return out

    return _walk_strings(obj, fn), total


def _anonymize_request(obj):
    """Anonymize the request body -> (obj, count, mapping). With Presidio enabled (.presidio
    marker) use NER + reversible placeholders, returning {placeholder: original} for response
    de-anonymization. Otherwise, or on any error, fall back to the legacy regex scrubber."""
    if not (_PRESIDIO_OK and os.path.exists(PRESIDIO_MARKER)):
        obj, n = scrub_body(obj)
        return obj, n, {}
    try:
        pii = not os.path.exists(NO_SCRUB_SENTINEL)
        mapping, total = {}, 0

        def fn(s):
            nonlocal mapping, total
            out, mapping, k = presidio_scrub.anonymize(s, mapping, pii=pii)
            total += k
            return out

        # Walk the ENTIRE body: content, prompt, tool_calls[].function.arguments (replayed
        # history -- the critical leak), and tool/function definitions all get the same
        # per-request mapping so the response/stream de-anon restores them consistently.
        obj = _walk_strings(obj, fn)
        return obj, total, mapping
    except Exception as e:
        log(f"presidio anonymize failed ({e}); falling back to legacy scrub")
        obj, n = scrub_body(obj)
        return obj, n, {}


def _deanonymize_body(body, mapping):
    """Restore placeholders in a non-streaming chat/completions response body. Tool-call
    arguments are de-anonymized only for LOCAL tools, so the real value (e.g. a secret) is
    executed/written locally while the model and relay only ever saw the placeholder."""
    if not mapping or not isinstance(body, dict):
        return body
    try:
        for ch in (body.get("choices") or []):
            msg = ch.get("message") if isinstance(ch, dict) else None
            if not isinstance(msg, dict):
                continue
            if isinstance(msg.get("content"), str):
                msg["content"] = presidio_scrub.deanonymize(msg["content"], mapping)
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") if isinstance(tc, dict) else None
                if (isinstance(fn, dict) and isinstance(fn.get("arguments"), str)
                        and presidio_scrub._is_local_tool(fn.get("name", ""))):
                    fn["arguments"] = presidio_scrub.deanonymize(fn["arguments"], mapping)
    except Exception:
        pass
    return body


# ── Model mapping ─────────────────────────────────────────────────────────────
def _gateway_model(model):
    """Strip the provider prefix to the gateway model name (mirrors the website).

    `nous/hermes-4-405b` -> `hermes-4-405b`, `anthropic/claude-opus-4-8` ->
    `claude-opus-4-8`. Bare names (no prefix) pass through unchanged.
    """
    if isinstance(model, str) and "/" in model:
        return model.split("/", 1)[1]
    return model


# Gateway-model allow-list, derived from the catalog so the two never drift. ghost ENFORCES
# unrestricted-only: a request for any model not in this set is rejected, so a misconfig or a
# manual `/model` can't route prompts to a closed, refusing model. The catalog is the one place
# to add a model -- and only if it's genuinely unrestricted.
_ALLOWED_GATEWAY_MODELS = frozenset(_gateway_model(mid) for mid, _ in _CATALOG_MODELS)


# ── Upstream: og-veil's local OpenAI-compatible server ────────────────────────
# The scrubber -> og-veil hop is plaintext localhost, so it must never go through
# the rotating proxy (that would defeat the localhost assumption and add latency);
# trust_env=False ignores any ambient HTTPS_PROXY. og-veil does the IP-masking on
# its *own* egress to chat-api.
def _veil_client():
    return httpx.Client(timeout=300.0, trust_env=False)


# og-veil / the gateway is intermittently flaky: a stale TEE selection 502s "Selected TEE is
# not active in the registry" and the gateway occasionally 500s "Stream setup failed". Both
# self-recover within seconds, so retry a couple of times with jittered backoff before giving
# up. Chat completions are effectively idempotent here, so a retry is safe.
_RETRY_STATUS = frozenset({500, 502, 503, 504})
_MAX_TRIES = 3


def _is_transient(status, body_text=""):
    if status in _RETRY_STATUS:
        return True
    t = (body_text or "").lower()
    return "tee is not active" in t or "stream setup failed" in t or "temporarily" in t


def _backoff(attempt):
    time.sleep(min(4.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.3))


def _decode_error(raw: bytes) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            return str(err or data.get("detail") or "og-veil error")
    except Exception:
        pass
    return raw.decode("utf-8", errors="ignore")[:300] or "og-veil error"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ── tiny helpers ──
    def _send_json(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status, message):
        self._send_json(status, {"error": {"message": message}})

    def do_GET(self):
        if self.path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/model-catalog.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(CATALOG_BYTES)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(CATALOG_BYTES)
            return
        # Provider-detection probes -- answer LOCALLY so the engine doesn't try to reach a
        # remote provider just to detect the endpoint type (it fires these per turn). Serving
        # them instantly keeps per-turn latency down, and the model list stays consistent with
        # the hosted catalog. Real chat goes through do_POST -> og-veil -> chat-api TEE gateway.
        probe = self.path.rstrip("/")
        if probe in ("/v1/models", "/api/v1/models", "/models"):
            ml = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": mid, "object": "model", "owned_by": mid.split("/", 1)[0]}
                        for mid, _ in _CATALOG_MODELS
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(ml)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(ml)
            return
        if probe in ("/api/tags", "/v1/props", "/props", "/version", "/api/version", "/v1/version"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self._error(404, "not found")

    def do_POST(self):
        path = self.path
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if not ("chat/completions" in path or path.endswith("/completions")):
            self._error(404, "ghost scrubbing bridge only serves /v1/chat/completions")
            return
        try:
            obj = json.loads(raw)
        except Exception as e:
            self._error(400, f"invalid JSON body: {e}")
            return

        # Scrub before anything leaves localhost, then strip the provider prefix
        # to the gateway model name. og-veil does the rest (OHTTP/TEE/verify).
        # With Presidio, redaction is reversible: keep the {placeholder: original}
        # map on the handler to de-anonymize og-veil's response/stream locally.
        obj, redactions, self._pii_map = _anonymize_request(obj)
        wants_stream = bool(obj.get("stream", False))
        obj["model"] = _gateway_model(obj.get("model"))

        # Unrestricted-only: refuse to route to a closed/safety-tuned model even if asked.
        if obj.get("model") not in _ALLOWED_GATEWAY_MODELS:
            self._error(
                400,
                f"ghost only routes to unrestricted models ({', '.join(sorted(_ALLOWED_GATEWAY_MODELS))}); "
                f"'{obj.get('model')}' is not enabled",
            )
            return

        log(f"chat/completions model={obj.get('model')} stream={wants_stream} redactions={redactions} -> {VEIL_URL}")
        self._headers_sent = False
        try:
            if wants_stream:
                self._relay_stream(obj)
            else:
                self._relay_single(obj)
        except httpx.HTTPError as e:
            # og-veil unreachable (not running / not logged in). Surface it so the
            # launcher's fallback to the local model kicks in -- but only if we haven't
            # already started a 200 response body (else we'd corrupt the stream).
            log(f"og-veil unreachable: {e}")
            if not self._headers_sent:
                self._error(502, f"could not reach og-veil at {VEIL_URL} ({type(e).__name__}) — is it running? try `ghost-login`")
        except Exception as e:
            log(f"upstream-error {e}")
            if not self._headers_sent:
                self._error(502, str(e))

    # ── transport: scrub -> forward to og-veil ──
    def _relay_single(self, obj):
        last_status, last_body = 502, b""
        for attempt in range(_MAX_TRIES):
            try:
                with _veil_client() as client:
                    resp = client.post(VEIL_URL + "/chat/completions", json=obj)
            except httpx.HTTPError as e:
                if attempt < _MAX_TRIES - 1:
                    log(f"og-veil transport error (try {attempt + 1}): {e}; retrying")
                    _backoff(attempt)
                    continue
                raise
            if resp.status_code < 400:
                # og-veil already verified the response and attached the
                # opengradient_verification block. De-anonymize placeholders locally
                # (content + local tool-call args) before handing it to the engine.
                try:
                    body = resp.json()
                except Exception:
                    self._error(502, _decode_error(resp.content))
                    return
                body = _deanonymize_body(body, getattr(self, "_pii_map", None))
                self._send_json(200, body)
                return
            last_status, last_body = resp.status_code, resp.content
            if _is_transient(resp.status_code, _decode_error(resp.content)) and attempt < _MAX_TRIES - 1:
                log(f"og-veil {resp.status_code} transient (try {attempt + 1}); retrying")
                _backoff(attempt)
                continue
            self._error(resp.status_code, _decode_error(resp.content))
            return
        self._error(last_status, _decode_error(last_body))

    def _relay_stream(self, obj):
        obj = dict(obj)
        obj["stream"] = True
        for attempt in range(_MAX_TRIES):
            try:
                with _veil_client() as client:
                    with client.stream("POST", VEIL_URL + "/chat/completions", json=obj) as resp:
                        if resp.status_code >= 400:
                            body = resp.read()
                            if _is_transient(resp.status_code, _decode_error(body)) and attempt < _MAX_TRIES - 1:
                                log(f"og-veil {resp.status_code} transient (stream try {attempt + 1}); retrying")
                                _backoff(attempt)
                                continue
                            self._error(resp.status_code, _decode_error(body))
                            return
                        # Status is good -> commit to the 200 stream. From here we must NOT
                        # retry or send another status line; a mid-stream failure can only be
                        # surfaced as a terminal error frame on the already-open body.
                        self.send_response(200)
                        self.send_header("Content-Type", resp.headers.get("Content-Type", "text/event-stream"))
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        verified = resp.headers.get("X-OpenGradient-Verified")
                        if verified:
                            self.send_header("X-OpenGradient-Verified", verified)
                        self.end_headers()
                        self._headers_sent = True
                        # og-veil verified the whole stream before replaying it. If we anonymized
                        # the request, de-anonymize on the way out (content + local tool-call args,
                        # split-delta safe); otherwise forward bytes untouched.
                        mp = getattr(self, "_pii_map", None)
                        deanon = presidio_scrub.StreamDeanonymizer(mp) if (mp and _PRESIDIO_OK) else None
                        try:
                            for chunk in resp.iter_raw():
                                if not chunk:
                                    continue
                                out = deanon.feed(chunk) if deanon else chunk
                                if out:
                                    self.wfile.write(out)
                                    self.wfile.flush()
                            if deanon:
                                tail = deanon.close()
                                if tail:
                                    self.wfile.write(tail)
                                    self.wfile.flush()
                        except Exception as e:
                            # Mid-stream drop: the 200 body is already flowing, so emit a terminal
                            # error event instead of corrupting it with a fresh status line.
                            log(f"mid-stream drop: {e}")
                            try:
                                self.wfile.write(
                                    b'data: {"error":{"message":"stream interrupted"}}\n\ndata: [DONE]\n\n'
                                )
                                self.wfile.flush()
                            except Exception:
                                pass
                        return
            except httpx.HTTPError as e:
                if attempt < _MAX_TRIES - 1:
                    log(f"og-veil transport error (stream try {attempt + 1}): {e}; retrying")
                    _backoff(attempt)
                    continue
                raise


def _mark_presidio_failed(reason):
    """Record that NER was expected but isn't working, so bin/ghost can surface it loudly."""
    log(f"!! NER scrubber UNAVAILABLE -- falling back to regex (names may NOT be scrubbed): {reason}")
    try:
        with open(PRESIDIO_FAILED_MARKER, "w") as f:
            f.write(reason + "\n")
    except Exception:
        pass


def _clear_presidio_failed():
    try:
        os.remove(PRESIDIO_FAILED_MARKER)
    except OSError:
        pass


if __name__ == "__main__":
    # If NER is expected (.presidio set), prove it actually works at startup and FAIL LOUD if not
    # -- a silent fall-through to the regex scrubber (empty denylist => no names scrubbed) is the
    # dangerous failure mode for a privacy tool. The marker is surfaced in `ghost`'s status line.
    expected = os.path.exists(PRESIDIO_MARKER)
    active = False
    if expected:
        if not _PRESIDIO_OK:
            _mark_presidio_failed("presidio/spacy import failed")
        else:
            try:
                presidio_scrub.anonymize("warmup")
                active = True
                _clear_presidio_failed()
                log("presidio warm (NER scrubbing active)")
            except Exception as e:
                _mark_presidio_failed(f"warmup error: {e}")
    else:
        _clear_presidio_failed()  # NER not expected; not a failure
    log(
        f"scrubbing bridge up on {LISTEN[0]}:{LISTEN[1]} -> og-veil {VEIL_URL}; "
        f"{len(DENY)} denylist terms; presidio={'on' if active else ('FAILED' if expected else 'off')}"
    )
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
