"""A stand-in for DreamFort on one local port: logins, the relay, the TEE registry and an enclave behind it.

It is real HTTP, real OHTTP and real signatures, so ghost's own code runs against it unchanged. What its "model"
does is written in the newest user message:

    [[say TEXT]]        answer TEXT (the default answer is "a fake reply")
    [[run COMMAND]]     call the terminal tool, then answer "ran: <its output>"
    [[fail KIND N]]     fail the first N attempts (every one, without N) at this message; KINDs are in FAILURES

    python tests/fake_dreamfort.py PORT LOG             the service; LOG gets a line of JSON per request
    python tests/fake_dreamfort.py PORT --bridge FILE   ghost's bridge (FILE is its bridge.py), talking to it
"""
import base64
import json
import os
import re
import socketserver
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from eth_abi import encode
from eth_hash.auto import keccak
from opengradient_ohttp._hpke import _LABEL_CHUNKED_RESPONSE, _LABEL_REQUEST, _SUITE, _derive_response_keys, _header_bytes
from opengradient_ohttp._registry import TEE_INFO_ARRAY_TYPE
from opengradient_ohttp._verify import build_inner_request

API_KEY, NO_CREDITS_KEY = "df_sk_good0000", "df_sk_broke0000"
FAILURES = {
    # refused, so never billed
    "402": (402, {"detail": "Insufficient credits"}), "429": (429, {"error": "Rate limit exceeded: 40 per 1 minute"}),
    "503": (503, {"detail": {"code": "tee_unreachable", "message": "TEE OHTTP request failed"}}),
    "retired": (502, {"detail": {"code": "tee_not_active", "message": "Selected TEE is not active"}}),
    "page": (502, b"<html><h1>502 Bad Gateway</h1></html>"), "451": (451, {"error": "Content policy violation"}),
    # accepted, and then not delivered
    "504": (504, b"<html><h1>504 Gateway Time-out</h1></html>"), "silent": None, "empty": None, "cut": None, "drop": None,
    "forged": None, "unsigned": None,
    # the model's host failed the enclave, which says so in the reply and bills nothing
    "host400": "Error code: 400 - {'error': {'message': \"This model's maximum context length is 131072 tokens.\"}}",
    "host503": "Error code: 503 - {'error': {'message': 'overloaded'}}",
}


