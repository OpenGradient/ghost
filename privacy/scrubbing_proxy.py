#!/usr/bin/env python3
"""PII-scrubbing OpenAI bridge to the OpenGradient chat-api TEE gateway (OHTTP).

This is ghost's hosted-inference path. The forked Hermes engine talks plain
OpenAI HTTP to us over localhost; we scrub PII/secrets out of the outbound
content, then wrap each request in OHTTP (the *same* oblivious-HTTP + HPKE
transport the chat.opengradient.ai website uses) and relay it through chat-api
to a TEE gateway resolved from the on-chain registry:

    Hermes (uncensored profile)
      --plaintext localhost-->  THIS (127.0.0.1:8788)   [scrub PII/secrets]
      --HPKE-encrypted OHTTP-->  chat-api /api/v1/chat/ohttp   [Supabase bearer]
      --relay-->  TEE gateway   [decrypts in-enclave, runs model, signs output]

Because Hermes talks to us over plaintext localhost, we read & rewrite the body
before it is ever encrypted, so your name/email/secrets never reach the relay
*or* the enclave. The relay (chat-api) sees your account token + IP but only
ciphertext; the enclave sees the prompt but never your identity.

Auth comes from `ghost-login` (see chat_login.py / chat_auth.py): a Supabase
session captured from the website, auto-refreshed as it ages.

Replaces ghost's previous direct-to-Nous path. The model catalog served here is
the chat-app hosted model line-up (Anthropic / OpenAI / Google / xAI / ByteDance
/ Nous Hermes), all running through this one private path.
"""
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import chat_auth
import ohttp_client as ohttp

LISTEN = ("127.0.0.1", 8788)
ROTATING_PROXY = "http://127.0.0.1:8899"  # chain to chat-api through Webshare rotation
LOG = os.path.expanduser("~/.ghost/privacy/scrubber.log")
DENYLIST_FILE = os.path.expanduser("~/.ghost/privacy/pii_denylist.txt")
DIRECT_MARKER = os.path.expanduser("~/.ghost/.ghost-direct")

# TEE response verification mode: off | warn | strict (default warn).
#   warn   -> verify when possible; log a warning on mismatch but still serve.
#   strict -> raise on any verification failure (refuse tampered responses).
#   off    -> skip verification entirely.
TEE_VERIFY = os.environ.get("GHOST_TEE_VERIFY", "warn").strip().lower()

_OHTTP_CONFIG_TTL = 300  # re-resolve a TEE from the registry every 5 min

# Curated picker whitelist served at /model-catalog.json -- the chat-app hosted
# model line-up. Every model here routes through this OHTTP path. Model ids
# carry a provider prefix (matching the website); the prefix is stripped to the
# gateway model name before the request is sent (see _gateway_model).
_CATALOG_MODELS = [
    ("nous/hermes-4-405b", "Hermes 4 405B — flagship open model, steerable & uncensored (default)"),
    ("nous/hermes-4-70b", "Hermes 4 70B — fast, low-cost open-weight assistant"),
    ("anthropic/claude-fable-5", "Claude Fable 5 — Anthropic's most capable model"),
    ("anthropic/claude-opus-4-8", "Claude Opus 4.8 — top-tier reasoning"),
    ("anthropic/claude-sonnet-4-6", "Claude Sonnet 4.6 — balanced writing & code"),
    ("anthropic/claude-haiku-4-5", "Claude Haiku 4.5 — fast, low-cost replies"),
    ("openai/gpt-5.5", "GPT-5.5 — most capable for hard problems"),
    ("openai/gpt-5", "GPT-5 — powerful all-rounder"),
    ("openai/gpt-5-mini", "GPT-5 Mini — fast and affordable"),
    ("google/gemini-3.5-flash", "Gemini 3.5 Flash — latest, fast and capable"),
    ("google/gemini-2.5-pro", "Gemini 2.5 Pro — deep reasoning, huge context"),
    ("x-ai/grok-4.3", "Grok 4.3 — top-tier reasoning"),
    ("x-ai/grok-4", "Grok 4 — solid all-rounder"),
    ("bytedance/seed-1.8", "Seed 1.8 — strong multilingual"),
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


# ── PII / secret scrubbing (unchanged from ghost's privacy posture) ───────────
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\-\s().]{7,}\d(?!\w)")

SECRET_RES = [
    re.compile(r"\b(?:sk|rk)-(?:ant-|nous-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bfw_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----"),
]

PASS_PATHS_SENTINEL = os.path.expanduser("~/.ghost/privacy/.pass_paths")
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


def scrub(text, pass_paths=False):
    if not isinstance(text, str) or not text:
        return text, 0
    n = 0
    held = []
    if pass_paths:
        def _hold(m):
            held.append(m.group(0))
            return f"\x00P{len(held)-1}\x00"

        text = PATH_RE.sub(_hold, text)
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


