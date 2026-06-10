#!/usr/bin/env python3
"""PII-scrubbing OpenAI-compatible reverse proxy for the uncensored Hermes profile.

Chain:  Hermes (uncensored profile)  --plaintext localhost-->  THIS (127.0.0.1:8788)
        --scrub PII from outbound message content-->  rotating Webshare proxy (8899)
        --HTTPS-->  Nous inference API.

Because Hermes talks to us over plaintext localhost, we can read & rewrite the request
body before it is ever TLS-encrypted to Nous. Result: the model/Nous never receive your
name, email, phone, etc. TLS to Nous is still validated normally (only the localhost hop
is plaintext). Edit ~/.ghost/privacy/pii_denylist.txt to add/remove redacted terms.
"""
import json, os, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx

LISTEN = ("127.0.0.1", 8788)
UPSTREAM = "https://inference-api.nousresearch.com/v1"
ROTATING_PROXY = "http://127.0.0.1:8899"          # chain upstream through Webshare rotation
LOG = os.path.expanduser("~/.ghost/privacy/scrubber.log")
DENYLIST_FILE = os.path.expanduser("~/.ghost/privacy/pii_denylist.txt")

# Curated picker whitelist served at /model-catalog.json -- only the 2 uncensored
# Nous models (both routed through this scrubber + the rotating proxy). Replacing the
# nousresearch.com catalog with this kills that phone-home AND hides the closed models.
CATALOG_BYTES = json.dumps({
    "version": 1,
    "updated_at": "2026-06-09T00:00:00Z",
    "providers": {
        "nous": {
            "metadata": {"display_name": "Nous (uncensored, proxied)"},
            "models": [
                {"id": "nousresearch/hermes-4-405b",
                 "description": "Hermes 4 405B -- uncensored, routed via privacy proxy"},
                {"id": "nousresearch/hermes-4-70b",
                 "description": "Hermes 4 70B -- uncensored, routed via privacy proxy"},
            ],
        }
    },
}).encode()


def log(m):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")
    except Exception:
        pass


# --- structured PII patterns ---
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SSN_RE   = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_RE    = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
IP_RE    = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\-\s().]{7,}\d(?!\w)")

# IronClaw-style secret-exfiltration guard: redact credentials in OUTBOUND content so the
# hosted path can never leak your keys to Nous. Outbound-only -- the local default never
# routes through here, so ghost stays fully uncensored + unredacted locally.
SECRET_RES = [
    re.compile(r"\b(?:sk|rk)-(?:ant-|nous-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"),   # OpenAI/Anthropic/Nous/Stripe
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"),                                    # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                              # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                                         # Google API key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),                                  # Slack
    re.compile(r"\bfw_[A-Za-z0-9]{20,}\b"),                                           # Fireworks
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), # JWT
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----"),  # private keys
]


def load_denylist():
    seed = []  # populate via ~/.ghost/privacy/pii_denylist.txt
    try:
        with open(DENYLIST_FILE) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    seed.append(ln)
    except FileNotFoundError:
        pass
    # longest-first so a full name matches before its first-name substring
    return sorted(set(seed), key=len, reverse=True)


DENY = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in load_denylist()]

# Path-aware mode: when this sentinel exists (set by `ghost --paths` for one session),
# filesystem paths are protected from redaction so 405B can do agentic file work. Default
# absent = full redaction. Name/secrets in prose are always scrubbed either way.
PASS_PATHS_SENTINEL = os.path.expanduser("~/.ghost/privacy/.pass_paths")
PATH_RE = re.compile(r"(?:~|/[\w.\-]+)(?:/[\w.\-]+)+")


def scrub(text, pass_paths=False):
    if not isinstance(text, str) or not text:
        return text, 0
    n = 0
    held = []
    if pass_paths:  # protect filesystem paths from redaction (agentic file-work mode)
        def _hold(m):
            held.append(m.group(0)); return f"\x00P{len(held)-1}\x00"
        text = PATH_RE.sub(_hold, text)
    for _term, rx in DENY:
        text, c = rx.subn("[REDACTED_PII]", text); n += c
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
    pp = os.path.exists(PASS_PATHS_SENTINEL)  # path-aware mode for this request?
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _proxy(self, method):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else b""
        path = self.path
        if path == "/healthz":   # local liveness probe; does not forward upstream
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if path == "/model-catalog.json":   # local picker whitelist; no nousresearch.com fetch
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(CATALOG_BYTES)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(CATALOG_BYTES)
            return
        # Provider-detection probes -- answer LOCALLY so they don't crawl through the slow
        # rotating proxy to Nous (which 404s them). The engine fires these per turn to detect
        # the endpoint type; serving them fast removes ~12s/turn of latency.
        probe = path.rstrip("/")
        if method == "GET" and probe in ("/v1/models", "/api/v1/models", "/models"):
            ml = json.dumps({"object": "list", "data": [
                {"id": "nousresearch/hermes-4-405b", "object": "model", "owned_by": "nous"},
                {"id": "nousresearch/hermes-4-70b", "object": "model", "owned_by": "nous"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(ml))); self.send_header("Connection", "close")
            self.end_headers(); self.wfile.write(ml); return
        if method == "GET" and probe in ("/api/tags", "/v1/props", "/props", "/version", "/api/version", "/v1/version"):
            self.send_response(404); self.send_header("Content-Length", "0")
            self.send_header("Connection", "close"); self.end_headers(); return
        redactions = 0
        if body and ("chat/completions" in path or path.endswith("/completions")):
            try:
                obj = json.loads(body)
                obj, redactions = scrub_body(obj)
                body = json.dumps(obj).encode()
            except Exception as e:
                log(f"scrub-parse-error {e}")
        fwd = {k: v for k, v in self.headers.items()
               if k.lower() not in ("host", "content-length", "connection",
                                     "accept-encoding", "proxy-connection")}
        fwd["Accept-Encoding"] = "identity"  # avoid gzip so we relay plaintext cleanly
        url = UPSTREAM + (path[len("/v1"):] if path.startswith("/v1") else path)
        is_stream = b'"stream":true' in body or b'"stream": true' in body
        log(f"{method} {path} redactions={redactions} stream={is_stream}")
        try:
            with httpx.Client(proxy=ROTATING_PROXY, timeout=300.0) as client:
                with client.stream(method, url, headers=fwd, content=body) as r:
                    self.send_response(r.status_code)
                    for k, v in r.headers.items():
                        if k.lower() in ("content-length", "transfer-encoding",
                                         "connection", "content-encoding"):
                            continue
                        self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in r.iter_bytes():
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
        except Exception as e:
            log(f"upstream-error {e}")
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": str(e)}}).encode())
            except Exception:
                pass

    def do_POST(self):
        self._proxy("POST")

    def do_GET(self):
        self._proxy("GET")


if __name__ == "__main__":
    log(f"scrubber up on {LISTEN[0]}:{LISTEN[1]} -> {UPSTREAM} via {ROTATING_PROXY}; "
        f"{len(DENY)} denylist terms")
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