class Enclave:
    def __init__(self):
        pair = _SUITE.kem.derive_key_pair(os.urandom(32))
        self.private, self.public = pair.private_key, pair.public_key.to_public_bytes()
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.der = self.rsa.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.tee_id = "0x" + keccak(self.der).hex()

    def registry_entry(self):
        return ("0x" + "11" * 20, "0x" + "22" * 20, "https://enclave.test", self.der, b"", b"\0" * 32, 0, True, 1, 1,
                (1, 0x20, 1, 3, self.public, b"", 1))

    def open(self, wire):
        """The request inside, and the function that seals a reply to it chunk by chunk."""
        enc = wire[7:39]
        context = _SUITE.create_recipient_context(enc, self.private, info=_LABEL_REQUEST + b"\0" + _header_bytes(wire[0]))
        inner, nonce = json.loads(context.open(wire[39:], aad=b"")), os.urandom(32)
        key, base = _derive_response_keys(context.export(_LABEL_CHUNKED_RESPONSE, 32), enc, nonce)

        def seal(pieces):
            yield nonce
            for i, piece in enumerate(pieces):
                final = i == len(pieces) - 1
                sealed = ChaCha20Poly1305(key).encrypt(bytes(a ^ b for a, b in zip(base, i.to_bytes(12, "big"))), piece, b"final" if final else b"")
                yield (b"\0" if final else struct.pack(">I", 0x80000000 | len(sealed))) + sealed
        return inner, seal

    def sign(self, inner, output):
        _, canonical = build_inner_request({k: v for k, v in inner.items() if k != "stream"})
        asked, answered, now = keccak(json.dumps(canonical, sort_keys=True).encode()), keccak(output.encode()), int(time.time())
        signature = self.rsa.sign(keccak(asked + answered + now.to_bytes(32, "big")),
                                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return {"tee_signature": base64.b64encode(signature).decode(), "tee_request_hash": asked.hex(), "tee_output_hash": answered.hex(),
                "tee_timestamp": now, "tee_id": self.tee_id}


class DreamFort:
    def __init__(self, log=None):
        self.enclave, self.log, self.registry_up, self.attempts = Enclave(), log, True, {}
        self.accepted, self.billed = [], []   # the requests the enclave opened, and the ones the account paid for

    def note(self, **row):
        if self.log:
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    @staticmethod
    def asked(inner):
        """The newest user message, and the tool output that came after it."""
        text, after = "", []
        for message in inner["messages"]:
            content = message.get("content")
            content = content if isinstance(content, str) else " ".join(p.get("text", "") for p in content or [] if isinstance(p, dict))
            if message["role"] == "user":
                text, after = content, []
            elif message["role"] == "tool":
                after.append(content)
        return text, after

    def failure(self, text):
        found = re.search(r"\[\[fail (\w+) ?(\d*)\]\]", text)
        if not found:
            return None
        seen = self.attempts[text] = self.attempts.get(text, 0) + 1
        return found[1] if not found[2] or seen <= int(found[2]) else None

    def reply(self, inner, kind):
        """The reply's events, as the gateway streams them."""
        text, after = self.asked(inner)
        command, say = re.search(r"\[\[run (.*?)\]\]", text, re.S), re.search(r"\[\[say (.*?)\]\]", text, re.S)
        call = command and not after and any(t["function"]["name"] == "terminal" for t in inner.get("tools") or [])
        calls = [{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": command[1]})}}] if call else []
        output = "" if calls else "ran: " + after[-1] if command and after else say[1] if say else "a fake reply"
        signed = self.enclave.sign(inner, json.dumps(calls, sort_keys=True) if calls else output + ("!" if kind == "forged" else ""))
        usage = {"prompt_tokens": len(json.dumps(inner)) // 4, "completion_tokens": 20}

        def event(delta, finish=None, **more):
            return b"data: " + json.dumps({"id": "chatcmpl-1", "model": inner["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **more}).encode() + b"\n\n"
        pieces = [event({"tool_calls": [{"index": 0, **calls[0]}]})] if calls else [event({"content": word}) for word in re.findall(r"\S+\s*|\s+", output)]
        if kind and isinstance(FAILURES[kind], str):
            return pieces[:1] + [b"data: " + json.dumps({"error": FAILURES[kind], "source": "provider"}).encode() + b"\n\n"]
        if kind == "unsigned":
            signed = {}
        pieces.append(event({}, "tool_calls" if calls else "stop", usage=usage, opengradient={"cost_usd": "0.001"}, **signed))
        return pieces + [b"data: [DONE]\n\n"]


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, status, body=None, kind="application/json"):
            body = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def key(self):
            return (self.headers.get("Authorization") or "").removeprefix("Bearer ")

        def do_GET(self):
            known = self.key() in (API_KEY, NO_CREDITS_KEY, "account-token")
            if self.path.endswith("/ohttp/config"):
                return self.send(200 if known else 401)
            if self.path.endswith("/credits/balance"):
                return self.send(200 if known else 401, {"balanceUsd": 12.5})
            self.send(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.endswith("/ohttp"):
                return self.relay(body)
            if self.path == "/":   # the chain's JSON-RPC
                if not service.registry_up:
                    return self.send(502, b"<html><h1>502 Bad Gateway</h1></html>", "text/html")
                return self.send(200, {"result": "0x" + encode([TEE_INFO_ARRAY_TYPE], [[service.enclave.registry_entry()]]).hex()})
            self.send(404)   # (code logins, which DreamFort doesn't offer yet)

        def relay(self, body):
            if self.key() == NO_CREDITS_KEY:
                return self.send(*FAILURES["402"])
            if self.key() not in (API_KEY, "account-token"):
                return self.send(401, {"detail": "Invalid API key"})
            if not body:
                return self.send(400, {"detail": "OHTTP request body is required"})
            if self.headers.get("X-TEE-ID") != service.enclave.tee_id:
                return self.send(*FAILURES["retired"])
            inner, seal = service.enclave.open(body)
            kind = service.failure(service.asked(inner)[0])
            refused = FAILURES.get(kind) if kind else None
            if isinstance(refused, tuple) and kind != "504":
                service.note(refused=kind)
                return self.send(*refused, *(["text/html"] if isinstance(refused[1], bytes) else []))
            service.accepted.append(inner)
            billed = not (kind and isinstance(refused, str))
            if billed:
                service.billed.append(inner)
            service.note(accepted=inner["model"], billed=billed, kind=kind, max_tokens=inner.get("max_tokens"), effort=inner.get("reasoning_effort"),
                         tools=len(inner.get("tools") or []), asked=service.asked(inner)[0][:80])
            if kind == "504":
                return self.send(*refused, "text/html")
            self.close_connection = True
            if kind == "silent":
                return None
            chunks = [] if kind == "empty" else list(seal(service.reply(inner, kind)))
            self.send_response(200)
            self.send_header("Content-Type", "message/ohttp-chunked-res")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in chunks[:2] if kind in ("cut", "drop") else chunks:
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            if kind != "drop":   # (or the connection just ends)
                self.wfile.write(b"0\r\n\r\n")
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):   # without looking up its own name, which takes half a minute on some Macs
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def serve(service, port=0):
    return Server(("127.0.0.1", port), handler(service))


class Redirected(httpx.HTTPTransport):
    """What ghost sends anywhere goes to the stand-in."""

    def __init__(self, port):
        super().__init__()
        self.port = port

    def handle_request(self, request):
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return super().handle_request(request)


if __name__ == "__main__":
    port = int(sys.argv[1])
    if sys.argv[2] == "--bridge":
        import runpy
        sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[3])))
        import outbound
        outbound.transport = lambda url, public_only=False: Redirected(port)
        os.environ["GHOST_CHAT_API_URL"] = f"http://127.0.0.1:{port}"
        runpy.run_path(sys.argv[3], run_name="__main__")
    else:
        serve(DreamFort(sys.argv[2]), port).serve_forever()