def scrub_body(obj):
    total = 0
    if not isinstance(obj, dict):
        return obj, 0
    pp = os.path.exists(PASS_PATHS_SENTINEL)
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                m["content"], k = scrub(c, pp); total += k
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"], k = scrub(part["text"], pp); total += k
    p = obj.get("prompt")
    if isinstance(p, str):
        obj["prompt"], k = scrub(p, pp); total += k
    elif isinstance(p, list):
        obj["prompt"] = [scrub(x, pp)[0] if isinstance(x, str) else x for x in p]
    return obj, total


# ── Model mapping ─────────────────────────────────────────────────────────────
def _gateway_model(model):
    """Strip the provider prefix to the gateway model name (mirrors the website).

    `nous/hermes-4-405b` -> `hermes-4-405b`, `anthropic/claude-opus-4-8` ->
    `claude-opus-4-8`. Bare names (no prefix) pass through unchanged.
    """
    if isinstance(model, str) and "/" in model:
        return model.split("/", 1)[1]
    return model


# ── OHTTP config cache (full on-chain registry read) ──────────────────────────
_cached_config = None
_cached_at = 0.0


def _get_ohttp_config(force=False):
    global _cached_config, _cached_at
    now = time.time()
    if not force and _cached_config is not None and (now - _cached_at) < _OHTTP_CONFIG_TTL:
        return _cached_config
    cfg = chat_auth.get_config()
    rpc = cfg.get("tee_registry_rpc_url")
    addr = cfg.get("tee_registry_address")
    if not rpc or not addr:
        raise ohttp.OhttpError(
            "TEE registry is not configured (re-run ghost-login, or set "
            "GHOST_TEE_REGISTRY_RPC_URL / GHOST_TEE_REGISTRY_ADDRESS)."
        )
    config = ohttp.read_registry_ohttp_config(
        rpc_url=rpc,
        registry_address=addr,
        tee_type=int(cfg.get("tee_registry_tee_type") or ohttp.TEE_TYPE_LLM_PROXY),
        app_env=str(cfg.get("app_env") or "production"),
    )
    _cached_config, _cached_at = config, now
    log(f"resolved TEE {config.tee_id} @ {config.endpoint}")
    return config


def _invalidate_config():
    global _cached_config
    _cached_config = None


# ── Upstream HTTP (optionally chained through the rotating proxy) ─────────────
def _http_client():
    use_proxy = not os.path.exists(DIRECT_MARKER)
    if use_proxy:
        return httpx.Client(proxy=ROTATING_PROXY, timeout=300.0)
    return httpx.Client(timeout=300.0)


def _chat_api_base():
    base = chat_auth.get_config().get("chat_api_base_url")
    if not base:
        raise ohttp.OhttpError("chat-api base URL unknown (re-run ghost-login).")
    return base.rstrip("/")


def _verify(inner_for_hash, body, config, *, response_content=None):
    """Verify a TEE response per GHOST_TEE_VERIFY (off|warn|strict).

    In strict mode a failure raises (caller must abort before/while responding).
    In warn mode failures are logged but the response is served anyway.
    """
    if TEE_VERIFY == "off":
        return
    try:
        result = ohttp.verify_tee_response(
            inner_request=inner_for_hash,
            response_body=body,
            signing_public_key_der=config.signing_public_key_der,
            response_content=response_content,
        )
        if result.get("verified"):
            log(f"TEE verified ✓ host={config.host} ts={result.get('timestamp')}")
        else:
            log(f"TEE response unsigned ({result.get('reason')}) host={config.host}")
    except Exception as e:
        if TEE_VERIFY == "strict":
            raise
        log(f"⚠️ TEE verification failed (warn mode, serving anyway): {e}")


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
        # the hosted catalog. Real chat goes through do_POST -> OHTTP -> chat-api TEE gateway.
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
            self._error(404, "ghost OHTTP bridge only serves /v1/chat/completions")
            return
        try:
            obj = json.loads(raw)
        except Exception as e:
            self._error(400, f"invalid JSON body: {e}")
            return

        # Scrub before anything leaves localhost.
        obj, redactions = scrub_body(obj)
        wants_stream = bool(obj.pop("stream", False))
        obj["model"] = _gateway_model(obj.get("model"))

        # The canonical subset the gateway signs over (model/messages/temperature
        # [+web_search]) -- used only for optional verification.
        inner_for_hash = {"model": obj.get("model"), "messages": obj.get("messages", [])}
        if "temperature" in obj:
            inner_for_hash["temperature"] = float(obj["temperature"])
        if obj.get("web_search"):
            inner_for_hash["web_search"] = True

        log(f"chat/completions model={obj.get('model')} stream={wants_stream} redactions={redactions}")
        try:
            if wants_stream:
                self._relay_stream(obj, inner_for_hash)
            else:
                self._relay_single(obj, inner_for_hash)
        except ohttp.OhttpError as e:
            self._error(502, f"OHTTP error: {e}")
        except Exception as e:
            log(f"upstream-error {e}")
            self._error(502, str(e))

    # ── auth + transport ──
    def _auth_headers(self, accept, stream, tee_id, force_refresh=False):
        token = chat_auth.get_valid_access_token(force_refresh=force_refresh)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": ohttp.OHTTP_REQUEST_MEDIA_TYPE,
            "Accept": accept,
            "X-TEE-ID": tee_id,
        }
        if stream:
            headers["X-OHTTP-Stream"] = "true"
        return headers

    def _post_ohttp(self, client, inner_request, *, stream):
        """Encapsulate + POST to chat-api, retrying once on a 401 (token refresh)."""
        config = _get_ohttp_config()
        accept = ohttp.OHTTP_CHUNKED_RESPONSE_MEDIA_TYPE if stream else ohttp.OHTTP_RESPONSE_MEDIA_TYPE
        url = _chat_api_base() + ohttp.OHTTP_ENDPOINT

        for attempt in (0, 1):
            enc = ohttp.encapsulate(config, inner_request)
            headers = self._auth_headers(accept, stream, config.tee_id, force_refresh=(attempt == 1))
            req = client.build_request("POST", url, headers=headers, content=enc.wire)
            resp = client.send(req, stream=stream)
            if resp.status_code == 401 and attempt == 0:
                resp.close()
                continue  # token likely expired between refreshes -> force refresh & retry
            return enc, resp, config
        return enc, resp, config

    def _relay_single(self, inner_request, inner_for_hash):
        with _http_client() as client:
            enc, resp, config = self._post_ohttp(client, inner_request, stream=False)
            sealed = resp.read()
            if resp.status_code >= 400:
                self._error(resp.status_code, _decode_relay_error(sealed))
                return
            inner = ohttp.decrypt_single(enc, sealed)
            if inner["status"] >= 400:
                self._error(inner["status"], str(inner["body"].get("error", "TEE inner error")))
                return
            body = inner["body"]
            # Verify before responding so strict mode can refuse a bad response.
            try:
                _verify(inner_for_hash, body, config)
            except Exception as e:
                self._error(502, f"TEE verification failed: {e}")
                return
            self._send_json(200, body)

    def _relay_stream(self, inner_request, inner_for_hash):
        wire_request = dict(inner_request)
        wire_request["stream"] = True
        with _http_client() as client:
            enc, resp, config = self._post_ohttp(client, wire_request, stream=True)
            if resp.status_code >= 400:
                sealed = resp.read()
                self._error(resp.status_code, _decode_relay_error(sealed))
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            decrypter = ohttp.ChunkedResponseDecrypter(enc.chunked_response_secret, enc.enc)
            full_content = []
            final_body = None
            for network_chunk in resp.iter_bytes():
                for plaintext in decrypter.push(network_chunk, done=False):
                    self.wfile.write(plaintext)
                    self.wfile.flush()
                    c, fb = _parse_sse(plaintext)
                    full_content.append(c)
                    if fb is not None:
                        final_body = fb
            for plaintext in decrypter.push(None, done=True):
                self.wfile.write(plaintext)
                self.wfile.flush()
                c, fb = _parse_sse(plaintext)
                full_content.append(c)
                if fb is not None:
                    final_body = fb

            # Headers/body already streamed, so verification here is log-only
            # even in strict mode (we can't un-send). Logged for auditability.
            if final_body is not None and TEE_VERIFY != "off":
                try:
                    _verify(inner_for_hash, final_body, config, response_content="".join(full_content))
                except Exception as e:
                    log(f"⚠️ TEE stream verification failed: {e}")


def _parse_sse(plaintext: bytes):
    """Pull assistant delta text and the signed final frame out of an SSE chunk."""
    content = ""
    final_body = None
    try:
        text = plaintext.decode("utf-8", errors="ignore")
    except Exception:
        return content, final_body
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        choices = parsed.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                content += delta["content"]
        if isinstance(parsed.get("tee_signature"), str) or isinstance(parsed.get("tee_output_hash"), str):
            final_body = parsed
    return content, final_body


def _decode_relay_error(raw: bytes) -> str:
    try:
        data = json.loads(raw)
        return str(data.get("detail") or data.get("error") or "relay error")
    except Exception:
        return raw.decode("utf-8", errors="ignore")[:300] or "relay error"


if __name__ == "__main__":
    logged_in = chat_auth.is_logged_in()
    log(
        f"OHTTP bridge up on {LISTEN[0]}:{LISTEN[1]} -> chat-api; verify={TEE_VERIFY}; "
        f"{len(DENY)} denylist terms; logged_in={logged_in}"
    )
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
